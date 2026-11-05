# vision — 객체 인식 파트 (영훈·현우)

역할: 영상 → `docs/events.md`의 `ZONE_ENTER`/`ZONE_EXIT`/`VEHICLE_STOPPED` 이벤트 + 박스 그린 영상.

1. 시작은 사전학습 YOLO(COCO `truck`)로 추가 학습 없이 검출. 크레인·컨테이너는 파인튜닝(시간 되면).
2. `model.track(..., tracker="bytetrack.yaml")`로 추적 ID.
3. 구역 판정: `data/sectors.json`의 `cam_zones.bay/queue` 다각형 + 박스 아래 중심점 `cv2.pointPolygonTest`.
4. 흔들림 방지: 구역 진입·이탈은 `settings.vision.zone_hold_sec`(3초) 유지 시에만. 가려져 잠깐 끊긴 ID는 같은 자리에서 다시 잡히면 같은 차량.
5. 추론은 GPU 노트북/Colab에서 미리 돌려 `vision/out/<CAM>.events.jsonl` + `<CAM>.mp4`로 저장. 실시간은 1채널만 초당 2~5장.

차종은 영상으로 나누지 않는다(출하 지시 정보 사용). 꼭 필요하면 박스 길이·가로세로비로 대형/소형 2구분.
