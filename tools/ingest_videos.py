"""영상 4개의 탐지 결과(det.json) → 이벤트 → 서버 /api/ingest — "영상 한 번 재생 = 반입 한 번"

    py app/server.py --video                 # 영상 모드 서버
    py vision/detect_video.py                # 영상 → <이름>.det.json (한 번만)
    py tools/ingest_videos.py                # 서버 지금 시각부터 이벤트를 한 번 넣음
    py tools/ingest_videos.py --dry          # 보내지 않고 이벤트만 출력

- 화면의 영상 반복 재생은 이벤트를 다시 넣지 않는다. 같은 파일을 두 번 실행해도 서버가 event_id로 중복을 거른다(결과 "duplicate").
- 영상에는 계근 신호가 없어서, 영상에 처음 나온 차마다 GATE_IN을 1분 앞에 넣는다(원료·적재량은 가정, payload.assumed=true).
- 영상 시각은 data/cams.json time_scale배로 늘린다(15초 클립 → 10분).
- 아차사고(R15·R16): 영상에 나온 실제 사람으로 판정한다(대기장 CAM-04 접근 규칙 ③ 등, payload.source = video).
  data/cams.json near_miss.stand_in = true일 때만 호퍼 영상에 data/stand_in.json의 합성 사람을 섞는다(source = stand_in).
"""
import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vision"))
from to_events import convert, video_rois  # noqa: E402
import near_miss  # noqa: E402


def eid(e, start, video, play):
    """중복 키 = 영상 파일 · 재생 회차 · 영상 안 시각 · 종류 · 차량 (넣는 시각과 무관 → 같은 영상을 다시 넣으면 duplicate)"""
    rel = round((datetime.fromisoformat(e["ts"]) - datetime.fromisoformat(start)).total_seconds())
    k = "|".join(map(str, (video, play, rel, e["type"], e.get("vehicle_id")))) + "|" + json.dumps(e.get("payload", {}), sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(k.encode("utf-8")).hexdigest()[:16]


def build(start, cfg=None, sectors=None, settings=None, play=1, stand_in=None):
    cfg = cfg or json.loads((ROOT / "data" / "cams.json").read_text(encoding="utf-8"))
    sectors = sectors or json.loads((ROOT / "data" / "sectors.json").read_text(encoding="utf-8"))
    settings = settings or json.loads((ROOT / "data" / "settings.json").read_text(encoding="utf-8"))
    bc = {s["id"]: s.get("bay_cargo") or s["cargo"] for s in sectors["sectors"]}
    mix = [c for c, n in settings["fleet"]["cargo_mix"].items() for _ in range(n)]
    nm_cfg = near_miss.config(cfg)
    if stand_in is None:
        stand_in = near_miss.load_stand_in() if near_miss.stand_in_on(nm_cfg) else {"cams": {}}
    out, report = [], []
    for c in cfg["cams"]:
        det = ROOT / cfg["dir"] / (Path(c["video"]).stem + ".det.json")
        if not det.exists():
            report.append((c["camera"], "det.json 없음"))
            continue
        d = near_miss.merge_stand_in(json.loads(det.read_text(encoding="utf-8")), stand_in.get("cams", {}).get(c["camera"]))
        evs = convert(d, start, video_rois(cfg, c["camera"]), scale=cfg.get("time_scale", 1), flush=True, nm_cfg=nm_cfg)
        nms = [e for e in evs if e["type"] == "NEAR_MISS"]
        report.append((c["camera"], f"이벤트 {len(evs)}건" + (f" · 아차사고 {len(nms)}건(" + ", ".join(e["payload"]["level"] + " " + e["payload"]["rule"] + ("·합성" if e["payload"]["source"] == "stand_in" else "") for e in nms) + ")" if nms else "")))
        first = {}
        for e in evs:
            first.setdefault(e["vehicle_id"], e)
        for i, (vid, e) in enumerate(first.items()):
            if c["sector_id"]:
                cargo = bc[c["sector_id"]][(c.get("bay") or 1) - 1]
            else:
                cargo = mix[int(hashlib.sha1(vid.encode()).hexdigest(), 16) % len(mix)]
            ts = (datetime.fromisoformat(e["ts"]) - timedelta(minutes=1)).isoformat(timespec="seconds")
            out.append({"ts": ts, "type": "GATE_IN", "source": "iot", "cam_id": None, "sector_id": None, "zone": "gate",
                        "track_id": e["track_id"], "vehicle_id": vid, "payload": {"cargo": cargo, "load_t": 22.0, "assumed": True}})
        for e in out[len(out) - len(first):] + evs:
            e["event_id"] = eid(e, start, c["video"], play)
            e["payload"] = {**e.get("payload", {}), "video": c["video"], "play": play}
        out += evs
    out.sort(key=lambda e: (e["ts"], e["type"] != "GATE_IN"))
    return out, report


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--start", help="영상 0초의 시각(없으면 서버 지금 시각)")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--play", type=int, default=1, help="재생 회차. 같은 회차를 다시 넣으면 서버가 duplicate로 거른다")
    a = ap.parse_args(argv)
    start = a.start
    if not start and not a.dry:
        start = json.loads(urllib.request.urlopen(a.url + "/api/state", timeout=10).read())["clock"]
    evs, rep = build(start or "2026-11-09T08:00:00", play=a.play)
    for cam, r in rep:
        print(cam, r)
    if a.dry:
        for e in evs:
            print(e["ts"], e["type"], e["vehicle_id"], e["payload"])
        return
    req = urllib.request.Request(a.url + "/api/ingest", data=json.dumps(evs, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    res = json.loads(urllib.request.urlopen(req, timeout=60).read())["results"]
    print("결과", {r: res.count(r) for r in set(res)})


if __name__ == "__main__":
    main(sys.argv[1:])
