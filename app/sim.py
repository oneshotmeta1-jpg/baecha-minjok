"""시뮬레이션 — 가상 도착 데이터, 시뮬레이션 시계, 가상 IoT·영상 이벤트.

영상(YOLO)과 기사 앱이 붙기 전까지, 둘이 낼 이벤트를 docs/events.md 형식 그대로 대신 낸다.
- source "iot": 기사 앱이 낼 신호(GATE_IN, BEACON_NEAR, CALL_ACK)
- source "sim": 영상·설비가 낼 신호(ZONE_ENTER/EXIT, VEHICLE_STOPPED, CRANE_START/DONE) — payload.stand_in 에 원래 출처
- source "engine": 배정 엔진(CALL, ALERT) / "ui": 화면 버튼(SECTOR_DOWN/UP)
시간 단위는 분. 한 걸음(STEP)씩 고정 간격으로 진행해 같은 시드면 항상 같은 결과가 나온다.
"""
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from . import engine

STEP = 0.25            # 시뮬레이션 한 걸음(분)
GATE_HOLD = 1.0        # 게이트 등록에 걸리는 시간(분)
EXIT_MIN = 2.0         # 섹터 → 출구 이동(분)
CARGO_MIX = [("열연코일", 0.35), ("냉연코일", 0.15), ("후판", 0.25), ("선재", 0.25)]

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def center(poly):
    return [sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly)]


def bbox(poly):
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    return min(xs), min(ys), max(xs), max(ys)


def make_arrivals(seed: int, n: int = 30, span_min: int = 150, std_items=None) -> list:
    """시드 고정 도착 데이터. 앞쪽 90분에 몰리게 만들어 대기가 생기도록 한다 (모두 가정값)."""
    rnd = random.Random(seed)
    out, t = [], 0.0
    for i in range(n):
        t += rnd.expovariate(1 / (4.0 if t < 90 else 8.0))
        if t > span_min:
            break
        r, acc, cargo = rnd.random(), 0.0, CARGO_MIX[-1][0]
        for c, p in CARGO_MIX:
            acc += p
            if r <= acc:
                cargo = c
                break
        vtype = "카고" if cargo == "선재" or (cargo == "열연코일" and rnd.random() < 0.5) else "트레일러"
        out.append({
            "id": f"TR-{401 + i:04d}",
            "type": vtype,
            "cargo": cargo,
            "arr_t": round(t, 2),
            "due_t": round(t + rnd.uniform(70, 200), 1),
            "urgent": rnd.random() < 0.08,
            "work_factor": round(rnd.uniform(0.85, 1.4), 3),   # 실제 작업시간 = 표준 x 이 값 (지연 발생용)
        })
    return out


