"""비전 결과(프레임별 탐지·추적) → 대시보드 이벤트 (워크플로우 ② 감지 구조)

    py vision/to_events.py 결과.json --start 2026-11-09T08:00:00 --out events.jsonl
    py tools/replay_events.py events.jsonl --url http://127.0.0.1:8000 --speed 60   # 서버는 --video 로

입력(비전 모듈이 쓰는 형식, 카메라마다 또는 한 파일에 섞어서):
    {"size": [1920, 1080],
     "frames": [{"t": 12.4, "cam_id": "CAM-01",
                 "objects": [{"track_id": 17, "cls": "truck", "bbox": [x1, y1, x2, y2], "vehicle_id": "82가 1234"},
                             {"track_id": 3,  "cls": "person", "bbox": [...]}]}]}
    - t: 영상 시작부터 초. bbox: 화면 픽셀. vehicle_id는 비콘·번호판으로 붙였으면 넣고, 없으면 track_id로 "T17".
판정 규칙 (설정 › 구역(ROI) = data/rois.json):
    - 위치 = 박스 아래 중심점(바퀴 접지점). 구역 안에 3초 이상 있어야 진입, 3초 이상 밖이어야 이탈(흔들림 무시).
    - 하역 구역 BAY-n 진입/이탈 → BAY_ENTER/BAY_EXIT(bay=n) · 대기선 → ZONE_ENTER(zone=queue) · 대기장 카메라 → ZONE_ARRIVE
    - 3초 동안 접지점이 거의 안 움직이면 VEHICLE_STOPPED
    - 아차사고(R15, vision/near_miss.py): 사람 발끝이 위험 구역(덤프 반경 · 후진 경로)에 있고 그 베이 트럭 조건이 맞으면
      L1 → (1초) L2 → (2초) L3. 위험이 끝날 때 NEAR_MISS 한 건, L3에 오르는 순간 SAFETY_ALERT(하차 중지 요청).
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import near_miss  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HOLD = 3.0          # 초
STOP_PX = 12        # 3초 동안 이만큼 안 움직이면 정지
RAISED = 0.6        # 적재함 상승 판정: 박스 세로/가로 — 최근 1초 중앙값이 1초 동안 넘어야 상승(R9 실영상 확인, docs/vision_check.md)
RAISE_WIN = 1.0     # 초
SAME_IOU = 0.4      # 새 추적 ID가 3초 안에 본 트럭 박스와 이만큼 겹치면 같은 트럭(ID 바뀜·중복 박스 합치기)
MIN_AREA = 0.003    # 화면 넓이의 0.3%보다 작은 트럭 박스는 버림(멀리 지나가는 차·오탐)


def inside(pt, poly):
    x, y = pt
    c = False
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-9) + x1:
            c = not c
    return c


def foot(b):
    return ((b[0] + b[2]) / 2, b[3])


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    i = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u > 0 else 0


def video_rois(cams_cfg, cam_id):
    """data/cams.json의 영상 구역(영상 픽셀) → convert()가 읽는 rois 모양"""
    c = next(c for c in cams_cfg["cams"] if c["camera"] == cam_id)
    return {"frame": c["size"], "m_per_px": c.get("m_per_px"), "cams": [{"camera": c["camera"], "sector_id": c["sector_id"], "zones": c["zones"]}]}


def convert(data, start, rois=None, scale=1.0, flush=False, nm_cfg=None):
    rois = rois or json.loads((ROOT / "data" / "rois.json").read_text(encoding="utf-8"))
    cams = {c["camera"]: c for c in rois["cams"]}
    fw, fh = data.get("size", rois["frame"])
    kx, ky = rois["frame"][0] / fw, rois["frame"][1] / fh
    t0 = datetime.fromisoformat(start)
    out = []
    trk = {}            # (cam, track) → {zone, cand, since, last_pt, pt_since, stopped, bay_t}
    nm = near_miss.Tracker(nm_cfg or near_miss.config(), lambda *a: ev(*a), m_per_px=rois.get("m_per_px"))
    alias = {}          # (cam, 추적 ID) → 같은 트럭으로 본 처음 ID
    seen = {}           # (cam, 대표 ID) → (마지막 시각, 마지막 박스)
    area0 = rois["frame"][0] * rois["frame"][1]

    def ev(t, typ, cam, vid, tid, sid=None, zone=None, payload=None):
        out.append({"ts": (t0 + timedelta(seconds=t * scale)).isoformat(timespec="seconds"), "type": typ, "source": "cam", "cam_id": cam,
                    "sector_id": sid, "zone": zone, "track_id": tid, "vehicle_id": vid, "payload": payload or {}})

    def move(t, cam_id, sid, tid, s, zid):
        """구역 후보가 3초 이상 유지되면 진입·이탈 확정 (zid=None: 구역 밖 · 화면에서 사라짐)"""
        vid = s["vid"]
        if zid != s["cand"]:
            s["cand"], s["since"] = zid, t
        elif zid != s["zone"] and t - s["since"] >= HOLD:
            old = s["zone"]
            if old and "-BAY-" in old:
                ev(t, "BAY_EXIT", cam_id, vid, tid, sid, "bay", {"bay": int(old.rsplit("-", 1)[1])})
            if old:
                ev(t, "ZONE_EXIT", cam_id, vid, tid, sid, "bay" if "-BAY-" in old else ("yard" if sid is None else "queue"))
            s["zone"] = zid
            if zid and "-BAY-" in zid:
                s["bay_t"] = t
                ev(t, "BAY_ENTER", cam_id, vid, tid, sid, "bay", {"bay": int(zid.rsplit("-", 1)[1])})
            elif zid and sid is None:
                ev(t, "ZONE_ARRIVE", cam_id, vid, tid, None, "yard")
            elif zid:
                ev(t, "ZONE_ENTER", cam_id, vid, tid, sid, "queue")

    for fr in sorted(data["frames"], key=lambda f: (f["t"], f["cam_id"])):
        cam = cams.get(fr["cam_id"])
        if not cam:
            continue
        t, sid = fr["t"], cam["sector_id"]
        zones = cam["zones"]
        trucks = {}
        for o in fr["objects"]:
            b = [o["bbox"][0] * kx, o["bbox"][1] * ky, o["bbox"][2] * kx, o["bbox"][3] * ky]
            pt = foot(b)
            if o["cls"] == "truck":
                if (b[2] - b[0]) * (b[3] - b[1]) < area0 * MIN_AREA:
                    continue
                key = (fr["cam_id"], o["track_id"])
                if key not in alias:            # 처음 보는 ID: 3초 안에 본 트럭과 겹치면 같은 트럭
                    hit = [k for k, (lt, lb) in seen.items() if k[0] == fr["cam_id"] and t - lt <= HOLD and iou(lb, b) >= SAME_IOU]
                    alias[key] = min(hit, key=lambda k: trk[k]["first"])[1] if hit else o["track_id"]
                tid = alias[key]
                if (fr["cam_id"], tid) in trucks:   # 같은 트럭의 중복 박스는 한 번만
                    continue
                seen[(fr["cam_id"], tid)] = (t, b)
                o = dict(o, track_id=tid)
                z = next((z for z in zones if z["type"] in ("하차", "대기") and inside(pt, z["points"])), None)
                zid = z["id"] if z else None
                vid = o.get("vehicle_id") or f"{fr['cam_id']}-T{o['track_id']}"
                k = (fr["cam_id"], o["track_id"])
                s = trk.setdefault(k, {"zone": None, "cand": None, "since": t, "pt": pt, "pt_t": t, "stopped": False, "bay_t": None, "vid": vid, "first": t})
                hist = s.setdefault("hist", [])          # (t, 비율) 최근 1초
                hist.append((t, (b[3] - b[1]) / max(b[2] - b[0], 1)))
                while hist and t - hist[0][0] > RAISE_WIN:
                    hist.pop(0)
                rs = sorted(h[1] for h in hist)
                c_up = rs[len(rs) // 2] >= RAISED          # 1초 중앙값 → 한 프레임 흔들림 무시
                if c_up != s.get("up_cand"):
                    s["up_cand"], s["up_since"] = c_up, t
                elif c_up != s.get("up", False) and t - s["up_since"] >= RAISE_WIN:   # 1초 유지돼야 상승/내림으로 바꿈
                    s["up"] = c_up
                raised = s.get("up", False)
                s["box"] = b
                trucks[k] = (zid, raised, s)
                move(t, fr["cam_id"], sid, o["track_id"], s, zid)
                d = ((pt[0] - s["pt"][0]) ** 2 + (pt[1] - s["pt"][1]) ** 2) ** 0.5
                if d > STOP_PX:
                    s["pt"], s["pt_t"], s["stopped"] = pt, t, False
                elif not s["stopped"] and t - s["pt_t"] >= HOLD and s["zone"]:
                    s["stopped"] = True
                    ev(t, "VEHICLE_STOPPED", fr["cam_id"], vid, o["track_id"], sid, "bay" if "-BAY-" in s["zone"] else "queue")
        for k, s in trk.items():                      # 이번 프레임에 안 보인 트럭 = 구역 밖
            if k[0] == fr["cam_id"] and k not in trucks and t - seen.get(k, (s["since"],))[0] > 1.0:   # 1초 넘게 안 보이면 구역 밖
                move(t, k[0], sid, k[1], s, None)
        for o in fr["objects"]:
            if o["cls"] != "person":
                continue
            b = [o["bbox"][0] * kx, o["bbox"][1] * ky, o["bbox"][2] * kx, o["bbox"][3] * ky]
            nm.see(t, fr["cam_id"], sid, o, b, zones, trucks, px_scale=kx)
        nm.end_frame(t, fr["cam_id"])
    nm.close_all()
    if flush and data["frames"]:            # 영상 한 번 = 반입 한 번: 끝날 때 하차장에 남은 차는 이탈로 닫는다
        t_end = max(f["t"] for f in data["frames"])
        for (cam_id, tid), s in trk.items():
            if s["zone"] and "-BAY-" in s["zone"]:
                sid = s["zone"].split("-")[0]
                ev(t_end, "BAY_EXIT", cam_id, s["vid"], tid, sid, "bay", {"bay": int(s["zone"].rsplit("-", 1)[1]), "closed_at_clip_end": True})
                ev(t_end, "ZONE_EXIT", cam_id, s["vid"], tid, sid, "bay")
                s["zone"] = None
    return out


def main(argv):
    ap = argparse.ArgumentParser(description="비전 결과 → 대시보드 이벤트")
    ap.add_argument("result")
    ap.add_argument("--start", default="2026-11-09T08:00:00", help="영상 0초의 시각")
    ap.add_argument("--out", default="-")
    a = ap.parse_args(argv)
    evs = convert(json.loads(Path(a.result).read_text(encoding="utf-8")), a.start)
    text = "\n".join(json.dumps(e, ensure_ascii=False) for e in evs) + "\n"
    if a.out == "-":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(text)
    else:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"{len(evs)}건 → {a.out}")


if __name__ == "__main__":
    main(sys.argv[1:])
