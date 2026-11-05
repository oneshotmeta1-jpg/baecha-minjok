# 하역 관제 개발 오피스 ↔ 코드 작업 규칙

- 오피스: https://claude.ai/artifact/SABeFbaZ3JLeVWXTnNrUck — 소스: `office/dock_dev_office.html` (고친 뒤 같은 경로로 다시 게시)
- 진행 상태: 오피스 DB 문서 `data/users/me/office` (ArtifactData collection `data/users/me`, doc `office`)
- 코드: `D:/내문서 및 바탕화면/Desktop/해커톤` (로컬 git, `main` = 확정본)
- 로컬 실행: `.claude/launch.json`의 `dock` → http://127.0.0.1:8000/
- GitHub 원격은 아직 없음. 팀 저장소가 생기면 `site.repo`에 주소를 넣고 push 단계를 추가한다.

## 상태 흐름

| 오피스 상태 | 누가 | 할 일 |
|---|---|---|
| `plan` | 사용자 | 회의 결과에서 개선안 고르고 OK |
| `requested` | Claude Code | "오피스 개선안 작업해줘" → **작업** |
| `candidate` | 사용자 | 후보 대시보드 확인 후 승인/반려 |
| `decided` | Claude Code | "오피스 결정 반영해줘" → **반영** |
| `idle` | — | 다음 목표 대기 |

## 작업 (requested → candidate)

1. `git switch main && git switch -c office/r<N>`
2. 마지막 라운드의 `selected` 개선안만 구현. `docs/events.md` 형식을 지킨다(바꾸면 문서도 같이).
3. `dock` 서버를 후보 코드로 다시 켜고 브라우저 창에서 직접 확인.
4. 커밋 (`office/r<N>` 브랜치에만).
5. 라운드에 `candidate` 기록, `status: "candidate"`:
   `version`(예: v1), `base`(현재 live), `built_at`, `branch`, `commit`, `preview_url`(http://127.0.0.1:8000/), `summary`,
   `done[{id,result}]`, `skipped[{id,reason}]`, `files[]`, `tests[{name,ok,note}]`, `checks[]`, `limits[]`,
   `report[{from,to,text}]`(역할별 작업 보고, 실제로 한 일만). `site.brief`의 "현재 vN" 문장도 갱신.

## 반영 (decided → idle)

- **승인**: `git switch main && git merge --no-ff office/r<N>` → `git branch -d office/r<N>` → 서버 재시작 확인
  → `round.applied = version`, `site.live = version`, `status: "idle"`.
- **반려**: `git switch main && git branch -D office/r<N>` → 서버를 main으로 재시작
  → `round.applied = base`, `status: "idle"`. `decision_note`는 다음 목표에 반영.

강제 push·main 직접 커밋 금지(뼈대 v0 제외).
