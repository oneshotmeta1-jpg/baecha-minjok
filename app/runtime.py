"""운영 런타임 — 추천 배정 시뮬레이션과 FIFO 기준선 시뮬레이션을 같은 시계로 함께 돌린다.

- 같은 시드·같은 도착 데이터·같은 돌발 이벤트(호퍼 정지·긴급 반입)를 두 방식에 똑같이 넣고, 지표를 나란히 비교한다.
- 담당자 행동(순번 승인·변경·알림 확인·가정값 변경)을 기록하고 SQLite에 남긴다.
- 가정값을 바꾸면 처음부터 지금 시각까지 같은 행동 기록으로 다시 돌려(재계산) 결과를 바꾼다. 같은 입력이면 항상 같은 결과.
FastAPI 없이도 쓸 수 있게 분리해 tests/test_sim.py에서 직접 검사한다.
"""
import json
import sqlite3
import time
from pathlib import Path

from .sim import Sim, STEP, default_assumptions

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
    def __init__(self, seed=7, db_path=None, assumptions_path=None, clock=time.monotonic):
        self.seed = seed
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
        self.rec = Sim(self.seed, on_event=self._on_event, mode="recommend", assumptions=self.assumptions)
        self.fifo = Sim(self.seed, mode="fifo", assumptions=self.assumptions)
        if self.speed:
            self.rec.speed = self.speed

    def reset(self):
        self.actions = []        # 시뮬레이션에 영향을 주는 행동 {t, kind, ...} — 재계산 때 다시 넣는다
        self.approved = set()    # "차량#회차"
        self.changes = []        # 순번 변경 기록
        self.ignored = set()     # 무시한 제안
        self.acks = {}           # 알림 id → {ack_sec}
        self.ui_events = []      # 담당자 행동 이벤트 — 재계산 때 같은 시각에 다시 기록
        self.alert_wall = {}     # 알림 id → 발생 실제 시각
        self.auto_approve = True # 자동 승인 스위치 (끄면 현장 담당자가 승인해야 호출 — L2)
        self.store.clear()
        self._new_sims()

    def _on_event(self, ev):
        self.store.event(ev)
        if ev["type"] == "ALERT":
            a = ev["payload"]["id"]
            self.alert_wall.setdefault(a, self.clock())
            for x in reversed(self.rec.alerts_all):
                if x["id"] == a:
                    if a in self.acks:              # 재계산 뒤에도 확인 표시 유지
                        x.update(ack=True, ack_at=self.acks[a]["ack_at"], ack_sec=self.acks[a]["ack_sec"])
                    self.store.alert(x)
                    break

    def _ui(self, type_, vehicle_id=None, sector_id=None, payload=None):
        """담당자 행동 이벤트 기록 (ORDER_APPROVED · ORDER_CHANGED · ALERT_ACK · ASSUMPTIONS_UPDATED)."""
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
        self.rec.replan(); self.fifo.replan()

    def step(self):
        self.rec.step()
        self.fifo.step()

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

    def change(self, vid, sid, reason, memo=""):
        v = self.rec.vehicles.get(vid)
        if not v or v["state"] != "yard":
            return None, "대기장에 있는 차량만 바꿀 수 있습니다"
        if reason not in REASONS:
            return None, "변경 사유를 골라 주세요"
        p = next((p for p in self.rec.plan if p["vehicle_id"] == vid), None)
        imp = self.rec.impact(vid, sid)
        if not self.rec.override(vid, sid):
            return None, "이 원료를 받을 수 없는 호퍼입니다"
        self.actions.append({"kind": "override", "t": self.rec.t, "vehicle_id": vid, "sector_id": sid})
        c = {"ts": self.rec.hhmm(self.rec.t), "t": self.rec.t, "vehicle_id": vid, "trip": v["trip"], "cargo": v["cargo"],
             "from": p["sector_id"] if p else None, "to": sid, "reason": reason, "memo": memo.strip()[:200],
             "delta_min": imp["delta_min"], "cost_won": imp["cost_won"]}
        self.changes.append(c)
        self.store.change(c)
        self.approved.discard(self._key(vid))
        self._ui("ORDER_CHANGED", vid, sid, {"from": c["from"], "to": sid, "reason": reason, "memo": c["memo"],
                                             "delta_min": imp["delta_min"], "cost_won": imp["cost_won"]})
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
        return "승인 대기 · 호출 시각 지남" if self._key(vid) in self.rec.pending else "승인 대기"

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
        for k, v in new.items():
            if k == "std_times":
                for cargo, m in v.items():
                    old = self.assumptions["std_times"].get(cargo)
                    if old is not None and float(m) != float(old):
                        self.assumptions["std_times"][cargo] = float(m)
                        changed.append(f"{cargo} 하차 표준 {old:g} → {float(m):g}분")
            elif k in self.assumptions and float(v) != float(self.assumptions[k]):
                if (k == "slow_factor" and float(v) < 1) or (k == "speed_limit_kmh" and not 5 <= float(v) <= 60):
                    continue          # 감속 계수는 1 이상(제한속도를 넘지 않게), 속도는 5~60km/h만
                old = self.assumptions[k]
                self.assumptions[k] = float(v)
                changed.append(f"{LABELS.get(k, k)} {old:g} → {float(v):g}")
        if not changed:
            return []
        self.history.insert(0, {"at": self.rec.hhmm(self.rec.t), "text": " · ".join(changed)})
        del self.history[20:]
        if self.assumptions_path:
            self.assumptions_path.write_text(json.dumps(dict(self.assumptions, history=self.history), ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf-8")
        self.rebuild()
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
                "cost": {"total": -eff["won"], "wait": -eff["won"], "idle": 0,
                         "wait_rate": a["wait_won_per_hour"], "won_per_min": eff["won_per_min"]},
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
        return {"counts": counts, "total": len(self.changes), "suggestions": sugg}

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

    def panel(self):
        eff = self.effects()
        cards = self.alert_cards(eff=eff)
        return {"cards": cards, "total": self.effect_total(eff), "auto_approve": self.auto_approve,
                "counts": {"all": len(cards), "safety": 0, "ops": len(cards),
                           "open": sum(c["status"] in ("승인 대기", "확인 필요") for c in cards)},
                "clock": self.rec.hhmm(self.rec.t)}

    # ---------- 순번·배정: 하차구(베이) 비는 시각 시간표 ----------
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
                "schedule": self.schedule(),
                "sectors": [self.rec.sector_view(sid) for sid in self.rec.sec_cfg]}

    def analysis(self):
        return {"compare": self.compare(), "series": {"rec": self.rec.series, "fifo": self.fifo.series},
                "cargo": self.cargo_table(), "feedback": self.feedback(), "clock": self.rec.clock().isoformat(timespec="seconds")}


LABELS = {"warn_pct": "혼잡 주의 기준(%)", "crit_pct": "혼잡 기준(%)", "target_wait_min": "목표 대기(분)",
          "call_margin_min": "호출 여유(분)", "delay_factor": "하차 지연 배수", "wait_won_per_hour": "대기 단가(원/시간)",
          "idle_won_per_hour": "호퍼 유휴 단가(원/시간)", "speed_limit_kmh": "구내 제한속도(km/h)",
          "slow_factor": "감속 계수"}
