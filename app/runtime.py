"""운영 런타임 — 추천 배정 시뮬레이션과 FIFO 기준선 시뮬레이션을 같은 시계로 함께 돌린다.

- 같은 시드·같은 도착 데이터·같은 돌발 이벤트(호퍼 정지·긴급 반입)를 두 방식에 똑같이 넣고, 지표를 나란히 비교한다.
- 담당자 행동(순번 승인·변경·알림 확인·가정값 변경)을 기록하고 SQLite에 남긴다.
- 가정값을 바꾸면 처음부터 지금 시각까지 같은 행동 기록으로 다시 돌려(재계산) 결과를 바꾼다. 같은 입력이면 항상 같은 결과.
FastAPI 없이도 쓸 수 있게 분리해 tests/test_sim.py에서 직접 검사한다.
"""
import copy
import csv
import io
import json
import re
import sqlite3
import time
from datetime import timedelta
from pathlib import Path

from datetime import datetime

from .sim import Sim, STEP, default_assumptions, trip

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

REASONS = ["기사·차량 사정", "현장 우선순위", "영상 판정 오류", "호퍼·설비 사정", "긴급 반입 끼어듦", "기타"]
REPEAT_SUGGEST = 3        # 같은 호퍼·같은 사유가 이만큼 쌓이면 가정값 조정 제안


