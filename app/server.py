"""하역관제 FastAPI 서버 — 포항제철소 소결공장 원료 반입

- 시뮬레이션 시계: 백그라운드에서 배속에 맞춰 추천 배정·FIFO 기준선을 함께 돌린다 (app/runtime.py)
- 화면: GET /api/state 와 WebSocket /ws (관제 화면), 탭별 GET /api/dock · /api/analysis · /api/alerts
- 담당자 행동: 순번 승인·변경(사유), 알림 확인, 가정값 변경(재계산), 피드백 제안 적용·무시
- 모든 핸들러는 async — 시계(이벤트 루프)와 같은 스레드에서 돌아 상태가 엇갈리지 않는다.

실행: python app/server.py [--lan]  →  http://127.0.0.1:8000/
"""
import asyncio
import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app.runtime import REASONS, Runtime  # noqa: E402
from app.sim import STEP, load  # noqa: E402
from app.cams import cam_list  # noqa: E402
from app import auth  # noqa: E402

DATA = ROOT / "data"
WEB = ROOT / "web"
VERSION = "v12"
TICK = 0.1  # 실제 초

import os  # noqa: E402
# DOCK_MEMORY=1: 테스트에서 이 모듈을 가져와도 이벤트 DB · 가정값 파일을 건드리지 않게 메모리에서만
rt = Runtime(seed=7) if os.environ.get("DOCK_MEMORY") else Runtime(seed=7, db_path=DATA / "events.db", assumptions_path=DATA / "assumptions.json")
clients: set = set()
mobiles: dict = {}   # 모바일 실시간 연결 → 로그인한 사람
mobile_sent: dict = {}


async def ticker():
    acc = 0.0
    prev = time.monotonic()
    while True:
        await asyncio.sleep(TICK)
        now = time.monotonic()
        real, prev = min(now - prev, 1.0), now
        if rt.running:
            acc += rt.rec.speed * real / 60.0           # speed = 시뮬레이션 초 / 실제 초
            while acc >= STEP:
                acc -= STEP
                rt.step()
            rt.store.commit()
        await broadcast()


async def broadcast():
    if not clients and not mobiles:
        return
    st = rt.state()
    msg = json.dumps(st, ensure_ascii=False)
    for ws in list(clients):
        try:
            await ws.send_text(msg)
        except Exception:
            clients.discard(ws)
    now = time.monotonic()
    due = [(ws, u) for ws, u in list(mobiles.items()) if now - mobile_sent.get(ws, 0) >= 1.0]   # 모바일은 1초에 한 번
    p2 = rt.panel2() if any(u["role"] != "driver" for _, u in due) else None
    for ws, u in due:
        mobile_sent[ws] = now           # 모바일: 사람마다 걸러서(운전자 = 내 차, 현장 = 담당 하역장)
        try:
            await ws.send_text(json.dumps(mobile_view(u, st, p2), ensure_ascii=False))
        except Exception:
            mobiles.pop(ws, None)
            mobile_sent.pop(ws, None)


def mobile_view(u, st=None, p2=None):
    if u["role"] == "driver":
        return {"role": "driver", **rt.driver_view((u.get("vehicle") or {}).get("plate"), st)}
    hop = ["A", "B", "C", "YARD"] if u["role"] == "control" else (u.get("hoppers") or [])
    return {"role": "field", **rt.field_view(hop, st, p2)}


@asynccontextmanager
async def lifespan(_app):
    import threading
    threading.Thread(target=rt.week_days, daemon=True).start()     # 최근 7일(시뮬레이션)을 미리 계산해 둔다 (약 5초)
    task = asyncio.create_task(ticker())
    yield
    task.cancel()


app = FastAPI(title="하역관제", version=VERSION, lifespan=lifespan)


def need_sector(sector_id):
    if sector_id not in rt.rec.sec_cfg:
        raise HTTPException(404, f"호퍼 {sector_id}가 없습니다")


def need_vehicle(vehicle_id):
    if vehicle_id not in rt.rec.vehicles:
        raise HTTPException(404, f"차량 {vehicle_id}가 없습니다")


