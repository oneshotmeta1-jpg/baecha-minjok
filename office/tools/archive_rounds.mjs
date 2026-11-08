#!/usr/bin/env node
// 오피스 DB 문서에서 오래된 라운드의 원문을 office/archive/r<N>.json 으로 옮기고 DB에는 요약만 남긴다.
//
// 사용법:
//   node office/tools/archive_rounds.mjs <내려받은 office.json> <정리본 출력 경로> [--keep 3]
//
// - 최근 --keep 개 라운드(기본 3)는 그대로 둔다. 진행 중인 라운드(applied 없음)도 그대로 둔다.
// - 이미 정리된 라운드(archived 필드 있음)는 건드리지 않는다. 몇 번 돌려도 결과가 같다.
// - 원문 파일이 이미 있으면 덮어쓰지 않는다(처음 옮긴 원문이 기준).
// - ArtifactData 'get'이 주는 래퍼({version, data} 등)도, 문서 본문만 있는 JSON도 받는다.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const args = process.argv.slice(2);
const keepAt = args.indexOf('--keep');
const KEEP = keepAt >= 0 ? Number(args.splice(keepAt, 2)[1]) : 3;
const [inPath, outPath] = args;
if (!inPath || !outPath || !(KEEP >= 1)) {
  console.error('사용법: node office/tools/archive_rounds.mjs <office.json> <정리본.json> [--keep 3]');
  process.exit(2);
}

const ARCHIVE_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'archive');
const bytes = v => Buffer.byteLength(JSON.stringify(v));
const clip = (s, n) => (s = String(s || '')).length > n ? s.slice(0, n).trimEnd() + '…' : s;

const raw = JSON.parse(fs.readFileSync(inPath, 'utf8'));
const doc = raw && Array.isArray(raw.rounds) ? raw : raw.data;
if (!doc || !Array.isArray(doc.rounds)) { console.error('rounds 배열을 찾지 못했습니다.'); process.exit(1); }

function compact(r, file) {
  const sel = new Set(r.selected || []);
  const c = r.candidate;
  const out = {};
  for (const [k, v] of Object.entries(r)) {
    if (['goal', 'items', 'log', 'candidate'].includes(k)) continue;
    out[k] = v;
  }
  out.goal = clip(r.goal, 300);
  out.items_total = (r.items || []).length;
  // 승인한 개선안만 제목 수준으로 남긴다(지난 라운드 요약·회의 프롬프트에 쓰임)
  out.items = (r.items || []).filter(i => sel.has(i.id))
    .map(i => ({ id: i.id, owner: i.owner, title: i.title, priority: i.priority, effort: i.effort }));
  out.log = [];
  if (c) {
    out.candidate = {
      version: c.version, base: c.base, branch: c.branch, commit: c.commit, compare: c.compare,
      built_at: c.built_at, summary: clip(c.summary, 400),
      done: (c.done || []).map(x => ({ id: x.id, result: clip(x.result, 120) })),
      skipped: (c.skipped || []).map(x => ({ id: x.id, reason: clip(x.reason, 120) })),
      watched: true,
    };
  }
  out.archived = file;
  return out;
}

const before = bytes(doc);
const rounds = doc.rounds;
const cutoff = rounds.length - KEEP;
const moved = [];
fs.mkdirSync(ARCHIVE_DIR, { recursive: true });
const next = rounds.map((r, i) => {
  if (i >= cutoff || r.archived || !r.applied) return r;
  const name = 'r' + (r.n || i + 1) + '.json';     // 화면의 R번호
  const file = 'office/archive/' + name;
  const target = path.join(ARCHIVE_DIR, name);
  if (!fs.existsSync(target)) fs.writeFileSync(target, JSON.stringify({ round: 'R' + (r.n || i + 1), archived_from: 'data/users/me/office', original: r }, null, 2) + '\n');
  moved.push({ round: 'R' + (r.n || i + 1), file, from: bytes(r), i });
  return compact(r, file);
});
const result = Object.assign({}, doc, { rounds: next });
fs.writeFileSync(outPath, JSON.stringify(result));

const after = bytes(result);
for (const m of moved) console.log(m.round + ' → ' + m.file + ' (' + m.from + 'B → ' + bytes(next[m.i]) + 'B)');
console.log('정리한 라운드 ' + moved.length + '개, 문서 ' + before + 'B → ' + after + 'B');
