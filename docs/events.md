# 이벤트 형식 (모듈 간 유일한 약속)

영상 모듈·IoT 시뮬레이터는 이벤트를 **내고**, 배정 엔진은 이벤트를 **받아 상태를 바꾸고**, 화면은 **상태를 그린다**.
이 형식만 지키면 각 파트가 따로 만들어도 당일 바로 연결된다. 바꿀 때는 PL(현우)과 먼저 합의한다.

## 한 건의 모양

```json
{
  "ts": "2026-11-09T15:04:12",
  "type": "ZONE_ENTER",
  "source": "cam",
  "cam_id": "CAM-B",
  "sector_id": "B",
  "zone": "bay",
  "track_id": 17,
  "vehicle_id": "TR-0412",
  "payload": { "bbox": [412, 230, 688, 401], "conf": 0.86 }
}
```

- `ts`: 시뮬레이션 시계 기준 ISO 시각
- `source`: `cam` | `iot` | `sim` | `engine` | `ui`
- 없는 값은 `null` (키는 남겨 둔다)

## 샘플과 확인

- 샘플: [sample_events.jsonl](sample_events.jsonl) — 소결공장 시나리오, 시드 7, 덤프트럭 20대 4시간 순환 운행, 호퍼 A 정지(08:02)·긴급 반입·A 복구 포함 1,244건
- 다시 만들기·형식 확인: `py tests/test_sim.py --samples`
- 이벤트 이름은 워크플로우 ⑤(데이터·이벤트 구조)와 같다. 하역 시작·끝은 `BAY_ENTER`/`BAY_EXIT`(베이 1~3), 하역장 정지는 `YARD_DOWN/UP`, 긴급은 `URGENT_IN`, 승인 요청은 `CALL_REQUEST`. `GATE_IN.payload`에 `load_t`(적재 t)·`trip`(회차)이 있다.
- 영상·설비가 붙기 전에는 시뮬레이터가 대신 내며 `source: "sim"`, `payload.stand_in: "cam" | "crane"`으로 표시한다.
  비전 파트는 같은 이벤트를 `source: "cam"`으로 내면 된다.

### 경로 거리·이동시간 (R3)

- `CALL.payload.route_m`: 대기장 → 배정 호퍼 경로 길이(m). OpenStreetMap 도로를 따라 잰 값(`data/sectors.json` `roads[].length_m`).
- `CALL.payload.travel_min`: 이동시간(분) = route_m ÷ (구내 제한속도 km/h × 1000 ÷ 60) × 감속 계수. 예: 672m ÷ 500m/분 × 1.15 = 1.5분.
- `CALL.payload.speed_limit_kmh`: 계산에 쓴 제한속도(기본 30, 가정값 탭에서 바꿀 수 있음). 기사 화면은 이 값을 그대로 보여 주면 된다.
- 엔진의 호출 시점 공식(남은 하차 시간 ≤ 이동시간 + 여유)도 같은 travel_min을 쓴다.

```json
{"ts": "2026-11-09T08:58:00", "type": "CALL", "source": "engine", "cam_id": null, "sector_id": "B", "zone": null,
 "track_id": 22, "vehicle_id": "87사 4622",
 "payload": {"rank": 1, "reason": "투입 기한 순(64분 남음) · 호퍼 B 전용 원료", "route_m": 672, "travel_min": 1.5, "speed_limit_kmh": 30}}
```

`data/sectors.json`의 `roads[]` 한 구간:

```json
{"from": "YARD", "to": "B", "points": [[896, 424], [778, 418], "..."], "length_m": 672, "road_m": 273, "osm_nodes": 9}
```

- `points`는 화면 좌표(px, 1600x900), `map.mpp`(1px = m)를 곱하면 길이가 `length_m`와 같다.
- `road_m`은 OSM 도로 위 길이, 나머지는 시설 상자 ↔ 도로 진입로. 시설 위치는 가정이다.
- 구간: 원료야드→계근대(입)→대기장→호퍼 A/B/C→계근대(출)→원료야드. 다시 만들기: `py tools/build_map.py` (지도 © OpenStreetMap contributors, ODbL).

## 서버 API (app/server.py)

