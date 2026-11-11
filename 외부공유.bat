@echo off
rem 외부 공유 주소 열기 — Cloudflare 임시 터널(tools/share_tunnel.py)로 켜 둔 서버(8000)를 https://….trycloudflare.com 주소로 연다
rem 창을 닫으면 주소가 없어진다(다시 켜면 주소가 바뀜)
chcp 65001 >nul
title 배차의 민족 - 외부 공유 주소 (Cloudflare)
cd /d "%~dp0"

rem 서버켜기.bat 으로 서버를 먼저 켠 뒤 실행하세요.
rem 켤 때마다 https://○○○.trycloudflare.com 주소가 새로 나와요. 이 창을 닫으면 외부 접속이 끊겨요.

set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not defined PY (where py >nul 2>nul && set "PY=py")
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo [오류] 파이썬을 찾지 못했습니다.
  pause
  exit /b 1
)

"%PY%" tools\share_tunnel.py

echo.
pause