class Sim:
    def __init__(self, seed: int = 7, on_event=None):
        self.seed = seed
        self.on_event = on_event
        self.cfg = load("sectors.json")
        self.settings = load("settings.json")
        self.std_items = load("std_times.json")["items"]
        self.start = datetime.fromisoformat(self.settings["sim"]["start"])
        self.speed = self.settings["sim"]["speed"]
        self.zone = {z["id"]: z for z in self.cfg["zones"]}
        self.sec_cfg = {s["id"]: s for s in self.cfg["sectors"]}
        self.roads = {(r["from"], r["to"]): r["points"] for r in self.cfg["roads"]}
        self.reset()

    # ---------- setup ----------
    def reset(self):
        self.t = 0.0
        self.running = False
        self.events = []
        self.alerts = []
        self.alert_seq = 0
        self.track_seq = 0
        self.urgent_seq = 0
        self.plan = []
        self.arrivals = make_arrivals(self.seed)
        self.pending_arrivals = list(self.arrivals)
        self.vehicles = {}
        self.yard_slots = {}
        self.sectors = {sid: {"id": sid, "up": True, "crit_since": None, "alerted": False}
                        for sid in self.sec_cfg}

    def std_of(self, vtype, cargo):
        for it in self.std_items:
            if it["vehicle"] == vtype and it["cargo"] == cargo:
                return it["minutes"]
        same = [it["minutes"] for it in self.std_items if it["cargo"] == cargo]
        return same[0] if same else 25

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

    def alert(self, level, kind, text, action, sector_id=None, vehicle=None):
        self.alert_seq += 1
        a = {"id": f"AL-{self.alert_seq}", "ts": self.hhmm(self.t), "level": level, "kind": kind,
             "text": text, "action": action, "sector_id": sector_id, "ack": False}
        self.alerts.insert(0, a)
        del self.alerts[80:]
        self.emit("ALERT", "engine", vehicle=vehicle, sector_id=sector_id,
                  payload={k: a[k] for k in ("id", "level", "kind", "text", "action")})

    # ---------- geometry ----------
    def yard_xy(self, v):
        used = set(self.yard_slots.values())
        slot = next(i for i in range(200) if i not in used)
        self.yard_slots[v["id"]] = slot
        x0, y0, x1, y1 = bbox(self.zone["YARD"]["poly"])
        cols = 6
        return [x0 + 30 + (slot % cols) * 60, y0 + 30 + (slot // cols) % 5 * 70]

    def queue_xy(self, sid, idx):
        x0, y0, x1, y1 = bbox(self.sec_cfg[sid]["poly"])
        return [x0 + 40, y0 + 45 + (idx % 3) * 60]

    def bay_xy(self, sid, idx):
        x0, y0, x1, y1 = bbox(self.sec_cfg[sid]["poly"])
        return [x0 + 180 + idx * 110, (y0 + y1) / 2]

    def start_path(self, v, pts, minutes):
        v["path"] = [list(p) for p in pts]
        v["path_t0"] = self.t
        v["path_dur"] = max(minutes, STEP)

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
        moved = 0
        for v in self.vehicles.values():
            if v["sector"] == sid and v["state"] in ("moving", "queue"):
                # 이동 중·대기 중인 차량은 다시 배정 대상으로 돌린다 (그 자리에서 다른 섹터로 출발)
                self.leave_sector_slot(v)
                v.update(state="yard", sector=None, rerouted=True, path=None)
                moved += 1
        self.alert("crit", "섹터 이상", f"{name} 정지", f"영향 차량 {moved}대 재배정, 작업 중 차량은 일시 중지",
                   sector_id=sid)

    def sector_up(self, sid):
        s = self.sectors[sid]
        if s["up"]:
            return
        s["up"] = True
        self.emit("SECTOR_UP", "ui", sector_id=sid)
        self.alert("info", "섹터 이상", f"{self.sec_cfg[sid]['name']} 복구", "순번 재계산", sector_id=sid)

    def add_urgent(self):
        self.urgent_seq += 1
        rnd = random.Random(self.seed * 1000 + self.urgent_seq)
        cargo = rnd.choice(["열연코일", "선재", "냉연코일"])
        a = {"id": f"UR-{self.urgent_seq:03d}", "type": "카고" if cargo == "선재" else "트레일러", "cargo": cargo,
             "arr_t": self.t, "due_t": self.t + 60, "urgent": True, "work_factor": 1.0}
        self.pending_arrivals.insert(0, a)
        return a["id"]

    def ack_alert(self, aid):
        for a in self.alerts:
            if a["id"] == aid:
                a["ack"] = True
                return True
        return False

    # ---------- main loop ----------
    def leave_sector_slot(self, v):
        v.pop("queue_idx", None)

    def step(self):
        self.t = round(self.t + STEP, 4)
        t = self.t
        st = self.settings

        # 1) 게이트 도착 (기사 앱 지오펜스 가정)
        while self.pending_arrivals and self.pending_arrivals[0]["arr_t"] <= t:
            a = self.pending_arrivals.pop(0)
            v = dict(a, state="arrived", sector=None, std=self.std_of(a["type"], a["cargo"]),
                     xy=center(self.zone["GATE"]["poly"]), yard_t=None, track_id=None, path=None,
                     work_elapsed=0.0, flags=set(), rerouted=False, gone=False, call_t=None)
            self.vehicles[v["id"]] = v
            self.emit("GATE_IN", "iot", vehicle=v, payload={
                "vehicle_type": v["type"], "cargo": v["cargo"], "urgent": v["urgent"],
                "due": self.clock(v["due_t"]).isoformat(timespec="seconds")})

        for v in self.vehicles.values():
            s_id = v["sector"]
            if v["state"] == "arrived" and t - v["arr_t"] >= GATE_HOLD:
                # 2) 대기장 진입: 비콘(신원) + 대기장 카메라(위치)
                self.track_seq += 1
                v.update(state="yard", yard_t=t, track_id=self.track_seq, xy=self.yard_xy(v))
                self.emit("BEACON_NEAR", "iot", vehicle=v, zone="yard")
                self.emit("ZONE_ENTER", "sim", vehicle=v, zone="yard", cam_id="CAM-Y",
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
                if (v["work_elapsed"] > v["std"] * st["delay"]["factor"] and "delay" not in v["flags"]):
                    v["flags"].add("delay")
                    self.alert("warn", "작업 지연",
                               f"{v['id']} 작업 {int(v['work_elapsed'])}분 (표준 {v['std']}분의 {st['delay']['factor']}배 초과)",
                               f"{self.sec_cfg[s_id]['name']} 현장 확인", sector_id=s_id, vehicle=v)
                if v["work_elapsed"] >= v["std"] * v["work_factor"]:
                    cam = self.sec_cfg[s_id]["cam_id"]
                    self.emit("CRANE_DONE", "sim", vehicle=v, sector_id=s_id, payload={"stand_in": "crane"})
                    self.emit("ZONE_EXIT", "sim", vehicle=v, sector_id=s_id, zone="bay", cam_id=cam,
                              payload={"stand_in": "cam"})
                    road = self.roads.get((s_id, "EXIT"), [])
                    v.update(state="done", done_t=t)
                    self.start_path(v, [v["xy"]] + road + [center(self.zone["EXIT"]["poly"])], EXIT_MIN)
            elif v["state"] == "done" and not v["gone"]:
                if self.walk(v):
                    v["gone"] = True
                    v["xy"] = None

        # 3) 섹터 대기 → 작업 시작 (빈 베이가 있으면)
        for sid, sc in self.sec_cfg.items():
            if not self.sectors[sid]["up"]:
                continue
            working = [v for v in self.vehicles.values() if v["state"] == "working" and v["sector"] == sid]
            queue = sorted((v for v in self.vehicles.values() if v["state"] == "queue" and v["sector"] == sid),
                           key=lambda v: v["queue_idx"])
            used = {v["bay"] for v in working}
            for v in queue:
                free = [b for b in range(sc["bays"]) if b not in used]
                if not free:
                    break
                b = free[0]
                used.add(b)
                self.leave_sector_slot(v)
                v.update(state="working", bay=b, work_start=t, xy=self.bay_xy(sid, b))
                self.emit("ZONE_ENTER", "sim", vehicle=v, sector_id=sid, zone="bay", cam_id=sc["cam_id"],
                          payload={"stand_in": "cam"})
                self.emit("VEHICLE_STOPPED", "sim", vehicle=v, sector_id=sid, zone="bay", cam_id=sc["cam_id"],
                          payload={"stand_in": "cam"})
                self.emit("CRANE_START", "sim", vehicle=v, sector_id=sid, payload={"stand_in": "crane"})
            rest = sorted((v for v in self.vehicles.values() if v["state"] == "queue" and v["sector"] == sid),
                          key=lambda v: v["queue_idx"])
            for i, v in enumerate(rest):
                v["queue_idx"] = i
                v["xy"] = self.queue_xy(sid, i)

        # 4) 순번 계산과 호출
        self.replan()
        for p in self.plan:
            if p["sector_id"] and p["call_t"] is not None and p["call_t"] <= t + 1e-9:
                self.call(self.vehicles[p["vehicle_id"]], p)
        self.replan()

        # 5) 알림: 대기 초과, 혼잡 지속
        for v in self.vehicles.values():
            if v["state"] == "yard" and t - v["yard_t"] >= st["wait"]["target_min"] + 1 and "wait" not in v["flags"]:
                v["flags"].add("wait")
                self.alert("warn", "대기 초과", f"{v['id']} 대기 {int(t - v['yard_t'])}분 (목표 {st['wait']['target_min']}분 초과)",
                           "우선 배정 또는 섹터 변경 검토", vehicle=v)
        for sid, s in self.sectors.items():
            pct = self.sector_view(sid)["congestion_pct"]
            if pct >= st["congestion"]["crit_pct"]:
                s["crit_since"] = t if s["crit_since"] is None else s["crit_since"]
                if t - s["crit_since"] >= st["congestion"]["crit_hold_min"] and not s["alerted"]:
                    s["alerted"] = True
                    self.alert("crit", "혼잡", f"{self.sec_cfg[sid]['name']} 혼잡 {pct}% {st['congestion']['crit_hold_min']}분 지속",
                               f"{self.sec_cfg[sid]['name']} 호출 보류, 다른 섹터로 분산", sector_id=sid)
            else:
                s["crit_since"], s["alerted"] = None, False

    def call(self, v, p):
        sid = p["sector_id"]
        sc = self.sec_cfg[sid]
        self.yard_slots.pop(v["id"], None)
        if v.get("rerouted"):
            q = self.queue_xy(sid, 0)
            pts = [v["xy"], [840, v["xy"][1]], [840, q[1]], q]
        else:
            pts = [v["xy"]] + self.roads.get(("YARD", sid), []) + [self.queue_xy(sid, 0)]
        v.update(state="moving", sector=sid, call_t=self.t, wait_min=round(self.t - v["yard_t"], 1), rerouted=False)
        self.emit("CALL", "engine", vehicle=v, sector_id=sid, payload={"rank": p["rank"], "reason": p["reason"]})
        self.emit("CALL_ACK", "iot", vehicle=v, sector_id=sid)     # 기사 '출발' (모바일 화면 전까지는 자동 응답)
        self.start_path(v, pts, sc["travel_min"])
        self.alert("info", "호출", f"{v['id']} → {sc['name']}로 이동", "기사 앱으로 호출 전송(가상)", sector_id=sid, vehicle=v)

    def replan(self):
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
                         "travel_min": sc["travel_min"], "cargo": sc["cargo"],
                         "working_remain": [engine.remain_min(v["work_elapsed"], v["std"]) for v in working],
                         "pending_eta": pend})
        yard = [v for v in self.vehicles.values() if v["state"] == "yard"]
        self.plan = engine.plan(yard, secs, t, self.settings)

    def run_until(self, minutes):
        while self.t < minutes:
            self.step()

    # ---------- state for screens ----------
    def sector_view(self, sid):
        sc = self.sec_cfg[sid]
        working = [v for v in self.vehicles.values() if v["state"] == "working" and v["sector"] == sid]
        queue = [v for v in self.vehicles.values() if v["state"] == "queue" and v["sector"] == sid]
        moving = [v for v in self.vehicles.values() if v["state"] == "moving" and v["sector"] == sid]
        pct = engine.congestion_pct(len(working), len(queue), sc["bays"], sc["queue_slots"])
        nxt = [p for p in self.plan if p["sector_id"] == sid]
        return {
            "id": sid, "name": sc["name"], "status": "up" if self.sectors[sid]["up"] else "down",
            "bays": sc["bays"], "queue_slots": sc["queue_slots"], "cargo": sc["cargo"],
            "congestion_pct": pct, "level": engine.level(pct, self.settings),
            "working": [{"vehicle_id": v["id"], "cargo": v["cargo"], "std": v["std"],
                         "progress_pct": engine.progress_pct(v["work_elapsed"], v["std"]),
                         "elapsed_min": round(v["work_elapsed"], 1),
                         "remain_min": round(engine.remain_min(v["work_elapsed"], v["std"]), 1),
                         "delayed": "delay" in v["flags"]} for v in sorted(working, key=lambda v: v["bay"])],
            "queue": [v["id"] for v in sorted(queue, key=lambda v: v["queue_idx"])],
            "moving": [v["id"] for v in moving],
            "next_call_at": self.hhmm(nxt[0]["call_t"]) if nxt else None,
        }

    def state(self):
        t = self.t
        vs = []
        for v in self.vehicles.values():
            if v["gone"]:
                continue
            p = next((p for p in self.plan if p["vehicle_id"] == v["id"]), None)
            vs.append({"id": v["id"], "type": v["type"], "cargo": v["cargo"], "state": v["state"],
                       "sector_id": v["sector"] or (p and p["sector_id"]), "rank": p and p["rank"],
                       "wait_min": round(t - v["yard_t"], 1) if v["state"] == "yard" and v["yard_t"] is not None else v.get("wait_min"),
                       "urgent": v["urgent"], "due": self.clock(v["due_t"]).isoformat(timespec="seconds"),
                       "xy": [round(c, 1) for c in v["xy"]] if v["xy"] else None})
        done = [v for v in self.vehicles.values() if v["state"] == "done"]
        return {
            "clock": self.clock().isoformat(timespec="seconds"),
            "t": t, "running": self.running, "speed": self.speed, "seed": self.seed,
            "mode": "recommend",
            "sectors": [self.sector_view(sid) for sid in self.sec_cfg],
            "vehicles": vs,
            "queue": [{"rank": p["rank"], "vehicle_id": p["vehicle_id"], "cargo": self.vehicles[p["vehicle_id"]]["cargo"],
                       "urgent": self.vehicles[p["vehicle_id"]]["urgent"], "sector_id": p["sector_id"],
                       "call_at": self.hhmm(p["call_t"]) if p["call_t"] is not None else None,
                       "wait_min": round(t - self.vehicles[p["vehicle_id"]]["yard_t"], 1),
                       "reason": p["reason"]} for p in self.plan],
            "counts": {"total": len(self.arrivals) + self.urgent_seq, "arrived": len(self.vehicles),
                       "yard": sum(v["state"] == "yard" for v in self.vehicles.values()),
                       "done": len(done), "exited": sum(v["gone"] for v in self.vehicles.values())},
            "kpi": None,   # FIFO 기준선 비교는 다음 라운드
            "alerts": self.alerts[:40],
        }