| 주소 | 하는 일 |
|---|---|
| `GET /api/state`, `WS /ws` | 아래 상태 (WebSocket은 0.1초마다 밀어 줌). R3부터 `routes`(구간별 거리·이동시간), `road`(계근대(입) 전 구내 도로 위 차량: 공차 복귀·상차·적재 반입), `road_cfg`(제한속도·감속 계수) 추가 |
| `GET /api/config` | 배치(sectors.json) + 실제 지도 레이어(site_map.json) + 설정 |
| `GET /api/events?limit=50` | 최근 이벤트 로그 (SQLite `data/events.db`) |
| `POST /api/sim/start` · `pause` · `reset` · `speed?value=60` | 시뮬레이션 시계 (speed = 시뮬레이션 초 / 실제 초) |
| `POST /api/events/sector_down?sector_id=A` · `sector_up` | 섹터 정지·복구 → 이동·대기 차량 재배정 + Alert |
| `POST /api/events/urgent` | 긴급 출하 차량 한 대 투입 |
| `POST /api/alerts/{id}/ack` | 알림 확인 |

## 종류

| type | 내는 곳 | 의미 |
|---|---|---|
| `GATE_IN` | 기사 앱(가정) → 시뮬레이터 | 도착, 차량 등록 (payload: vehicle, cargo, due, urgent) |
| `ZONE_ARRIVE` | 기사 앱 위치(가정) → 시뮬레이터 | 차량 대기장 도착 (zone=yard) |
| `BEACON_NEAR` | 기사 앱(가정) → 시뮬레이터 | 차량이 어느 구역 근처인지(호퍼 대기선). 영상 이벤트에 vehicle_id를 붙이는 데 씀 |
| `ZONE_ENTER` / `ZONE_EXIT` | 영상 | 구역(`yard`·`queue`·`bay`) 진입·이탈, 3초 이상 유지 시에만 |
| `VEHICLE_STOPPED` | 영상 | 구역 안에서 멈춤 → 대기 또는 작업 시작 |
| `CALL` | 배정 엔진 | 차량 호출 (payload: rank, reason, route_m, travel_min, speed_limit_kmh, approval = auto · approved · changed) → 기사 화면 알림 |
| `CALL_PENDING` | 배정 엔진 | 호출 시각이 됐지만 현장 담당자 승인을 기다림 (자동 승인 꺼짐 · L2). payload: rank, reason, since |
| `CALL_ACK` | 기사 모바일 화면(`source: app`) · 시뮬레이터 대신 응답(`iot`) | 기사가 [확인했어요 · 이동 시작]을 누름. 앱으로 등록한 차는 이걸 받아야 출발하고, 3분 동안 없으면 `호출 응답 없음` 알림 뒤 출발(대신 응답). 이미 출발한 차의 CALL_ACK는 `ignored` |
| `BAY_ENTER` / `BAY_EXIT` | 영상(베이 구역 3초 이상) · 기사 앱 [도착했어요](`source: app`, payload.manual) | 하역 시작 · 끝 (payload: bay 1~3, cargo / load_t, unload_min). 진입→이탈 = 건별 작업 기록 · 예측 하역시간의 근거. 이미 하역 중인 차의 BAY_ENTER(영상과 수동이 겹침)는 `ignored` — 중복 진입 없음 |
| `YARD_DOWN` / `YARD_UP` | 시연 버튼 | 하역장 정지 · 복구 → 영향 차량 재배정 |
| `URGENT_IN` | 시연 버튼 | 긴급 입고(소결 재고 부족) → 순번 맨 앞 |
| `SAFETY_ALERT` | 영상(합성) · 시뮬레이터 | 안전 경고: 적재함 상승 중 덤프 반경(가정 5m, rois.json REAR 구역) 안 작업자 → L3 자동 실행(기사 앱 경고 · 하차 중지 요청, 가상). 하차 진행이 멈춤 (payload: id, level=L3, rule, dist_m, bay, dump_deg, repeat, action, stand_in) |
| `NEAR_MISS` | 영상(R15, vision/near_miss.py) | 아차사고 한 건: 사람 발끝이 위험 구역에 있고 규칙 조건(① 적재함 상승 중 덤프 반경 · ② 베이 진입 30초 안 후진 경로)이 이어진 동안을 한 건으로, 끝날 때 한 번. 단계 L1 주의 → 1초 L2 경고 → 2초 L3(그 순간 `SAFETY_ALERT`로 하차 중지 요청). payload: id, rule, rule_no, level, duration_s, min_dist_m, zone_id, bay, person_track, snapshot_t, clip[시작, 끝](영상 초), source = video · stand_in(합성 사람), assumed |
| `SAFETY_ACK` | 맵 위 안전 띠 · 알림 패널 안전 탭 | 관제·현장 담당자가 안전 경고 확인 또는 오탐지 → 하차 재개 (payload: id, how = 확인/오탐지, ack_sec = 뜬 뒤 확인까지 실제 초, repeat). 같은 위험이 계속 감지되면 SAFETY_ALERT(repeat=1)가 다시 온다 |
| `SAFETY_SEEN` | 상단 안전 띠 [관제 확인] · 3D 매핑 카드 헤더 배지 | 관제가 안전 경고를 봤다는 확인 — 관제 화면 배치도 · 구역 3D 매핑 카드 · 상단 띠의 깜빡임을 멈추고 빨간색 고정. **안전 중지 · 후속 호출 보류는 유지**(해제는 `SAFETY_ACK`). payload: id, seen_sec = 뜬 뒤 확인까지 실제 초. (3D 매핑 요청서 3-5의 `safety_ack` = 이것, `safety_clear` = `SAFETY_ACK` — 팀 확인 필요) |
| `DRIVER_STOPPED` | 기사 앱 안전 경고 화면 [멈췄어요] | 운전자가 하역을 멈췄다고 알림 (payload: id = 안전 경고). 하역 재개는 여전히 관제·현장의 SAFETY_ACK |
| `ALERT` | 배정 엔진 | 알림 발생 (payload: level, kind, text, action, related = 제안이 영향을 주는 반입 "차량#회차" 목록) |
| `ORDER_APPROVED` | 통합하역작업 탭 | 담당자가 추천 순번 승인 (payload: rank, reason) |
| `ORDER_CHANGED` | 통합하역작업 탭 | 담당자가 보낼 호퍼 변경 (payload: from, to, reason, memo, delta_min, cost_won) |
| `ALERT_ACK` | 관제 화면 · 알림 이력 탭 | 알림 확인 (payload: alert_id, ack_sec = 뜬 뒤 확인까지 실제 초) |
| `ASSUMPTIONS_UPDATED` | 설정 › 가정값·단가 | 가정값 변경 → 처음부터 다시 계산 (payload: changes[]) |
| `EFFECT_CONFIRMED` | 런타임(효과 계산) | 반입 한 건의 효과 확정 — 두 방식 모두 하차를 시작했을 때 한 번 (payload: trip, rec_wait_min, fifo_wait_min, effect_min, won, source = 효과를 만든 것) |
| `ALERT_IGNORED` | 알림 이력 [무시] | 담당자가 알림의 제안을 무시 (payload: alert_id, kind). 무시한 알림의 실제 효과도 알림 이력에 남는다 |
| `CALL_REQUEST` | 순번·배정 [현장 담당자에게 승인 요청] | 관제 → 현장 담당자 앱 승인 요청(가상 푸시) (payload: n, vehicles = "차량#회차", to) |
| `APPROVAL_MODE` | 순번·배정 자동 승인 스위치 | 자동 승인 켜기/끄기 (payload: auto). 끄면 승인 전 호출은 `CALL_PENDING`으로 멈춘다 |

