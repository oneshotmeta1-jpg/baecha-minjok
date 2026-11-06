"""포항제철소 실제 지도(OpenStreetMap)로 시나리오 맵과 경로 거리를 만든다.

  py tools/build_map.py            # tools/osm_cache/*.osm → data/site_map.json + data/sectors.json(roads·좌표)
  py tools/build_map.py --fetch    # OSM에서 다시 받기 (api.openstreetmap.org, 두 구역)
  py tools/build_map.py --png out.png   # 확인용 그림

- 지도: © OpenStreetMap contributors, ODbL 1.0. 제철소 안 도로망은 OSM에 공개된 수준이고 실제 운영 도로와 다를 수 있다.
- 시나리오 지점(원료야드·계근대·대기장·호퍼 A/B/C)의 위치는 **가정**이다. 실제 소결공장 호퍼 위치가 아니다.
- 경로 거리는 OSM 도로를 따라 잰 실제 길이(m)다. 이동시간은 시뮬레이터가 설정값(제한속도·감속 계수)으로 계산한다.
"""
import heapq
import json
import math
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "tools" / "osm_cache"
BOXES = {"west": (129.360, 35.990, 129.410, 36.030), "east": (129.385, 35.985, 129.440, 36.045)}

W, H = 1600, 900
# 시나리오 지점 (위도, 경도) — 가정 배치. 도로 위 가장 가까운 지점으로 붙인다.
ANCHORS = {
    "ORE": (36.03200, 129.40180),   # 원료야드 상차 (원료 부두 옆 북쪽 도로, 가정)
    "GATE": (36.03000, 129.39500),  # 계근대(입) — 원료야드에서 내려오는 간선
    "YARD": (36.02794, 129.39268),  # 차량 대기장 — 간선 교차로 동쪽 빈터
    "A": (36.02649, 129.38687),     # 호퍼 A (부원료)
    "B": (36.02620, 129.39120),     # 호퍼 B (재활용)
    "C": (36.02389, 129.38905),     # 호퍼 C (연료·공용)
    "EXIT": (36.02173, 129.38411),  # 계근대(출) → 서쪽 강변 도로로 원료야드 복귀
}
LEGS = [("ORE", "GATE"), ("GATE", "YARD"), ("YARD", "A"), ("YARD", "B"), ("YARD", "C"),
        ("A", "EXIT"), ("B", "EXIT"), ("C", "EXIT"), ("EXIT", "ORE")]
# 시나리오 표시 상자 크기(px)와 도로에서 띄우는 방향
BOX = {"GATE": (96, 50), "EXIT": (96, 50), "YARD": (210, 120), "ORE": (230, 110), "A": (150, 92), "B": (150, 92), "C": (130, 92)}
OFFSET = {"ORE": (0, -62), "GATE": (58, 0), "YARD": (118, 6), "A": (-82, -6), "B": (84, 4), "C": (80, 30), "EXIT": (-58, 30)}
DRIVE = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "service",
         "primary_link", "secondary_link", "tertiary_link", "living_street", "road"}
SHOW = DRIVE | {"track"}
M_LAT = 111_320.0


def fetch():
    CACHE.mkdir(parents=True, exist_ok=True)
    for name, (a, b, c, d) in BOXES.items():
        url = f"https://api.openstreetmap.org/api/0.6/map?bbox={a},{b},{c},{d}"
        req = urllib.request.Request(url, headers={"User-Agent": "baecha-minjok-hackathon/0.1"})
        (CACHE / f"{name}.osm").write_bytes(urllib.request.urlopen(req, timeout=120).read())
        print("받음", name)


def load():
    nodes, ways = {}, {}
    for f in sorted(CACHE.glob("*.osm")):
        r = ET.parse(f).getroot()
        for n in r.iter("node"):
            nodes[n.get("id")] = (float(n.get("lat")), float(n.get("lon")))
        for w in r.iter("way"):
            ways[w.get("id")] = ({t.get("k"): t.get("v") for t in w.iter("tag")}, [nd.get("ref") for nd in w.iter("nd")])
    return nodes, ways


