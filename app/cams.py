"""영상 채널 (R9): data/cams.json + 해커톤/영상/ → 채널마다 영상/시뮬레이션"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def video_near_miss(cfg, media):
    """시뮬레이션 모드용: 영상 탐지 결과(det.json)에서 실제 사람으로 잡힌 아차사고(NEAR_MISS, source=video).
    off = 영상 시작부터 분(time_scale 반영), cycle = 영상 한 바퀴(분). 합성 사람은 섞지 않는다."""
    sys.path.insert(0, str(ROOT / "vision"))
    from to_events import convert, video_rois   # noqa: E402
    import near_miss   # noqa: E402
    nm_cfg = near_miss.config(cfg)
    scale = cfg.get("time_scale", 1)
    start = "2026-11-09T08:00:00"
    out = []
    for c in cfg["cams"]:
        det = media / (Path(c["video"]).stem + ".det.json")
        if not det.exists():
            continue
        d = json.loads(det.read_text(encoding="utf-8"))
        if not d.get("frames"):
            continue
        dur = max(f["t"] for f in d["frames"])
        for e in convert(d, start, video_rois(cfg, c["camera"]), scale=scale, flush=True, nm_cfg=nm_cfg):
            if e["type"] != "NEAR_MISS" or e["payload"].get("source") != "video":
                continue
            off = (datetime.fromisoformat(e["ts"]) - datetime.fromisoformat(start)).total_seconds() / 60
            out.append({"off": round(off, 3), "cycle": round(dur * scale / 60, 3), "cam_id": c["camera"], "vehicle_id": e["vehicle_id"],
                        "zone": e.get("zone"), "payload": dict(e["payload"], video=c["video"])})
    return out


def cam_list(cfg, media, stand_in=None):
    stand_in = stand_in if (cfg.get("near_miss") or {}).get("stand_in") else None   # 합성 사람 꺼짐(R16 기본) → 화면에도 안 그림
    """카메라 채널마다 영상 파일이 있으면 source=video(영상 URL · 탐지 결과 URL), 없으면 sim(시뮬레이션 그림)."""
    from urllib.parse import quote
    out = []
    for c in cfg["cams"]:
        v = media / c["video"]
        det = v.with_name(v.stem + ".det.json")
        out.append({**{k: c[k] for k in ("camera", "sector_id", "size", "view", "zones") if k in c}, "bay": c.get("bay"), "limit": c.get("limit"),
                    "source": "video" if v.exists() else "sim",
                    "video_url": "/media/" + quote(c["video"]) if v.exists() else None,
                    "det_url": "/media/" + quote(det.name) if det.exists() else None,
                    "m_per_px": c.get("m_per_px"),
                    "stand_in": ((stand_in or {}).get("cams") or {}).get(c["camera"], [])})   # 합성 사람 궤적(R15) — 화면이 같은 규칙으로 보간해 그림
    return out
