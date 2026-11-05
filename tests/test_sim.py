"""시뮬레이션·엔진 확인 스크립트: python tests/test_sim.py [--samples]

1. 이벤트 형식이 docs/events.md와 일대일로 맞는다
2. 같은 시드 → 같은 결과, 덤프트럭이 여러 번 왔다 갔다 하고 모두 계근대(출)까지 간다
3. 혼잡도·진척도·호출 시점·우선순위 공식을 손계산과 대조
4. 호퍼 정지 → 재배정 + Alert, 긴급 반입 → 순번 1번
5. 카메라 판정: 적재함 각도와 판정 문구, 사진 판정 규칙(세로/가로 비율)
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
check("이벤트 종류가 events.md 표와 일대일", seen == types, f"문서에만: {sorted(types - seen)}, 코드에만: {sorted(seen - types)}")
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
check("호출 시점: 남은 8분 - (이동2+여유1) → 5분 뒤 호출", abs(p["call_t"] - 5) < 1e-9, f"call_t={p['call_t']}")
sec[0]["working_remain"] = [3.0]
check("남은 3분 ≤ 이동2+여유1 → 지금 호출", engine.plan(yard, sec, 0, cfg)[0]["call_t"] == 0)
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

if "--samples" in sys.argv:
    out = ROOT / "docs" / "sample_events.jsonl"
    out.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in s.events) + "\n", encoding="utf-8")
    print(f"샘플 {len(s.events)}건 → {out.relative_to(ROOT)} (호퍼 A 정지 {s.hhmm(down_at)})")

bad = [r for r in results if not r[1]]
print(f"\n{len(results) - len(bad)}/{len(results)} 통과")
sys.exit(1 if bad else 0)