R2에서 팀이 제안한 `order_approved` 등 소문자 이름은 기존 형식에 맞춰 대문자로 썼다. 변경 사유 코드는
`기사·차량 사정 / 현장 우선순위 / 영상 판정 오류 / 호퍼·설비 사정 / 긴급 반입 끼어듦 / 기타` 여섯 가지(app/runtime.py 의 REASONS 목록).

```json
{"ts": "2026-11-09T08:30:00", "type": "ORDER_CHANGED", "source": "ui", "cam_id": null, "sector_id": "C", "zone": null,
 "track_id": 9, "vehicle_id": "81바 6572",
 "payload": {"from": "A", "to": "C", "reason": "호퍼·설비 사정", "memo": "A 하차구 1번 점검", "delta_min": 7.8, "cost_won": 3900}}
{"ts": "2026-11-09T08:31:00", "type": "ORDER_APPROVED", "source": "ui", "cam_id": null, "sector_id": "B", "zone": null,
 "track_id": 12, "vehicle_id": "88자 2126", "payload": {"rank": 1, "reason": "투입 기한 임박(26분 남음) · 호퍼 B 전용 원료"}}
{"ts": "2026-11-09T08:32:00", "type": "ALERT_ACK", "source": "ui", "cam_id": null, "sector_id": "A", "zone": null,
 "track_id": null, "vehicle_id": null, "payload": {"alert_id": "AL-14", "ack_sec": 12.4}}
{"ts": "2026-11-09T08:35:00", "type": "ASSUMPTIONS_UPDATED", "source": "ui", "cam_id": null, "sector_id": null, "zone": null,
 "track_id": null, "vehicle_id": null, "payload": {"changes": ["슬러지 하차 표준 15 → 18분"]}}
```

### 탭별 API (R2)

