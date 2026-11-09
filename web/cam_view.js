/* CAM 그리기 공통 (R17) — 대시보드(/)와 CAM 4채널 새 창(/cams)이 같은 코드를 쓴다.
   - drawOv: 실제 영상 위 판정 겹쳐 그리기(박스 · 추적 ID · 구역 · 위험 반경 · 아차사고 강조 · 접근 거리선)
   - renderHopperCam / renderYardCam: 영상이 없는 채널의 시뮬레이션 그림(측면 시점)
   서버 판정(vision/to_events.py · vision/near_miss.py)과 같은 규칙을 화면에서 다시 계산한다. */
(function(){
function detAt(d,t){ if(!d||!d.frames.length) return null; const F=d.frames; let lo=0,hi=F.length-1; while(lo<hi){ const m=(lo+hi+1)>>1; if(F[m].t<=t) lo=m; else hi=m-1; } return F[lo]; }
const ZCOL={하차:'#5BD38F',대기:'#F0A54A',위험:'#FF5A6E','보행 통로':'#6FA2FF','출입 금지':'#C38BFF'};
/* 합성 사람(R15, data/stand_in.json) — 서버 판정(vision/near_miss.py)과 같은 직선 보간 */
function standAt(tracks,t){ const out=[]; (tracks||[]).forEach(tr=>{ const k=tr.keys; if(!k||!k.length||t<k[0][0]||t>k[k.length-1][0]) return;
  let i=0; for(let j=0;j<k.length;j++) if(k[j][0]<=t) i=j; const a=k[i], b=k[Math.min(i+1,k.length-1)], r=b[0]===a[0]?0:(t-a[0])/(b[0]-a[0]);
  const v=[1,2,3,4].map(n=>a[n]+(b[n]-a[n])*r); out.push({track_id:tr.track_id,cls:'person',bbox:[v[0]-v[2]/2,v[1]-v[3],v[0]+v[2]/2,v[1]],stand_in:true}); }); return out; }
function drawOv(cv,c,d,t,small,OV,APR){
  /* c = /api/cams 채널(size · zones · stand_in) · d = 탐지 결과(det.json) · t = 영상 초 · OV = 겹쳐 그리기 켜고 끄기 · APR = 접근 규칙 ③
     반환: 이 순간의 위험 {level, text} 또는 null (새 창의 빨간 테두리에 씀) */
  const ctx=cv.getContext('2d'); if(!c) return null; const id=c.camera, [fw,fh]=c.size; let risk=null;
  if(cv.width!==fw||cv.height!==fh){ cv.width=fw; cv.height=fh; }
  ctx.clearRect(0,0,fw,fh); const lw=Math.max(2,fw/480), fs=Math.round(fw/(small?40:64));
  ctx.font='700 '+fs+'px Inter,Noto Sans KR,sans-serif'; ctx.lineJoin='round';
  if(OV.zone||OV.radius) (c.zones||[]).forEach(z=>{ if(z.type==='위험'?!OV.radius:!OV.zone) return;
    ctx.setLineDash([lw*4,lw*3]); ctx.lineWidth=lw; ctx.strokeStyle=ZCOL[z.type]||'#fff'; ctx.beginPath(); z.points.forEach((p,i)=>i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1])); ctx.closePath(); ctx.stroke();
    if(z.type==='위험'){ ctx.fillStyle='rgba(255,90,110,.12)'; ctx.fill(); } ctx.setLineDash([]);
    if(!small){ ctx.fillStyle=ZCOL[z.type]||'#fff'; const xs=z.points.map(p=>p[0]), ys=z.points.map(p=>p[1]);   // 위험 구역 이름은 아래쪽에(베이 이름과 겹치지 않게)
      if(z.type==='위험') ctx.fillText(z.name,Math.min(...xs)+6,Math.max(...ys)-8); else ctx.fillText(z.name,z.points[0][0]+6,z.points[0][1]+fs+4); } });
  const f0=detAt(d,t); if(!f0) return null;
  const f={objects:f0.objects.concat(standAt(c.stand_in,t))};
  const zs=(c.zones||[]).filter(z=>z.type==='하차'||z.type==='대기');
  // 아차사고 위험(R15): 사람 발끝이 위험 구역 안 + 그 베이 트럭 조건(덤프 반경 = 적재함 상승 · 후진 경로 = 베이에 트럭) → 빨간 강조
  const trucksNow=f.objects.filter(o=>o.cls==='truck').map(o=>{ const b=o.bbox, ft=[(b[0]+b[2])/2,b[3]], bz=zs.find(z=>z.type==='하차'&&ptIn(ft,z.points)); return {o,bz,up:bz&&medRatio(d,t,o.track_id,(b[3]-b[1])/Math.max(1,b[2]-b[0]))>=0.6}; });
  const danger=new Map();
  f.objects.filter(o=>o.cls==='person').forEach(o=>{ const b=o.bbox, ft=[(b[0]+b[2])/2,b[3]];
    (c.zones||[]).filter(z=>z.type==='위험'&&ptIn(ft,z.points)).forEach(z=>{ const n=z.id.split('-').pop(), rev=/후진/.test(z.rule||'');
      const hit=trucksNow.find(x=>x.bz&&x.bz.id===c.sector_id+'-BAY-'+n&&(rev||x.up)); if(hit) danger.set(o,z); }); });
  danger.forEach(z=>{ ctx.beginPath(); z.points.forEach((p,i)=>i?ctx.lineTo(p[0],p[1]):ctx.moveTo(p[0],p[1])); ctx.closePath(); ctx.fillStyle='rgba(215,38,61,.38)'; ctx.fill(); ctx.lineWidth=lw*2; ctx.strokeStyle='#FF2D46'; ctx.stroke(); });
  if(danger.size) risk={level:'L3',text:/후진/.test(([...danger.values()][0].rule)||'')?'후진 경로 안 사람':'덤프 반경 안 사람'};
  if(danger.size){ const z=[...danger.values()][0], msg='아차사고 위험 · '+(/후진/.test(z.rule||'')?'후진 경로':'적재함 상승 중 덤프 반경')+' 안 사람';
    ctx.font='800 '+Math.round(fs*1.1)+'px Inter,Noto Sans KR,sans-serif'; const mw=ctx.measureText(msg).width+20; ctx.fillStyle='#D7263D'; ctx.fillRect(fw/2-mw/2,8,mw,fs*1.1+14); ctx.fillStyle='#fff'; ctx.fillText(msg,fw/2-mw/2+10,8+fs*1.1+5);
    ctx.font='700 '+fs+'px Inter,Noto Sans KR,sans-serif'; }
  // 접근(R16 규칙 ③): 사람 발끝 ↔ '움직이는' 트럭 박스 가장 가까운 점 · 사람 키(1.7m)로 m 환산 · 3m 이하만 선과 거리
  if(APR&&(APR.cams||[]).includes(id)){ const near=approachPairs(d,f0,t,APR);
    near.forEach(n=>{ const lv=n.d<=APR.l3_m?'L3':n.d<=APR.l2_m?'L2':'L1', col=lv==='L3'?'#FF2D46':lv==='L2'?'#FF8A3D':'#FFD23F';
      ctx.lineWidth=lw*2.2; ctx.strokeStyle=col; ctx.beginPath(); ctx.moveTo(n.p[0],n.p[1]); ctx.lineTo(n.q[0],n.q[1]); ctx.stroke();
      ctx.beginPath(); ctx.arc(n.p[0],n.p[1],lw*3,0,Math.PI*2); ctx.fillStyle=col; ctx.fill();
      const lab=n.d.toFixed(1)+'m · '+lv, w2=ctx.measureText(lab).width+12, lx=Math.min(fw-w2-4,n.p[0]+lw*4), ly=n.p[1]+fs+8;
      ctx.fillStyle=col; ctx.fillRect(lx,ly-fs-4,w2,fs+8); ctx.fillStyle='#141414'; ctx.fillText(lab,lx+6,ly); });
    const top=near.slice().sort((a,b)=>a.d-b.d)[0];
    if(top&&top.d<=APR.l2_m&&!risk) risk={level:top.d<=APR.l3_m?'L3':'L2',text:'움직이는 트럭과 사람 '+top.d.toFixed(1)+'m'};
    if(top&&top.d<=APR.l2_m){ const msg='아차사고 위험 · 움직이는 트럭과 사람 '+top.d.toFixed(1)+'m';
      ctx.font='800 '+Math.round(fs*1.1)+'px Inter,Noto Sans KR,sans-serif'; const mw=ctx.measureText(msg).width+20; ctx.fillStyle='#D7263D'; ctx.fillRect(fw/2-mw/2,8,mw,fs*1.1+14); ctx.fillStyle='#fff'; ctx.fillText(msg,fw/2-mw/2+10,8+fs*1.1+5);
      ctx.font='700 '+fs+'px Inter,Noto Sans KR,sans-serif'; } }
  if(!OV.box) return risk;
  const TB=f.objects.filter(o=>o.cls==='truck').map(o=>o.bbox);
  const inner=b=>TB.some(q=>q!==b&&(q[2]-q[0])*(q[3]-q[1])>(b[2]-b[0])*(b[3]-b[1])&&Math.max(0,Math.min(b[2],q[2])-Math.max(b[0],q[0]))*Math.max(0,Math.min(b[3],q[3])-Math.max(b[1],q[1]))>=0.8*(b[2]-b[0])*(b[3]-b[1]));
  f.objects.forEach(o=>{ const b=o.bbox, w=b[2]-b[0], h=b[3]-b[1], truck=o.cls==='truck';
    if(truck&&(w*h<fw*fh*0.003||inner(b))) return;   // 작은 오탐 · 큰 트럭 박스 안의 중복 박스(운전석만 잡힌 것 등)는 숨김
    const fx=(b[0]+b[2])/2, fy=b[3], inZ=zs.find(z=>ptIn([fx,fy],z.points)), out=truck&&!inZ;   // 구역 밖 트럭(원료 벽 오탐 등)은 판정하지 않음
    const rr=truck?medRatio(d,t,o.track_id,h/w):0, up=truck&&!out&&rr>=0.6, dz=!truck&&danger.get(o), col=out?'#9AA0A6':truck?(up?'#F0A54A':'#5BD38F'):(dz?'#FF2D46':'#FF8FA0');
    ctx.lineWidth=lw*(dz?2.4:1.2); ctx.strokeStyle=col; if(o.stand_in) ctx.setLineDash([lw*3,lw*2]); ctx.strokeRect(b[0],b[1],w,h); ctx.setLineDash([]);
    const lab=(OV.id?'#'+o.track_id+' · ':'')+(out?'구역 밖':truck?(up?'적재함↑ '+rr.toFixed(2):'트럭 '+rr.toFixed(2)):'사람'+(o.stand_in?'(합성)':'')+(dz?' · 위험':''))+(inZ&&!small?' · '+inZ.name.split(' ')[0]+' '+(inZ.name.split(' ')[1]||''):'');
    const tw2=ctx.measureText(lab).width+10; ctx.fillStyle=col; ctx.fillRect(b[0],Math.max(0,b[1]-fs-8),tw2,fs+8); ctx.fillStyle='#141414'; ctx.fillText(lab,b[0]+5,Math.max(fs,b[1]-6)); });
  return risk;
}
function approachPairs(d,f,t,APR){ // 서버 규칙과 같게: 최근 move_win_sec 동안 접지점 이동 ≥ max(move_px, 너비×move_frac)인 트럭만 · 사람마다 가장 가까운 것
  if(!d||!f) return []; const F=d.frames, w=APR.move_win_sec||1; let lo=0,hi=F.length-1; while(lo<hi){ const m=(lo+hi+1)>>1; if(F[m].t<=t-w) lo=m; else hi=m-1; }
  const past=F[lo], ft=b=>[(b[0]+b[2])/2,b[3]], out=[];
  const mov=f.objects.filter(o=>o.cls==='truck').filter(o=>{ if(!past||t-past.t<w*0.6) return false;
    const b=o.bbox, ow=b[2]-b[0], a=ft(b);   // 같은 ID가 없으면 1초 전 가장 많이 겹친 박스
    let q=past.objects.find(x=>x.cls==='truck'&&x.track_id===o.track_id);
    if(!q){ let bi=0; past.objects.filter(x=>x.cls==='truck').forEach(x=>{ const c=x.bbox, ix=Math.max(0,Math.min(b[2],c[2])-Math.max(b[0],c[0]))*Math.max(0,Math.min(b[3],c[3])-Math.max(b[1],c[1])); if(ix>bi){ bi=ix; q=x; } }); }
    if(!q) return false; const c=ft(q.bbox); return Math.hypot(a[0]-c[0],a[1]-c[1])>=Math.max(APR.move_px,ow*APR.move_frac); });
  f.objects.filter(o=>o.cls==='person').forEach(o=>{ const b=o.bbox, h=b[3]-b[1]; if(h<20) return; const p=ft(b), mpp=APR.person_h_m/h; let best=null;
    mov.forEach(m=>{ const c=m.bbox, q=[Math.min(Math.max(p[0],c[0]),c[2]),Math.min(Math.max(p[1],c[1]),c[3])], dd=Math.hypot(p[0]-q[0],p[1]-q[1])*mpp; if(!best||dd<best.d) best={d:dd,p,q}; });
    if(best&&best.d<=APR.dist_m) out.push(best); });
  return out; }
