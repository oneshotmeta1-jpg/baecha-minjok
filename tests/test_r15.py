"""R15 구역(ROI) 편집 · 아차사고 — py tests/test_r15.py

1. 다각형 판정 · 아차사고 규칙 ①② 단계(L1→L2→L3) · 끊김 · 30초 창 · 꺼진 규칙
2. 합성 사람(stand_in) 보간 · 기록에 합성 표시
3. NEAR_MISS 이벤트 → 서버 기록(같은 id 한 번) · docs/events.md 예시가 그대로 접수됨
4. 구역 저장 API: 로그인 없으면 401 · 관리자 아니면 403 · 관리자 저장 + 처리 기록 · 잘못된 구역 422 · 파일 모양 유지
5. 실제 영상(있으면): 시연 순서 3번 반복해도 같은 건수 · 합성 사람 꺼짐(R16) → 호퍼 아차사고 없음 · CAM-04 실제 사람 접근 L3
6. R16 접근 규칙 ③: 움직이는 트럭 ↔ 사람 거리 단계 · 서 있는 트럭 · 운전자 · 구역 밖 · 가장 가까운 트럭 하나
판정 규칙 테스트는 tests/zones_fixture.json의 고정 구역을 쓴다(현장에서 다시 그린 data/cams.json 구역과 무관).
"""
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vision"))
sys.path.insert(0, str(ROOT / "tools"))
sys.stdout.reconfigure(encoding="utf-8")

import near_miss  # noqa: E402
from to_events import convert, video_rois, inside  # noqa: E402

results = []


def check(name, ok, note=""):
    results.append((name, bool(ok)))
    print(("통과 " if ok else "실패 ") + name + (f" — {note}" if note else ""))


CFG = json.loads((ROOT / "data" / "cams.json").read_text(encoding="utf-8"))
_FIX = json.loads((ROOT / "tests" / "zones_fixture.json").read_text(encoding="utf-8"))
for _c in CFG["cams"]:
    _c["zones"] = _FIX[_c["camera"]]
NM = near_miss.config(CFG)

# 1) 다각형 · 규칙
sq = [[0, 0], [10, 0], [10, 10], [0, 10]]
check("다각형 안/밖 (경계 바깥 · 오목 다각형)", inside((5, 5), sq) and not inside((11, 5), sq)
      and not inside((5, 5), [[0, 0], [10, 0], [10, 10], [6, 10], [6, 4], [4, 4], [4, 10], [0, 10]]))
check("단계 기준: 0초 L1 · 1초 L2 · 2초 L3 (가정값)", [near_miss.level_of(d, NM) for d in (0, 0.99, 1.0, 1.99, 2.0, 5)] == ["L1", "L1", "L2", "L2", "L3", "L3"])

TR = lambda b, tid=1: {"track_id": tid, "cls": "truck", "bbox": b}
PS = lambda b, tid=7: {"track_id": tid, "cls": "person", "bbox": b}
LOW, HIGH = [240, 300, 890, 572], [240, 100, 890, 572]          # 적재함 내림 / 올림 (세로/가로 0.42 / 0.73)
IN_REAR, OUT = [950, 480, 990, 640], [1150, 480, 1190, 640]      # A-REAR-1 안 / 밖


def run(person_secs, raised=True, cam="CAM-01", person_box=IN_REAR, nm=None, step=0.25, until=12.0):
    """0~until초: 트럭이 A-BAY-1에 있음(2초부터 적재함 올림), person_secs 구간에 사람이 person_box에."""
    fr, t = [], 0.0
    while t <= until:
        objs = [TR(HIGH if raised and t >= 2 else LOW)]
        if any(a <= t <= b for a, b in person_secs):
            objs.append(PS(person_box))
        fr.append({"t": round(t, 2), "cam_id": cam, "objects": objs})
        t += step
    return convert({"size": [1280, 720], "frames": fr}, "2026-11-09T08:00:00", video_rois(CFG, cam), scale=1, flush=True, nm_cfg=nm or NM)


