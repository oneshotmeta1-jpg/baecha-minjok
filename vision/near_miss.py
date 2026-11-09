"""아차사고(사고 직전 위험 상황) 판정 — R15

영상 탐지 결과(사람 · 트럭 박스)와 구역(ROI)으로 규칙을 보고, 위험한 순간 한 번(에피소드)마다
NEAR_MISS 이벤트를 하나 낸다. 단계가 L3까지 오르면 그 순간 SAFETY_ALERT(하차 중지 요청)도 낸다.

규칙 (data/cams.json "near_miss" — 수치는 모두 가정값):
    ① dump     적재함이 올라간 트럭이 있는 베이의 위험 구역(덤프 반경)에 사람 발끝
    ② reverse  트럭이 베이에 들어온 지 reverse_window_sec 안에 후진 경로 구역에 사람 발끝
    ③ approach · ④ no_entry · ⑤ queue_cross  설정 항목만 있고 꺼져 있음(다음 라운드)
단계: 조건이 이어진 시간(영상 초) 0초 L1 주의 → level_sec.L2 이상 L2 경고 → level_sec.L3 이상 L3 하차 중지.
조건이 gap_sec 넘게 끊기면 그 위험은 끝난 것으로 보고 NEAR_MISS(가장 높았던 단계)를 낸다.
거리: 사람 발끝 ↔ 트럭 박스 가장 가까운 점, 화면 픽셀 × 카메라별 m_per_px(가정). 박스 안이면 0m.

합성 사람(stand_in): 호퍼 영상(CAM-01·03)에는 사람이 나오지 않아, data/stand_in.json의 손으로 둔
사람 궤적을 탐지 결과에 섞는다. 이런 사람으로 생긴 기록은 payload.source = "stand_in"으로 표시한다.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT = {
    "level_sec": {"L2": 1.0, "L3": 2.0},
    "gap_sec": 0.5,
    "reverse_window_sec": 30,
    "m_per_px": 0.02,
    "rules": [
        {"no": 1, "key": "dump", "name": "적재함 상승 중 덤프 반경 안 사람", "enabled": True},
        {"no": 2, "key": "reverse", "name": "베이 진입 직후 후진 경로 안 사람", "enabled": True},
        {"no": 3, "key": "approach", "name": "트럭·사람 기준 거리 이하 접근", "enabled": False},
        {"no": 4, "key": "no_entry", "name": "출입 금지 구역 진입", "enabled": False},
        {"no": 5, "key": "queue_cross", "name": "호출 전 대기선 넘어 호퍼 진입", "enabled": False},
    ],
}
RULE_TEXT = {"dump": "덤프 반경", "reverse": "후진 경로"}
LEVELS = ("L1", "L2", "L3")


def config(cams_cfg=None):
    """cams.json의 near_miss 설정(없으면 기본값)."""
    c = dict(DEFAULT)
    c.update((cams_cfg or {}).get("near_miss") or {})
    return c


def zone_rule(z):
    """위험 구역 → 규칙 키. 이름(rule)에 '후진'이 있으면 후진 경로, 아니면 덤프 반경."""
    return "reverse" if "후진" in str(z.get("rule", "")) else "dump"


def level_of(dur, cfg):
    ls = cfg["level_sec"]
    return "L3" if dur >= ls["L3"] else "L2" if dur >= ls["L2"] else "L1"


def box_dist(pt, b):
    dx = max(b[0] - pt[0], 0, pt[0] - b[2])
    dy = max(b[1] - pt[1], 0, pt[1] - b[3])
    return (dx * dx + dy * dy) ** 0.5


# ---------- 합성 사람 (stand_in) ----------
def load_stand_in(path=None):
    p = Path(path) if path else ROOT / "data" / "stand_in.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"cams": {}}


def stand_in_at(tracks, t):
    """t초의 합성 사람 박스들. 궤적 키프레임 [t, 발끝 x, 발끝 y, 너비, 높이] 사이는 직선 보간."""
    out = []
    for tr in tracks or []:
        k = tr["keys"]
        if not k or t < k[0][0] or t > k[-1][0]:
            continue
        i = max(j for j in range(len(k)) if k[j][0] <= t)
        a, b = k[i], k[min(i + 1, len(k) - 1)]
        r = 0 if b[0] == a[0] else (t - a[0]) / (b[0] - a[0])
        x, y, w, h = (a[n] + (b[n] - a[n]) * r for n in range(1, 5))
        out.append({"track_id": tr["track_id"], "cls": "person", "bbox": [round(x - w / 2, 1), round(y - h, 1), round(x + w / 2, 1), round(y, 1)],
                    "conf": None, "stand_in": True})
    return out


def merge_stand_in(det, tracks):
    """탐지 결과 프레임마다 합성 사람을 덧붙인 사본."""
    if not tracks:
        return det
    return dict(det, frames=[dict(f, objects=list(f["objects"]) + stand_in_at(tracks, f["t"])) for f in det["frames"]])


# ---------- 판정 ----------
class Tracker:
    """사람 × 위험 구역마다 위험이 이어지는 동안을 한 건으로 묶는다."""

    def __init__(self, cfg, emit, m_per_px=None):
        self.cfg = cfg
        self.emit = emit                        # emit(t, type, cam, vid, tid, sid, zone, payload)
        self.mpp = m_per_px or cfg.get("m_per_px", 0.02)
        self.on = {r["key"] for r in cfg["rules"] if r.get("enabled")}
        self.no = {r["key"]: r["no"] for r in cfg["rules"]}
        self.open = {}                          # (cam, 사람 ID, 구역 ID) → 에피소드
        self.seen_frame = set()
        self.n = 0

    def see(self, t, cam, sid, person, b, zones, trucks, px_scale=1.0):
        """사람 한 명(이미 화면 좌표로 맞춘 박스 b). trucks = {(cam, tid): (구역 ID, 상승 여부, 상태 dict)}."""
        if sid is None:
            return
        pt = ((b[0] + b[2]) / 2, b[3])
        for z in zones:
            if z.get("type") != "위험":
                continue
            rule = zone_rule(z)
            if rule not in self.on or not _inside(pt, z["points"]):
                continue
            n = z["id"].rsplit("-", 1)[1]
            bay_id = f"{sid}-BAY-{n}"
            hit = None
            for k, (zid, raised, s) in trucks.items():
                if k[0] != cam or zid != bay_id:
                    continue
                if (rule == "dump" and raised) or (rule == "reverse" and s.get("bay_t") is not None and t - s["bay_t"] <= self.cfg["reverse_window_sec"]):
                    hit = (k, s)
                    break
            if not hit:
                continue
            (k, s) = hit
            key = (cam, person["track_id"], z["id"])
            d = round(box_dist(pt, s.get("box", b)) / px_scale * self.mpp, 1)
            ep = self.open.get(key)
            if not ep:
                self.n += 1
                ep = self.open[key] = {"t0": t, "last": t, "level": "L1", "min_d": d, "snap": t, "rule": rule, "z": z, "n": n, "sid": sid,
                                       "vid": s["vid"], "tid": k[1], "person": person["track_id"], "stand_in": bool(person.get("stand_in")),
                                       "id": f"NM-{cam}-{person['track_id']}-{int(round(t * 10))}", "alerted": False}
            ep["last"] = t
            if d < ep["min_d"]:
                ep["min_d"], ep["snap"] = d, t
            lv = level_of(t - ep["t0"], self.cfg)
            if LEVELS.index(lv) > LEVELS.index(ep["level"]):
                ep["level"] = lv
            if lv == "L3" and not ep["alerted"]:
                ep["alerted"] = True
                self.emit(t, "SAFETY_ALERT", cam, ep["vid"], ep["tid"], sid, "back" if rule == "reverse" else "rear",
                          {"level": "L3", "rule": z.get("rule") or ("후진 경로" if rule == "reverse" else "덤프 반경 5m"), "zone_id": z["id"],
                           "person_track": person["track_id"], "bay": int(n), "dist_m": ep["min_d"], "action": "앱 경고 · 하차 중지 요청",
                           "near_miss_id": ep["id"], "person": "stand_in" if ep["stand_in"] else "video"})

    def end_frame(self, t, cam):
        """이 카메라 프레임 끝: gap_sec 넘게 조건이 끊긴 위험은 닫는다."""
        for key in [k for k, ep in self.open.items() if k[0] == cam and t - ep["last"] > self.cfg["gap_sec"]]:
            self._close(key)

    def close_all(self):
        for key in list(self.open):
            self._close(key)

    def _close(self, key):
        ep = self.open.pop(key)
        cam = key[0]
        dur = round(ep["last"] - ep["t0"], 2)
        self.emit(ep["last"], "NEAR_MISS", cam, ep["vid"], ep["tid"], ep["sid"], "back" if ep["rule"] == "reverse" else "rear",
                  {"id": ep["id"], "rule": RULE_TEXT[ep["rule"]], "rule_no": self.no[ep["rule"]], "level": ep["level"], "duration_s": dur,
                   "min_dist_m": ep["min_d"], "zone_id": ep["z"]["id"], "bay": int(ep["n"]), "person_track": ep["person"],
                   "snapshot_t": round(ep["snap"], 2), "clip": [round(max(0, ep["t0"] - 2), 2), round(ep["last"] + 2, 2)],
                   "source": "stand_in" if ep["stand_in"] else "video", "start_offset_s": round(ep["t0"], 2), "assumed": ["m_per_px", "level_sec"]})


def _inside(pt, poly):
    x, y = pt
    c = False
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-9) + x1:
            c = not c
    return c
