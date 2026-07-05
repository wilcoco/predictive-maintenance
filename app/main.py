"""예방 보전 모니터 — FastAPI 백엔드 (독립 경량 스택).

외부 도구(CT/PLC 태핑 등)가 POST /ingest 로 측정값을 넣으면,
설비별 베이스라인·드리프트·드롭아웃을 판정해 경고를 기록하고 대시보드로 보여준다.
Odoo/MES 에 묶지 않음 — 설비·ERP 비침습.

실행:  uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
"""
import os, time, threading
from contextlib import closing

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db, detect, pipeline

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")
_write_lock = threading.Lock()  # SQLite 쓰기 직렬화

app = FastAPI(title="예방 보전 모니터")


@app.on_event("startup")
def _startup():
    db.init()
    if os.environ.get("SEED_DEMO", "").lower() in ("1", "true", "yes"):
        from .seed import seed_if_empty
        seed_if_empty()


# ----------------------------------------------------------------------
# 수집
# ----------------------------------------------------------------------
@app.post("/ingest")
async def ingest(req: Request):
    body = await req.json()
    device = str(body.get("device", "FGP-L2"))
    irms = float(body["irms"])
    ts = float(body.get("ts") or time.time())
    # 선택: 측정값에 금형/제품 태그 동봉 (없으면 run_context 를 따름)
    mold = str(body["mold"]) if "mold" in body and body["mold"] is not None else None
    product = str(body["product"]) if "product" in body and body["product"] is not None else None
    with _write_lock, closing(db.db()) as c:
        ev = pipeline.process_reading(c, device, ts, irms, mold=mold, product=product)
        c.commit()
    return {"ok": True, "level": ev["level"], "baseline": ev["baseline"],
            "drift": ev["drift"], "dropout": ev["dropout"],
            "mold": ev["mold_id"], "product": ev["product_id"],
            "regime_learned": ev["regime_learned"]}


# ----------------------------------------------------------------------
# 마스터 sync (MES/ERP → 이 앱, 받기 전용) + 가동 컨텍스트(장착/이관)
# ----------------------------------------------------------------------
@app.post("/api/master/sync")
async def master_sync(req: Request):
    """회사 MES/ERP 가 마스터를 밀어넣는 단일 엔드포인트 (upsert).
    {"equipment":[{id,name,grp,...}], "molds":[{id,name,...}],
     "products":[{id,name,customer,...}], "bom":[{product_id,mold_id,equipment_id?}]}"""
    payload = await req.json()
    with _write_lock, closing(db.db()) as c:
        counts = db.upsert_masters(c, payload)
        c.commit()
    return {"ok": True, "upserted": counts}


@app.get("/api/master")
def master(kind: str = ""):
    with closing(db.db()) as c:
        out = {}
        if kind in ("", "equipment"):
            out["equipment"] = [dict(r) for r in c.execute(
                "SELECT id,name,grp FROM equipment ORDER BY grp,id")]
        if kind in ("", "molds"):
            rows = []
            for r in c.execute("SELECT id,name FROM molds ORDER BY id"):
                m = dict(r)
                cur = c.execute("SELECT device,since FROM run_context WHERE mold_id=?",
                                (m["id"],)).fetchone()
                m["current_device"] = cur["device"] if cur else None
                m["since"] = cur["since"] if cur else None
                m["devices_ran"] = [x["device"] for x in c.execute(
                    "SELECT DISTINCT device FROM run_sessions WHERE mold_id=?", (m["id"],))]
                a = c.execute("SELECT COUNT(*) n FROM alerts WHERE mold_id=? AND level IN "
                              "('WARNING','ALARM')", (m["id"],)).fetchone()
                m["alerts"] = a["n"]
                rows.append(m)
            out["molds"] = rows
        if kind in ("", "products"):
            out["products"] = [dict(r) for r in c.execute(
                "SELECT id,name,customer FROM products ORDER BY id")]
        if kind in ("", "bom"):
            out["bom"] = [dict(r) for r in c.execute(
                "SELECT product_id,mold_id,equipment_id FROM bom")]
    return out


