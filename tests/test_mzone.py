"""모바일 3D 매핑 — py tests/test_mzone.py

운전자: 호출 화면 = 내 경로 하나(전체 맵) · 도착 화면 = 내 베이 · 내 원료(구역 맵, 다른 차 번호 없음)
현장 담당자: 홈 = 담당 구역 3D 매핑(web/map3d.js, PC 구역 카드와 같은 상태 규칙)
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
os.environ["DOCK_MEMORY"] = "1"

from app.runtime import Runtime  # noqa: E402

results = []


def check(name, ok, note=""):
    results.append((name, bool(ok)))
    print(("통과 " if ok else "실패 ") + name + (f" — {note}" if note else ""))


r = Runtime(7)
r.advance(60)
st = r.state()
z = r.zone_view("A", st)
sec = next(x for x in st["sectors"] if x["id"] == "A")
check("구역 데이터: 하차 중 차마다 핀(#추적 ID · 베이) · 빈 베이는 empty/reserved · 베이별 원료",
      len([i for i in z["items"] if i.get("bay")]) == len(sec["working"]) and all(i["id"].startswith("#") for i in z["items"])
      and set(z["bays"]) | {i["bay"] for i in z["items"] if i.get("bay")} == {1, 2, 3}
      and [b["cargo"] for b in z["bay_list"]] == ["석회석", "사문암", "규석"], str(z["bay_list"]))
x = next((r.force_safety(s) for s in "ABC" if any(v["state"] == "working" and v["sector"] == s for v in r.rec.vehicles.values())), None)
st = r.state()
zs = r.zone_view(x["sector_id"], st)
it = next(i for i in zs["items"] if i.get("plate") == x["vehicle_id"])
check("안전 경고 → danger · 베이 stop · 작업자 위치 · 관제 확인 전(ack=false)", it["state"] == "danger" and zs["bays"][x["bay"] + 1] == "stop"
      and it["worker"] and it["ack"] is False)
r.safety_seen(x["id"])
it2 = next(i for i in r.zone_view(x["sector_id"], r.state())["items"] if i.get("plate") == x["vehicle_id"])
check("관제 확인 → ack=true(깜빡임 멈춤)", it2["ack"] is True)
f = r.field_view(["A", "C"])
check("현장 담당자: 담당 하역장만 구역 데이터", [q["id"] for q in f["zones"]] == ["A", "C"])

# 운전자: 호출 · 이동 중에 배정 구역(베이 상태만, 다른 차 번호 없음)
P = "99가 9999"
r2 = Runtime(7)
r2.advance(20)
r2.driver_trip(P, "석회석", 24)
for _ in range(400):
    r2.advance(0.25)
    if r2.rec.vehicles[P]["state"] == "moving":
        break
d = r2.driver_view(P)
zd = d.get("zone")
check("운전자 호출 화면: 배정 구역 · 내 베이 · 베이 3칸(원료 · 상태)", zd and zd["id"] == d["vehicle"]["sector_id"] and zd["my_bay"] == d["vehicle"]["bay"]
      and len(zd["bays"]) == 3 and all({"n", "cargo", "state"} == set(b) for b in zd["bays"]), str(zd))
check("내 베이 원료 = 내가 싣고 온 원료(섞임 방지 확인)", next(b for b in zd["bays"] if b["n"] == zd["my_bay"])["cargo"] == "석회석")
check("운전자 구역 데이터에 다른 차 번호 · 추적 ID 없음", "items" not in zd and all("plate" not in b for b in zd["bays"]))

m = (ROOT / "web" / "m" / "index.html").read_text(encoding="utf-8")
check("기사 앱: 호출 화면 = 전체 맵(내 경로) · 도착 화면 = 구역 맵 · map3d.js 먼저 로드",
      "function routeMap(v)" in m and "function zoneMap()" in m and "routeMap(v)+" in m and "zoneMap()" in m.split("function arrive(v)")[1][:900]
      and m.find('<script src="/web/map3d.js"></script>') < m.find("<script>\n"))
check("현장 홈: 담당 구역 3D 매핑(Map3D.mount) · 구역 고르기 · 다시 그려도 같은 카드 옮겨 붙임",
      "Map3D.mount(el,{zone:z.id" in m and "data-fz" in m and "slot.replaceWith(FM3.el)" in m)

bad = [q for q in results if not q[1]]
print(f"\n{len(results) - len(bad)}/{len(results)} 통과")
sys.exit(1 if bad else 0)
