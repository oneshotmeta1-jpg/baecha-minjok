"""R17 CAM 4채널 새 창(/cams) — py tests/test_r17.py

1) 공통 그리기 파일(web/cam_view.js): 대시보드와 새 창이 같은 함수를 씀 · 대시보드에 옛 정의가 남지 않음
2) 권한: /cams 페이지는 관제 · 현장만(로그인 안 하면 /login, 운전자는 자기 화면) · /api/cams · /api/cams/live · /media 도 관제 · 현장
3) 현장 담당자는 담당 하역장 채널만(대기장 담당이면 CAM-04) · 관제는 4채널
4) /api/cams/live: 채널별 혼잡 · 시뮬레이션 그림용 하역장 · 위험(안전 경고 · 최근 아차사고 3분)
"""
import os
import re
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


# 1) 공통 그리기 파일
js = (ROOT / "web" / "cam_view.js").read_text(encoding="utf-8")
idx = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
cams = (ROOT / "web" / "cams.html").read_text(encoding="utf-8")
names = "detAt ZCOL standAt drawOv approachPairs medRatio ptIn truckGeom drawTruck scene renderHopperCam renderYardCam".split()
check("cam_view.js가 그리기 함수를 모두 내보냄(window.CamView)", all(re.search(r"function " + n + r"\(", js) or n == "ZCOL" for n in names)
      and "window.CamView={" + ",".join(names) + "}" in js)
check("대시보드 · 새 창 둘 다 cam_view.js를 불러 씀", '<script src="/web/cam_view.js"></script>' in idx and '<script src="/web/cam_view.js"></script>' in cams
      and "CamView.drawOv(" in idx and "CamView.drawOv(" in cams)
check("대시보드에 옛 그리기 정의가 남지 않음(코드 한 곳)", not any(re.search(r"function " + n + r"\(", idx) for n in names if n != "ZCOL"))
check("cam_view.js는 대시보드 전역(VC · DET)을 쓰지 않음(인자로 받음)", "VC[" not in js and "DET[" not in js)
node = shutil.which("node")
if node:
    r = subprocess.run([node, "--check", str(ROOT / "web" / "cam_view.js")], capture_output=True, text=True)
    check("cam_view.js 문법 확인(node --check)", r.returncode == 0, r.stderr[:200])
check("대시보드: [새 창으로 크게] 버튼 두 곳(CAM 4채널 카드 · 상단) · 창 이름 하나(dock_cams)로 열기",
      'id="camsWinBtn"' in idx and 'id="camsWinBtn2"' in idx and "window.open('','dock_cams'" in idx and "localStorage.getItem('camsWin')" in idx)
check("새 창: 1~4 · Esc · F 키 · 4분할/1채널 · [상세] → 대시보드 #cam/CAM-0n · 창 크기·위치 기록",
      all(k in cams for k in ("/^[1-4]$/", "'Escape'", "requestFullscreen", "classList.toggle('one'", "'#cam/'+id", "localStorage.setItem('camsWin'")))

# 2)·3) 서버 권한 · 채널 거르기
from fastapi.testclient import TestClient  # noqa: E402
from app import auth, server  # noqa: E402

store = auth.AuthStore(":memory:")
auth.install(server.app, store)
store.create("P1", "abcd1234", "control", "관제")
store.create("FA", "abcd1234", "field", "현장A", hoppers=["A"])
store.create("FY", "abcd1234", "field", "현장대기장", hoppers=["YARD"])
store.create("85자 2064", "abcd1234", "driver", "기사", vehicle={"plate": "85자 2064", "tonnage": "25t", "carrier": "운송사 A"})


def client(lid=None):
    c = TestClient(server.app, follow_redirects=False)
    if lid:
        assert c.post("/api/auth/login", json={"login_id": lid, "password": "abcd1234"}).status_code == 200
    return c


