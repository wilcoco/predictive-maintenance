"""SQLite 데이터 레이어 — 독립 경량 스택 (외부 DB 불필요).

DB_PATH 환경변수로 저장 위치 지정 (Railway 볼륨 마운트 대응).
기본값 ./data/pdm.db — 볼륨을 /app/data 에 붙이면 재배포에도 데이터 유지.
"""
import os, sqlite3, threading, time
from contextlib import closing

DB_PATH = os.environ.get("DB_PATH", os.path.join("data", "pdm.db"))
_lock = threading.Lock()

# 설비별 기본 설정 (신품 정상치 확인 후 /api/config 로 덮어쓰기)
DEFAULTS = dict(
    label=None, grp="기타", unit="A",
    nominal=8.0, soft=9.6, hard=12.0,   # 절대 임계 (soft=조기경보, hard=트립근처)
    idle_floor=0.5,                     # 이 값 이하 = 정지/대기 → 베이스라인에서 제외
    baseline_n=200,                     # 베이스라인 = running 샘플 median 창(개수)
    drift_pct=0.15,                     # 베이스라인이 nominal 대비 +15% → 드리프트 경고
    dropout_enable=0,                   # 가동 중 급락 감지 — 연속가동 설비(히터·컴프레서 등)에만 켤 것
                                        # (기동/정지가 잦은 설비는 정상 정지를 오탐하므로 기본 OFF)
)
CFG_KEYS = list(DEFAULTS.keys())


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
        CREATE TABLE IF NOT EXISTS config(
            device TEXT PRIMARY KEY, label TEXT, grp TEXT, unit TEXT,
            nominal REAL, soft REAL, hard REAL,
            idle_floor REAL, baseline_n INTEGER, drift_pct REAL, dropout_enable INTEGER);
        CREATE TABLE IF NOT EXISTS alerts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device TEXT NOT NULL, ts REAL NOT NULL,
            kind TEXT NOT NULL, level TEXT NOT NULL, irms REAL NOT NULL, note TEXT);
        CREATE INDEX IF NOT EXISTS ix_alerts ON alerts(device, ts);
        CREATE TABLE IF NOT EXISTS state(
            device TEXT PRIMARY KEY, level TEXT, drift INTEGER, dropout INTEGER, updated REAL);
        """)
        c.commit()


def get_config(c, device):
    r = c.execute("SELECT * FROM config WHERE device=?", (device,)).fetchone()
    if r:
        d = dict(r)
        d.pop("device", None)
        return d
    d = dict(DEFAULTS)
    d["label"] = device
    return d


def ensure_config(c, device):
    r = c.execute("SELECT 1 FROM config WHERE device=?", (device,)).fetchone()
    if not r:
        d = dict(DEFAULTS)
        d["label"] = device
        c.execute(
            "INSERT INTO config(device,label,grp,unit,nominal,soft,hard,"
            "idle_floor,baseline_n,drift_pct,dropout_enable) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (device, d["label"], d["grp"], d["unit"], d["nominal"], d["soft"],
             d["hard"], d["idle_floor"], d["baseline_n"], d["drift_pct"], d["dropout_enable"]))


def set_config(c, device, patch):
    ensure_config(c, device)
    cur = get_config(c, device)
    for k in CFG_KEYS:
        if k in patch and patch[k] is not None:
            cur[k] = patch[k]
    c.execute(
        "UPDATE config SET label=?,grp=?,unit=?,nominal=?,soft=?,hard=?,"
        "idle_floor=?,baseline_n=?,drift_pct=?,dropout_enable=? WHERE device=?",
        (cur["label"], cur["grp"], cur["unit"], cur["nominal"], cur["soft"], cur["hard"],
         cur["idle_floor"], int(cur["baseline_n"]), cur["drift_pct"],
         int(cur["dropout_enable"]), device))
    return cur


def get_state(c, device):
    r = c.execute("SELECT level,drift,dropout FROM state WHERE device=?", (device,)).fetchone()
    if r:
        return {"level": r["level"] or "OK", "drift": bool(r["drift"]), "dropout": bool(r["dropout"])}
    return {"level": "OK", "drift": False, "dropout": False}


def set_state(c, device, level, drift, dropout):
    c.execute(
        "INSERT INTO state(device,level,drift,dropout,updated) VALUES(?,?,?,?,?) "
        "ON CONFLICT(device) DO UPDATE SET level=?,drift=?,dropout=?,updated=?",
        (device, level, int(drift), int(dropout), time.time(),
         level, int(drift), int(dropout), time.time()))


def recent_window(c, device, n=600):
    rows = c.execute(
        "SELECT ts,irms FROM readings WHERE device=? ORDER BY ts DESC LIMIT ?",
        (device, n)).fetchall()
    return [(r["ts"], r["irms"]) for r in reversed(rows)]


def list_devices(c):
    rows = c.execute("SELECT device FROM config ORDER BY grp, device").fetchall()
    devs = [r["device"] for r in rows]
    # 설정 없이 데이터만 들어온 device 도 포함
    extra = c.execute("SELECT DISTINCT device FROM readings").fetchall()
    for r in extra:
        if r["device"] not in devs:
            devs.append(r["device"])
    return devs