@app.post("/api/context")
async def context_set(req: Request):
    """장착/이관 등록: {"device":"INJ-1","mold":"MOLD-A","product":"PROD-100"}.
    금형을 다른 설비/현장으로 옮기면 새 device 로 다시 호출 — 이력은 세션으로 남고
    새 (설비×금형) 레짐은 자동 재학습된다."""
    b = await req.json()
    device = str(b["device"])
    mold = str(b.get("mold") or "")
    product = str(b.get("product") or "")
    with _write_lock, closing(db.db()) as c:
        db.ensure_config(c, device)
        # BOM 정합 확인 (경고만 — 마스터는 MES/ERP 소관이므로 차단하지 않음)
        bom_ok = True
        if mold and product:
            bom_ok = bool(c.execute(
                "SELECT 1 FROM bom WHERE product_id=? AND mold_id=?",
                (product, mold)).fetchone())
        ctx = db.set_context(c, device, mold, product)
        c.commit()
    return {"ok": True, "context": ctx,
            "bom_match": bom_ok,
            "note": None if bom_ok else "BOM에 없는 제품–금형 조합 (마스터 확인 필요)"}


@app.get("/api/context")
def context_get(device: str):
    with closing(db.db()) as c:
        ctx = db.get_context(c, device)
        ctx["mold_name"] = db.master_name(c, "molds", ctx["mold_id"])
        ctx["product_name"] = db.master_name(c, "products", ctx["product_id"])
    return ctx


# ----------------------------------------------------------------------
# 엔티티 뷰 (분기) + 귀속 힌트 (통합)
# ----------------------------------------------------------------------
@app.get("/api/entity")
def entity(type: str, id: str):
    with closing(db.db()) as c:
        if type == "mold":
            info = c.execute("SELECT id,name,meta FROM molds WHERE id=?", (id,)).fetchone()
            cur = c.execute("SELECT device,since FROM run_context WHERE mold_id=?", (id,)).fetchone()
            sessions = [dict(r) for r in c.execute(
                "SELECT device,product_id,start_ts,end_ts FROM run_sessions "
                "WHERE mold_id=? ORDER BY start_ts DESC LIMIT 50", (id,))]
            regimes = [dict(r) for r in c.execute(
                "SELECT device,nominal,soft,learned,n FROM regimes WHERE mold_id=?", (id,))]
            by_dev = [dict(r) for r in c.execute(
                "SELECT device, COUNT(*) n, MAX(ts) last_ts FROM alerts "
                "WHERE mold_id=? AND level IN ('WARNING','ALARM') GROUP BY device", (id,))]
            # 귀속 힌트: 서로 다른 설비 2대 이상에서 경고 → 금형 원인 의심
            hint = None
            if len(by_dev) >= 2:
                hint = (f"이 금형은 서로 다른 설비 {len(by_dev)}대에서 경고 발생 — "
                        f"이상이 금형을 따라감: 금형 원인 가능성 높음 (분해·점검 권장)")
            elif len(by_dev) == 1:
                other = c.execute(
                    "SELECT COUNT(DISTINCT mold_id) n FROM alerts WHERE device=? "
                    "AND mold_id!=? AND level IN ('WARNING','ALARM')",
                    (by_dev[0]["device"], id)).fetchone()
                if other["n"] >= 1:
                    hint = (f"경고가 {by_dev[0]['device']} 한 대에서만 발생했고 그 설비는 "
                            f"다른 금형에서도 경고 이력 있음 — 설비 원인 가능성")
            return {"type": "mold", "id": id,
                    "name": info["name"] if info else None,
                    "current": dict(cur) if cur else None,
                    "sessions": sessions, "regimes": regimes,
                    "alerts_by_device": by_dev, "hint": hint}

        if type == "equipment":
            cfg_row = c.execute("SELECT label,grp FROM config WHERE device=?", (id,)).fetchone()
            ctx = db.get_context(c, id)
            regimes = [dict(r) for r in c.execute(
                "SELECT mold_id,nominal,soft,learned,n FROM regimes WHERE device=?", (id,))]
            by_mold = [dict(r) for r in c.execute(
                "SELECT mold_id, COUNT(*) n, MAX(ts) last_ts FROM alerts "
                "WHERE device=? AND level IN ('WARNING','ALARM') GROUP BY mold_id", (id,))]
            hint = None
            alerted_molds = [m for m in by_mold if m["mold_id"]]
            if len(alerted_molds) >= 2:
                hint = (f"이 설비는 서로 다른 금형 {len(alerted_molds)}종에서 경고 발생 — "
                        f"이상이 설비를 따라감: 설비(모터·구동부) 원인 가능성 높음")
            return {"type": "equipment", "id": id,
                    "label": cfg_row["label"] if cfg_row else id,
                    "context": ctx, "regimes": regimes,
                    "alerts_by_mold": by_mold, "hint": hint}

        if type == "product":
            info = c.execute("SELECT id,name,customer FROM products WHERE id=?", (id,)).fetchone()
            bom = [dict(r) for r in c.execute(
                "SELECT mold_id,equipment_id FROM bom WHERE product_id=?", (id,))]
            running = [dict(r) for r in c.execute(
                "SELECT device,mold_id,since FROM run_context WHERE product_id=?", (id,))]
            al = c.execute("SELECT COUNT(*) n FROM alerts WHERE product_id=? AND level IN "
                           "('WARNING','ALARM')", (id,)).fetchone()
            return {"type": "product", "id": id,
                    "name": info["name"] if info else None,
                    "customer": info["customer"] if info else None,
                    "bom": bom, "running_on": running, "alerts": al["n"]}
    return JSONResponse({"error": "type must be mold|equipment|product"}, status_code=400)