@app.get("/api/health")
async def health():
    return {"ok": True, "version": VERSION}


@app.get("/api/config")
async def config():
    return JSONResponse({"version": VERSION, "sectors": load("sectors.json"), "site_map": load("site_map.json"), "std_times": load("std_times.json"),
                         "settings": rt.rec.settings, "reasons": REASONS})


@app.get("/api/state")
async def state():
    return rt.state()


@app.get("/api/events")
async def events(limit: int = 50, sector_id: str | None = None):
    if sector_id:       # 하역장 상세 › 이벤트 기록: 그 호퍼 것만
        return [e for e in rt.store.recent_events(2000) if e.get("sector_id") == sector_id][:min(limit, 500)]
    return rt.store.recent_events(min(limit, 500))


# ---------- 시계 ----------
@app.post("/api/sim/start")
async def sim_start():
    rt.running = True
    return {"running": True}


@app.post("/api/sim/pause")
async def sim_pause():
    rt.running = False
    return {"running": False}


@app.post("/api/sim/reset")
async def sim_reset():
    rt.running = False
    rt.reset()
    return {"ok": True}


@app.post("/api/sim/speed")
async def sim_speed(value: int):
    if not 1 <= value <= 1200:
        raise HTTPException(400, "배속은 1~1200 사이여야 합니다")
    rt.speed = value
    rt.rec.speed = value
    return {"speed": value}


# ---------- 시연 버튼 ----------
@app.post("/api/events/sector_down")
async def sector_down(sector_id: str):
    need_sector(sector_id)
    rt.sector_down(sector_id)
    return {"ok": True}


@app.post("/api/events/sector_up")
async def sector_up(sector_id: str):
    need_sector(sector_id)
    rt.sector_up(sector_id)
    return {"ok": True}


@app.post("/api/events/urgent")
async def urgent():
    return {"vehicle_id": rt.add_urgent()}


# ---------- 통합하역작업: 순번 승인 · 변경 ----------
@app.get("/api/dock")
async def dock():
    return rt.dock()


@app.post("/api/order/approve")
async def order_approve(vehicle_id: str):
    need_vehicle(vehicle_id)
    if not rt.approve(vehicle_id):
        raise HTTPException(409, "순번표에 없는 차량입니다")
    return {"ok": True}


@app.post("/api/approval")
async def approval(auto: bool):
    """자동 승인 스위치. auto=false면 현장 담당자가 승인해야 호출 (L2)."""
    return {"auto_approve": rt.set_auto_approve(auto)}


@app.get("/api/schedule")
async def schedule():
    return rt.schedule()


@app.get("/api/analysis2")
async def analysis2(range: str = "today"):
    """운영 분석 (성과·비용 · 건별 기록). range=week 는 시드를 바꾼 시뮬레이션 6일 + 오늘."""
    return rt.analysis_range(range if range in ("week", "demo") else "today")


@app.get("/api/alerts/history")
async def alerts_history(range: str = "today"):
    return rt.alerts_range(range if range in ("week", "demo") else "today")


@app.post("/api/alerts/{aid}/ignore")
async def alert_ignore(aid: str):
    a = rt.ignore_alert(aid)
    if not a:
        raise HTTPException(404, "없는 알림입니다")
    return {"ok": True}


@app.post("/api/preview")
async def preview(body: dict = Body(...)):
    """바꿔보기: 저장하지 않고 같은 행동 기록으로 다시 돌려 차이만 돌려준다."""
    try:
        return rt.preview(body)
    except (TypeError, ValueError):
        raise HTTPException(400, "숫자를 넣어 주세요")


@app.get("/api/settings")
async def settings_view():
    return rt.settings_view()


@app.get("/api/rois")
async def rois():
    return load("rois.json")