nms = lambda evs: [e for e in evs if e["type"] == "NEAR_MISS"]
e1 = run([(5, 5.5)])
e2 = run([(5, 6.25)])
e3 = run([(5, 8)])
check("규칙 ①: 0.5초 L1 · 1.25초 L2 · 3초 L3", [x["payload"]["level"] for x in nms(e1) + nms(e2) + nms(e3)] == ["L1", "L2", "L3"],
      " / ".join(f'{x["payload"]["duration_s"]}초 {x["payload"]["level"]}' for x in nms(e1) + nms(e2) + nms(e3)))
check("L3에 오를 때만 SAFETY_ALERT(하차 중지 요청) · NEAR_MISS와 같은 id로 이어짐",
      not any(e["type"] == "SAFETY_ALERT" for e in e1 + e2) and [e["payload"]["near_miss_id"] for e in e3 if e["type"] == "SAFETY_ALERT"] == [nms(e3)[0]["payload"]["id"]])
check("적재함이 내려가 있으면 덤프 반경에 사람이 있어도 아차사고 아님", not nms(run([(5, 8)], raised=False)))
check("구역 밖 사람은 아차사고 아님", not nms(run([(5, 8)], person_box=OUT)))
g = nms(run([(5, 6), (6.5, 8)]))
check("0.5초 이하 끊김은 한 건으로 묶고, 더 길게 끊기면 두 건", len(g) == 1 and len(nms(run([(5, 6), (7.25, 8)]))) == 2, f"{len(g)}건")
p = nms(e3)[0]["payload"]
check("기록 필드: 규칙 번호 · 지속 · 최소 거리(m) · 스냅샷 시각 · 영상 구간 · 출처",
      p["rule_no"] == 1 and p["rule"] == "덤프 반경" and p["min_dist_m"] >= 0 and p["clip"][0] <= p["snapshot_t"] <= p["clip"][1] and p["source"] == "video", json.dumps(p, ensure_ascii=False)[:160])
off = json.loads(json.dumps(NM)); off["rules"][0]["enabled"] = False
check("규칙을 끄면(설정 enabled=false) 기록하지 않음", not nms(run([(5, 8)], nm=off)))
check("규칙 ①② 켜짐 · ③ 접근은 대기장(CAM-04)만 켜짐(R16) · ④⑤ 꺼짐", [r["enabled"] for r in NM["rules"]] == [True, True, True, False, False]
      and near_miss.rule(NM, "approach")["cams"] == ["CAM-04"])
# 규칙 ② 후진 경로: CAM-03 C-BACK-1, 베이 진입(3초 유지) 뒤 30초 안
BACK = [250, 420, 290, 560]
def run3(person_at, until=45.0):
    fr = [{"t": t / 2, "cam_id": "CAM-03", "objects": [TR([420, 320, 860, 520])] + ([PS(BACK)] if person_at[0] <= t / 2 <= person_at[1] else [])} for t in range(int(until * 2) + 1)]
    return nms(convert({"size": [1280, 720], "frames": fr}, "2026-11-09T08:00:00", video_rois(CFG, "CAM-03"), scale=1, flush=True, nm_cfg=NM))
r2a, r2b = run3((5, 7.5)), run3((36, 38.5))
check("규칙 ②: 베이 진입 30초 안 후진 경로 사람 → 기록, 30초 지나면 아님",
      [x["payload"]["rule"] for x in r2a] == ["후진 경로"] and r2a[0]["payload"]["rule_no"] == 2 and not r2b)

# 2) 합성 사람
SI = json.loads((ROOT / "data" / "stand_in.json").read_text(encoding="utf-8"))
tr = SI["cams"]["CAM-01"]
mid = near_miss.stand_in_at(tr, 8.3)[0]
k0, k1 = tr[0]["keys"][0], tr[0]["keys"][1]
x = k0[1] + (k1[1] - k0[1]) * (8.3 - k0[0]) / (k1[0] - k0[0])
check("합성 사람: 키프레임 사이 직선 보간 · 범위 밖은 없음 · stand_in 표시",
      abs((mid["bbox"][0] + mid["bbox"][2]) / 2 - x) < 0.6 and not near_miss.stand_in_at(tr, 1.0) and mid["stand_in"])
