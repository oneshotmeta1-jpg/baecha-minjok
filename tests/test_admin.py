"""R14 설정 › 가입 승인(관리자) — py tests/test_admin.py

관리자 API 권한, 승인 · 반려(사유) · 역할 변경 · 사용 중지/다시 사용 · 관리자 지정/해제 · 비밀번호 초기화,
세션 끊기, 마지막 관리자 보호, 처리 기록, 휴대폰 가림, 가입 신청 → 관리자 알림.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app.notify import Notifier  # noqa: E402

results = []


def check(name, ok, note=""):
    results.append((name, bool(ok)))
    print(("통과 " if ok else "실패 ") + name + (f" — {note}" if note else ""))


app = FastAPI()
st = auth.AuthStore(":memory:")
auth.install(app, st)
N = Notifier(st)
app.state.on_signup = N.signup_request
app.state.on_admin = lambda uid, how: (N.act_ref(f"signup#{uid}", how), N.db.commit())
app.add_api_route("/api/state", lambda: {"ok": True}, methods=["GET"])


def signup(c, **kw):
    base = {"name": "홍길동", "phone": "010-1234-5678", "password": "abcd1234", "password2": "abcd1234", "consent": {"terms": True, "safety": True}}
    return c.post("/api/auth/signup", json={**base, **kw})


def login(lid, pw="abcd1234"):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"login_id": lid, "password": pw})
    return c, r


c0 = TestClient(app)
signup(c0, role="control", emp_no="ADM1", dept="관제실", name="관리자")          # 첫 관제 = 관리자
signup(c0, role="control", emp_no="CTL2", dept="관제실", name="관제둘")
signup(c0, role="field", emp_no="FLD1", dept="원료입고팀", hoppers=["A"], name="현장하나", phone="01099998888")
signup(c0, role="driver", plate="82가 1234", tonnage="25t", carrier="운송사 A", name="기사")
uid = {u["login_id"]: u["id"] for u in st.list_users()}
adm, _ = login("ADM1")

# 1) 권한
check("로그인 안 하면 관리자 API 401", TestClient(app).get("/api/admin/users").status_code == 401)
st.admin_do(st.user(login_id="ADM1"), uid["CTL2"], "approve")
ctl, _ = login("CTL2")
drv, _ = login("82가 1234")
check("관리자가 아닌 관제 담당자 · 운전자는 관리자 API 403",
      ctl.get("/api/admin/users").status_code == 403 and ctl.post(f"/api/admin/users/{uid['FLD1']}/approve").status_code == 403
      and drv.get("/api/admin/users").status_code == 403)
d = adm.get("/api/admin/users").json()
f1 = next(u for u in d["users"] if u["login_id"] == "FLD1")
check("목록: 승인 대기가 맨 위 · 대기 수 · 휴대폰 가림(010-****-8888)", d["users"][0]["status"] == "pending" and d["pending"] == 1 and f1["phone"] == "010-****-8888")
check("/api/me: 관리자에게 승인 대기 수", adm.get("/api/me").json().get("pending_signups") == 1 and "pending_signups" not in ctl.get("/api/me").json())

# 2) 가입 신청 알림
row = st.db.execute("SELECT kind, title, body FROM notifications WHERE user_id=? AND ref=?", (uid["ADM1"], f"signup#{uid['FLD1']}")).fetchone()
check("가입 신청(승인 대기) → 관리자에게 '승인' 알림(이름 대신 역할 · 아이디)", row and row[0] == "승인" and "FLD1" in row[2] and "현장하나" not in row[1] + row[2], str(row))

# 3) 승인 · 반려
check("반려는 사유가 있어야 함", adm.post(f"/api/admin/users/{uid['FLD1']}/reject", json={"reason": " "}).status_code == 409)
r = adm.post(f"/api/admin/users/{uid['FLD1']}/reject", json={"reason": "사번 확인 불가"})
_, lr = login("FLD1")
check("반려 → 로그인 시 '반려됐어요 · 사유' 표시", r.status_code == 200 and lr.status_code == 403 and "사번 확인 불가" in lr.json()["detail"], lr.json()["detail"])
acted = st.db.execute("SELECT action FROM notifications WHERE ref=?", (f"signup#{uid['FLD1']}",)).fetchone()[0]
check("처리하면 가입 신청 알림 = 조치됨(반려)", acted == "반려")
adm.post(f"/api/admin/users/{uid['FLD1']}/approve")
fld, lr = login("FLD1")
check("승인 → 바로 로그인(반려 사유는 지워짐)", lr.status_code == 200 and st.list_users(q="FLD1")[0]["reject_reason"] is None)

# 4) 세션 끊기
check("현장 로그인 상태 확인", fld.get("/api/me").status_code == 200)
adm.post(f"/api/admin/users/{uid['FLD1']}/disable")
check("사용 중지 → 이미 로그인한 사람도 다음 요청부터 401 · 로그인 거절", fld.get("/api/me").status_code == 401 and login("FLD1")[1].status_code == 403)
adm.post(f"/api/admin/users/{uid['FLD1']}/enable")
fld, lr = login("FLD1")
check("다시 사용 → 로그인 됨", lr.status_code == 200)
check("현장 담당자로 바꿀 때 담당 하역장이 없으면 거절", adm.post(f"/api/admin/users/{uid['CTL2']}/role", json={"role": "field", "hoppers": []}).status_code == 409)
r = adm.post(f"/api/admin/users/{uid['FLD1']}/role", json={"role": "field", "hoppers": ["B", "C"]})
check("역할 · 담당 변경 → 그 사람 세션 끊김(다시 로그인) · 담당 B·C로", r.status_code == 200 and fld.get("/api/me").status_code == 401
      and st.user(login_id="FLD1")["hoppers"] == ["B", "C"])
check("차량 없는 계정은 운전자로 못 바꿈", adm.post(f"/api/admin/users/{uid['CTL2']}/role", json={"role": "driver"}).status_code == 409)

# 5) 마지막 관리자 보호
check("자기 자신은 사용 중지 못 함", adm.post(f"/api/admin/users/{uid['ADM1']}/disable").status_code == 409)
check("마지막 관리자는 해제 못 함", adm.post(f"/api/admin/users/{uid['ADM1']}/admin", json={"on": False}).status_code == 409)
check("마지막 관리자를 다른 역할로 못 바꿈", adm.post(f"/api/admin/users/{uid['ADM1']}/role", json={"role": "field", "hoppers": ["A"]}).status_code == 409)
check("관제가 아닌 사람은 관리자 지정 못 함", adm.post(f"/api/admin/users/{uid['FLD1']}/admin", json={"on": True}).status_code == 409)
r = adm.post(f"/api/admin/users/{uid['CTL2']}/admin", json={"on": True})
ctl2, _ = login("CTL2")
check("관리자 지정 → 그 사람도 관리자 API 사용", r.status_code == 200 and ctl2.get("/api/admin/users").status_code == 200)
check("관리자가 둘이면 한 명 해제 가능", ctl2.post(f"/api/admin/users/{uid['ADM1']}/admin", json={"on": False}).status_code == 200)
check("해제된 사람은 다음 요청부터 관리자 API 403", adm.get("/api/admin/users").status_code == 403)

# 6) 비밀번호 초기화 · 변경
r = ctl2.post(f"/api/admin/users/{uid['82가 1234']}/reset_password")
tmp = r.json().get("temp_password", "")
dump = "\n".join(str(x) for x in st.db.iterdump())
check("임시 비밀번호는 응답에 한 번만 · DB · 기록 어디에도 평문 없음", len(tmp) == 8 and tmp not in dump)
check("초기화하면 그 사람 세션 끊김", drv.get("/api/me").status_code == 401)
check("옛 비밀번호로는 로그인 안 됨", login("82가 1234")[1].status_code == 401)
dv, lr = login("82가 1234", tmp)
check("임시 비밀번호로 로그인 → must_change_pw 표시(새 비밀번호로 바꾸게)", lr.status_code == 200 and lr.json()["must_change_pw"] and dv.get("/api/me").json()["must_change_pw"])
check("새 비밀번호 규칙 · 확인 불일치 거절", dv.post("/api/auth/change_password", json={"old": tmp, "new": "short", "new2": "short"}).status_code == 422
      and dv.post("/api/auth/change_password", json={"old": tmp, "new": "newpass99", "new2": "newpass98"}).status_code == 422)
r = dv.post("/api/auth/change_password", json={"old": tmp, "new": "newpass99", "new2": "newpass99"})
check("바꾸면 must_change_pw 풀림 · 새 비밀번호로 로그인", r.status_code == 200 and not dv.get("/api/me").json()["must_change_pw"] and login("82가 1234", "newpass99")[1].status_code == 200)

# 7) 처리 기록
lg = ctl2.get("/api/admin/users").json()["log"]
acts = [l["action"] for l in lg]
check("처리 기록: 승인 · 반려 · 중지 · 다시 사용 · 역할 · 관리자 · 비밀번호 초기화 · 변경이 남음(최근 순)",
      {"approve", "reject", "disable", "enable", "role", "admin", "reset_password", "change_password"} <= set(acts) and lg[0]["action"] == "change_password", ", ".join(acts[:8]))
check("기록에 누가 · 누구를 · 사유", any(l["action"] == "reject" and l["by"] == "관리자" and l["to"] == "현장하나" and l["detail"] == "사번 확인 불가" for l in lg))

bad = [x for x in results if not x[1]]
print(f"\n{len(results) - len(bad)}/{len(results)} 통과")
sys.exit(1 if bad else 0)
