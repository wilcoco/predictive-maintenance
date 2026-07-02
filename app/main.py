"""예방 보전 모니터 — FastAPI 백엔드 (독립 경량 스택).

외부 도구(CT/PLC 태핑 등)가 POST /ingest 로 측정값을 넣으면,
설비별 베이스라인·드리프트·드롭아웃을 판정해 경고를 기록하고 대시보드로 보여준다.
Odoo/MES 에 묶지 않음 — 설비·ERP 비침습.

실행:  uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
"""
import os, time, threading
from contextlib import closing

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
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
    with _write_lock, closing(db.db()) as c:
        ev = pipeline.process_reading(c, device, ts, irms)
        c.commit()
    return {"ok": True, "level": ev["level"], "baseline": ev["baseline"],
            "drift": ev["drift"], "dropout": ev["dropout"]}


# ----------------------------------------------------------------------
# 조회 API
# ----------------------------------------------------------------------
def _summarize(c, device, spark_n=40):
    cfg = db.get_config(c, device)
    window = db.recent_window(c, device, n=max(600, int(cfg["baseline_n"]) + 50))
    if window:
        ev = detect.evaluate(window, cfg)
        st = db.get_state(c, device)
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
        "nominal": cfg["nominal"], "soft": cfg["soft"], "hard": cfg["hard"],
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
            "SELECT ts,kind,level,irms,note FROM alerts WHERE device=? ORDER BY ts DESC LIMIT ?",
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
    patch = {}
    for k in ("label", "grp", "unit"):
        if k in b:
            patch[k] = str(b[k])
    for k in ("nominal", "soft", "hard", "idle_floor", "drift_pct"):
        if k in b and b[k] is not None:
            patch[k] = float(b[k])
    if "baseline_n" in b and b["baseline_n"] is not None:
        patch["baseline_n"] = int(b["baseline_n"])
    if "dropout_enable" in b and b["dropout_enable"] is not None:
        patch["dropout_enable"] = 1 if b["dropout_enable"] else 0
    with _write_lock, closing(db.db()) as c:
        cur = db.set_config(c, device, patch)
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