def rdp(pts, eps):
    if len(pts) < 3:
        return pts
    (x1, y1), (x2, y2) = pts[0], pts[-1]
    L = math.hypot(x2 - x1, y2 - y1) or 1e-9
    i, dm = 0, -1
    for k in range(1, len(pts) - 1):
        d = abs((y2 - y1) * pts[k][0] - (x2 - x1) * pts[k][1] + x2 * y1 - y2 * x1) / L
        if d > dm:
            i, dm = k, d
    if dm <= eps:
        return [pts[0], pts[-1]]
    return rdp(pts[:i + 1], eps)[:-1] + rdp(pts[i:], eps)


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    if "--fetch" in argv:
        fetch()
    nodes, ways = load()
    lat_c = sum(a[0] for a in ANCHORS.values()) / len(ANCHORS)
    kx = M_LAT * math.cos(math.radians(lat_c))

    def m(p):  # (lat, lon) → 동쪽·남쪽 미터 (임시 원점)
        return ((p[1] - 129.0) * kx, (37.0 - p[0]) * M_LAT)

    # 도로 그래프
    adj = {}
    for tags, ids in ways.values():
        if tags.get("highway") not in DRIVE or tags.get("access") in ("no", "private_no"):
            continue
        ow = tags.get("oneway") == "yes"
        for a, b in zip(ids, ids[1:]):
            if a in nodes and b in nodes:
                (x1, y1), (x2, y2) = m(nodes[a]), m(nodes[b])
                d = math.hypot(x2 - x1, y2 - y1)
                adj.setdefault(a, []).append((b, d))
                adj.setdefault(b, [])
                if not ow:
                    adj[b].append((a, d))

    def comp(start):
        seen, st = {start}, [start]
        while st:
            u = st.pop()
            for v, _ in adj[u]:
                if v not in seen:
                    seen.add(v)
                    st.append(v)
        return seen

    def nearest(p, pool):
        x, y = m(p)
        return min(pool, key=lambda n: (m(nodes[n])[0] - x) ** 2 + (m(nodes[n])[1] - y) ** 2)

    # 가장 큰 연결 도로망 안에서만 붙인다 (끊긴 조각에 붙으면 경로가 없음)
    first = nearest(ANCHORS["YARD"], list(adj))
    pool = list(comp(first))
    snap = {k: nearest(p, pool) for k, p in ANCHORS.items()}

    def dijkstra(s, t):
        dist, prev, pq = {s: 0.0}, {}, [(0.0, s)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == t:
                break
            if d > dist.get(u, 1e18):
                continue
            for v, w in adj[u]:
                nd = d + w
                if nd < dist.get(v, 1e18):
                    dist[v], prev[v] = nd, u
                    heapq.heappush(pq, (nd, v))
        path = [t]
        while path[-1] != s:
            path.append(prev[path[-1]])
        return path[::-1], dist[t]

    legs = {}
    for a, b in LEGS:
        path, d = dijkstra(snap[a], snap[b])
        legs[(a, b)] = (path, d)

    # 화면 틀: 경로 전체 + 여백, 16:9
    allm = [m(nodes[n]) for path, _ in legs.values() for n in path]
    x0, x1 = min(p[0] for p in allm), max(p[0] for p in allm)
    y0, y1 = min(p[1] for p in allm), max(p[1] for p in allm)
    pad = 260
    x0, x1, y0, y1 = x0 - pad, x1 + pad, y0 - pad, y1 + pad
    mpp = max((x1 - x0) / W, (y1 - y0) / H)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    ox, oy = cx - W * mpp / 2, cy - H * mpp / 2

    def px(p):
        x, y = m(p)
        return [round((x - ox) / mpp, 1), round((y - oy) / mpp, 1)]

    def inside(q, margin=200):
        return -margin <= q[0] <= W + margin and -margin <= q[1] <= H + margin

    # 배경 레이어
    layers = {"roads": [], "rail": [], "water": [], "coast": [], "pier": [], "industrial": [], "building": [], "labels": []}
    for tags, ids in ways.values():
        pts = [px(nodes[i]) for i in ids if i in nodes]
        if len(pts) < 2 or not any(inside(q, 0) for q in pts):
            continue
        pts = rdp(pts, 1.2)
        hw = tags.get("highway")
        if hw in SHOW:
            cls = "main" if hw in ("motorway", "trunk", "primary", "secondary", "primary_link", "secondary_link") else (
                "mid" if hw.startswith("tertiary") or hw == "unclassified" else "minor")
            layers["roads"].append({"c": cls, "p": pts, **({"n": tags["name"]} if tags.get("name") and cls != "minor" else {})})
        elif tags.get("railway") == "rail":
            layers["rail"].append({"p": pts})
        elif tags.get("natural") == "coastline":
            layers["coast"].append({"p": pts})
        elif tags.get("natural") == "water" or tags.get("waterway") in ("riverbank",):
            layers["water"].append({"p": pts})
        elif tags.get("waterway") in ("river", "stream"):
            layers["coast"].append({"p": pts})
        elif tags.get("man_made") in ("pier", "breakwater"):
            layers["pier"].append({"p": pts})
        elif tags.get("landuse") == "industrial":
            layers["industrial"].append({"p": pts, **({"n": tags["name"]} if tags.get("name") else {})})
        elif "building" in tags and len(pts) >= 3:
            layers["building"].append({"p": pts})

    # 경로 (px) — 지점 상자 중심 ↔ 도로 붙는 점은 짧은 진입로로 잇는다
    road_px = {k: px(nodes[n]) for k, n in snap.items()}
    site = {}
    for k, (dx, dy) in OFFSET.items():
        rx, ry = road_px[k]
        site[k] = [round(rx + dx, 1), round(ry + dy, 1)]

    def seg_len(pts):
        return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:])) * mpp

    roads = []
    for (a, b), (path, d) in legs.items():
        pts = [px(nodes[n]) for n in path]
        pts = rdp(pts, 0.6)
        full = [site[a]] + pts + [site[b]]
        roads.append({"from": a, "to": b, "points": full, "length_m": round(seg_len(full)),
                      "road_m": round(d), "osm_nodes": len(path)})

    def rect(c, wh):
        w, h = wh
        return [[round(c[0] - w / 2), round(c[1] - h / 2)], [round(c[0] + w / 2), round(c[1] - h / 2)],
                [round(c[0] + w / 2), round(c[1] + h / 2)], [round(c[0] - w / 2), round(c[1] + h / 2)]]

    out = {"_note": "© OpenStreetMap contributors (ODbL 1.0). tools/build_map.py가 만든 파일. 좌표는 px(1600x900), 1px = mpp 미터.",
           "attribution": "지도 © OpenStreetMap contributors (ODbL)",
           "width": W, "height": H, "mpp": round(mpp, 4),
           "origin_note": "북쪽이 위. 위도 {:.4f} 기준 등장방형 투영".format(lat_c),
           "anchors_latlon": ANCHORS, "layers": layers}
    (ROOT / "data" / "site_map.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    sec = json.loads((ROOT / "data" / "sectors.json").read_text(encoding="utf-8"))
    sec["map"] = {"width": W, "height": H, "mpp": round(mpp, 4), "source": "data/site_map.json"}
    sec["roads"] = roads
    for z in sec["zones"]:
        z["poly"] = rect(site[z["id"]], BOX[z["id"]])
    for s in sec["sectors"]:
        s["poly"] = rect(site[s["id"]], BOX[s["id"]])
        s.pop("travel_min", None)
    sec["ore"] = {"id": "ORE", "name": "원료야드 (상차)", "poly": rect(site["ORE"], BOX["ORE"])}
    (ROOT / "data" / "sectors.json").write_text(json.dumps(sec, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"mpp {mpp:.3f} m/px · 틀 {W * mpp:.0f} x {H * mpp:.0f} m · 레이어 " +
          ", ".join(f"{k} {len(v)}" for k, v in layers.items()))
    for r in roads:
        print(f"  {r['from']:>4} → {r['to']:<4} {r['length_m']:>5} m (도로 {r['road_m']} m)")

    if "--png" in argv:
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(im)
        for b in layers["building"]:
            d.polygon([tuple(p) for p in b["p"]], outline=(180, 180, 180))
        for r in layers["roads"]:
            d.line([tuple(p) for p in r["p"]], fill={"main": (220, 80, 80), "mid": (200, 170, 0)}.get(r["c"], (120, 120, 120)), width=2)
        for c in layers["coast"] + layers["water"]:
            d.line([tuple(p) for p in c["p"]], fill=(60, 120, 220), width=3)
        for r in roads:
            d.line([tuple(p) for p in r["points"]], fill=(0, 160, 60), width=5)
        for k, c in site.items():
            d.rectangle([c[0] - 8, c[1] - 8, c[0] + 8, c[1] + 8], fill=(0, 0, 0))
            d.text((c[0] + 10, c[1] - 6), k, fill=(0, 0, 0))
        if "--junctions" in argv:
            for n, nb in adj.items():
                if len({v for v, _ in nb}) >= 3 and n in pool:
                    q = px(nodes[n])
                    if 0 <= q[0] <= W and 0 <= q[1] <= H:
                        d.ellipse([q[0] - 3, q[1] - 3, q[0] + 3, q[1] + 3], fill=(200, 0, 200))
                        d.text((q[0] + 4, q[1] + 2), "%.5f,%.5f" % nodes[n], fill=(150, 0, 150))
        im.save(argv[argv.index("--png") + 1])


if __name__ == "__main__":
    main(sys.argv[1:])