fake = {"size": [1280, 720], "frames": [{"t": t / 4, "cam_id": "CAM-01", "objects": [TR(HIGH)]} for t in range(0, 60)]}
merged = near_miss.merge_stand_in(fake, [{"track_id": 99, "keys": [[5, 980, 640, 50, 160], [8, 985, 640, 50, 160]]}])
sm = nms(convert(merged, "2026-11-09T08:00:00", video_rois(CFG, "CAM-01"), scale=1, flush=True, nm_cfg=NM))
check("합성 사람으로 생긴 기록은 source=stand_in", len(sm) == 1 and sm[0]["payload"]["source"] == "stand_in", sm and sm[0]["payload"]["level"])

check("합성 사람 스위치: 기본 꺼짐(cams.json near_miss.stand_in=false)", near_miss.stand_in_on(NM) is False)
from app.cams import cam_list  # noqa: E402
with tempfile.TemporaryDirectory() as _td:
    _on = json.loads(json.dumps(CFG)); _on["near_miss"]["stand_in"] = True
    _cl0, _cl1 = cam_list(CFG, Path(_td), stand_in=SI), cam_list(_on, Path(_td), stand_in=SI)
check("화면(/api/cams): 꺼져 있으면 합성 사람 궤적을 보내지 않음 · 켜면 보냄",
      all(not c["stand_in"] for c in _cl0) and any(c["stand_in"] for c in _cl1))

# 3) 서버 기록 · 문서 예시
from app.runtime import Runtime  # noqa: E402
doc = (ROOT / "docs" / "events.md").read_text(encoding="utf-8")
table = doc.split("## 종류")[1].split("##")[0]
check("docs/events.md 종류 표에 NEAR_MISS", "`NEAR_MISS`" in table)
ex = re.search(r"```json\n(\{\"ts\"[^`]*?\"type\": \"NEAR_MISS\"[^`]*?)\n```", doc)
rt = Runtime(7, video=True)
gate = {"ts": "2026-11-09T08:05:00", "type": "GATE_IN", "source": "iot", "vehicle_id": "CAM-01-T1", "payload": {"cargo": "석회석", "load_t": 22}}
ex_ev = json.loads(ex.group(1).replace("\n", " ")) if ex else {}
res = rt.ingest([gate, ex_ev, dict(ex_ev)])
check("문서 예시 NEAR_MISS가 서버에 접수 · 같은 id 두 번째는 무시", res[1:] == ["ok", "ignored"] and len(rt.rec.near_miss) == 1, str(res))
st = rt.state()
check("화면 상태에 아차사고 기록(near_miss) 포함", st.get("near_miss") and st["near_miss"][0]["rule"] == "덤프 반경", str(st.get("near_miss"))[:120])

# 4) 구역 저장 API
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app import auth, rois as roi_edit  # noqa: E402

td = Path(tempfile.mkdtemp())
shutil.copy(ROOT / "data" / "cams.json", td / "cams.json")
holder = {"cfg": json.loads((td / "cams.json").read_text(encoding="utf-8"))}
app = FastAPI()
st_ = auth.AuthStore(":memory:")
auth.install(app, st_)
roi_edit.install(app, st_, td / "cams.json", ROOT / "data" / "rois.json", lambda: holder["cfg"], lambda c: holder.update(cfg=c))
c0 = TestClient(app)
base = {"name": "관리자", "phone": "010-1234-5678", "password": "abcd1234", "password2": "abcd1234", "consent": {"terms": True, "safety": True}}
c0.post("/api/auth/signup", json={**base, "role": "control", "emp_no": "ADM1", "dept": "관제실"})
c0.post("/api/auth/signup", json={**base, "role": "control", "emp_no": "CTL2", "dept": "관제실", "name": "관제둘"})
st_.admin_do(st_.user(login_id="ADM1"), st_.user(login_id="CTL2")["id"], "approve")


