"""R1 확인 스크립트: py tests/test_r1.py

1. 이벤트 형식이 docs/events.md와 일대일로 맞는다 (I1)
2. 같은 시드로 두 번 돌리면 같은 결과, 모든 차량이 출구까지 간다 (I3)
3. 혼잡도·진척도·호출 시점 공식을 손계산과 대조 (I4)
4. 섹터 정지 → 재배정 + Alert, 긴급 출하 → 순번 1번 (I8)
--samples 를 주면 docs/sample_events.jsonl 을 다시 만든다.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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


def scenario(seed=7):
    s = Sim(seed)
    s.run_until(51)
    s.sector_down("A")
    s.run_until(75)
    s.add_urgent()
    s.run_until(110)
    s.sector_up("A")
    s.run_until(480)
    return s


# 1) 이벤트 형식
s = scenario()
types = doc_types()
seen = {e["type"] for e in s.events}
check("이벤트 종류가 events.md 표와 일대일", seen == types, f"문서에만: {sorted(types - seen)}, 코드에만: {sorted(seen - types)}")
check("모든 이벤트가 같은 키 9개를 가짐", all(list(e.keys()) == KEYS for e in s.events), f"{len(s.events)}건")
check("source 값이 약속된 것만", {e["source"] for e in s.events} <= {"cam", "iot", "sim", "engine", "ui"})

# 2) 결정성과 완주
s2 = scenario()
check("같은 시드 → 같은 이벤트 순서", [json.dumps(e, ensure_ascii=False) for e in s.events] ==
      [json.dumps(e, ensure_ascii=False) for e in s2.events], f"{len(s.events)}건")
st = s.state()
check("모든 차량이 출구까지 감", st["counts"]["exited"] == st["counts"]["total"],
      f"{st['counts']['exited']}/{st['counts']['total']}대, 종료 {st['clock'][11:16]}")

# 3) 공식 손계산
check("혼잡도 (작업1+대기1)/(베이2+슬롯2)=50%", engine.congestion_pct(1, 1, 2, 2) == 50)
check("혼잡도 (2+2)/(1+2)=133%", engine.congestion_pct(2, 2, 1, 2) == 133)
check("진척도 경과10/표준25=40%, 초과 시 100%", engine.progress_pct(10, 25) == 40 and engine.progress_pct(30, 25) == 100)
cfg = s.settings
yard = [{"id": "V1", "cargo": "후판", "std": 35, "urgent": False, "due_t": 200, "yard_t": 0, "arr_t": 0}]
sec = [{"id": "B", "name": "섹터 B", "up": True, "bays": 1, "travel_min": 3, "cargo": ["후판"],
        "working_remain": [10.0], "pending_eta": []}]
p = engine.plan(yard, sec, 0, cfg)[0]
check("호출 시점: 남은 10분 - (이동3+여유2) → 5분 뒤 호출", abs(p["call_t"] - 5) < 1e-9, f"call_t={p['call_t']}")
sec[0]["working_remain"] = [4.0]
check("남은 4분 ≤ 이동3+여유2 → 지금 호출", engine.plan(yard, sec, 0, cfg)[0]["call_t"] == 0)
yard2 = [dict(yard[0], id="V2", cargo="열연코일", due_t=300, yard_t=-40),
         dict(yard[0], id="V3", cargo="열연코일", due_t=50),
         dict(yard[0], id="V4", cargo="열연코일", urgent=True, due_t=400)]
sec2 = [dict(sec[0], id="A", cargo=["열연코일"], working_remain=[])]
order = [x["vehicle_id"] for x in engine.plan(yard2, sec2, 0, cfg)]
check("우선순위: 긴급 → 마감 임박 → 오래 기다림", order == ["V4", "V3", "V2"], str(order))

# 4) 돌발 이벤트
t = Sim(7)
t.run_until(51)   # TR-0417(열연코일)이 섹터 A로 이동 중인 시각
before = [v["id"] for v in t.vehicles.values() if v["sector"] == "A" and v["state"] in ("moving", "queue")]
t.sector_down("A")
t.replan()
after_bad = [v["id"] for v in t.vehicles.values() if v["sector"] == "A" and v["state"] in ("moving", "queue")]
planned_a = [p for p in t.plan if p["sector_id"] == "A"]
check("섹터 A 정지 → 이동·대기 차량이 A에서 빠짐", before and not after_bad and not planned_a, f"영향 {len(before)}대: {before}")
check("섹터 정지 Alert", any(a["kind"] == "섹터 이상" and a["level"] == "crit" for a in t.alerts))
t.run_until(60)
moved = [t.vehicles[i]["sector"] or next((p["sector_id"] for p in t.plan if p["vehicle_id"] == i), None) for i in before]
check("영향 차량(열연)이 섹터 C로 재배정", moved and all(m == "C" for m in moved), str(dict(zip(before, moved))))
uid = t.add_urgent()
t.run_until(61.5)
check("긴급 출하 → 순번 1번", t.plan and t.plan[0]["vehicle_id"] == uid, t.plan[0]["reason"] if t.plan else "순번 없음")

if "--samples" in sys.argv:
    out = ROOT / "docs" / "sample_events.jsonl"
    out.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in s.events) + "\n", encoding="utf-8")
    print(f"샘플 {len(s.events)}건 → {out.relative_to(ROOT)}")

bad = [r for r in results if not r[1]]
print(f"\n{len(results) - len(bad)}/{len(results)} 통과")
sys.exit(1 if bad else 0)
