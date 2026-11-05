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
        self.approved.add(self._key(vid))
        self._ui("ORDER_APPROVED", vid, p["sector_id"], {"rank": p["rank"], "reason": p["reason"]})
        return True

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
        return "승인됨" if self._key(vid) in self.approved else "승인 대기"

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
    def compare(self):
        r, f = self.rec.metrics(), self.fifo.metrics()
        a = self.assumptions
        saved_wait = f["wait_total"] - r["wait_total"]
        saved_idle = f["idle_min"] - r["idle_min"]
        cost_wait = saved_wait / 60 * a["wait_won_per_hour"]
        cost_idle = saved_idle / 60 * a["idle_won_per_hour"]
        dec = sum(1 for _ in self.approved) + len(self.changes)
        return {"fifo": f, "rec": r,
                "reduction_pct": round((f["avg_wait"] - r["avg_wait"]) / f["avg_wait"] * 100) if f["avg_wait"] else 0,
                "saved_wait_min": round(saved_wait, 1), "saved_idle_min": round(saved_idle, 1),
                "cost": {"wait": int(round(cost_wait, -2)), "idle": int(round(cost_idle, -2)),
                         "total": int(round(cost_wait + cost_idle, -2)),
                         "wait_rate": a["wait_won_per_hour"], "idle_rate": a["idle_won_per_hour"]},
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

    # ---------- 화면용 묶음 ----------
    def state(self):
        s = self.rec.state()
        s["running"] = self.running
        for q in s["queue"]:
            q["status"] = self.order_status(q["vehicle_id"])
        s["compare_brief"] = {"reduction_pct": self.compare()["reduction_pct"]}
        return s

    def dock(self):
        return {"compare": self.compare(), "assumptions": self.assumptions, "history": self.history[:5],
                "changes": list(reversed(self.changes[-10:])), "reasons": REASONS,
                "sectors": [self.rec.sector_view(sid) for sid in self.rec.sec_cfg]}

    def analysis(self):
        return {"compare": self.compare(), "series": {"rec": self.rec.series, "fifo": self.fifo.series},
                "cargo": self.cargo_table(), "feedback": self.feedback(), "clock": self.rec.clock().isoformat(timespec="seconds")}


LABELS = {"warn_pct": "혼잡 주의 기준(%)", "crit_pct": "혼잡 기준(%)", "target_wait_min": "목표 대기(분)",
          "call_margin_min": "호출 여유(분)", "delay_factor": "하차 지연 배수", "wait_won_per_hour": "대기 단가(원/시간)",
          "idle_won_per_hour": "호퍼 유휴 단가(원/시간)"}