| 주소 | 하는 일 |
|---|---|
| `GET /api/dock` | 호퍼 카드(배정 대기·처리·평균 하차), FIFO vs 추천 비교, 가정값·변경 이력, 최근 변경 기록 |
| `POST /api/order/approve?vehicle_id=` · `POST /api/order/approve_all` | 순번 승인 |
| `GET /api/order/impact?vehicle_id=&sector_id=` | 바꾸면 추천 대비 대기 +N분 · 비용 +₩N (저장 전 미리 보기) |
| `POST /api/order/change` `{vehicle_id, sector_id, reason, memo}` | 호퍼 변경 + 사유 기록(SQLite `changes`) |
| `GET/POST /api/assumptions` | 가정값 보기·바꾸기(→ `data/assumptions.json`, 처음부터 재계산) |
| `GET /api/analysis` | 배정 방식 성과, 시간대별 대기 차량(두 방식), 원료별 성과, 추천 피드백·제안 |
| `POST /api/suggestion/apply?key=` · `ignore?key=` | 가정값 조정 제안 적용·무시 |
| `GET /api/alerts?level=&kind=&sector_id=&unacked=` | 알림 이력 + 요약(오늘 건수·미확인·평균 확인 시간), SQLite `alerts`에도 저장 |

### 알림 패널 · 효과 · 승인 (R4)

- **효과(분)** = 반입 한 건("차량#회차")마다 `FIFO였을 때 대기 − 추천 대기`. 같은 도착 데이터로 기존 방식(도착순 · 원료별 고정 호퍼)을 함께 돌린 값이고, 두 방식 모두 하차를 시작하면 **확정**, 아니면 **예상**(추천은 순번표의 예상 하차 시작, FIFO는 지금까지 대기 = 하한).
- **금액** = 효과(분) × ₩500/분(= 대기 단가 ₩30,000/시간, 가정). 호퍼 유휴는 금액에 넣지 않고 시간(분)으로만 본다.
- 알림 카드의 효과 = `ALERT.payload.related`에 있는 반입들의 효과 합. 상태: 승인 대기(관련 차량이 승인을 기다림) · 실행됨(호퍼 이상·대기 초과는 엔진이 바로 재배정·우선 배정) · 확인 필요 · 확인함.

| 주소 | 하는 일 |
|---|---|
| `GET /api/panel` | 알림 카드(무슨 일·제안·효과·상태·관련 순번) + 오늘 대기 절감 합계 |
| `POST /api/approval?auto=true\|false` | 자동 승인 스위치 (`APPROVAL_MODE`) |
| `GET /api/schedule` | 호퍼 × 하차구 줄별 구간(하차 중 · 지연 · 호출돼 오는 중 · 호출 예약 · 승인 대기 · 정지), 지금 −5분 ~ +40분 |

```json
{"ts": "2026-11-09T09:05:00", "type": "CALL_PENDING", "source": "engine", "cam_id": null, "sector_id": "A", "zone": null,
 "track_id": 14, "vehicle_id": "82자 2321", "payload": {"rank": 1, "reason": "투입 기한 순(41분 남음) · 호퍼 A 전용 원료", "since": "09:05"}}
{"ts": "2026-11-09T09:04:00", "type": "APPROVAL_MODE", "source": "ui", "cam_id": null, "sector_id": null, "zone": null,
 "track_id": null, "vehicle_id": null, "payload": {"auto": false}}
```

### 운영 분석 · 알림 이력 · 설정 · 바꿔보기 (R5)

- **효과를 만든 것(source)**: 그 반입에 붙은 결정·알림으로 하나만 고른다. 담당자 순번 변경 → 긴급 반입 → 호퍼 정지 → 재배정 → 대기 초과 → 우선 배정 → 혼잡 → 호출 보류 → 작업 지연 → 호출 늦춤 → 그 밖은 "가장 빨리 비는 하차구 (일반 추천)".
- **예상 효과** = 알림이 뜰 때(제안할 때) 관련 반입의 FIFO 대비 효과를 계산해 남긴 값, **실제 효과** = 관련 반입이 모두 `EFFECT_CONFIRMED` 된 뒤의 합.
- `ASSUMPTIONS_UPDATED`는 설정 저장과 바꿔보기 [설정에 반영] 모두에서 나오고, 변경 이력에 누가(관제 담당자)·언제가 남는다. 바꿔보기 미리보기 자체는 저장하지 않으므로 이벤트를 내지 않는다(같은 행동 기록으로 복사본을 다시 돌려 차이만 돌려줌).
- **최근 7일**: 시드를 바꾼 시뮬레이션 6일 + 오늘. 실제 운영 기록이 아니다.

