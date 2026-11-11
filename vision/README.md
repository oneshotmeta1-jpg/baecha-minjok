# vision — 영상 인식(객체 탐지 · 판정)

영상(`영상/` 폴더, git 제외) → 프레임별 탐지 · 추적 → 대시보드 이벤트(`docs/events.md`).
모델: 사전학습 **YOLO11n**(COCO `truck` · `person`) + **ByteTrack** 추적. 파인튜닝은 하지 않았다.

| 파일 | 하는 일 |
|---|---|
| `detect_video.py` | 영상 → `<영상이름>.det.json`(프레임마다 트럭 · 사람 박스와 추적 ID). 한 번만 미리 돌려 둔다(가중치 `yolo11n.pt`, git 제외) |
| `to_events.py` | 탐지 결과 → 이벤트. 박스 아래 중심점이 구역 안에 **3초** 이상이면 진입/이탈(`BAY_ENTER` · `ZONE_ARRIVE` …), 추적 ID가 바뀌어도 같은 트럭으로 합침(IoU 0.4), 적재함 상승 = 박스 세로/가로 최근 1초 중앙값 ≥ 0.6이 1초 유지 |
| `near_miss.py` | **아차사고 판정**. ① 적재함 상승 중 덤프 반경 안 사람 ② 베이 진입 30초 안 후진 경로 안 사람 ③ 대기장(CAM-04)에서 사람과 움직이는 트럭 3m 이하(사람 키 1.7m로 거리 환산). 이어진 시간 · 거리로 L1 → L2 → L3, 끝날 때 `NEAR_MISS`, ①② L3면 `SAFETY_ALERT`. 기준값은 `data/cams.json` `near_miss`(가정값). 합성 사람(`data/stand_in.json`)은 기본 꺼짐 |
| `dump_state.py` | 사진 한 장으로 적재함 상태 판정(하차 중 / 내림) |

## 실행

```bash
py vision/detect_video.py          # 영상/ 4개 → <이름>.det.json
py tools/ingest_videos.py          # det.json → 이벤트 → 영상 모드 서버(--video) /api/ingest
```

- CAM-01 · 03(측면)은 사전학습으로 트럭을 잡는다. CAM-02(후면 실내)는 트럭을 못 잡는다 → 파인튜닝 필요.
- 대기장(CAM-04) 영상의 실제 사람으로 아차사고 2건(L3)이 잡혔다. 호퍼 영상에는 사람이 나오지 않는다.
- 결과와 장면 캡처: [docs/vision_check.md](../docs/vision_check.md). 기본 시뮬레이션 모드에서도 서버가 켜질 때 영상 아차사고를 뽑아 영상 한 바퀴마다 기록한다(`app/cams.py`).
