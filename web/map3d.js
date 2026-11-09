/* map3d.js — 호퍼 상세 화면용 3D 매핑 카드 (A안: 탐지 목록 위)
 * 사용:
 *   const m = Map3D.mount(document.getElementById('map3d'), { zone: 'A' });
 *   m.update([{ id: '#3', bay: 1, state: 'unload', plate: '86사 8564', detail: '3:20 경과 / 예측 6분' }]);
 *   m.setZone('B');            // 구역 전환 (A, B, C, W=대기장)
 *   m.onHover = id => {...};   // 핀에 마우스 올리면 탐지 목록 행 강조 등에 사용
 *   m.highlight('#3');         // 탐지 목록 행 hover 시 핀 강조
 */
(function () {
  const COL = { blue: '#2F6FDE', green: '#16A06E', gray: '#8A94A3', orange: '#F08A24', red: '#E5383B', purple: '#7B4FD6' };
  const MAT = { '석회석': '#D2CBBE', '사문암': '#7F9478', '규석': '#EEE7D4', '밀스케일': '#4F4845', '슬러지': '#7A5E43', '집진더스트': '#8F8C89', '분코크스': '#2E2E31', '무연탄': '#222224' };
  const ZONES = {
    A: { name: '호퍼 A', cam: 'CAM-01', mats: ['석회석', '사문암', '규석'] },
    B: { name: '호퍼 B', cam: 'CAM-02', mats: ['밀스케일', '슬러지', '집진더스트'] },
    C: { name: '호퍼 C', cam: 'CAM-03', mats: ['분코크스', '무연탄', '석회석'] },
    W: { name: '대기장', cam: 'CAM-04', waiting: true }
  };
  // 상태 → 색, 짧은 멘트(카드가 작아서 1줄만), 링 모양
  const STATE = {
    call:     { c: COL.blue,   t: '호출·이동',   dash: 1, solid: 1 },
    enter:    { c: COL.blue,   t: '진입' },
    unload:   { c: COL.green,  t: '하역 중' },
    delay:    { c: COL.orange, t: '작업 지연' },
    done:     { c: COL.gray,   t: '하역 완료' },
    danger:   { c: COL.red,    t: '작업자 · 덤프 반경', lg: '안전 경고', dash: 1, solid: 1 },
    reverse:  { c: COL.red,    t: '후진 경로 · 작업자', lg: '안전 경고', dash: 1, solid: 1 },
    wait:     { c: COL.gray,   t: '' },
    approval: { c: COL.blue,   t: '승인 대기', dash: 1 },
    urgent:   { c: COL.purple, t: '긴급 입고', solid: 1 },
    overdue:  { c: COL.orange, t: '대기 초과' },
    unknown:  { c: COL.gray,   t: '미등록 차량', dash: 1 }
  };
  const BAYSTATE = { stop: { c: COL.red, t: '안전 중지' }, empty: { c: COL.gray, t: '비어 있음' }, reserved: { c: COL.blue, t: '도착 예정' } };
  const PRIORITY = ['danger', 'reverse', 'delay', 'overdue', 'urgent', 'unload', 'enter', 'call', 'approval', 'unknown', 'done', 'wait'];

  function shade(hex, f) { const n = parseInt(hex.slice(1), 16); const m = v => Math.max(0, Math.min(255, Math.round(v * f))); return '#' + [n >> 16, (n >> 8) & 255, n & 255].map(v => m(v).toString(16).padStart(2, '0')).join(''); }
  function esc(s) { return String(s ?? '').replace(/[&<>"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch])); }

  // ── 아이소메트릭 그리기 ─────────────────────────
  // 카드가 가로로 넓어서 옆으로 펼친 3D 시점(빗각 투영)을 사용
  const S = 25, OX = 300, OY = 200;
  const P = (x, y, z = 0) => [OX + (x * 0.95 - y * 0.30) * S, OY + (x * 0.12 + y * 0.31) * S - z * S * 0.72];
  const VB = (() => { let a = [1e9, 1e9, -1e9, -1e9]; for (const x of [0, 30]) for (const y of [0, 22]) for (const z of [-0.6, 4.6]) { const [sx, sy] = P(x, y, z); a = [Math.min(a[0], sx), Math.min(a[1], sy), Math.max(a[2], sx), Math.max(a[3], sy)]; } const m = 12; return [a[0] - m, a[1] - 40, a[2] - a[0] + 2 * m, a[3] - a[1] + 40 + m]; })();
  function D() { this.o = []; }
  D.prototype.poly = function (a, fill, st = 'none', sw = 1, dash = '', op = null) { this.o.push(`<polygon points="${a.map(q => P(...q).map(v => v.toFixed(1)).join(',')).join(' ')}" fill="${fill}" stroke="${st}" stroke-width="${sw}" ${dash ? `stroke-dasharray="${dash}"` : ''} ${op != null ? `fill-opacity="${op}"` : ''} stroke-linejoin="round"/>`); };
  D.prototype.box = function (x, y, z, w, d, h, col, o = {}) { const st = o.st || shade(col, 0.82); this.poly([[x + w, y, z], [x + w, y + d, z], [x + w, y + d, z + h], [x + w, y, z + h]], o.r || shade(col, 0.78), st, 0.8); this.poly([[x, y + d, z], [x + w, y + d, z], [x + w, y + d, z + h], [x, y + d, z + h]], o.l || shade(col, 0.9), st, 0.8); this.poly([[x, y, z + h], [x + w, y, z + h], [x + w, y + d, z + h], [x, y + d, z + h]], o.t || col, st, 0.8); };
  D.prototype.pile = function (x, y, w, d, h, col) { const A = [x, y, 0], B = [x + w, y, 0], C = [x + w, y + d, 0], E = [x, y + d, 0], M = [x + w / 2, y + d / 2, h]; this.poly([A, B, M], shade(col, 1.08), shade(col, .85), .6); this.poly([A, E, M], shade(col, 1.12), shade(col, .85), .6); this.poly([B, C, M], shade(col, .76), shade(col, .7), .6); this.poly([E, C, M], shade(col, .94), shade(col, .8), .6); };
  D.prototype.ring = function (cx, cy, r, c, dash, blink) { const a = []; for (let i = 0; i < 48; i++) { const t = i / 48 * Math.PI * 2; a.push([cx + r * Math.cos(t), cy + r * Math.sin(t), 0.02]); } if (blink) { this.o.push('<g class="m3-ping">'); this.poly(a, 'none', c, 5); this.o.push('</g><g class="m3-blink">'); } this.poly(a, c, c, 4, dash ? '12 8' : '', 0.16); if (blink) this.o.push('</g>'); };
  D.prototype.line = function (a, b, c, w = 2, dash = '') { const [x1, y1] = P(...a), [x2, y2] = P(...b); this.o.push(`<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${c}" stroke-width="${w}" ${dash ? `stroke-dasharray="${dash}"` : ''}/>`); };
  D.prototype.truck = function (x, y, dir, o = {}) {
    const bed = o.bed || '#E2B33C', cab = o.cab || '#6B7280';
    if (dir === 'x') { this.box(x, y, .15, 2.8, 1.7, 1.15, bed); this.box(x + 2.9, y, 0, 1.4, 1.7, 1.5, cab); return; }
    if (dir === 'ny') { this.box(x, y, 0, 1.7, 1.4, 1.5, cab); this.box(x, y + 1.5, .15, 1.7, 2.8, 1.15, bed); return; }
    this.box(x, y, .15, 1.7, 2.8, 1.15, bed); this.box(x, y + 2.9, 0, 1.7, 1.4, 1.5, cab);
  };
  D.prototype.worker = function (x, y) { const [sx, sy] = P(x, y, 0); this.o.push(`<ellipse cx="${sx}" cy="${sy}" rx="6" ry="3.5" fill="#A61B1B"/><rect x="${sx - 6}" y="${sy - 30}" width="12" height="30" fill="#D62828"/><ellipse cx="${sx}" cy="${sy - 30}" rx="6" ry="3.5" fill="#F06262"/>`); };
  const HATCH = `<defs><pattern id="m3h" width="14" height="14" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="14" height="14" fill="#E5383B" fill-opacity=".1"/><line x1="0" y1="0" x2="0" y2="14" stroke="#E5383B" stroke-width="4" stroke-opacity=".35"/></pattern></defs>`;

  // 위치 계산: 베이 n(1~3) / 대기장 칸 n(1~12) / 직접 좌표(gx, gy)
  const bayX = i => 1.5 + (i - 1) * 9;
  const slot = n => { const k = (n - 1) % 6, r = n > 6 ? 1 : 0; return [2.5 + k * 4.2, r ? 13.4 : 2.4, r]; };
  function anchorOf(it, zone) {
    if (it.gx != null) return [it.gx, it.gy];
    if (zone.waiting) { const [x, y] = slot(it.slot || 1); return [x + 1.8, y + 3.1]; }
    if (it.state === 'call') return [3.6, 17.4];
    return [bayX(it.bay || 1) + 4.25, 10.6];
  }

  const isAlert = it => (it.state === 'danger' || it.state === 'reverse');
  const isBlink = it => isAlert(it) && !it.ack;
  function drawYard(zone, items, bays) {
    const d = new D();
    const blinkBay = new Set(items.filter(isBlink).map(it => it.bay));
    d.box(0, 0, -.6, 30, 22, .6, '#FAFBFC', { r: '#D9DEE5', l: '#E6EAF0', st: '#D5DAE1' });
    d.poly([[0, 15.6, .01], [30, 15.6, .01], [30, 19.8, .01], [0, 19.8, .01]], '#E3E7ED');
    d.line([0, 17.7, .02], [30, 17.7, .02], '#fff', 3, '14 10');
    zone.mats.forEach((m, i) => {
      const x0 = bayX(i + 1), b = bays[i + 1];
      d.poly([[x0, 3.9, .02], [x0 + 8.4, 3.9, .02], [x0 + 8.4, 14.4, .02], [x0, 14.4, .02]], '#fff', '#D7DCE3', 2);
      if (b === 'stop') { const bl = blinkBay.has(i + 1); if (bl) d.o.push('<g class="m3-blink">'); d.poly([[x0, 3.9, .03], [x0 + 8.4, 3.9, .03], [x0 + 8.4, 14.4, .03], [x0, 14.4, .03]], 'url(#m3h)', COL.red, bl ? 6 : 4); if (bl) d.o.push('</g>'); }
      if (b === 'empty' || b === 'reserved') { const c = b === 'empty' ? COL.gray : COL.blue; d.poly([[x0 + .4, 8.4, .03], [x0 + 8, 8.4, .03], [x0 + 8, 14, .03], [x0 + .4, 14, .03]], c, c, 3, '12 8', .08); }
    });
    items.forEach(it => { const st = STATE[it.state] || STATE.wait; const [ax, ay] = it.state === 'reverse' && it.worker ? it.worker : anchorOf(it, zone); d.ring(ax, ay, it.state === 'reverse' ? 1.6 : 2.9, st.c, st.dash, isBlink(it)); });
    d.box(1, .4, 0, 28, 3, 4.2, '#FFFFFF', { r: '#D4D9E0', l: '#E9ECF1', st: '#CDD3DB' });
    d.box(1, .4, 4.2, 28, .45, .32, COL.blue, { t: '#3A7BEA', l: '#2A62C8', r: '#2457B5', st: '#2457B5' });
    zone.mats.forEach((m, i) => d.pile(bayX(i + 1) + 1.7, 4.5, 5.1, 3.6, 2.5, MAT[m]));
    items.forEach(it => {
      if (it.state === 'call') d.truck(1.6, 16.55, 'x', { bed: COL.blue, cab: '#2457B5' });
      else if (it.kind !== 'person' && it.bay) d.truck(bayX(it.bay) + 3.4, 8.7, 'y');
      if (it.worker) d.worker(it.worker[0], it.worker[1]);
    });
    return d.o.join('');
  }
  function drawWaiting(items) {
    const d = new D();
    d.box(0, 0, -.6, 30, 22, .6, '#FAFBFC', { r: '#D9DEE5', l: '#E6EAF0', st: '#D5DAE1' });
    d.poly([[0, 9.2, .01], [30, 9.2, .01], [30, 12.8, .01], [0, 12.8, .01]], '#E3E7ED');
    for (let n = 1; n <= 12; n++) { const [x, y] = slot(n); d.poly([[x, y, .02], [x + 3.6, y, .02], [x + 3.6, y + 6.2, .02], [x, y + 6.2, .02]], '#fff', '#CBD2DB', 2); }
    d.line([2.5, 9, .03], [27.5, 9, .03], '#F2B84B', 4); d.line([2.5, 13, .03], [27.5, 13, .03], '#F2B84B', 4);
    items.forEach(it => { const st = STATE[it.state] || STATE.wait; const [ax, ay] = anchorOf(it, ZONES.W); d.ring(ax, ay, 2.6, st.c, st.dash); });
    items.slice().sort((a, b) => (a.slot || 0) - (b.slot || 0)).forEach(it => { const [x, y, r] = slot(it.slot || 1); d.truck(x + .95, y + 1, r ? 'ny' : 'y'); });
    return d.o.join('');
  }

  // ── 스타일 ─────────────────────────
  const CSS = `
.m3{background:#fff;border-radius:14px;border:1px solid #ECEEF1;font-family:inherit;overflow:hidden}
.m3-h{display:flex;align-items:center;gap:8px;height:40px;padding:0 14px}
.m3-t{font-size:14px;font-weight:700;color:#1F2937}
.m3-s{font-size:12px;color:#8A94A3}
.m3-sw{margin-left:auto;display:flex;align-items:center;gap:6px;font-size:12px;color:#4B5563;cursor:pointer;user-select:none}
.m3-sw input{display:none}
.m3-tr{width:30px;height:17px;border-radius:9px;background:#D1D5DB;position:relative;transition:background .15s}
.m3-tr:after{content:"";position:absolute;top:2px;left:2px;width:13px;height:13px;border-radius:50%;background:#fff;transition:left .15s}
.m3-sw input:checked + .m3-tr{background:#1F2937}
.m3-sw input:checked + .m3-tr:after{left:15px}
.m3-b{display:flex;gap:10px;padding:0 12px 10px;height:var(--m3h,180px)}
.m3.off .m3-b{display:none}
.m3-map{position:relative;flex:1;min-width:0;background:#F6F7F9;border-radius:10px}
.m3-map svg{position:absolute;inset:0;width:100%;height:100%}
.m3-pin{position:absolute;transform:translate(-50%,calc(-100% - 6px));white-space:nowrap;font-size:11px;font-weight:700;line-height:1;padding:4px 7px 4px 6px;border-radius:10px;background:#fff;color:#1F2937;box-shadow:0 1px 3px rgba(15,23,42,.18);display:flex;align-items:center;gap:4px;cursor:default;transition:transform .1s}
.m3-pin i{width:6px;height:6px;border-radius:50%;flex:none}
.m3-pin.solid{color:#fff}
.m3-pin.below{transform:translate(-50%,8px)}
.m3-pin.below.hl{transform:translate(-50%,8px) scale(1.12)}
.m3-pin.solid i{display:none}
.m3-pin.hl{transform:translate(-50%,calc(-100% - 6px)) scale(1.12);z-index:3;outline:2px solid #1F2937}
.m3-badge{position:absolute;transform:translate(-50%,-50%);width:16px;height:16px;border-radius:50%;color:#fff;font-size:10px;font-weight:800;display:flex;align-items:center;justify-content:center}
.m3-tag{position:absolute;transform:translate(-50%,0);font-size:10.5px;font-weight:700;padding:2px 6px;border-radius:8px;background:rgba(255,255,255,.92);border:1px solid}
.m3-lg{width:96px;flex:none;display:flex;flex-direction:column;gap:6px;justify-content:center;font-size:11.5px;color:#4B5563}
.m3-lg div{display:flex;align-items:center;gap:6px}
.m3-lg b{margin-left:auto;color:#1F2937}
.m3-lg i{width:8px;height:8px;border-radius:50%}
.m3-blink{animation:m3p 1s ease-in-out infinite}
.m3-ping{transform-box:fill-box;transform-origin:center;animation:m3ping 1s ease-out infinite}
.m3-pin.blink{animation:m3bg 1s ease-in-out infinite}
.m3-tag.blink{animation:m3p 1s ease-in-out infinite;background:#FDECEC}
.m3-al{display:none;align-items:center;gap:5px;font-size:12px;font-weight:700;color:#fff;background:#E5383B;border-radius:10px;padding:3px 9px;cursor:pointer;border:0;font-family:inherit}
.m3-al.on{display:flex}
.m3-al.blink{animation:m3bg 1s ease-in-out infinite}
@keyframes m3p{0%,100%{opacity:1}50%{opacity:.35}}
@keyframes m3ping{0%{transform:scale(1);opacity:.8}100%{transform:scale(1.7);opacity:0}}
@keyframes m3bg{0%,100%{background:#E5383B}50%{background:#A51D22}}
@media (prefers-reduced-motion: reduce){.m3-blink,.m3-pin.blink,.m3-tag.blink,.m3-al.blink{animation:none}.m3-ping{display:none}}
.m3-tip{position:absolute;z-index:5;background:#1F2937;color:#fff;font-size:11.5px;line-height:1.45;padding:6px 9px;border-radius:8px;pointer-events:none;white-space:nowrap;transform:translate(-50%,calc(-100% - 30px));display:none}`;
  function injectCSS() { if (document.getElementById('m3-css')) return; const s = document.createElement('style'); s.id = 'm3-css'; s.textContent = CSS; document.head.appendChild(s); }
  const pct = (x, y, z) => { const [sx, sy] = P(x, y, z); return [((sx - VB[0]) / VB[2]) * 100, ((sy - VB[1]) / VB[3]) * 100]; };
  const store = { get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch (e) { } } };

  function mount(el, opt = {}) {
    injectCSS();
    const key = opt.storageKey || 'map3d.on';
    const api = { zone: opt.zone || 'A', items: [], bays: {}, onHover: null };
    el.innerHTML = `<div class="m3"><div class="m3-h"><span class="m3-t">3D 매핑</span><span class="m3-s"></span><button class="m3-al" title="누르면 관제 확인"></button>
      <label class="m3-sw"><span>표시</span><input type="checkbox"><span class="m3-tr"></span></label></div>
      <div class="m3-b"><div class="m3-map"></div><div class="m3-lg"></div></div></div>`;
    const root = el.firstElementChild, sub = root.querySelector('.m3-s'), cb = root.querySelector('input'), map = root.querySelector('.m3-map'), lg = root.querySelector('.m3-lg'), al = root.querySelector('.m3-al');
    if (opt.height) root.style.setProperty('--m3h', opt.height + 'px');
    const on = store.get(key); cb.checked = on == null ? (opt.visible ?? true) : on === '1';
    root.classList.toggle('off', !cb.checked);
    cb.addEventListener('change', () => { root.classList.toggle('off', !cb.checked); store.set(key, cb.checked ? '1' : '0'); });

    function render() {
      const zone = ZONES[api.zone];
      sub.textContent = `${zone.name} · ${zone.cam}`;
      // 같은 차량이 여러 상태면 우선순위 높은 것 하나만
      const byId = {};
      api.items.forEach(it => { const k = it.id; if (!byId[k] || PRIORITY.indexOf(it.state) < PRIORITY.indexOf(byId[k].state)) byId[k] = it; });
      const items = Object.values(byId);
      const alerts = items.filter(isAlert), unacked = alerts.filter(it => !it.ack);
      al.classList.toggle('on', alerts.length > 0); al.classList.toggle('blink', unacked.length > 0);
      al.textContent = unacked.length ? `안전 경고 ${unacked.length}` : (alerts.length ? `안전 중지 ${alerts.length} · 확인됨` : '');
      const base = zone.waiting ? drawWaiting(items) : drawYard(zone, items, api.bays);
      let html = `<svg viewBox="${VB.join(' ')}" preserveAspectRatio="xMidYMid meet">${HATCH}${base}</svg><div class="m3-tip"></div>`;
      // 지도 실제 표시 영역 비율 보정 (meet)
      const w = map.clientWidth || 300, h = map.clientHeight || 200, sc = Math.min(w / VB[2], h / VB[3]), offX = (w - VB[2] * sc) / 2, offY = (h - VB[3] * sc) / 2;
      const pos = (x, y, z) => { const [sx, sy] = P(x, y, z); return [offX + (sx - VB[0]) * sc, offY + (sy - VB[1]) * sc]; };
      const blinkBay = new Set(items.filter(isBlink).map(it => it.bay));
      if (!zone.waiting) Object.entries(api.bays).forEach(([n, b]) => { const bs = BAYSTATE[b]; if (!bs) return; const [x, y] = pos(bayX(+n) + 4.2, 14.6, 0); html += `<span class="m3-tag ${b === 'stop' && blinkBay.has(+n) ? 'blink' : ''}" style="left:${x}px;top:${y}px;color:${bs.c};border-color:${bs.c}">${n}번 ${bs.t}</span>`; });
      items.forEach(it => {
        const st = STATE[it.state] || STATE.wait, [ax, ay] = anchorOf(it, zone);
        const top = it.worker && it.state === 'reverse' ? [it.worker[0], it.worker[1], 1.4] : [ax, ay, it.state === 'call' ? 1.7 : 2.6];
        const [x, y] = pos(...top);
        if (zone.waiting && it.order) { const [bx, by] = pos(ax, ay - .5, 2.6); html += `<span class="m3-badge" style="left:${bx}px;top:${by}px;background:${st.c === COL.gray ? '#4B5563' : st.c}">${it.order}</span>`; }
        if (!st.t) return; // 일반 대기는 배지만
        const label = it.state === 'delay' && it.ratio ? `${st.t} ×${it.ratio}` : st.t;
        const tip = [it.id, it.plate, it.detail].filter(Boolean).join(' · ');
        const below = zone.waiting && (it.slot || 0) > 6;
        const yy = below ? pos(ax, ay + 2.6, 0)[1] : (zone.waiting && it.order ? y - 14 : y);
        html += `<span class="m3-pin ${st.solid ? 'solid' : ''} ${below ? 'below' : ''} ${isBlink(it) ? 'blink' : ''}" data-id="${esc(it.id)}" data-tip="${esc(tip)}" style="left:${x}px;top:${yy}px;${st.solid ? `background:${st.c}` : ''}"><i style="background:${st.c}"></i>${esc(label)}</span>`;
      });
      map.innerHTML = html;
      // 범례: 지금 화면에 있는 상태만
      const cnt = {};
      items.forEach(it => { const st = STATE[it.state] || STATE.wait; const k = st.lg || st.t || '대기'; cnt[k] = cnt[k] || { c: st.c, n: 0 }; cnt[k].n++; });
      Object.values(api.bays).forEach(b => { const bs = BAYSTATE[b]; if (bs) { cnt[bs.t] = cnt[bs.t] || { c: bs.c, n: 0 }; cnt[bs.t].n++; } });
      lg.innerHTML = Object.entries(cnt).map(([k, v]) => `<div><i style="background:${v.c}"></i>${k}<b>${v.n}</b></div>`).join('') || '<div style="color:#9AA3AF">탐지 없음</div>';
      const tipEl = map.querySelector('.m3-tip');
      map.querySelectorAll('.m3-pin').forEach(p => {
        p.addEventListener('mouseenter', () => { if (p.dataset.tip) { tipEl.textContent = p.dataset.tip; tipEl.style.left = p.style.left; tipEl.style.top = p.style.top; tipEl.style.display = 'block'; } api.onHover && api.onHover(p.dataset.id); });
        p.addEventListener('mouseleave', () => { tipEl.style.display = 'none'; api.onHover && api.onHover(null); });
      });
    }
    api.update = (items, bays) => { api.items = items || []; if (bays) api.bays = bays; render(); return api; };
    api.ack = id => { api.items.forEach(it => { if (!id || it.id === id) if (isAlert(it)) it.ack = true; }); render(); api.onAck && api.onAck(id); return api; };
    al.addEventListener('click', () => api.ack());
    api.setZone = z => { api.zone = z; api.items = []; api.bays = {}; render(); return api; };
    api.setVisible = v => { cb.checked = !!v; cb.dispatchEvent(new Event('change')); };
    api.highlight = id => map.querySelectorAll('.m3-pin').forEach(p => p.classList.toggle('hl', id != null && p.dataset.id === id));
    cb.addEventListener('change', () => { if (cb.checked) render(); });
    if (window.ResizeObserver) new ResizeObserver(() => { if (cb.checked) render(); }).observe(map);
    render();
    return api;
  }
  window.Map3D = { mount, STATE, BAYSTATE, ZONES };
})();
