# 예방 보전 모니터 (비침습 부하 추세)

설비·PLC·ERP를 **건드리지 않고**, 외부 도구가 넣어주는 측정값(CT 전류·서보 토크·압력…)의
**추세를 쌓아 하드 알람이 뜨기 전에 잡는** 독립 경량 모니터링 스택.

> "주기적 확인"의 주체를 사람 → 시스템으로 옮긴다. 알람은 사후. 우리는 **추세**를 본다.

```
[외부 수집도구] → POST /ingest → [FastAPI + SQLite] → 판정(베이스라인·드리프트·드롭아웃) → [웹 대시보드]
 CT / PLC 태핑        측정값          독립 스택, Odoo 아님        경고 기록 + 리드타임         오버뷰 + 설비별 상세
```

- **다중 설비**: 그룹(도장/사출/유틸)별 오버뷰 + 설비별 상세.
- **이중 탐지기**: 드리프트(점진 열화) + 드롭아웃(단선·급정지).
- **비침습**: 신호는 외부 도구가 넣는다. 이 앱은 *적재→분석→경고→대시보드* 레이어.

---

## 로컬 실행

```bash
pip install -r requirements.txt
SEED_DEMO=1 uvicorn app.main:app --reload --port 8000
#  → http://localhost:8000   (데모 데이터가 채워진 대시보드)
```

실데이터만 쓰려면 `SEED_DEMO` 없이 실행하고, 외부 도구가 `/ingest` 로 전송.

## 측정값 넣기 (ingest 계약)

수집 도구(무엇이든)가 이 한 엔드포인트로 POST 하면 된다:

```bash
curl -X POST http://localhost:8000/ingest \
  -H "Content-Type: application/json" \
  -d '{"device":"FGP-L2","irms":9.7}'          # ts 생략 시 서버 시각
```

| 필드 | 필수 | 설명 |
|---|---|---|
| `device` | ✔ | 설비 ID (처음 보면 자동 등록) |
| `irms` | ✔ | 측정값 (전류 A / 토크 % / 압력… 단위는 설정에서) |
| `ts` | | epoch 초. 생략 시 수신 시각 |

## Railway 배포

1. 이 repo를 GitHub에 두고 Railway에서 **New Project → Deploy from GitHub**.
2. Nixpacks가 `requirements.txt`를 잡고 `railway.toml`의 startCommand로 기동. 헬스체크 `/health`.
3. **영구 저장(권장):** Railway **Volume**을 `/app/data`에 마운트하고 변수 `DB_PATH=/app/data/pdm.db` 설정.
   - 볼륨이 없으면 재배포 때 SQLite가 초기화된다(측정 이력 유실).
4. (선택) 배포 시연용으로 `SEED_DEMO=1` → 빈 DB면 다중 설비 데모가 자동으로 채워짐. **운영에선 끄기.**
5. (선택) `WEBHOOK_URL` = KakaoWork/Slack incoming webhook → WARNING/ALARM 전이 시 메시지.

| 환경변수 | 기본값 | 용도 |
|---|---|---|
| `PORT` | 8000 | Railway가 주입 |
| `DB_PATH` | `data/pdm.db` | SQLite 경로(볼륨 지정) |
| `SEED_DEMO` | (off) | 빈 DB에 데모 시드 |
| `WEBHOOK_URL` | (off) | 경고 알림 |

배포 후 외부에서 데이터 흘려보기: `python scripts/seed_demo.py --url https://<앱>.up.railway.app`

---

## API

| 메서드 | 경로 | 용도 |
|---|---|---|
| POST | `/ingest` | 측정값 적재 + 판정 |
| GET | `/api/devices` | 전체 설비 요약(오버뷰, 스파크라인 포함) |
| GET | `/api/status?device=` | 설비 현재 상태 |
| GET | `/api/readings?device=&minutes=` | 추세 시계열 |
| GET | `/api/alerts?device=&limit=` | 경고 이력 |
| GET/POST | `/api/config` | 설비별 임계·탐지 설정 |
| GET | `/health` | 헬스체크 |

## 판정 로직

- **베이스라인** = `idle_floor` 초과(=가동 중) 샘플의 중앙값 → 정지/대기 전류를 섞지 않는다(상태 게이팅).
- **레벨**: `ALARM` = 순간 전류 ≥ hard(안전 트립). `WARNING` = 아래 선택된 추세 방식이 발동. `OK` = 그 외.
  진입/이탈에 히스테리시스, 베이스라인이 설 때까지 워밍업 보류 → 채터링·초기 오탐 방지.
- **드롭아웃**: 가동 중이던 신호가 급락(< idle_floor) → 단선·급정지(히터밴드 등 급사형). 재가동 전까지 래치.
- 상태가 **악화될 때만 1건** 기록(스팸 방지). ALARM 기록 시 직전 WARNING과의 **리드타임**을 함께 남김.

### 추세 탐지 방식 (`method`, 설비별 선택)

| 방식 | 언제 | 원리 |
|---|---|---|
| `absolute` | 기본·직관 | 베이스라인 ≥ soft |
| `drift` | 점진 열화 | 베이스라인 ≥ nominal×(1+`drift_pct`) — 상대 상승률 |
| `cusum` | 조기감지·노이즈 | 누적합 S > `cusum_h`·σ — 작지만 지속되는 상승을 가장 빨리 |
| `zscore` | 노이즈 큰 신호 | 로버스트 z(중앙값+MAD) ≥ `z_k` — 정상분포 이탈 |

**자동학습**(`learn=1`): 정상치를 모를 때, running 샘플이 충분히 쌓이면 `nominal/soft/hard`를 자동 확정.

### 설정 마법사 (권장 진입점)

대시보드의 **＋ 설비 설정 마법사** 버튼 → 설비의 성격을 묻는 5개 질문(고장 방식·가동 패턴·
신호 노이즈·정상치 인지 여부·트립값)에 답하면 **적합한 탐지 방식과 임계치를 자동 추천·설정**한다.
통계 지식 없이 현장 담당이 바로 세팅 가능. 세부값은 상세 화면의 "설정 편집"에서 언제든 수동 조정.

설정 항목 전체: `label, grp, unit, nominal, soft, hard, idle_floor, baseline_n, drift_pct,
dropout_enable, method, cusum_k, cusum_h, z_k, learn`.

## 구조

```
app/main.py       FastAPI 라우트 + 정적 서빙
app/pipeline.py   수집→판정→경고 (ingest·시드 공유)
app/detect.py     탐지 엔진 (베이스라인·드리프트·드롭아웃) — 설비 무관 재사용
app/db.py         SQLite 스키마·쿼리
app/seed.py       데모 시드(SEED_DEMO)
web/index.html    대시보드 (오버뷰 + 설비 상세 SPA)
scripts/seed_demo.py  외부 HTTP backfill/스트리밍
```
