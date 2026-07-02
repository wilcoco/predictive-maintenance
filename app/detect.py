"""예방 탐지 엔진 — 설비 종류 무관 재사용 (독립 모듈).

두 가지 탐지기:
  · 드리프트(점진)  — running 샘플의 베이스라인이 nominal 대비 서서히 상승
  · 드롭아웃(급변)  — 가동 중이던 신호가 갑자기 0 근처로 급락 (단선/급사)

레벨(절대 임계): OK / WARNING(≥soft) / ALARM(≥hard). 하드 트립은 고정 안전망,
소프트/드리프트는 조기경보. 상태가 '악화'될 때만 alert 1건 기록 → 스팸 방지.
"""
import statistics

LEVEL_RANK = {"OK": 0, "WARNING": 1, "ALARM": 2}
HYSTERESIS = 0.05  # 이탈 임계 = 진입 임계 × (1-0.05). 임계선 근처 채터링(반복 경고) 방지.


def _median(xs):
    return statistics.median(xs) if xs else None


def running_values(window, idle_floor):
    """정지/대기(idle_floor 이하)를 뺀 '가동 중' 전류만."""
    return [v for _, v in window if v is not None and v > idle_floor]


def baseline(window, cfg):
    run = running_values(window, cfg["idle_floor"])
    n = int(cfg["baseline_n"])
    b = _median(run[-n:]) if run else None
    return b if b is not None else cfg["nominal"]


def level_for(irms, cfg):
    if irms is None:
        return "NODATA"
    if irms >= cfg["hard"]:
        return "ALARM"
    if irms >= cfg["soft"]:
        return "WARNING"
    return "OK"


def classify(irms, base, cfg, prev):
    """레벨 판정 (디바운스).

    · WARNING = 베이스라인(가동 running 중앙값)이 soft 초과 — 매끄러운 추세라 채터링 없음.
                순간값 노이즈가 아니라 '추세'를 본다는 원칙 그대로.
    · ALARM   = 순간 전류가 hard 초과 — 안전 트립이라 모든 피크를 잡아야 함.
    이탈은 임계×(1-H) 아래에서만 → 임계선 근처 반복 경고 방지."""
    if irms is None:
        return "NODATA"
    soft, hard, h = cfg["soft"], cfg["hard"], HYSTERESIS
    if base is None:
        base = irms
    # ALARM: 순간 피크(안전), 이탈 히스테리시스
    if irms >= hard or (prev == "ALARM" and irms >= hard * (1 - h)):
        return "ALARM"
    # WARNING: 베이스라인 추세, 이탈 히스테리시스
    if base >= soft or (prev in ("WARNING", "ALARM") and base >= soft * (1 - h)):
        return "WARNING"
    return "OK"


def drift_ratio(base, cfg):
    nom = cfg["nominal"] or 0
    if nom <= 0:
        return 0.0
    return base / nom - 1.0


def is_dropout(window, cfg):
    """직전까지 가동(부하 있음) 중이었는데 마지막 값이 idle 이하로 급락."""
    if not cfg.get("dropout_enable"):
        return False
    if len(window) < 3:
        return False
    last = window[-1][1]
    prev = [v for _, v in window[-6:-1]]
    if not prev:
        return False
    was_running = max(prev) > max(cfg["idle_floor"] * 3, cfg["soft"] * 0.4)
    return was_running and last is not None and last < cfg["idle_floor"]


def evaluate(window, cfg):
    """window: [(ts,irms), ...] 오래된→최신. 마지막이 현재 판정 대상."""
    last = window[-1][1] if window else None
    base = baseline(window, cfg)
    lvl = level_for(last, cfg)
    dr = drift_ratio(base, cfg)
    drift_flag = last is not None and last > cfg["idle_floor"] and dr >= cfg["drift_pct"]
    dropout_flag = is_dropout(window, cfg)
    return {
        "irms": last,
        "level": lvl,
        "baseline": round(base, 3) if base is not None else None,
        "drift_ratio": round(dr, 4),
        "drift": bool(drift_flag),
        "dropout": bool(dropout_flag),
    }
