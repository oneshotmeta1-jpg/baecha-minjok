"""영상 → 프레임별 탐지·추적 JSON (<영상이름>.det.json) — vision/to_events.py 입력 형식 그대로

    py vision/detect_video.py                       # data/cams.json의 영상 전부 (이미 있으면 건너뜀)
    py vision/detect_video.py --cam CAM-01 --force  # 하나만 다시

- YOLO11n(COCO 사전학습) + ByteTrack. truck·bus·car → "truck", person → "person".
- 결과: 영상 옆 <영상이름>.det.json  {"size":[w,h], "fps":..., "cam_id":..., "frames":[{"t","cam_id","objects":[{track_id,cls,bbox,conf}]}]}
- 화면 오버레이는 이 파일을 재생 시각에 맞춰 그린다(실시간 추론 아님 · 결과 재생).
- 가중치: vision/yolo11n.pt (git 제외). 없으면 --weights로 경로를 준다.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLS = {0: "person", 2: "truck", 5: "truck", 7: "truck"}


def cams_cfg():
    return json.loads((ROOT / "data" / "cams.json").read_text(encoding="utf-8"))


def det_path(video):
    return video.with_name(video.stem + ".det.json")


def detect(video, cam_id, weights, stride=1, conf=0.25):
    import cv2
    from ultralytics import YOLO
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 24
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    model = YOLO(weights)
    frames, i = [], 0
    while True:
        ok, im = cap.read()
        if not ok:
            break
        if i % stride == 0:
            r = model.track(im, persist=True, tracker="bytetrack.yaml", conf=conf, classes=list(CLS), verbose=False)[0]
            objs = []
            if r.boxes is not None and r.boxes.id is not None:
                for b, tid in zip(r.boxes, r.boxes.id.tolist()):
                    objs.append({"track_id": int(tid), "cls": CLS[int(b.cls)], "bbox": [round(v, 1) for v in b.xyxy[0].tolist()],
                                 "conf": round(float(b.conf), 2)})
            frames.append({"t": round(i / fps, 3), "cam_id": cam_id, "objects": objs})
        i += 1
    return {"size": [w, h], "fps": round(fps, 2), "cam_id": cam_id, "video": video.name, "model": Path(weights).name,
            "frames": frames, "duration": round(i / fps, 2)}


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--cam")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--weights", default=str(ROOT / "vision" / "yolo11n.pt"))
    ap.add_argument("--stride", type=int, default=1, help="몇 프레임마다 추론할지")
    a = ap.parse_args(argv)
    cfg = cams_cfg()
    for c in cfg["cams"]:
        if a.cam and c["camera"] != a.cam:
            continue
        v = ROOT / cfg["dir"] / c["video"]
        if not v.exists():
            print(f"{c['camera']}: 영상 없음 ({v.name}) — 건너뜀")
            continue
        out = det_path(v)
        if out.exists() and not a.force:
            print(f"{c['camera']}: 이미 있음 {out.name}")
            continue
        d = detect(v, c["camera"], a.weights, a.stride)
        out.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        n = sum(len(f["objects"]) for f in d["frames"])
        ids = {(o["cls"], o["track_id"]) for f in d["frames"] for o in f["objects"]}
        print(f"{c['camera']}: {len(d['frames'])}프레임 · 박스 {n}개 · 추적 ID 트럭 {sum(1 for k in ids if k[0]=='truck')} / 사람 {sum(1 for k in ids if k[0]=='person')} → {out.name}")


if __name__ == "__main__":
    main(sys.argv[1:])
