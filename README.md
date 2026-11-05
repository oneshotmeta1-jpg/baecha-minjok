# 배차의 민족 — 상하역 지연 관제 대시보드

K-뉴딜 아카데미 내부 해커톤(비전 AI 안전관제). 포항제철소 소결공장에 부원료·재활용 원료·연료를 나르는 덤프트럭을 CCTV로 판정하고(적재함 상승 = 하차 중), 호퍼별 혼잡도·하차 진척도로 대기장 차량의 하차 순번(순서·호퍼·호출 시점)을 정해 주는 관제 대시보드입니다.

- 큰틀·로드맵: [PLAN.md](PLAN.md)
- 모듈 간 이벤트 형식: [docs/events.md](docs/events.md)
- 개발 진행 방식(오피스 라운드): [office/OFFICE_WORKFLOW.md](office/OFFICE_WORKFLOW.md)

## 실행

```bash
pip install -r requirements.txt
python app/server.py
```

이 PC에서 http://127.0.0.1:8000/ 을 엽니다. 시작 버튼을 누르면 시뮬레이션이 돌고, 배속·섹터 정지·긴급 출하 버튼으로 시연합니다.

## 같은 와이파이의 팀원과 공유

```bash
python app/server.py --lan
```

실행 창에 `팀원 공유 주소: http://192.168.x.x:8000/` 이 찍힙니다. 같은 와이파이에 있는 팀원은 그 주소로 들어오면 됩니다.

- 서버는 하나라서 누가 시작·정지·섹터 정지를 누르면 모두의 화면이 같이 바뀝니다.
- 서버를 켠 PC가 꺼지거나 잠들면 접속이 끊깁니다.
- 접속이 안 되면: Windows 네트워크가 "개인(Private)"인지, 방화벽에서 이 Python이 허용됐는지 확인합니다. 행사장 와이파이는 기기끼리 통신을 막아 두는 경우가 있어, 그때는 휴대폰 핫스팟에 다 같이 붙으면 됩니다.
- 와이파이를 다시 잡으면 IP가 바뀔 수 있으니 실행 창의 주소를 다시 확인합니다.

## 사진 한 장으로 적재함 판정

```bash
python vision/dump_state.py 사진.jpg --weights yolo11n.pt --out web/assets/dump_sample.jpg
```

## 확인

```bash
python tests/test_sim.py
```