anon, cc, ca, cy, cd = client(), client("P1"), client("FA"), client("FY"), client("85자 2064")
r0 = anon.get("/cams")
check("/cams: 로그인 안 하면 로그인 화면으로", r0.status_code == 303 and r0.headers["location"] == "/login")
rd = cd.get("/cams")
check("/cams: 운전자는 자기 화면(/m/)으로 돌려보냄", rd.status_code == 303 and rd.headers["location"] == "/m/")
check("/cams: 관제 · 현장은 열림", cc.get("/cams").status_code == 200 and ca.get("/cams").status_code == 200 and "CAM 4채널" in cc.get("/cams").text)
check("운전자: /api/cams · /api/cams/live · /media 403", cd.get("/api/cams").status_code == 403 and cd.get("/api/cams/live").status_code == 403
      and cd.get("/media/x.mp4").status_code == 403)
check("로그인 안 하면 /api/cams/live 401", anon.get("/api/cams/live").status_code == 401)
ids = lambda c, path: [x.get("camera") or x.get("id") for x in c.get(path).json()["cams"]]
check("관제: 4채널 모두", ids(cc, "/api/cams") == ["CAM-01", "CAM-02", "CAM-03", "CAM-04"] and ids(cc, "/api/cams/live") == ["CAM-01", "CAM-02", "CAM-03", "CAM-04"],
      str(ids(cc, "/api/cams")))
check("현장(호퍼 A 담당): CAM-01만 · 대기장 차량 목록 없음", ids(ca, "/api/cams") == ["CAM-01"] and ids(ca, "/api/cams/live") == ["CAM-01"]
      and ca.get("/api/cams/live").json()["yard_vehicles"] == [])
check("현장(대기장 담당): CAM-04만", ids(cy, "/api/cams") == ["CAM-04"] and ids(cy, "/api/cams/live") == ["CAM-04"])
check("현장: 관제 전체 상태(/api/state)는 여전히 403", ca.get("/api/state").status_code == 403)
vid = next((c for c in server.cam_list(server.CAMS, server.MEDIA) if c["video_url"]), None)
if vid:
    check("현장: 영상 파일(/media) 읽기 가능", ca.get(vid["video_url"], headers={"Range": "bytes=0-99"}).status_code in (200, 206))

# 4) 채널별 상태 · 위험
server.rt.reset()
server.rt.advance(90)
L = cc.get("/api/cams/live").json()
a = next(x for x in L["cams"] if x["id"] == "CAM-01")
check("live: 시각 · 호퍼 혼잡(단계 · %) · 시뮬레이션 그림용 하역장(working · queue_info)",
      L["clock"] and a["level"] in ("ok", "warn", "crit") and isinstance(a["congestion_pct"], int) and "working" in a["sector"] and "queue_info" in a["sector"], a["summary"])
x = next((server.rt.force_safety(s) for s in "ABC" if any(v["state"] == "working" and v["sector"] == s for v in server.rt.rec.vehicles.values())), None)
L = cc.get("/api/cams/live").json()
risky = {c["id"]: c["risk"] for c in L["cams"] if c["risk"]}
cam_of = {"A": "CAM-01", "B": "CAM-02", "C": "CAM-03"}
check("안전 경고가 열리면 그 호퍼 채널만 위험(빨간 테두리)", x and list(risky) == [cam_of[x["sector_id"]]] and risky[cam_of[x["sector_id"]]]["kind"] == "safety", str(risky))
server.rt.safety_resolve(x["id"], "ack")
from app import sim as SM  # noqa: E402
t = server.rt.rec.t
server.rt.rec.near_miss.append({"id": "NM-T", "t": t, "ts": "09:30", "cam_id": "CAM-04", "sector_id": None, "level": "L3", "rule": "사람·트럭 접근", "min_dist_m": 0.3})
L = cc.get("/api/cams/live").json()
r4 = next(c for c in L["cams"] if c["id"] == "CAM-04")["risk"]
check("최근 아차사고(3분 안) → 그 채널 위험 · 문구에 단계 · 규칙 · 거리", r4 and r4["kind"] == "near_miss" and "L3" in r4["text"] and "0.3m" in r4["text"], str(r4))
server.rt.rec.near_miss[-1]["t"] = t - (server.NM_RECENT_MIN + 1)
L = cc.get("/api/cams/live").json()
check("3분 지나면 위험 표시 해제", next(c for c in L["cams"] if c["id"] == "CAM-04")["risk"] is None)

bad = [r for r in results if not r[1]]
print(f"\n{len(results) - len(bad)}/{len(results)} 통과")
sys.exit(1 if bad else 0)
