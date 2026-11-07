"""R12 모바일(운전자 · 현장 담당자) 흐름과 권한 — py tests/test_mobile.py

1) 런타임: 운전자 흐름(등록 → 대기 → 호출 → 이동 시작 → 도착 → 하역 → 완료), 순번 값 = PC 순번표, 응답 없음 알림, 중복 진입
2) 서버(실제 app/server.py · 메모리 모드): 역할별 API 403 · 걸러진 내용 · 실시간(/ws/m) · 승인 동기화
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
os.environ["DOCK_MEMORY"] = "1"

from app.runtime import Runtime  # noqa: E402

results = []


def check(name, ok, note=""):
    results.append((name, bool(ok)))
    print(("통과 " if ok else "실패 ") + name + (f" — {note}" if note else ""))


# ---------- 1) 런타임: 운전자 흐름 ----------
P = "99가 9999"
r = Runtime(7)
r.advance(20)
res, err = r.driver_trip(P, "석회석", 24, "OD-1")
check("운행 등록 = GATE_IN(source app) · 앱 차량 표시", res == "ok" and r.rec.vehicles[P]["app"]
      and any(e["type"] == "GATE_IN" and e.get("source") == "app" and e["vehicle_id"] == P for e in r.rec.events))
check("진행 중인 운행이 있으면 다시 등록 못 함", r.driver_trip(P, "석회석", 24)[1] is not None)
check("중량 확인(1~30t)", r.driver_trip("98가 1111", "석회석", 99)[1] is not None)
same = None
for _ in range(80):
    r.advance(0.25)
    d = r.driver_view(P)
    if d["queue"]:
        row = next(q for q in r.state()["queue"] if q["vehicle_id"] == P)
        same = (d["queue"]["rank"], d["queue"]["call_at"], d["queue"]["wait_min"], d["queue"]["ahead"]) == (row["rank"], row["call_at"], row["wait_min"], row["rank"] - 1)
        break
check("대기 화면 순번 · 예상 호출 · 대기 · 앞 차량 = PC 순번표 같은 행", same, d["queue"] and f"{d['queue']['rank']}번째 · 호출 {d['queue']['call_at']}")
t0 = None
for _ in range(400):
    r.advance(0.25)
    v = r.rec.vehicles[P]
    if v["state"] == "moving":
        t0 = r.rec.t
        break
x0 = list(r.rec.vehicles[P]["xy"])
r.advance(1)
check("호출됐어도 [이동 시작] 전에는 앱 차량이 출발하지 않음(자동 CALL_ACK 없음)",
      t0 is not None and r.rec.vehicles[P]["xy"] == x0 and not any(e["type"] == "CALL_ACK" and e["vehicle_id"] == P for e in r.rec.events))
check("도착은 [이동 시작] 뒤에", r.driver_arrive(P)[1] is not None)
ok, _ = r.driver_ack(P)
check("[이동 시작] = CALL_ACK(source app) → 출발", ok == "ok" and any(e["type"] == "CALL_ACK" and e.get("source") == "app" and e["vehicle_id"] == P for e in r.rec.events))
r.advance(0.5)
check("다시 누르면 이미 출발(ignored)", r.driver_ack(P)[0] == "ignored")
res, _ = r.driver_arrive(P)
v = r.rec.vehicles[P]
check("[도착했어요] = BAY_ENTER(source app, manual) → 하역 중", res == "ok" and v["state"] == "working"
      and any(e["type"] == "BAY_ENTER" and e.get("source") == "app" and e["payload"].get("manual") for e in r.rec.events))
el = v["work_elapsed"]
res2, _ = r.driver_arrive(P)
check("하역 중에 또 [도착했어요] → 중복 진입 없음(ignored · 경과 유지)", res2 == "ignored" and r.rec.vehicles[P]["work_elapsed"] == el)
for _ in range(200):
    r.advance(0.5)
    if r.rec.vehicles[P]["gone"]:
        break
d = r.driver_view(P)
check("하역 끝 → 건별 기록 1건 · 오늘 운행 1회 · 다음 운행 등록 가능", d["vehicle"] is None and d["trips_today"] == 1 and d["last_trip"]["id"] == P
      and r.driver_trip(P, "석회석", 20)[0] == "ok")
r.set_assumptions({"target_wait_min": 17})
check("가정값을 바꿔 다시 계산해도 앱 운행이 남음", r.driver_view(P)["trips_today"] == 1 and r.rec.vehicles.get(P) is not None)

r2 = Runtime(7)
r2.advance(20)
r2.driver_trip(P, "석회석", 24)
for _ in range(400):
    r2.advance(0.25)
    if r2.rec.vehicles[P]["state"] == "moving":
        break
r2.advance(3.5)
check("[이동 시작]이 3분 없으면 '호출 응답 없음' 알림 뒤 출발(시연이 멈추지 않게)",
      any(a["kind"] == "호출 응답 없음" and P in a["text"] for a in r2.rec.alerts_all) and r2.rec.vehicles[P]["acked"] == "timeout")

# ---------- 2) 서버: 권한 · 걸러진 내용 · 실시간 · 승인 동기화 ----------
from fastapi.testclient import TestClient  # noqa: E402
from app import auth, server  # noqa: E402

store = auth.AuthStore(":memory:")
auth.install(server.app, store)
server.rt.set_auto_approve(False)          # L2: 현장 승인해야 호출
store.create("P1", "abcd1234", "control", "관제")
store.create("FA", "abcd1234", "field", "현장A", hoppers=["A"])
store.create("FB", "abcd1234", "field", "현장B", hoppers=["B", "C"])
store.create("85자 2064", "abcd1234", "driver", "기사", vehicle={"plate": "85자 2064", "tonnage": "25t", "carrier": "운송사 A"})


def client(lid):
    c = TestClient(server.app)
    assert c.post("/api/auth/login", json={"login_id": lid, "password": "abcd1234"}).status_code == 200
    return c


server.rt.advance(90)
ca, cb, cd, cc = client("FA"), client("FB"), client("85자 2064"), client("P1")
d = cd.get("/api/driver/me").json()
check("운전자: 내 차(시뮬레이션 트럭 85자 2064)만 — 다른 차 정보 없음", d["plate"] == "85자 2064" and "vehicles" not in d and "queue" in d)
check("운전자: 관제 데이터 · 현장 API · 관제 실시간 403", cd.get("/api/state").status_code == 403 and cd.get("/api/field/home").status_code == 403
      and cd.post("/api/order/approve?vehicle_id=x").status_code == 403)
fa = ca.get("/api/field/home").json()
check("현장(호퍼 A 담당): 담당 하역장만 받음", [s["id"] for s in fa["sectors"]] == ["A"] and fa["yard"] is None
      and all(x["sector_id"] == "A" for x in fa["approvals"] + fa["safety"] + fa["checks"]))
check("현장: 관제 화면 API(/api/state · 순번 승인 원래 API)는 403", ca.get("/api/state").status_code == 403 and ca.post("/api/order/approve?vehicle_id=x").status_code == 403)
fb = cb.get("/api/field/home").json()
appr = (fb["approvals"] or [None])[0]
if appr:
    check("현장: 담당 밖 하역장 차 승인 → 403", ca.post("/api/field/approve?vehicle_id=" + appr["vehicle_id"]).status_code == 403, appr["vehicle_id"] + " → " + appr["sector_id"])
    p_before = {a["vehicle_id"] for a in cc.get("/api/panel2").json()["approvals"]}
    rr = cb.post("/api/field/approve?vehicle_id=" + appr["vehicle_id"])
    server.rt.advance(0.25)
    p_after = {a["vehicle_id"] for a in cc.get("/api/panel2").json()["approvals"]}
    v = server.rt.rec.vehicles[appr["vehicle_id"]]
    check("현장이 폰에서 승인 → PC 알림 패널에서 빠지고 호출이 나감", rr.status_code == 200 and appr["vehicle_id"] in p_before
          and appr["vehicle_id"] not in p_after and v["state"] in ("moving", "queue", "working"), f"{appr['vehicle_id']} → {v['state']}")
else:
    check("현장 B·C 승인 대기 차량이 있어야 함", False, "승인 대기 없음")
x = None
server.rt.set_auto_approve(True)           # 하차 중인 차가 생기게
for _ in range(80):
    for sid in "ABC":
        x = server.rt.force_safety(sid)
        if x:
            break
    if x:
        break
    server.rt.advance(0.5)
other = cb if x["sector_id"] == "A" else ca
mine = ca if x["sector_id"] == "A" else cb
check("현장: 담당 밖 안전 경고 확인 → 403", other.post(f"/api/field/safety/{x['id']}/ack").status_code == 403)
check("현장: 담당 안전 경고 확인 → 하차 재개 · PC 안전 탭에서도 처리됨", mine.post(f"/api/field/safety/{x['id']}/ack").status_code == 200
      and not any(y["id"] == x["id"] for y in cc.get("/api/panel2").json()["safety_open"]))
al = next((a for a in server.rt.rec.alerts_all if a.get("sector_id") in ("A", "B", "C") and not a.get("ack")), None)   # 호퍼가 있는 미확인 알림
if al:
    a_sec = next(a["sector_id"] for a in server.rt.rec.alerts_all if a["id"] == al["id"])
    own, oth = (ca, cb) if a_sec == "A" else (cb, ca)
    check("현장: 담당 밖 알림 확인 403 · 담당 알림 확인 200(PC에서도 확인됨)", oth.post(f"/api/field/alerts/{al['id']}/ack").status_code == 403
          and own.post(f"/api/field/alerts/{al['id']}/ack").status_code == 200 and next(a for a in server.rt.rec.alerts_all if a["id"] == al["id"])["ack"])
else:
    check("확인할 운영 알림이 있어야 함", False, f"t={server.rt.rec.t} checks={server.rt.panel2()['checks'][:1]} alerts={len(server.rt.rec.alerts_all)}")
with ca.websocket_connect("/ws/m") as w:
    m = w.receive_json()
check("실시간(/ws/m) 현장: 담당 하역장 내용만", m["role"] == "field" and [s["id"] for s in m["sectors"]] == ["A"] and "vehicles" not in m)
with cd.websocket_connect("/ws/m") as w:
    m = w.receive_json()
check("실시간(/ws/m) 운전자: 내 차 내용만", m["role"] == "driver" and m["plate"] == "85자 2064" and "sectors" not in m and "queue" in m)
blocked = []
for c_, path in ((cd, "/ws"), (ca, "/ws"), (cc, "/ws/m")):
    try:
        with c_.websocket_connect(path) as w:
            w.receive_text()
        blocked.append(False)
    except Exception:
        blocked.append(True)
check("관제 실시간(/ws)은 운전자 · 현장 막힘, 모바일 실시간은 관제 막힘", all(blocked), str(blocked))
cd2 = client("85자 2064")
cd2.post("/api/auth/logout")
check("로그아웃한 운전자는 모바일 API 401", cd2.get("/api/driver/me").status_code == 401)

bad = [x for x in results if not x[1]]
print(f"\n{len(results) - len(bad)}/{len(results)} 통과")
sys.exit(1 if bad else 0)