| 주소 | 하는 일 |
|---|---|
| `GET /api/analysis2?range=today\|week` | 성과·비용(절감 합계, 효과를 만든 것별 합, 시간 지표, 그래프) + 건별 기록(차량별 효과) + 원료별 실측 하차시간 |
| `GET /api/alerts/history?range=` | 알림 → 제안 → 처리 → 예상·실제 효과 한 줄씩 + 퍼널(알림 → 제안 → 실행 → 효과 확정) + 예상 대 실제 |
| `POST /api/alerts/{id}/ignore` | 제안 무시 (`ALERT_IGNORED`) |
| `POST /api/preview` `{std_times?, target_wait_min?, call_margin_min?}` | 바꿔보기: 호출 시각·효과·하차구 비는 시각이 어떻게 바뀌는지 (저장 안 함) |
| `GET /api/settings` | 가정값 + 변경 이력 + 원료별 실측 평균 + 표준시간 제안 |
| `GET /api/rois` | CAM별 구역(ROI) 다각형 (data/rois.json, 1920x1080 첫 프레임 기준) |
| `GET /api/export/report.csv` · `trips.csv` · `alerts.csv` `?range=` | 보고서 · 건별 기록 · 알림 이력 CSV (엑셀용 BOM) |

```json
{"ts": "2026-11-09T08:41:15", "type": "EFFECT_CONFIRMED", "source": "engine", "cam_id": null, "sector_id": "A", "zone": null,
 "track_id": 9, "vehicle_id": "81자 1965", "payload": {"trip": 1, "rec_wait_min": 6.5, "fifo_wait_min": 11.25, "effect_min": -4.8,
 "won": -2375, "source": "가장 빨리 비는 하차구 (일반 추천)"}}
```

### 예측값 · 목표 기준값 · 호출 정책 (R6)

- **예측 하차시간**(자동) = 오늘 실측 평균, 실측 3건 미만이면 기본 표준시간(data/std_times.json). 5분마다 다시 계산하고 순번·호출 시각·하차구 시간표에 쓴다. 1분 이상 바뀌면 설정 › 변경 이력에 "자동 계산"으로 남는다.
- **하차시간 목표**(판정용, 설정에서 바꿈) = 넘으면 기준미달, 목표 × 지연 배수를 넘으면 하차 지연 알림. 바꿔도 순번은 그대로.
- **호출 정책**(호출 여유 · 긴급 우선 · 구내 운행) = 바꾸면 순번이 다시 계산된다.
- 변경 이력 한 줄 = 시각 · 누가(관제 담당자 / 자동 계산) · 무엇 · 영향("순번 N건 호출 시각 다시 계산" / "기준미달 판정만 바뀜 · 순번 그대로").
- 바꿔보기의 예측 하차시간은 `pred_override`로 고정할 수 있고, 설정 ①에서 [자동으로]를 누르면 풀린다.
- 시연 시나리오: data/demo.json (시드 7, 08:40 호퍼 A 정지 → 08:55 긴급 반입 → 09:15 복구)을 하루 끝까지 돌린 고정 결과. `GET /api/analysis2?range=demo`.

### 안전 경고 (R8)

- 반입마다 시드로 한 번 정한다: 적재함이 올라가 있는 시간 × `sectors.json` safety.per_raised_hour(가정 0.6회/시간). 같은 시드·같은 반입이면 같은 결과.
- 열려 있는 동안 그 차의 하차 시간은 멈춘다(지연 배수에 안전 중지 시간이 들어가지 않음). 아무도 확인하지 않으면 6분 뒤 "자동 해제 · 미확인"으로 남는다.
- 확인 뒤에도 30% 확률(가정)로 3분 뒤 같은 위험이 다시 감지된다(repeat=1). 오탐지는 다시 오지 않는다.
- 안전 경고는 금액으로 계산하지 않는다. 시연 시나리오(data/demo.json)는 08:30 호퍼 A에 1건을 넣는다. 화면 상단 [안전 경고] 버튼으로도 만든다(`POST /api/events/safety?sector_id=`).
- `GET /api/panel2`: 알림 패널 [순번 | 안전] — approvals(승인 대기) · checks(확인 필요, 같은 호퍼·종류 묶음) · safety_open · safety_done. `POST /api/safety/{id}/ack` · `/dismiss`.

```json
{"ts": "2026-11-09T08:30:00", "type": "SAFETY_ALERT", "source": "sim", "cam_id": "CAM-01", "sector_id": "A", "zone": "rear",
 "track_id": 7, "vehicle_id": "82아 2341", "payload": {"id": "SA-1", "level": "L3", "rule": "덤프 반경 5m", "dist_m": 3.0, "bay": 1,
 "dump_deg": 50, "repeat": 0, "action": "앱 경고 · 하차 중지 요청 (가상)", "stand_in": "cam"}}
```

### 아차사고 (R15 · R16)

