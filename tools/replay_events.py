"""이벤트 파일(JSONL)을 시각 순서대로 서버 /api/ingest 로 보낸다 — 비전 결과 재생 · 시연용.

    py app/server.py --video --port 8000            # 영상 모드 서버
    py vision/to_events.py 결과.json --out events.jsonl
    py tools/replay_events.py events.jsonl --speed 60   # 1분 영상을 1초에 (0이면 기다리지 않고 한 번에)

GATE_IN(계근대 · 기사 앱)처럼 영상에 없는 신호는 같은 형식으로 파일에 넣으면 된다(docs/events.md).
"""
import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime


def post(url, evs):
    req = urllib.request.Request(url + "/api/ingest", data=json.dumps(evs, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--speed", type=float, default=60, help="영상 초 / 실제 초 (0 = 한 번에)")
    a = ap.parse_args(argv)
    evs = [json.loads(l) for l in open(a.file, encoding="utf-8") if l.strip()]
    evs.sort(key=lambda e: e["ts"])
    if a.speed <= 0:
        print(post(a.url, evs))
        return
    t_prev = None
    for e in evs:
        t = datetime.fromisoformat(e["ts"])
        if t_prev is not None:
            time.sleep(max((t - t_prev).total_seconds() / a.speed, 0))
        t_prev = t
        r = post(a.url, [e])
        print(e["ts"], e["type"], e.get("vehicle_id") or "", r["results"][0])


if __name__ == "__main__":
    main(sys.argv[1:])
