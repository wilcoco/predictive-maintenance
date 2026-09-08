"""SQLite 데이터 레이어 — 독립 경량 스택 (외부 DB 불필요).

DB_PATH 환경변수로 저장 위치 지정 (Railway 볼륨 마운트 대응).
기본값 ./data/pdm.db — 볼륨을 /app/data 에 붙이면 재배포에도 데이터 유지.
"""
import os, sqlite3, threading, time
from contextlib import closing

DB_PATH = os.environ.get("DB_PATH", os.path.join("data", "pdm.db"))
_lock = threading.Lock()

# 설비별 설정. 마법사(질문 몇 개)로 채우거나 /api/config 로 직접 설정.
DEFAULTS = dict(
    label=None, grp="기타", unit="A",
    nominal=8.0, soft=9.6, hard=12.0,   # 절대 임계 (soft=조기경보, hard=트립근처)
    idle_floor=0.5,                     # 이 값 이하 = 정지/대기 → 베이스라인에서 제외
    baseline_n=200,                     # 베이스라인 = running 샘플 median 창(개수)
    drift_pct=0.15,                     # (drift 방식) nominal 대비 +15% → 경고
    dropout_enable=0,                   # 급락(단선/급정지) 감지 — 연속가동 설비에만
    method="absolute",                  # 추세 탐지 방식: absolute|drift|cusum|zscore
    cusum_k=0.5,                        # (cusum) 허용 슬랙 = k·σ
    cusum_h=5.0,                        # (cusum) 결정 임계 = h·σ
    z_k=3.5,                            # (zscore) 로버스트 z 경보 임계
    learn=0,                            # 1이면 정상치 자동학습 후 nominal/soft/hard 확정
)
CFG_KEYS = list(DEFAULTS.keys())
_TEXT = {"label", "grp", "unit", "method"}
_INT = {"baseline_n", "dropout_enable", "learn"}


def _sqltype(k):
    if k in _TEXT: return "TEXT"
    if k in _INT: return "INTEGER"
    return "REAL"


def cast(k, v):
    if v is None: return None
    if k in _TEXT: return str(v)
    if k in _INT: return int(float(v))
    return float(v)