- 판정: `vision/near_miss.py`, 규칙·단계 기준은 `data/cams.json` `near_miss`(가정값). ④ 출입 금지 · ⑤ 대기선 넘음은 설정 항목만 있고 꺼져 있다.
- 거리(①②): 사람 발끝 ↔ 트럭 박스 가장 가까운 점 × 카메라별 `m_per_px`(가정). 시간은 영상 초(time_scale 곱하기 전).
- **③ 접근(R16, 대기장 CAM-04)**: 구역(대기장 주차면) 안 사람과 **움직이는** 트럭(최근 1초 접지점 이동 ≥ max(20px, 박스 너비 10%)) 사이가 3m 이하.
  거리는 사람 박스 키를 1.7m로 보고 환산(멀리 있을수록 작게 보이는 원근 보정). 단계 = 거리(3m L1 · 2m L2 · 1m L3)와 이어진 시간(1초 L2 · 2초 L3) 중 높은 것.
  처음 보인 순간 0.8m 안에 있던 트럭은 그 사람이 내린 차(운전자)로 보고 짝짓지 않는다. 한 사람은 가장 가까운 트럭 하나. 대기장엔 하차가 없어 L3여도 `SAFETY_ALERT`는 없다.
- 합성 사람: `data/stand_in.json`의 손으로 둔 궤적. **R16부터 기본 꺼짐**(`near_miss.stand_in: false`) — 켜면 호퍼 영상에 섞고 `source: "stand_in"`, 화면에 "합성" 표시.
  시뮬레이션의 호퍼 안전 경고 무작위 발생(합성 작업자)도 `data/sectors.json` `safety.synthetic: false`로 꺼짐([위험 시연] 버튼은 그대로).
- 기록: 서버는 `NEAR_MISS`를 차가 나간 뒤에 와도 받고(같은 id는 `ignored`), `GET /api/state`의 `near_miss`(최근 30건)로 준다. 단계 L3만 하차를 멈춘다(SAFETY_ALERT, 규칙 ①②).
- 시뮬레이션 모드(R16): 서버가 켜질 때 영상 탐지 결과에서 **실제 사람으로 잡힌** 아차사고(source=video)를 뽑아, 영상 한 바퀴(클립 × time_scale ≈ 11분)마다 같은 장면을 기록한다(id 뒤 `-R<회차>`). L2·L3는 첫 회차에만 알림(`ALERT` kind 아차사고).

```json
{"ts": "2026-11-09T08:08:10", "type": "NEAR_MISS", "source": "cam", "cam_id": "CAM-01", "sector_id": "A", "zone": "rear", "track_id": 1, "vehicle_id": "CAM-01-T1", "payload": {"id": "NM-CAM-01-901-84", "rule": "덤프 반경", "rule_no": 1, "level": "L3", "duration_s": 3.83, "min_dist_m": 1.8, "zone_id": "A-REAR-1", "bay": 1, "person_track": 901, "snapshot_t": 8.79, "clip": [6.42, 14.25], "source": "stand_in", "assumed": ["m_per_px", "level_sec"]}}
```

③ 접근(R16, 실제 영상 CAM-04 — 추가 필드: `rule_key`, `truck_track`, `truck_moving`, `truck_speed_kmh`(가정 환산), `dist_method`; `sector_id`·`bay`는 null, `zone`은 yard):

```
{"ts": "2026-11-09T08:06:52", "type": "NEAR_MISS", "source": "cam", "cam_id": "CAM-04", "sector_id": null, "zone": "yard", "track_id": 55, "vehicle_id": "CAM-04-T55", "payload": {"id": "NM-CAM-04-132-69", "rule": "사람·트럭 접근", "rule_no": 3, "rule_key": "approach", "level": "L3", "duration_s": 3.41, "min_dist_m": 0.0, "zone_id": "YARD-WAIT-1", "bay": null, "person_track": 132, "truck_track": 55, "truck_moving": true, "truck_speed_kmh": 7.0, "dist_method": "사람 키 1.7m 기준 환산", "snapshot_t": 9.02, "clip": [4.9, 12.31], "source": "video", "start_offset_s": 6.9, "assumed": ["person_h_m", "dist_m", "level_sec", "move_px"]}}
```

### 구역(ROI) 편집 (R15)

