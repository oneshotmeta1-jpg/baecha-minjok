"""계정 · 로그인 · 역할 권한 (R11)

- 계정·세션은 SQLite(data/auth.db, git 제외 · --check면 메모리). 비밀번호는 솔트 + scrypt 해시로만 저장한다(평문 저장·로그 출력 없음).
- 세션은 HttpOnly 쿠키. 쿠키에는 임의 토큰만, DB에는 그 토큰의 SHA-256만 둔다. "로그인 유지" = 14일, 아니면 12시간(브라우저 닫으면 끝).
- 같은 아이디로 5번 틀리면 1분 잠금(맞는 비밀번호도 거절). 성공하면 횟수 초기화.
- 역할: control(관제 담당자) · field(현장 담당자) · driver(운전자). 권한은 아래 RULES 한 표로 검사한다.
- 서버 --no-auth면 지금처럼 로그인 없이(관제 담당자로 본다).
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time

from fastapi import Body, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

ROLES = {"control": "관제 담당자", "field": "현장 담당자", "driver": "운전자"}
HOPPERS = ["A", "B", "C", "YARD"]
COOKIE = "dock_session"
REMEMBER_SEC = 14 * 24 * 3600
SESSION_SEC = 12 * 3600
MAX_FAIL, LOCK_SEC = 5, 60
SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)
PLATE = re.compile(r"^\d{2,3}[가-힣]\s?\d{4}$")          # 차량번호 예: 82가 1234

# 권한 표 (위에서부터 처음 맞는 줄): (메서드, 경로 정규식, 허용 역할). 없는 /api 경로는 GET = 관제·현장, 그 밖 = 관제만.
RULES = [
    ("POST", r"^/api/auth/change_password$", {"control", "field", "driver"}),
    ("*", r"^/api/auth/", {"public"}),
    ("GET", r"^/api/health$", {"public"}),
    ("GET", r"^/api/me$", {"control", "field", "driver"}),
    ("*", r"^/api/admin/", {"control"}),                                        # 관리자 화면 — 관제 담당자 중 관리자만(처리 함수에서 한 번 더)
    ("*", r"^/api/driver/", {"driver"}),                                        # 운전자 = 자기 차량만 (서버가 로그인한 차량번호로 고름)
    ("*", r"^/api/field/", {"field", "control"}),                               # 현장 = 담당 하역장만 (서버가 담당 하역장으로 거름)
    ("GET", r"^/api/notify/log$", {"control"}),                                 # 알림 기록(받음 · 읽음 · 조치까지 초) — 관제
    ("*", r"^/api/notify/", {"control", "field", "driver"}),                    # 내 알림함 · 받음/읽음 표시(서버가 로그인한 사람 것만)
    ("POST", r"^/api/ingest$", {"control", "ingest"}),                          # 영상 모듈: 이 PC에서 또는 토큰
    ("WS", r"^/ws$", {"control"}),                                              # 관제 화면 실시간 = 전체 상태
    ("WS", r"^/ws/m$", {"field", "driver"}),                                    # 모바일 실시간 = 역할별로 걸러 보냄
]


def allowed_roles(method, path):
    for m, pat, roles in RULES:
        if (m == "*" or m == method) and re.search(pat, path):
            return roles
    if path.startswith("/api/") or path.startswith("/media/"):
        return {"control"}                      # 관제 화면 API는 관제만 (현장 · 운전자는 /api/field · /api/driver)
    return {"public"}


def hash_pw(pw, salt=None):
    salt = salt or os.urandom(16)
    return salt, hashlib.scrypt(pw.encode("utf-8"), salt=salt, **SCRYPT)


def pw_problem(pw):
    if len(pw or "") < 8 or not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        return "비밀번호는 8자 이상, 영문과 숫자를 섞어 주세요."
    return None


class AuthStore:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, login_id TEXT UNIQUE NOT NULL, role TEXT NOT NULL,
                name TEXT NOT NULL, phone TEXT, dept TEXT, hoppers TEXT, vehicle TEXT, consent TEXT,
                status TEXT NOT NULL, is_admin INTEGER DEFAULT 0, pw_salt BLOB NOT NULL, pw_hash BLOB NOT NULL, created_at REAL);
            CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL, remember INTEGER);
            CREATE TABLE IF NOT EXISTS fails (login_id TEXT PRIMARY KEY, n INTEGER NOT NULL, locked_until REAL);
            CREATE TABLE IF NOT EXISTS auth_log (id INTEGER PRIMARY KEY, ts REAL, actor_id INTEGER, target_id INTEGER, action TEXT, detail TEXT);
        """)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(users)")}
        for c, ddl in (("reject_reason", "TEXT"), ("must_change_pw", "INTEGER DEFAULT 0"), ("decided_at", "REAL")):
            if c not in cols:                                     # R14: 반려 사유 · 임시 비밀번호 · 처리 시각
                self.db.execute(f"ALTER TABLE users ADD COLUMN {c} {ddl}")
        self.db.commit()

    # ---------- 계정 ----------
    def _row(self, r):
        if not r:
            return None
        k = ["id", "login_id", "role", "name", "phone", "dept", "hoppers", "vehicle", "consent", "status", "is_admin", "created_at"]
        u = dict(zip(k, r))
        for f in ("hoppers", "vehicle", "consent"):
            u[f] = json.loads(u[f]) if u[f] else None
        u["is_admin"] = bool(u["is_admin"])
        u["role_name"] = ROLES[u["role"]]
        return u

    def user(self, uid=None, login_id=None):
        q = "SELECT id,login_id,role,name,phone,dept,hoppers,vehicle,consent,status,is_admin,created_at FROM users WHERE "
        r = self.db.execute(q + ("id=?" if uid is not None else "login_id=?"), (uid if uid is not None else login_id,)).fetchone()
        return self._row(r)

    def has_admin(self):
        return bool(self.db.execute("SELECT 1 FROM users WHERE is_admin=1 AND status='active'").fetchone())

    def create(self, login_id, password, role, name, status="active", is_admin=False, phone=None, dept=None, hoppers=None, vehicle=None, consent=None):
        salt, h = hash_pw(password)
        cur = self.db.execute(
            "INSERT INTO users (login_id,role,name,phone,dept,hoppers,vehicle,consent,status,is_admin,pw_salt,pw_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (login_id, role, name, phone, dept, json.dumps(hoppers or [], ensure_ascii=False), json.dumps(vehicle, ensure_ascii=False) if vehicle else None,
             json.dumps(consent or {}, ensure_ascii=False), status, int(is_admin), salt, h, time.time()))
        self.db.commit()
        return self.user(uid=cur.lastrowid)

    def set_status(self, login_id, status, is_admin=None):
        self.db.execute("UPDATE users SET status=?" + (", is_admin=?" if is_admin is not None else "") + " WHERE login_id=?",
                        (status, int(is_admin), login_id) if is_admin is not None else (status, login_id))
        self.db.commit()

    def signup(self, d):
        """회원가입 3단계의 ② 정보 → 계정. 관제·현장 = 승인 대기, 운전자 = 차량번호 확인 후 바로 사용.
        관리자가 아직 없으면 처음 가입한 관제 담당자를 관리자로 바로 쓴다(첫 실행 부트스트랩)."""
        role = d.get("role")
        if role not in ROLES:
            return None, {"role": "역할을 골라 주세요."}
        err = {}
        name = (d.get("name") or "").strip()
        if not name:
            err["name"] = "이름을 입력해 주세요."
        pw = d.get("password") or ""
        if pw_problem(pw):
            err["password"] = pw_problem(pw)
        if pw != d.get("password2"):
            err["password2"] = "비밀번호가 서로 달라요."
        phone = re.sub(r"\D", "", d.get("phone") or "")
        if not re.fullmatch(r"01\d{8,9}", phone):
            err["phone"] = "휴대폰 번호를 확인해 주세요(010으로 시작하는 숫자)."
        c = d.get("consent") or {}
        if not c.get("terms"):
            err["terms"] = "서비스 이용약관에 동의해야 가입할 수 있어요."
        if role in ("field", "driver") and not c.get("safety"):
            err["safety"] = "안전 경고 알림은 끌 수 없어요(덤프 하역 반경 경고)."
        vehicle, hoppers, dept = None, [], None
        if role == "driver":
            plate = re.sub(r"\s+", " ", (d.get("plate") or "").strip())
            if not PLATE.match(plate):
                err["plate"] = "차량번호를 확인해 주세요(예: 82가 1234)."
            if d.get("tonnage") not in ("15t", "25t"):
                err["tonnage"] = "톤급을 골라 주세요."
            if not d.get("carrier"):
                err["carrier"] = "소속 운송사를 골라 주세요."
            login_id = plate
            vehicle = {"plate": plate, "tonnage": d.get("tonnage"), "carrier": d.get("carrier")}
        else:
            login_id = (d.get("emp_no") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9-]{4,20}", login_id):
                err["emp_no"] = "사번을 확인해 주세요(영문·숫자 4~20자)."
            dept = d.get("dept")
            if not dept:
                err["dept"] = "소속 부서를 골라 주세요."
            if role == "field":
                hoppers = [h for h in (d.get("hoppers") or []) if h in HOPPERS]
                if not hoppers:
                    err["hoppers"] = "담당 하역장을 하나 이상 골라 주세요."
        if login_id and self.user(login_id=login_id):
            err["plate" if role == "driver" else "emp_no"] = "이미 가입된 " + ("차량번호" if role == "driver" else "사번") + "예요."
        if err:
            return None, err
        first_admin = role == "control" and not self.has_admin()
        status = "active" if role == "driver" or first_admin else "pending"
        consent = {"terms": True, "safety": bool(c.get("safety")), "ops": bool(c.get("ops")), "location": bool(c.get("location"))}
        u = self.create(login_id, pw, role, name, status, is_admin=first_admin, phone=phone, dept=dept, hoppers=hoppers, vehicle=vehicle, consent=consent)
        return u, None

    # ---------- 로그인 · 세션 ----------
    def login(self, login_id, password, now=None):
        """→ ("ok", user) | ("locked", 남은 초) | ("bad", 남은 기회) | ("pending"|"rejected", user)"""
        now = now or time.time()
        f = self.db.execute("SELECT n, locked_until FROM fails WHERE login_id=?", (login_id,)).fetchone()
        if f and f[1] and f[1] > now:
            return "locked", int(f[1] - now + 0.999)
        r = self.db.execute("SELECT id, pw_salt, pw_hash, status FROM users WHERE login_id=?", (login_id,)).fetchone()
        ok = False
        if r:
            ok = hmac.compare_digest(hash_pw(password or "", r[1])[1], r[2])
        else:
            hash_pw(password or "")              # 없는 아이디도 같은 시간이 걸리게
        if not ok:
            n = (f[0] if f and not (f[1] and f[1] <= now and f[0] >= MAX_FAIL) else 0) + 1
            locked = now + LOCK_SEC if n >= MAX_FAIL else None
            self.db.execute("INSERT INTO fails (login_id,n,locked_until) VALUES (?,?,?) ON CONFLICT(login_id) DO UPDATE SET n=excluded.n, locked_until=excluded.locked_until",
                            (login_id, n, locked))
            self.db.commit()
            return ("locked", LOCK_SEC) if locked else ("bad", MAX_FAIL - n)
        self.db.execute("DELETE FROM fails WHERE login_id=?", (login_id,))
        self.db.commit()
        u = self.user(uid=r[0])
        if u["status"] != "active":
            return u["status"], u
        u["must_change_pw"] = bool(self.db.execute("SELECT must_change_pw FROM users WHERE id=?", (u["id"],)).fetchone()[0])
        return "ok", u

    def new_session(self, uid, remember, now=None):
        now = now or time.time()
        tok = secrets.token_urlsafe(32)
        self.db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (hashlib.sha256(tok.encode()).hexdigest(), uid,
                                                                  now + (REMEMBER_SEC if remember else SESSION_SEC), int(bool(remember))))
        self.db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        self.db.commit()
        return tok

    def session_user(self, tok, now=None):
        if not tok:
            return None
        now = now or time.time()
        r = self.db.execute("SELECT user_id, expires FROM sessions WHERE token_hash=?", (hashlib.sha256(tok.encode()).hexdigest(),)).fetchone()
        if not r or r[1] < now:
            return None
        u = self.user(uid=r[0])
        return u if u and u["status"] == "active" else None

    def end_session(self, tok):
        if tok:
            self.db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(tok.encode()).hexdigest(),))
            self.db.commit()


