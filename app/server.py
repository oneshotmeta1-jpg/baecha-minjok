"""상하역 지연 관제 - FastAPI 서버 (v0 뼈대)

v0: 관제 화면(web/)과 설정 JSON만 내보낸다.
다음 라운드에서 붙일 것: 시뮬레이션 시계(sim.py), 배정 엔진(engine.py), 알림(alerts.py),
/api/state, /ws(WebSocket), /cam/{id}.mjpg, /m/driver, /m/manager.

실행: python app/server.py  →  http://127.0.0.1:8000/
"""
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WEB = ROOT / "web"
VERSION = "v0"

app = FastAPI(title="상하역 지연 관제", version=VERSION)


def load(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


@app.get("/api/health")
def health():
    return {"ok": True, "version": VERSION}


@app.get("/api/config")
def config():
    # 화면은 이 값으로 배치도·섹터 카드·CAM 자리를 그린다
    return JSONResponse({
        "version": VERSION,
        "sectors": load("sectors.json"),
        "std_times": load("std_times.json"),
        "settings": load("settings.json"),
    })


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


app.mount("/web", StaticFiles(directory=WEB), name="web")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