- `GET /api/rois/edit`: 영상 채널 구역(cams.json, 영상 픽셀) + 시뮬레이션 구역(rois.json, 1920×1080)을 함께 준다(읽기 통합). `can_edit` = 관리자 여부, `history` = 최근 구역 저장 기록.
- `POST /api/admin/rois/{camera}` body `{"zones": [...]}`: 관리자만(아니면 403). 검사에 걸리면 422와 이유 문장. 저장하면 cams.json을 고쳐 쓰고 처리 기록(auth_log, action `roi`)에 누가 · 언제 · 무엇(추가/삭제/수정 구역 ID)을 남긴다.
- 구역 ID 모양(판정이 ID로 베이를 찾음): 하차 `A-BAY-1` · 위험 `A-REAR-1`(덤프 반경 5m) / `A-BACK-1`(후진 경로) · 대기 `*-WAIT-n` · 보행 통로 `*-WALK-n` · 출입 금지 `*-NOGO-n`(판정은 다음 라운드).
- 저장하면 화면 겹쳐 그리기는 바로 바뀌고, 판정 이벤트는 다음 영상 수집(`tools/ingest_videos.py`)부터 바뀐다. rois.json(시뮬레이션 그림)은 이번에 보기만.

## 차량 상태

`arrived`(도착) → `yard`(대기장 대기) → `moving`(호출 후 이동) → `queue`(섹터 대기) → `working`(작업 중) → `done`(완료·출차)

## 화면이 받는 상태 (`GET /api/state`, WebSocket `/ws`로 같은 모양을 밀어 줌)

```json
{
  "clock": "2026-11-09T15:04:12",
  "mode": "recommend",
  "sectors": [
    { "id": "B", "status": "up", "congestion_pct": 75, "level": "warn",
      "working": [ { "vehicle_id": "TR-0412", "progress_pct": 62, "remain_min": 13 } ],
      "queue": ["TR-0420"], "next_call_at": "2026-11-09T15:15:00" }
  ],
  "vehicles": [
    { "id": "TR-0412", "type": "트레일러", "cargo": "후판", "state": "working",
      "sector_id": "B", "rank": null, "wait_min": 12, "urgent": false, "due": "2026-11-09T17:00:00",
      "xy": [1100, 450] }
  ],
  "queue": [ { "rank": 1, "vehicle_id": "TR-0420", "sector_id": "C", "call_at": "15:09", "reason": "긴급 출하 · C가 가장 먼저 빔" } ],
  "kpi": { "baseline": { "avg_wait": 0, "max_wait": 0, "util_pct": 0, "late": 0 },
           "recommend": { "avg_wait": 0, "max_wait": 0, "util_pct": 0, "late": 0 },
           "reduction_pct": 0 },
  "alerts": [ { "id": "AL-3", "ts": "15:02", "level": "crit", "kind": "혼잡", "text": "섹터 B 혼잡 85%↑ 5분", "action": "B 호출 보류, 대기 2대 → C", "ack": false } ]
}
```

## 영상 모드 — 실제 영상 결과로 대시보드 돌리기

시뮬레이터는 차를 만들지 않고, **들어온 이벤트로만** 차가 움직인다. 배정 엔진·예측·알림·안전·효과(FIFO 비교)는 그대로 돈다.

```bash
py app/server.py --video                     # 영상 모드 서버 (GET /api/mode → {"mode": "video"})
py vision/to_events.py 결과.json --start 2026-11-09T08:00:00 --out events.jsonl
py tools/replay_events.py events.jsonl --speed 60
```

- `POST /api/ingest` — 이벤트 1건(객체) 또는 여러 건(배열). 응답 `{"results": [...], "mode": ...}`.
  결과 코드: `ok` · `ignored`(이미 끝난 운행 등 반영할 것 없음) · `unknown_vehicle`(GATE_IN 없이 온 차) · `unknown_cargo`.
- 빠진 키(`source`, `zone`, `payload` 등)는 기본값으로 채운다. `vehicle_id`가 없으면 `track_id`로 `T17`.
- 이벤트 시각까지 시계를 돌린 뒤 반영한다(시각 순서로 보낼 것).

| 이벤트 | 누가 | 대시보드에서 바뀌는 것 |
|---|---|---|
| `GATE_IN` (payload `cargo`, `load_t`, `due` 선택) | 계근대·기사 앱 | 도착 차량 생성 · FIFO 기준선 도착 |
| `ZONE_ARRIVE` | CAM-04 대기장 | 대기장 대기 → 엔진이 베이·순번 추천, 호출(CALL) |
| `ZONE_ENTER` zone=queue | CAM-01~03 대기선 | 대기선 |
| `BAY_ENTER` / `BAY_EXIT` (payload `bay` 1~3) | CAM-01~03 하차장 | 하역 중 → 완료, 건별 작업 기록 1건 |
| `SAFETY_ALERT` (payload `rule` 덤프 반경 5m · 후진 경로) | CAM | 안전 탭 · 베이 안전 중지 · 이후 호출 보류 |
| `NEAR_MISS` (payload `level` L1~L3, `source` video · stand_in) | CAM | 아차사고 기록 · CAM 크게 보기 목록 (①② L3는 위 SAFETY_ALERT가 함께 옴) · ③ 대기장 접근은 맵 대기장 표식(최근 10분) |
| `YARD_DOWN` / `YARD_UP` | 현장 | 하역장 정지·복구 |

