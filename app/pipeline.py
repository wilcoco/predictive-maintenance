"""수집→판정→경고 기록 파이프라인 (ingest 와 데모 시드가 공유).

한 건의 측정값을 받아: 적재 → 베이스라인/드리프트/드롭아웃 판정 →
상태 '악화' 전이에만 alert 1건 기록. 커밋은 호출자가 한다.
"""
import os, json, urllib.request
from . import db, detect

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")  # KakaoWork/Slack incoming webhook (선택)


def _notify(text):
    if not WEBHOOK_URL:
        return
    try:
        data = json.dumps({"text": text}).encode()
        req = urllib.request.Request(WEBHOOK_URL, data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=4)
    except Exception:
        pass  # 알림 실패가 수집을 막지 않도록


def _emit(c, device, ts, kind, level, irms, note, notify=True):
    c.execute("INSERT INTO alerts(device,ts,kind,level,irms,note) VALUES(?,?,?,?,?,?)",
              (device, ts, kind, level, irms, note))
    if notify:
        _notify(f"[예방보전] {device} · {kind}/{level} · {irms:.2f} — {note}")


def _learn(c, device, cfg, run_vals, ts, notify):
    """정상치 자동학습: 충분한 running 샘플이 모이면 nominal/soft/hard 확정."""
    import statistics
    med = round(statistics.median(run_vals), 3)
    patch = dict(nominal=med, soft=round(med * 1.2, 3), hard=round(med * 1.5, 3), learn=0)
    cfg.update(patch)
    db.set_config(c, device, patch)
    _emit(c, device, ts, "LEARN", "OK", med,
          f"정상치 자동학습 완료 — nominal={med}, soft={patch['soft']}, hard={patch['hard']} "
          f"(hard 는 임시값, 실제 트립값 확인 후 보정 권장)", notify)


def process_reading(c, device, ts, irms, notify=True):
    db.ensure_config(c, device)
    c.execute("INSERT INTO readings(device,ts,irms) VALUES(?,?,?)", (device, ts, irms))
    cfg = db.get_config(c, device)
    window = db.recent_window(c, device, n=max(600, int(cfg["baseline_n"]) + 50))
    ev = detect.evaluate(window, cfg)
    st = db.get_state(c, device)

    running = irms > cfg["idle_floor"]
    run_vals = detect.running_values(window, cfg["idle_floor"])
    level_state, drift_state, dropout_state, cusum_state = \
        st["level"], st["drift"], st["dropout"], st["cusum"]

    if running:
        # 0) 학습모드: 정상치가 충분히 모이면 임계 자동 확정
        if cfg.get("learn") and len(run_vals) >= int(cfg["baseline_n"]):
            _learn(c, device, cfg, run_vals, ts, notify)

        # 1) 선택된 방식으로 추세 경고 판정 (히스테리시스 포함)
        prev_warn = st["level"] in ("WARNING", "ALARM")
        warn, cusum_state, info = detect.trend_state(
            cfg, ev["baseline"], run_vals, prev_warn, st["cusum"])
        # 2) 레벨: ALARM=순간 피크(안전), WARNING=추세 경고
        if irms >= cfg["hard"] or (st["level"] == "ALARM" and irms >= cfg["hard"] * (1 - detect.HYSTERESIS)):
            new_level = "ALARM"
        elif warn:
            new_level = "WARNING"
        else:
            new_level = "OK"

        # 3) 악화 전이에만 기록
        if detect.LEVEL_RANK.get(new_level, 0) > detect.LEVEL_RANK.get(st["level"], 0):
            note = _reason(cfg, ev, info)
            if new_level == "ALARM":
                w = c.execute(
                    "SELECT ts FROM alerts WHERE device=? AND level='WARNING' AND ts<=? "
                    "ORDER BY ts DESC LIMIT 1", (device, ts)).fetchone()
                if w:
                    note += f" · 경고 후 {(ts - w['ts'])/60.0:.0f}분 만에 알람 (리드타임)"
            _emit(c, device, ts, "LEVEL", new_level, irms, note, notify)
        level_state = new_level
        drift_state = ev["drift"]
        dropout_state = False  # 가동 재개 = 드롭아웃 해제
    else:
        # 정지 중: 레벨·추세·CUSUM 보존. 드롭아웃만 이 순간에 판정.
        if ev["dropout"] and not st["dropout"]:
            _emit(c, device, ts, "DROPOUT", "ALARM", irms,
                  "가동 중 신호 급락 — 단선/급정지 의심", notify)
        dropout_state = st["dropout"] or ev["dropout"]
        if dropout_state:
            level_state = "ALARM"

    db.set_state(c, device, level_state, drift_state, dropout_state, cusum_state)
    return ev


def _reason(cfg, ev, info):
    m = cfg.get("method", "absolute")
    if m == "cusum":
        return f"CUSUM {info.get('cusum')} > {info.get('cusum_h')} — 지속 상승 추세 (베이스라인 {ev['baseline']})"
    if m == "zscore":
        return f"로버스트 z={info.get('z')} ≥ {cfg['z_k']} — 정상분포 이탈 (베이스라인 {ev['baseline']})"
    if m == "drift":
        return f"베이스라인 {ev['baseline']} — nominal 대비 +{ev['drift_ratio']*100:.0f}% (임계 {cfg['drift_pct']*100:.0f}%)"
    return f"베이스라인 {ev['baseline']} ≥ soft {cfg['soft']}"
