"""구역(ROI) 편집 — R15. 설정 › 구역(ROI)에서 영상 첫 프레임 위에 그린 다각형을 data/cams.json에 저장한다.

- 읽기 통합: GET /api/rois/edit 가 영상 채널 구역(cams.json, 영상 픽셀)과 시뮬레이션 구역(rois.json, 1920×1080)을 함께 준다.
  편집·저장은 영상 채널(cams.json)만. rois.json은 시뮬레이션 그림용이라 이번에는 보기만 한다(합치기는 마이그레이션 위험 · 확인 필요).
- 저장: POST /api/admin/rois/{camera} — 관리자만. 바꾼 사람 · 시각 · 요약을 처리 기록(auth_log, action "roi")에 남긴다.
- 판정 규칙이 구역 ID로 베이를 찾으므로 ID는 정해진 모양만 받는다:
  하차 <호퍼>-BAY-<n> · 위험 <호퍼>-REAR-<n>(덤프 반경) / <호퍼>-BACK-<n>(후진 경로) · 대기 *-WAIT-<n> · 보행 통로 *-WALK-<n> · 출입 금지 *-NOGO-<n>
"""
import json
import re
from pathlib import Path

from fastapi import Body, HTTPException, Request

TYPES = ["하차", "대기", "위험", "보행 통로", "출입 금지"]
RULES = {"덤프 반경 5m": ("REAR", "적재함 상승 중"), "후진 경로": ("BACK", "베이 진입 30초 안")}
PREFIX = {"대기": "WAIT", "보행 통로": "WALK", "출입 금지": "NOGO"}
MAX_ZONES, MAX_POINTS = 12, 24


class RoiError(ValueError):
    pass


def clean(cam, zones):
    """화면에서 보낸 구역 목록을 검사하고 저장할 모양으로 정리한다. 틀리면 RoiError(사람이 읽을 문장)."""
    if not isinstance(zones, list) or not zones:
        raise RoiError("구역이 하나 이상 있어야 해요.")
    if len(zones) > MAX_ZONES:
        raise RoiError(f"구역은 {MAX_ZONES}개까지예요.")
    w, h = cam["size"]
    sid = cam.get("sector_id")
    out, ids = [], set()
    for i, z in enumerate(zones, 1):
        typ, zid, name = z.get("type"), str(z.get("id", "")).strip(), str(z.get("name", "")).strip()[:40]
        where = f"{i}번째 구역({zid or '이름 없음'})"
        if typ not in TYPES:
            raise RoiError(f"{where}: 종류를 골라 주세요.")
        pts = z.get("points")
        if not isinstance(pts, list) or not 3 <= len(pts) <= MAX_POINTS:
            raise RoiError(f"{where}: 점은 3~{MAX_POINTS}개여야 해요.")
        try:
            pts = [[int(round(float(p[0]))), int(round(float(p[1])))] for p in pts]
        except (TypeError, ValueError, IndexError):
            raise RoiError(f"{where}: 점 좌표가 숫자가 아니에요.")
        if any(not (0 <= x <= w and 0 <= y <= h) for x, y in pts):
            raise RoiError(f"{where}: 점이 화면({w}×{h}) 밖에 있어요.")
        if _area(pts) < 400:
            raise RoiError(f"{where}: 다각형이 너무 작거나 한 줄로 겹쳐 있어요.")
        rec = {"id": zid, "type": typ, "name": name or zid}
        if typ in ("하차", "위험"):
            if not sid:
                raise RoiError(f"{where}: 대기장 카메라에는 하차 · 위험 구역을 둘 수 없어요.")
            if typ == "하차" and not re.fullmatch(rf"{sid}-BAY-[1-9]", zid):
                raise RoiError(f"{where}: 하차 구역 ID는 {sid}-BAY-<베이 번호> 모양이어야 해요.")
            if typ == "위험":
                rule = z.get("rule") or "덤프 반경 5m"
                if rule not in RULES:
                    raise RoiError(f"{where}: 위험 규칙은 '덤프 반경 5m' 또는 '후진 경로'예요.")
                if not re.fullmatch(rf"{sid}-{RULES[rule][0]}-[1-9]", zid):
                    raise RoiError(f"{where}: {rule} 구역 ID는 {sid}-{RULES[rule][0]}-<베이 번호> 모양이어야 해요.")
                rec["rule"], rec["active_when"] = rule, RULES[rule][1]
        elif not re.fullmatch(rf"[A-Z0-9]+-{PREFIX[typ]}-[1-9][0-9]?", zid):
            raise RoiError(f"{where}: {typ} 구역 ID는 <카메라·호퍼>-{PREFIX[typ]}-<번호> 모양이어야 해요.")
        if typ in ("하차", "대기"):
            try:
                d = float(z.get("dwell_sec", 3))
            except (TypeError, ValueError):
                d = -1
            if not 0 <= d <= 30:
                raise RoiError(f"{where}: 체류 시간은 0~30초예요.")
            if d != 3:
                rec["dwell_sec"] = d
        if typ in ("보행 통로", "출입 금지"):
            rec["active_when"] = "항상 (판정은 다음 라운드)"
        if zid in ids:
            raise RoiError(f"{where}: 같은 ID가 두 번 있어요.")
        ids.add(zid)
        rec["points"] = pts
        out.append(rec)
    if sid and not any(z["type"] == "하차" for z in out):
        raise RoiError("호퍼 카메라에는 하차 구역이 하나 이상 있어야 해요(트럭 진입·이탈 판정).")
    return out