function medRatio(d,t,tid,now){ // 같은 추적 ID의 최근 1초 세로/가로 중앙값(한 프레임 흔들림 무시 · 변환기와 같은 규칙)
  if(!d) return now; const F=d.frames, r=[]; let lo=0,hi=F.length-1; while(lo<hi){ const m=(lo+hi+1)>>1; if(F[m].t<=t) lo=m; else hi=m-1; }
  for(let i=lo;i>=0&&t-F[i].t<=1;i--) F[i].objects.forEach(o=>{ if(o.track_id===tid&&o.cls==='truck'){ const b=o.bbox; r.push((b[3]-b[1])/Math.max(1,b[2]-b[0])); } });
  if(!r.length) return now; r.sort((a,b)=>a-b); return r[r.length>>1]; }
function ptIn(pt,poly){ let c=false; for(let i=0,j=poly.length-1;i<poly.length;j=i++){ const [xi,yi]=poly[i],[xj,yj]=poly[j]; if((yi>pt[1])!==(yj>pt[1])&&pt[0]<(xj-xi)*(pt[1]-yi)/((yj-yi)||1e-9)+xi) c=!c; } return c; }
function truckGeom(x,gy,k,deg){
  // 옆모습 덤프트럭: 왼쪽 캡, 오른쪽 끝(후단)이 적재함 회전축. 반환: 그리기용 값 + 전체 박스
  const L=150*k, cabW=36*k, cabH=40*k, chH=10*k, bodyW=L-cabW-6*k, bodyH=30*k, wr=10*k;
  const chY=gy-wr*1.6-chH, pivot=[x+L,chY];
  const a=deg*Math.PI/180, corners=[[-bodyW,0],[0,0],[0,-bodyH],[-bodyW,-bodyH]].map(([dx,dy])=>[pivot[0]+dx*Math.cos(a)-dy*Math.sin(a),pivot[1]+dx*Math.sin(a)+dy*Math.cos(a)]);
  const xs=corners.map(c=>c[0]).concat([x,x+L]), ys=corners.map(c=>c[1]).concat([chY-cabH,gy]);
  return {L,cabW,cabH,chH,bodyW,bodyH,wr,chY,pivot,a,box:[Math.min(...xs),Math.min(...ys),Math.max(...xs),Math.max(...ys)]};
}
function drawTruck(ctx,x,gy,k,deg,cab,label,boxColor,dumping){
  const g=truckGeom(x,gy,k,deg);
  ctx.fillStyle='#222'; ctx.fillRect(x+4*k,g.chY,g.L-4*k,g.chH);
  ctx.save(); ctx.translate(g.pivot[0],g.pivot[1]); ctx.rotate(g.a);
  ctx.fillStyle='#7d8287'; ctx.fillRect(-g.bodyW,-g.bodyH,g.bodyW,g.bodyH);
  ctx.fillStyle='#62676c'; for(let i=1;i<4;i++) ctx.fillRect(-g.bodyW+i*g.bodyW/4,-g.bodyH,2*k,g.bodyH);
  ctx.restore();
  if(dumping && deg>25){ ctx.fillStyle='rgba(120,92,70,.85)'; for(let i=0;i<14;i++){ const px=g.pivot[0]+4*k+(i%4)*4*k, py=g.pivot[1]+(i*7%30)*k*0.9; ctx.fillRect(px,py,3*k,3*k);} }
  if(deg>3){ ctx.strokeStyle='#c9ccd0'; ctx.lineWidth=4*k; ctx.beginPath(); ctx.moveTo(x+g.cabW+8*k,g.chY); const tip=[g.pivot[0]-g.bodyW*0.55*Math.cos(g.a),g.pivot[1]-g.bodyW*0.55*Math.sin(g.a)]; ctx.lineTo(tip[0],tip[1]); ctx.stroke(); }
  ctx.fillStyle=cab; ctx.fillRect(x,g.chY-g.cabH,g.cabW,g.cabH+g.chH);
  ctx.fillStyle='#bcd3e0'; ctx.fillRect(x+5*k,g.chY-g.cabH+6*k,g.cabW-12*k,14*k);
  [x+18*k,x+58*k,x+g.L-42*k,x+g.L-16*k].forEach(cx=>{ctx.fillStyle='#111';ctx.beginPath();ctx.arc(cx,gy-g.wr,g.wr,0,Math.PI*2);ctx.fill();ctx.fillStyle='#ddd';ctx.beginPath();ctx.arc(cx,gy-g.wr,g.wr*0.45,0,Math.PI*2);ctx.fill();});
  if(label){
    const b=g.box; ctx.strokeStyle=boxColor; ctx.lineWidth=2; ctx.strokeRect(b[0]-3,b[1]-3,b[2]-b[0]+6,b[3]-b[1]+6);
    const lf=Math.min(22,Math.max(17,Math.round(15*k/0.8))); ctx.font='700 '+lf+'px Inter,Noto Sans KR,sans-serif';
    const tw=ctx.measureText(label).width, lx=Math.max(4,Math.min(b[0]-3,ctx.canvas.width-tw-16)); ctx.fillStyle=boxColor; ctx.fillRect(lx,b[1]-lf-9,tw+12,lf+7); ctx.fillStyle='#fff'; ctx.fillText(label,lx+6,b[1]-7);
  }
  return g.box;
}
function scene(ctx,W,H,title,clock,down){
  const sky=ctx.createLinearGradient(0,0,0,H*0.62); sky.addColorStop(0,'#c9d0d6'); sky.addColorStop(1,'#e6e8ea');
  ctx.fillStyle=sky; ctx.fillRect(0,0,W,H);
  ctx.fillStyle='#9aa0a6'; ctx.fillRect(0,H*0.30,W,H*0.32*0.35); // 뒤편 소결공장 벽
  ctx.fillStyle='#7f858b'; for(let i=0;i<6;i++) ctx.fillRect(i*W/6+10,H*0.27,W/12,H*0.05);
  ctx.fillStyle='#55595d'; ctx.fillRect(0,H*0.78,W,H*0.22);
  ctx.fillStyle='#4a4e52'; ctx.fillRect(0,H*0.78,W,3);
  const fz=Math.min(24,Math.max(17,Math.round(W/640*17))), bh=fz+12;   // 채널명·시각: 타일에서도 읽히게
  ctx.font='700 '+fz+'px Inter,Noto Sans KR,sans-serif'; ctx.fillStyle='rgba(20,20,20,.78)'; ctx.fillRect(8,8,ctx.measureText(title).width+18,bh); ctx.fillStyle='#fff'; ctx.fillText(title,17,8+fz+3);
  const ct=clock; ctx.fillStyle='rgba(20,20,20,.78)'; const cw=ctx.measureText(ct).width+18; ctx.fillRect(W-cw-8,8,cw,bh); ctx.fillStyle='#fff'; ctx.fillText(ct,W-cw+1,8+fz+3);
  if(down){ ctx.fillStyle='rgba(215,38,61,.82)'; ctx.fillRect(0,H*0.42,W,30); ctx.fillStyle='#fff'; ctx.font='700 15px Inter,Noto Sans KR,sans-serif'; ctx.fillText('호퍼 정지 · 하차 일시 중지',14,H*0.42+20); }
}
function renderHopperCam(cv,cam,sec,vehById,clock){
  const ctx=cv.getContext('2d'), W=cv.width, H=cv.height, gy=H*0.86, k=W/640*0.9;
  scene(ctx,W,H,cam.id+' · '+cam.name+' 측면',clock,sec.status==='down');
  // 구역 선: 대기선(왼쪽) · 베이(오른쪽, 호퍼 투입구)
  const qx1=W*0.30; ctx.setLineDash([6,5]); ctx.lineWidth=2;
  ctx.strokeStyle='#F0A54A'; ctx.strokeRect(6,H*0.40,qx1-12,gy-H*0.40+4);
  ctx.strokeStyle='#5BD38F'; ctx.strokeRect(qx1,H*0.18,W-qx1-6,gy-H*0.18+4); ctx.setLineDash([]);
  ctx.font='700 '+Math.min(18,Math.max(14,Math.round(W/640*14)))+'px Inter,Noto Sans KR,sans-serif'; ctx.fillStyle='#F0A54A'; ctx.fillText('대기선',12,gy-10); ctx.fillStyle='#5BD38F'; ctx.fillText('베이 ('+sec.bays+')',qx1+6,H*0.18+18);
  const bayW=(W-qx1-6)/sec.bays;
  for(let b=0;b<sec.bays;b++){ const px=qx1+bayW*(b+0.5); ctx.fillStyle='#2b2d2f'; ctx.beginPath(); ctx.moveTo(px-44*k,gy+2); ctx.lineTo(px+44*k,gy+2); ctx.lineTo(px+26*k,H); ctx.lineTo(px-26*k,H); ctx.fill(); }
  const det=[];
  sec.working.forEach(w=>{
    const v=vehById[w.vehicle_id]; const bx=qx1+bayW*(w.bay+0.5)-75*k;
    const color=w.delayed?'#D7263D':(w.dump_deg>=15?'#F0A54A':'#5BD38F');
    const label='#'+w.track_id+' '+w.vehicle_id.slice(-4)+' · '+(sec.bays>2?(w.safety?'안전 정지':w.dump_deg>=15?'적재함↑':'하역 준비'):w.judge)+(w.dump_deg?' '+w.dump_deg+'°':'');
    drawTruck(ctx,bx,gy,k,w.dump_deg,'#b3302b',label,color,w.dump_deg>=15);
    det.push({id:w.track_id,veh:w.vehicle_id,zone:'베이 '+(w.bay+1),stay:w.elapsed_min,deg:w.dump_deg,judge:w.judge+(w.delayed?' · 지연':'')});
  });
  (sec.queue_info||[]).slice(0,1).forEach(q=>{
    drawTruck(ctx,14,gy,k*0.82,0,'#8f2a26','#'+q.track_id+' '+q.vehicle_id.slice(-4)+' · 하차 대기','#F0A54A',false);
  });
  (sec.queue_info||[]).forEach(q=>det.push({id:q.track_id,veh:q.vehicle_id,zone:'대기선',stay:q.since_call_min,deg:0,judge:'하차 대기 · 정지'}));
  if((sec.queue_info||[]).length>1){ ctx.fillStyle='#F0A54A'; ctx.font='700 13px Inter,sans-serif'; ctx.fillText('+'+(sec.queue_info.length-1),qx1-34,H*0.40+30); }
  return det;
}
function renderYardCam(cv,cam,st,clock){
  const ctx=cv.getContext('2d'), W=cv.width, H=cv.height, gy=H*0.86;
  scene(ctx,W,H,cam.id+' · '+cam.name+' · 계근대 쪽',clock,false);
  const yard=st.vehicles.filter(v=>v.state==='yard'||v.state==='arrived');
  ctx.setLineDash([6,5]); ctx.lineWidth=2; ctx.strokeStyle='#8FB3FF'; ctx.strokeRect(6,H*0.36,W-12,gy-H*0.36+4); ctx.setLineDash([]);
  ctx.font='600 11px Inter,Noto Sans KR,sans-serif'; ctx.fillStyle='#8FB3FF'; ctx.fillText('대기장 구역',12,H*0.36+14);
  const det=[]; const k=W/640*0.5;
  yard.slice(0,8).forEach((v,i)=>{
    const row=i<4?0:1, x=16+(i%4)*(W-24)/4, y=row?gy:gy-46*k*2.2;
    drawTruck(ctx,x,y,k,0,v.urgent?'#D7263D':'#b3302b','#'+v.track_id+' '+v.id.slice(-4),v.urgent?'#D7263D':'#8FB3FF',false);
  });
  yard.forEach(v=>det.push({id:v.track_id,veh:v.id,zone:v.state==='arrived'?'계근대':'대기장',stay:v.wait_min||0,deg:0,judge:v.judge}));
  if(yard.length>8){ ctx.fillStyle='#fff'; ctx.font='700 14px Inter,sans-serif'; ctx.fillText('+'+(yard.length-8)+'대',W-60,H*0.36+18); }
  return det;
}

window.CamView={detAt,ZCOL,standAt,drawOv,approachPairs,medRatio,ptIn,truckGeom,drawTruck,scene,renderHopperCam,renderYardCam};
})();