# ---------- 관리자 (R14): 가입 승인 · 반려 · 역할 · 사용 중지 · 관리자 지정 · 비밀번호 초기화 ----------
def mask_phone(p):
    p = re.sub(r"\D", "", p or "")
    return f"{p[:3]}-****-{p[-4:]}" if len(p) >= 10 else ("" if not p else "****")


class AdminError(Exception):
    pass


def _admin_methods():
    def users(self, q="", role="", status=""):
        rows = self.db.execute("SELECT id, login_id, role, name, phone, dept, hoppers, vehicle, consent, status, is_admin, created_at, reject_reason, decided_at FROM users ORDER BY "
                               "CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC").fetchall()
        out = []
        for r in rows:
            u = {"id": r[0], "login_id": r[1], "role": r[2], "role_name": ROLES[r[2]], "name": r[3], "phone": mask_phone(r[4]), "dept": r[5],
                 "hoppers": json.loads(r[6] or "[]"), "vehicle": json.loads(r[7]) if r[7] else None, "consent": json.loads(r[8] or "{}"),
                 "status": r[9], "is_admin": bool(r[10]), "created_at": r[11], "reject_reason": r[12], "decided_at": r[13]}
            if q and q not in u["name"] and q not in u["login_id"]:
                continue
            if role and u["role"] != role or status and u["status"] != status:
                continue
            out.append(u)
        return out

    def log_action(self, actor_id, target_id, action, detail=""):
        self.db.execute("INSERT INTO auth_log (ts, actor_id, target_id, action, detail) VALUES (?,?,?,?,?)", (time.time(), actor_id, target_id, action, detail))

    def recent_log(self, limit=20):
        rows = self.db.execute("""SELECT l.ts, a.name, t.name, t.login_id, l.action, l.detail FROM auth_log l
                                  LEFT JOIN users a ON a.id=l.actor_id LEFT JOIN users t ON t.id=l.target_id ORDER BY l.id DESC LIMIT ?""", (limit,)).fetchall()
        return [{"ts": time.strftime("%m-%d %H:%M", time.localtime(r[0])), "by": r[1], "to": r[2], "to_id": r[3], "action": r[4], "detail": r[5]} for r in rows]

    def kill_sessions(self, uid):
        self.db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))

    def _target(self, uid):
        u = self.user(uid=uid)
        if not u:
            raise AdminError("없는 계정이에요.")
        return u

    def _admins(self, exclude=None):
        return self.db.execute("SELECT COUNT(*) FROM users WHERE is_admin=1 AND status='active' AND id<>?", (exclude or -1,)).fetchone()[0]

    def admin_do(self, actor, uid, action, **kw):
        """관리자 처리 하나. 반환: (메시지, 추가 정보). 잘못되면 AdminError."""
        t = self._target(uid)
        now = time.time()
        extra = {}
        if action == "approve":
            if t["status"] not in ("pending", "rejected"):
                raise AdminError("승인 대기 · 반려 계정만 승인할 수 있어요.")
            self.db.execute("UPDATE users SET status='active', reject_reason=NULL, decided_at=? WHERE id=?", (now, uid))
            msg, detail = f"{t['name']} 승인", ""
        elif action == "reject":
            reason = (kw.get("reason") or "").strip()[:200]
            if not reason:
                raise AdminError("반려 사유를 적어 주세요.")
            if t["id"] == actor["id"]:
                raise AdminError("자기 자신은 반려할 수 없어요.")
            if t["is_admin"] and not self._admins(exclude=uid):
                raise AdminError("마지막 관리자는 반려할 수 없어요.")
            self.db.execute("UPDATE users SET status='rejected', reject_reason=?, decided_at=? WHERE id=?", (reason, now, uid))
            self.kill_sessions(uid)
            msg, detail = f"{t['name']} 반려", reason
        elif action == "role":
            role, hoppers = kw.get("role") or t["role"], kw.get("hoppers")
            if role not in ROLES:
                raise AdminError("역할을 확인해 주세요.")
            if role == "driver" and not t["vehicle"]:
                raise AdminError("차량 정보가 없는 계정은 운전자로 바꿀 수 없어요.")
            if t["is_admin"] and role != "control" and not self._admins(exclude=uid):
                raise AdminError("마지막 관리자는 관제 담당자에서 바꿀 수 없어요.")
            hop = [h for h in (hoppers if hoppers is not None else t["hoppers"]) if h in HOPPERS]
            if role == "field" and not hop:
                raise AdminError("현장 담당자는 담당 하역장을 하나 이상 골라 주세요.")
            self.db.execute("UPDATE users SET role=?, hoppers=?, is_admin=CASE WHEN ?='control' THEN is_admin ELSE 0 END WHERE id=?",
                            (role, json.dumps(hop if role == "field" else [], ensure_ascii=False), role, uid))
            self.kill_sessions(uid)               # 화면 · 권한이 바뀌니 다시 로그인
            msg, detail = f"{t['name']} 역할 변경", f"{ROLES[t['role']]} → {ROLES[role]}" + (f" ({', '.join(hop)})" if role == "field" else "")
        elif action == "disable":
            if t["id"] == actor["id"]:
                raise AdminError("자기 자신은 사용 중지할 수 없어요.")
            if t["is_admin"] and not self._admins(exclude=uid):
                raise AdminError("마지막 관리자는 사용 중지할 수 없어요.")
            self.db.execute("UPDATE users SET status='disabled', decided_at=? WHERE id=?", (now, uid))
            self.kill_sessions(uid)
            msg, detail = f"{t['name']} 사용 중지", ""
        elif action == "enable":
            if t["status"] != "disabled":
                raise AdminError("사용 중지된 계정만 다시 쓸 수 있어요.")
            self.db.execute("UPDATE users SET status='active', decided_at=? WHERE id=?", (now, uid))
            msg, detail = f"{t['name']} 다시 사용", ""
        elif action == "admin":
            on = bool(kw.get("on"))
            if on and (t["role"] != "control" or t["status"] != "active"):
                raise AdminError("사용 중인 관제 담당자만 관리자로 지정할 수 있어요.")
            if not on and t["is_admin"] and not self._admins(exclude=uid):
                raise AdminError("마지막 관리자는 해제할 수 없어요.")
            self.db.execute("UPDATE users SET is_admin=? WHERE id=?", (int(on), uid))
            msg, detail = f"{t['name']} 관리자 {'지정' if on else '해제'}", ""
        elif action == "reset_password":
            tmp = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz") for _ in range(4)) + "".join(secrets.choice("23456789") for _ in range(4))
            salt, h = hash_pw(tmp)
            self.db.execute("UPDATE users SET pw_salt=?, pw_hash=?, must_change_pw=1 WHERE id=?", (salt, h, uid))
            self.db.execute("DELETE FROM fails WHERE login_id=?", (t["login_id"],))
            self.kill_sessions(uid)
            msg, detail = f"{t['name']} 비밀번호 초기화", "임시 비밀번호 발급(화면에 한 번만 표시)"
            extra["temp_password"] = tmp            # 저장 · 기록하지 않고 응답에만 한 번
        else:
            raise AdminError("모르는 처리예요.")
        self.log_action(actor["id"], uid, action, detail)
        self.db.commit()
        return msg, extra

    def change_password(self, uid, old, new):
        r = self.db.execute("SELECT pw_salt, pw_hash FROM users WHERE id=?", (uid,)).fetchone()
        if not r or not hmac.compare_digest(hash_pw(old or "", r[0])[1], r[1]):
            raise AdminError("지금 비밀번호가 맞지 않아요.")
        if pw_problem(new):
            raise AdminError(pw_problem(new))
        if new == old:
            raise AdminError("새 비밀번호는 지금과 달라야 해요.")
        salt, h = hash_pw(new)
        self.db.execute("UPDATE users SET pw_salt=?, pw_hash=?, must_change_pw=0 WHERE id=?", (salt, h, uid))
        self.log_action(uid, uid, "change_password", "")
        self.db.commit()

    def pending_count(self):
        return self.db.execute("SELECT COUNT(*) FROM users WHERE status='pending'").fetchone()[0]

    for f in (users, log_action, recent_log, kill_sessions, _target, _admins, admin_do, change_password, pending_count):
        setattr(AuthStore, f.__name__ if f.__name__ != "users" else "list_users", f)