class Store:
    """SQLite: 이벤트 로그, 순번 변경 사유, 알림 이력."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts TEXT, type TEXT, source TEXT, vehicle_id TEXT, sector_id TEXT, body TEXT);
        CREATE TABLE IF NOT EXISTS changes (id INTEGER PRIMARY KEY, ts TEXT, vehicle_id TEXT, from_sector TEXT, to_sector TEXT,
            reason TEXT, memo TEXT, delta_min REAL, cost_won INTEGER);
        CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, ts TEXT, level TEXT, kind TEXT, text TEXT, action TEXT,
            sector_id TEXT, vehicle_id TEXT, ack INTEGER, ack_at TEXT, ack_sec REAL);
        """)

    def clear(self):
        self.db.executescript("DELETE FROM events; DELETE FROM changes; DELETE FROM alerts;")
        self.db.commit()

    def event(self, ev):
        self.db.execute("INSERT INTO events (ts,type,source,vehicle_id,sector_id,body) VALUES (?,?,?,?,?,?)",
                        (ev["ts"], ev["type"], ev["source"], ev["vehicle_id"], ev["sector_id"], json.dumps(ev, ensure_ascii=False)))

    def alert(self, a):
        self.db.execute("INSERT OR REPLACE INTO alerts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (a["id"], a["ts"], a["level"], a["kind"], a["text"], a["action"], a["sector_id"], a["vehicle_id"],
                         int(a["ack"]), a["ack_at"], a["ack_sec"]))

    def change(self, c):
        self.db.execute("INSERT INTO changes (ts,vehicle_id,from_sector,to_sector,reason,memo,delta_min,cost_won) VALUES (?,?,?,?,?,?,?,?)",
                        (c["ts"], c["vehicle_id"], c["from"], c["to"], c["reason"], c["memo"], c["delta_min"], c["cost_won"]))

    def recent_events(self, limit):
        rows = self.db.execute("SELECT body FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def commit(self):
        self.db.commit()


class Runtime:
    def __init__(self, seed=7, db_path=None, assumptions_path=None, clock=time.monotonic, video=False):
        self.seed = seed
        self.video = video          # True: 영상 모드 — 실제 움직임은 POST /api/ingest 이벤트로만
        self.clock = clock
        self.store = Store(db_path or ":memory:")
        self.assumptions_path = Path(assumptions_path) if assumptions_path else None
        self.assumptions = default_assumptions()
        self.history = []                       # 가정값 변경 이력
        if self.assumptions_path and self.assumptions_path.exists():
            saved = json.loads(self.assumptions_path.read_text(encoding="utf-8"))
            self.history = saved.pop("history", [])
            self.assumptions.update(saved)
        self.speed = None
        self.running = False
        self.reset()

    # ---------- 만들기 / 다시 돌리기 ----------
    def _new_sims(self):
        self.expected = {}       # 알림 id → 제안할 때 계산한 예상 효과
        self.confirmed = set()   # 효과가 확정된 반입 (EFFECT_CONFIRMED를 이미 낸 것)
        self.fifo = None
        self.rec = Sim(self.seed, on_event=self._on_event, mode="recommend", assumptions=self.assumptions, driven=self.video)
        self.fifo = Sim(self.seed, mode="fifo", assumptions=self.assumptions)
        if self.video:                      # FIFO 기준선 = 같은 도착을 기존 방식으로 돌린 가상 결과 (도착은 GATE_IN에서 받음)
            self.fifo.pending_arrivals = []
            self.fifo.truck = {}
            self.ingested = []
        if self.speed:
            self.rec.speed = self.speed

    def reset(self):
        self.actions = []        # 시뮬레이션에 영향을 주는 행동 {t, kind, ...} — 재계산 때 다시 넣는다
        self.driver_stops = {}   # 안전 경고 id → 운전자 [멈췄어요] 시각
        self.unload_checks = {}  # "차량#회차" → 운전자 하역 완료 확인 {answer: ok|late|not_yet, actual_min, detected_min, ...} (재계산 때 유지)
        self.approved = set()    # "차량#회차"
        self.changes = []        # 순번 변경 기록
        self.ignored = set()     # 무시한 제안
        self.acks = {}           # 알림 id → {ack_sec}
        self.ui_events = []      # 담당자 행동 이벤트 — 재계산 때 같은 시각에 다시 기록
        self.alert_wall = {}     # 알림 id → 발생 실제 시각
        self.auto_approve = True # 자동 승인 스위치 (끄면 현장 담당자가 승인해야 호출 — L2)
        self.ignored_alerts = set()   # 담당자가 [무시]한 알림
        self.requested = {}           # 현장 담당자에게 승인 요청을 보낸 반입 → 요청 시각
        self.safety_wall = {}         # 안전 경고 id → 발생 실제 시각 (확인까지 걸린 초)
        self.safety_acks = {}         # 안전 경고 id → {how, ack_sec} (재계산 때 유지)
        self.week_cache = None
        self.store.clear()
        self._new_sims()

    def _on_event(self, ev):
        self.store.event(ev)
        if ev["type"] == "SAFETY_ALERT":
            self.safety_wall.setdefault(ev["payload"]["id"], self.clock())
        if ev["type"] == "ALERT":
            a = ev["payload"]["id"]
            self.alert_wall.setdefault(a, self.clock())
            rel = ev["payload"].get("related") or []
            if rel and self.fifo is not None:                 # 제안할 때의 예상 효과를 남겨 둔다
                eff = self.effects()
                es = [eff[k] for k in rel if k in eff]
                if es:
                    m = round(sum(e["effect"] for e in es), 1)
                    self.expected[a] = {"min": m, "won": int(round(m * self.won_per_min())), "n": len(es)}
            for x in reversed(self.rec.alerts_all):
                if x["id"] == a:
                    if a in self.acks:              # 재계산 뒤에도 확인 표시 유지
                        x.update(ack=True, ack_at=self.acks[a]["ack_at"], ack_sec=self.acks[a]["ack_sec"])
                    self.store.alert(x)
                    break

    def _ui(self, type_, vehicle_id=None, sector_id=None, payload=None):
        """담당자 행동 이벤트 기록 (ORDER_APPROVED · ORDER_CHANGED · ALERT_ACK · ASSUMPTIONS_UPDATED · DRIVER_STOPPED)."""
        u = {"t": self.rec.t, "type": type_, "vehicle_id": vehicle_id, "sector_id": sector_id, "payload": payload or {}}
        self.ui_events.append(u)
        self._emit_ui(u)

    def _emit_ui(self, u):
        v = self.rec.vehicles.get(u["vehicle_id"]) if u["vehicle_id"] else None
        self.rec.emit(u["type"], "ui", vehicle=v or ({"id": u["vehicle_id"]} if u["vehicle_id"] else None),
                      sector_id=u["sector_id"], payload=u["payload"])

    def _apply(self, a):
        """기록된 행동 하나를 두 시뮬레이션에 넣는다 (변경은 추천 쪽에만)."""
        k = a["kind"]
        if k == "down":
            self.rec.sector_down(a["sector_id"]); self.fifo.sector_down(a["sector_id"])
        elif k == "up":
            self.rec.sector_up(a["sector_id"]); self.fifo.sector_up(a["sector_id"])
        elif k == "urgent":
            self.rec.add_urgent(); self.fifo.add_urgent()
        elif k == "override":
            self.rec.override(a["vehicle_id"], a["sector_id"])
        elif k == "approve":
            self.rec.approved.add(a["key"])
        elif k == "auto":
            self.rec.require_approval = not a["on"]
        elif k == "safety_force":
            self.rec.force_safety(a["sector_id"]); self.fifo.force_safety(a["sector_id"])
        elif k == "safety":
            self.rec.safety_resolve(a["id"], a["how"], self.safety_acks.get(a["id"], {}).get("ack_sec"))
        elif k == "safety_seen":
            self.rec.safety_seen(a["id"])
        elif k == "ingest":
            self._ingest_one(a["ev"])
        self.rec.replan(); self.fifo.replan()

    def step(self):
        self.rec.step()
        self.fifo.step()
        self._confirm_effects()

    def _confirm_effects(self):
        """두 방식 모두 하차를 시작한 반입의 효과를 확정하고 EFFECT_CONFIRMED를 낸다."""
        started_f = {f"{d['id']}#{d['trip']}" for d in self.fifo.done_trips}
        started_f |= {self.fifo.key(v) for v in self.fifo.vehicles.values() if v["state"] in ("working", "done") and not v["gone"]}
        new = []
        for v in self.rec.vehicles.values():
            if v["state"] in ("working", "done") and not v["gone"]:
                k = self.rec.key(v)
                if k not in self.confirmed and k in started_f:
                    new.append(k)
        for d in self.rec.done_trips:
            k = f"{d['id']}#{d['trip']}"
            if k not in self.confirmed and k in started_f:
                new.append(k)
        if not new:
            return
        eff = self.effects()
        for k in dict.fromkeys(new):
            e = eff.get(k)
            if not e or not e["confirmed"]:
                continue
            self.confirmed.add(k)
            vid, trip = k.split("#")
            v = self.rec.vehicles.get(vid)
            self.rec.emit("EFFECT_CONFIRMED", "engine", vehicle={"id": vid, "track_id": v.get("track_id") if v else None},
                          sector_id=v["sector"] if v else None,
                          payload={"trip": int(trip), "rec_wait_min": e["rec"], "fifo_wait_min": e["fifo"], "effect_min": e["effect"],
                                   "won": int(round(e["effect"] * self.won_per_min())), "source": self.source_of(k)})

    def advance(self, minutes):
        target = self.rec.t + minutes
        while self.rec.t + 1e-9 < target:
            self.step()

    def rebuild(self):
        """가정값이 바뀌었을 때: 처음부터 지금 시각까지 같은 행동 기록으로 다시 돌린다."""
        now, running = self.rec.t, self.running
        self.store.clear()
        self._new_sims()
        pending = sorted(self.actions, key=lambda a: a["t"])
        uis = sorted(self.ui_events, key=lambda u: u["t"])
        i = j = 0
        while True:
            while i < len(pending) and pending[i]["t"] <= self.rec.t + 1e-9:
                self._apply(pending[i]); i += 1
            while j < len(uis) and uis[j]["t"] <= self.rec.t + 1e-9:
                self._emit_ui(uis[j]); j += 1
            if self.rec.t + 1e-9 >= now:
                break
            self.step()
        for c in self.changes:
            self.store.change(c)
        self.running = running
        self.store.commit()

    def clone(self, assumptions=None):
        """같은 행동 기록으로 지금 시각까지 다시 돌린 복사본 (바꿔보기 미리보기용 — 저장·로그를 건드리지 않음)."""
        tmp = Runtime(self.seed)
        tmp.assumptions = copy.deepcopy(assumptions if assumptions is not None else self.assumptions)
        tmp.actions = copy.deepcopy(self.actions)
        tmp.ui_events = copy.deepcopy(self.ui_events)
        tmp.approved = set(self.approved)
        tmp.changes = copy.deepcopy(self.changes)
        tmp.acks = dict(self.acks)
        tmp.auto_approve = self.auto_approve
        tmp.ignored_alerts = set(self.ignored_alerts)
        tmp.rec.t = self.rec.t          # rebuild()는 rec.t까지 돌린다
        tmp.rebuild()
        return tmp

    # ---------- 영상 모드: 이벤트 받기 (워크플로우 ② "모든 이벤트는 한 형식으로") ----------
    def ev_minutes(self, ev):
        try:
            return (datetime.fromisoformat(ev["ts"]) - self.rec.start).total_seconds() / 60
        except (KeyError, ValueError, TypeError):
            return self.rec.t

    def ingest(self, events):
        """영상·현장 신호 이벤트를 받는다. 이벤트 시각까지 시계를 돌린 뒤 상태에 반영한다.
        FIFO 기준선에는 GATE_IN을 같은 시각의 도착으로 넣는다(가상 결과)."""
        if isinstance(events, dict):
            events = [events]
        out = []
        seen_ids = self.__dict__.setdefault("ingested_ids", set())
        for ev in sorted(events, key=lambda e: e.get("ts", "")):
            if ev.get("event_id") and ev["event_id"] in seen_ids:     # 같은 영상 이벤트를 두 번 넣지 않음(영상 한 번 = 반입 한 번)
                out.append("duplicate")
                continue
            m = self.ev_minutes(ev)
            while self.rec.t + 1e-9 < m:
                self.step()
            ev = {"source": "cam", "vehicle_id": None, "sector_id": None, "zone": None, "payload": {}, **ev,
                  "ts": ev.get("ts") or self.rec.clock().isoformat(timespec="seconds")}
            r = self._ingest_one(ev)
            if r in ("ok", "ignored") and ev.get("event_id"):
                seen_ids.add(ev["event_id"])
            if r == "ok":
                self.actions.append({"kind": "ingest", "t": self.rec.t, "ev": ev})   # 재계산(가정값 변경) 때도 같은 시각에 다시 넣는다
            out.append(r)
        self.store.commit()
        return out

    def _ingest_one(self, ev):
        """이벤트 한 건을 추천 시뮬레이션에 넣고, GATE_IN · 하역장 정지/복구는 FIFO 기준선에도 같은 시각으로 넣는다."""
        r = self.rec.ingest(ev)
        if r != "ok":
            return r
        self.store.event(ev)
        if getattr(self, "ingested", None) is not None:
            self.ingested.append(ev)
        if ev["type"] == "GATE_IN" and ev.get("vehicle_id"):
            v = self.rec.vehicles[ev["vehicle_id"]]
            a = {k: v[k] for k in ("id", "trip", "type", "cargo", "arr_t", "due_t", "urgent", "load_t")}
            a["work_factor"] = trip(self.seed, {"plate": v["id"], "cargo": v["cargo"]}, v["trip"], v["arr_t"], self.fifo.fc)["work_factor"]
            self.fifo.truck.setdefault(v["id"], {"plate": v["id"], "cargo": v["cargo"], "first_t": v["arr_t"], "extra": True})
            self.fifo.pending_arrivals.append(a)
            self.fifo.pending_arrivals.sort(key=lambda x: (x["arr_t"], x["id"]))
        elif ev["type"] in ("YARD_DOWN", "YARD_UP") and ev.get("sector_id") in self.fifo.sec_cfg:
            (self.fifo.sector_down if ev["type"] == "YARD_DOWN" else self.fifo.sector_up)(ev["sector_id"])
        return r

    # ---------- 모바일 (R12): 운전자 = 내 차만 · 현장 담당자 = 담당 하역장만 ----------
    def app_event(self, typ, vid, sector_id=None, payload=None):
        """기사 앱 · 현장 앱 신호(source app)를 지금 시각 이벤트로 넣는다."""
        ev = {"ts": self.rec.clock().isoformat(timespec="seconds"), "type": typ, "source": "app", "cam_id": None,
              "sector_id": sector_id, "zone": None, "track_id": None, "vehicle_id": vid, "payload": payload or {}}
        return self.ingest([ev])[0]

    def driver_view(self, plate, st=None):
        st = st or self.state()
        v = self.rec.vehicles.get(plate)
        trips = [d for d in self.rec.done_trips if d["id"] == plate]
        out = {"plate": plate, "clock": st["clock"], "trips_today": len(trips), "vehicle": None, "queue": None, "safety": None,
               "accept": [], "steps": [], "last_trip": trips[-1] if trips else None, "sim_truck": plate in self.rec.truck and not self.rec.truck[plate].get("extra"),
               "unload_check": self.unload_check_view(plate)}
        if not v or v["gone"]:
            return out
        sv = next((x for x in st["vehicles"] if x["id"] == plate), None)
        names = {x["id"]: x["name"] for x in st["sectors"]}
        work = next((w for x in st["sectors"] for w in x["working"] if w["vehicle_id"] == plate), None)
        out["vehicle"] = {"state": v["state"], "cargo": v["cargo"], "load_t": v["load_t"], "trip": v["trip"], "app": bool(v.get("app")),
                          "sector_id": v["sector"], "sector": names.get(v["sector"]), "bay": (v["bay"] + 1) if v.get("bay") is not None else None,
                          "eta_min": sv.get("eta_min") if sv else None, "acked": v.get("acked"), "order": v.get("order"),
                          "progress_pct": work["progress_pct"] if work else None, "remain_min": work["remain_min"] if work else None,
                          "dump_deg": work["dump_deg"] if work else None, "travel_min": None,
                          "await": ({"detected_min": round(v.get("detected_min") or 0, 1), "since": self.rec.hhmm(v["await_t"]),
                                     "not_yet": self.rec.hhmm(v["not_yet_t"]) if v.get("not_yet_t") is not None else None,
                                     "wait_left": round(max(0, (5.0 if v.get("not_yet_t") is None else 20.0) - (self.rec.t - (v["await_t"] if v.get("not_yet_t") is None else v["not_yet_t"]))), 1)}
                                    if v["state"] == "working" and v.get("await_t") is not None and not v.get("unload_ok") else None)}
        if v["sector"]:
            out["vehicle"]["travel_min"] = next((x["travel_min"] for x in st["sectors"] if x["id"] == v["sector"]), None)
            z = self.zone_view(v["sector"], st)
            if z:
                out["zone"] = {"id": z["id"], "name": z["name"], "my_bay": (v["bay"] + 1) if v.get("bay") is not None else None,
                               "bays": z["bay_list"]}
        q = next((r for r in st["queue"] if r["vehicle_id"] == plate), None)
        if q:          # PC 순번표의 같은 행 그대로 (새 숫자를 만들지 않음)
            out["queue"] = {"rank": q["rank"], "ahead": q["rank"] - 1, "call_at": q["call_at"], "wait_min": q["wait_min"], "status": q["status"],
                            "sector_id": q["sector_id"], "sector": names.get(q["sector_id"]), "awaiting": q.get("awaiting"), "reason": q["reason"]}
        x = next((x for x in self.rec.safety if x["status"] == "open" and x["vehicle_id"] == plate), None)
        if x:
            out["safety"] = {"id": x["id"], "rule": x.get("rule_text"), "sector": names.get(x["sector_id"]), "bay": (v["bay"] + 1) if v.get("bay") is not None else None,
                             "dist_m": x.get("dist_m"), "radius_m": x.get("radius_m", 5), "ts": x.get("ts"), "stopped": self.driver_stops.get(x["id"])}
        hm = self.rec.hhmm
        out["steps"] = [{"k": "도착 · 게이트 통과", "t": hm(v["arr_t"])},
                        {"k": "대기", "t": hm(v["yard_t"]) if v.get("yard_t") is not None else None},
                        {"k": "호출 · 이동", "t": hm(v["call_t"]) if v.get("call_t") is not None and v["state"] != "yard" else None},
                        {"k": "진입 · 하역중", "t": hm(v["work_start"]) if v.get("work_start") is not None and v["state"] in ("working", "done") else None},
                        {"k": "이탈", "t": hm(v["done_t"]) if v.get("done_t") is not None and v["state"] == "done" else None}]
        return out

    # ---------- 운전자 하역 완료 확인 → 하차 예상 시간 추천 ----------
    def _last_done(self, plate):
        ds = [d for d in self.rec.done_trips if d["id"] == plate]
        return ds[-1] if ds else None

    def unload_check_view(self, plate):
        """기사 앱: 시스템이 하역 완료로 본 마지막 운행 — 운전자가 아직 확인 안 했으면(또는 '아직 안 끝났어요') 확인 카드"""
        d = self._last_done(plate)
        if not d:
            return None
        key = f"{d['id']}#{d['trip']}"
        c = self.unload_checks.get(key)
        if (c and c["answer"] in ("ok", "late", "skip")) or d.get("unload_ok") == "timeout":
            return None
        hm = self.rec.hhmm
        return {"key": key, "cargo": d["cargo"], "load_t": d["load_t"], "sector": self.rec.sec_cfg.get(d["sector"], {}).get("name", d["sector"]),
                "bay": (d["bay"] + 1) if d.get("bay") is not None else None, "start": hm(d["work_start"]), "end": hm(d["work_start"] + d["unload_min"]),
                "detected_min": round(d["unload_min"], 1), "pred_min": round(d["pred"], 1), "answer": c["answer"] if c else None,
                "not_yet_at": c.get("at") if c else None, "elapsed_min": round(self.rec.t - d["work_start"], 1),
                "can_late": self.rec.t - (d["work_start"] + d["unload_min"]) <= self.UNLOAD_LATE_WIN}

    UNLOAD_LATE_WIN = 20     # 감지한 완료 뒤 이 분 안에서만 [아직 안 끝났어요] · [지금 끝났어요] (오래 지나 누르면 실제 시간이 아님)

    def driver_unload(self, plate, answer, demo=False, actual=None):
        v = self.rec.vehicles.get(plate)
        if v and not v["gone"] and v["state"] == "working" and v.get("await_t") is not None and not v.get("unload_ok"):
            return self._unload_live(v, answer)
        return self._unload_after(plate, answer, demo, actual)

    def _unload_live(self, v, answer):
        """하역 중(100%)에 기다리는 확인: 시뮬레이션 이벤트로 넣어 다시 계산해도 같다 → [맞아요]/[지금 끝났어요]면 그때 이탈"""
        if answer not in ("ok", "not_yet", "done_now"):
            return None, "알 수 없는 답이에요."
        if answer == "done_now" and v.get("not_yet_t") is None:
            return None, "먼저 [아직 안 끝났어요]를 누른 뒤 다 내리면 [지금 끝났어요]를 눌러요."
        if answer == "not_yet" and v.get("not_yet_t") is not None:
            return None, "이미 [아직 안 끝났어요]를 눌렀어요. 다 내리면 [지금 끝났어요]를 눌러 주세요."
        r = self.app_event("UNLOAD_CONFIRMED", v["id"], sector_id=v["sector"], payload={"answer": answer, "detected_min": v.get("detected_min")})
        if r != "ok":
            return None, "지금은 확인할 수 없어요."
        key = f"{v['id']}#{v['trip']}"
        base = {"key": key, "plate": v["id"], "cargo": v["cargo"], "sector": v["sector"], "detected_min": round(v.get("detected_min") or 0, 1),
                "pred_min": round(v["std"], 1), "t": self.rec.t, "at": self.rec.hhmm(self.rec.t), "demo": False}
        if answer == "not_yet":
            rec = dict(base, answer="not_yet", actual_min=None)
        else:
            rec = dict(base, answer="ok" if answer == "ok" else "late",
                       actual_min=round(v.get("detected_min") if answer == "ok" else self.rec.t - v["work_start"], 1),
                       not_yet_at=(self.unload_checks.get(key) or {}).get("at"))
        self.unload_checks[key] = rec
        return rec, None

    def _unload_after(self, plate, answer, demo=False, actual=None):
        """[맞아요, 끝났어요] ok = 감지한 하차 시간이 맞음 · [아직 안 끝났어요] not_yet · 그 뒤 [지금 끝났어요] done_now = 지금까지가 실제 하차 시간"""
        d = self._last_done(plate)
        if not d:
            return None, "확인할 하역이 없어요."
        key = f"{d['id']}#{d['trip']}"
        c = self.unload_checks.get(key)
        if c and c["answer"] in ("ok", "late", "skip"):
            return None, "이미 확인했어요."
        if answer not in ("ok", "not_yet", "done_now", "skip"):
            return None, "알 수 없는 답이에요."
        if answer in ("not_yet", "done_now") and self.rec.t - (d["work_start"] + d["unload_min"]) > self.UNLOAD_LATE_WIN:
            return None, f"완료로 감지된 지 {self.UNLOAD_LATE_WIN}분이 넘어서 실제 끝난 시각을 받을 수 없어요. [맞아요] 또는 [건너뛰기]를 눌러 주세요."
        if answer == "skip":           # 기억 안 남 — 통계에 넣지 않고 카드만 닫음
            self.unload_checks[key] = {"key": key, "plate": plate, "cargo": d["cargo"], "answer": "skip", "actual_min": None, "demo": demo,
                                       "detected_min": round(d["unload_min"], 1), "t": self.rec.t, "at": self.rec.hhmm(self.rec.t)}
            return self.unload_checks[key], None
        t, base = self.rec.t, {"key": key, "plate": plate, "cargo": d["cargo"], "sector": d["sector"], "detected_min": round(d["unload_min"], 1),
                               "pred_min": round(d["pred"], 1), "t": self.rec.t, "at": self.rec.hhmm(self.rec.t), "demo": demo}
        if answer == "not_yet":
            rec = dict(base, answer="not_yet", actual_min=None)
        else:
            act = d["unload_min"] if answer == "ok" else (actual if actual is not None else t - d["work_start"])
            rec = dict(base, answer="ok" if answer == "ok" else "late", actual_min=round(max(act, d["unload_min"] if answer != "ok" else 0), 1),
                       not_yet_at=c.get("at") if c else None)
        self.unload_checks[key] = rec
        self._ui("UNLOAD_CONFIRMED", plate, d["sector"], {"key": key, "answer": rec["answer"], "detected_min": rec["detected_min"],
                                                          "actual_min": rec["actual_min"], "demo": demo})
        return rec, None

    def demo_unload_checks(self):
        """시연: 끝난 운행 중 아직 확인 안 된 것에 운전자 확인을 채운다(시드 고정). 70%는 감지 시간이 맞고,
        30%는 '아직 안 끝났어요' 뒤 1~4분 더 걸림(시스템이 일찍 완료로 본 경우). 기록에 demo=true."""
        import random
        n = 0
        for d in self.rec.done_trips:
            key = f"{d['id']}#{d['trip']}"
            if key in self.unload_checks and self.unload_checks[key]["answer"] in ("ok", "late", "skip"):   # 운전자가 답한 것(건너뛰기 포함)은 덮지 않음
                continue
            rnd = random.Random(f"{self.seed}|uc|{key}")
            late = rnd.random() < 0.3
            act = d["unload_min"] + (rnd.uniform(1, 4) if late else 0)
            rec = {"key": key, "plate": d["id"], "cargo": d["cargo"], "sector": d["sector"], "detected_min": round(d["unload_min"], 1),
                   "pred_min": round(d["pred"], 1), "t": self.rec.t, "at": self.rec.hhmm(self.rec.t), "demo": True,
                   "answer": "late" if late else "ok", "actual_min": round(act, 1), "not_yet_at": None}
            self.unload_checks[key] = rec
            n += 1
        return n

    def unload_stats(self):
        """설정 › 예측값: 원료별 운전자 확인 하차 시간(중앙값) vs 지금 예측 · 목표 → 추천(3건 이상 · 1분 이상 차이)"""
        out = []
        for cargo in self.assumptions["std_times"]:
            cs = [c for c in self.unload_checks.values() if c["cargo"] == cargo and c["answer"] in ("ok", "late")]
            pred, target = round(self.rec.pred_of(cargo), 1), self.assumptions["std_times"][cargo]
            if not cs:
                out.append({"cargo": cargo, "n": 0, "pred": pred, "target": target})
                continue
            xs = sorted(c["actual_min"] for c in cs)
            med = xs[len(xs) // 2] if len(xs) % 2 else round((xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2, 1)
            det = round(sum(c["detected_min"] for c in cs) / len(cs), 1)
            late = sum(c["answer"] == "late" for c in cs)
            reco = round(med) if len(cs) >= 3 and abs(med - pred) >= 1 else None
            out.append({"cargo": cargo, "n": len(cs), "median": round(med, 1), "min": xs[0], "max": xs[-1], "detected": det, "late": late,
                        "demo": sum(c["demo"] for c in cs), "pred": pred, "target": target, "reco": reco,
                        "text": (f"운전자 확인 {len(cs)}건 실제 중앙값 {med:g}분 · 지금 예측 {pred:g}분 → {reco}분으로" if reco is not None else None)})
        allc = [c for c in self.unload_checks.values() if c["answer"] in ("ok", "late")]
        return {"by_cargo": out, "n": len(allc), "late": sum(c["answer"] == "late" for c in allc), "demo": sum(c["demo"] for c in allc),
                "pending": sum(c["answer"] == "not_yet" for c in self.unload_checks.values()), "min_n": 3}

    # ---------- 모바일 3D 매핑: 구역(호퍼) 하나의 베이 · 차량 상태 (PC 3D 매핑 카드와 같은 규칙) ----------
    def zone_view(self, sid, st):
        """web/map3d.js에 넘길 items · bays와, 기사 앱 구역 맵용 베이별 상태. 상태 규칙은 관제 화면 m3Data와 같다."""
        sec = next((x for x in st["sectors"] if x["id"] == sid), None)
        if not sec:
            return None
        so, veh = st.get("safety_open", []), {v["id"]: v for v in st["vehicles"]}
        bx = lambda n: 1.5 + (n - 1) * 9                     # map3d.js 베이 위치 식
        items, bays, by_bay = [], {}, {}
        for w in sec["working"]:
            n = w["bay"] + 1
            sa = next((x for x in so if x["vehicle_id"] == w["vehicle_id"]), None)
            rev = bool(sa and sa.get("rule") == "reverse")
            state = ("reverse" if rev else "danger") if sa else ("delay" if w["delayed"] else ("unload" if w["dump_deg"] >= 15 else "enter"))
            it = {"id": f"#{w['track_id']}", "bay": n, "state": state, "plate": w["vehicle_id"], "ratio": w.get("delay_x"),
                  "detail": (f"{sa.get('ts')} 감지 · 하차 중지 요청(가상)" if sa else f"경과 {int(w['elapsed_min'])}분 / 예측 {round(w['std'])}분")}
            if sa:
                it.update(ack=bool(sa.get("seen")), worker=[bx(n) + 4.2, 16.2] if rev else [bx(n) + 6.4, 9.6])
                bays[n] = "stop"
            items.append(it)
            by_bay[n] = state
        mv = sorted([veh[i] for i in sec["moving"] if i in veh], key=lambda v: v.get("eta_min") or 0)
        if mv:
            items.append({"id": f"#{mv[0]['track_id']}", "state": "call", "plate": mv[0]["id"],
                          "detail": f"{mv[0].get('eta_min') or 0}분 뒤 도착 · {(mv[0].get('bay') or 0) + 1}번 베이" + (f" · 외 {len(mv) - 1}대" if len(mv) > 1 else "")})
        for n in range(1, sec["bays"] + 1):
            if n not in by_bay:
                bays[n] = "reserved" if any((v.get("bay") or 0) + 1 == n for v in mv) else "empty"
        bc = self.rec.sec_cfg[sid].get("bay_cargo") or []
        return {"id": sid, "name": sec["name"], "status": sec["status"], "items": items, "bays": bays,
                "bay_list": [{"n": n, "cargo": bc[n - 1] if n - 1 < len(bc) else None,
                              "state": "stop" if bays.get(n) == "stop" else by_bay.get(n) or bays.get(n)} for n in range(1, sec["bays"] + 1)],
                "queue": len(sec["queue"]), "moving": len(mv)}

    def accept_bays(self, cargo):
        """원료 → 받을 수 있는 하역장 · 베이 (sectors.json bay_cargo)"""
        return [{"sector_id": sid, "name": c["name"], "bay": i + 1}
                for sid, c in self.rec.sec_cfg.items() for i, bc in enumerate(c.get("bay_cargo") or []) if bc == cargo]

    def driver_trip(self, plate, cargo, load_t, order=None):
        v = self.rec.vehicles.get(plate)
        if v and not v["gone"]:
            return None, "이미 진행 중인 운행이 있어요(하역이 끝나면 다음 운행을 등록해요)."
        if cargo not in self.rec.pred:
            return None, "원료를 골라 주세요."
        try:
            load_t = float(load_t)
        except (TypeError, ValueError):
            return None, "중량을 숫자로 넣어 주세요."
        if not 1 <= load_t <= 30:
            return None, "중량은 1~30t 사이로 넣어 주세요."
        r = self.app_event("GATE_IN", plate, payload={"cargo": cargo, "load_t": load_t, "order": order or None, "vehicle_type": "덤프 25.5t"})
        return (r, None) if r == "ok" else (None, "등록하지 못했어요(" + r + ").")

    def driver_ack(self, plate):
        v = self.rec.vehicles.get(plate)
        if not v or v["state"] != "moving":
            return None, "호출을 받은 뒤에 출발할 수 있어요."
        return self.app_event("CALL_ACK", plate, sector_id=v["sector"]), None

    def driver_arrive(self, plate):
        v = self.rec.vehicles.get(plate)
        if not v or v["state"] not in ("moving", "queue", "working"):
            return None, "호출받은 하역장으로 가는 중일 때 도착을 알릴 수 있어요."
        if v["state"] == "moving" and v.get("app") and not v.get("acked"):
            return None, "먼저 [이동 시작]을 눌러 주세요."
        if v.get("bay") is None:
            return None, "배정된 베이가 아직 없어요."
        return self.app_event("BAY_ENTER", plate, sector_id=v["sector"], payload={"bay": v["bay"] + 1, "cargo": v["cargo"], "manual": True}), None

    def driver_stopped(self, plate):
        x = next((x for x in self.rec.safety if x["status"] == "open" and x["vehicle_id"] == plate), None)
        if not x:
            return None
        self.driver_stops[x["id"]] = self.rec.hhmm(self.rec.t)
        self._ui("DRIVER_STOPPED", plate, x["sector_id"], {"id": x["id"]})
        return x["id"]

    def field_view(self, hoppers, st=None, p2=None):
        st, p2 = st or self.state(), p2 or self.panel2()
        mine = set(hoppers or [])
        secs = [x for x in st["sectors"] if x["id"] in mine]
        return {"clock": st["clock"], "hoppers": sorted(mine),
                "sectors": [{k: x[k] for k in ("id", "name", "status", "level", "congestion_pct", "cargo", "bays")} | {"working": len(x["working"]), "queue": len(x["queue"])} for x in secs],
                "yard": st["counts"]["yard"] if "YARD" in mine else None,
                "safety": [dict(x, sector_name=self.rec.sec_cfg[x["sector_id"]]["name"], driver_stopped=self.driver_stops.get(x["id"]))
                           for x in p2["safety_open"] if x.get("sector_id") in mine],
                "safety_done": [x for x in p2.get("safety_done", []) if x.get("sector_id") in mine][:5],
                "approvals": [dict(x, cargo=x.get("cargo") or (self.rec.vehicles.get(x["vehicle_id"]) or {}).get("cargo"),
                                   accept=sorted({b["sector_id"] for b in self.accept_bays(x.get("cargo") or (self.rec.vehicles.get(x["vehicle_id"]) or {}).get("cargo"))}))
                              for x in p2["approvals"] if x.get("sector_id") in mine],     # accept = 이 원료를 받는 호퍼(하역장 바꾸기 후보)
                "checks": [g for g in p2["checks"] if g.get("sector_id") in mine],
                "auto_approve": p2["auto_approve"], "effect_total": st.get("effect_total"), "reasons": REASONS,
                "zones": [z for z in (self.zone_view(x["id"], st) for x in secs) if z]}

    def field_owns(self, hoppers, vehicle_id=None, safety_id=None, alert_id=None):
        """현장 담당자가 이 차 · 안전 경고 · 알림을 처리해도 되는지(담당 하역장 기준)"""
        mine = set(hoppers or [])
        if vehicle_id is not None:
            p = next((p for p in self.rec.plan if p["vehicle_id"] == vehicle_id), None)
            v = self.rec.vehicles.get(vehicle_id)
            return bool((p and p["sector_id"] in mine) or (v and v["sector"] in mine))
        if safety_id is not None:
            x = next((x for x in self.rec.safety if x["id"] == safety_id), None)
            return bool(x and x["sector_id"] in mine)
        if alert_id is not None:
            a = next((a for a in self.rec.alerts_all if a["id"] == alert_id), None)
            return bool(a and a.get("sector_id") in mine)
        return False

    # ---------- 시연 버튼 ----------
    def _record(self, kind, **kw):
        a = dict(kw, kind=kind, t=self.rec.t)
        self.actions.append(a)
        self._apply(a)
        return a

    def sector_down(self, sid):
        self._record("down", sector_id=sid)

    def sector_up(self, sid):
        self._record("up", sector_id=sid)

    def add_urgent(self):
        before = set(self.rec.truck)
        self._record("urgent")
        return next(iter(set(self.rec.truck) - before), None)

    # ---------- 순번 승인 · 변경 ----------
    def _key(self, vid):
        v = self.rec.vehicles.get(vid)
        return f"{vid}#{v['trip']}" if v else vid

    def approve(self, vid):
        p = next((p for p in self.rec.plan if p["vehicle_id"] == vid), None)
        if not p:
            return False
        key = self._key(vid)
        self.approved.add(key)
        self._record("approve", key=key)          # 승인이 호출을 풀어 주므로 재계산 때도 같은 시각에 다시 넣는다
        self._ui("ORDER_APPROVED", vid, p["sector_id"], {"rank": p["rank"], "reason": p["reason"]})
        return True

    # ---------- 안전 경고 ----------
    def force_safety(self, sid):
        n0 = len(self.rec.safety)
        self._record("safety_force", sector_id=sid)
        return self.rec.safety[-1] if len(self.rec.safety) > n0 else None

    def safety_seen(self, sid_):
        """[관제 확인]: 깜빡임 멈춤 · 빨간색 고정. 하차 중지와 후속 호출 보류는 유지(해제 = safety_resolve).
        상태(safety_open[].seen)가 실시간으로 모든 화면에 퍼진다 — 어느 화면에서 눌러도 세 곳이 함께 바뀐다."""
        x = next((x for x in self.rec.safety if x["id"] == sid_), None)
        if not x or x["status"] != "open":
            return None
        if x.get("seen"):
            return x
        self._record("safety_seen", id=sid_)
        wall = self.safety_wall.get(sid_)
        self._ui("SAFETY_SEEN", x["vehicle_id"], x["sector_id"],
                 {"id": sid_, "seen_sec": round(self.clock() - wall, 1) if wall is not None else None})
        return x

    def safety_resolve(self, sid_, how):
        """[확인] 또는 [오탐지]. 하차 중지가 풀리고, 확인까지 걸린 실제 초를 남긴다."""
        x = next((x for x in self.rec.safety if x["id"] == sid_), None)
        if not x or x["status"] != "open" or how not in ("ack", "dismiss"):
            return None
        wall = self.safety_wall.get(sid_)
        self.safety_acks[sid_] = {"how": how, "ack_sec": round(self.clock() - wall, 1) if wall is not None else None}
        self._record("safety", id=sid_, how=how)
        self._ui("SAFETY_ACK", x["vehicle_id"], x["sector_id"],
                 {"id": sid_, "how": "확인" if how == "ack" else "오탐지", "ack_sec": self.safety_acks[sid_]["ack_sec"], "repeat": x["repeat"]})
        return x

    def request_approval(self):
        """[현장 담당자에게 승인 요청]: 아직 승인 안 된 순번을 현장 담당자 앱으로 보낸다(가상)."""
        keys = []
        for p in self.rec.plan:
            if not p["sector_id"] or p["vehicle_id"] in self.rec.overrides:
                continue
            k = self._key(p["vehicle_id"])
            if k not in self.approved and k not in self.requested:
                self.requested[k] = self.rec.hhmm(self.rec.t)
                keys.append(k)
        if keys:
            self._ui("CALL_REQUEST", None, None, {"n": len(keys), "vehicles": keys, "to": "현장 담당자 앱(가상 푸시)"})
        return keys

    def set_auto_approve(self, on: bool):
        """자동 승인 스위치. 끄면 호출 시각이 돼도 현장 담당자 승인 전에는 출발하지 않는다(L2)."""
        on = bool(on)
        if on == self.auto_approve:
            return on
        self.auto_approve = on
        self._record("auto", on=on)
        self._ui("APPROVAL_MODE", None, None, {"auto": on})
        return on

    def approve_all(self):
        n = 0
        for p in list(self.rec.plan):
            if p["sector_id"] and p["vehicle_id"] not in self.rec.overrides and self._key(p["vehicle_id"]) not in self.approved:
                n += self.approve(p["vehicle_id"])
        return n

    def impact(self, vid, sid):
        return self.rec.impact(vid, sid)

    def change(self, vid, sid, reason, memo="", by="관제", by_name=None):
        v = self.rec.vehicles.get(vid)
        if not v or v["state"] != "yard":
            return None, "대기장에 있는 차량만 바꿀 수 있습니다"
        if reason not in REASONS:
            return None, "변경 사유를 골라 주세요"
        if reason == "기타" and not (memo or "").strip():
            return None, "'기타'는 메모에 이유를 적어 주세요"
        p = next((p for p in self.rec.plan if p["vehicle_id"] == vid), None)
        imp = self.rec.impact(vid, sid)
        if not self.rec.override(vid, sid):
            return None, "이 원료를 받을 수 없는 호퍼입니다"
        self.actions.append({"kind": "override", "t": self.rec.t, "vehicle_id": vid, "sector_id": sid})
        c = {"ts": self.rec.hhmm(self.rec.t), "t": self.rec.t, "vehicle_id": vid, "trip": v["trip"], "cargo": v["cargo"],
             "from": p["sector_id"] if p else None, "to": sid, "reason": reason, "memo": memo.strip()[:200],
             "delta_min": imp["delta_min"], "cost_won": imp["cost_won"], "by": by, "by_name": by_name}
        self.changes.append(c)
        self.store.change(c)
        self.approved.discard(self._key(vid))
        self._ui("ORDER_CHANGED", vid, sid, {"from": c["from"], "to": sid, "reason": reason, "memo": c["memo"],
                                             "delta_min": imp["delta_min"], "cost_won": imp["cost_won"], "by": by})
        return c, None

    def order_status(self, vid):
        v = self.rec.vehicles.get(vid)
        if not v:
            return "—"
        if v["state"] != "yard":
            return "이동 중"
        if vid in self.rec.overrides:
            return "변경됨"
        if self._key(vid) in self.approved:
            return "승인됨"
        if self._key(vid) in self.rec.pending:
            return "승인 대기 · 호출 시각 지남"
        return "현장 승인 대기 · 요청 " + self.requested[self._key(vid)] if self._key(vid) in self.requested else "승인 대기"

    # ---------- 알림 ----------
    def ack(self, aid):
        wall = self.alert_wall.get(aid)
        sec = round(self.clock() - wall, 1) if wall is not None else None
        a = self.rec.ack_alert(aid, sec)
        if not a:
            return None
        self.acks[aid] = {"ack_at": a["ack_at"], "ack_sec": a["ack_sec"]}
        self.store.alert(a)
        self._ui("ALERT_ACK", None, a["sector_id"], {"alert_id": aid, "ack_sec": a["ack_sec"]})
        return a

    def ignore_alert(self, aid):
        a = next((x for x in self.rec.alerts_all if x["id"] == aid), None)
        if not a or aid in self.ignored_alerts:
            return a
        self.ignored_alerts.add(aid)
        self._ui("ALERT_IGNORED", None, a["sector_id"], {"alert_id": aid, "kind": a["kind"]})
        return a

    def alert_list(self, level=None, kind=None, sector_id=None, unacked=False):
        rows = [a for a in reversed(self.rec.alerts_all)
                if (not level or a["level"] == level) and (not kind or a["kind"] == kind)
                and (not sector_id or a["sector_id"] == sector_id) and (not unacked or not a["ack"])]
        allr = self.rec.alerts_all
        secs = [a["ack_sec"] for a in allr if a["ack"] and a["ack_sec"] is not None]
        return {"rows": rows,
                "summary": {"total": len(allr), "unacked": sum(not a["ack"] and a["kind"] != "호출" for a in allr),
                            "crit": sum(a["level"] == "crit" for a in allr),
                            "avg_ack_sec": round(sum(secs) / len(secs), 1) if secs else None,
                            "by_kind": {k: sum(a["kind"] == k for a in allr) for k in sorted({a["kind"] for a in allr})}}}

    # ---------- 가정값 ----------
    def set_assumptions(self, new: dict):
        changed = []
        before = {p["vehicle_id"]: (p["sector_id"], round(p["call_t"] or 0, 1)) for p in self.rec.plan}
        keys_changed = set()
        for k, v in new.items():
            if k == "pred_override":
                cur = dict(self.assumptions.get("pred_override") or {})
                for cargo, m in (v or {}).items():
                    if cargo not in self.assumptions["std_times"]:
                        continue
                    if m is None or m == "":
                        if cargo in cur:
                            cur.pop(cargo)
                            changed.append(f"{cargo} 예측 하차시간 고정 해제 → 자동(실측 평균)")
                    elif float(m) != cur.get(cargo):
                        changed.append(f"{cargo} 예측 하차시간 {cur.get(cargo, '자동')} → {float(m):g}분 고정")
                        cur[cargo] = float(m)
                if cur != (self.assumptions.get("pred_override") or {}):
                    self.assumptions["pred_override"] = cur
                    keys_changed.add("pred_override")
                continue
            if k == "idle_in_cost":
                on = v in (True, "true", "1", 1)
                if on != bool(self.assumptions.get("idle_in_cost")):
                    self.assumptions["idle_in_cost"] = on
                    changed.append("호퍼 유휴 금액 포함 " + ("켬" if on else "끔"))
                continue
            if k == "cost_note":
                note = str(v).strip()[:200]
                if note != self.assumptions.get("cost_note", ""):
                    self.assumptions["cost_note"] = note
                    changed.append("단가 근거 메모 수정")
                continue
            if k == "std_times":
                for cargo, m in v.items():
                    old = self.assumptions["std_times"].get(cargo)
                    if old is not None and float(m) != float(old):
                        self.assumptions["std_times"][cargo] = float(m)
                        changed.append(f"{cargo} 하차시간 목표 {old:g} → {float(m):g}분")
                        keys_changed.add("std_times")
            elif k in self.assumptions and float(v) != float(self.assumptions[k]):
                if (k == "slow_factor" and float(v) < 1) or (k == "speed_limit_kmh" and not 5 <= float(v) <= 60):
                    continue          # 감속 계수는 1 이상(제한속도를 넘지 않게), 속도는 5~60km/h만
                old = self.assumptions[k]
                self.assumptions[k] = float(v)
                changed.append(f"{LABELS.get(k, k)} {old:g} → {float(v):g}")
                keys_changed.add(k)
        if not changed:
            return []
        entry = {"at": self.rec.hhmm(self.rec.t), "by": "관제 담당자", "text": " · ".join(changed)}
        self.history.insert(0, entry)
        del self.history[20:]
        self.rebuild()
        after = {p["vehicle_id"]: (p["sector_id"], round(p["call_t"] or 0, 1)) for p in self.rec.plan}
        moved = sum(1 for vid in set(before) | set(after) if before.get(vid) != after.get(vid))
        judge_only = keys_changed <= self.JUDGE_KEYS
        if moved:
            entry["impact"] = f"순번 {moved}건 호출 시각 다시 계산"
        elif judge_only and keys_changed:
            entry["impact"] = "기준미달 판정만 바뀜 · 순번 그대로"
        else:
            entry["impact"] = "지금 순번은 그대로 · 앞으로 계산에 반영"
        if self.assumptions_path:
            self.assumptions_path.write_text(json.dumps(dict(self.assumptions, history=self.history), ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf-8")
        self._ui("ASSUMPTIONS_UPDATED", None, None, {"changes": changed})
        return changed

    # ---------- 비교 · 분석 ----------
    # ---------- 효과: 차량 1대마다 (FIFO였을 때 대기 − 추천 대기) ----------
    @staticmethod
    def _waits(sim):
        """반입("차량#회차")별 대기(대기장 진입 → 하차 시작). (분, 확정 여부). 아직 못 들어갔으면 지금까지(하한)."""
        out = {}
        for d in sim.done_trips:
            out[f"{d['id']}#{d['trip']}"] = (d["wait_total"], True)
        for v in sim.vehicles.values():
            if v["gone"] or v["yard_t"] is None:
                continue
            if v["state"] in ("working", "done"):
                out[sim.key(v)] = (v["work_start"] - v["yard_t"], True)
            else:
                out[sim.key(v)] = (sim.t - v["yard_t"], False)
        return out

    def won_per_min(self):
        return self.assumptions["wait_won_per_hour"] / 60        # ₩30,000/시간 = ₩500/분 (가정)

    def effects(self):
        """반입별 효과. 확정 = 두 방식 모두 하차를 시작한 반입. 예상 = 추천 쪽은 순번표의 예상 하차 시작, FIFO 쪽은 지금까지 대기(하한)."""
        rw, fw = self._waits(self.rec), self._waits(self.fifo)
        plan_start = {self.rec.key(self.rec.vehicles[p["vehicle_id"]]): p["start_t"] for p in self.rec.plan if p.get("start_t") is not None}
        out = {}
        for k, (r, r_ok) in rw.items():
            if k not in fw:
                continue
            f, f_ok = fw[k]
            if not r_ok and k in plan_start:
                v = self.rec.vehicles[k.split("#")[0]]
                r = plan_start[k] - v["yard_t"]
            out[k] = {"rec": round(r, 1), "fifo": round(f, 1), "effect": round(r - f, 1),
                      "confirmed": r_ok and f_ok, "fifo_lower": not f_ok}
        return out

    def effect_total(self, eff=None):
        eff = eff if eff is not None else self.effects()
        done = [e for e in eff.values() if e["confirmed"]]
        m = round(sum(e["effect"] for e in done), 1)
        return {"min": m, "won": int(round(m * self.won_per_min())), "vehicles": len(done),
                "won_per_min": self.won_per_min(), "saved_min": round(-sum(min(e["effect"], 0) for e in done), 1),
                "added_min": round(sum(max(e["effect"], 0) for e in done), 1)}

    def compare(self):
        r, f = self.rec.metrics(), self.fifo.metrics()
        a = self.assumptions
        saved_wait = f["wait_total"] - r["wait_total"]
        saved_idle = f["idle_min"] - r["idle_min"]
        eff = self.effect_total()
        dec = sum(1 for _ in self.approved) + len(self.changes)
        return {"fifo": f, "rec": r,
                "reduction_pct": round((f["avg_wait"] - r["avg_wait"]) / f["avg_wait"] * 100) if f["avg_wait"] else 0,
                "saved_wait_min": round(saved_wait, 1), "saved_idle_min": round(saved_idle, 1),
                "effect": eff,
                # 금액 = 차량별 효과 합(분) × 대기 단가. 호퍼 유휴는 금액에 넣지 않고 시간으로만 (두 번 세기 방지)
                "cost": self._cost(eff, saved_idle),
                "match_pct": round(len(self.approved) / dec * 100) if dec else None,
                "decisions": {"approved": len(self.approved), "changed": len(self.changes)}}

    def cargo_table(self):
        rows = {}
        for d in self.rec.done_trips:
            r = rows.setdefault(d["cargo"], {"cargo": d["cargo"], "trips": 0, "tons": 0.0, "unload": 0.0, "wait": 0.0, "delays": 0, "late": 0, "sectors": set()})
            r["trips"] += 1; r["tons"] += d["load_t"]; r["unload"] += d["unload_min"]; r["wait"] += d["wait_total"]
            r["delays"] += d["delayed"]; r["late"] += d["late"]; r["sectors"].add(d["sector"])
        out = []
        for r in rows.values():
            n = r["trips"]
            note = []
            if r["delays"] >= 2:
                note.append(f"하차 지연 {r['delays']}건 — 표준시간 확인")
            if len(r["sectors"]) > 1:
                note.append("공용 호퍼로 분산됨 (" + "·".join(sorted(r["sectors"])) + ")")
            out.append({"cargo": r["cargo"], "trips": n, "tons": round(r["tons"], 1), "avg_unload": round(r["unload"] / n, 1),
                        "avg_wait": round(r["wait"] / n, 1), "delays": r["delays"], "late": r["late"], "note": " · ".join(note)})
        return sorted(out, key=lambda r: -r["trips"])

    def feedback(self):
        counts = {r: 0 for r in REASONS}
        for c in self.changes:
            counts[c["reason"]] += 1
        sugg = []
        groups = {}
        for c in self.changes:
            groups.setdefault((c["to"], c["reason"]), []).append(c)
        for (sid, reason), cs in groups.items():
            if len(cs) < REPEAT_SUGGEST or reason in ("기타", "긴급 반입 끼어듦"):
                continue
            cargo = max({c["cargo"] for c in cs}, key=lambda x: sum(c["cargo"] == x for c in cs))
            old = self.assumptions["std_times"].get(cargo)
            if old is None:
                continue
            new = round(old * 1.2)
            key = f"{sid}|{reason}|{cargo}|{old:g}"
            if key in self.ignored or new == old:
                continue
            name = self.rec.sec_cfg[sid]["name"] if sid in self.rec.sec_cfg else sid
            sugg.append({"key": key, "cargo": cargo, "old": old, "new": new,
                         "text": f"{name}에서 '{reason}' 변경이 {len(cs)}건 반복됐어요. {cargo} 하차 표준시간을 {old:g}분 → {new:g}분으로 올리면 추천이 실제와 가까워져요."})
        by = {}
        for c in self.changes:
            by[c.get("by") or "관제"] = by.get(c.get("by") or "관제", 0) + 1
        return {"counts": counts, "total": len(self.changes), "suggestions": sugg, "by": by,
                "memos": [c["memo"] for c in reversed(self.changes) if c.get("memo")][:5]}

    def apply_suggestion(self, key):
        for s in self.feedback()["suggestions"]:
            if s["key"] == key:
                return self.set_assumptions({"std_times": {s["cargo"]: s["new"]}})
        return None

    def ignore_suggestion(self, key):
        self.ignored.add(key)

    # ---------- 알림 패널: 무슨 일 → 제안 → 효과 ----------
    AUTO_DONE = {"호퍼 이상", "대기 초과"}     # 엔진이 바로 처리하는 제안 (재배정 · 우선 배정)

    def _in_plan(self, key):
        vid = key.split("#")[0]
        v = self.rec.vehicles.get(vid)
        return bool(v and self.rec.key(v) == key and v["state"] == "yard")

    def alert_status(self, a):
        if a["ack"]:
            return "확인함"
        waiting = [k for k in a.get("related", []) if k in self.rec.pending
                   or (self.rec.require_approval and k not in self.approved and self._in_plan(k))]
        if waiting:
            return "승인 대기"
        return "실행됨" if a["kind"] in self.AUTO_DONE else "확인 필요"

    def alert_cards(self, limit=30, eff=None):
        eff = eff if eff is not None else self.effects()
        rank = {self.rec.key(self.rec.vehicles[p["vehicle_id"]]): p["rank"] for p in self.rec.plan}
        out = []
        for a in reversed(self.rec.alerts_all):
            if a["kind"] == "호출":
                continue
            es = [eff[k] for k in a.get("related", []) if k in eff]
            card = dict(a, status=self.alert_status(a), ranks=[rank[k] for k in a.get("related", []) if k in rank])
            card["approve_vid"] = next((k.split("#")[0] for k in a.get("related", [])
                                        if self._in_plan(k) and k not in self.approved), None) if card["status"] == "승인 대기" else None
            if es:
                m = round(sum(e["effect"] for e in es), 1)
                card["effect"] = {"min": m, "won": int(round(m * self.won_per_min())), "n": len(es),
                                  "fifo": round(sum(e["fifo"] for e in es), 1), "rec": round(sum(e["rec"] for e in es), 1),
                                  "confirmed": all(e["confirmed"] for e in es), "fifo_lower": any(e["fifo_lower"] for e in es)}
            out.append(card)
            if len(out) >= limit:
                break
        return out

    CHECK_KINDS = ("하차 지연", "혼잡", "호퍼 이상")

    def panel2(self):
        """알림 패널(시안 v4): [순번 | 안전] · 순번 › [승인 대기 | 확인 필요]."""
        sim, eff = self.rec, self.effects()
        hh = sim.hhmm
        # 확인 필요: 현장 점검 요청 — 같은 호퍼·같은 종류는 한 카드로
        groups = {}
        for a in sim.alerts_all:
            if a["kind"] not in self.CHECK_KINDS or a["ack"] or a["id"] in self.ignored_alerts or a["level"] == "info":
                continue
            g = groups.setdefault((a["sector_id"], a["kind"]), {"sector_id": a["sector_id"], "kind": a["kind"], "items": []})
            es = [eff[k] for k in a.get("related", []) if k in eff]
            g["items"].append({"id": a["id"], "ts": a["ts"], "t": a["t"], "text": a["text"], "vehicle_id": a["vehicle_id"],
                               "suggest": a["action"], "effect": round(sum(e["effect"] for e in es), 1) if es and all(e["confirmed"] for e in es) else None})
        checks = []
        for g in groups.values():
            g["items"].sort(key=lambda x: -x["t"])
            name = sim.sec_cfg[g["sector_id"]]["name"] if g["sector_id"] in sim.sec_cfg else ""
            n = len(g["items"])
            g.update(name=name, n=n, ts=g["items"][0]["ts"], t=g["items"][0]["t"], suggest=g["items"][0]["suggest"],
                     title=f"{name} {g['kind']}" + (f" {n}건" if n > 1 else ""), ids=[x["id"] for x in g["items"]])
            checks.append(g)
        checks.sort(key=lambda g: -g["t"])
        # 승인 대기: 순번·호출을 바꾸는 제안 (승인해야 운전자에게 호출이 감)
        cause = {}
        for g in checks:
            for a in sim.alerts_all:
                if a["id"] in g["ids"]:
                    for k in a.get("related", []):
                        cause[k] = f"{g['title']} 때문 (확인 필요 탭)"
        waitover = {k: a for a in sim.alerts_all if a["kind"] == "대기 초과" for k in a.get("related", [])}
        sched = self.schedule()
        bay = {g["vehicle_id"]: r["bay"] for r in sched["rows"] for g in r["segs"]}
        appr = []
        for p in sim.plan:
            if not p["sector_id"] or p["vehicle_id"] in sim.overrides:
                continue
            v = sim.vehicles[p["vehicle_id"]]
            k = sim.key(v)
            if k in self.approved:
                continue
            e = eff.get(k)
            why = cause.get(k) or (f"대기 {int(sim.t - v['yard_t'])}분 (목표 {self.assumptions['target_wait_min']:g}분)" if k in waitover
                                   else p["reason"].split(" · ")[0])
            appr.append({"vehicle_id": v["id"], "rank": p["rank"], "sector_id": p["sector_id"], "name": sim.sec_cfg[p["sector_id"]]["name"],
                         "bay": bay.get(v["id"]), "call_at": hh(p["call_t"]) if p["call_t"] is not None else None,
                         "travel": round(sim.travel("YARD", p["sector_id"]), 1), "why": why, "urgent": v["urgent"],
                         "effect": e["effect"] if e else None, "effect_confirmed": bool(e and e["confirmed"]),
                         "overdue": k in sim.pending, "wait": round(sim.t - v["yard_t"], 1)})
        safe_open = [x for x in sim.safety if x["status"] == "open"]
        safe_done = [dict(x, ack_sec=self.safety_acks.get(x["id"], {}).get("ack_sec", x["ack_sec"])) for x in sim.safety if x["status"] != "open"]
        n_checks = sum(g["n"] for g in checks)
        return {"approvals": appr, "checks": checks, "safety_open": safe_open, "safety_done": list(reversed(safe_done)),
                "counts": {"approvals": len(appr), "checks": n_checks, "seq": len(appr) + n_checks, "safety": len(safe_open)},
                "auto_approve": self.auto_approve, "total": self.effect_total(eff), "clock": hh(sim.t),
                "sector_names": {sid: sc["name"] for sid, sc in sim.sec_cfg.items()}}

    def panel(self):
        eff = self.effects()
        cards = self.alert_cards(eff=eff)
        return {"cards": cards, "total": self.effect_total(eff), "auto_approve": self.auto_approve,
                "counts": {"all": len(cards), "safety": 0, "ops": len(cards),
                           "open": sum(c["status"] in ("승인 대기", "확인 필요") for c in cards)},
                "clock": self.rec.hhmm(self.rec.t)}

    # ---------- 순번·배정: 베이 비는 시각 시간표 ----------
    def schedule(self, before=5, after=40):
        sim, t = self.rec, self.rec.t
        t0, t1 = t - before, t + after
        rows = []
        plan_by_sector = {}
        for p in sim.plan:
            if p["sector_id"] and p.get("start_t") is not None:
                plan_by_sector.setdefault(p["sector_id"], []).append(p)
        for sid, sc in sim.sec_cfg.items():
            up = sim.sectors[sid]["up"]
            view = sim.sector_view(sid)
            lanes = [[] for _ in range(sc["bays"])]
            free = [t] * sc["bays"]
            for v in sim.vehicles.values():                     # 하차 중
                if v["state"] == "working" and v["sector"] == sid:
                    late = "delay" in v["flags"]
                    if not up:
                        end, kind = t1, "stop"
                    elif late:
                        end, kind = max(t + 1, v["work_start"] + v["work_elapsed"] * 1.05), "late"
                    else:
                        end, kind = v["work_start"] + max(v["std"], v["work_elapsed"] + 0.25), "work"
                    pct = round(v["work_elapsed"] / v["std"] * 100)
                    label = v["id"] + (" · 지연 ×%.1f" % (v["work_elapsed"] / v["std"]) if late else " · %d%%" % min(100, pct))
                    lanes[v["bay"]].append({"kind": kind, "t0": v["work_start"], "t1": end, "vehicle_id": v["id"], "label": label})
                    free[v["bay"]] = max(free[v["bay"]], end)
            incoming = sorted([v for v in sim.vehicles.values() if v["sector"] == sid and v["state"] in ("moving", "queue")],
                              key=lambda v: (v["path_t0"] + v["path_dur"]) if v["state"] == "moving" else t)
            for v in incoming:                                   # 호출돼 오는 중 · 호퍼 앞 대기
                arr = max(t, v["path_t0"] + v["path_dur"]) if v["state"] == "moving" else t
                b = min(range(sc["bays"]), key=lambda i: free[i])
                st_ = max(arr, free[b])
                where = " · 이동 중 " + sim.hhmm(arr) + " 도착" if v["state"] == "moving" else " · 호퍼 앞 대기"
                lanes[b].append({"kind": "coming", "t0": st_, "t1": st_ + v["std"], "vehicle_id": v["id"], "label": v["id"] + where})
                free[b] = st_ + v["std"]
            if up:
                for p in sorted(plan_by_sector.get(sid, []), key=lambda p: p["start_t"]):   # 순번표 예약
                    v = sim.vehicles[p["vehicle_id"]]
                    b = min(range(sc["bays"]), key=lambda i: free[i])
                    st_ = max(p["start_t"], free[b])
                    k = sim.key(v)
                    need = sim.require_approval and k not in self.approved and v["id"] not in sim.overrides
                    lanes[b].append({"kind": "pending" if need else "reserved", "t0": st_, "t1": st_ + v["std"],
                                     "vehicle_id": v["id"], "rank": p["rank"],
                                     "label": f"{p['rank']}. {v['id']} · {sim.hhmm(p['call_t'])} 호출" + (" · 승인 대기" if need else "")})
                    free[b] = st_ + v["std"]
            for i, lane in enumerate(lanes):
                segs = [dict(x, at0=sim.hhmm(x["t0"]), at1=sim.hhmm(x["t1"]),
                             t0=round(max(x["t0"], t0), 2), t1=round(min(x["t1"], t1), 2))
                        for x in lane if x["t1"] > t0 and x["t0"] < t1]
                busy_until = max([x["t1"] for x in lane if x["kind"] in ("work", "late", "stop", "coming")] + [t])
                rows.append({"sector_id": sid, "name": sc["name"], "bay": i + 1, "up": up, "level": view["level"],
                             "pct": view["congestion_pct"], "free_at": sim.hhmm(busy_until),
                             "free_now": busy_until <= t + 1e-9 and up, "segs": segs})
        ticks = []
        k = int((t0 // 5 + 1) * 5)
        while k <= t1:
            ticks.append({"t": k, "hhmm": sim.hhmm(k), "major": k % 15 == 0})
            k += 5
        return {"t0": t0, "t1": t1, "now": t, "now_hhmm": sim.hhmm(t), "ticks": ticks, "rows": rows,
                "auto_approve": self.auto_approve}

    # ---------- 화면용 묶음 ----------
    def state(self):
        s = self.rec.state()
        s["running"] = self.running
        eff = self.effects()
        last_alert = {}
        for a in self.rec.alerts_all:
            if a["kind"] != "호출":
                for k in a.get("related", []):
                    last_alert[k] = {"id": a["id"], "kind": a["kind"], "ts": a["ts"]}
        for q in s["queue"]:
            q["status"] = self.order_status(q["vehicle_id"])
            k = self.rec.key(self.rec.vehicles[q["vehicle_id"]])
            q["effect"] = eff.get(k)
            q["alert"] = last_alert.get(k)
        s["compare_brief"] = {"reduction_pct": self.compare()["reduction_pct"]}
        s["auto_approve"] = self.auto_approve
        s["effect_total"] = self.effect_total(eff)
        s["panel_open"] = sum(self.alert_status(a) in ("승인 대기", "확인 필요")
                              for a in self.rec.alerts_all[-60:] if a["kind"] != "호출")
        return s

    def dock(self):
        return {"compare": self.compare(), "assumptions": self.assumptions, "history": self.history[:5],
                "changes": list(reversed(self.changes[-10:])), "reasons": REASONS, "auto_approve": self.auto_approve,
                "schedule": self.schedule(), "clock": self.rec.clock().isoformat(timespec="seconds"),
                "preds": {c: round(self.rec.pred_of(c), 1) for c in self.assumptions["std_times"]},
                "pred_fixed": sorted((self.assumptions.get("pred_override") or {}).keys()),
                "sectors": [self.rec.sector_view(sid) for sid in self.rec.sec_cfg]}

    def analysis(self):
        return {"compare": self.compare(), "series": {"rec": self.rec.series, "fifo": self.fifo.series},
                "cargo": self.cargo_table(), "feedback": self.feedback(), "clock": self.rec.clock().isoformat(timespec="seconds")}


    # ---------- 금액 ----------
    def _cost(self, eff, saved_idle):
        a = self.assumptions
        idle_won = int(round(saved_idle / 60 * a["idle_won_per_hour"])) if a.get("idle_in_cost") else 0
        return {"total": -eff["won"] + idle_won, "wait": -eff["won"], "idle": idle_won, "idle_in_cost": bool(a.get("idle_in_cost")),
                "wait_rate": a["wait_won_per_hour"], "won_per_min": eff["won_per_min"], "idle_rate": a["idle_won_per_hour"]}

    # ---------- 효과를 만든 것 · 건별 기록 ----------
    SOURCES = ["가장 빨리 비는 베이 (일반 추천)", "대기 초과 → 우선 배정", "혼잡 → 호출 보류", "작업 지연 → 호출 늦춤",
               "긴급 반입 → 순번 올림", "호퍼 정지 → 재배정", "담당자 순번 변경"]
    ALERT_SOURCE = {"호퍼 이상": "호퍼 정지 → 재배정", "대기 초과": "대기 초과 → 우선 배정", "혼잡": "혼잡 → 호출 보류",
                    "하차 지연": "작업 지연 → 호출 늦춤"}

    def _source_index(self):
        idx = {}
        for a in self.rec.alerts_all:
            src = self.ALERT_SOURCE.get(a["kind"])
            if src and a["level"] != "info":
                for k in a.get("related", []):
                    idx.setdefault(k, set()).add(src)
        return idx

    def source_of(self, key, idx=None, changed=None, urgent=None):
        changed = changed if changed is not None else {f"{c['vehicle_id']}#{c['trip']}" for c in self.changes}
        if key in changed:
            return "담당자 순번 변경"
        if urgent is None:
            vid = key.split("#")[0]
            v = self.rec.vehicles.get(vid)
            d = next((d for d in self.rec.done_trips if f"{d['id']}#{d['trip']}" == key), None)
            urgent = bool((d and d.get("urgent")) or (v and self.rec.key(v) == key and v["urgent"]))
        if urgent:
            return "긴급 반입 → 순번 올림"
        srcs = (idx if idx is not None else self._source_index()).get(key, set())
        for s in ["호퍼 정지 → 재배정", "대기 초과 → 우선 배정", "혼잡 → 호출 보류", "작업 지연 → 호출 늦춤"]:
            if s in srcs:
                return s
        return "가장 빨리 비는 베이 (일반 추천)"

    def records(self, eff=None):
        """차량별 효과 표 (건별 기록). 효과 합 = effect_total()과 같은 반입 집합."""
        eff = eff if eff is not None else self.effects()
        idx, changed = self._source_index(), {f"{c['vehicle_id']}#{c['trip']}" for c in self.changes}
        hh = self.rec.hhmm
        rows = []
        done = {f"{d['id']}#{d['trip']}": d for d in self.rec.done_trips}
        for k, e in eff.items():
            vid, trip = k.split("#")
            d = done.get(k)
            v = self.rec.vehicles.get(vid)
            if d:
                info = dict(cargo=d["cargo"], load_t=d["load_t"], yard_t=d["yard_t"], call_t=d["call_t"], start=d["work_start"],
                            end=d["unload_end"], urgent=d.get("urgent"), sector=d["sector"], state="이탈")
            elif v and self.rec.key(v) == k:
                st = {"yard": "대기", "moving": "이동 중", "queue": "호퍼 앞 대기", "working": "하차 중", "done": "이탈"}.get(v["state"], v["state"])
                info = dict(cargo=v["cargo"], load_t=v["load_t"], yard_t=v["yard_t"], call_t=v.get("call_t"),
                            start=v.get("work_start") if v["state"] in ("working", "done") else None,
                            end=v.get("done_t") if v["state"] == "done" else None, urgent=v["urgent"], sector=v["sector"], state=st)
            else:
                continue
            src = self.source_of(k, idx, changed, info["urgent"])
            rows.append({"key": k, "vehicle_id": vid, "trip": int(trip), "cargo": info["cargo"], "load_t": info["load_t"],
                         "sector": info["sector"], "yard": hh(info["yard_t"]), "yard_t": info["yard_t"],
                         "call": hh(info["call_t"]) if info["call_t"] is not None else None,
                         "enter": hh(info["start"]) if info["start"] is not None else None,
                         "exit": hh(info["end"]) if info["end"] is not None else None,
                         "rec_wait": e["rec"], "fifo_wait": e["fifo"], "fifo_lower": e["fifo_lower"], "effect": e["effect"],
                         "won": int(round(e["effect"] * self.won_per_min())), "confirmed": e["confirmed"],
                         "source": src, "state": info["state"] if e["confirmed"] or info["state"] != "이탈" else info["state"]})
        rows.sort(key=lambda r: -r["yard_t"])
        return rows

    def sources(self, rows=None):
        rows = rows if rows is not None else self.records()
        out = []
        for s in self.SOURCES:
            rs = [r for r in rows if r["source"] == s and r["confirmed"]]
            m = round(sum(r["effect"] for r in rs), 1)
            out.append({"source": s, "n": len(rs), "min": m, "won": int(round(m * self.won_per_min())),
                        "saved": round(-sum(min(r["effect"], 0) for r in rs), 1), "added": round(sum(max(r["effect"], 0) for r in rs), 1)})
        return out

    def cargo_actual(self):
        """원료별 실측 하차시간 (진입 → 이탈) vs 표준(가정)."""
        out = []
        for cargo, std in self.assumptions["std_times"].items():
            ds = [d for d in self.rec.done_trips if d["cargo"] == cargo]
            if not ds:
                out.append({"cargo": cargo, "n": 0, "actual": None, "std": std, "diff": None})
                continue
            m = round(sum(d["unload_min"] for d in ds) / len(ds), 1)
            out.append({"cargo": cargo, "n": len(ds), "actual": m, "std": std, "diff": round(m - std, 1)})
        return out

    # ---------- 알림 이력: 알림 → 제안 → 처리 → 효과 ----------
    def handling(self, a):
        if a["id"] in self.ignored_alerts:
            return "무시"
        rel = a.get("related", [])
        changed = {f"{c['vehicle_id']}#{c['trip']}" for c in self.changes}
        if any(k in changed for k in rel):
            return "변경 저장"
        st = self.alert_status(a) if not a["ack"] else "확인함"
        if st == "승인 대기":
            return "승인 대기"
        if a["kind"] in self.AUTO_DONE or any(not self._in_plan(k) for k in rel):
            return "실행"
        return "확인함" if a["ack"] else "제안만"

    def alert_history(self, eff=None):
        eff = eff if eff is not None else self.effects()
        rows = []
        for a in self.rec.alerts_all:
            if a["kind"] in ("호출", "아차사고"):          # 아차사고는 아래 '안전'에서 기록 한 건씩
                continue
            rel = a.get("related", [])
            es = [eff[k] for k in rel if k in eff]
            done = bool(es) and all(e["confirmed"] for e in es) and len(es) == len(rel)
            act = None
            if done:
                m = round(sum(e["effect"] for e in es), 1)
                act = {"min": m, "won": int(round(m * self.won_per_min()))}
            rows.append({"id": a["id"], "ts": a["ts"], "t": a["t"], "level": a["level"], "kind": a["kind"], "text": a["text"],
                         "suggest": a["action"], "handling": self.handling(a), "expected": self.expected.get(a["id"]),
                         "actual": act, "has_vehicles": bool(rel), "sector_id": a["sector_id"], "vehicle_id": a["vehicle_id"],
                         "group": "운영", "ack": a["ack"]})
        for c in self.changes:                          # 순번 변경도 한 줄로
            k = f"{c['vehicle_id']}#{c['trip']}"
            e = eff.get(k)
            rows.append({"id": "CH-" + k, "ts": c["ts"], "t": c["t"], "level": "info", "kind": "순번 변경",
                         "text": f"{c['vehicle_id']} · {c['reason']}" + (f" · {c['memo']}" if c["memo"] else ""),
                         "suggest": f"{self.rec.sec_cfg.get(c['from'], {}).get('name', c['from'])} → {self.rec.sec_cfg[c['to']]['name']}",
                         "handling": "변경 저장",
                         "expected": {"min": c["delta_min"], "won": int(round(c["delta_min"] * self.won_per_min()))},
                         "actual": {"min": e["effect"], "won": int(round(e["effect"] * self.won_per_min()))} if e and e["confirmed"] else None,
                         "has_vehicles": True, "sector_id": c["to"], "vehicle_id": c["vehicle_id"], "group": "순번 변경", "ack": True,
                         "actual_note": "FIFO 대비"})
        for x in self.rec.safety:                        # 안전 경고도 한 줄로 (금액 계산 안 함)
            how = {"open": "미확인", "ack": "확인", "dismissed": "오탐지", "auto": "자동 해제 · 미확인"}[x["status"]]
            ack = self.safety_acks.get(x["id"], {}).get("ack_sec")
            rows.append({"id": x["id"], "ts": x["ts"][:5], "t": x["t"], "level": "crit", "kind": "안전",
                         "text": f"{self.rec.sec_cfg[x['sector_id']]['name']} · {x['cargo']} {x.get('rule_text', '덤프 반경')} 안 작업자 ({x['dist_m']}m) · {x['vehicle_id']}"
                                 + (" · 다시 감지" if x["repeat"] else ""),
                         "suggest": "L3 자동 실행: 앱 경고 · 하차 중지 요청 (가상)", "handling": how + (f" · {ack}초" if ack is not None else ""),
                         "expected": None, "actual": None, "has_vehicles": False, "sector_id": x["sector_id"], "vehicle_id": x["vehicle_id"],
                         "group": "안전", "ack": x["status"] != "open", "actual_note": "금액 없음"})
        alerts = {a["id"]: a for a in self.rec.alerts_all}
        for x in self.rec.near_miss:                     # 아차사고(영상 NEAR_MISS · 대기장 접근 등)도 '안전'으로 — CAM 상세 아차사고 기록과 같은 건
            lv, yard = x.get("level") or "", x.get("sector_id") is None
            where = "대기장" if yard else f"{self.rec.sec_cfg.get(x['sector_id'], {}).get('name', x['sector_id'])} {x.get('bay')}번 베이"
            m = re.search(r"-R(\d+)$", x["id"])
            rep = f" · 같은 영상 장면 {m.group(1)}회차" if m and int(m.group(1)) > 1 else ""
            a = alerts.get(x.get("alert_id"))
            how = ("확인함" if a["ack"] else "미확인") if a else "기록"
            rows.append({"id": x["id"], "ts": (x.get("ts") or "")[:5], "t": x["t"], "level": {"L3": "crit", "L2": "warn"}.get(lv, "info"), "kind": "아차사고",
                         "text": f"{where} · {x.get('rule')} {lv} · 최소 {x.get('min_dist_m')}m · {x.get('vehicle_id') or ''} "
                                 f"({'합성 사람' if x.get('source') == 'stand_in' else '실제 영상'} {x.get('cam_id') or ''}){rep}",
                         "suggest": "대기장 보행 동선 · 운전자 서행 확인 (CAM 장면 보기)" if yard else "덤프 반경 · 후진 경로 작업자 동선 확인 (CAM 장면 보기)",
                         "handling": how, "expected": None, "actual": None, "has_vehicles": False, "sector_id": x.get("sector_id"),
                         "vehicle_id": x.get("vehicle_id"), "group": "안전", "ack": how != "미확인", "actual_note": "금액 없음", "cam_id": x.get("cam_id")})
        rows.sort(key=lambda r: -r["t"])
        return rows

    def alert_funnel(self, rows):
        ops = [r for r in rows if r["group"] == "운영"]
        prop = [r for r in ops if r["has_vehicles"]]
        exe = [r for r in prop if r["handling"] in ("실행", "변경 저장")]
        conf = [r for r in exe if r["actual"] is not None]
        both = [r for r in conf if r["expected"] is not None]
        ign = [r for r in prop if r["handling"] == "무시"]
        exp_sum = round(sum(r["expected"]["min"] for r in both), 1)
        act_sum = round(sum(r["actual"]["min"] for r in both), 1)
        return {"ops": len(ops), "by_kind": {k: sum(r["kind"] == k for r in ops) for k in sorted({r["kind"] for r in ops})},
                "proposed": len(prop), "executed": len(exe),
                "waiting": sum(r["handling"] == "승인 대기" for r in prop), "ignored": len(ign),
                "only": sum(r["handling"] == "제안만" for r in prop), "confirmed": len(conf),
                "expected_min": exp_sum, "actual_min": act_sum,
                "ratio_pct": round(act_sum / exp_sum * 100) if exp_sum < -0.05 else None, "compared": len(both),
                "ignored_rows": [{"ts": r["ts"], "kind": r["kind"], "expected": r["expected"], "actual": r["actual"]} for r in ign]}

    # ---------- 바꿔보기 (저장하지 않는 미리보기) ----------
    PREVIEW_KEYS = ("target_wait_min", "call_margin_min")
    JUDGE_KEYS = {"std_times", "target_wait_min", "warn_pct", "crit_pct", "delay_factor", "idle_in_cost", "cost_note",
                  "wait_won_per_hour", "idle_won_per_hour"}       # 바꿔도 순번은 안 바뀌는 값 (판정·알림·금액)

    def judge_counts(self, r=None):
        """기준미달 건수: 하차시간이 목표를 넘은 반입, 대기가 목표 대기시간을 넘은 반입 (끝난 반입 기준)."""
        r = r or self
        tw = r.assumptions["target_wait_min"]
        ds = r.rec.done_trips
        return {"unload": sum(1 for d in ds if d["unload_min"] > r.assumptions["std_times"].get(d["cargo"], d["std"]) + 1e-9),
                "wait": sum(1 for d in ds if d["wait_total"] > tw + 1e-9), "n": len(ds)}

    def preview(self, change):
        new = copy.deepcopy(self.assumptions)
        new.setdefault("pred_override", {})
        diff = []
        for cargo, m in (change.get("pred") or {}).items():
            cur = self.rec.pred_of(cargo) if cargo in self.rec.pred else None
            if cur is not None and abs(float(m) - cur) >= 0.05:
                diff.append({"key": "pred:" + cargo, "label": f"{cargo} 예측 하차시간", "old": round(cur, 1), "new": float(m), "unit": "분",
                             "judge_only": False})
                new["pred_override"][cargo] = float(m)
        for cargo, m in (change.get("std_times") or {}).items():
            if cargo in new["std_times"] and float(m) != float(new["std_times"][cargo]):
                diff.append({"key": "std:" + cargo, "label": f"{cargo} 하차시간 목표", "old": new["std_times"][cargo], "new": float(m), "unit": "분",
                             "judge_only": True})
                new["std_times"][cargo] = float(m)
        for k in self.PREVIEW_KEYS:
            if k in change and float(change[k]) != float(new[k]):
                diff.append({"key": k, "label": LABELS[k].split("(")[0], "old": new[k], "new": float(change[k]), "unit": "분",
                             "judge_only": k in self.JUDGE_KEYS})
                new[k] = float(change[k])
        if not diff:
            return {"diff": [], "rows": [], "bays": [], "same": True}
        tmp = self.clone(new)
        e0, e1 = self.effects(), tmp.effects()
        p0 = {p["vehicle_id"]: p for p in self.rec.plan}
        p1 = {p["vehicle_id"]: p for p in tmp.rec.plan}
        hh = self.rec.hhmm
        rows = []
        for vid in list(dict.fromkeys(list(p0) + list(p1))):
            a, b = p0.get(vid), p1.get(vid)
            v = self.rec.vehicles.get(vid) or tmp.rec.vehicles.get(vid)
            k = f"{vid}#{v['trip']}" if v else None
            ca = a["call_t"] if a else None
            cb = b["call_t"] if b else None
            ea, eb = (e0.get(k) or {}).get("effect"), (e1.get(k) or {}).get("effect")
            moved = (ca is None) != (cb is None) or (ca is not None and cb is not None and abs(ca - cb) >= 0.5) \
                or (a and b and a["sector_id"] != b["sector_id"]) or (ea is not None and eb is not None and abs(ea - eb) >= 0.5)
            rows.append({"vehicle_id": vid, "rank": (b or a)["rank"], "rank_old": a["rank"] if a else None,
                         "sector_old": a["sector_id"] if a else None, "sector_new": b["sector_id"] if b else None,
                         "call_old": hh(ca) if ca is not None else None, "call_new": hh(cb) if cb is not None else None,
                         "effect_old": ea, "effect_new": eb, "changed": bool(moved),
                         "state_new": (tmp.rec.vehicles.get(vid) or {}).get("state")})
        s0, s1 = self.schedule(), tmp.schedule()
        bays = []
        for r0, r1 in zip(s0["rows"], s1["rows"]):
            if r0["free_at"] != r1["free_at"]:
                bays.append({"sector_id": r0["sector_id"], "name": r0["name"], "bay": r0["bay"], "old": r0["free_at"], "new": r1["free_at"]})
        c0, c1 = self.compare()["effect"], tmp.compare()["effect"]
        return {"diff": diff, "rows": rows, "bays": bays, "same": False, "schedule": s1,
                "judge": {"old": self.judge_counts(), "new": self.judge_counts(tmp)},
                "judge_only": all(d["judge_only"] for d in diff),
                "total_old": c0, "total_new": c1, "changed_n": sum(r["changed"] for r in rows), "values": new}

    # ---------- 최근 7일 (시드를 바꾼 시뮬레이션 날짜) ----------
    WEEK_SEEDS = [1, 2, 3, 4, 5, 6]

    def _day_summary(self, r, date):
        eff = r.effects()
        rows = r.records(eff)
        c = r.compare()
        ah = r.alert_history(eff)
        return {"date": date, "compare": c, "sources": r.sources(rows), "records": rows, "alerts": ah,
                "funnel": r.alert_funnel(ah), "series": {"rec": r.rec.series, "fifo": r.fifo.series},
                "cargo_actual": r.cargo_actual()}

    def week(self):
        return self.week_days() + [self._day_summary(self, self.rec.start.strftime("%m/%d") + " (오늘)")]

    def week_days(self):
        """지난 6일(시드를 바꾼 시뮬레이션). 가정값이 같으면 캐시를 쓴다. 서버 시작 때 미리 계산해 둔다."""
        key = json.dumps(self.assumptions, sort_keys=True, ensure_ascii=False)
        if not self.week_cache or self.week_cache["key"] != key:
            days = []
            start = self.rec.start
            for i, sd in enumerate(self.WEEK_SEEDS):
                r = Runtime(sd)
                r.assumptions = copy.deepcopy(self.assumptions)
                r._new_sims()
                r.advance(r.rec.fc["shift_min"] + 30)
                days.append(r._day_summary(r, (start - timedelta(days=len(self.WEEK_SEEDS) - i)).strftime("%m/%d")))
            self.week_cache = {"key": key, "days": days}
        return self.week_cache["days"]

    def _days(self, rng):
        if rng == "week":
            return self.week()
        if rng == "demo":
            return [self.demo_day()]
        return [self._day_summary(self, self.rec.start.strftime("%m/%d") + " (오늘)")]

    def analysis_range(self, rng="today"):
        days = self._days(rng)
        today = days[-1]
        allrec = [dict(r, date=d["date"]) for d in days for r in d["records"]]
        src = {}
        for d in days:
            for s in d["sources"]:
                x = src.setdefault(s["source"], {"source": s["source"], "n": 0, "min": 0.0, "won": 0, "saved": 0.0, "added": 0.0})
                for k in ("n", "won"):
                    x[k] += s[k]
                for k in ("min", "saved", "added"):
                    x[k] = round(x[k] + s[k], 1)
        sm = lambda f: sum(f(d) for d in days)
        eff_min = round(sm(lambda d: d["compare"]["effect"]["min"]), 1)
        tot = {"effect_min": eff_min, "won": sm(lambda d: d["compare"]["cost"]["total"]),
               "vehicles": sm(lambda d: d["compare"]["effect"]["vehicles"]),
               "saved_min": round(sm(lambda d: d["compare"]["effect"]["saved_min"]), 1),
               "added_min": round(sm(lambda d: d["compare"]["effect"]["added_min"]), 1),
               "won_per_min": self.won_per_min(), "idle_in_cost": bool(self.assumptions.get("idle_in_cost")),
               "idle_won": sm(lambda d: d["compare"]["cost"]["idle"])}
        metric = lambda side, k: sm(lambda d: d["compare"][side][k])
        util = lambda side: round(sum(d["compare"][side]["util_pct"] for d in days) / len(days))
        times = {"util": [util("fifo"), util("rec")], "delays": [metric("fifo", "delays"), metric("rec", "delays")],
                 "trips": [metric("fifo", "trips_done"), metric("rec", "trips_done")],
                 "idle": [round(metric("fifo", "idle_min")), round(metric("rec", "idle_min"))],
                 "avg_wait": [round(sum(d["compare"]["fifo"]["avg_wait"] for d in days) / len(days), 1),
                              round(sum(d["compare"]["rec"]["avg_wait"] for d in days) / len(days), 1)],
                 "max_wait": [max(d["compare"]["fifo"]["max_wait"] for d in days), max(d["compare"]["rec"]["max_wait"] for d in days)]}
        if rng == "week":
            series = {"rec": [{"hhmm": d["date"].split(" ")[0], "waiting": d["compare"]["rec"]["avg_wait"]} for d in days],
                      "fifo": [{"hhmm": d["date"].split(" ")[0], "waiting": d["compare"]["fifo"]["avg_wait"]} for d in days],
                      "unit": "평균 대기(분)"}
        else:
            series = dict(today["series"], unit="대기 차량(대)")
        spark = {k: [{"x": p["hhmm"], "fifo": f.get(k), "rec": p.get(k)} for p, f in zip(today["series"]["rec"], today["series"]["fifo"])]
                 for k in ("util", "delays", "trips")} if rng != "week" else             {k: [{"x": d["date"].split(" ")[0], "fifo": d["compare"]["fifo"][m], "rec": d["compare"]["rec"][m]} for d in days]
             for k, m in (("util", "util_pct"), ("delays", "delays"), ("trips", "trips_done"))}
        fb = self.feedback()
        c = self.compare()
        return {"range": rng, "days": [{"date": d["date"], "effect_min": d["compare"]["effect"]["min"], "won": d["compare"]["cost"]["total"],
                                        "avg_wait": [d["compare"]["fifo"]["avg_wait"], d["compare"]["rec"]["avg_wait"]],
                                        "reduction_pct": d["compare"]["reduction_pct"]} for d in days],
                "total": tot, "sources": [src[s] for s in self.SOURCES], "times": times, "series": series,
                "records": allrec, "cargo_actual": today["cargo_actual"], "feedback": fb,
                "match_pct": c["match_pct"], "decisions": c["decisions"], "compare": c,
                "simulated_days": rng == "week", "spark": spark, "wait_band": 8,
                "now": today["series"]["rec"][-1]["hhmm"] if rng == "today" and today["series"]["rec"] else None,
                "demo": today.get("demo") if rng == "demo" else None,
                "clock": self.rec.clock().isoformat(timespec="seconds")}

    def alerts_range(self, rng="today"):
        days = self._days(rng)
        rows = [dict(r, date=d["date"]) for d in reversed(days) for r in d["alerts"]]
        f = self.alert_funnel(rows)
        return {"range": rng, "rows": rows, "funnel": f, "simulated_days": rng == "week"}

    # ---------- 설정 › 기준값·단가 화면 ----------
    def settings_view(self):
        sim = self.rec
        preds = []
        for cargo in self.assumptions["std_times"]:
            ds = [d["unload_min"] for d in sim.done_trips if d["cargo"] == cargo]
            avg = round(sum(ds) / len(ds), 1) if ds else None
            preds.append({"cargo": cargo, "pred": round(sim.pred_of(cargo), 1), "n": len(ds), "actual": avg, "base": sim.base_of(cargo),
                          "auto": len(ds) >= 3, "fixed": cargo in (self.assumptions.get("pred_override") or {}),
                          "target": self.assumptions["std_times"][cargo],
                          "under": sum(1 for d in sim.done_trips if d["cargo"] == cargo and d["unload_min"] > self.assumptions["std_times"][cargo] + 1e-9)})
        tsug = []
        for x in preds:
            if x["n"] >= 3 and x["actual"] is not None and x["actual"] - x["target"] >= 2:
                tsug.append({"cargo": x["cargo"], "actual": x["actual"], "target": x["target"], "new": round(x["actual"]), "under": x["under"],
                             "text": f"{x['cargo']}은(는) 실측 {x['actual']:g}분인데 목표 {x['target']:g}분이라 기준미달이 {x['under']}건 반복돼요. 목표를 {round(x['actual'])}분으로?"})
        travel = [sim.travel("YARD", sid) for sid in sim.sec_cfg]
        auto = [{"at": e["at"], "t": e["t"], "by": "자동 계산", "text": f"{e['cargo']} 예측 하차시간 {e['old']} → {e['new']}분 (실측 {e['n']}건)",
                 "impact": "순번 계산에 바로 반영"} for e in sim.pred_log]
        hist = sorted([dict(h, t=-1) for h in self.history] + auto, key=lambda h: h["at"], reverse=True)[:12]
        return {"values": self.assumptions, "preds": preds, "pred_at": sim.hhmm(sim.pred_at), "pred_every": 5, "pred_min_n": 3,
                "unload": self.unload_stats(),
                "travel": [round(min(travel), 1), round(max(travel), 1)], "target_suggestions": tsug, "history": hist,
                "judge": self.judge_counts(), "suggestions": self.feedback()["suggestions"],
                "congestion_hold_min": sim.settings["congestion"]["crit_hold_min"], "clock": sim.hhmm(sim.t)}

    # ---------- 시연 시나리오 (고정 결과) ----------
    def demo_day(self):
        """data/demo.json의 돌발(호퍼 정지 · 긴급 반입 · 복구)을 고정 시드로 하루 끝까지 돌린 결과. 몇 번을 눌러도 같은 숫자."""
        key = json.dumps(self.assumptions, sort_keys=True, ensure_ascii=False)
        if getattr(self, "demo_cache", None) and self.demo_cache["key"] == key:
            return self.demo_cache["day"]
        demo = json.loads((DATA / "demo.json").read_text(encoding="utf-8"))
        r = Runtime(demo["seed"])
        r.assumptions = copy.deepcopy(self.assumptions)
        r._new_sims()
        for a in sorted(demo["actions"], key=lambda a: a["t"]):
            r.advance(a["t"] - r.rec.t)
            if a["kind"] == "down":
                r.sector_down(a["sector_id"])
            elif a["kind"] == "up":
                r.sector_up(a["sector_id"])
            elif a["kind"] == "urgent":
                r.add_urgent()
            elif a["kind"] == "safety":
                x = r.force_safety(a["sector_id"])
                if x:                                    # 시연: 관제 담당자가 40초 뒤 확인했다고 가정
                    r.advance(0.75)
                    r.safety_wall[x["id"]] = r.clock() - 40
                    r.safety_resolve(x["id"], "ack")
        r.advance(r.rec.fc["shift_min"] + 30 - r.rec.t)
        day = r._day_summary(r, demo["label"])
        day["demo"] = demo
        self.demo_cache = {"key": key, "day": day}
        return day

    # ---------- CSV ----------
    @staticmethod
    def _csv(header, rows):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        w.writerows(rows)
        return "﻿" + buf.getvalue()           # 엑셀에서 한글이 깨지지 않게 BOM

    def csv_trips(self, rng="today"):
        a = self.analysis_range(rng)
        rows = [[r["date"], r["vehicle_id"], r["trip"], r["cargo"], r["load_t"], r["yard"], r["call"] or "", r["enter"] or "", r["exit"] or "",
                 r["rec_wait"], ("≥" if r["fifo_lower"] else "") + str(r["fifo_wait"]), r["effect"], r["won"], r["source"], r["state"],
                 "확정" if r["confirmed"] else "예상"] for r in a["records"]]
        conf = [r for r in a["records"] if r["confirmed"]]
        rows.append(["합계(확정)", "", "", "", "", "", "", "", "", "", "", round(sum(r["effect"] for r in conf), 1),
                     sum(r["won"] for r in conf), f"{len(conf)}대", "", ""])
        return self._csv(["날짜", "차량", "회차", "원료", "중량(t)", "대기 시작", "호출", "진입", "이탈", "실제 대기(분)", "FIFO였다면(분)",
                          "효과(분)", "금액(원)", "효과를 만든 것", "상태", "확정"], rows)

    def csv_alerts(self, rng="today"):
        a = self.alerts_range(rng)
        fm = lambda e: "" if not e else e["min"]
        rows = [[r["date"], r["ts"], r["kind"], r["text"], r["suggest"], r["handling"], fm(r["expected"]), fm(r["actual"])] for r in a["rows"]]
        return self._csv(["날짜", "시각", "알림", "무슨 일", "제안", "처리", "예상 효과(분)", "실제 효과(분)"], rows)

    def csv_report(self, rng="today"):
        a = self.analysis_range(rng)
        t = a["total"]
        rows = [["범위", {"week": "최근 7일 (시뮬레이션)", "demo": "시연 시나리오 (고정)"}.get(rng, "오늘 (시뮬레이션)")],
                ["오늘 대기 절감(원)" if rng != "week" else "7일 대기 절감(원)", t["won"]],
                ["효과 합(분)", t["effect_min"]], ["효과 확정 차량", t["vehicles"]], ["단가(원/분)", t["won_per_min"]],
                ["호퍼 유휴 금액 포함", "예" if t["idle_in_cost"] else "아니오"], [], ["절감이 어디서 나왔나", "건수", "효과(분)", "금액(원)"]]
        rows += [[s["source"], s["n"], s["min"], s["won"]] for s in a["sources"]]
        rows += [[], ["시간 지표", "FIFO", "추천"], ["베이 가동률(%)"] + a["times"]["util"], ["작업 지연(건)"] + a["times"]["delays"],
                 ["처리(회)"] + a["times"]["trips"], ["호퍼 유휴(분)"] + a["times"]["idle"], ["평균 대기(분)"] + a["times"]["avg_wait"]]
        if rng == "week":
            rows += [[], ["날짜", "효과(분)", "절감(원)", "FIFO 평균 대기", "추천 평균 대기"]]
            rows += [[d["date"], d["effect_min"], d["won"]] + d["avg_wait"] for d in a["days"]]
        return self._csv(["항목", "값"], rows)


LABELS = {"warn_pct": "혼잡 주의 기준(%)", "crit_pct": "혼잡 기준(%)", "target_wait_min": "목표 대기(분)",
          "call_margin_min": "호출 여유(분)", "delay_factor": "하차 지연 배수", "wait_won_per_hour": "대기 단가(원/시간)",
          "idle_won_per_hour": "호퍼 유휴 단가(원/시간)", "speed_limit_kmh": "구내 제한속도(km/h)",
          "slow_factor": "감속 계수"}
