"""수집→판정→경고 기록 파이프라인 (ingest 와 데모 시드가 공유).

한 건의 측정값을 받아: 컨텍스트(금형·제품) 해석 → 적재 →
(설비×금형) 레짐별 베이스라인/추세/드롭아웃 판정 →
상태 '악화' 전이에만 alert 1건 기록. 커밋은 호출자가 한다.

분기/통합 원칙:
  · nominal/soft = (설비×금형) 레짐별 — 금형이 바뀌면 정상 부하가 다르다.
  · hard = 설비 레벨 유지 — 과부하 트립은 설비(모터) 물성.
  · 새 레짐(이관·금형 교체 직후)은 자동 학습이 끝날 때까지 추세경고 보류
    (교체 직후 오탐 방지). 하드 ALARM 은 항상 살아 있음.
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


def _emit(c, device, ts, kind, level, irms, note, notify=True, mold_id="", product_id=""):
    tag = ""
    if mold_id or product_id:
        tag = " [" + "·".join(x for x in (f"금형 {mold_id}" if mold_id else "",
                                          f"제품 {product_id}" if product_id else "") if x) + "]"
    c.execute("INSERT INTO alerts(device,ts,kind,level,irms,note,mold_id,product_id) "
              "VALUES(?,?,?,?,?,?,?,?)",
              (device, ts, kind, level, irms, note + tag, mold_id, product_id))
    if notify:
        _notify(f"[예방보전] {device}{tag} · {kind}/{level} · {irms:.2f} — {note}")


def _learn_thresholds(c, device, mold_id, cfg, run_vals, ts, notify):
    """정상치 자동학습 → 레짐에 기록. 금형 없음('')이면 설비 config 에도 반영(하위호환)."""
    import statistics
    med = round(statistics.median(run_vals), 3)
    soft = round(med * 1.2, 3)
    db.learn_regime(c, device, mold_id, med, soft, len(run_vals))
    where = f"금형 {mold_id} 레짐" if mold_id else "설비 기본"
    if not mold_id:
        db.set_config(c, device, dict(nominal=med, soft=soft,
                                      hard=round(med * 1.5, 3), learn=0))
        cfg.update(nominal=med, soft=soft, hard=round(med * 1.5, 3), learn=0)
    _emit(c, device, ts, "LEARN", "OK", med,
          f"정상치 자동학습 완료 ({where}) — nominal={med}, soft={soft}", notify,
          mold_id=mold_id)


def process_reading(c, device, ts, irms, mold=None, product=None, notify=True):
    db.ensure_config(c, device)
    cfg = db.get_config(c, device)

    # 0) 컨텍스트: ingest 에 명시된 태그 우선, 없으면 run_context (없으면 '')
    ctx = db.get_context(c, device)
    mold_id = mold if mold is not None else ctx["mold_id"]
    product_id = product if product is not None else ctx["product_id"]
    if mold is not None and (mold_id != ctx["mold_id"] or product_id != ctx["product_id"]):
        db.set_context(c, device, mold_id, product_id, ts)  # 인라인 태그 = 장착 변경으로 기록

    c.execute("INSERT INTO readings(device,ts,irms,mold_id,product_id) VALUES(?,?,?,?,?)",
              (device, ts, irms, mold_id, product_id))

    # 1) 레짐: (설비×금형) 조합. 창·상태·임계 모두 레짐 단위.
    regime = db.ensure_regime(c, device, mold_id, cfg)
    eff = dict(cfg)  # 유효 임계 = 설비 cfg + 레짐 nominal/soft (hard 는 설비 유지)
    if regime and regime["learned"]:
        eff["nominal"], eff["soft"] = regime["nominal"], regime["soft"]

    window = db.recent_window(c, device, n=max(600, int(cfg["baseline_n"]) + 50),
                              mold_id=mold_id)
    ev = detect.evaluate(window, eff)
    st = db.get_state(c, device, mold_id)

    running = irms > cfg["idle_floor"]
    run_vals = detect.running_values(window, cfg["idle_floor"])
    level_state, drift_state, dropout_state, cusum_state = \
        st["level"], st["drift"], st["dropout"], st["cusum"]

    if running:
        # 2) 학습: 레짐 미학습 상태에서 running 샘플이 충분하면 정상치 확정.
        #    · 금형 레짐(mold_id≠'')은 항상 자동학습 (이관/교체 후 재학습이 기본 동작)
        #    · 무금형('')은 cfg.learn=1 일 때만 (기존 동작 유지)
        need_learn = (not regime["learned"]) and (mold_id or cfg.get("learn"))
        if need_learn and len(run_vals) >= int(cfg["baseline_n"]):
            _learn_thresholds(c, device, mold_id, cfg, run_vals, ts, notify)
            regime = db.get_regime(c, device, mold_id)
            eff["nominal"], eff["soft"] = regime["nominal"], regime["soft"]

        # 3) 추세 경고 — 단, 레짐이 학습 전이면 보류 (임계가 미확정이므로)
        trend_armed = regime["learned"] or (not mold_id and not cfg.get("learn"))
        if trend_armed:
            prev_warn = st["level"] in ("WARNING", "ALARM")
            warn, cusum_state, info = detect.trend_state(
                eff, ev["baseline"], run_vals, prev_warn, st["cusum"])
        else:
            warn, cusum_state, info = False, 0.0, {"learning": True}

        # 4) 레벨: ALARM=순간 피크(설비 하드, 안전), WARNING=추세 경고
        if irms >= cfg["hard"] or (st["level"] == "ALARM" and irms >= cfg["hard"] * (1 - detect.HYSTERESIS)):
            new_level = "ALARM"
        elif warn:
            new_level = "WARNING"
        else:
            new_level = "OK"

        # 5) 악화 전이에만 기록
        if detect.LEVEL_RANK.get(new_level, 0) > detect.LEVEL_RANK.get(st["level"], 0):
            note = _reason(eff, ev, info)
            if new_level == "ALARM":
                w = c.execute(
                    "SELECT ts FROM alerts WHERE device=? AND level='WARNING' AND ts<=? "
                    "ORDER BY ts DESC LIMIT 1", (device, ts)).fetchone()
                if w:
                    note += f" · 경고 후 {(ts - w['ts'])/60.0:.0f}분 만에 알람 (리드타임)"
            _emit(c, device, ts, "LEVEL", new_level, irms, note, notify,
                  mold_id=mold_id, product_id=product_id)
        level_state = new_level
        drift_state = ev["drift"]
        dropout_state = False  # 가동 재개 = 드롭아웃 해제
    else:
        # 정지 중: 레벨·추세·CUSUM 보존. 드롭아웃만 이 순간에 판정.
        if ev["dropout"] and not st["dropout"]:
            _emit(c, device, ts, "DROPOUT", "ALARM", irms,
                  "가동 중 신호 급락 — 단선/급정지 의심", notify,
                  mold_id=mold_id, product_id=product_id)
        dropout_state = st["dropout"] or ev["dropout"]
        if dropout_state:
            level_state = "ALARM"

    db.set_state(c, device, level_state, drift_state, dropout_state, cusum_state,
                 mold_id=mold_id)
    ev["mold_id"], ev["product_id"] = mold_id, product_id
    ev["regime_learned"] = bool(regime and regime["learned"])
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
