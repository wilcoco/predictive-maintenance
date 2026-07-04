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
        CREATE TABLE IF NOT EXISTS state(
            device TEXT PRIMARY KEY, level TEXT, drift INTEGER, dropout INTEGER,
            cusum REAL DEFAULT 0, updated REAL);
        """)
        # 스키마 진화 대응 — 없는 컬럼만 추가
        have = {r["name"] for r in c.execute("PRAGMA table_info(config)")}
        for k in CFG_KEYS:
            if k not in have:
                c.execute(f"ALTER TABLE config ADD COLUMN {k} {_sqltype(k)}")
        have_st = {r["name"] for r in c.execute("PRAGMA table_info(state)")}
        if "cusum" not in have_st:
            c.execute("ALTER TABLE state ADD COLUMN cusum REAL DEFAULT 0")
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


def get_state(c, device):
    r = c.execute("SELECT level,drift,dropout,cusum FROM state WHERE device=?", (device,)).fetchone()
    if r:
        return {"level": r["level"] or "OK", "drift": bool(r["drift"]),
                "dropout": bool(r["dropout"]), "cusum": r["cusum"] or 0.0}
    return {"level": "OK", "drift": False, "dropout": False, "cusum": 0.0}


def set_state(c, device, level, drift, dropout, cusum=0.0):
    c.execute(
        "INSERT INTO state(device,level,drift,dropout,cusum,updated) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(device) DO UPDATE SET level=?,drift=?,dropout=?,cusum=?,updated=?",
        (device, level, int(drift), int(dropout), float(cusum), time.time(),
         level, int(drift), int(dropout), float(cusum), time.time()))


def recent_window(c, device, n=600):
    rows = c.execute(
        "SELECT ts,irms FROM readings WHERE device=? ORDER BY ts DESC LIMIT ?",
        (device, n)).fetchall()
    return [(r["ts"], r["irms"]) for r in reversed(rows)]


def running_count(c, device, idle_floor):
    r = c.execute("SELECT COUNT(*) n FROM readings WHERE device=? AND irms>?",
                  (device, idle_floor)).fetchone()
    return r["n"]


def list_devices(c):
    rows = c.execute("SELECT device FROM config ORDER BY grp, device").fetchall()
    devs = [r["device"] for r in rows]
    for r in c.execute("SELECT DISTINCT device FROM readings").fetchall():
        if r["device"] not in devs:
            devs.append(r["device"])
    return devs
