"""시뮬레이션·엔진 확인 스크립트: python tests/test_sim.py [--samples]

1. 이벤트 형식이 docs/events.md와 일대일로 맞는다
2. 같은 시드 → 같은 결과, 덤프트럭이 여러 번 왔다 갔다 하고 모두 계근대(출)까지 간다
3. 혼잡도·진척도·호출 시점·우선순위 공식을 손계산과 대조
4. 호퍼 정지 → 재배정 + Alert, 긴급 반입 → 순번 1번
5. 카메라 판정: 적재함 각도와 판정 문구, 사진 판정 규칙(세로/가로 비율)
6. (R3) 실제 지도 경로 거리 · 구내 제한속도 30km/h 이동시간 · 왕복 시간
--samples 를 주면 docs/sample_events.jsonl 을 다시 만든다.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from app import engine  # noqa: E402
from app.sim import Sim  # noqa: E402

KEYS = ["ts", "type", "source", "cam_id", "sector_id", "zone", "track_id", "vehicle_id", "payload"]
results = []


def check(name, ok, note=""):
    results.append((name, bool(ok), note))
    print(("통과 " if ok else "실패 ") + name + (f" — {note}" if note else ""))


def doc_types():
    text = (ROOT / "docs" / "events.md").read_text(encoding="utf-8")
    table = text.split("## 종류")[1].split("##")[0]
    return set(re.findall(r"`([A-Z_]+)`", table))


def find_moment(sim, sid, cargo_ok, until=200):
    """sid로 가는 중(이동·대기)이고 다른 호퍼로도 갈 수 있는 차량이 있는 시각까지 돌린다."""
    while sim.t < until:
        sim.step()
        hit = [v for v in sim.vehicles.values() if v["sector"] == sid and v["state"] in ("moving", "queue")
               and cargo_ok(v["cargo"])]
        if hit:
            return [v["id"] for v in hit]
    return []


def scenario(seed=7):
    s = Sim(seed)
    find_moment(s, "A", lambda c: c == "석회석")
    down_at = s.t
    s.sector_down("A")
    s.run_until(down_at + 10)
    s.add_urgent()
    s.run_until(down_at + 40)
    s.sector_up("A")
    s.run_until(420)
    return s, down_at


# 1) 이벤트 형식
s, down_at = scenario()
types = doc_types()
seen = {e["type"] for e in s.events}
check("시뮬레이션 이벤트가 모두 events.md 표에 있음", seen <= types, f"코드에만: {sorted(seen - types)}")
check("모든 이벤트가 같은 키 9개를 가짐", all(list(e.keys()) == KEYS for e in s.events), f"{len(s.events)}건")
check("source 값이 약속된 것만", {e["source"] for e in s.events} <= {"cam", "iot", "sim", "engine", "ui"})

# 2) 결정성과 순환 운행
s2, _ = scenario()
check("같은 시드 → 같은 이벤트 순서", [json.dumps(e, ensure_ascii=False) for e in s.events] ==
      [json.dumps(e, ensure_ascii=False) for e in s2.events], f"{len(s.events)}건")
st = s.state()
trips = {}
for d in s.done_trips:
    trips[d["id"]] = trips.get(d["id"], 0) + 1
check("덤프트럭이 여러 번 왔다 갔다 함", max(trips.values()) >= 3 and st["counts"]["trips_done"] > st["counts"]["fleet"],
      f"트럭 {st['counts']['fleet']}대 · 반입 {st['counts']['trips_done']}회 · {st['counts']['tons_done']}t · 최다 {max(trips.values())}회")
check("근무 끝에 모든 차량이 계근대(출)까지 감", st["counts"]["on_site"] == 0 and not s.pending_arrivals,
      f"종료 {st['clock'][11:16]}")

# 3) 공식 손계산
check("혼잡도 (작업1+대기1)/(하차구2+슬롯2)=50%", engine.congestion_pct(1, 1, 2, 2) == 50)
check("혼잡도 (1+2)/(1+2)=100%", engine.congestion_pct(1, 2, 1, 2) == 100)
check("진척도 경과5/표준10=50%, 초과 시 100%", engine.progress_pct(5, 10) == 50 and engine.progress_pct(12, 10) == 100)
cfg = s.settings
yard = [{"id": "V1", "cargo": "슬러지", "std": 15, "urgent": False, "due_t": 200, "yard_t": 0, "arr_t": 0}]
sec = [{"id": "B", "name": "호퍼 B", "up": True, "bays": 1, "travel_min": 2, "cargo": ["슬러지"],
        "working_remain": [8.0], "pending_eta": []}]
p = engine.plan(yard, sec, 0, cfg)[0]
mg = cfg["call"]["margin_min"]
check(f"호출 시점: 남은 8분 - (이동2+여유{mg:g}) → {8 - 2 - mg:g}분 뒤 호출", abs(p["call_t"] - (8 - 2 - mg)) < 1e-9, f"call_t={p['call_t']}")
sec[0]["working_remain"] = [2.0 + mg]
check(f"남은 {2 + mg:g}분 ≤ 이동2+여유{mg:g} → 지금 호출", engine.plan(yard, sec, 0, cfg)[0]["call_t"] == 0)
yard2 = [dict(yard[0], id="V2", cargo="석회석", due_t=300, yard_t=-40),
         dict(yard[0], id="V3", cargo="석회석", due_t=20),
         dict(yard[0], id="V4", cargo="석회석", urgent=True, due_t=400)]
sec2 = [dict(sec[0], id="A", cargo=["석회석"], working_remain=[])]
order = [x["vehicle_id"] for x in engine.plan(yard2, sec2, 0, cfg)]
check("우선순위: 긴급 → 투입 기한 임박 → 오래 기다림", order == ["V4", "V3", "V2"], str(order))

# 4) 돌발 이벤트
t = Sim(7)
before = find_moment(t, "A", lambda c: c == "석회석")
t.sector_down("A")
t.replan()
after_bad = [v["id"] for v in t.vehicles.values() if v["sector"] == "A" and v["state"] in ("moving", "queue")]
check("호퍼 A 정지 → A로 가던 차량이 빠짐", before and not after_bad and not [p for p in t.plan if p["sector_id"] == "A"],
      f"{t.hhmm(t.t)} 영향 {before}")
check("호퍼 정지 Alert", any(a["kind"] == "호퍼 이상" and a["level"] == "crit" for a in t.alerts))
t.run_until(t.t + 8)
moved = {i: t.vehicles[i]["sector"] or next((p["sector_id"] for p in t.plan if p["vehicle_id"] == i), None) for i in before}
check("석회석 차량이 공용 호퍼 C로 재배정", moved and all(m == "C" for m in moved.values()), str(moved))
uid = t.add_urgent()
t.run_until(t.t + 1.5)
first = t.plan[0] if t.plan else None
called = any(e["type"] == "CALL" and e["vehicle_id"] == uid for e in t.events)
check("긴급 반입 → 순번 1번(또는 바로 호출)", called or (first and first["vehicle_id"] == uid),
      "바로 호출됨" if called else (first["reason"] if first else "순번 없음"))

# 5) 카메라 판정
w = Sim(7)
seen_j = set()
while w.t < 120:
    w.step()
    for v in w.vehicles.values():
        if v["state"] == "working":
            seen_j.add((w.cam_judge(v)[0], w.dump_angle(v) >= 15))
check("하차 중에 적재함 상승(≥15°) 판정이 나옴", ("하차 중 · 적재함 상승", True) in seen_j, str(sorted(seen_j)))
check("상승 전후에 '정차/내림' 판정도 나옴", any(not up for _, up in seen_j))
sys.path.insert(0, str(ROOT / "vision"))
from dump_state import judge  # noqa: E402
raised = judge([(0, 974, 1487, 1517, 0.72), (0, 336, 1493, 1515, 0.64)])   # 팀 제공 사진의 실제 YOLO 결과
lowered = judge([(100, 500, 1500, 1050, 0.8)])
check("사진 판정: 적재함 든 사진 → 하차 중 (비율 0.79)", raised["state"] == "unloading" and raised["ratio"] == 0.79)
check("사진 판정: 낮고 긴 박스 → 적재함 내림", lowered["state"] == "bed_down", f"비율 {lowered['ratio']}")

# 6) R2: FIFO 비교 · 순번 승인/변경 · 알림 확인 · 가정값 재계산 · 피드백 제안
from app.runtime import Runtime  # noqa: E402
fake_clock = [100.0]
rt = Runtime(7, clock=lambda: fake_clock[0])
rt.advance(420)
c = rt.compare()
check("FIFO 대비 추천 평균 대기 감소 (시드 7, 하루)", c["reduction_pct"] > 0,
      f"FIFO {c['fifo']['avg_wait']}분 → 추천 {c['rec']['avg_wait']}분 ({c['reduction_pct']}%) · 반입 {c['fifo']['trips_done']}→{c['rec']['trips_done']}회")
eff_all = rt.effects()
done_eff = [e for e in eff_all.values() if e["confirmed"]]
check("효과 = 차량마다 (FIFO 대기 − 추천 대기), 금액 = 분 × ₩500 · 유휴는 금액 제외",
      done_eff and abs(c["effect"]["min"] - round(sum(e["effect"] for e in done_eff), 1)) < 0.05
      and c["effect"]["won_per_min"] == 500 and c["cost"]["total"] == -int(round(c["effect"]["min"] * 500)) and c["cost"]["idle"] == 0,
      f"{c['effect']['vehicles']}대 · {c['effect']['min']}분 · 절감 ₩{c['cost']['total']:,}")
check("차량별 효과 합 ≈ 두 방식 전체 대기 차이 (같은 차·같은 회차 비교)",
      abs(-c["effect"]["min"] - c["saved_wait_min"]) <= max(15, abs(c["saved_wait_min"]) * 0.5),
      f"효과 합 {-c['effect']['min']}분 vs 전체 대기 차이 {c['saved_wait_min']}분")
pn = rt.panel()
check("알림 패널 합계 = 순번표·운영 분석 효과 합계 (같은 계산)", pn["total"] == rt.compare()["effect"], f"₩{-pn['total']['won']:,}")

rt = Runtime(7, clock=lambda: fake_clock[0])
lime = None
while rt.rec.t < 120 and not lime:
    rt.step()
    lime = next((p for p in rt.rec.plan if rt.rec.vehicles[p["vehicle_id"]]["cargo"] == "석회석" and p["sector_id"]), None)
vid, to = lime["vehicle_id"], ("C" if lime["sector_id"] == "A" else "A")
imp = rt.impact(vid, to)
ch, err = rt.change(vid, to, "호퍼·설비 사정", "A 하차구 점검")
check("순번 변경: 영향 계산·사유 기록·상태 '변경됨'", ch and not err and rt.order_status(vid) == "변경됨"
      and next(p for p in rt.rec.plan if p["vehicle_id"] == vid)["sector_id"] == to
      and rt.store.db.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 1,
      f"{vid} {lime['sector_id']}→{to} · 대기 {imp['delta_min']:+}분 · ₩{imp['cost_won']:,}")
check("변경 사유가 목록에 없으면 거절", rt.change(vid, "A", "아무거나")[1] is not None)
while len(rt.rec.plan) < 3 and rt.rec.t < 200:
    rt.step()
n = rt.approve_all()
others = [p for p in rt.rec.plan if p["vehicle_id"] not in rt.rec.overrides and p["sector_id"]]
check("전체 승인 → 바꾼 차량 빼고 모두 '승인됨'", n == len(others) > 0 and all(rt.order_status(p["vehicle_id"]) == "승인됨" for p in others)
      and rt.order_status(vid) in ("변경됨", "이동 중"), f"{n}대 · 바꾼 차량 {rt.order_status(vid)}")
while not any(a["kind"] != "호출" for a in rt.rec.alerts_all) and rt.rec.t < 300:
    rt.step()
before = rt.alert_list()["summary"]["unacked"]
target = next(a for a in rt.rec.alerts_all if a["kind"] != "호출")
fake_clock[0] += 12.5
rt.ack(target["id"])
s2 = rt.alert_list()["summary"]
check("알림 확인 → 미확인 감소, 평균 확인 시간 기록", s2["unacked"] == before - 1 and s2["avg_ack_sec"] == 12.5, f"{before}→{s2['unacked']}건 · {s2['avg_ack_sec']}초")
check("알림 거르기(종류)", all(a["kind"] == target["kind"] for a in rt.alert_list(kind=target["kind"])["rows"]))
t_before = rt.rec.t
changed = rt.set_assumptions({"std_times": {"슬러지": 18}, "target_wait_min": 20})
types_after = {e["type"] for e in rt.rec.events}
check("가정값 변경 → 같은 시각까지 재계산, 변경·승인·확인 기록 유지",
      changed and rt.rec.t == t_before and rt.history
      and any(e["type"] == "CALL" and e["vehicle_id"] == vid and e["sector_id"] == to for e in rt.rec.events)
      and {"ORDER_CHANGED", "ORDER_APPROVED", "ALERT_ACK", "ASSUMPTIONS_UPDATED"} <= types_after
      and next(a for a in rt.rec.alerts_all if a["id"] == target["id"])["ack"],
      " · ".join(changed))
for i in range(3):            # 같은 호퍼·같은 사유 3번 → 제안
    while True:
        rt.step()
        p = next((p for p in rt.rec.plan if rt.rec.vehicles[p["vehicle_id"]]["cargo"] == "석회석" and p["sector_id"] == "A"
                  and p["vehicle_id"] not in rt.rec.overrides), None)
        if p or rt.rec.t > 400:
            break
    if p:
        rt.change(p["vehicle_id"], "C", "호퍼·설비 사정")
fb = rt.feedback()
sg = fb["suggestions"]
check("같은 사유 3번 반복 → 가정값 조정 제안", sg and sg[0]["cargo"] == "석회석", sg[0]["text"] if sg else str(fb["counts"]))
if sg:
    old = rt.assumptions["std_times"]["석회석"]
    rt.apply_suggestion(sg[0]["key"])
    check("제안 [적용] → 표준시간이 바뀌고 이력 남음", rt.assumptions["std_times"]["석회석"] == sg[0]["new"] != old,
          f"석회석 {old:g} → {rt.assumptions['std_times']['석회석']:g}분")


# ---------- R3: 실제 지도 경로 · 제한속도 ----------
r3 = Sim(7)
v_max = r3.settings["road"]["speed_limit_kmh"] * 1000 / 60          # m/분
check("900m 구간 = 제한속도 30km/h로 1.8분", abs(900 / v_max - 1.8) < 1e-9, f"{900 / v_max:.2f}분 (감속 계수 ×{r3.settings['road']['slow_factor']} 반영하면 {900 / r3.m_per_min():.1f}분)")
smap = json.loads((ROOT / "data" / "site_map.json").read_text(encoding="utf-8"))
legs = {(r["from"], r["to"]): r for r in r3.cfg["roads"]}
need = [("ORE", "GATE"), ("GATE", "YARD"), ("EXIT", "ORE")] + [("YARD", x) for x in "ABC"] + [(x, "EXIT") for x in "ABC"]
check("경로 9구간 모두 OSM 도로 길이(m)가 있음", all(k in legs and legs[k]["length_m"] > 100 and legs[k]["osm_nodes"] >= 2 for k in need)
      and "OpenStreetMap" in smap["attribution"], " · ".join(f"{a}→{b} {legs[(a, b)]['length_m']}m" for a, b in need[:6]))
# 화면 좌표 길이 × 축척(m/px) ≈ 기록된 길이 (지도와 거리가 같은 좌표계)
geo = [abs(sum(((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5 for p, q in zip(r["points"], r["points"][1:])) * r3.mpp - r["length_m"]) <= 3
       for r in r3.cfg["roads"]]
check("화면 경로 길이 × 축척 = 기록된 거리 (±3m)", all(geo), f"축척 1px = {r3.mpp}m")
r3.run_until(240)
calls = [e for e in r3.events if e["type"] == "CALL"]
check("호출 이벤트에 경로 거리·이동시간이 실림",
      calls and all(e["payload"]["route_m"] == legs[("YARD", e["sector_id"])]["length_m"]
                    and abs(e["payload"]["travel_min"] - legs[("YARD", e["sector_id"])]["length_m"] / r3.m_per_min()) < 0.06 for e in calls),
      f"{len(calls)}건 · 예: {calls[0]['payload']['route_m']}m {calls[0]['payload']['travel_min']}분" if calls else "")
check("엔진 호출 시점이 같은 이동시간을 씀", all(abs(x["travel_min"] - r3.travel("YARD", x["id"])) < 1e-9 for x in r3.sector_inputs()))
# 모든 이동이 제한속도 이하 (경로 길이 ÷ 걸린 시간)
r4, speeds = Sim(7), []
while r4.t < 240:
    r4.step()
    for v in r4.vehicles.values():
        if v.get("path") and v["state"] in ("moving", "done") and not v["gone"] and abs(v["path_t0"] - r4.t) < 1e-9:
            speeds.append(v["path_m"] / v["path_dur"] * 60 / 1000)
check("어느 차량도 30km/h를 넘지 않음", speeds and max(speeds) <= r4.settings["road"]["speed_limit_kmh"] + 1e-9,
      f"이동 {len(speeds)}회 · 최고 {max(speeds):.1f}km/h (감속 계수 반영 평균 속도)")
# 왕복: 계근대(출) → 원료야드 → 상차 → 계근대(입) = 경로 시간 + 상차 시간
ret_trips = []
r5 = Sim(7)
while r5.t < 240:
    r5.step()
    for a in r5.pending_arrivals:
        if a.get("ret") and abs(a["ret"]["exit_t"] - r5.t) < 1e-9:
            ret_trips.append(a["arr_t"] - a["ret"]["exit_t"])
legs_min = r5.travel("EXIT", "ORE") + r5.travel("ORE", "GATE")
lo, hi = r5.fc["load_min"]
check("왕복 = 공차 복귀 + 상차 + 적재 반입, 기존 가정 22~38분 범위와 비슷",
      ret_trips and all(legs_min + lo - 1e-6 <= c <= legs_min + hi + 1e-6 for c in ret_trips) and 18 <= sum(ret_trips) / len(ret_trips) <= 42,
      f"{len(ret_trips)}회 · 평균 {sum(ret_trips) / len(ret_trips):.1f}분 (이동 {legs_min:.1f}분 + 상차 {lo}~{hi}분)")
phases = set()
r6 = Sim(7)
while r6.t < 120:
    r6.step()
    phases |= {x["phase"] for x in r6.road_trucks()}
check("구내 도로 위 차량: 공차 복귀·상차·적재 반입이 지도에 나옴", phases == {"return", "loading", "inbound"}, str(sorted(phases)))
from app.runtime import Runtime  # noqa: E402
rt3 = Runtime(7)
rt3.advance(30)
b_before = rt3.rec.travel("YARD", "B")
rt3.set_assumptions({"speed_limit_kmh": 20})
check("가정값: 제한속도 30 → 20km/h면 이동시간이 1.5배", abs(rt3.rec.travel("YARD", "B") / b_before - 1.5) < 0.01,
      f"대기장→호퍼 B {b_before:.1f} → {rt3.rec.travel('YARD', 'B'):.1f}분")
rt3.set_assumptions({"slow_factor": 0.8})
check("감속 계수 1 미만(제한속도 초과)은 거절", rt3.assumptions["slow_factor"] >= 1)


# ---------- R4: 승인 흐름(L2) · 알림 패널 · 하차구 시간표 ----------
ra = Runtime(7)
ra.advance(40)
ra.set_auto_approve(False)
ra.advance(25)
held = sorted(ra.rec.pending)
held_vids = [k.split("#")[0] for k in held]
check("자동 승인 끄면 호출 시각이 돼도 승인 전에는 출발 안 함",
      held and all(ra.rec.vehicles[v]["state"] == "yard" for v in held_vids)
      and all(ra.order_status(v) == "승인 대기 · 호출 시각 지남" for v in held_vids),
      f"승인 기다리는 차량 {len(held)}대")
vid = held_vids[0]
ra.approve(vid)
ra.advance(0.5)
check("승인하면 다음 걸음에 호출 · CALL에 승인 방식 기록", ra.rec.vehicles[vid]["state"] in ("moving", "queue", "working")
      and any(e["type"] == "CALL" and e["vehicle_id"] == vid and e["payload"]["approval"] == "approved" for e in ra.rec.events),
      f"{vid} → {ra.rec.vehicles[vid]['state']}")
before_t = ra.rec.t
ra.set_assumptions({"target_wait_min": 16})
check("재계산해도 승인 모드·승인 기록 유지", ra.rec.require_approval and ra.rec.vehicles[vid]["state"] != "yard"
      and abs(ra.rec.t - before_t) < 1e-9 and ra.rec.pending)
ra.set_auto_approve(True)
ra.advance(1)
check("자동 승인 다시 켜면 밀린 호출이 풀림", not ra.rec.pending)
ra.ignore_alert(next(a["id"] for a in ra.rec.alerts_all if a["kind"] != "호출"))
ra.advance(0.25)
ra.request_approval()
types_r4 = {e["type"] for e in ra.rec.events}
check("CALL_PENDING · APPROVAL_MODE 이벤트가 나옴", {"CALL_PENDING", "APPROVAL_MODE"} <= types_r4)
check("이벤트 종류가 events.md 표와 일대일 (시뮬레이션 + 담당자 행동 + 승인 흐름)", (seen | types_after | types_r4) == types,
      f"문서에만: {sorted(types - seen - types_after - types_r4)} · 코드에만: {sorted((seen | types_after | types_r4) - types)}")
cards = [x for x in ra.panel()["cards"]]
check("알림 카드: 무슨 일 → 제안 → 효과 · 상태",
      cards and all(x["text"] and x["action"] and x["status"] in ("승인 대기", "확인 필요", "실행됨", "확인함") for x in cards)
      and any("effect" in x for x in cards),
      f"{len(cards)}장 · 상태 {sorted({x['status'] for x in cards})}")
rs = Runtime(7)
rs.advance(60)
while not [p for p in rs.rec.plan if p["sector_id"]] and rs.rec.t < 200:   # 순번표에 차량이 있는 시각
    rs.step()
sch = rs.schedule()
bays = {(r["sector_id"], r["bay"]) for r in sch["rows"]}
working = [(v["sector"], v["bay"] + 1, v["id"]) for v in rs.rec.vehicles.values() if v["state"] == "working"]
segs = {(r["sector_id"], r["bay"], g["vehicle_id"]) for r in sch["rows"] for g in r["segs"] if g["kind"] in ("work", "late")}
check("하차구 시간표: 호퍼×하차구 5줄, 하차 중 차량이 제 하차구 줄에 있음",
      len(bays) == sum(s["bays"] for s in rs.rec.cfg["sectors"]) and set(working) <= segs, f"하차 중 {len(working)}대")
planned = {p["vehicle_id"] for p in rs.rec.plan if p["sector_id"]}
resv = {g["vehicle_id"] for r in sch["rows"] for g in r["segs"] if g["kind"] in ("reserved", "pending")}
check("순번표 차량이 시간표에 호출 예약으로 표시", planned and planned <= resv,
      f"순번 {len(planned)}대")
rs.sector_down("B")
rs.advance(1)
sch2 = rs.schedule()
check("호퍼 정지 → 그 줄은 정지 구간, 예약 없음",
      all(not r["up"] and all(g["kind"] in ("stop", "coming") for g in r["segs"]) for r in sch2["rows"] if r["sector_id"] == "B"))

# ---------- R5: 건별 기록 · 효과 출처 · 알림 이력 · 바꿔보기 · 설정 · 구역 ----------
r5 = Runtime(7)
r5.advance(200)
eff5 = r5.effects()
recs = r5.records(eff5)
conf5 = [r for r in recs if r["confirmed"]]
tot5 = r5.effect_total(eff5)
check("건별 기록 효과 합 = 성과·비용 합계", abs(round(sum(r["effect"] for r in conf5), 1) - tot5["min"]) < 0.05 and len(conf5) == tot5["vehicles"],
      f"{len(conf5)}대 · {tot5['min']}분")
srcs = r5.sources(recs)
check("효과를 만든 것별 합 = 합계 (종류 7가지)", len(srcs) == 7 and abs(round(sum(x["min"] for x in srcs), 1) - tot5["min"]) < 0.05
      and sum(x["n"] for x in srcs) == tot5["vehicles"],
      " · ".join(f"{x['source'].split(' ')[0]} {x['n']}" for x in srcs if x["n"]))
csvt = r5.csv_trips("today").splitlines()
check("CSV 합계 줄 = 화면 합계", csvt[-1].split(",")[11] == str(round(sum(r["effect"] for r in conf5), 1))
      and int(csvt[-1].split(",")[12]) == sum(r["won"] for r in conf5), csvt[-1][:60])
check("EFFECT_CONFIRMED 이벤트 = 확정 차량 수", sum(e["type"] == "EFFECT_CONFIRMED" for e in r5.rec.events) == len(r5.confirmed)
      and len(r5.confirmed) >= tot5["vehicles"] - sum(1 for k in r5.confirmed if k not in eff5))
ah5 = r5.alert_history(eff5)
fn5 = r5.alert_funnel(ah5)
check("알림 퍼널: 알림 ≥ 제안 ≥ 실행 ≥ 효과 확정, 예상·실제 합은 목록과 같음",
      fn5["ops"] >= fn5["proposed"] >= fn5["executed"] >= fn5["confirmed"] > 0
      and abs(fn5["actual_min"] - round(sum(r["actual"]["min"] for r in ah5 if r["group"] == "운영" and r["has_vehicles"]
                                           and r["handling"] in ("실행", "변경 저장") and r["actual"] and r["expected"]), 1)) < 0.05,
      f"{fn5['ops']} → {fn5['proposed']} → {fn5['executed']} → {fn5['confirmed']} · 예상 {fn5['expected_min']} / 실제 {fn5['actual_min']}분")
check("알림마다 예상 효과(제안할 때)가 남음", any(r["expected"] for r in ah5 if r["group"] == "운영"))
snap = (json.dumps(r5.assumptions, sort_keys=True, ensure_ascii=False), r5.rec.t, len(r5.rec.events), len(r5.history))
pv = r5.preview({"std_times": {"석회석": 13}, "target_wait_min": 20})
check("바꿔보기는 저장·기록을 건드리지 않음",
      snap == (json.dumps(r5.assumptions, sort_keys=True, ensure_ascii=False), r5.rec.t, len(r5.rec.events), len(r5.history)))
check("바꿔보기 결과: 바뀐 값 2개, 호출이 바뀌는 차량·하차구 비는 시각", len(pv["diff"]) == 2 and pv["rows"] and "schedule" in pv,
      f"차량 {pv['changed_n']}대 · 하차구 {len(pv['bays'])}줄 · 효과 합 {pv['total_old']['min']} → {pv['total_new']['min']}분")
same = r5.preview({"target_wait_min": r5.assumptions["target_wait_min"]})
check("현재 기준과 같으면 미리보기 없음", same["same"] and not same["diff"])
r5.set_assumptions({"std_times": {"석회석": 13}, "target_wait_min": 20})
check("[설정에 반영] = 미리보기와 같은 결과", abs(r5.compare()["effect"]["min"] - pv["total_new"]["min"]) < 0.05,
      f"{r5.compare()['effect']['min']}분")
c_off = r5.compare()["cost"]
r5.set_assumptions({"idle_in_cost": True})
c_on = r5.compare()["cost"]
check("유휴 금액 포함 스위치: 끄면 대기 절감만, 켜면 유휴 금액이 더해짐",
      not c_off["idle_in_cost"] and c_off["idle"] == 0 and c_on["idle_in_cost"] and c_on["total"] == c_on["wait"] + c_on["idle"],
      f"끔 ₩{c_off['total']:,} · 켬 ₩{c_on['total']:,}")
check("변경 이력에 누가·무엇을", r5.history[0]["by"] == "관제 담당자" and "유휴" in r5.history[0]["text"])
rois = json.loads((ROOT / "data" / "rois.json").read_text(encoding="utf-8"))
kinds = {z["type"] for c in rois["cams"] for z in c["zones"]}
check("구역(ROI) JSON: CAM 4개, 하차·대기·위험 다각형", len(rois["cams"]) == 4 and kinds == {"하차", "대기", "위험"}
      and all(len(z["points"]) >= 3 for c in rois["cams"] for z in c["zones"]),
      f"구간 {sum(len(c['zones']) for c in rois['cams'])}개")
r7 = Runtime(7)
r7.WEEK_SEEDS = [1, 2]
wk = r7.analysis_range("week")
check("최근 7일(시뮬레이션): 날짜별 합 = 기간 합계", len(wk["days"]) == 3 and abs(sum(d["effect_min"] for d in wk["days"]) - wk["total"]["effect_min"]) < 0.1,
      f"{len(wk['days'])}일 · {wk['total']['effect_min']}분")

# ---------- R6: 예측값(자동) · 목표 기준값(판정용) · 호출 정책 · 시연 시나리오 ----------
r6 = Runtime(7)
r6.advance(150)
sv6 = r6.settings_view()
pmap = {x["cargo"]: x for x in sv6["preds"]}
ok_pred = all((abs(x["pred"] - x["actual"]) < 0.11) if x["auto"] else x["pred"] == x["base"] for x in sv6["preds"] if not x["fixed"])
check("예측 하차시간 = 실측 평균 (3건 미만이면 기본값)", ok_pred and any(x["auto"] for x in sv6["preds"]) and any(not x["auto"] for x in sv6["preds"]),
      " · ".join(f"{c} {x['pred']}({x['n']}건)" for c, x in list(pmap.items())[:4]))
plan0 = [(p["vehicle_id"], p["sector_id"], round(p["call_t"] or 0, 2)) for p in r6.rec.plan]
j0 = r6.judge_counts()
r6.set_assumptions({"std_times": {"석회석": 20}, "target_wait_min": 30})
plan1 = [(p["vehicle_id"], p["sector_id"], round(p["call_t"] or 0, 2)) for p in r6.rec.plan]
j1 = r6.judge_counts()
check("목표만 바꾸면 순번 그대로, 기준미달 판정만 바뀜 + 영향 문구", plan0 == plan1 and (j1["unload"] < j0["unload"] or j1["wait"] < j0["wait"])
      and r6.history[0]["impact"] == "기준미달 판정만 바뀜 · 순번 그대로",
      f"하차 기준미달 {j0['unload']} → {j1['unload']} · 대기 {j0['wait']} → {j1['wait']}")
pv6 = r6.preview({"pred": {"석회석": pmap["석회석"]["pred"] + 5}})
check("바꿔보기에서 예측값을 바꾸면 순번이 바뀜", not pv6["judge_only"] and pv6["changed_n"] > 0, f"{pv6['changed_n']}대")
r6.set_assumptions({"call_margin_min": 5})
check("호출 정책을 바꾸면 순번 다시 계산 · 영향 문구", r6.history[0]["impact"].startswith("순번 ") or r6.history[0]["impact"].startswith("지금 순번"),
      r6.history[0]["impact"])
r6.set_assumptions({"pred_override": {"석회석": 14}})
check("예측값 고정 → 순번 계산에 그 값", abs(r6.rec.pred_of("석회석") - 14) < 1e-9 and r6.settings_view()["preds"][0]["fixed"])
r6.set_assumptions({"pred_override": {"석회석": None}})
check("[자동으로] → 고정 해제", "석회석" not in r6.assumptions["pred_override"])
check("예측값이 바뀌면 변경 이력에 '자동 계산'", any(h["by"] == "자동 계산" for h in r6.settings_view()["history"]))
demos = [Runtime(7).analysis_range("demo")["total"] for _ in range(3)]
check("시연 시나리오: 3번 눌러도 같은 숫자", demos[0] == demos[1] == demos[2] and demos[0]["vehicles"] > 50,
      f"₩{demos[0]['won']:,} · {demos[0]['effect_min']}분 · {demos[0]['vehicles']}대")

if "--samples" in sys.argv:
    out = ROOT / "docs" / "sample_events.jsonl"
    out.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in s.events) + "\n", encoding="utf-8")
    print(f"샘플 {len(s.events)}건 → {out.relative_to(ROOT)} (호퍼 A 정지 {s.hhmm(down_at)})")

bad = [r for r in results if not r[1]]
print(f"\n{len(results) - len(bad)}/{len(results)} 통과")
sys.exit(1 if bad else 0)
