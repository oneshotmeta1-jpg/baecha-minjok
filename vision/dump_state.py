"""덤프트럭 적재함 상태 판정 (영상 한 장 → 하차 중 / 적재함 내림)

YOLO(COCO 'truck')로 트럭을 찾고, 같은 트럭의 박스들을 합친 '전체 박스'의 세로/가로 비율로 판정한다.
- 적재함을 내린 덤프트럭: 옆에서 보면 길고 낮다 → 비율 약 0.35~0.5
- 적재함을 들어 올린 덤프트럭(하차 중): 적재함이 위로 솟아 박스가 높아진다 → 비율 0.6 이상
옆모습 기준 규칙이라 카메라는 호퍼 옆에서 차량 측면을 보도록 둔다(가정).

사용: python vision/dump_state.py <이미지> [--weights yolo11n.pt] [--out web/assets/dump_sample.jpg]
"""
import argparse
import json
import sys
from pathlib import Path

RAISED_RATIO = 0.6


def judge(boxes):
    """boxes: [(x1,y1,x2,y2,conf)] — 겹치는 트럭 박스를 하나로 합쳐 판정."""
    if not boxes:
        return None
    x1 = min(b[0] for b in boxes); y1 = min(b[1] for b in boxes)
    x2 = max(b[2] for b in boxes); y2 = max(b[3] for b in boxes)
    ratio = (y2 - y1) / max(x2 - x1, 1)
    raised = ratio >= RAISED_RATIO
    return {"box": [round(x1), round(y1), round(x2), round(y2)], "ratio": round(ratio, 2),
            "state": "unloading" if raised else "bed_down",
            "label": "하차 중 · 적재함 상승" if raised else "적재함 내림",
            "conf": round(max(b[4] for b in boxes), 2)}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--weights", default="yolo11n.pt")
    ap.add_argument("--out", default=None, help="박스·판정을 그린 이미지 저장 경로")
    ap.add_argument("--width", type=int, default=900)
    a = ap.parse_args()

    from ultralytics import YOLO
    from PIL import Image, ImageDraw, ImageFont

    r = YOLO(a.weights).predict(a.image, conf=0.25, verbose=False)[0]
    trucks = [(*b.xyxy[0].tolist(), float(b.conf)) for b in r.boxes if r.names[int(b.cls)] == "truck"]
    res = judge(trucks)
    out = {"image": Path(a.image).name, "model": Path(a.weights).name, "raw": [
        {"cls": "truck", "conf": round(t[4], 3), "box": [round(v) for v in t[:4]]} for t in trucks], "judge": res}
    print(json.dumps(out, ensure_ascii=False, indent=1))

    if a.out and res:
        im = Image.open(a.image).convert("RGB")
        k = a.width / im.width
        im = im.resize((a.width, round(im.height * k)))
        d = ImageDraw.Draw(im)
        try:
            font = ImageFont.truetype("malgunbd.ttf", 26)
            small = ImageFont.truetype("malgun.ttf", 20)
        except OSError:
            font = small = ImageFont.load_default()
        for t in trucks:
            d.rectangle([v * k for v in t[:4]], outline=(255, 255, 255), width=2)
        bx = [v * k for v in res["box"]]
        color = (215, 38, 61) if res["state"] == "unloading" else (29, 71, 184)
        d.rectangle(bx, outline=color, width=5)
        text = f"truck {res['conf']:.2f} · {res['label']}"
        tw = d.textlength(text, font=font)
        d.rectangle([bx[0], bx[1], bx[0] + tw + 16, bx[1] + 38], fill=color)
        d.text((bx[0] + 8, bx[1] + 4), text, fill=(255, 255, 255), font=font)
        note = f"전체 박스 세로/가로 {res['ratio']} ≥ {RAISED_RATIO} → 적재함 상승"
        d.rectangle([bx[0], bx[1] + 40, bx[0] + d.textlength(note, font=small) + 16, bx[1] + 70], fill=(20, 20, 20))
        d.text((bx[0] + 8, bx[1] + 44), note, fill=(255, 255, 255), font=small)
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        im.save(a.out, quality=82)
        Path(a.out).with_suffix(".json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
