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

## 서버 API (app/server.py)

| 주소 | 하는 일 |
|---|---|
| `GET /api/state`, `WS /ws` | 아래 상태 (WebSocket은 0.1초마다 밀어 줌) |
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
| `CALL` | 배정 엔진 | 차량 호출 (payload: sector_id, rank, reason) → 기사 화면 알림 |
| `CALL_ACK` | 기사 모바일 화면 | 기사가 ‘출발’을 누름 |
| `CRANE_START` / `CRANE_DONE` | 설비 신호(가정) 또는 담당자 화면 버튼 | 크레인 상하역 시작·완료 (DONE이면 진척도 100% 확정) |
| `SECTOR_DOWN` / `SECTOR_UP` | 돌발 이벤트 버튼 | 섹터 정지·복구 → 재배정 |
| `ALERT` | 배정 엔진 | 알림 발생 (payload: level, kind, text, action) |

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