_admin_methods()

ADMIN_NAMES = {"approve": "승인", "reject": "반려", "role": "역할 변경", "disable": "사용 중지", "enable": "다시 사용", "admin": "관리자 지정/해제",
               "reset_password": "비밀번호 초기화", "change_password": "비밀번호 변경"}

GUEST = {"id": 0, "login_id": "-", "role": "control", "role_name": ROLES["control"], "name": "관제 담당자", "is_admin": True, "no_auth": True}


def home_of(u):
    return "/" if u["role"] == "control" else "/m/"


def public_user(u):
    return {k: u.get(k) for k in ("login_id", "role", "role_name", "name", "dept", "hoppers", "vehicle", "is_admin", "status", "no_auth") if k in u}


def install(app, store, no_auth=False, secure_cookie=False, ingest_token=None, shift_label="오후 교대"):
    """앱에 로그인 API · 권한 검사를 붙인다. store=None 또는 no_auth=True면 검사하지 않는다(지금처럼)."""
    off = no_auth or store is None

    def current(request):
        if off:
            return dict(GUEST, shift=shift_label)
        u = store.session_user(request.cookies.get(COOKIE))
        return dict(u, shift=shift_label) if u else None

    app.state.auth_current = current

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path, method = request.url.path, request.method
        if off:
            return await call_next(request)
        roles = allowed_roles(method, path)
        mobile = path == "/m" or path.startswith("/m/")
        if path in ("/", "/index.html", "/web/index.html") or mobile:
            u = current(request)
            if not u:
                return RedirectResponse("/login", status_code=303)
            want = "/m/" if mobile else "/"
            if home_of(u) != want and not (u["role"] == "control" and want == "/m/"):
                return RedirectResponse(home_of(u), status_code=303)
            return await call_next(request)
        if "public" in roles:
            return await call_next(request)
        u = current(request)
        if "ingest" in roles and not u:
            host = request.client.host if request.client else ""
            tok = request.headers.get("x-ingest-token")
            if host in ("127.0.0.1", "::1", "localhost", "testclient") or (ingest_token and tok and hmac.compare_digest(tok, ingest_token)):
                return await call_next(request)
        if not u:
            return JSONResponse({"detail": "로그인이 필요해요.", "login": "/login"}, status_code=401)
        if u["role"] not in roles:
            return JSONResponse({"detail": f"{ROLES[u['role']]} 계정으로는 할 수 없는 작업이에요."}, status_code=403)
        request.state.user = u
        return await call_next(request)

    def ws_user(socket, path="/ws"):
        if off:
            return GUEST
        u = store.session_user(socket.cookies.get(COOKIE))
        return u if u and u["role"] in allowed_roles("WS", path) else None

    app.state.ws_user = ws_user

    @app.post("/api/auth/login")
    async def login(body: dict = Body(...)):
        if off:
            return {"user": public_user(GUEST), "home": "/"}
        lid, pw = (body.get("login_id") or "").strip(), body.get("password") or ""
        if not lid or not pw:
            raise HTTPException(400, "아이디와 비밀번호를 입력해 주세요.")
        res, x = store.login(lid, pw)
        if res == "locked":
            return JSONResponse({"detail": f"5번 틀려서 잠겼어요. {x}초 뒤에 다시 해 주세요.", "retry_after": x}, status_code=423)
        if res == "bad":
            return JSONResponse({"detail": "아이디 또는 비밀번호가 맞지 않아요." + (f" (남은 기회 {x}번)" if x <= 2 else ""), "left": x}, status_code=401)
        if res == "pending":
            return JSONResponse({"detail": "관리자 승인을 기다리고 있어요. 승인되면 로그인할 수 있어요."}, status_code=403)
        if res == "rejected":
            why = store.db.execute("SELECT reject_reason FROM users WHERE id=?", (x["id"],)).fetchone()[0]
            return JSONResponse({"detail": "가입 신청이 반려됐어요." + (f" 사유: {why}" if why else " 관리자에게 문의해 주세요."), "reason": why}, status_code=403)
        if res == "disabled":
            return JSONResponse({"detail": "사용 중지된 계정이에요. 관리자에게 문의해 주세요."}, status_code=403)
        remember = bool(body.get("remember"))
        tok = store.new_session(x["id"], remember)
        r = JSONResponse({"user": public_user(x), "home": home_of(x), "must_change_pw": bool(x.get("must_change_pw"))})
        r.set_cookie(COOKIE, tok, httponly=True, samesite="lax", secure=secure_cookie, path="/",
                     max_age=REMEMBER_SEC if remember else None)
        return r

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        if store:
            store.end_session(request.cookies.get(COOKIE))
        r = JSONResponse({"ok": True, "login": "/login"})
        r.delete_cookie(COOKIE, path="/")
        return r

    @app.post("/api/auth/signup")
    async def signup(body: dict = Body(...)):
        if off:
            raise HTTPException(400, "로그인 없이 켠 서버(--no-auth)에서는 가입하지 않아요.")
        u, err = store.signup(body)
        if err:
            return JSONResponse({"detail": "입력을 확인해 주세요.", "errors": err}, status_code=422)
        msg = {"active": "가입됐어요. 바로 로그인할 수 있어요.", "pending": "가입 신청을 받았어요. 관리자가 확인한 뒤 승인하면 로그인할 수 있어요."}[u["status"]]
        if u["status"] == "pending" and getattr(app.state, "on_signup", None):
            app.state.on_signup(u)
        if u["is_admin"]:
            msg = "첫 관제 담당자라 관리자로 가입됐어요. 바로 로그인할 수 있어요."
        return {"status": u["status"], "message": msg, "login_id": u["login_id"], "role": u["role"]}

    def need_admin(request):
        u = current(request)
        if not u or u.get("role") != "control" or not u.get("is_admin"):
            raise HTTPException(403, "관리자만 할 수 있어요.")
        if off:
            raise HTTPException(400, "로그인 없이 켠 서버(--no-auth)에는 계정이 없어요.")
        return u

    @app.get("/api/admin/users")
    async def admin_users(request: Request, q: str = "", role: str = "", status: str = ""):
        need_admin(request)
        return {"users": store.list_users(q.strip(), role, status), "pending": store.pending_count(), "log": store.recent_log(20)}

    @app.post("/api/admin/users/{uid}/{action}")
    async def admin_action(request: Request, uid: int, action: str, body: dict = Body(default={})):
        a = need_admin(request)
        try:
            msg, extra = store.admin_do(a, uid, action, **(body or {}))
        except AdminError as e:
            raise HTTPException(409, str(e))
        if action in ("approve", "reject") and getattr(app.state, "on_admin", None):
            app.state.on_admin(uid, ADMIN_NAMES[action])          # 가입 신청 알림 = 조치됨
        return {"message": msg, **extra}

    @app.post("/api/auth/change_password")
    async def change_password(request: Request, body: dict = Body(...)):
        u = current(request)
        if not u or off:
            raise HTTPException(401, "로그인이 필요해요.")
        if body.get("new") != body.get("new2"):
            raise HTTPException(422, "새 비밀번호가 서로 달라요.")
        try:
            store.change_password(u["id"], body.get("old"), body.get("new"))
        except AdminError as e:
            raise HTTPException(422, str(e))
        return {"ok": True, "home": home_of(u)}

    @app.get("/api/auth/check_id")
    async def check_id(login_id: str):
        return {"available": off or not store.user(login_id=login_id.strip())}

    @app.get("/api/me")
    async def me(request: Request):
        u = current(request)
        if not u:
            raise HTTPException(401, "로그인이 필요해요.")
        extra = {}
        if store and not off:
            extra["must_change_pw"] = bool(store.db.execute("SELECT must_change_pw FROM users WHERE id=?", (u["id"],)).fetchone()[0])
            if u.get("is_admin"):
                extra["pending_signups"] = store.pending_count()
        return dict(public_user(u), shift=u.get("shift"), home=home_of(u), **extra)

    return current
