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
        start, sid, s = cand[0]
        heapq.heapreplace(heaps[sid], start + v["std"])
        call_t = now + max(0.0, start - s["travel_min"] - margin)
        if len(fits) == 1:
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
