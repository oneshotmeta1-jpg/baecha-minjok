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
check("비용 = 줄어든 대기×대기 단가 + 줄어든 유휴×유휴 단가",
      abs(c["cost"]["total"] - round(c["saved_wait_min"] / 60 * 30000 + c["saved_idle_min"] / 60 * 200000, -2)) <= 100,
      f"{c['cost']}")

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
check("이벤트 종류가 events.md 표와 일대일 (시뮬레이션 + 담당자 행동)", (seen | types_after) == types,
      f"문서에만: {sorted(types - seen - types_after)}")
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

if "--samples" in sys.argv:
    out = ROOT / "docs" / "sample_events.jsonl"
    out.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in s.events) + "\n", encoding="utf-8")
    print(f"샘플 {len(s.events)}건 → {out.relative_to(ROOT)} (호퍼 A 정지 {s.hhmm(down_at)})")

bad = [r for r in results if not r[1]]
print(f"\n{len(results) - len(bad)}/{len(results)} 통과")
sys.exit(1 if bad else 0)
