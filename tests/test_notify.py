"""R13 알림 — py tests/test_notify.py

라우팅(역할 · 담당 하역장 · 동의 · 내 차), 1분 묶음, 안전 30초 재전송(최대 3번) · 조치/해제 시 멈춤,
받음 · 읽음 · 조치 기록, 서버 API · 실시간(/ws/m)에 알림함 포함, 권한.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
os.environ["DOCK_MEMORY"] = "1"

from app.auth import AuthStore  # noqa: E402
from app.notify import Notifier  # noqa: E402
from app.runtime import Runtime  # noqa: E402

results = []


def check(name, ok, note=""):
    results.append((name, bool(ok)))
    print(("통과 " if ok else "실패 ") + name + (f" — {note}" if note else ""))


T = [1000.0]
st = AuthStore(":memory:")
N = Notifier(st, clock=lambda: T[0])
fa = st.create("FA", "abcd1234a", "field", "현장A", hoppers=["A", "YARD"], consent={"ops": True})
fb = st.create("FB", "abcd1234a", "field", "현장B", hoppers=["B"], consent={"ops": False})
fc = st.create("FC", "abcd1234a", "field", "현장C", hoppers=["C"], consent={"ops": True})
pc = st.create("P1", "abcd1234a", "control", "관제")
P = "99가 9999"
dr = st.create(P, "abcd1234a", "driver", "기사", vehicle={"plate": P})
other = st.create("98가 8888", "abcd1234a", "driver", "다른 기사", vehicle={"plate": "98가 8888"})


def inbox(u):
    return N.inbox(u["id"], limit=200)


def ev(typ, vid=None, sid=None, **p):
    return {"ts": "2026-11-09T08:10:00", "type": typ, "source": "engine", "vehicle_id": vid, "sector_id": sid, "payload": p}


# ---------- 1) 라우팅 표 ----------
def who(e):
    return sorted({(r[0]["id"], r[1]) for r in N.route(e)})


check("호출 → 그 차의 운전자만", who(ev("CALL", P, "C", bay=3, travel_min=2.3)) == [(dr["id"], "호출")])
check("안전 경고 → 그 차 운전자 + 그 호퍼 현장 + 관제(다른 호퍼 현장 · 다른 운전자 없음)",
      who(ev("SAFETY_ALERT", P, "A", id="SA-1", rule="덤프 반경 5m", dist_m=3)) == sorted([(dr["id"], "안전"), (fa["id"], "안전"), (pc["id"], "안전")]))
check("승인 요청(CALL_PENDING) → 그 호퍼 현장만", who(ev("CALL_PENDING", P, "B", reason="x")) == [(fb["id"], "승인")])
check("운영 알림(하차 지연) → 동의한 현장만(B는 동의 안 함 → 없음)",
      who(ev("ALERT", P, "C", id="AL-1", kind="하차 지연", text="t")) == [(fc["id"], "운영")] and who(ev("ALERT", P, "B", id="AL-2", kind="하차 지연", text="t")) == [])
check("호출 응답 없음 → 동의와 상관없이 그 호퍼 현장", who(ev("ALERT", P, "B", id="AL-3", kind="호출 응답 없음", text="t")) == [(fb["id"], "운영")])
check("대기 초과(하역장 없음) → 대기장 담당 현장", who(ev("ALERT", P, None, id="AL-4", kind="대기 초과", text="t")) == [(fa["id"], "운영")])
check("호출 알림(info '호출')은 현장에 보내지 않음", who(ev("ALERT", P, "A", id="AL-5", kind="호출", text="t")) == [])
check("운전자 [멈췄어요] → 그 호퍼 현장", who(ev("DRIVER_STOPPED", P, "A", id="SA-1")) == [(fa["id"], "안전")])
check("하역장 정지 → 그 호퍼 현장 + 관제", who(ev("YARD_DOWN", None, "C")) == sorted([(fc["id"], "운영"), (pc["id"], "운영")]))
check("하역장 변경(ORDER_CHANGED) → 그 차 운전자", who(ev("ORDER_CHANGED", P, "C", to="C", reason="x")) == [(dr["id"], "호출")])

# ---------- 2) 묶음 · 저장 ----------
e = ev("CALL", P, "C", bay=3, travel_min=2.3)
a = N.send(*N.route(e)[0], e)
T[0] += 30
b = N.send(*N.route(e)[0], e)
T[0] += 31
c = N.send(*N.route(e)[0], e)
check("같은 내용은 1분 안에 한 번만(묶음) · 1분 지나면 다시", a and b is None and c)
row = st.db.execute("SELECT kind, priority, title, sim_ts, sent_at FROM notifications WHERE id=?", (a,)).fetchone()
check("저장: 종류 · 우선순위 · 내용 · 이벤트 시각 · 보낸 시각", row[0] == "호출" and row[1] == 1 and "3번 베이" in row[2] and row[3] and row[4] == 1000.0, str(row))
body = " ".join(str(x) for x in st.db.execute("SELECT title, body FROM notifications").fetchall())
check("알림 내용에 이름 · 전화번호 없음(차량번호 · 하역장 · 베이만)", "기사" not in body and "010" not in body)

# ---------- 3) 런타임과 함께: 안전 재전송 · 멈춤 · 조치 기록 ----------
r = Runtime(7)
N.feed(r)                    # 처음 부르면 지금까지의 이벤트는 건너뜀(서버를 켠 시점부터)
r.advance(30)
x = None
for _ in range(80):
    x = next((r.force_safety(s) for s in "ABC" if any(v["state"] == "working" for v in r.rec.vehicles.values() if v["sector"] == s)), None)
    if x:
        break
    r.advance(0.5)
N.feed(r)
uid_field = {"A": fa, "B": fb, "C": fc}[x["sector_id"]]["id"]
nid = st.db.execute("SELECT id FROM notifications WHERE user_id=? AND ref=? AND event_type='SAFETY_ALERT'", (uid_field, x["id"])).fetchone()[0]
reps = []
for k in range(5):
    T[0] += 31
    N.tick(r)
    reps.append(st.db.execute("SELECT repeat FROM notifications WHERE id=?", (nid,)).fetchone()[0])
check("안전 경고: 조치 없으면 30초마다 다시, 최대 3번", reps == [1, 2, 3, 3, 3], str(reps))
check("다시 보내면 '받음'이 풀려 화면에 다시 뜸", st.db.execute("SELECT delivered_at FROM notifications WHERE id=?", (nid,)).fetchone()[0] is None)
r.safety_resolve(x["id"], "ack")
N.feed(r)
row = st.db.execute("SELECT acted_at, action FROM notifications WHERE id=?", (nid,)).fetchone()
T[0] += 31
N.tick(r)
check("관제 · 현장이 확인하면 조치 기록(확인)되고 재전송 멈춤", row[0] and row[1] == "확인" and st.db.execute("SELECT repeat FROM notifications WHERE id=?", (nid,)).fetchone()[0] == 3)

r.driver_trip(P, "석회석", 24)
for _ in range(400):
    r.advance(0.25)
    N.feed(r)
    if r.rec.vehicles[P]["state"] == "moving":
        break
ib = inbox(dr)
call = next((n for n in ib["items"] if n["kind"] == "호출" and n["link"] == "call"), None)
check("시뮬레이터 호출 → 운전자 알림함에 호출 · 새 알림(new)", call and any(n["id"] == call["id"] for n in ib["new"]), call and call["title"])
T[0] += 4
N.mark(dr["id"], [call["id"]], "delivered_at")
T[0] += 3
r.driver_ack(P)
N.feed(r)
T[0] += 2
N.mark(dr["id"], [call["id"]], "read_at")
lg = next(l for l in N.log() if l["id"] == call["id"])
check("받음 · 조치(이동 시작) · 읽음까지 초가 기록됨", lg["recv_sec"] is not None and lg["action"] == "이동 시작" and lg["act_sec"] is not None and lg["read_sec"] is not None, str(lg))
check("받은 알림은 new에서 빠지고 읽으면 안 읽음 수가 줄어듦", not any(n["id"] == call["id"] for n in inbox(dr)["new"]) and inbox(dr)["unread"] < ib["unread"])
N.mark(fa["id"], "all", "read_at")
check("모두 읽음", inbox(fa)["unread"] == 0)
check("다른 사람 알림은 읽음 처리 안 됨", N.mark(fb["id"], [call["id"]], "read_at") == 0)
r.set_assumptions({"target_wait_min": 17})
n0 = st.db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
N.feed(r)
check("재계산(시뮬레이션 다시 만듦) 때 지난 이벤트로 알림을 또 보내지 않음", st.db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == n0)

# ---------- 4) 서버: API · 실시간 · 권한 ----------
from fastapi.testclient import TestClient  # noqa: E402
from app import auth, server  # noqa: E402

store = AuthStore(":memory:")
auth.install(server.app, store)
server.NOTI = Notifier(store)
store.create("FA", "abcd1234a", "field", "현장A", hoppers=["A"], consent={"ops": True})
store.create("P1", "abcd1234a", "control", "관제")
store.create(P, "abcd1234a", "driver", "기사", vehicle={"plate": P})


def client(lid):
    c_ = TestClient(server.app)
    assert c_.post("/api/auth/login", json={"login_id": lid, "password": "abcd1234a"}).status_code == 200
    return c_


server.NOTI.feed(server.rt)
cd, ca, cc = client(P), client("FA"), client("P1")
cd.post("/api/driver/trip", json={"cargo": "석회석", "load_t": 24})
for _ in range(400):
    server.rt.advance(0.25)
    server.NOTI.feed(server.rt)
    if server.rt.rec.vehicles[P]["state"] == "moving":
        break
me = cd.get("/api/driver/me").json()
check("운전자 /api/driver/me에 내 알림함(notes) — 호출 알림", me["notes"]["unread"] >= 1 and any(n["kind"] == "호출" for n in me["notes"]["items"]))
ids = [n["id"] for n in me["notes"]["new"]]
check("받음 표시 API(seen) → new에서 빠짐", cd.post("/api/notify/seen", json={"ids": ids}).json()["n"] == len(ids) and not cd.get("/api/notify/inbox").json()["new"])
check("읽음 API: 남의 알림 id로는 0건", ca.post("/api/notify/read", json={"ids": ids}).json()["n"] == 0)
check("알림 기록(/api/notify/log)은 관제만", cc.get("/api/notify/log").status_code == 200 and ca.get("/api/notify/log").status_code == 403 and cd.get("/api/notify/log").status_code == 403)
with cd.websocket_connect("/ws/m") as w:
    m = w.receive_json()
check("실시간(/ws/m)에도 내 알림함", "notes" in m and m["notes"]["unread"] >= 1)

bad = [x for x in results if not x[1]]
print(f"\n{len(results) - len(bad)}/{len(results)} 통과")
sys.exit(1 if bad else 0)
