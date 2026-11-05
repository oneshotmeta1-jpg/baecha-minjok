"""상하역 지연 관제 - FastAPI 서버

- 시뮬레이션 시계: 백그라운드에서 배속에 맞춰 sim.step() 을 돌린다 (시작·일시정지·배속·초기화)
- 이벤트 로그: 모든 이벤트를 SQLite(data/events.db)에 남긴다
- 화면: GET /api/state 와 WebSocket /ws 로 같은 상태를 받는다 (docs/events.md)
- 돌발 이벤트: 섹터 정지·복구, 긴급 출하 넣기

실행: python app/server.py  →  http://127.0.0.1:8000/
"""
import asyncio
import json
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app.sim import Sim, STEP, load  # noqa: E402

DATA = ROOT / "data"
WEB = ROOT / "web"
VERSION = "v1"
TICK = 0.1  # 실제 초


class EventLog:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts TEXT, type TEXT, source TEXT,"
                        " vehicle_id TEXT, sector_id TEXT, body TEXT)")
        self.clear()

    def clear(self):
        self.db.execute("DELETE FROM events")
        self.db.commit()

    def add(self, ev):
        self.db.execute("INSERT INTO events (ts,type,source,vehicle_id,sector_id,body) VALUES (?,?,?,?,?,?)",
                        (ev["ts"], ev["type"], ev["source"], ev["vehicle_id"], ev["sector_id"],
                         json.dumps(ev, ensure_ascii=False)))

    def recent(self, limit):
        rows = self.db.execute("SELECT body FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]


log = EventLog(DATA / "events.db")
sim = Sim(seed=7, on_event=log.add)
clients: set = set()


async def ticker():
    acc = 0.0
    prev = time.monotonic()
    while True:
        await asyncio.sleep(TICK)
        now = time.monotonic()
        real, prev = min(now - prev, 1.0), now        # 실제로 흐른 시간만큼 (멈칫해도 1초 이상 따라잡지 않음)
        if sim.running:
            acc += sim.speed * real / 60.0           # speed = 시뮬레이션 초 / 실제 초
            while acc >= STEP:
                acc -= STEP
                sim.step()
            log.db.commit()
        await broadcast()


async def broadcast():
    if not clients:
        return
    msg = json.dumps(sim.state(), ensure_ascii=False)
    for ws in list(clients):
        try:
            await ws.send_text(msg)
        except Exception:
            clients.discard(ws)


@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(ticker())
    yield
    task.cancel()


app = FastAPI(title="상하역 지연 관제", version=VERSION, lifespan=lifespan)


@app.get("/api/health")
def health():
    return {"ok": True, "version": VERSION}


@app.get("/api/config")
def config():
    return JSONResponse({"version": VERSION, "sectors": load("sectors.json"),
                         "std_times": load("std_times.json"), "settings": load("settings.json")})


@app.get("/api/state")
def state():
    return sim.state()


@app.get("/api/events")
def events(limit: int = 50):
    return log.recent(min(limit, 500))


@app.post("/api/sim/start")
def sim_start():
    sim.running = True
    return {"running": True}


@app.post("/api/sim/pause")
def sim_pause():
    sim.running = False
    return {"running": False}


@app.post("/api/sim/reset")
def sim_reset():
    sim.reset()
    log.clear()
    return {"ok": True}


@app.post("/api/sim/speed")
def sim_speed(value: int):
    if not 1 <= value <= 1200:
        raise HTTPException(400, "배속은 1~1200 사이여야 합니다")
    sim.speed = value
    return {"speed": value}


def need_sector(sector_id):
    if sector_id not in sim.sec_cfg:
        raise HTTPException(404, f"섹터 {sector_id}가 없습니다")


@app.post("/api/events/sector_down")
def sector_down(sector_id: str):
    need_sector(sector_id)
    sim.sector_down(sector_id)
    sim.replan()
    return {"ok": True}


@app.post("/api/events/sector_up")
def sector_up(sector_id: str):
    need_sector(sector_id)
    sim.sector_up(sector_id)
    sim.replan()
    return {"ok": True}


@app.post("/api/events/urgent")
def urgent():
    return {"vehicle_id": sim.add_urgent()}


@app.post("/api/alerts/{alert_id}/ack")
def ack(alert_id: str):
    if not sim.ack_alert(alert_id):
        raise HTTPException(404, "알림이 없습니다")
    return {"ok": True}


@app.websocket("/ws")
async def ws(socket: WebSocket):
    await socket.accept()
    clients.add(socket)
    try:
        await socket.send_text(json.dumps(sim.state(), ensure_ascii=False))
        while True:
            await socket.receive_text()
    except WebSocketDisconnect:
        clients.discard(socket)


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/web", StaticFiles(directory=WEB), name="web")


def lan_ips():
    """이 PC의 내부망 IPv4 주소들 (같은 와이파이의 팀원이 들어올 주소)."""
    import socket
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))          # 실제로 보내지 않음, 기본 경로의 내 주소만 얻음
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        ips.update(i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


if __name__ == "__main__":
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser(description="상하역 지연 관제 서버")
    ap.add_argument("--lan", action="store_true", help="같은 와이파이의 다른 기기도 접속하게 연다 (0.0.0.0)")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # 실행 창·로그에서 한글이 깨지지 않게
    except (AttributeError, ValueError):
        pass
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    print(f"이 PC: http://127.0.0.1:{args.port}/")
    if args.lan:
        for ip in lan_ips():
            print(f"팀원 공유 주소: http://{ip}:{args.port}/")
    uvicorn.run(app, host=host, port=args.port)