def login(lid):
    c = TestClient(app)
    c.post("/api/auth/login", json={"login_id": lid, "password": "abcd1234"})
    return c


adm, ctl = login("ADM1"), login("CTL2")
zones = holder["cfg"]["cams"][0]["zones"]
moved = json.loads(json.dumps(zones)); moved[0]["points"][0] = [160, 470]
check("구역 저장: 로그인 없으면 401 · 관리자 아닌 관제 담당자 403",
      TestClient(app).post("/api/admin/rois/CAM-01", json={"zones": moved}).status_code == 401
      and ctl.post("/api/admin/rois/CAM-01", json={"zones": moved}).status_code == 403)
ed = ctl.get("/api/rois/edit").json()
check("구역 보기: 영상 구역 + 시뮬레이션 구역 함께(읽기 통합) · 관리자 아니면 can_edit=false",
      len(ed["cams"]) == 4 and len(ed["sim"]) == 4 and ed["can_edit"] is False and adm.get("/api/rois/edit").json()["can_edit"] is True)
r = adm.post("/api/admin/rois/CAM-01", json={"zones": moved})
saved = json.loads((td / "cams.json").read_text(encoding="utf-8"))
log = st_.recent_log(5)
check("관리자 저장: 파일 · 서버 설정 바뀜 · 처리 기록(누가 · 무엇)", r.status_code == 200 and saved["cams"][0]["zones"][0]["points"][0] == [160, 470]
      and holder["cfg"]["cams"][0]["zones"][0]["points"][0] == [160, 470] and log[0]["action"] == "roi" and log[0]["by"] == "관리자" and "수정 A-BAY-1" in log[0]["detail"],
      log[0]["detail"] if log else "")
check("저장해도 다른 카메라 · 아차사고 설정은 그대로", saved["cams"][1:] == holder["cfg"]["cams"][1:] and saved["near_miss"] == CFG["near_miss"] and saved["time_scale"] == CFG["time_scale"])
bad = [
    ("점 2개", [{"id": "A-BAY-1", "type": "하차", "name": "x", "points": [[1, 1], [500, 500]]}]),
    ("화면 밖 점", [{"id": "A-BAY-1", "type": "하차", "name": "x", "points": [[1, 1], [1500, 1], [1, 500]]}]),
    ("ID 모양", [{"id": "BAY1", "type": "하차", "name": "x", "points": [[1, 1], [500, 1], [1, 500]]}]),
    ("하차 구역 없음", [{"id": "A-REAR-1", "type": "위험", "rule": "덤프 반경 5m", "name": "x", "points": [[1, 1], [500, 1], [1, 500]]}]),
    ("규칙과 ID 불일치", zones[:1] + [{"id": "A-REAR-1", "type": "위험", "rule": "후진 경로", "name": "x", "points": [[1, 1], [500, 1], [1, 500]]}]),
    ("ID 중복", zones[:1] + zones[:1]),
]
codes = {n: adm.post("/api/admin/rois/CAM-01", json={"zones": z}).status_code for n, z in bad}
check("잘못된 구역은 422로 거절(이유 문장)", set(codes.values()) == {422}, str(codes))
cfg0 = json.loads((ROOT / "data" / "cams.json").read_text(encoding="utf-8"))
check("cams.json 쓰기 모양: 다시 읽으면 같은 내용", json.loads(roi_edit.dumps(cfg0)) == cfg0)
shutil.rmtree(td, ignore_errors=True)