def csv_response(text, name):
    return Response(content=text.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/export/report.csv")
async def export_report(range: str = "today"):
    return csv_response(rt.csv_report(range if range in ("week", "demo") else "today"), f"report_{range}.csv")


@app.get("/api/export/trips.csv")
async def export_trips(range: str = "today"):
    return csv_response(rt.csv_trips(range if range in ("week", "demo") else "today"), f"trips_{range}.csv")


@app.get("/api/export/alerts.csv")
async def export_alerts(range: str = "today"):
    return csv_response(rt.csv_alerts(range if range in ("week", "demo") else "today"), f"alerts_{range}.csv")


@app.post("/api/ingest")
async def ingest(body=Body(...)):
    """영상(비전 결과)·현장 신호 이벤트 받기. 한 건(dict) 또는 여러 건(list). 형식은 docs/events.md.
    영상 모드(--video)에서는 차량 움직임이 이 이벤트로만 바뀐다."""
    return {"results": rt.ingest(body), "mode": "video" if rt.video else "sim"}


@app.get("/api/mode")
async def mode():
    return {"mode": "video" if rt.video else "sim"}


# ---------- 영상 채널 (R9): data/cams.json + 해커톤/영상/ ----------
CAMS = load("cams.json") if (DATA / "cams.json").exists() else {"dir": "영상", "cams": []}
MEDIA = ROOT / CAMS.get("dir", "영상")


@app.get("/api/cams")
async def cams():
    return {"cams": cam_list(CAMS, MEDIA), "time_scale": CAMS.get("time_scale", 1), "mode": "video" if rt.video else "sim"}


@app.get("/api/panel2")
async def panel2():
    """알림 패널 [순번 | 안전] (시안 v4)."""
    return rt.panel2()


@app.post("/api/safety/{sid}/ack")
async def safety_ack(sid: str):
    if not rt.safety_resolve(sid, "ack"):
        raise HTTPException(404, "열린 안전 경고가 없습니다")
    return {"ok": True}


@app.post("/api/safety/{sid}/dismiss")
async def safety_dismiss(sid: str):
    if not rt.safety_resolve(sid, "dismiss"):
        raise HTTPException(404, "열린 안전 경고가 없습니다")
    return {"ok": True}


@app.post("/api/events/safety")
async def safety_force(sector_id: str):
    """시연: 그 호퍼에서 하차 중인 차에 안전 경고를 만든다."""
    need_sector(sector_id)
    x = rt.force_safety(sector_id)
    if not x:
        raise HTTPException(409, "그 호퍼에서 하차 중인 차량이 없습니다")
    return x


@app.get("/api/panel")
async def panel():
    """알림 패널: 무슨 일 → 제안 → 효과(차량별 FIFO 대비) + 오늘 대기 절감 합계."""
    return rt.panel()


@app.post("/api/order/request_approval")
async def order_request_approval():
    """[현장 담당자에게 승인 요청] — 현장 담당자 앱으로 보냄(가상)."""
    return {"requested": rt.request_approval()}


@app.post("/api/order/approve_all")
async def order_approve_all():
    return {"approved": rt.approve_all()}


@app.get("/api/order/impact")
async def order_impact(vehicle_id: str, sector_id: str):
    need_vehicle(vehicle_id)
    need_sector(sector_id)
    return rt.impact(vehicle_id, sector_id)


@app.post("/api/order/change")
async def order_change(body: dict = Body(...)):
    vid, sid = body.get("vehicle_id"), body.get("sector_id")
    need_vehicle(vid)
    need_sector(sid)
    c, err = rt.change(vid, sid, body.get("reason", ""), body.get("memo", ""))
    if err:
        raise HTTPException(409, err)
    return c


# ---------- 가정값 ----------
@app.get("/api/assumptions")
async def get_assumptions():
    return {"values": rt.assumptions, "history": rt.history}


@app.post("/api/assumptions")
async def set_assumptions(body: dict = Body(...)):
    try:
        changed = rt.set_assumptions(body)
    except (TypeError, ValueError):
        raise HTTPException(400, "숫자로 적어 주세요")
    return {"changed": changed, "history": rt.history[:5]}


# ---------- 운영 분석 ----------
@app.get("/api/analysis")
async def analysis():
    return rt.analysis()


@app.post("/api/suggestion/apply")
async def suggestion_apply(key: str):
    changed = rt.apply_suggestion(key)
    if changed is None:
        raise HTTPException(404, "이미 처리된 제안입니다")
    return {"changed": changed}


@app.post("/api/suggestion/ignore")
async def suggestion_ignore(key: str):
    rt.ignore_suggestion(key)
    return {"ok": True}


# ---------- 알림 이력 ----------
@app.get("/api/alerts")
async def alerts(level: str = "", kind: str = "", sector_id: str = "", unacked: bool = False):
    return rt.alert_list(level or None, kind or None, sector_id or None, unacked)


@app.post("/api/alerts/{alert_id}/ack")
async def ack(alert_id: str):
    a = rt.ack(alert_id)
    if not a:
        raise HTTPException(404, "알림이 없습니다")
    return a


@app.websocket("/ws/m")
async def ws_mobile(socket: WebSocket):
    u = app.state.ws_user(socket, "/ws/m") if getattr(app.state, "ws_user", None) else None
    if not u:
        await socket.close(code=4401)
        return
    await socket.accept()
    mobiles[socket] = u
    try:
        await socket.send_text(json.dumps(mobile_view(u), ensure_ascii=False))
        while True:
            await socket.receive_text()
    except WebSocketDisconnect:
        mobiles.pop(socket, None)


@app.websocket("/ws")
async def ws(socket: WebSocket):
    if getattr(app.state, "ws_user", None) and not app.state.ws_user(socket):
        await socket.close(code=4401)          # 로그인 안 함 · 운전자(관제 화면 실시간은 관제·현장만)
        return
    await socket.accept()
    clients.add(socket)
    try:
        await socket.send_text(json.dumps(rt.state(), ensure_ascii=False))
        while True:
            await socket.receive_text()
    except WebSocketDisconnect:
        clients.discard(socket)


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


@app.get("/login")
async def login_page():
    return FileResponse(WEB / "login.html")


@app.get("/signup")
async def signup_page():
    return FileResponse(WEB / "signup.html")


@app.get("/m/")
async def mobile_page():
    return FileResponse(WEB / "m" / "index.html")


# ---------- 모바일 (R12): 운전자 = 내 차만 · 현장 담당자 = 담당 하역장만 ----------
def me_of(request):
    return getattr(request.state, "user", None) or (app.state.auth_current(request) if hasattr(app.state, "auth_current") else None) or {}


def my_plate(request):
    plate = (me_of(request).get("vehicle") or {}).get("plate")
    if not plate:
        raise HTTPException(403, "차량이 등록된 운전자 계정이 아니에요.")
    return plate


def my_hoppers(request):
    u = me_of(request)
    return ["A", "B", "C", "YARD"] if u.get("role") == "control" else (u.get("hoppers") or [])


@app.get("/api/driver/me")
async def driver_me(request: Request):
    """운전자: 자기 차량(가입한 차량번호)의 지금 상태 · 순번(PC 순번표의 같은 행) · 안전 경고만."""
    return rt.driver_view(my_plate(request))


@app.get("/api/driver/accept")
async def driver_accept(cargo: str = ""):
    return {"cargo": cargo, "bays": rt.accept_bays(cargo) if cargo else [], "cargos": list(rt.rec.pred)}


@app.post("/api/driver/trip")
async def driver_trip(request: Request, body: dict = Body(...)):
    plate = my_plate(request)
    r, err = rt.driver_trip(plate, body.get("cargo"), body.get("load_t"), body.get("order"))
    if err:
        raise HTTPException(409, err)
    return rt.driver_view(plate)


@app.post("/api/driver/ack")
async def driver_ack(request: Request):
    r, err = rt.driver_ack(my_plate(request))
    if err:
        raise HTTPException(409, err)
    return {"result": r}


@app.post("/api/driver/arrive")
async def driver_arrive(request: Request):
    r, err = rt.driver_arrive(my_plate(request))
    if err:
        raise HTTPException(409, err)
    return {"result": r, "message": "도착 확인됨" if r == "ignored" else "도착했어요"}


@app.post("/api/driver/stopped")
async def driver_stopped(request: Request):
    x = rt.driver_stopped(my_plate(request))
    if not x:
        raise HTTPException(409, "지금 내 차에 열린 안전 경고가 없어요.")
    return {"safety_id": x}


@app.get("/api/field/home")
async def field_home(request: Request):
    return rt.field_view(my_hoppers(request))


def need_own(request, **kw):
    if not rt.field_owns(my_hoppers(request), **kw):
        raise HTTPException(403, "담당 하역장이 아니에요.")


@app.post("/api/field/approve")
async def field_approve(request: Request, vehicle_id: str):
    need_own(request, vehicle_id=vehicle_id)
    if not rt.approve(vehicle_id):
        raise HTTPException(409, "순번에 없는 차량이에요.")
    return {"ok": True}


@app.post("/api/field/change")
async def field_change(request: Request, body: dict = Body(...)):
    vid, sid = body.get("vehicle_id"), body.get("sector_id")
    need_own(request, vehicle_id=vid)
    if sid not in my_hoppers(request):
        raise HTTPException(403, "담당 하역장으로만 바꿀 수 있어요.")
    need_sector(sid)
    c, err = rt.change(vid, sid, body.get("reason", "현장 판단"), body.get("memo", ""))
    if err:
        raise HTTPException(409, err)
    return c


@app.post("/api/field/safety/{sid}/{how}")
async def field_safety(request: Request, sid: str, how: str):
    need_own(request, safety_id=sid)
    if how not in ("ack", "dismiss") or not rt.safety_resolve(sid, how):
        raise HTTPException(409, "이미 처리된 안전 경고예요.")
    return {"ok": True}


@app.post("/api/field/alerts/{aid}/ack")
async def field_alert_ack(request: Request, aid: str):
    need_own(request, alert_id=aid)
    if not rt.ack(aid):
        raise HTTPException(404, "알림이 없습니다")
    return {"ok": True}


app.mount("/web", StaticFiles(directory=WEB), name="web")
if MEDIA.is_dir():   # 영상 폴더만 읽기 전용으로 (StaticFiles: Range 요청 지원 · 폴더 밖 경로 거절)
    app.mount("/media", StaticFiles(directory=MEDIA), name="media")


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
    ap = argparse.ArgumentParser(description="하역관제 서버")
    ap.add_argument("--lan", action="store_true", help="같은 와이파이의 다른 기기도 접속하게 연다 (0.0.0.0)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--video", action="store_true", help="영상 모드: 차량 움직임은 /api/ingest 이벤트로만 (비전 결과 재생)")
    ap.add_argument("--check", action="store_true",
                    help="확인용 서버: 이벤트 DB·가정값 파일을 건드리지 않고 메모리에서만 돈다 (켜 둔 공유 서버와 같이 써도 됨)")
    ap.add_argument("--no-auth", action="store_true", help="로그인 없이(지금처럼 관제 담당자로) — 시연용")
    args = ap.parse_args()
    if args.video:
        rt = Runtime(seed=7, video=True)    # 영상 모드: tools/replay_events.py 또는 비전 모듈이 /api/ingest로 이벤트를 보낸다
    elif args.check:
        rt = Runtime(seed=7)          # 핸들러들은 모듈 전역 rt를 쓰므로 여기서 바꿔 끼우면 된다
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # 실행 창·로그에서 한글이 깨지지 않게
    except (AttributeError, ValueError):
        pass
    import os
    auth.install(app, None if args.no_auth else auth.AuthStore(":memory:" if args.check else DATA / "auth.db"),
                 no_auth=args.no_auth, secure_cookie=False, ingest_token=os.environ.get("DOCK_INGEST_TOKEN"),
                 shift_label=rt.rec.settings.get("shift", {}).get("label", "오후 교대"))
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    print(f"이 PC: http://127.0.0.1:{args.port}/" + ("  (로그인 없이 --no-auth)" if args.no_auth else "  (로그인 필요 · 처음이면 /signup에서 관제 담당자로 가입 = 관리자)"))
    if args.lan:
        for ip in lan_ips():
            print(f"팀원 공유 주소: http://{ip}:{args.port}/")
    uvicorn.run(app, host=host, port=args.port)
