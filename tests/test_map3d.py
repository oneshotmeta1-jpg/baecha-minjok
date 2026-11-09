"""3D 매핑 카드 · 안전 경고 깜빡임(전달 패키지 요청서) — py tests/test_map3d.py

1) 파일 · 로드 순서: web/map3d.js · web/safety-blink.js가 사용 코드보다 먼저 · 정적 경로(/web)
2) 관제 확인(SAFETY_SEEN): 깜빡임만 멈추고 안전 중지 유지 · 다시 계산해도 유지 · 해제(SAFETY_ACK)는 따로
3) API: POST /api/safety/{id}/seen → 상태(safety_open[].seen)로 모든 화면 · 다른 접속자에게
"""
import os
import shutil
import subprocess
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


idx = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
m3 = (ROOT / "web" / "map3d.js").read_text(encoding="utf-8")
sb = (ROOT / "web" / "safety-blink.js").read_text(encoding="utf-8")
i_m3, i_sb, i_main = idx.find('<script src="/web/map3d.js">'), idx.find('<script src="/web/safety-blink.js">'), idx.find("<script>\n(async function(){")
check("map3d.js · safety-blink.js를 사용 코드보다 먼저 불러옴(/web 정적 경로)", 0 < i_m3 < i_main and 0 < i_sb < i_main)
check("map3d.js · safety-blink.js가 Map3D · SafetyBlink를 내보냄", "window.Map3D = {" in m3 and "window.SafetyBlink = { attach }" in sb)
check("깜빡임 1초 주기(초당 3회 미만) · 움직임 줄이기 설정이면 고정",
      "1s ease-in-out infinite" in m3 and "prefers-reduced-motion" in m3 and "1s ease-in-out infinite" in sb and "prefers-reduced-motion" in sb
      and "sbBand 1s" in idx and "prefers-reduced-motion:reduce){.sstrip.blink{animation:none}" in idx)
check("카드: 탐지 목록 바로 위 · 높이 168(헤더 포함 약 210px) · 켬/끔 저장 키",
      idx.find('id="m3card"') < idx.find('aria-label="탐지 목록"') and "height:168" in idx and "storageKey:'map3d.on'" in idx)
check("CAM 탭 → 구역(A·B·C·W) · 탐지 목록 행 ↔ 핀 강조 · 헤더 배지 = 관제 확인",
      "ZONE_OF={'CAM-01':'A','CAM-02':'B','CAM-03':'C','CAM-04':'W'}" in idx and "M3C.setZone(z)" in idx and "M3C.highlight(" in idx
      and "M3C.onHover=" in idx and "M3C.onAck=" in idx and "data-tid" in idx)
node = shutil.which("node")
if node:
    bad = [f for f in ("map3d.js", "safety-blink.js") if subprocess.run([node, "--check", str(ROOT / "web" / f)], capture_output=True).returncode]
    check("map3d.js · safety-blink.js 문법(node --check)", not bad, str(bad))

# 2) 런타임
from app.runtime import Runtime  # noqa: E402

r = Runtime(7)
r.advance(60)
x = next((r.force_safety(s) for s in "ABC" if any(v["state"] == "working" and v["sector"] == s for v in r.rec.vehicles.values())), None)
check("안전 경고: 처음엔 관제 확인 전(seen=false)", x and x["seen"] is False and x["status"] == "open")
v = r.rec.vehicles[x["vehicle_id"]]
e0 = v["work_elapsed"]
y = r.safety_seen(x["id"])
r.advance(1)
x1 = next(a for a in r.rec.safety if a["id"] == x["id"])
check("관제 확인 → seen · 확인 시각 · 열린 채로(안전 중지 유지 · 하차 안 됨)", y and x1["seen"] and x1["seen_at"] and x1["status"] == "open"
      and r.rec.vehicles[x["vehicle_id"]]["work_elapsed"] == e0, f"{x1['seen_at']} · 경과 {e0}")
check("SAFETY_SEEN 이벤트 기록 · 두 번 눌러도 한 번", [e["type"] for e in r.rec.events].count("SAFETY_SEEN") == 1 and r.safety_seen(x["id"]))
r.set_assumptions({"target_wait_min": 16})        # 다시 계산(행동 기록 재생)해도 유지
x2 = next(a for a in r.rec.safety if a["id"] == x["id"])
check("다시 계산해도 관제 확인 유지", x2["seen"] and x2["status"] == "open")
r.safety_resolve(x["id"], "ack")
r.advance(0.5)
check("해제(SAFETY_ACK) → 닫힘 · 하차 재개", next(a for a in r.rec.safety if a["id"] == x["id"])["status"] == "ack"
      and r.rec.vehicles[x["vehicle_id"]]["work_elapsed"] > e0)
check("닫힌 경고는 관제 확인 안 됨", r.safety_seen(x["id"]) is None)
doc = (ROOT / "docs" / "events.md").read_text(encoding="utf-8")
check("docs/events.md에 SAFETY_SEEN(관제 확인 · 중지 유지 · 팀 확인 필요 표시)", "`SAFETY_SEEN`" in doc and "팀 확인 필요" in doc.split("`SAFETY_SEEN`")[1][:400])

# 3) API
from fastapi.testclient import TestClient  # noqa: E402
from app import server  # noqa: E402

c = TestClient(server.app)
server.rt.reset()
server.rt.advance(60)
xs = next((server.rt.force_safety(s) for s in "ABC" if any(v["state"] == "working" and v["sector"] == s for v in server.rt.rec.vehicles.values())), None)
rs = c.post(f"/api/safety/{xs['id']}/seen")
st = c.get("/api/state").json()
check("POST /api/safety/{id}/seen → 상태에 seen(다른 화면 · 접속자도 같은 상태를 받음)", rs.status_code == 200
      and next(a for a in st["safety_open"] if a["id"] == xs["id"])["seen"] is True)
check("없는 경고 seen → 404", c.post("/api/safety/SA-999/seen").status_code == 404)
check("/web/map3d.js · /web/safety-blink.js 정적 파일 제공", c.get("/web/map3d.js").status_code == 200 and c.get("/web/safety-blink.js").status_code == 200)

bad = [r_ for r_ in results if not r_[1]]
print(f"\n{len(results) - len(bad)}/{len(results)} 통과")
sys.exit(1 if bad else 0)
