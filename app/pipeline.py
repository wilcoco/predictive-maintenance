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


def process_reading(c, device, ts, irms, notify=True):
    db.ensure_config(c, device)
    c.execute("INSERT INTO readings(device,ts,irms) VALUES(?,?,?)", (device, ts, irms))
    cfg = db.get_config(c, device)
    window = db.recent_window(c, device, n=max(600, int(cfg["baseline_n"]) + 50))
    ev = detect.evaluate(window, cfg)
    st = db.get_state(c, device)

    running = irms > cfg["idle_floor"]
    level_state, drift_state, dropout_state = st["level"], st["drift"], st["dropout"]

    if running:
        # 가동 중일 때만 건강 레벨을 판정한다.
        # (정지 구간에 상태를 리셋하면, 재가동마다 경고가 재발되는 스팸이 생김)
        new_level = detect.classify(irms, ev["baseline"], cfg, st["level"])  # 추세 기반 + 디바운스
        # 1) 절대 레벨 악화 (OK→WARNING→ALARM) 전이에만 기록
        if detect.LEVEL_RANK.get(new_level, 0) > detect.LEVEL_RANK.get(st["level"], 0):
            note = f"임계 초과 (soft={cfg['soft']}, hard={cfg['hard']})"
            if new_level == "ALARM":
                w = c.execute(
                    "SELECT ts FROM alerts WHERE device=? AND level='WARNING' AND ts<=? "
                    "ORDER BY ts DESC LIMIT 1", (device, ts)).fetchone()
                if w:
                    note += f" · 경고 후 {(ts - w['ts'])/60.0:.0f}분 만에 알람 (리드타임)"
            _emit(c, device, ts, "LEVEL", new_level, irms, note, notify)
        level_state = new_level

        # 2) 드리프트 진입 (한 번만)
        if ev["drift"] and not st["drift"]:
            _emit(c, device, ts, "DRIFT", "WARNING", irms,
                  f"베이스라인 {ev['baseline']} — nominal 대비 +{ev['drift_ratio']*100:.0f}% 상승 추세", notify)
        drift_state = ev["drift"]
        dropout_state = False  # 가동 재개 = 드롭아웃 해제
    else:
        # 정지 중: 레벨·드리프트는 보존. 드롭아웃(단선/급정지)만 이 순간에 판정.
        if ev["dropout"] and not st["dropout"]:
            _emit(c, device, ts, "DROPOUT", "ALARM", irms,
                  "가동 중 신호 급락 — 단선/급정지 의심", notify)
        # 재가동 전까지 래치 — 죽은 설비가 정상(OK)으로 보이지 않게
        dropout_state = st["dropout"] or ev["dropout"]
        if dropout_state:
            level_state = "ALARM"

    db.set_state(c, device, level_state, drift_state, dropout_state)
    return ev
