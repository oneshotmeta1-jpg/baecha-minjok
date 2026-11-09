/* safety-blink.js — 관제 화면 3D 배치도용 안전 경고 깜빡임
 * 배치도가 이미지든 SVG든 상관없이, 배치도를 감싼 요소 위에 겹쳐서 표시한다.
 *
 *   const sb = SafetyBlink.attach(document.getElementById('overview'));  // position:relative 필요(자동 설정)
 *   sb.set([{ id: 'A-1', x: 23.6, y: 39.8, label: '작업자 · 덤프 하역 반경 안', sub: '호퍼 A 1번 베이' }]);
 *        // x, y = 배치도 안에서의 위치(%)
 *        // box: { x, y, w, h } (%) 를 주면 해당 영역(야드 칩 등) 테두리도 함께 깜빡임
 *   sb.ack('A-1');    // 관제 확인 → 깜빡임 멈추고 빨간색 고정
 *   sb.clear('A-1');  // 해제 → 표시 제거
 *
 * 이미 있는 요소(야드 칩, 카드, SVG 도형)를 깜빡이게 하려면 클래스만 붙이면 됨:
 *   el.classList.add('sb-blink')      HTML 요소
 *   svgShape.classList.add('sb-blink-svg')   SVG 도형(테두리·채움)
 */
(function () {
  const CSS = `
@keyframes sbPulse{0%,100%{opacity:1}50%{opacity:.35}}
@keyframes sbPing{0%{transform:translate(-50%,-50%) scale(.6);opacity:.75}100%{transform:translate(-50%,-50%) scale(2.4);opacity:0}}
@keyframes sbBg{0%,100%{background:#E5383B}50%{background:#A51D22}}
@keyframes sbBox{0%,100%{box-shadow:0 0 0 3px #E5383B, 0 0 0 7px rgba(229,56,59,.25)}50%{box-shadow:0 0 0 3px rgba(229,56,59,.25), 0 0 0 7px rgba(229,56,59,0)}}
@keyframes sbStroke{0%,100%{stroke-opacity:1;fill-opacity:.22}50%{stroke-opacity:.3;fill-opacity:.05}}
.sb-layer{position:absolute;inset:0;pointer-events:none;z-index:4}
.sb-dot{position:absolute;width:18px;height:18px;border-radius:50%;background:#E5383B;transform:translate(-50%,-50%);border:3px solid #fff;box-sizing:border-box;animation:sbPulse 1s ease-in-out infinite}
.sb-ping{position:absolute;width:34px;height:34px;border-radius:50%;border:3px solid #E5383B;animation:sbPing 1s ease-out infinite}
.sb-lb{position:absolute;transform:translate(-50%,calc(-100% - 18px));white-space:nowrap;color:#fff;font-weight:700;font-size:13px;padding:6px 12px;border-radius:14px;animation:sbBg 1s ease-in-out infinite;box-shadow:0 2px 6px rgba(0,0,0,.2)}
.sb-lb small{display:block;font-weight:500;font-size:11px;opacity:.9;margin-top:1px}
.sb-box{position:absolute;border-radius:16px;animation:sbBox 1s ease-in-out infinite}
.sb-acked .sb-dot,.sb-acked .sb-lb,.sb-acked .sb-box{animation:none}
.sb-acked .sb-ping{display:none}
.sb-acked .sb-lb{background:#E5383B}
.sb-acked .sb-box{box-shadow:0 0 0 3px #E5383B}
.sb-blink{animation:sbPulse 1s ease-in-out infinite}
.sb-blink-svg{stroke:#E5383B !important;stroke-width:3;fill:#E5383B;animation:sbStroke 1s ease-in-out infinite}
@media (prefers-reduced-motion: reduce){.sb-dot,.sb-lb,.sb-box,.sb-blink,.sb-blink-svg{animation:none}.sb-ping{display:none}.sb-lb{background:#E5383B}.sb-box{box-shadow:0 0 0 4px #E5383B}}`;
  function css() { if (document.getElementById('sb-css')) return; const s = document.createElement('style'); s.id = 'sb-css'; s.textContent = CSS; document.head.appendChild(s); }
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  function attach(el) {
    css();
    if (getComputedStyle(el).position === 'static') el.style.position = 'relative';
    const layer = document.createElement('div'); layer.className = 'sb-layer'; el.appendChild(layer);
    let list = [];
    function render() {
      layer.innerHTML = list.map(a => `<div class="${a.ack ? 'sb-acked' : ''}">
        ${a.box ? `<div class="sb-box" style="left:${a.box.x}%;top:${a.box.y}%;width:${a.box.w}%;height:${a.box.h}%"></div>` : ''}
        <div class="sb-ping" style="left:${a.x}%;top:${a.y}%"></div><div class="sb-dot" style="left:${a.x}%;top:${a.y}%"></div>
        ${a.label ? `<div class="sb-lb" style="left:${a.x}%;top:${a.y}%">${esc(a.label)}${a.sub ? `<small>${esc(a.sub)}</small>` : ''}</div>` : ''}</div>`).join('');
    }
    const api = {
      set(alerts) { list = (alerts || []).map(a => ({ ...a })); render(); return api; },
      add(a) { list = list.filter(x => x.id !== a.id).concat({ ...a }); render(); return api; },
      ack(id) { list.forEach(a => { if (a.id === id) a.ack = true; }); render(); return api; },
      clear(id) { list = list.filter(a => a.id !== id); render(); return api; },
      get list() { return list.slice(); }
    };
    return api;
  }
  window.SafetyBlink = { attach };
})();