# 5) 실제 영상 시연 (영상 탐지 결과가 있을 때만)
if all((ROOT / CFG["dir"] / (Path(c["video"]).stem + ".det.json")).exists() for c in CFG["cams"]):
    import ingest_videos  # noqa: E402
    runs = []
    for _ in range(3):
        evs, _rep = ingest_videos.build("2026-11-09T08:00:00")
        rv = Runtime(7, video=True)
        rv.ingest(evs)
        runs.append((tuple((x["cam_id"], x["rule"], x["level"], x["source"]) for x in rv.rec.near_miss), len(rv.rec.safety)))
    check("시연 3번 반복: 같은 아차사고 건수 · 같은 안전 경고 수", len(set(runs)) == 1, str(runs[0]))
    nm0 = dict(((c, r), (lv, s)) for c, r, lv, s in runs[0][0])
    check("합성 사람 꺼짐: 호퍼(CAM-01~03) 아차사고·안전 경고 없음 · 기록은 모두 실제 영상(source=video)",
          not any(c in ("CAM-01", "CAM-02", "CAM-03") for c, _r in nm0) and runs[0][1] == 0 and all(s == "video" for _lv, s in nm0.values()), str(nm0))
    apr = [x for x in runs[0][0] if x[0] == "CAM-04"]
    check("대기장 CAM-04 실제 영상: 움직이는 트럭과 사람 접근 2건 모두 L3", [x[1:] for x in apr] == [("사람·트럭 접근", "L3", "video")] * 2, str(apr))
    _on = json.loads(json.dumps(json.loads((ROOT / "data" / "cams.json").read_text(encoding="utf-8")))); _on["near_miss"]["stand_in"] = True
    for _c in _on["cams"]:
        _c["zones"] = _FIX[_c["camera"]] if _c["camera"] != "CAM-04" else _c["zones"]
    evs_on, _ = ingest_videos.build("2026-11-09T08:00:00", cfg=_on)
    check("스위치를 켜면 R15처럼 합성 사람 기록(source=stand_in, '합성')이 다시 생김",
          any(e["type"] == "NEAR_MISS" and e["payload"]["source"] == "stand_in" for e in evs_on))
else:
    print("건너뜀 실제 영상 시연 — 영상/*.det.json 없음 (py vision/detect_video.py)")

# 6) R16 접근 규칙 ③ (대기장 CAM-04, 1920×1080)
PH = lambda x, y, tid=7, h=60: {"track_id": tid, "cls": "person", "bbox": [x - 12, y - h, x + 12, y]}


def run4(truck_box_at, people_at, until=12.0, step=0.25, nm=None):
    """truck_box_at(t) → 트럭 박스 목록[(tid, bbox)], people_at(t) → 사람 목록"""
    fr, t = [], 0.0
    while t <= until:
        objs = [{"track_id": tid, "cls": "truck", "bbox": b} for tid, b in truck_box_at(t)] + people_at(t)
        fr.append({"t": round(t, 2), "cam_id": "CAM-04", "objects": objs})
        t += step
    return nms(convert({"size": [1920, 1080], "frames": fr}, "2026-11-09T08:00:00", video_rois(CFG, "CAM-04"), scale=1, flush=True, nm_cfg=nm or NM))


# 트럭이 오른쪽에서 왼쪽으로 초당 60px(박스 너비 300) — 사람(키 60px → 1px = 1.7/60 m)은 x=1000에 서 있음
mv_truck = lambda t: [(55, [1600 - 60 * t, 300, 1900 - 60 * t, 450])]
stand = lambda t: [PH(1000, 400)]
a1 = run4(mv_truck, stand)
check("접근 ③: 움직이는 트럭이 사람에게 다가오면 한 건 · L3 · 최소 거리 0m · 출처 실제 영상 · SAFETY_ALERT 없음",
      len(a1) == 1 and a1[0]["payload"]["level"] == "L3" and a1[0]["payload"]["min_dist_m"] == 0 and a1[0]["payload"]["source"] == "video"
      and a1[0]["payload"]["rule_no"] == 3 and a1[0]["zone"] == "yard" and a1[0]["payload"]["bay"] is None,
      json.dumps(a1[0]["payload"], ensure_ascii=False)[:200] if a1 else "없음")
# 거리 단계만 보기: 지속 시간 단계가 오르지 않게 level_sec를 크게
long_ = json.loads(json.dumps(NM)); long_["level_sec"] = {"L2": 99, "L3": 99}
def stop_at(gap_px):   # 트럭이 사람 오른쪽 gap_px 앞에서 멈추지 않고 그 거리로 지나감(위아래로 비켜 지나감)
    return lambda t: [(55, [1600 - 60 * t, 400 + gap_px, 1900 - 60 * t, 530 + gap_px])]   # 박스 위쪽이 사람 발끝(y=400)보다 gap_px 아래
