# tests — 확인 스크립트

pytest 없이 파일마다 바로 실행한다: `py tests/test_sim.py` (끝에 `n/n 통과`). 서버를 켜지 않고 메모리에서 돈다(`DOCK_MEMORY=1`).

```bash
for t in tests/test_*.py; do py $t | tail -1; done
```

| 파일 | 확인하는 것 |
|---|---|
| `test_sim.py` | 시뮬레이터 · 엔진 · 런타임 전반: 이벤트 형식(`docs/events.md`와 일대일), 순번 · 호출, 효과 · 금액, 다시 계산, 안전 경고, 영상 변환 |
| `test_auth.py` | 가입 · 로그인 · 비밀번호 · 역할 권한 |
| `test_admin.py` | 관리자 가입 승인 · 반려 · 역할 · 비밀번호 초기화 · 삭제 |
| `test_mobile.py` | 운전자 · 현장 담당자 흐름과 권한 |
| `test_notify.py` | 알림 라우팅 · 중복 방지 · 재전송 · 읽음 |
| `test_r15.py` | 구역(ROI) 편집 · 아차사고 규칙 ①②③ · 합성 사람 스위치 · 실제 영상 결과 · 알림 이력 '안전' |
| `test_r17.py` | CAM 4채널 새 창 · 공통 그리기 파일 · 채널 권한 |
| `test_map3d.py` | 3D 매핑 카드 · 안전 경고 관제 확인(SAFETY_SEEN) |
| `test_unload.py` | 운전자 하역 완료 확인 · 하차 예상 시간 추천 · 화면 스크립트 문법 검사 |
| `test_mzone.py` | 모바일 3D 매핑(운전자 경로 · 구역 맵, 현장 담당 구역) |
| `test_change.py` | 현장 앱 하역장 변경 사유 → 운영 분석 |
| `zones_fixture.json` | 판정 테스트용 고정 구역(현장에서 구역을 다시 그려도 테스트가 깨지지 않게) |
