# docs — 설계 · 규칙 문서

| 문서 | 내용 |
|---|---|
| `events.md` | **모듈 간 이벤트 형식과 종류**(GATE_IN · BAY_ENTER · SAFETY_ALERT · NEAR_MISS · UNLOAD_CONFIRMED …), 화면 동작 ↔ API ↔ 이벤트 표 |
| `auth.md` | 역할(관제 · 현장 · 운전자) · 로그인 · 권한 · 화면별 접근 |
| `notify.md` | 알림 받는 사람 · 재전송 · 읽음 기록 규칙 |
| `vision_check.md` | 실제 영상 탐지 확인 결과(적재함 판정, 3초 규칙, 대기장 사람 · 트럭 접근 아차사고와 장면 캡처) |
| `map3d.md` | 3D 매핑 카드 · 안전 경고 깜빡임 적용 내용과 팀 확인 필요 사항 |
| `cams_window.md` | CAM 4채널 새 창 사용법 · 두 모니터 시연 체크리스트 |
| `screen_map.md` | 시안 화면 ↔ 기능 연결표 |
| `ui_guide.md` | 화면 디자인 기준(글자 크기 · 여백 · 색) |
| `screens/` | 화면 캡처(전후 비교 · 아차사고 장면) |
| `sample_events.jsonl` | 이벤트 예시 묶음(`tools/replay_events.py`로 재생) |