# ----------------------------------------------------------------------
# 조회 API
# ----------------------------------------------------------------------
def _summarize(c, device, spark_n=40):
    cfg = db.get_config(c, device)
    ctx = db.get_context(c, device)
    mold_id = ctx["mold_id"]
    # 현재 레짐(설비×금형) 기준으로 창·상태·임계를 본다
    regime = db.get_regime(c, device, mold_id)
    eff_nominal, eff_soft = cfg["nominal"], cfg["soft"]
    if regime and regime["learned"]:
        eff_nominal, eff_soft = regime["nominal"], regime["soft"]
    eff = dict(cfg); eff["nominal"], eff["soft"] = eff_nominal, eff_soft
    window = db.recent_window(c, device, n=max(600, int(cfg["baseline_n"]) + 50),
                              mold_id=mold_id)
    if window:
        ev = detect.evaluate(window, eff)
        st = db.get_state(c, device, mold_id)
        # 대시보드 레벨/플래그는 디바운스된 상태값과 일치시킨다 (경고 이력과 동일 기준)
        level, drift, dropout = st["level"], st["drift"], st["dropout"]
    else:
        ev = {"irms": None, "level": "NODATA", "baseline": None, "drift_ratio": 0.0}
        level, drift, dropout = "NODATA", False, False
    return {
        "device": device, "label": cfg["label"] or device, "grp": cfg["grp"],
        "unit": cfg["unit"], "irms": ev["irms"], "ts": window[-1][0] if window else None,
        "level": level, "baseline": ev["baseline"],
        "drift": drift, "drift_pct": round(ev["drift_ratio"] * 100, 1),
        "dropout": dropout,
        "nominal": eff_nominal, "soft": eff_soft, "hard": cfg["hard"],
        "method": cfg["method"], "learn": cfg["learn"],
        "mold": mold_id or None, "product": ctx["product_id"] or None,
        "mold_name": db.master_name(c, "molds", mold_id),
        "product_name": db.master_name(c, "products", ctx["product_id"]),
        "regime_learned": bool(regime and regime["learned"]) if mold_id else None,
        "spark": [v for _, v in window[-spark_n:]] if spark_n else [],
    }


@app.get("/api/devices")
def devices():
    with closing(db.db()) as c:
        return [_summarize(c, d) for d in db.list_devices(c)]


@app.get("/api/status")
def status(device: str = "FGP-L2"):
    with closing(db.db()) as c:
        return _summarize(c, device, spark_n=0)


@app.get("/api/readings")
def readings(device: str = "FGP-L2", minutes: int = 60):
    since = time.time() - minutes * 60
    with closing(db.db()) as c:
        rows = c.execute(
            "SELECT ts,irms FROM readings WHERE device=? AND ts>=? ORDER BY ts",
            (device, since)).fetchall()
    return [{"ts": r["ts"], "irms": r["irms"]} for r in rows]


@app.get("/api/alerts")
def get_alerts(device: str = "FGP-L2", limit: int = 30):
    with closing(db.db()) as c:
        rows = c.execute(
            "SELECT ts,kind,level,irms,note,mold_id,product_id FROM alerts "
            "WHERE device=? ORDER BY ts DESC LIMIT ?",
            (device, limit)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/config")
def read_config(device: str = "FGP-L2"):
    with closing(db.db()) as c:
        cfg = db.get_config(c, device)
    cfg["device"] = device
    return cfg


@app.post("/api/config")
async def write_config(req: Request):
    b = await req.json()
    device = str(b.get("device", "FGP-L2"))
    patch = {k: b[k] for k in db.CFG_KEYS if k in b and b[k] is not None}
    with _write_lock, closing(db.db()) as c:
        cur = db.set_config(c, device, patch)  # 타입 캐스팅은 db.cast 가 처리
        c.commit()
    cur["device"] = device
    return {"ok": True, "config": cur}


@app.get("/health")
def health():
    return {"ok": True, "ts": time.time()}


# ----------------------------------------------------------------------
# 정적 대시보드
# ----------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
