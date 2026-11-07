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
    ("*", r"^/api/auth/", {"public"}),
    ("GET", r"^/api/health$", {"public"}),
    ("GET", r"^/api/me$", {"control", "field", "driver"}),
    ("GET", r"^/api/driver/me$", {"driver"}),                                   # 운전자 = 자기 차량만
    ("POST", r"^/api/order/(approve|approve_all|change|request_approval)$", {"control", "field"}),
    ("POST", r"^/api/approval$", {"control", "field"}),
    ("POST", r"^/api/safety/[^/]+/(ack|dismiss)$", {"control", "field"}),
    ("POST", r"^/api/alerts/[^/]+/(ack|ignore)$", {"control", "field"}),
    ("POST", r"^/api/preview$", {"control", "field"}),
    ("GET", r"^/api/export/", {"control"}),
    ("POST", r"^/api/ingest$", {"control", "ingest"}),                          # 영상 모듈: 이 PC에서 또는 토큰
    ("GET", r"^/media/", {"control", "field"}),
    ("WS", r"^/ws$", {"control", "field"}),
]


def allowed_roles(method, path):
    for m, pat, roles in RULES:
        if (m == "*" or m == method) and re.search(pat, path):
            return roles
    if path.startswith("/api/"):
        return {"control", "field"} if method == "GET" else {"control"}
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
        """)
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

    def ws_user(socket):
        if off:
            return GUEST
        u = store.session_user(socket.cookies.get(COOKIE))
        return u if u and u["role"] in allowed_roles("WS", "/ws") else None

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
            return JSONResponse({"detail": "가입 신청이 반려됐어요. 관리자에게 문의해 주세요."}, status_code=403)
        remember = bool(body.get("remember"))
        tok = store.new_session(x["id"], remember)
        r = JSONResponse({"user": public_user(x), "home": home_of(x)})
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
        if u["is_admin"]:
            msg = "첫 관제 담당자라 관리자로 가입됐어요. 바로 로그인할 수 있어요."
        return {"status": u["status"], "message": msg, "login_id": u["login_id"], "role": u["role"]}

    @app.get("/api/auth/check_id")
    async def check_id(login_id: str):
        return {"available": off or not store.user(login_id=login_id.strip())}

    @app.get("/api/me")
    async def me(request: Request):
        u = current(request)
        if not u:
            raise HTTPException(401, "로그인이 필요해요.")
        return dict(public_user(u), shift=u.get("shift"), home=home_of(u))

    return current
