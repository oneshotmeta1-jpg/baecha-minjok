"""시뮬레이션 — 포항제철소 소결공장 원료 반입 (덤프트럭 순환 운행)

덤프트럭 한 대가 하루에 여러 번 왔다 갔다 한다:
  원료야드 상차 → 계근대(입) → 차량 대기장 → (호출) → 하차 호퍼 → 적재함 상승·하차 → 계근대(출) → 공차 복귀 → 재상차 …
영상(YOLO)과 기사 앱이 붙기 전까지, 둘이 낼 이벤트를 docs/events.md 형식 그대로 대신 낸다.
- source "iot": 기사 앱 신호(GATE_IN, BEACON_NEAR, CALL_ACK)
- source "sim": 영상·설비 신호 대체(ZONE_ENTER/EXIT, VEHICLE_STOPPED, CRANE_START/DONE) — payload.stand_in 에 원래 출처
  (소결 호퍼에서는 CRANE_START/DONE = 덤프 하차 시작/완료로 쓴다)
- source "engine": 배정 엔진(CALL, ALERT) / "ui": 화면 버튼(SECTOR_DOWN/UP)
시간 단위는 분. 고정 간격(STEP)으로 진행해 같은 시드면 항상 같은 결과가 나온다.
"""
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from . import engine

STEP = 0.25            # 시뮬레이션 한 걸음(분)
GATE_HOLD = 1.0        # 계근·등록에 걸리는 시간(분)
VEHICLE = "덤프 25.5t"
PLATE_HEAD = ["80", "81", "82", "83", "85", "86", "87", "88"]
PLATE_MID = "사아바자"

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def center(poly):
    return [sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly)]


def bbox(poly):
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def make_fleet(seed: int, fleet_cfg: dict) -> list:
    """시드 고정 덤프트럭 목록. 차마다 맡은 원료가 정해져 있다(가정)."""
    rnd = random.Random(seed)
    cargos = [c for c, n in fleet_cfg["cargo_mix"].items() for _ in range(n)]
    rnd.shuffle(cargos)
    plates, out = set(), []
    for i in range(fleet_cfg["trucks"]):
        while True:
            p = f"{rnd.choice(PLATE_HEAD)}{rnd.choice(PLATE_MID)} {rnd.randint(1000, 9999)}"
            if p not in plates:
                plates.add(p)
                break
        out.append({"plate": p, "cargo": cargos[i % len(cargos)],
                    "first_t": round(rnd.uniform(0, fleet_cfg["first_wave_min"]), 2)})
    return out


def trip(seed: int, truck: dict, n: int, t: float, fc: dict, urgent=None) -> dict:
    """트럭 한 대의 n번째 반입. 같은 시드·차·회차면 항상 같은 값."""
    rnd = random.Random(f"{seed}|{truck['plate']}|{n}")
    return {"id": truck["plate"], "trip": n, "type": VEHICLE, "cargo": truck["cargo"], "arr_t": round(t, 2),
            "due_t": round(t + rnd.uniform(*fc["due_min"]), 1),
            "urgent": rnd.random() < fc["urgent_rate"] if urgent is None else urgent,
            "load_t": round(rnd.uniform(*fc["load_t"]), 1),
            "work_factor": round(rnd.uniform(0.85, 1.45), 3)}


def default_assumptions() -> dict:
    """화면에서 바꿀 수 있는 가정값의 기본값 (settings.json · std_times.json에서)."""
    st, items = load("settings.json"), load("std_times.json")["items"]
    return {"std_times": {i["cargo"]: i["minutes"] for i in items},
            "warn_pct": st["congestion"]["warn_pct"], "crit_pct": st["congestion"]["crit_pct"],
            "target_wait_min": st["wait"]["target_min"], "call_margin_min": st["call"]["margin_min"],
            "delay_factor": st["delay"]["factor"],
            "speed_limit_kmh": st["road"]["speed_limit_kmh"], "slow_factor": st["road"]["slow_factor"],
            "wait_won_per_hour": st["cost"]["wait_won_per_hour"], "idle_won_per_hour": st["cost"]["sector_idle_won_per_hour"]}


def apply_assumptions(settings: dict, std_items: list, a: dict):
    """가정값을 설정에 덮어쓴다 (data/assumptions.json 또는 화면에서 바꾼 값)."""
    if not a:
        return
    for it in std_items:
        if it["cargo"] in a.get("std_times", {}):
            it["minutes"] = a["std_times"][it["cargo"]]
    m = {"warn_pct": ("congestion", "warn_pct"), "crit_pct": ("congestion", "crit_pct"),
         "target_wait_min": ("wait", "target_min"), "call_margin_min": ("call", "margin_min"),
         "delay_factor": ("delay", "factor"), "speed_limit_kmh": ("road", "speed_limit_kmh"),
         "slow_factor": ("road", "slow_factor"), "wait_won_per_hour": ("cost", "wait_won_per_hour"),
         "idle_won_per_hour": ("cost", "sector_idle_won_per_hour")}
    for k, (sec, key) in m.items():
        if k in a:
            settings[sec][key] = a[k]


