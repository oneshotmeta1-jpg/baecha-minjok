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
- 소결 호퍼에서는 `CRANE_START`/`CRANE_DONE`을 **덤프 하차 시작·완료**로 쓴다(`payload.stand_in: "dump"`, DONE에는 `load_t`). `GATE_IN.payload`에 `load_t`(적재 t)·`trip`(회차)이 있다.
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
| `BEACON_NEAR` | 기사 앱(가정) → 시뮬레이터 | 차량이 어느 구역 근처인지. 영상 이벤트에 vehicle_id를 붙이는 데 씀 |
| `ZONE_ENTER` / `ZONE_EXIT` | 영상 | 구역(`yard`·`queue`·`bay`) 진입·이탈, 3초 이상 유지 시에만 |
| `VEHICLE_STOPPED` | 영상 | 구역 안에서 멈춤 → 대기 또는 작업 시작 |
| `CALL` | 배정 엔진 | 차량 호출 (payload: rank, reason, route_m, travel_min, speed_limit_kmh) → 기사 화면 알림 |
| `CALL_ACK` | 기사 모바일 화면 | 기사가 ‘출발’을 누름 |
| `CRANE_START` / `CRANE_DONE` | 설비 신호(가정) 또는 담당자 화면 버튼 | 크레인 상하역 시작·완료 (DONE이면 진척도 100% 확정) |
| `SECTOR_DOWN` / `SECTOR_UP` | 돌발 이벤트 버튼 | 섹터 정지·복구 → 재배정 |
| `ALERT` | 배정 엔진 | 알림 발생 (payload: level, kind, text, action) |
| `ORDER_APPROVED` | 통합하역작업 탭 | 담당자가 추천 순번 승인 (payload: rank, reason) |
| `ORDER_CHANGED` | 통합하역작업 탭 | 담당자가 보낼 호퍼 변경 (payload: from, to, reason, memo, delta_min, cost_won) |
| `ALERT_ACK` | 관제 화면 · 알림 이력 탭 | 알림 확인 (payload: alert_id, ack_sec = 뜬 뒤 확인까지 실제 초) |
| `ASSUMPTIONS_UPDATED` | 통합하역작업 탭 가정값 | 가정값 변경 → 처음부터 다시 계산 (payload: changes[]) |

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
