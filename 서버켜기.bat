@echo off
chcp 65001 >nul
title 배차의 민족 - 상하역 관제 서버
cd /d "%~dp0"

rem 1) 파이썬 찾기
set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not defined PY (where py >nul 2>nul && set "PY=py")
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo [오류] 파이썬을 찾지 못했습니다. https://www.python.org 에서 설치해 주세요.
  pause
  exit /b 1
)

rem 2) 필요한 패키지가 없으면 설치 (처음 한 번)
"%PY%" -c "import fastapi, uvicorn, websockets" >nul 2>nul
if errorlevel 1 (
  echo 처음 실행이라 필요한 패키지를 설치합니다...
  "%PY%" -m pip install -r requirements.txt
)

rem 3) 3초 뒤 브라우저 열기
start "" /b cmd /c "timeout /t 3 >nul & start "" http://127.0.0.1:8000/"

echo.
echo  서버를 켭니다. 끄려면 이 창을 닫거나 Ctrl + C 를 누르세요.
echo  팀원은 아래에 나오는 "팀원 공유 주소"로 들어오면 됩니다.
echo.
"%PY%" app\server.py --lan

echo.
echo 서버가 꺼졌습니다.
pause
