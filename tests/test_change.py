"""현장 앱 하역장 변경 사유 → 운영 분석 '담당자가 바꾼 이유' · 알림 이력 라벨 — py tests/test_change.py"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
os.environ["DOCK_MEMORY"] = "1"

results = []


def check(name, ok, note=""):
    results.append((name, bool(ok)))
    print(("통과 " if ok else "실패 ") + name + (f" — {note}" if note else ""))


from fastapi.testclient import TestClient  # noqa: E402
from app import auth, server  # noqa: E402
from app.runtime import REASONS  # noqa: E402

store = auth.AuthStore(":memory:")
auth.install(server.app, store)
store.create("FX", "abcd1234", "field", "현장엑스", hoppers=["A", "B", "C"])
store.create("PX", "abcd1234", "control", "관제엑스")


def client(lid):
    c = TestClient(server.app)
    assert c.post("/api/auth/login", json={"login_id": lid, "password": "abcd1234"}).status_code == 200
    return c


cf, cc = client("FX"), client("PX")
server.rt.reset()
server.rt.set_auto_approve(False)
fv = None
for _ in range(120):
    server.rt.advance(0.5)
    fv = cf.get("/api/field/home").json()
    if fv["approvals"]:
        break
check("현장 화면 데이터에 변경 사유 목록(관제와 같은 6가지)", fv["reasons"] == REASONS)
a = fv["approvals"][0]
vid, cur = a["vehicle_id"], a["sector_id"]
acc = {b["sector_id"] for b in server.rt.accept_bays(server.rt.rec.vehicles[vid]["cargo"])}
to = next((h for h in "ABC" if h != cur and h in acc), None)
r0 = cf.post("/api/field/change", json={"vehicle_id": vid, "sector_id": to or cur, "reason": "현장 판단"})
check("목록에 없는 사유는 거절(예전 앱이 보내던 '현장 판단')", r0.status_code == 409 and "사유" in r0.json()["detail"])
r1 = cf.post("/api/field/change", json={"vehicle_id": vid, "sector_id": to or cur, "reason": "기타", "memo": "  "})
check("'기타'는 메모가 있어야 함", r1.status_code == 409 and "메모" in r1.json()["detail"])
if to:
    r2 = cf.post("/api/field/change", json={"vehicle_id": vid, "sector_id": to, "reason": "호퍼·설비 사정", "memo": "B 호퍼 진동 점검"})
    check("현장 앱 변경 → 사유 · 메모 · 누가(현장 · 이름) 기록", r2.status_code == 200 and r2.json()["reason"] == "호퍼·설비 사정"
          and r2.json()["by"] == "현장" and r2.json()["by_name"] == "현장엑스", str(r2.status_code))
    fb = cc.get("/api/analysis2").json()["feedback"]
    check("운영 분석 '담당자가 바꾼 이유'에 반영(사유 건수 · 현장 앱 건수 · 메모)", fb["counts"]["호퍼·설비 사정"] == 1 and fb["by"].get("현장") == 1
          and fb["total"] == 1 and fb["memos"][0] == "B 호퍼 진동 점검", str(fb))
    ev = [e for e in server.rt.rec.events if e["type"] == "ORDER_CHANGED"]
    check("ORDER_CHANGED 이벤트에 by=현장", ev and ev[-1]["payload"]["by"] == "현장")
else:
    check("다른 호퍼로 바꿀 수 있는 승인 대기 차량이 있어야 함", False, vid)

m = (ROOT / "web" / "m" / "index.html").read_text(encoding="utf-8")
check("현장 앱 변경 시트: 하역장 → 사유 칩 → 메모 → [바꾸기] · '현장 판단' 고정값 없음",
      "data-why=" in m and 'id="chgMemo"' in m and "reason:why,memo" in m and "reason:'현장 판단'" not in m)
idx = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
check("알림 이력 종류 라벨 한 줄(.lvl nowrap)", ".lvl{font-size:11.5px;font-weight:700;padding:2px 8px;border-radius:999px;white-space:nowrap" in idx)
check("운영 분석: 현장 앱 · 관제 화면 건수 표시", "현장 앱 '+((fb.by||{})['현장']||0)" in idx)

bad = [q for q in results if not q[1]]
print(f"\n{len(results) - len(bad)}/{len(results)} 통과")
sys.exit(1 if bad else 0)