def _area(pts):
    return abs(sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))) / 2


def summary(before, after):
    """바뀐 내용 한 줄 (처리 기록용)."""
    b, a = {z["id"]: z for z in before}, {z["id"]: z for z in after}
    parts = [f"추가 {k}" for k in a if k not in b] + [f"삭제 {k}" for k in b if k not in a]
    parts += [f"수정 {k}" for k in a if k in b and a[k] != b[k]]
    return ", ".join(parts) or "바뀐 것 없음"


def dumps(cfg):
    """cams.json을 지금처럼 사람이 읽기 좋은 모양(카메라 한 덩어리, 구역 한 줄)으로 쓴다."""
    head = {k: v for k, v in cfg.items() if k != "cams"}
    lines = ["{"]
    for k, v in head.items():
        lines.append(f"  {json.dumps(k, ensure_ascii=False)}: " + json.dumps(v, ensure_ascii=False, indent=2).replace("\n", "\n  ") + ",")
    lines.append('  "cams": [')
    for i, c in enumerate(cfg["cams"]):
        rest = {k: v for k, v in c.items() if k != "zones"}
        body = json.dumps(rest, ensure_ascii=False)[1:-1]
        zs = ",\n".join("       " + json.dumps(z, ensure_ascii=False) for z in c.get("zones", []))
        lines.append("    {" + body + ',\n     "zones": [\n' + zs + "\n     ]}" + ("," if i < len(cfg["cams"]) - 1 else ""))
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def install(app, store, cams_path, rois_path, get_cfg, set_cfg, off=False):
    """구역 편집 API를 붙인다. get_cfg()/set_cfg(cfg)로 서버가 들고 있는 cams.json 설정을 읽고 바꾼다."""
    cams_path, rois_path = Path(cams_path), Path(rois_path)

    def who(request):
        cur = getattr(app.state, "auth_current", None)
        return cur(request) if cur else None

    @app.get("/api/rois/edit")
    async def rois_edit(request: Request):
        u = who(request) or {}
        cfg = get_cfg()
        sim = json.loads(rois_path.read_text(encoding="utf-8")) if rois_path.exists() else {"cams": []}
        hist = [x for x in store.recent_log(60) if x["action"] == "roi"][:10] if store is not None and not off else []
        return {"cams": [{k: c.get(k) for k in ("camera", "sector_id", "bay", "size", "view", "zones", "m_per_px", "video")} for c in cfg["cams"]],
                "sim": sim.get("cams", []), "sim_frame": sim.get("frame"), "types": TYPES, "rules": list(RULES),
                "can_edit": bool(u.get("is_admin")), "no_auth": bool(off), "history": hist}

    @app.post("/api/admin/rois/{camera}")
    async def rois_save(request: Request, camera: str, body: dict = Body(...)):
        u = who(request)
        if not u or u.get("role") != "control" or not u.get("is_admin"):
            raise HTTPException(403, "관리자만 구역을 저장할 수 있어요.")
        cfg = get_cfg()
        cam = next((c for c in cfg["cams"] if c["camera"] == camera), None)
        if not cam:
            raise HTTPException(404, "없는 카메라예요.")
        try:
            zones = clean(cam, body.get("zones"))
        except RoiError as e:
            raise HTTPException(422, str(e))
        before = cam.get("zones", [])
        new = dict(cfg, cams=[dict(c, zones=zones) if c["camera"] == camera else c for c in cfg["cams"]])
        cams_path.write_text(dumps(new), encoding="utf-8")
        set_cfg(new)
        detail = f"{camera} 구역 {len(zones)}개 · {summary(before, zones)}"
        if store is not None and not off:
            store.log_action(u.get("id"), None, "roi", detail)
            store.db.commit()
        return {"message": f"{camera} 구역을 저장했어요. 화면 겹쳐 그리기는 바로, 판정 이벤트는 다음 영상 수집부터 바뀌어요.", "detail": detail,
                "zones": zones}
