"""알림 (R13): 누구에게 · 무엇을 · 언제 — 받음 · 읽음 · 조치까지 기록

- 라우팅 표(ROUTES) 한 곳: 이벤트 종류 → 받는 사람(역할 · 담당 하역장 · 알림 동의 · 내 차).
- 저장: notifications 테이블(계정 DB와 같은 SQLite). 보낸 · 받은(화면에 도착) · 읽은 · 조치 시각(실제 시각)과 조치 이름.
- 묶음: 같은 사람에게 같은 내용(dedup 키)은 1분 안에 다시 보내지 않는다.
- 안전 경고: 조치(멈춤 · 확인)되거나 경고가 닫힐 때까지 30초마다 다시(최대 3번). 닫히면 바로 멈춘다.
- 우선순위: 0 안전 > 1 호출 > 2 승인 > 3 운영.
- 푸시 내용에는 이름 · 전화번호를 넣지 않는다(차량번호 · 하역장 · 베이만).
"""
import json
import time

PRI = {"안전": 0, "호출": 1, "승인": 2, "운영": 3}
DEDUP_SEC = 60
RESEND_SEC, RESEND_MAX = 30, 3
OPS_KINDS = {"혼잡", "하차 지연", "대기 초과", "호퍼 이상"}          # 운영 알림 — 동의한 현장 담당자만
REQUIRED_KINDS = {"호출 응답 없음"}                                  # 현장 담당자에게 동의와 관계없이


def hname(sid):
    return "호퍼 " + sid if sid and sid != "YARD" else ("대기장" if sid == "YARD" else "")