def db():
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init():
    with closing(db()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS readings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device TEXT NOT NULL, ts REAL NOT NULL, irms REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_readings ON readings(device, ts);
        CREATE TABLE IF NOT EXISTS config(device TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS alerts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device TEXT NOT NULL, ts REAL NOT NULL,
            kind TEXT NOT NULL, level TEXT NOT NULL, irms REAL NOT NULL, note TEXT);
        CREATE INDEX IF NOT EXISTS ix_alerts ON alerts(device, ts);

        -- ── 마스터 (MES/ERP 에서 sync 로 받기 전용 — 이 앱은 생성·관리하지 않음) ──
        CREATE TABLE IF NOT EXISTS equipment(
            id TEXT PRIMARY KEY, name TEXT, grp TEXT, meta TEXT, updated REAL);
        CREATE TABLE IF NOT EXISTS molds(
            id TEXT PRIMARY KEY, name TEXT, meta TEXT, updated REAL);
        CREATE TABLE IF NOT EXISTS products(
            id TEXT PRIMARY KEY, name TEXT, customer TEXT, meta TEXT, updated REAL);
        CREATE TABLE IF NOT EXISTS bom(          -- 제품–금형–(호환)설비 관계
            product_id TEXT NOT NULL, mold_id TEXT NOT NULL,
            equipment_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(product_id, mold_id, equipment_id));

        -- ── 가동 컨텍스트: "지금 설비 M 에 금형 K 로 제품 P" (이관=컨텍스트 변경) ──
        CREATE TABLE IF NOT EXISTS run_context(
            device TEXT PRIMARY KEY, mold_id TEXT DEFAULT '', product_id TEXT DEFAULT '',
            since REAL);
        CREATE TABLE IF NOT EXISTS run_sessions(   -- 장착 이력 = 금형 이동/이관 이력
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device TEXT NOT NULL, mold_id TEXT DEFAULT '', product_id TEXT DEFAULT '',
            start_ts REAL, end_ts REAL);

        -- ── 레짐: (설비×금형) 조합별 정상치 — 통합 판정의 핵심 ──
        CREATE TABLE IF NOT EXISTS regimes(
            device TEXT NOT NULL, mold_id TEXT NOT NULL DEFAULT '',
            nominal REAL, soft REAL, learned INTEGER DEFAULT 0, n INTEGER DEFAULT 0,
            created REAL, PRIMARY KEY(device, mold_id));
        """)
        # 스키마 진화 대응 — 없는 컬럼만 추가
        have = {r["name"] for r in c.execute("PRAGMA table_info(config)")}
        for k in CFG_KEYS:
            if k not in have:
                c.execute(f"ALTER TABLE config ADD COLUMN {k} {_sqltype(k)}")
        for tbl in ("readings", "alerts"):
            cols = {r["name"] for r in c.execute(f"PRAGMA table_info({tbl})")}
            for k in ("mold_id", "product_id"):
                if k not in cols:
                    c.execute(f"ALTER TABLE {tbl} ADD COLUMN {k} TEXT DEFAULT ''")
        # state: (device×mold) 복합키로 재구성 — 레벨/CUSUM 은 레짐별 (일시 상태라 드롭 무해)
        st_cols = {r["name"] for r in c.execute("PRAGMA table_info(state)")}
        if st_cols and "mold_id" not in st_cols:
            c.execute("DROP TABLE state")
        c.execute("""
        CREATE TABLE IF NOT EXISTS state(
            device TEXT NOT NULL, mold_id TEXT NOT NULL DEFAULT '',
            level TEXT, drift INTEGER, dropout INTEGER,
            cusum REAL DEFAULT 0, updated REAL, PRIMARY KEY(device, mold_id))""")
        # det: 멀티 탐지기 스냅샷(JSON) — 각 탐지기 발동상태·지표. 히스테리시스 prev + 화면 표시.
        st_cols2 = {r["name"] for r in c.execute("PRAGMA table_info(state)")}
        if "det" not in st_cols2:
            c.execute("ALTER TABLE state ADD COLUMN det TEXT")
        c.commit()


def get_config(c, device):
    r = c.execute("SELECT * FROM config WHERE device=?", (device,)).fetchone()
    d = dict(DEFAULTS)
    d["label"] = device
    if r:
        row = dict(r); row.pop("device", None)
        for k in CFG_KEYS:
            if row.get(k) is not None:
                d[k] = row[k]
    return d


def ensure_config(c, device):
    if not c.execute("SELECT 1 FROM config WHERE device=?", (device,)).fetchone():
        d = dict(DEFAULTS); d["label"] = device
        cols = ",".join(["device"] + CFG_KEYS)
        ph = ",".join(["?"] * (1 + len(CFG_KEYS)))
        c.execute(f"INSERT INTO config({cols}) VALUES({ph})",
                  [device] + [d[k] for k in CFG_KEYS])


def set_config(c, device, patch):
    ensure_config(c, device)
    cur = get_config(c, device)
    for k in CFG_KEYS:
        if k in patch and patch[k] is not None:
            cur[k] = cast(k, patch[k])
    sets = ",".join(f"{k}=?" for k in CFG_KEYS)
    c.execute(f"UPDATE config SET {sets} WHERE device=?",
              [cur[k] for k in CFG_KEYS] + [device])
    return cur


def get_state(c, device, mold_id=""):
    r = c.execute("SELECT level,drift,dropout,cusum,det FROM state WHERE device=? AND mold_id=?",
                  (device, mold_id)).fetchone()
    if r:
        det = {}
        if r["det"]:
            try:
                det = _json.loads(r["det"])
            except Exception:
                det = {}
        return {"level": r["level"] or "OK", "drift": bool(r["drift"]),
                "dropout": bool(r["dropout"]), "cusum": r["cusum"] or 0.0, "det": det}
    return {"level": "OK", "drift": False, "dropout": False, "cusum": 0.0, "det": {}}


def set_state(c, device, level, drift, dropout, cusum=0.0, mold_id="", det=None):
    det_json = _json.dumps(det, ensure_ascii=False) if det is not None else None
    c.execute(
        "INSERT INTO state(device,mold_id,level,drift,dropout,cusum,det,updated) "
        "VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(device,mold_id) DO UPDATE SET level=?,drift=?,dropout=?,cusum=?,det=?,updated=?",
        (device, mold_id, level, int(drift), int(dropout), float(cusum), det_json, time.time(),
         level, int(drift), int(dropout), float(cusum), det_json, time.time()))


def recent_window(c, device, n=600, mold_id=None):
    """mold_id=None → 전체(레짐 무관), 문자열('' 포함) → 그 레짐의 데이터만."""
    if mold_id is None:
        rows = c.execute(
            "SELECT ts,irms FROM readings WHERE device=? ORDER BY ts DESC LIMIT ?",
            (device, n)).fetchall()
    else:
        rows = c.execute(
            "SELECT ts,irms FROM readings WHERE device=? AND mold_id=? ORDER BY ts DESC LIMIT ?",
            (device, mold_id, n)).fetchall()
    return [(r["ts"], r["irms"]) for r in reversed(rows)]


# ----------------------------------------------------------------------
# 마스터 (MES/ERP sync 수신 전용)
# ----------------------------------------------------------------------
import json as _json

def upsert_masters(c, payload):
    """{"equipment":[...], "molds":[...], "products":[...], "bom":[...]} 일괄 upsert."""
    now = time.time()
    counts = {}
    for row in payload.get("equipment", []):
        meta = _json.dumps({k: v for k, v in row.items() if k not in ("id", "name", "grp")},
                           ensure_ascii=False)
        c.execute("INSERT INTO equipment(id,name,grp,meta,updated) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET name=?,grp=?,meta=?,updated=?",
                  (row["id"], row.get("name"), row.get("grp"), meta, now,
                   row.get("name"), row.get("grp"), meta, now))
        # 모니터링 중인 설비면 라벨·그룹을 마스터 기준으로 동기화
        if c.execute("SELECT 1 FROM config WHERE device=?", (row["id"],)).fetchone():
            if row.get("name"):
                c.execute("UPDATE config SET label=? WHERE device=?", (row["name"], row["id"]))
            if row.get("grp"):
                c.execute("UPDATE config SET grp=? WHERE device=?", (row["grp"], row["id"]))
    counts["equipment"] = len(payload.get("equipment", []))
    for row in payload.get("molds", []):
        meta = _json.dumps({k: v for k, v in row.items() if k not in ("id", "name")},
                           ensure_ascii=False)
        c.execute("INSERT INTO molds(id,name,meta,updated) VALUES(?,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET name=?,meta=?,updated=?",
                  (row["id"], row.get("name"), meta, now, row.get("name"), meta, now))
    counts["molds"] = len(payload.get("molds", []))
    for row in payload.get("products", []):
        meta = _json.dumps({k: v for k, v in row.items() if k not in ("id", "name", "customer")},
                           ensure_ascii=False)
        c.execute("INSERT INTO products(id,name,customer,meta,updated) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET name=?,customer=?,meta=?,updated=?",
                  (row["id"], row.get("name"), row.get("customer"), meta, now,
                   row.get("name"), row.get("customer"), meta, now))
    counts["products"] = len(payload.get("products", []))
    for row in payload.get("bom", []):
        c.execute("INSERT OR IGNORE INTO bom(product_id,mold_id,equipment_id) VALUES(?,?,?)",
                  (row["product_id"], row["mold_id"], row.get("equipment_id", "")))
    counts["bom"] = len(payload.get("bom", []))
    return counts


def master_name(c, table, _id):
    if not _id:
        return None
    r = c.execute(f"SELECT name FROM {table} WHERE id=?", (_id,)).fetchone()
    return r["name"] if r else None


# ----------------------------------------------------------------------
# 가동 컨텍스트 (장착/이관)
# ----------------------------------------------------------------------
def get_context(c, device):
    r = c.execute("SELECT mold_id,product_id,since FROM run_context WHERE device=?",
                  (device,)).fetchone()
    if r:
        return {"mold_id": r["mold_id"] or "", "product_id": r["product_id"] or "",
                "since": r["since"]}
    return {"mold_id": "", "product_id": "", "since": None}


def set_context(c, device, mold_id, product_id, ts=None):
    """컨텍스트 변경 = 이전 세션 종료 + 새 세션 시작. 금형 이동/이관의 기록 단위."""
    ts = ts or time.time()
    cur = get_context(c, device)
    if cur["mold_id"] == (mold_id or "") and cur["product_id"] == (product_id or ""):
        return cur  # 변화 없음
    c.execute("UPDATE run_sessions SET end_ts=? WHERE device=? AND end_ts IS NULL",
              (ts, device))
    c.execute("INSERT INTO run_sessions(device,mold_id,product_id,start_ts) VALUES(?,?,?,?)",
              (device, mold_id or "", product_id or "", ts))
    c.execute("INSERT INTO run_context(device,mold_id,product_id,since) VALUES(?,?,?,?) "
              "ON CONFLICT(device) DO UPDATE SET mold_id=?,product_id=?,since=?",
              (device, mold_id or "", product_id or "", ts,
               mold_id or "", product_id or "", ts))
    return {"mold_id": mold_id or "", "product_id": product_id or "", "since": ts}


# ----------------------------------------------------------------------
# 레짐: (설비×금형) 조합별 정상치
# ----------------------------------------------------------------------
def get_regime(c, device, mold_id):
    r = c.execute("SELECT nominal,soft,learned,n FROM regimes WHERE device=? AND mold_id=?",
                  (device, mold_id)).fetchone()
    return dict(r) if r else None


def ensure_regime(c, device, mold_id, cfg):
    """새 (설비×금형) 조합 등장 시 레짐 행 생성 — 임계는 학습 전(미확정) 상태."""
    if get_regime(c, device, mold_id) is None:
        c.execute("INSERT INTO regimes(device,mold_id,nominal,soft,learned,n,created) "
                  "VALUES(?,?,?,?,0,0,?)",
                  (device, mold_id, cfg["nominal"], cfg["soft"], time.time()))
    return get_regime(c, device, mold_id)


def learn_regime(c, device, mold_id, nominal, soft, n):
    c.execute("UPDATE regimes SET nominal=?,soft=?,learned=1,n=? WHERE device=? AND mold_id=?",
              (nominal, soft, n, device, mold_id))


def list_devices(c):
    rows = c.execute("SELECT device FROM config ORDER BY grp, device").fetchall()
    devs = [r["device"] for r in rows]
    for r in c.execute("SELECT DISTINCT device FROM readings").fetchall():
        if r["device"] not in devs:
            devs.append(r["device"])
    return devs