class Sim:
    def __init__(self, seed: int = 7, on_event=None, mode: str = "recommend", assumptions: dict | None = None):
        self.seed = seed
        self.on_event = on_event
        self.mode = mode                     # "recommend" = 추천 배정, "fifo" = 기준선(도착순·고정 호퍼)
        self.cfg = load("sectors.json")
        self.settings = load("settings.json")
        self.std_items = load("std_times.json")["items"]
        apply_assumptions(self.settings, self.std_items, assumptions)
        self.fc = self.settings["fleet"]
        self.start = datetime.fromisoformat(self.settings["sim"]["start"])
        self.speed = self.settings["sim"]["speed"]
        self.zone = {z["id"]: z for z in self.cfg["zones"]}
        self.sec_cfg = {s["id"]: s for s in self.cfg["sectors"]}
        self.routes = {(r["from"], r["to"]): r for r in self.cfg["roads"]}
        self.mpp = self.cfg["map"]["mpp"]          # 화면 1px = 몇 m (실제 지도 축척)
        self.ore = self.cfg["ore"]
        self.reset()

    # ---------- setup ----------
    def reset(self):
        self.t = 0.0
        self.running = False
        self.events = []
        self.alerts = []            # 최근 80건 (화면용)
        self.alerts_all = []        # 전체 기록 (알림 이력)
        self.overrides = {}         # 담당자가 바꾼 호퍼 {차량: 호퍼}
        self.series = []            # 10분마다 대기 차량 수
        self.busy_min = 0.0         # 하차구가 실제로 쓰인 시간(하차구·분)
        self.cap_min = 0.0          # 가동 중인 하차구 시간(하차구·분)
        self.alert_seq = 0
        self.track_seq = 0
        self.urgent_seq = 0
        self.plan = []
        self.require_approval = False   # True면 현장 담당자 승인(L2) 뒤에만 호출 (자동 승인 스위치 끔)
        self.approved = set()           # 승인된 "차량#회차"
        self.pending = set()            # 호출 시각이 됐지만 승인을 기다리는 "차량#회차"
        self.fleet = make_fleet(self.seed, self.fc)
        self.truck = {f["plate"]: f for f in self.fleet}
        self.pending_arrivals = sorted((trip(self.seed, f, 1, f["first_t"], self.fc) for f in self.fleet),
                                       key=lambda a: (a["arr_t"], a["id"]))
        self.vehicles = {}
        self.yard_slots = {}
        self.done_trips = []          # 끝난 반입 기록 (KPI용)
        self.sectors = {sid: {"id": sid, "up": True, "crit_since": None, "alerted": False, "idle_min": 0.0}
                        for sid in self.sec_cfg}

    def std_of(self, vtype, cargo):
        for it in self.std_items:
            if it["vehicle"] == vtype and it["cargo"] == cargo:
                return it["minutes"]
        same = [it["minutes"] for it in self.std_items if it["cargo"] == cargo]
        return same[0] if same else 10

    def clock(self, t=None):
        return self.start + timedelta(minutes=self.t if t is None else t)

    def hhmm(self, t):
        return self.clock(t).strftime("%H:%M")

    # ---------- events ----------
    def emit(self, type_, source, vehicle=None, sector_id=None, zone=None, cam_id=None, payload=None):
        ev = {
            "ts": self.clock().isoformat(timespec="seconds"),
            "type": type_,
            "source": source,
            "cam_id": cam_id,
            "sector_id": sector_id,
            "zone": zone,
            "track_id": vehicle.get("track_id") if vehicle else None,
            "vehicle_id": vehicle["id"] if vehicle else None,
            "payload": payload or {},
        }
        self.events.append(ev)
        if self.on_event:
            self.on_event(ev)
        return ev

    @staticmethod
    def key(v):
        """반입 한 건을 가리키는 키: 같은 차가 하루 여러 번 오므로 '차량#회차'."""
        return f"{v['id']}#{v['trip']}"

    def alert(self, level, kind, text, action, sector_id=None, vehicle=None, related=None):
        """related: 이 알림의 제안이 영향을 주는 반입("차량#회차") — 알림 패널의 효과(분)를 이 차량들로 계산한다."""
        self.alert_seq += 1
        rel = list(dict.fromkeys(related if related is not None else ([self.key(vehicle)] if vehicle else [])))
        a = {"id": f"AL-{self.alert_seq}", "ts": self.hhmm(self.t), "t": self.t, "level": level, "kind": kind,
             "text": text, "action": action, "sector_id": sector_id, "vehicle_id": vehicle["id"] if vehicle else None,
             "related": rel, "ack": False, "ack_at": None, "ack_sec": None}
        self.alerts_all.append(a)
        self.alerts.insert(0, a)
        del self.alerts[80:]
        self.emit("ALERT", "engine", vehicle=vehicle, sector_id=sector_id,
                  payload={k: a[k] for k in ("id", "level", "kind", "text", "action", "related")})

    def planned_keys_any(self):
        return [self.key(self.vehicles[p["vehicle_id"]]) for p in self.plan if p["sector_id"]]

    def planned_keys(self, sid, n=2):
        """그 호퍼로 순번이 잡힌 대기장 차량 (앞에서 n대)."""
        return [self.key(self.vehicles[p["vehicle_id"]]) for p in self.plan if p["sector_id"] == sid][:n]

    # ---------- geometry ----------
    def yard_xy(self, v):
        used = set(self.yard_slots.values())
        slot = next(i for i in range(500) if i not in used)
        self.yard_slots[v["id"]] = slot
        x0, y0, x1, y1 = bbox(self.zone["YARD"]["poly"])
        cols, rows = 6, 4
        return [x0 + (x1 - x0) * ((slot % cols) + 0.5) / cols, y0 + (y1 - y0) * (((slot // cols) % rows) + 0.5) / rows]

    def queue_xy(self, sid, idx):
        x0, y0, x1, y1 = bbox(self.sec_cfg[sid]["poly"])
        return [x0 + (x1 - x0) * (0.14 + 0.16 * (idx % 2)), y0 + (y1 - y0) * 0.64]

    def bay_xy(self, sid, idx):
        x0, y0, x1, y1 = bbox(self.sec_cfg[sid]["poly"])
        n = self.sec_cfg[sid]["bays"]
        return [x0 + (x1 - x0) * (0.5 + 0.45 * (idx + 0.5) / n), y0 + (y1 - y0) * 0.64]

    # ---------- 거리·이동시간 (제철소 안 제한속도) ----------
    def m_per_min(self):
        """감속까지 반영한 평균 속도(m/분). 30km/h = 500m/분, 감속 계수 1.15면 약 435m/분."""
        r = self.settings["road"]
        return r["speed_limit_kmh"] * 1000 / 60 / max(r["slow_factor"], 1.0)

    def route_m(self, a, b):
        return self.routes[(a, b)]["length_m"]

    def travel(self, a, b):
        """구간 이동시간(분) = 경로 길이 ÷ 제한속도 × 감속 계수."""
        return round(self.route_m(a, b) / self.m_per_min(), 2)

    def start_path(self, v, pts, leg):
        """경로를 따라 이동 시작. 거리는 그 구간의 도로 경로 길이(OSM, m), 시간 = 거리 ÷ (제한속도 ÷ 감속 계수).
        화면 상자 안의 자리(대기장 칸·호퍼 대기 줄) 차이는 기호일 뿐이라 거리에 넣지 않는다."""
        v["path"] = [list(p) for p in pts]
        v["path_t0"] = self.t
        v["path_m"] = self.route_m(*leg)
        v["path_dur"] = max(v["path_m"] / self.m_per_min(), STEP)

    def walk(self, v):
        """경로 위 현재 위치. 끝에 닿으면 True."""
        pts = v["path"]
        f = min((self.t - v["path_t0"]) / v["path_dur"], 1.0)
        segs = [((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 for a, b in zip(pts, pts[1:])]
        total = sum(segs) or 1
        d = f * total
        for (a, b), L in zip(zip(pts, pts[1:]), segs):
            if d <= L and L > 0:
                k = d / L
                v["xy"] = [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k]
                break
            d -= L
        else:
            v["xy"] = list(pts[-1])
        return f >= 1.0

    # ---------- user / demo actions ----------
    def sector_down(self, sid):
        s = self.sectors[sid]
        if not s["up"]:
            return
        s["up"] = False
        name = self.sec_cfg[sid]["name"]
        self.emit("SECTOR_DOWN", "ui", sector_id=sid)
        moved = []
        for v in self.vehicles.values():
            if v["sector"] == sid and v["state"] in ("moving", "queue"):
                # 이동 중·대기 중인 차량은 다시 배정 대상으로 (그 자리에서 다른 호퍼로 출발)
                v.pop("queue_idx", None)
                v.update(state="yard", sector=None, rerouted=True, path=None)
                moved.append(self.key(v))
        self.replan()
        self.alert("crit", "호퍼 이상", f"{name} 정지", f"영향 차량 {len(moved)}대 재배정, 하차 중 차량은 일시 중지",
                   sector_id=sid, related=moved + [k for k in self.planned_keys_any() if k not in moved][:3])

    def sector_up(self, sid):
        s = self.sectors[sid]
        if s["up"]:
            return
        s["up"] = True
        self.emit("SECTOR_UP", "ui", sector_id=sid)
        self.alert("info", "호퍼 이상", f"{self.sec_cfg[sid]['name']} 복구", "순번 재계산", sector_id=sid, related=[])

    def add_urgent(self):
        """긴급 반입: 소결 배합에 바로 필요한 원료(예: 분코크스 부족)를 실은 트럭 한 대."""
        self.urgent_seq += 1
        rnd = random.Random(f"{self.seed}|urgent|{self.urgent_seq}")
        plate = f"{rnd.choice(PLATE_HEAD)}{rnd.choice(PLATE_MID)} {rnd.randint(1000, 9999)}"
        while plate in self.truck:
            plate = plate[:-1] + str((int(plate[-1]) + 1) % 10)
        cargo = rnd.choice(["분코크스", "석회석", "밀스케일"])
        t = {"plate": plate, "cargo": cargo, "first_t": self.t, "extra": True}
        self.truck[plate] = t
        a = trip(self.seed, t, 1, self.t, self.fc, urgent=True)
        a["due_t"] = self.t + 30
        self.pending_arrivals.insert(0, a)
        return plate

    def ack_alert(self, aid, ack_sec=None):
        for a in reversed(self.alerts_all):
            if a["id"] == aid:
                if not a["ack"]:
                    a.update(ack=True, ack_at=self.hhmm(self.t), ack_sec=ack_sec)
                return a
        return None

    def override(self, vid, sid):
        """담당자가 대기장 차량의 호퍼를 바꾼다. 대기장에 있는 차량만."""
        v = self.vehicles.get(vid)
        if not v or v["state"] != "yard" or sid not in self.sec_cfg or v["cargo"] not in self.sec_cfg[sid]["cargo"]:
            return False
        self.overrides[vid] = sid
        self.replan()
        return True

    def sector_inputs(self):
        """엔진에 넘기는 호퍼 상태 (재계산·영향 계산 공용)."""
        t = self.t
        secs = []
        for sid, sc in self.sec_cfg.items():
            working = [v for v in self.vehicles.values() if v["state"] == "working" and v["sector"] == sid]
            pend = []
            for v in self.vehicles.values():
                if v["sector"] != sid:
                    continue
                if v["state"] == "queue":
                    pend.append((0.0, v["std"]))
                elif v["state"] == "moving":
                    pend.append((max(v["path_t0"] + v["path_dur"] - t, 0.0), v["std"]))
            secs.append({"id": sid, "name": sc["name"], "up": self.sectors[sid]["up"], "bays": sc["bays"],
                         "travel_min": self.travel("YARD", sid), "cargo": sc["cargo"],
                         "working_remain": [engine.remain_min(v["work_elapsed"], v["std"]) for v in working],
                         "pending_eta": pend})
        return secs

    def yard_inputs(self):
        out = []
        for v in self.vehicles.values():
            if v["state"] == "yard":
                v["forced"] = self.overrides.get(v["id"])
                out.append(v)
        return out

    def impact(self, vid, sid):
        return engine.change_impact(self.yard_inputs(), self.sector_inputs(), self.t, self.settings, vid, sid)

    # ---------- main loop ----------
    def step(self):
        self.t = round(self.t + STEP, 4)
        t = self.t
        st = self.settings

        # 1) 계근대(입) 도착
        while self.pending_arrivals and self.pending_arrivals[0]["arr_t"] <= t:
            a = self.pending_arrivals.pop(0)
            v = dict(a, state="arrived", sector=None, std=self.std_of(a["type"], a["cargo"]),
                     xy=center(self.zone["GATE"]["poly"]), yard_t=None, track_id=None, path=None,
                     work_elapsed=0.0, flags=set(), rerouted=False, gone=False, call_t=None, wait_min=None)
            self.vehicles[v["id"]] = v
            self.emit("GATE_IN", "iot", vehicle=v, payload={
                "vehicle_type": v["type"], "cargo": v["cargo"], "load_t": v["load_t"], "trip": v["trip"],
                "urgent": v["urgent"], "due": self.clock(v["due_t"]).isoformat(timespec="seconds")})

        for v in self.vehicles.values():
            s_id = v["sector"]
            if v["state"] == "arrived" and t - v["arr_t"] >= GATE_HOLD:
                # 2) 대기장 진입: 비콘(신원) + 대기장 카메라(위치)
                self.track_seq += 1
                v.update(state="yard", yard_t=t, track_id=self.track_seq, xy=self.yard_xy(v))
                self.emit("BEACON_NEAR", "iot", vehicle=v, zone="yard")
                self.emit("ZONE_ENTER", "sim", vehicle=v, zone="yard", cam_id=self.zone["YARD"]["cam_id"],
                          payload={"stand_in": "cam"})
            elif v["state"] == "moving":
                if self.walk(v):
                    q_idx = sum(1 for o in self.vehicles.values() if o["state"] == "queue" and o["sector"] == s_id)
                    v.update(state="queue", queue_idx=q_idx, xy=self.queue_xy(s_id, q_idx))
                    cam = self.sec_cfg[s_id]["cam_id"]
                    self.emit("BEACON_NEAR", "iot", vehicle=v, sector_id=s_id, zone="queue")
                    self.emit("ZONE_ENTER", "sim", vehicle=v, sector_id=s_id, zone="queue", cam_id=cam,
                              payload={"stand_in": "cam"})
            elif v["state"] == "working":
                if self.sectors[s_id]["up"]:
                    v["work_elapsed"] += STEP
                if v["work_elapsed"] > v["std"] * st["delay"]["factor"] and "delay" not in v["flags"]:
                    v["flags"].add("delay")
                    self.alert("warn", "하차 지연",
                               f"{v['id']} {v['cargo']} 하차 {int(v['work_elapsed'])}분 (표준 {v['std']}분의 {st['delay']['factor']}배 초과)",
                               f"{self.sec_cfg[s_id]['name']} 다음 호출 보류, 적재함 들러붙음·호퍼 막힘 확인",
                               sector_id=s_id, vehicle=v, related=self.planned_keys(s_id, 1))
                if v["work_elapsed"] >= v["std"] * v["work_factor"]:
                    cam = self.sec_cfg[s_id]["cam_id"]
                    self.emit("CRANE_DONE", "sim", vehicle=v, sector_id=s_id,
                              payload={"stand_in": "dump", "load_t": v["load_t"]})
                    self.emit("ZONE_EXIT", "sim", vehicle=v, sector_id=s_id, zone="bay", cam_id=cam,
                              payload={"stand_in": "cam"})
                    v.update(state="done", done_t=t)
                    self.start_path(v, [v["xy"]] + self.routes[(s_id, "EXIT")]["points"][1:], (s_id, "EXIT"))
            elif v["state"] == "done" and not v["gone"]:
                if self.walk(v):
                    v["gone"] = True
                    v["xy"] = None
                    self.done_trips.append({"id": v["id"], "trip": v["trip"], "cargo": v["cargo"], "load_t": v["load_t"],
                                            "sector": s_id, "wait_min": v["wait_min"] or 0.0, "done_t": t,
                                            "wait_total": round(v["work_start"] - v["yard_t"], 2),
                                            "unload_min": round(v["work_elapsed"], 2), "std": v["std"],
                                            "delayed": "delay" in v["flags"], "late": v["work_start"] > v["due_t"]})
                    # 공차 복귀 → 원료야드 재상차 → 다음 회차 (근무 시간 안에서만)
                    if not self.truck[v["id"]].get("extra"):
                        # 계근대(출) → 원료야드(공차, 제한속도) → 상차 대기·상차(가정) → 계근대(입)(적재, 제한속도)
                        back = self.travel("EXIT", "ORE")
                        load = random.Random(f"{self.seed}|{v['id']}|cycle|{v['trip']}").uniform(*self.fc["load_min"])
                        inbound = self.travel("ORE", "GATE")
                        nxt = t + back + load + inbound
                        if nxt < self.fc["shift_min"]:
                            a = trip(self.seed, self.truck[v["id"]], v["trip"] + 1, nxt, self.fc)
                            a["ret"] = {"exit_t": t, "ore_t": round(t + back, 2), "dep_t": round(nxt - inbound, 2)}
                            self.pending_arrivals.append(a)
                            self.pending_arrivals.sort(key=lambda x: (x["arr_t"], x["id"]))

        # 3) 호퍼 대기 → 하차 시작 (빈 하차구가 있으면)
        for sid, sc in self.sec_cfg.items():
            if not self.sectors[sid]["up"]:
                continue
            working = [v for v in self.vehicles.values() if v["state"] == "working" and v["sector"] == sid]
            self.busy_min += len(working) * STEP
            self.cap_min += sc["bays"] * STEP
            queue = sorted((v for v in self.vehicles.values() if v["state"] == "queue" and v["sector"] == sid),
                           key=lambda v: v["queue_idx"])
            used = {v["bay"] for v in working}
            if not working and not queue:
                self.sectors[sid]["idle_min"] += STEP
            for v in queue:
                free = [b for b in range(sc["bays"]) if b not in used]
                if not free:
                    break
                b = free[0]
                used.add(b)
                v.pop("queue_idx", None)
                v.update(state="working", bay=b, work_start=t, xy=self.bay_xy(sid, b))
                self.emit("ZONE_ENTER", "sim", vehicle=v, sector_id=sid, zone="bay", cam_id=sc["cam_id"],
                          payload={"stand_in": "cam"})
                self.emit("VEHICLE_STOPPED", "sim", vehicle=v, sector_id=sid, zone="bay", cam_id=sc["cam_id"],
                          payload={"stand_in": "cam"})
                self.emit("CRANE_START", "sim", vehicle=v, sector_id=sid, payload={"stand_in": "dump"})
            rest = sorted((v for v in self.vehicles.values() if v["state"] == "queue" and v["sector"] == sid),
                          key=lambda v: v["queue_idx"])
            for i, v in enumerate(rest):
                v["queue_idx"] = i
                v["xy"] = self.queue_xy(sid, i)

        # 4) 순번 계산과 호출
        self.replan()
        for p in self.plan:
            if p["sector_id"] and p["call_t"] is not None and p["call_t"] <= t + 1e-9:
                v = self.vehicles[p["vehicle_id"]]
                k = self.key(v)
                if self.require_approval and k not in self.approved and v["id"] not in self.overrides:
                    # L2: 호출 시각이 됐어도 현장 담당자가 승인해야 출발 (자동 승인 스위치를 끈 경우)
                    if k not in self.pending:
                        self.pending.add(k)
                        self.emit("CALL_PENDING", "engine", vehicle=v, sector_id=p["sector_id"],
                                  payload={"rank": p["rank"], "reason": p["reason"], "since": self.hhmm(t)})
                    continue
                self.call(v, p)
        self.replan()

        # 10분마다 대기 차량 수 (대기장 + 호출 이동 + 호퍼 대기 = 아직 하차를 시작 못 한 차)
        if abs(t / 10 - round(t / 10)) < 1e-9:
            self.series.append({"t": t, "hhmm": self.hhmm(t),
                                "waiting": sum(v["state"] in ("yard", "moving", "queue") for v in self.vehicles.values())})

        # 5) 알림: 대기 초과, 혼잡 지속
        for v in self.vehicles.values():
            if v["state"] == "yard" and t - v["yard_t"] >= st["wait"]["target_min"] + 1 and "wait" not in v["flags"]:
                v["flags"].add("wait")
                self.alert("warn", "대기 초과", f"{v['id']} {v['cargo']} 대기 {int(t - v['yard_t'])}분 (목표 {st['wait']['target_min']}분 초과)",
                           "우선 배정 또는 공용 호퍼 C로 변경 검토", vehicle=v)
        for sid, s in self.sectors.items():
            pct = self.sector_view(sid)["congestion_pct"]
            if pct >= st["congestion"]["crit_pct"]:
                s["crit_since"] = t if s["crit_since"] is None else s["crit_since"]
                if t - s["crit_since"] >= st["congestion"]["crit_hold_min"] and not s["alerted"]:
                    s["alerted"] = True
                    self.alert("crit", "혼잡", f"{self.sec_cfg[sid]['name']} 혼잡 {pct}% {st['congestion']['crit_hold_min']}분 지속",
                               f"{self.sec_cfg[sid]['name']} 호출 보류, 공용 호퍼로 분산", sector_id=sid,
                               related=self.planned_keys(sid))
            else:
                s["crit_since"], s["alerted"] = None, False

    def call(self, v, p):
        sid = p["sector_id"]
        sc = self.sec_cfg[sid]
        self.yard_slots.pop(v["id"], None)
        q = self.queue_xy(sid, 0)
        road = self.routes[("YARD", sid)]["points"]
        # 정지된 호퍼로 가다 되돌아온 차는 대기장 입구로 돌아간 뒤 새 호퍼 경로를 탄다
        pts = ([v["xy"]] + road[1:-1] + [q]) if not v.get("rerouted") else ([v["xy"], road[1]] + road[1:-1] + [q])
        k = self.key(v)
        how = "changed" if v["id"] in self.overrides else ("approved" if k in self.approved else "auto")
        self.pending.discard(k)
        self.overrides.pop(v["id"], None)
        v.update(state="moving", sector=sid, call_t=self.t, wait_min=round(self.t - v["yard_t"], 1), rerouted=False, approval=how)
        self.start_path(v, pts, ("YARD", sid))
        self.emit("CALL", "engine", vehicle=v, sector_id=sid, payload={
            "rank": p["rank"], "reason": p["reason"], "route_m": v["path_m"], "travel_min": round(v["path_dur"], 1),
            "speed_limit_kmh": self.settings["road"]["speed_limit_kmh"], "approval": how})
        self.emit("CALL_ACK", "iot", vehicle=v, sector_id=sid)     # 기사 '출발' (모바일 화면 전까지는 자동 응답)
        self.alert("info", "호출", f"{v['id']} {v['cargo']} → {sc['name']}", "기사 앱으로 호출 전송(가상)", sector_id=sid, vehicle=v)

    def replan(self):
        secs, yard = self.sector_inputs(), self.yard_inputs()
        if self.mode == "fifo":
            self.plan = engine.plan_fifo(yard, secs, self.t)
        else:
            self.plan = engine.plan(yard, secs, self.t, self.settings)

    def metrics(self):
        """방식 비교용 누적 지표. 대기 = 대기장 진입 → 하차 시작 (아직 못 시작했으면 지금까지)."""
        t = self.t
        waits = [d["wait_total"] for d in self.done_trips]          # 끝난 반입
        waits += [(v["work_start"] if v["state"] in ("working", "done") else t) - v["yard_t"]
                  for v in self.vehicles.values() if not v["gone"] and v["yard_t"] is not None]   # 진행 중
        idle = sum(s["idle_min"] for s in self.sectors.values())
        return {"vehicles": len(waits), "avg_wait": round(sum(waits) / len(waits), 1) if waits else 0.0,
                "max_wait": round(max(waits), 1) if waits else 0.0, "wait_total": round(sum(waits), 1),
                "idle_min": round(idle, 1), "util_pct": round(self.busy_min / self.cap_min * 100) if self.cap_min else 0,
                "late": sum(d["late"] for d in self.done_trips), "trips_done": len(self.done_trips),
                "delays": sum(1 for d in self.done_trips if d["delayed"])}

    def run_until(self, minutes):
        while self.t < minutes:
            self.step()

    # ---------- camera view (what the vision module would report) ----------
    def dump_angle(self, v):
        """적재함 각도(도). 실제 하차 시간 기준: 앞 25% 상승, 중간 유지, 끝 20% 내림. 지연이면 들린 채 유지."""
        real = v["std"] * v["work_factor"]
        f = v["work_elapsed"] / real if real else 1
        if f < 0.25:
            return round(50 * f / 0.25)
        if f < 0.8:
            return 50
        return round(50 * max(1 - f, 0) / 0.2)

    def cam_judge(self, v):
        if v["state"] == "working":
            ang = self.dump_angle(v)
            if ang >= 15:
                return "하차 중 · 적재함 상승", ang
            return ("하차 준비 · 정차" if v["work_elapsed"] < v["std"] * v["work_factor"] / 2 else "하차 마무리 · 적재함 내림"), ang
        if v["state"] == "queue":
            return "하차 대기 · 정지", 0
        if v["state"] == "moving":
            return "진입 중", 0
        if v["state"] == "done":
            return "출차 중", 0
        return "대기장 대기 · 정지", 0

    # ---------- state for screens ----------
    def sector_view(self, sid):
        sc = self.sec_cfg[sid]
        working = [v for v in self.vehicles.values() if v["state"] == "working" and v["sector"] == sid]
        queue = [v for v in self.vehicles.values() if v["state"] == "queue" and v["sector"] == sid]
        moving = [v for v in self.vehicles.values() if v["state"] == "moving" and v["sector"] == sid]
        pct = engine.congestion_pct(len(working), len(queue), sc["bays"], sc["queue_slots"])
        nxt = [p for p in self.plan if p["sector_id"] == sid]
        return {
            "id": sid, "name": sc["name"], "role": sc.get("role", ""), "status": "up" if self.sectors[sid]["up"] else "down",
            "bays": sc["bays"], "queue_slots": sc["queue_slots"], "cargo": sc["cargo"], "cam_id": sc["cam_id"],
            "congestion_pct": pct, "level": engine.level(pct, self.settings),
            "working": [{"vehicle_id": v["id"], "cargo": v["cargo"], "load_t": v["load_t"], "std": v["std"], "bay": v["bay"],
                         "progress_pct": engine.progress_pct(v["work_elapsed"], v["std"]),
                         "elapsed_min": round(v["work_elapsed"], 1),
                         "remain_min": round(engine.remain_min(v["work_elapsed"], v["std"]), 1),
                         "dump_deg": self.dump_angle(v), "track_id": v["track_id"],
                         "judge": self.cam_judge(v)[0],
                         "delayed": "delay" in v["flags"]} for v in sorted(working, key=lambda v: v["bay"])],
            "queue": [v["id"] for v in sorted(queue, key=lambda v: v["queue_idx"])],
            "queue_info": [{"vehicle_id": v["id"], "cargo": v["cargo"], "track_id": v["track_id"],
                            "since_call_min": round(self.t - v["call_t"], 1) if v["call_t"] is not None else 0.0}
                           for v in sorted(queue, key=lambda v: v["queue_idx"])],
            "moving": [v["id"] for v in moving],
            "idle_min": round(self.sectors[sid]["idle_min"], 1),
            "route_m": self.route_m("YARD", sid), "travel_min": round(self.travel("YARD", sid), 1),
            "assigned": len(nxt),
            "done": sum(1 for d in self.done_trips if d["sector"] == sid),
            "avg_unload": round(sum(d["unload_min"] for d in self.done_trips if d["sector"] == sid) /
                                max(1, sum(1 for d in self.done_trips if d["sector"] == sid)), 1),
            "warn_pct": self.settings["congestion"]["warn_pct"], "crit_pct": self.settings["congestion"]["crit_pct"],
            "next_call_at": self.hhmm(nxt[0]["call_t"]) if nxt and nxt[0]["call_t"] is not None else None,
        }

    def routes_view(self):
        """구간별 거리·이동시간. base_min = 제한속도로만 달렸을 때, travel_min = 감속 계수 반영."""
        r = self.settings["road"]
        v_max = r["speed_limit_kmh"] * 1000 / 60
        return [{"from": a, "to": b, "length_m": x["length_m"], "base_min": round(x["length_m"] / v_max, 1),
                 "travel_min": round(self.travel(a, b), 1)} for (a, b), x in self.routes.items()]

    def along(self, pts, f):
        """경로 위 비율 f(0~1) 지점."""
        segs = [((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 for a, b in zip(pts, pts[1:])]
        d = max(0.0, min(f, 1.0)) * (sum(segs) or 1)
        for (a, b), L in zip(zip(pts, pts[1:]), segs):
            if d <= L and L > 0:
                k = d / L
                return [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k]
            d -= L
        return list(pts[-1])

    def ore_xy(self, i):
        x0, y0, x1, y1 = bbox(self.ore["poly"])
        return [x0 + (x1 - x0) * ((i % 6) + 0.5) / 6, y0 + (y1 - y0) * (((i // 6) % 3) + 0.6) / 3.4]

    def road_trucks(self):
        """구내 도로 위 차량 (아직 계근대(입)에 오지 않은 차): 공차 복귀 → 원료야드 상차 → 적재 반입."""
        t, out, k = self.t, [], 0
        back = self.routes[("EXIT", "ORE")]["points"]
        inb = self.routes[("ORE", "GATE")]["points"]
        for a in self.pending_arrivals:
            if a["arr_t"] - t > 120:
                continue
            ret = a.get("ret")
            dep = ret["dep_t"] if ret else a["arr_t"] - self.travel("ORE", "GATE")
            if ret and t < ret["ore_t"]:
                if t < ret["exit_t"]:
                    continue
                phase, xy = "return", self.along(back, (t - ret["exit_t"]) / max(ret["ore_t"] - ret["exit_t"], 1e-6))
            elif t < dep:
                phase, xy = "loading", self.ore_xy(k)
                k += 1
            else:
                phase, xy = "inbound", self.along(inb, (t - dep) / max(a["arr_t"] - dep, 1e-6))
            out.append({"id": a["id"], "cargo": a["cargo"], "trip": a["trip"], "phase": phase,
                        "load_t": a["load_t"] if phase == "inbound" else 0, "xy": [round(c, 1) for c in xy]})
        return out

    def state(self):
        t = self.t
        vs = []
        for v in self.vehicles.values():
            if v["gone"]:
                continue
            p = next((p for p in self.plan if p["vehicle_id"] == v["id"]), None)
            judge, ang = self.cam_judge(v)
            vs.append({"id": v["id"], "type": v["type"], "cargo": v["cargo"], "load_t": v["load_t"], "trip": v["trip"],
                       "state": v["state"], "sector_id": v["sector"] or (p and p["sector_id"]), "rank": p and p["rank"],
                       "wait_min": round(t - v["yard_t"], 1) if v["state"] == "yard" and v["yard_t"] is not None else v.get("wait_min"),
                       "urgent": v["urgent"], "due": self.clock(v["due_t"]).isoformat(timespec="seconds"),
                       "track_id": v["track_id"], "judge": judge, "dump_deg": ang,
                       "xy": [round(c, 1) for c in v["xy"]] if v["xy"] else None})
        yard = [v for v in self.vehicles.values() if v["state"] == "yard"]
        up_bays = sum(sc["bays"] for sid, sc in self.sec_cfg.items() if self.sectors[sid]["up"])
        busy = sum(1 for v in self.vehicles.values() if v["state"] == "working" and self.sectors[v["sector"]]["up"])
        waits = [d["wait_min"] for d in self.done_trips]
        sviews = [self.sector_view(sid) for sid in self.sec_cfg]
        return {
            "clock": self.clock().isoformat(timespec="seconds"),
            "t": t, "running": self.running, "speed": self.speed, "seed": self.seed,
            "mode": "recommend",
            "site": self.cfg.get("site", {}),
            "sectors": sviews,
            "vehicles": vs,
            "queue": [{"rank": p["rank"], "vehicle_id": p["vehicle_id"], "cargo": self.vehicles[p["vehicle_id"]]["cargo"],
                       "load_t": self.vehicles[p["vehicle_id"]]["load_t"],
                       "urgent": self.vehicles[p["vehicle_id"]]["urgent"], "sector_id": p["sector_id"],
                       "call_at": self.hhmm(p["call_t"]) if p["call_t"] is not None else None,
                       "due": self.hhmm(self.vehicles[p["vehicle_id"]]["due_t"]),
                       "wait_min": round(t - self.vehicles[p["vehicle_id"]]["yard_t"], 1),
                       "reason": p["reason"], "start_at": self.hhmm(p["start_t"]) if p.get("start_t") is not None else None,
                       "awaiting": self.key(self.vehicles[p["vehicle_id"]]) in self.pending} for p in self.plan],
            "counts": {"fleet": len(self.fleet), "on_site": sum(not v["gone"] for v in self.vehicles.values()),
                       "yard": len(yard), "moving": sum(v["state"] == "moving" for v in self.vehicles.values()),
                       "trips_done": len(self.done_trips),
                       "tons_done": round(sum(d["load_t"] for d in self.done_trips), 1),
                       "arrived": len(self.vehicles), "exited": len(self.done_trips),
                       "total": len(self.done_trips) + sum(not v["gone"] for v in self.vehicles.values()) + len(self.pending_arrivals)},
            "kpi": {"yard_avg_wait": round(sum(t - v["yard_t"] for v in yard) / len(yard), 1) if yard else 0.0,
                    "done_avg_wait": round(sum(waits) / len(waits), 1) if waits else 0.0,
                    "util_pct": round(busy / up_bays * 100) if up_bays else 0,
                    "levels": {lv: sum(1 for s in sviews if s["status"] == "up" and s["level"] == lv) for lv in ("ok", "warn", "crit")},
                    "up": sum(1 for s in sviews if s["status"] == "up"), "late": sum(d["late"] for d in self.done_trips)},
            "alerts": self.alerts[:40],
            "road": self.road_trucks(),
            "road_cfg": self.settings["road"],
            "routes": self.routes_view(),
        }
