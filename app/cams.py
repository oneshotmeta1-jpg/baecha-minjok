"""영상 채널 (R9): data/cams.json + 해커톤/영상/ → 채널마다 영상/시뮬레이션"""


def cam_list(cfg, media, stand_in=None):
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
