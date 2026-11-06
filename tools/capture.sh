#!/usr/bin/env bash
# 화면 전후 비교용 스크린샷: bash tools/capture.sh <접두어> [주소=http://127.0.0.1:8001] [크기=1920,1080]
# 헤드리스 Chrome으로 다섯 화면을 찍어 docs/screens/<접두어>_<화면>.png 로 저장한다.
P=${1:-shot}; B=${2:-http://127.0.0.1:8001}; S=${3:-1920,1080}
C="/c/Program Files/Google/Chrome/Application/chrome.exe"
OUT="$(cd "$(dirname "$0")/.." && pwd)/docs/screens"
mkdir -p "$OUT"
for v in monitor dock analysis alerts settings; do
  "$C" --headless=new --disable-gpu --hide-scrollbars --window-size=$S --virtual-time-budget=8000 \
       --screenshot="$(cygpath -w "$OUT/${P}_$v.png")" "$B/#$v" >/dev/null 2>&1
  echo "$OUT/${P}_$v.png"
done