class Notifier:
    def __init__(self, store, clock=time.time):
        """store = app.auth.AuthStore (계정 · 담당 하역장 · 동의를 읽고, 같은 DB에 알림을 저장)"""
        self.store, self.clock = store, clock
        self.db = store.db if store else None
        self.rec_id, self.idx = None, 0
        if self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS notifications (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT, priority INTEGER,
                    title TEXT, body TEXT, link TEXT, event_type TEXT, ref TEXT, dedup TEXT, sector_id TEXT, vehicle_id TEXT,
                    sim_ts TEXT, sent_at REAL, last_sent REAL, repeat INTEGER DEFAULT 0, delivered_at REAL, read_at REAL, acted_at REAL, action TEXT);
                CREATE INDEX IF NOT EXISTS n_user ON notifications(user_id, id);
            """)
            self.db.commit()

    # ---------- 받는 사람 ----------
    def users(self, role=None):
        if not self.db:
            return []
        rows = self.db.execute("SELECT id, role, hoppers, vehicle, consent FROM users WHERE status='active'" + (" AND role=?" if role else ""),
                               (role,) if role else ()).fetchall()
        return [{"id": r[0], "role": r[1], "hoppers": json.loads(r[2] or "[]"), "vehicle": json.loads(r[3]) if r[3] else None,
                 "consent": json.loads(r[4] or "{}")} for r in rows]

    def driver_of(self, plate):
        return [u for u in self.users("driver") if (u["vehicle"] or {}).get("plate") == plate]

    def field_of(self, sid, ops=False):
        return [u for u in self.users("field") if sid in u["hoppers"] and (not ops or u["consent"].get("ops"))]

    def route(self, ev, rt=None):
        """이벤트 하나 → [(user, kind, title, body, link, ref, dedup)] — 라우팅 표"""
        typ, p, vid, sid = ev["type"], ev.get("payload") or {}, ev.get("vehicle_id"), ev.get("sector_id")
        out = []
        if typ == "CALL":
            bay = p.get("bay")
            for u in self.driver_of(vid):
                out.append((u, "호출", f"{hname(sid)} {bay}번 베이로 이동하세요" if bay else f"{hname(sid)}로 이동하세요",
                            f"{vid} · 약 {p.get('travel_min', '—')}분 · 후진 진입", "call", f"{vid}#call", f"call|{vid}|{sid}|{bay}"))
        elif typ == "SAFETY_ALERT":
            sa = p.get("id")
            title = f"{hname(sid)} {p.get('rule', '덤프 반경')} 안 작업자"
            body = f"{vid} 하역을 멈추세요 · 작업자 약 {p.get('dist_m', '—')}m"
            for u in self.driver_of(vid):
                out.append((u, "안전", "하역을 멈추세요", f"{hname(sid)} 베이 {p.get('bay', '')} · 뒤에 작업자 약 {p.get('dist_m', '—')}m", "safety", sa, f"safety|{sa}"))
            for u in self.field_of(sid) + self.users("control"):
                out.append((u, "안전", title, body, "home", sa, f"safety|{sa}"))
        elif typ == "CALL_PENDING":
            for u in self.field_of(sid):
                out.append((u, "승인", f"승인 요청 · {vid} → {hname(sid)}", p.get("reason", ""), "seq", f"{vid}#approve", f"pending|{vid}|{sid}"))
        elif typ == "ORDER_CHANGED":
            to = p.get("to")
            for u in self.driver_of(vid):
                out.append((u, "호출", f"하역장이 {hname(to)}(으)로 바뀌었어요", f"{vid} · 사유: {p.get('reason', '')}", "wait", f"{vid}#change", f"change|{vid}|{to}"))
        elif typ == "DRIVER_STOPPED":
            for u in self.field_of(sid):
                out.append((u, "안전", f"운전자 멈춤 확인 · {vid}", f"{hname(sid)} 안전 경고에 운전자가 [멈췄어요]를 눌렀어요", "home", p.get("id"), f"stopped|{p.get('id')}"))
        elif typ in ("YARD_DOWN", "YARD_UP"):
            down = typ == "YARD_DOWN"
            title = f"{hname(sid)} 하역장 {'정지' if down else '복구'}"
            for u in self.field_of(sid) + self.users("control"):
                out.append((u, "운영", title, "배정 차량은 다른 호퍼로 재배정돼요" if down else "다시 호출을 받아요", "home", f"{sid}#{typ}", f"yard|{sid}|{typ}"))
            if rt is not None:          # 그 하역장으로 배정 · 이동 중이던 운전자
                plates = {p_["vehicle_id"] for p_ in rt.rec.plan if p_["sector_id"] == sid} | {v["id"] for v in rt.rec.vehicles.values() if v["sector"] == sid and v["state"] in ("moving", "queue")}
                for plate in plates:
                    for u in self.driver_of(plate):
                        out.append((u, "운영", title, "대기가 길어질 수 있어요 · 바뀌면 다시 알려 드려요" if down else "곧 호출될 수 있어요", "wait", f"{sid}#{typ}", f"yard|{sid}|{typ}"))
        elif typ == "ALERT":
            kind = p.get("kind")
            if kind in OPS_KINDS or kind in REQUIRED_KINDS:
                at = sid or "YARD"                        # 대기 초과처럼 하역장이 없는 알림 = 대기장 담당
                for u in self.field_of(at, ops=kind not in REQUIRED_KINDS):
                    out.append((u, "운영", f"{hname(at)} · {kind}", p.get("text", ""), "alert", p.get("id"), f"alert|{kind}|{at}|{vid}"))
        return out

    # ---------- 보내기 ----------
    def send(self, u, kind, title, body, link, ref, dedup, ev, now=None):
        now = now if now is not None else self.clock()
        last = self.db.execute("SELECT sent_at FROM notifications WHERE user_id=? AND dedup=? ORDER BY id DESC LIMIT 1", (u["id"], dedup)).fetchone()
        if last and now - last[0] < DEDUP_SEC:
            return None                                       # 1분 안의 같은 내용은 묶음
        cur = self.db.execute("""INSERT INTO notifications (user_id,kind,priority,title,body,link,event_type,ref,dedup,sector_id,vehicle_id,sim_ts,sent_at,last_sent)
                                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (u["id"], kind, PRI[kind], title, body, link, ev["type"], ref, dedup, ev.get("sector_id"), ev.get("vehicle_id"), ev.get("ts"), now, now))
        return cur.lastrowid

    def feed(self, rt, now=None):
        """런타임에 새로 쌓인 이벤트를 읽어 알림을 만든다(서버 ticker가 매번 부름). 재계산으로 시뮬레이션이 새로 만들어지면 이미 지난 이벤트는 다시 보내지 않는다."""
        if not self.db:
            return 0
        evs = rt.rec.events
        if self.rec_id != id(rt.rec):
            self.rec_id, self.idx = id(rt.rec), len(evs)
            return 0
        n = 0
        for ev in evs[self.idx:]:
            for r in self.route(ev, rt):
                if self.send(*r, ev, now=now):
                    n += 1
            if ev["type"] == "SAFETY_ACK":                    # 관제 · 현장이 확인/오탐지 → 그 안전 알림은 조치됨
                self.act_ref((ev.get("payload") or {}).get("id"), "확인" if (ev.get("payload") or {}).get("how") == "확인" else "오탐지", now=now)
            elif ev["type"] == "CALL_ACK" and ev.get("source") == "app":
                self.act_ref(f"{ev['vehicle_id']}#call", "이동 시작", now=now)
            elif ev["type"] == "DRIVER_STOPPED":
                self.act_ref((ev.get("payload") or {}).get("id"), "멈춤", now=now, roles=("driver",))
            elif ev["type"] == "ORDER_APPROVED":
                self.act_ref(f"{ev['vehicle_id']}#approve", "승인", now=now)
            elif ev["type"] == "ALERT_ACK":
                self.act_ref((ev.get("payload") or {}).get("alert_id"), "확인", now=now)
        self.idx = len(evs)
        self.db.commit()
        return n

    def tick(self, rt, now=None):
        """안전 경고 재전송: 열린 경고인데 아직 조치가 없으면 30초마다(최대 3번). 닫힌 경고는 멈춤."""
        if not self.db:
            return []
        now = now if now is not None else self.clock()
        open_ids = {x["id"] for x in rt.rec.safety if x["status"] == "open"}
        rows = self.db.execute("SELECT id, ref, repeat, last_sent FROM notifications WHERE kind='안전' AND acted_at IS NULL AND event_type='SAFETY_ALERT'").fetchall()
        again = []
        for nid, ref, rep, last in rows:
            if ref not in open_ids:
                self.db.execute("UPDATE notifications SET acted_at=?, action=COALESCE(action,'경고 해제') WHERE id=?", (now, nid))
            elif rep < RESEND_MAX and now - last >= RESEND_SEC:
                self.db.execute("UPDATE notifications SET repeat=repeat+1, last_sent=?, delivered_at=NULL WHERE id=?", (now, nid))
                again.append(nid)
        self.db.commit()
        return again

    # ---------- 받음 · 읽음 · 조치 ----------
    def act_ref(self, ref, action, now=None, roles=None):
        if not ref or not self.db:
            return
        now = now if now is not None else self.clock()
        q = "UPDATE notifications SET acted_at=?, action=? WHERE ref=? AND acted_at IS NULL"
        args = [now, action, ref]
        if roles:
            q += " AND user_id IN (SELECT id FROM users WHERE role IN (%s))" % ",".join("?" * len(roles))
            args += list(roles)
        self.db.execute(q, args)

    def mark(self, user_id, ids, field, now=None):
        if not self.db or field not in ("delivered_at", "read_at"):
            return 0
        now = now if now is not None else self.clock()
        q = f"UPDATE notifications SET {field}=? WHERE user_id=? AND {field} IS NULL"
        args = [now, user_id]
        if ids != "all":
            ids = [int(i) for i in ids][:200]
            if not ids:
                return 0
            q += " AND id IN (%s)" % ",".join("?" * len(ids))
            args += ids
        n = self.db.execute(q, args).rowcount
        if field == "read_at":       # 읽었으면 받은 것으로도
            self.db.execute("UPDATE notifications SET delivered_at=COALESCE(delivered_at, read_at) WHERE user_id=?", (user_id,))
        self.db.commit()
        return n

    def inbox(self, user_id, limit=20):
        if not self.db:
            return {"unread": 0, "items": [], "new": []}
        cols = ["id", "kind", "priority", "title", "body", "link", "ref", "sim_ts", "sent_at", "repeat", "delivered_at", "read_at", "acted_at", "action"]
        rows = [dict(zip(cols, r)) for r in self.db.execute(f"SELECT {','.join(cols)} FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))]
        unread = self.db.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL", (user_id,)).fetchone()[0]
        for r in rows:
            r["at"] = (r["sim_ts"] or "")[11:16]
        new = [r for r in rows if r["delivered_at"] is None]
        return {"unread": unread, "items": rows, "new": sorted(new, key=lambda r: (r["priority"], -r["id"]))[:3]}

    def log(self, limit=200):
        """관제용: 알림 기록(받는 사람 · 받음 · 읽음 · 조치까지 초)"""
        if not self.db:
            return []
        rows = self.db.execute("""SELECT n.id, u.name, u.role, n.kind, n.title, n.sim_ts, n.sent_at, n.delivered_at, n.read_at, n.acted_at, n.action, n.repeat
                                  FROM notifications n JOIN users u ON u.id=n.user_id ORDER BY n.id DESC LIMIT ?""", (limit,)).fetchall()
        sec = lambda a, b: round(a - b, 1) if a and b else None   # noqa: E731
        return [{"id": r[0], "to": r[1], "role": r[2], "kind": r[3], "title": r[4], "at": (r[5] or "")[11:19],
                 "recv_sec": sec(r[7], r[6]), "read_sec": sec(r[8], r[6]), "act_sec": sec(r[9], r[6]), "action": r[10], "repeat": r[11]} for r in rows]