lv = [(g, [x["payload"]["level"] for x in run4(stop_at(g), stand, nm=long_)]) for g in (90, 50, 20)]   # 2.6m · 1.4m · 0.6m 아래로 비켜 감
check("접근 ③ 거리 단계: 3m 이하 L1 · 2m 이하 L2 · 1m 이하 L3 (가정값)", [x[1] for x in lv] == [["L1"], ["L2"], ["L3"]], str(lv))
park = lambda t: [(2, [1020, 300, 1300, 450])]
check("서 있는 트럭 옆 사람(바로 옆이어도)은 아차사고 아님", not run4(park, stand))
drv = lambda t: [PH(1010, 400, tid=8)] if t >= 1 else []
mv_slow_start = lambda t: [(55, [1020 - (60 * (t - 4) if t > 4 else 0), 300, 1300 - (60 * (t - 4) if t > 4 else 0), 450])]
check("운전자 제외: 트럭 바로 옆에서 처음 나타난 사람은 그 트럭이 움직여도 아차사고 아님", not run4(mv_slow_start, drv))
outside = lambda t: [PH(1000, 260)]           # 대기장 구역(y ≥ 285) 밖
check("대기장 구역 밖 사람은 판정하지 않음", not run4(lambda t: [(55, [1600 - 60 * t, 150, 1900 - 60 * t, 300])], outside))
two = lambda t: [(55, [1600 - 60 * t, 300, 1900 - 60 * t, 450]), (56, [1700 - 60 * t, 300, 2000 - 60 * t, 450])]
check("한 사람은 가장 가까운 움직이는 트럭 하나로 한 건", len(run4(two, stand)) == 1)
off3 = json.loads(json.dumps(NM)); near_miss.rule(off3, "approach")["enabled"] = False
check("규칙 ③을 끄면 기록하지 않음", not run4(mv_truck, stand, nm=off3))
oth = json.loads(json.dumps(NM)); near_miss.rule(oth, "approach")["cams"] = ["CAM-09"]
check("규칙 ③은 지정한 카메라(cams)에서만", not run4(mv_truck, stand, nm=oth))

# 7) 시뮬레이션 모드: 영상 아차사고 재생 · 합성 안전 경고 꺼짐
from app import sim as SM  # noqa: E402
sav = list(SM.VIDEO_NM)
SM.VIDEO_NM[:] = [{"off": 3.0, "cycle": 11.0, "cam_id": "CAM-04", "vehicle_id": "CAM-04-T55", "zone": "yard",
                   "payload": {"id": "NM-X", "rule": "사람·트럭 접근", "rule_no": 3, "rule_key": "approach", "level": "L3", "min_dist_m": 0.0,
                               "duration_s": 3.4, "source": "video", "snapshot_t": 9.0, "clip": [4.9, 12.3]}}]
rs = Runtime(7)
rs.advance(30)
SM.VIDEO_NM[:] = sav
ids = [x["id"] for x in rs.rec.near_miss]
check("시뮬레이션 모드: 실제 영상 아차사고를 영상 한 바퀴(11분)마다 기록 · 알림은 첫 회차만",
      ids == ["NM-X-R1", "NM-X-R2", "NM-X-R3"] and sum(a["kind"] == "아차사고" for a in rs.rec.alerts_all) == 1, str(ids))
rs2 = Runtime(7); rs2.advance(240)
check("시뮬레이션 모드: 호퍼 합성 안전 경고(무작위 작업자) 꺼짐 · 서버 없이 만들면 영상 아차사고 목록도 비어 있음",
      not rs2.rec.safety and not rs2.rec.near_miss, f"{len(rs2.rec.safety)}건")

bad_r = [r for r in results if not r[1]]
print(f"\n{len(results) - len(bad_r)}/{len(results)} 통과")
sys.exit(1 if bad_r else 0)
