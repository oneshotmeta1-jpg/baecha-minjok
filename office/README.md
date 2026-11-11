# office — 개발 오피스(라운드 진행 기록)

해커톤 개발을 '회의 → 개선안 선택 → 후보 구현 → 승인 → 반영' 라운드로 진행한 픽셀 오피스.

| 파일 | 내용 |
|---|---|
| `OFFICE_WORKFLOW.md` | 오피스 ↔ 코드 작업 규칙(상태 흐름 · 브랜치 `office/r<N>` · 반영 · DB 정리) |
| `dock_dev_office.html` | 오피스 화면 소스(claude.ai 아티팩트로 게시). 진행 상태는 아티팩트 DB `data/users/me/office` |
| `archive/r<N>.json` | 오래된 라운드의 회의 대화 · 개선안 상세 · 후보 보고 원문(DB에는 최근 3개 라운드만 원문) |
| `tools/archive_rounds.mjs` | DB 문서에서 오래된 라운드 원문을 `archive/`로 옮기는 스크립트 |
