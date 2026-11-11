# tools — 보조 명령어

| 파일 | 하는 일 | 실행 |
|---|---|---|
| `build_map.py` | OpenStreetMap에서 포항제철소 주변 지도를 받아 `data/site_map.json`과 `data/sectors.json`의 경로 · 거리를 다시 만든다 | `py tools/build_map.py --fetch` |
| `ingest_videos.py` | 영상 4개의 탐지 결과(`영상/*.det.json`) → 이벤트 → 영상 모드 서버 `/api/ingest` | `py tools/ingest_videos.py [--dry]` |
| `replay_events.py` | 이벤트 파일(JSONL)을 시각 순서대로 서버에 보낸다 | `py tools/replay_events.py 파일.jsonl` |
| `users.py` | 계정 관리 명령(목록 · 승인 · 반려 · 관리자 · 추가 · 비밀번호 초기화 · 삭제) — `data/auth.db`를 바로 고친다 | `py tools/users.py list` |
| `share_tunnel.py` | Cloudflare 임시 터널로 외부 공유 주소를 연다(`외부공유.bat`이 실행) | `py tools/share_tunnel.py` |
| `capture_header.py` · `capture.sh` | 화면 확인용 스크린샷(헤더 폭별 · 화면 전후 비교) | 파일 첫 줄 참고 |
