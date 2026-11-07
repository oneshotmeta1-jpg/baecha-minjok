"""헤더 한 줄 확인용: 화면 폭 4개 × 알림 0/있음 헤더를 찍어 한 장으로 붙인다 (R10).

    py tools/capture_header.py <접두어> [주소=http://127.0.0.1:8001] [--badge 120]

- 헤드리스 Chrome으로 폭 1280·1440·1600·1920 × 높이 1080 화면을 찍고 위쪽 헤더만 잘라 docs/screens/<접두어>_header.png 로.
- --badge N: 주소에 ?badge=N 을 붙여 알림 배지를 N으로 고정(R10 화면만 지원 · 스크린샷 확인용).
- 헤더 줄 수는 헤더 높이로 판정한다(한 줄 ≈ 56px).
"""
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
WIDTHS = [1280, 1440, 1600, 1920]


def shot(url, w, out):
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={w},1080",
                    "--virtual-time-budget=6000", f"--screenshot={out}", url], capture_output=True, timeout=120)
    return cv2.imdecode(np.fromfile(str(out), np.uint8), cv2.IMREAD_COLOR) if out.exists() else None   # 한글 경로


def header_h(img):
    """헤더 바 높이(px): 위에서부터 배경색(바탕)이 아닌 줄이 처음 이어지는 구간"""
    bg = img[2, 2].astype(int)
    diff = np.abs(img[:, 10:-10].astype(int) - bg).max(axis=2) > 6
    busy = diff.mean(axis=1) > 0.02
    ys = np.where(busy)[0]
    if not len(ys):
        return 0
    y = ys[0]
    while y < len(busy) and busy[y]:
        y += 1
    return int(y - ys[0])


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    pre = argv[0] if argv else "hdr"
    base = argv[1] if len(argv) > 1 and not argv[1].startswith("--") else "http://127.0.0.1:8001"
    badge = argv[argv.index("--badge") + 1] if "--badge" in argv else None
    out_dir = ROOT / "docs" / "screens"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for w in WIDTHS:
        tmp = out_dir / f"_tmp_{w}.png"
        url = base + ("/?badge=" + badge if badge else "/") + "#monitor"
        img = shot(url, w, tmp)
        tmp.unlink(missing_ok=True)
        h = header_h(img)
        crop = img[:150, :]
        pad = np.full((150, 1920 - w, 3), 237, np.uint8) if w < 1920 else np.zeros((150, 0, 3), np.uint8)
        row = np.hstack([crop, pad])
        cv2.putText(row, f"{w}px  header {h}px", (1920 - 420, 140), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 200), 2)
        rows.append(row)
        print(f"{w}px: 헤더 높이 {h}px")
    out = out_dir / f"{pre}_header.png"
    cv2.imencode('.png', np.vstack(rows))[1].tofile(str(out))
    print(out)


if __name__ == "__main__":
    main(sys.argv[1:])