`vision/to_events.py` 판정 규칙: 박스 아래 중심점(접지점)이 `data/rois.json` 구역 안에 3초 이상 → 진입, 3초 이상 밖·화면에서 사라짐 → 이탈.
사람 발끝이 REAR-n(덤프 반경)에 있고 그 베이 트럭 박스가 세로/가로 ≥ 0.6(적재함 상승), 또는 BACK-n(후진 경로)에 있고 그 베이 진입 30초 안 → 아차사고(R15): 이어진 시간으로 L1 → L2(1초) → L3(2초, 이때 SAFETY_ALERT), 끝날 때 NEAR_MISS 한 건.
대기장(CAM-04)에서 사람과 움직이는 트럭이 3m 이하 → 아차사고 ③ 접근(R16): 거리·지속 시간으로 L1~L3, 끝날 때 NEAR_MISS 한 건(SAFETY_ALERT 없음).

## 실제 영상 채널 (R9)

- 채널 설정: `data/cams.json` — CAM-01~04 영상 파일(`해커톤/영상/`, git 제외) · 영상 픽셀 기준 판정 구역 · `time_scale`(15초 클립 → 10분).
  영상 파일이 없으면 그 채널은 시뮬레이션 그림으로 돌아간다(`GET /api/cams`의 `source`: `video` | `sim`).
- 영상은 `/media/`로 읽기 전용(Range 지원, 폴더 밖 경로 404).
- 탐지: `py vision/detect_video.py` → 영상 옆 `<이름>.det.json`(YOLO11n + ByteTrack). 화면은 이 결과를 재생 시각에 맞춰 겹쳐 그린다.
- 이벤트: `py tools/ingest_videos.py` — det.json → `to_events.py`(추적 ID 바뀜 합치기 · 클립 끝에서 닫기) → `/api/ingest`.
  영상에 없는 계근(GATE_IN)·원료는 가정으로 붙인다(`payload.assumed`).
- **영상 한 번 = 반입 한 번**: 이벤트마다 `event_id`(영상 파일 · 재생 회차 · 영상 안 시각 · 종류 · 차량). 같은 것을 다시 넣으면 결과 `duplicate`. 다음 반입으로 넣으려면 `--play 2`.
  화면의 반복 재생은 이벤트를 넣지 않는다.
- 판정 규칙 확인 기록: [vision_check.md](vision_check.md).

## 모바일 앱 (R12) — 운전자 · 현장 담당자

서버가 로그인한 사람으로 걸러 준다: 운전자 = 가입한 차량번호의 차만, 현장 담당자 = 담당 하역장만. 실시간은 `/ws/m`(1초마다, 사람마다 다른 내용).

| 화면 버튼 | API | 들어가는 이벤트 |
|---|---|---|
| 운전자 · 운행 등록 [등록하고 게이트로] | `POST /api/driver/trip` {cargo, load_t, order} | `GATE_IN` (source app, payload cargo · load_t · order) |
| 운전자 · 호출 [확인했어요 · 이동 시작] | `POST /api/driver/ack` | `CALL_ACK` (source app) |
| 운전자 · 도착 확인 [도착했어요] | `POST /api/driver/arrive` | `BAY_ENTER` (source app, payload bay · cargo · manual) |
| 운전자 · 안전 경고 [멈췄어요] | `POST /api/driver/stopped` | `DRIVER_STOPPED` |
| 현장 · 순번 [승인] / [변경] | `POST /api/field/approve` · `/api/field/change` | `ORDER_APPROVED` / `ORDER_CHANGED` |
| 현장 · 안전 [확인] / [취소 · 오탐지] | `POST /api/field/safety/{id}/ack · dismiss` | `SAFETY_ACK` |
| 현장 · 알림 [확인] | `POST /api/field/alerts/{id}/ack` | `ALERT_ACK` |

운전자 대기 화면의 순번 · 예상 호출 · 대기 분은 PC 순번표(`/api/state` queue)의 같은 행을 그대로 쓴다(앞 차량 = 순번 − 1).

```json
{"ts": "2026-11-09T08:30:10", "type": "CALL_ACK", "source": "app", "cam_id": null, "sector_id": "C", "zone": null, "track_id": null, "vehicle_id": "82가 1234", "payload": {}}
{"ts": "2026-11-09T08:33:40", "type": "BAY_ENTER", "source": "app", "cam_id": null, "sector_id": "C", "zone": null, "track_id": null, "vehicle_id": "82가 1234", "payload": {"bay": 3, "cargo": "석회석", "manual": true}}
```
