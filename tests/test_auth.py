"""R11 계정 · 로그인 · 권한 확인 — py tests/test_auth.py

app/auth.py의 install()을 작은 FastAPI 앱에 붙여 실제 경로(관제 화면 API와 같은 주소)로 확인한다.
(app/server.py를 가져오면 이벤트 DB를 여는 런타임이 같이 떠서 여기선 쓰지 않는다)
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from fastapi import FastAPI, WebSocket  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402

results = []


def check(name, ok, note=""):
    results.append((name, bool(ok)))
    print(("통과 " if ok else "실패 ") + name + (f" — {note}" if note else ""))


def make(no_auth=False, store=None):
    app = FastAPI()
    st = None if no_auth else (store or auth.AuthStore(":memory:"))
    auth.install(app, st, no_auth=no_auth)

    @app.get("/")
    async def index():
        return {"page": "관제 화면"}

    @app.get("/m/")
    async def mobile():
        return {"page": "모바일"}

    for p in ("/api/state", "/api/rois", "/api/export/report.csv"):
        app.add_api_route(p, lambda: {"ok": True}, methods=["GET"])
    for p in ("/api/sim/start", "/api/order/approve", "/api/assumptions", "/api/safety/S1/ack", "/api/ingest", "/api/events/urgent"):
        app.add_api_route(p, lambda: {"ok": True}, methods=["POST"])

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        if not app.state.ws_user(socket):
            await socket.close(code=4401)
            return
        await socket.accept()
        await socket.send_text("ok")
        await socket.close()
    return app, st


def signup(c, **kw):
    base = {"name": "홍길동", "phone": "010-1234-5678", "password": "abcd1234", "password2": "abcd1234", "consent": {"terms": True, "safety": True}}
    return c.post("/api/auth/signup", json={**base, **kw})


app, st = make()
c = TestClient(app)

# 1) 해시 저장 · 가입 상태
r = signup(c, role="control", emp_no="P1001", dept="관제실")
check("첫 관제 담당자 가입 = 관리자 · 바로 사용(부트스트랩)", r.status_code == 200 and r.json()["status"] == "active" and st.user(login_id="P1001")["is_admin"], r.text)
row = st.db.execute("SELECT pw_salt, pw_hash FROM users WHERE login_id='P1001'").fetchone()
dump = "\n".join(str(x) for x in st.db.iterdump())
check("비밀번호는 솔트+scrypt 해시만 (DB 어디에도 평문 없음)", len(row[0]) == 16 and len(row[1]) == 32 and "abcd1234" not in dump)
r = signup(c, role="field", emp_no="F2001", dept="원료입고팀", hoppers=["A", "B"])
check("현장 담당자 가입 = 승인 대기", r.json()["status"] == "pending")
r = signup(c, role="driver", plate="82가 1234", tonnage="25t", carrier="운송사 A")
check("운전자 가입 = 차량번호 확인 후 바로 사용(아이디 = 차량번호)", r.json()["status"] == "active" and r.json()["login_id"] == "82가 1234")
r = signup(c, role="driver", plate="1234", tonnage="25t", carrier="", password="short", password2="x", consent={"terms": False, "safety": False})
e = r.json().get("errors", {})
check("가입 입력 확인: 차량번호 · 운송사 · 비밀번호 규칙 · 확인 불일치 · 약관 · 안전 알림 필수", r.status_code == 422 and {"plate", "carrier", "password", "password2", "terms", "safety"} <= set(e), ", ".join(e))
r = signup(c, role="field", emp_no="F2001", dept="원료입고팀", hoppers=["A"])
check("같은 사번 중복 가입 막음", r.status_code == 422 and "emp_no" in r.json()["errors"])

# 2) 로그인 · 세션 · 역할별 화면
check("로그인 안 하면 API 401 · 관제 화면은 로그인으로", c.get("/api/state").status_code == 401 and c.get("/", follow_redirects=False).headers.get("location") == "/login")
r = c.post("/api/auth/login", json={"login_id": "F2001", "password": "abcd1234"})
check("승인 대기 계정은 로그인 거절(403)", r.status_code == 403 and "승인" in r.json()["detail"])
r = c.post("/api/auth/login", json={"login_id": "P1001", "password": "abcd1234", "remember": True})
ck = r.headers.get("set-cookie", "")
check("관제 로그인 → 관제 화면, HttpOnly 쿠키 · 로그인 유지 14일", r.status_code == 200 and r.json()["home"] == "/" and "HttpOnly" in ck and "Max-Age=1209600" in ck, ck[:80])
check("관제: 화면 · 시연 · 기준값 · 내보내기 · 순번 승인 모두 됨",
      all(x.status_code == 200 for x in (c.get("/"), c.post("/api/sim/start"), c.post("/api/assumptions"), c.get("/api/export/report.csv"), c.post("/api/order/approve"))))
me = c.get("/api/me").json()
check("/api/me = 이름 · 역할 · 교대 (헤더 담당자 칩)", me["name"] == "홍길동" and me["role_name"] == "관제 담당자" and me["shift"] == "오후 교대")
with c.websocket_connect("/ws") as w:
    check("관제: 실시간 연결(WebSocket) 됨", w.receive_text() == "ok")
c.post("/api/auth/logout")
check("로그아웃하면 바로 401", c.get("/api/state").status_code == 401)

st.set_status("F2001", "active")       # 관리자 승인 (I8/I9 전에는 tools/users.py approve)
r = c.post("/api/auth/login", json={"login_id": "F2001", "password": "abcd1234"})
ck = r.headers.get("set-cookie", "")
check("승인 뒤 현장 담당자 로그인 → /m/, 로그인 유지 안 하면 브라우저 세션 쿠키", r.status_code == 200 and r.json()["home"] == "/m/" and "Max-Age" not in ck)
check("현장: 관제 화면 API(순번 승인 · 안전 확인 · 화면 조회)는 403 — 현장은 /api/field/*로만(R12, 담당 하역장 기준)",
      all(x.status_code == 403 for x in (c.post("/api/order/approve"), c.post("/api/safety/S1/ack"), c.get("/api/state"))))
check("현장: 시연(시작 · 긴급 입고) · 기준값 저장은 403",
      all(x.status_code == 403 for x in (c.post("/api/sim/start"), c.post("/api/events/urgent"), c.post("/api/assumptions"))))
check("현장이 관제 화면(/)을 열면 /m/으로", c.get("/", follow_redirects=False).headers.get("location") == "/m/")
c.post("/api/auth/logout")

c.post("/api/auth/login", json={"login_id": "82가 1234", "password": "abcd1234"})
check("운전자: 관제 데이터 · 순번 승인 403, 자기 정보(/api/me)만", c.get("/api/state").status_code == 403 and c.post("/api/order/approve").status_code == 403
      and c.get("/api/me").json()["vehicle"]["plate"] == "82가 1234")
try:
    with c.websocket_connect("/ws") as w:
        w.receive_text()
    ws_blocked = False
except Exception:
    ws_blocked = True
check("운전자: 관제 실시간 연결(WebSocket) 막힘", ws_blocked)
c.post("/api/auth/logout")

# 3) 세션 만료 · 잠금
tok = st.new_session(st.user(login_id="P1001")["id"], remember=False, now=time.time() - auth.SESSION_SEC - 5)
c.cookies.set(auth.COOKIE, tok)
check("세션 만료(12시간 지남) → 401", c.get("/api/state").status_code == 401)
c.cookies.clear()
codes = [c.post("/api/auth/login", json={"login_id": "P1001", "password": "wrong-pass1"}).status_code for _ in range(5)]
r = c.post("/api/auth/login", json={"login_id": "P1001", "password": "abcd1234"})
check("5번 틀리면 잠금 → 맞는 비밀번호도 1분간 거절(423 · 남은 초)", codes == [401, 401, 401, 401, 423] and r.status_code == 423 and 0 < r.json()["retry_after"] <= 60, f"{codes} → {r.status_code}")
st.db.execute("UPDATE fails SET locked_until=? WHERE login_id='P1001'", (time.time() - 1,))
r = c.post("/api/auth/login", json={"login_id": "P1001", "password": "abcd1234"})
check("1분 지나면 다시 로그인 · 성공하면 실패 횟수 초기화", r.status_code == 200 and not st.db.execute("SELECT 1 FROM fails WHERE login_id='P1001'").fetchone())
c.post("/api/auth/logout")

# 4) 영상 넣기 · --no-auth
check("영상 넣기(/api/ingest): 이 PC에서 오면 로그인 없이 됨", c.post("/api/ingest").status_code == 200)
app2, _ = make(no_auth=True)
c2 = TestClient(app2)
check("--no-auth: 로그인 없이 지금처럼(관제 담당자) — 화면 · 시연 · API 모두",
      c2.get("/").status_code == 200 and c2.post("/api/sim/start").status_code == 200 and c2.get("/api/me").json()["no_auth"])

bad = [r for r in results if not r[1]]
print(f"\n{len(results) - len(bad)}/{len(results)} 통과")
sys.exit(1 if bad else 0)
