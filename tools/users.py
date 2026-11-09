"""계정 관리 (R11 임시 — 설정 › 가입 승인 탭(I9)이 생기기 전까지)

    py tools/users.py list                       # 계정 목록(비밀번호는 보이지 않음)
    py tools/users.py approve <아이디>            # 승인 대기 → 사용
    py tools/users.py reject <아이디>             # 반려
    py tools/users.py admin <아이디>              # 관리자로
    py tools/users.py add <역할 control|field|driver> <아이디> <이름>   # 비밀번호는 입력창에서(화면·기록에 안 남음)
    py tools/users.py delete <아이디>            # 계정 삭제(되돌릴 수 없음 · 아이디를 한 번 더 입력해 확인)
    py tools/users.py reset <아이디>             # 비밀번호 초기화: 임시 비밀번호를 이 창에만 한 번 보여 주고, 다음 로그인 때 바꾸게 함

data/auth.db(git 제외)를 바로 고친다. 서버가 켜져 있어도 된다.
"""
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.auth import ROLES, AuthStore, pw_problem  # noqa: E402


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    st = AuthStore(ROOT / "data" / "auth.db")
    if not argv or argv[0] == "list":
        rows = st.db.execute("SELECT login_id, role, name, status, is_admin, hoppers FROM users ORDER BY id").fetchall()
        for lid, role, name, status, adm, hop in rows:
            print(f"{lid:<14} {ROLES[role]:<7} {name:<8} {status:<8} {'관리자' if adm else ''} {hop or ''}")
        print(f"{len(rows)}명")
        return
    cmd, lid = argv[0], argv[1] if len(argv) > 1 else None
    if cmd in ("approve", "reject", "admin"):
        u = st.user(login_id=lid)
        if not u:
            sys.exit(f"없는 아이디: {lid}")
        st.set_status(lid, "rejected" if cmd == "reject" else "active", is_admin=True if cmd == "admin" else None)
        print(f"{lid}: {st.user(login_id=lid)['status']}" + (" · 관리자" if cmd == "admin" else ""))
    elif cmd == "delete":
        u = st.user(login_id=lid)
        if not u:
            sys.exit(f"없는 아이디: {lid}")
        print(f"{u['name']} · {lid} · {ROLES[u['role']]} 계정을 지워요. 되돌릴 수 없어요.")
        if input("확인을 위해 아이디를 한 번 더 입력하세요: ").strip() != lid:
            sys.exit("아이디가 달라서 지우지 않았어요.")
        admin = st.db.execute("SELECT id FROM users WHERE is_admin=1 AND status='active' AND id<>? LIMIT 1", (u["id"],)).fetchone()
        actor = st.user(uid=admin[0]) if admin else {"id": -1}     # 처리한 사람: 명령어(관리자 대신)
        try:
            st.admin_do(actor, u["id"], "delete", confirm=lid)
        except Exception as e:
            sys.exit(str(e))
        print(f"{lid}: 삭제했어요")
    elif cmd == "reset":
        u = st.user(login_id=lid)
        if not u:
            sys.exit(f"없는 아이디: {lid}")
        _, extra = st.admin_do(u, u["id"], "reset_password")      # 관리자 화면과 같은 처리(세션 끊기 · 기록), 처리한 사람 = 본인(명령어)
        print(f"{lid}: 임시 비밀번호 {extra['temp_password']}  (이 창에만 보여요 · 다음 로그인 때 새 비밀번호로 바꾸게 돼요)")
    elif cmd == "add":
        role, name = argv[1], argv[3]
        lid = argv[2]
        pw = getpass.getpass("비밀번호: ")
        if pw_problem(pw):
            sys.exit(pw_problem(pw))
        if pw != getpass.getpass("비밀번호 확인: "):
            sys.exit("비밀번호가 서로 달라요.")
        vehicle = {"plate": lid, "tonnage": "25t", "carrier": "운송사 A"} if role == "driver" else None
        st.create(lid, pw, role, name, vehicle=vehicle, hoppers=["A", "B", "C"] if role == "field" else [])
        print(f"{lid}: 만들었어요 ({ROLES[role]})")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
