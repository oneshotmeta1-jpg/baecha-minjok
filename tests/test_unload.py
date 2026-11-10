"""운전자 하역 완료 확인 → 하차 예상 시간 추천 · 운전자 로그아웃 — py tests/test_unload.py

1) 런타임: 완료로 감지된 운행 → 확인 카드 · [맞아요] = 감지 시간 · [아직 안 끝났어요] → [지금 끝났어요] = 실제 시간 · 20분 넘으면 거절 · 건너뛰기
2) 통계: 원료별 중앙값 · 3건 이상 · 예측과 1분 이상 차이 → 추천 · 다시 계산해도 유지 · 시연 데이터 표시
3) API: 운전자 /api/driver/unload · 관제 /api/settings 의 unload · 운전자 화면 로그아웃 버튼
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


# 1) 런타임
r = Runtime(7)
plate = None
for _ in range(400):                      # 방금 하역이 끝난 차를 잡는다(20분 창 안)
    r.advance(0.25)
    if r.rec.done_trips and r.rec.t - (r.rec.done_trips[-1]["work_start"] + r.rec.done_trips[-1]["unload_min"]) < 15:
        plate = r.rec.done_trips[-1]["id"]
        break
d = r._last_done(plate)
u = r.unload_check_view(plate)
check("완료로 감지 → 확인 카드(감지 하차 시간 · 예측 · 늦게 알리기 가능)", u and u["detected_min"] == round(d["unload_min"], 1) and u["can_late"] and u["answer"] is None, str(u))
check("기사 앱 화면 상태에 확인 카드", r.driver_view(plate)["unload_check"]["key"] == u["key"])
rec, err = r.driver_unload(plate, "not_yet")
check("[아직 안 끝났어요] → 카드 유지(지금 끝났어요 기다림) · 통계 아님", rec["answer"] == "not_yet" and r.unload_check_view(plate)["answer"] == "not_yet"
      and r.unload_stats()["n"] == 0 and r.unload_stats()["pending"] == 1)
r.advance(3)
rec, err = r.driver_unload(plate, "done_now")
check("[지금 끝났어요] → 실제 하차 시간 = 진입부터 지금까지(감지보다 김) · 카드 닫힘", rec["answer"] == "late" and rec["actual_min"] > rec["detected_min"]
      and abs(rec["actual_min"] - (r.rec.t - d["work_start"])) < 0.11 and r.unload_check_view(plate) is None, f"감지 {rec['detected_min']} → 실제 {rec['actual_min']}")
check("두 번 확인 → 거절", r.driver_unload(plate, "ok")[1])
check("UNLOAD_CONFIRMED 이벤트 기록", sum(e["type"] == "UNLOAD_CONFIRMED" for e in r.rec.events) == 2)
r2 = Runtime(7)
r2.advance(240)
p2 = r2.rec.done_trips[0]["id"]
d2 = r2._last_done(p2)
late = r2.rec.t - (d2["work_start"] + d2["unload_min"]) > r2.UNLOAD_LATE_WIN
v2 = r2.unload_check_view(p2)
check("완료 감지 뒤 20분 넘으면 [아직 안 끝났어요] · [지금 끝났어요] 거절(실제 시간이 아님) · [맞아요]/[건너뛰기]만",
      late and not v2["can_late"] and r2.driver_unload(p2, "done_now")[1] and r2.driver_unload(p2, "not_yet")[1])
rs, _ = r2.driver_unload(p2, "skip")
check("[건너뛰기] → 카드 닫힘 · 통계에 안 들어감", rs["answer"] == "skip" and r2.unload_check_view(p2) is None and r2.unload_stats()["n"] == 0)
check("확인할 운행이 없으면 거절", r2.driver_unload("없는 차", "ok")[1])

# 1-2) 기사 앱 차량: 100%에서 멈추고 운전자 확인을 기다림
rl = Runtime(7)
rl.advance(20)


def until(cond, n=800):
    for _ in range(n):
        rl.advance(0.25)
        if cond():
            return True
    return False


P = "99가 9999"
rl.driver_trip(P, "석회석", 24)
vl = rl.rec.vehicles[P]
until(lambda: vl["state"] == "moving")
rl.driver_ack(P)
until(lambda: vl.get("await_t") is not None)
rl.advance(2)
aw = rl.driver_view(P)["vehicle"]["await"]
check("앱 차량: 하역 100%(완료 감지)여도 이탈하지 않고 운전자 확인을 기다림 · 기사 앱에 확인 카드", vl["state"] == "working" and aw and aw["detected_min"] > 0, str(aw))
check("[지금 끝났어요]는 [아직 안 끝났어요] 뒤에만", rl.driver_unload(P, "done_now")[1])
rl.driver_unload(P, "not_yet")
rl.advance(3)
check("[아직 안 끝났어요] → 계속 하역 중(베이 차지) · 카드가 [지금 끝났어요]로", vl["state"] == "working" and rl.driver_view(P)["vehicle"]["await"]["not_yet"])
rec_l, _ = rl.driver_unload(P, "done_now")
until(lambda: vl["gone"])
dl = rl._last_done(P)
check("[지금 끝났어요] → 그때 이탈 · 건별 기록의 하차 시간 = 실제(감지보다 김) · 확인 기록 late",
      rec_l["answer"] == "late" and dl["unload_ok"] == "done_now" and dl["unload_min"] > dl["detected_min"] and abs(dl["unload_min"] - rec_l["actual_min"]) <= 0.26,
      f"감지 {dl['detected_min']} → 실제 {dl['unload_min']}")
P2 = "97가 2222"
rl.driver_trip(P2, "석회석", 24)
v2 = rl.rec.vehicles[P2]
until(lambda: v2["state"] == "moving")
rl.driver_ack(P2)
until(lambda: v2.get("await_t") is not None)
rl.advance(1.5)
rl.driver_unload(P2, "ok")
until(lambda: v2["gone"])
d2_ = rl._last_done(P2)
check("[맞아요] → 이탈 · 하차 시간 = 감지 시각(확인 기다린 시간은 빼고)", d2_["unload_ok"] == "ok" and d2_["unload_min"] == d2_["detected_min"])
P3 = "96가 3333"
rl.driver_trip(P3, "분코크스", 20)
v3 = rl.rec.vehicles[P3]
until(lambda: v3["state"] == "moving")
rl.driver_ack(P3)
until(lambda: v3["gone"])
check("5분 동안 답이 없으면 알림 후 이탈(시연이 멈추지 않게) · 통계 안 넣음 · 카드 없음",
      rl._last_done(P3)["unload_ok"] == "timeout" and any(a["kind"] == "하역 완료 확인 없음" and P3 in a["text"] for a in rl.rec.alerts_all)
      and rl.unload_check_view(P3) is None and f"{P3}#1" not in rl.unload_checks)
rl.set_assumptions({"target_wait_min": 16})
check("다시 계산해도 같은 결과(확인이 이벤트로 남음)", rl._last_done(P)["unload_ok"] == "done_now" and rl._last_done(P2)["unload_ok"] == "ok")
_uc = [e["payload"].get("answer") for e in rl.rec.events if e["type"] == "UNLOAD_CONFIRMED" and e.get("source") == "app"]
check("UNLOAD_CONFIRMED(source app) 이벤트 3건(아직 안 끝났어요 · 지금 끝났어요 · 맞아요)", sorted(_uc) == ["done_now", "not_yet", "ok"], str(_uc))

# 2) 통계 · 추천
n = r2.demo_unload_checks()
st = r2.unload_stats()
x = next(c for c in st["by_cargo"] if c["n"] >= 3)
check("시연 채우기 → 끝난 운행마다 확인(시연 표시) · 건너뛴 운행은 그대로", n == len(r2.rec.done_trips) - 1 and st["demo"] == n and st["n"] == n, f"{n}건")
xs = sorted(c["actual_min"] for c in r2.unload_checks.values() if c["cargo"] == x["cargo"] and c["answer"] in ("ok", "late"))
med = xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2
check("원료별 중앙값 · 최소~최대 · 감지 평균 · 일찍 완료 판단 수", abs(x["median"] - med) < 0.06 and x["min"] == xs[0] and x["max"] == xs[-1] and x["late"] >= 0, x["text"] or "")
recos = [c for c in st["by_cargo"] if c.get("reco") is not None]
check("추천: 3건 이상 · 예측과 1분 이상 차이일 때만 · 값 = 중앙값 반올림", recos and all(c["n"] >= 3 and abs(c["median"] - c["pred"]) >= 1 and c["reco"] == round(c["median"]) for c in recos)
      and all(c.get("reco") is None for c in st["by_cargo"] if c["n"] and (c["n"] < 3 or abs(c["median"] - c["pred"]) < 1)), str([(c["cargo"], c["reco"]) for c in recos]))
k0 = len(r2.unload_checks)
r2.set_assumptions({"pred_override": {recos[0]["cargo"]: recos[0]["reco"]}})
check("[예측 n분으로] = 예측값 고정 → 순번 계산에 그 값 · 확인 기록은 다시 계산해도 유지",
      abs(r2.rec.pred_of(recos[0]["cargo"]) - recos[0]["reco"]) < 1e-9 and len(r2.unload_checks) == k0)
check("설정 화면 데이터에 unload(추천) 포함", "unload" in r2.settings_view() and r2.settings_view()["unload"]["n"] == st["n"])

# 3) API · 화면
from fastapi.testclient import TestClient  # noqa: E402
from app import auth, server  # noqa: E402

store = auth.AuthStore(":memory:")
auth.install(server.app, store)
server.rt.reset()
server.rt.advance(200)
dp = server.rt.rec.done_trips[-1]["id"]
store.create("P9", "abcd1234", "control", "관제")
store.create(dp, "abcd1234", "driver", "기사", vehicle={"plate": dp, "tonnage": "25t", "carrier": "운송사 A"})


def client(lid):
    c = TestClient(server.app)
    assert c.post("/api/auth/login", json={"login_id": lid, "password": "abcd1234"}).status_code == 200
    return c


cd, cc = client(dp), client("P9")
me = cd.get("/api/driver/me").json()
check("운전자 API: 내 마지막 운행 확인 카드", me["unload_check"] and me["unload_check"]["key"].startswith(dp + "#"))
ra = cd.post("/api/driver/unload", json={"answer": "ok"})
check("운전자 [맞아요] → 200 · 감지 시간 그대로 실제 시간", ra.status_code == 200 and ra.json()["actual_min"] == ra.json()["detected_min"])
check("다시 누르면 409", cd.post("/api/driver/unload", json={"answer": "ok"}).status_code == 409)
check("관제는 운전자 확인 API 403 · 설정에서 통계 봄", cc.post("/api/driver/unload", json={"answer": "ok"}).status_code == 403
      and cc.get("/api/settings").json()["unload"]["n"] == 1)
check("운전자는 시연 채우기 API 403(관제만)", cd.post("/api/events/driver_checks").status_code == 403 and cc.post("/api/events/driver_checks").status_code == 200)
m = (ROOT / "web" / "m" / "index.html").read_text(encoding="utf-8")
check("운전자 화면: 상단 바 로그아웃 버튼 · 확인 버튼(맞아요 · 아직 안 끝났어요 · 지금 끝났어요 · 건너뛰기)",
      'class="outb" id="out">로그아웃' in m and all('data-uc="' + k + '"' in m for k in ("ok", "not_yet", "done_now", "skip")))
idx = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
check("설정 › 하차 예상 시간 추천 · [예측 n분으로] · [목표도 n분] · 시연 메뉴 [운전자 하역 확인 채우기]",
      "function unloadReco(u)" in idx and "data-pfix" in idx and 'id="ucDemoBtn"' in idx)

import re as _re  # noqa: E402
import shutil as _sh  # noqa: E402
import subprocess as _sp  # noqa: E402
import tempfile as _tf  # noqa: E402
if _sh.which("node"):     # 화면 스크립트 문법(따옴표 하나로 화면 전체가 '불러오는 중'에서 멈춘 적이 있음)
    errs = []
    for f in ("web/m/index.html", "web/index.html", "web/cams.html"):
        for i, js in enumerate(_re.findall(r"<script>([\s\S]*?)</script>", (ROOT / f).read_text(encoding="utf-8"))):
            tmp = Path(_tf.mkdtemp()) / "s.js"
            tmp.write_text("(async()=>{\n" + js + "\n})", encoding="utf-8")
            r_ = _sp.run([_sh.which("node"), "--check", str(tmp)], capture_output=True, text=True)
            if r_.returncode:
                errs.append(f"{f}#{i}: " + r_.stderr.strip().splitlines()[-1])
    check("화면 스크립트 문법(모바일 · 관제 · CAM 새 창)", not errs, "; ".join(errs))

bad = [x for x in results if not x[1]]
print(f"\n{len(results) - len(bad)}/{len(results)} 통과")
sys.exit(1 if bad else 0)
