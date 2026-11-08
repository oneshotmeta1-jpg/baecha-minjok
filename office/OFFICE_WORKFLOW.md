# 하역 관제 개발 오피스 ↔ 코드 작업 규칙

- 오피스: https://claude.ai/artifact/SABeFbaZ3JLeVWXTnNrUck — 소스: `office/dock_dev_office.html` (고친 뒤 같은 경로로 다시 게시)
- 진행 상태: 오피스 DB 문서 `data/users/me/office` (ArtifactData collection `data/users/me`, doc `office`)
- 코드: `D:/내문서 및 바탕화면/Desktop/해커톤` = https://github.com/oneshotmeta1-jpg/baecha-minjok (비공개, `main` = 확정본)
- 로컬 실행: `.claude/launch.json`의 `dock` → http://127.0.0.1:8000/
- gh CLI 없음. push는 git 자격 증명 관리자로 된다. 저장소 설정 변경(팀원 초대 등)은 브라우저에서.

## 상태 흐름

| 오피스 상태 | 누가 | 할 일 |
|---|---|---|
| `plan` | 사용자 | 회의 결과에서 개선안 고르고 OK |
| `requested` | Claude Code | "오피스 개선안 작업해줘" → **작업** |
| `candidate` | 사용자 | 후보 대시보드 확인 후 승인/반려 |
| `decided` | Claude Code | "오피스 결정 반영해줘" → **반영** |
| `idle` | — | 다음 목표 대기 |

## 작업 (requested → candidate)

1. `git switch main && git pull --ff-only && git switch -c office/r<N>`
2. 마지막 라운드의 `selected` 개선안만 구현. `docs/events.md` 형식을 지킨다(바꾸면 문서도 같이).
3. `dock` 서버를 후보 코드로 다시 켜고 브라우저 창에서 직접 확인.
4. 커밋 후 `git push -u origin office/r<N>` (main은 바뀌지 않음).
5. 라운드에 `candidate` 기록, `status: "candidate"`:
   `version`(예: v2), `base`(현재 live), `built_at`, `branch`, `commit`,
   `compare`(`<repo>/compare/main...office/r<N>`), `preview_url`(http://127.0.0.1:8000/), `summary`,
   `done[{id,result}]`, `skipped[{id,reason}]`, `files[]`, `tests[{name,ok,note}]`, `checks[]`, `limits[]`,
   `report[{from,to,text}]`(역할별 작업 보고, 실제로 한 일만). `site.brief`의 "현재 vN" 문장도 갱신.

## 반영 (decided → idle)

- **승인**: `git switch main && git pull --ff-only && git merge --no-ff office/r<N>` → `git push origin main`
  → `git branch -d office/r<N>` → `git push origin --delete office/r<N>` → 서버 재시작 확인
  → `round.applied = version`, `site.live = version`, `status: "idle"`.
- **반려**: `git switch main && git branch -D office/r<N>` → `git push origin --delete office/r<N>` → 서버를 main으로 재시작
  → `round.applied = base`, `status: "idle"`. `decision_note`는 다음 목표에 반영.

- **DB 정리 (승인·반려 반영 때마다 이어서)**: 최근 3개 라운드만 원문을 두고, 그보다 오래된 라운드의
  회의 대화(`log`)·개선안 상세·후보 보고는 `office/archive/r<N>.json`으로 옮긴다. DB에는 목표(300자)·승인한 개선안 제목·
  결과·후보 요약과 `archived` 경로, `items_total`만 남는다. 라운드 목록·적용 버전·`site.brief`는 그대로다.
  1. ArtifactData `get`(`out_dir` 사용)으로 최신 문서를 받고 `version`을 적어 둔다.
  2. `node office/tools/archive_rounds.mjs <받은 office.json> <정리본.json>` (`--keep 3` 기본, 다시 돌려도 결과 같음)
  3. 새 `office/archive/*.json`을 main에 커밋·push (원문이 저장소에 올라간 뒤에만 DB를 줄인다).
  4. ArtifactData `set`에 `file_path: <정리본.json>`, `if_version: <1에서 본 version>`. 실패하면 1부터 다시.
  - 오피스 화면이 열려 있으면 화면이 새 문서를 받아 다시 그린다. 회의 중이면 회의가 끝난 뒤에 정리한다.

push는 오피스에서 승인한 범위 안에서만. 강제 push·main 직접 커밋 금지(문서 정리 제외).
