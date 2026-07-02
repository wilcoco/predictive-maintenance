"""데모 데이터 생성 — 빈 DB에 다중 설비 합성 이력을 심는다.

SEED_DEMO=1 로 배포하면 대시보드가 즉시 살아있는 상태로 보인다 (실데이터 전).
실제 운영에서는 켜지 말 것 — 외부 도구가 /ingest 로 실측을 넣는다.

시나리오:
  · FGP-L2      점진 드리프트 → soft 초과 (WARNING + DRIFT)
  · HEATER-3    가동 중 급락 → DROPOUT
  · 나머지      정상 범위
"""
import math, random, time
from contextlib import closing
from . import db, pipeline

STEP = 30          # 초
N = 300            # 포인트 (≈2.5시간)

# device, label, group, unit, nominal, soft, hard, dropout_enable
DEVICES = [
    ("FGP-L2",    "경화제 FGP 서보",   "도장", "A", 8.0,  9.6,  12.0, 0),
    ("CONV-L2",   "L2 컨베어 구동",    "도장", "A", 12.0, 15.0, 19.0, 0),
    ("COMP-01",   "컴프레서 #1",       "유틸", "A", 30.0, 38.0, 48.0, 0),
    ("CWP-01",    "냉각수 펌프",       "유틸", "A", 18.0, 23.0, 29.0, 0),
    ("INJ-HYD-1", "1호기 유압펌프",    "사출", "A", 45.0, 55.0, 68.0, 0),
    ("INJ-SV-1",  "1호기 사출서보",    "사출", "%", 40.0, 52.0, 65.0, 0),
    ("HEATER-3",  "3호기 노즐히터",    "사출", "A", 6.0,  9.0,  11.0, 1),  # 연속가동 → 드롭아웃 ON
]


def _value(dev, prog, i, rnd, nominal, soft):
    """설비별 합성 전류(또는 토크). prog: 0→1 진행도."""
    # 간헐 정지 (도장 라인) — 베이스라인 상태게이팅 시연
    if dev in ("FGP-L2", "CONV-L2") and (i % 90) >= 84:
        return round(max(0.0, rnd.gauss(0.1, 0.05)), 3)

    if dev == "FGP-L2":
        # nominal 8 → 며칠 상당의 우상향을 압축: 드리프트(+15%) 먼저, 끝에서 soft 도 초과
        base = nominal * (1.0 + 0.36 * prog)
        v = base + math.sin(i / 9.0) * 0.35 + rnd.gauss(0, 0.25)
    elif dev == "HEATER-3":
        if prog > 0.9:
            return 0.0  # 급락 (단선) → DROPOUT
        v = nominal + math.sin(i / 7.0) * 0.3 + rnd.gauss(0, 0.15)
    else:
        amp = nominal * 0.06
        v = nominal + math.sin(i / 11.0) * amp + rnd.gauss(0, amp * 0.6)
    return round(max(0.0, v), 3)


def generate(start_ts=None):
    if start_ts is None:
        start_ts = time.time() - N * STEP   # 데이터가 '지금'에서 끝나도록
    with closing(db.db()) as c:
        for dev, label, grp, unit, nominal, soft, hard, dropout in DEVICES:
            db.set_config(c, dev, dict(label=label, grp=grp, unit=unit,
                                       nominal=nominal, soft=soft, hard=hard,
                                       dropout_enable=dropout))
        c.commit()
        for idx, (dev, label, grp, unit, nominal, soft, hard, dropout) in enumerate(DEVICES):
            rnd = random.Random(1000 + idx)
            for i in range(N):
                ts = start_ts + i * STEP
                v = _value(dev, i / (N - 1), i, rnd, nominal, soft)
                pipeline.process_reading(c, dev, ts, v, notify=False)
        c.commit()


def seed_if_empty(start_ts=None):
    with closing(db.db()) as c:
        n = c.execute("SELECT COUNT(*) n FROM readings").fetchone()["n"]
    if n == 0:
        generate(start_ts)
        return True
    return False


if __name__ == "__main__":
    db.init()
    print("seeded" if seed_if_empty() else "already has data")
