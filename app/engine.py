"""배정 엔진 — PLAN.md 공식만 담은 순수 함수 모음 (상태를 직접 바꾸지 않음).

혼잡도(%) = (작업 중 + 섹터 대기) / (베이 수 + 허용 대기 슬롯) x 100
진척도(%) = min(작업 경과 / 표준 작업시간, 1) x 100
호출 시점: 섹터에 베이가 비는 시각 - (이동시간 + 여유) <= 지금  →  호출
순번: 긴급 → 마감 임박 → 오래 기다린 순으로, 가장 빨리 비는 적합 섹터에 배정 (greedy)
"""
import heapq


def congestion_pct(working: int, queue: int, bays: int, queue_slots: int) -> int:
    cap = bays + queue_slots
    return round((working + queue) / cap * 100) if cap else 0


def level(pct: float, settings: dict) -> str:
    c = settings["congestion"]
    return "crit" if pct >= c["crit_pct"] else "warn" if pct >= c["warn_pct"] else "ok"


def progress_pct(elapsed: float, std: float) -> int:
    return round(min(elapsed / std, 1) * 100) if std else 100


def remain_min(elapsed: float, std: float) -> float:
    return max(std - elapsed, 0.0)


def priority_key(v: dict, now: float):
    """긴급 먼저 → 마감까지 남은 시간이 짧은 순 → 오래 기다린 순 → 도착순."""
    return (0 if v["urgent"] else 1, v["due_t"] - now, -(now - v["yard_t"]), v["arr_t"], v["id"])


def reason_head(v: dict, now: float, settings: dict) -> str:
    """추천 이유 앞부분: 순번을 정한 기준(긴급 출하 / 마감 임박 / 마감 순) + 오래 기다렸으면 대기 시간."""
    left = int(v["due_t"] - now)
    if v["urgent"]:
        head = "긴급 반입"
    elif left <= settings.get("due_soon_min", 30):
        head = f"투입 기한 임박({left}분 남음)"
    else:
        head = f"투입 기한 순({left}분 남음)"
    waited = int(now - v["yard_t"])
    if waited >= settings["wait"]["target_min"]:
        head += f" · 오래 기다림({waited}분)"
    return head


def plan(yard: list, sectors: list, now: float, settings: dict) -> list:
    """대기장 차량의 하역 순번(순서·섹터·호출 시각)을 만든다.

    yard: 대기장 차량 [{id, cargo, std, urgent, due_t, yard_t, arr_t}]
    sectors: [{id, name, up, bays, travel_min, cargo, working_remain:[분], pending_eta:[(도착까지 분, std)]}]
    반환: [{rank, vehicle_id, sector_id, call_t, start_t, reason}] — call_t <= now 이면 지금 호출.
    """
    margin = settings["call"]["margin_min"]
    heaps = {}
    for s in sectors:
        if not s["up"]:
            continue
        h = sorted(s["working_remain"])[: s["bays"]]
        h += [0.0] * (s["bays"] - len(h))
        heapq.heapify(h)
        for eta, std in sorted(s["pending_eta"]):          # 이미 호출된 차량이 베이를 먼저 차지
            t = heapq.heappop(h)
            heapq.heappush(h, max(t, eta) + std)
        heaps[s["id"]] = h

    out = []
    for v in sorted(yard, key=lambda v: priority_key(v, now)):
        fits = [s for s in sectors if v["cargo"] in s["cargo"]]
        if v.get("forced"):                                   # 담당자가 바꾼 호퍼만 본다
            fits = [s for s in fits if s["id"] == v["forced"]] or fits
        cand = []
        for s in fits:
            if s["id"] not in heaps:
                continue
            start = max(heaps[s["id"]][0], s["travel_min"])  # 이동 전에는 도착할 수 없음
            cand.append((start, s["id"], s))
        head = reason_head(v, now, settings)
        if not cand:
            down = "·".join(s["name"] for s in fits) or "적합 섹터 없음"
            out.append({"vehicle_id": v["id"], "sector_id": None, "call_t": None, "start_t": None,
                        "reason": f"{head} · 배정 보류({down} 정지)"})
            continue
        cand.sort(key=lambda c: (c[0], c[1]))
        # 원래 받는 호퍼(목록 첫 번째)를 우선한다. 다른 호퍼가 '확실히' 빠를 때만 돌린다 —
        # 조금 빠르다고 공용 호퍼로 보내면 그 호퍼 전용 원료(연료)가 밀린다.
        gain = settings["call"].get("switch_gain_min", 0)
        home = next((c for c in cand if fits and c[1] == fits[0]["id"]), None)
        if home and not v.get("forced") and home[0] - cand[0][0] < gain:
            cand.remove(home)
            cand.insert(0, home)
        start, sid, s = cand[0]
        heapq.heapreplace(heaps[sid], start + v["std"])
        call_t = now + max(0.0, start - s["travel_min"] - margin)
        if v.get("forced"):
            tail = f"담당자 변경 → {s['name']}"
        elif len(fits) == 1:
            tail = f"{s['name']} 전용 원료"
        else:
            other = [c for c in cand[1:] if c[1] != sid]
            gap = f" ({int(other[0][0] - start)}분 먼저)" if other and other[0][0] - start >= 1 else ""
            tail = f"{s['name']}가 가장 빨리 빔{gap}"
        out.append({"vehicle_id": v["id"], "sector_id": sid, "call_t": call_t, "start_t": now + start,
                    "reason": f"{head} · {tail}"})
    for i, p in enumerate(out, 1):
        p["rank"] = i
    return out


def plan_fifo(yard: list, sectors: list, now: float) -> list:
    """기준선(기존 방식 가정): 도착순으로, 원료마다 정해진 호퍼(목록의 첫 호퍼) 앞에 바로 가서 줄을 선다.
    정해진 호퍼가 서 있으면 다음 적합 호퍼로. 미리 부르지 않고 도착하면 바로 보낸다."""
    out = []
    for i, v in enumerate(sorted(yard, key=lambda v: (v["arr_t"], v["id"])), 1):
        fits = [s for s in sectors if v["cargo"] in s["cargo"] and s["up"]]
        sid = fits[0]["id"] if fits else None
        out.append({"vehicle_id": v["id"], "sector_id": sid, "call_t": now if sid else None, "start_t": None,
                    "reason": "도착순 · 정해진 호퍼" if sid else "도착순 · 호퍼 정지로 보류", "rank": i})
    return out


def change_impact(yard: list, sectors: list, now: float, settings: dict, vehicle_id: str, sector_id: str) -> dict:
    """순번 하나를 다른 호퍼로 바꿨을 때 추천 대비 영향: 대기장 차량 전체의 하차 시작 시각 합의 차이."""
    base = {p["vehicle_id"]: p for p in plan(yard, sectors, now, settings)}
    forced = [dict(v, forced=sector_id) if v["id"] == vehicle_id else v for v in yard]
    alt = {p["vehicle_id"]: p for p in plan(forced, sectors, now, settings)}
    delta = 0.0
    for vid, p in base.items():
        a = alt.get(vid)
        if p["start_t"] is not None and a and a["start_t"] is not None:
            delta += a["start_t"] - p["start_t"]
    rate = settings["cost"]["wait_won_per_hour"]
    return {"delta_min": round(delta, 1), "cost_won": int(round(delta / 60 * rate, -2)),
            "start_rec": base.get(vehicle_id, {}).get("start_t"), "start_alt": alt.get(vehicle_id, {}).get("start_t")}
