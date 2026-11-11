# 배차의 민족 — 원료 하역장 관제 · 호출 순번 추천 대시보드

K-뉴딜 아카데미 내부 해커톤. 포항제철소 소결공장에 원료를 나르는 덤프트럭을 CCTV로 판정하고(적재함 상승 = 하차 중), 호퍼별 혼잡도 · 하차 진척도로 대기장 차량의 하차 순번(순서 · 호퍼 · 호출 시점)을 추천하며, 작업자 위험(아차사고 · 안전 경고)을 알려 주는 관제 대시보드입니다. 화면 수치 대부분은 **시뮬레이션 값 · 가정값**입니다.

- 큰틀 · 공식: [PLAN.md](PLAN.md) · 이벤트 형식: [docs/events.md](docs/events.md) · 개발 진행(오피스 라운드): [office/OFFICE_WORKFLOW.md](office/OFFICE_WORKFLOW.md)

## 폴더 구성

| 폴더 | 내용 |
|---|---|
| [`app/`](app/README.md) | 서버(FastAPI) · 운영 상태(추천 vs FIFO) · 시뮬레이터 · 순번 공식 · 계정 · 알림 |
| [`web/`](web/README.md) | 화면: 관제 PC(`/`) · 모바일(`/m/`) · CAM 4채널 새 창(`/cams`) · 로그인 |
| [`vision/`](vision/README.md) | 영상 인식: YOLO 탐지 · 추적 → 이벤트, 아차사고 판정 |
| [`data/`](data/README.md) | 현장 구성 · 운영 기준 · 비용 단가 · 영상 구역 · 지도(JSON, 대부분 가정값) |
| [`tools/`](tools/README.md) | 지도 다시 만들기 · 영상 이벤트 넣기 · 계정 관리 · 외부 공유 |
| [`tests/`](tests/README.md) | 확인 스크립트(파일마다 실행) |
| [`docs/`](docs/README.md) | 이벤트 · 권한 · 알림 · 영상 확인 · 화면 기준 문서 |
| [`office/`](office/README.md) | 개발 오피스 라운드 기록 |
| `영상/` | CCTV 영상 4개와 탐지 결과(git 제외) |
| `서버켜기.bat` · `외부공유.bat` | 서버 켜기 · 외부 공유 주소 열기(더블클릭) |

## 실행

```bash
pip install -r requirements.txt
python app/server.py            # 또는 서버켜기.bat
```

이 PC에서 http://127.0.0.1:8000/ 을 엽니다. 처음엔 회원가입(첫 관제 담당자 = 관리자) 후 로그인합니다. 시계 칩에서 시작 · 배속(×1 실제 속도 · ×5 기사 앱 확인용 · ×30~×300), 시연 메뉴에서 하역장 정지 · 긴급 입고 · 안전 경고 · 실제 운영 체험(×1)을 씁니다.

| 옵션 | 뜻 |
|---|---|
| `--lan` | 같은 와이파이의 휴대폰 · 팀원 PC도 접속(실행 창에 '팀원 공유 주소') |
| `--video` | 영상 모드: 차량 움직임은 `/api/ingest` 이벤트로만(`tools/ingest_videos.py`) |
| `--check` | 확인용: 이벤트 DB · 계정 DB를 건드리지 않고 메모리에서만(다른 포트와 함께) |
| `--no-auth` | 로그인 없이(시연용) |

다른 망에서 접속하려면 `외부공유.bat`(Cloudflare 임시 주소). 모바일은 앱이 아니라 휴대폰 브라우저로 `/m/`을 엽니다.

## 같은 와이파이에서 접속이 안 되면

- Windows 네트워크가 "개인(Private)"인지, 방화벽에서 이 Python이 허용됐는지 확인합니다.
- 행사장 와이파이는 기기끼리 통신을 막는 경우가 있어, 휴대폰 핫스팟에 다 같이 붙으면 됩니다.
- 와이파이를 다시 잡으면 IP가 바뀔 수 있으니 실행 창의 주소를 다시 확인합니다.

## 확인

```bash
for t in tests/test_*.py; do python $t | tail -1; done
```

지도 © OpenStreetMap contributors (ODbL).
