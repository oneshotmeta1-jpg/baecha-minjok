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
    - 사람이 REAR-n(덤프 반경)에 들어오고 그 베이 트럭의 적재함이 올라가 있으면(박스 세로/가로 ≥ 0.6) SAFETY_ALERT(덤프 반경)
    - 사람이 BACK-n(후진 경로)에 들어오고 그 베이 트럭이 진입한 지 30초 안이면 SAFETY_ALERT(후진 경로)
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOLD = 3.0          # 초
STOP_PX = 12        # 3초 동안 이만큼 안 움직이면 정지
RAISED = 0.6        # 적재함 상승 판정: 박스 세로/가로


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


def convert(data, start, rois=None):
    rois = rois or json.loads((ROOT / "data" / "rois.json").read_text(encoding="utf-8"))
    cams = {c["camera"]: c for c in rois["cams"]}
    fw, fh = data.get("size", rois["frame"])
    kx, ky = rois["frame"][0] / fw, rois["frame"][1] / fh
    t0 = datetime.fromisoformat(start)
    out = []
    trk = {}            # (cam, track) → {zone, cand, since, last_pt, pt_since, stopped, bay_t}
    person_in = set()   # (cam, track, zone_id)

    def ev(t, typ, cam, vid, tid, sid=None, zone=None, payload=None):
        out.append({"ts": (t0 + timedelta(seconds=t)).isoformat(timespec="seconds"), "type": typ, "source": "cam", "cam_id": cam,
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
                z = next((z for z in zones if z["type"] in ("하차", "대기") and inside(pt, z["points"])), None)
                zid = z["id"] if z else None
                vid = o.get("vehicle_id") or f"T{o['track_id']}"
                k = (fr["cam_id"], o["track_id"])
                s = trk.setdefault(k, {"zone": None, "cand": None, "since": t, "pt": pt, "pt_t": t, "stopped": False, "bay_t": None, "vid": vid})
                raised = (b[3] - b[1]) / max(b[2] - b[0], 1) >= RAISED
                trucks[k] = (zid, raised, s)
                move(t, fr["cam_id"], sid, o["track_id"], s, zid)
                d = ((pt[0] - s["pt"][0]) ** 2 + (pt[1] - s["pt"][1]) ** 2) ** 0.5
                if d > STOP_PX:
                    s["pt"], s["pt_t"], s["stopped"] = pt, t, False
                elif not s["stopped"] and t - s["pt_t"] >= HOLD and s["zone"]:
                    s["stopped"] = True
                    ev(t, "VEHICLE_STOPPED", fr["cam_id"], vid, o["track_id"], sid, "bay" if "-BAY-" in s["zone"] else "queue")
        for k, s in trk.items():                      # 이번 프레임에 안 보인 트럭 = 구역 밖
            if k[0] == fr["cam_id"] and k not in trucks:
                move(t, k[0], sid, k[1], s, None)
        for o in fr["objects"]:
            if o["cls"] != "person" or sid is None:
                continue
            b = [o["bbox"][0] * kx, o["bbox"][1] * ky, o["bbox"][2] * kx, o["bbox"][3] * ky]
            pt = foot(b)
            for z in zones:
                if z["type"] != "위험":
                    continue
                key = (fr["cam_id"], o["track_id"], z["id"])
                if not inside(pt, z["points"]):
                    person_in.discard(key)
                    continue
                if key in person_in:
                    continue
                n = z["id"].rsplit("-", 1)[1]
                bay_id = f"{sid}-BAY-{n}"
                hit = next(((k, s) for k, (zid, raised, s) in trucks.items() if zid == bay_id and (
                    (z.get("rule") != "후진 경로" and raised) or (z.get("rule") == "후진 경로" and s["bay_t"] is not None and t - s["bay_t"] <= 30))), None)
                if hit:
                    person_in.add(key)
                    (k, s) = hit
                    ev(t, "SAFETY_ALERT", fr["cam_id"], s["vid"], k[1], sid, "back" if z.get("rule") == "후진 경로" else "rear",
                       {"level": "L3", "rule": z.get("rule", "덤프 반경 5m"), "zone_id": z["id"], "person_track": o["track_id"],
                        "bay": int(n), "dist_m": None, "action": "앱 경고 · 하차 중지 요청"})
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
