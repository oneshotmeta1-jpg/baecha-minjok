"""외부 공유 주소 열기 (Cloudflare 임시 터널) — 외부공유.bat 이 실행한다.

- 켜져 있는 관제 서버(http://127.0.0.1:8000)에 https://○○○.trycloudflare.com 주소를 붙인다(계정 · 도메인 필요 없음, 무료).
- 켤 때마다 주소가 바뀐다. 이 창을 닫으면 외부 접속이 끊긴다(서버는 그대로).
- 주소를 아는 사람은 누구나 로그인 화면까지 들어올 수 있다 → 팀원에게만 알리고 시연이 끝나면 닫는다.
"""
import os
import re
import shutil
import subprocess
import sys
import urllib.request

PORT = int(os.environ.get("DOCK_PORT", "8000"))
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def find_cloudflared():
    for p in (shutil.which("cloudflared"), r"C:\Program Files (x86)\cloudflared\cloudflared.exe", r"C:\Program Files\cloudflared\cloudflared.exe"):
        if p and os.path.exists(p):
            return p
    return None


def server_up():
    try:
        return urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=3).status == 200
    except Exception:
        return False


def banner(url):
    line = "=" * 64
    print("\n" + line)
    print("  외부 공유 주소가 열렸어요 (이 창을 닫으면 끊겨요)")
    print(line)
    print(f"  휴대폰(모바일)   {url}/m/")
    print(f"  PC 관제 화면     {url}/")
    print(line)
    print("  · 휴대폰 데이터(LTE/5G) · 다른 와이파이에서도 들어와요.")
    print("  · 켤 때마다 주소가 바뀌어요. 팀원에게만 알려 주세요.")
    print("  · 모바일 주소는 클립보드에 복사해 뒀어요(카톡에 붙여넣기).")
    try:
        import qrcode   # 있으면 휴대폰 카메라로 찍을 QR도 (pip install qrcode)
        q = qrcode.QRCode(border=1)
        q.add_data(url + "/m/")
        q.print_ascii(invert=True)
    except Exception:
        pass
    print(line + "\n")
    try:
        subprocess.run("clip", input=(url + "/m/").encode("utf-16-le"), check=False, shell=True)
    except Exception:
        pass


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    cf = find_cloudflared()
    if not cf:
        print("[오류] cloudflared를 찾지 못했어요. https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/ 에서 설치해 주세요.")
        return 1
    if not server_up():
        print(f"[알림] 관제 서버(http://127.0.0.1:{PORT})가 꺼져 있어요. 먼저 서버켜기.bat 을 실행한 뒤 다시 켜 주세요.")
        return 1
    print("외부 주소를 만드는 중이에요 (10초쯤 걸려요)...")
    p = subprocess.Popen([cf, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{PORT}"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    shown = False
    try:
        for line in p.stdout:                    # 출력을 계속 읽어야 터널이 멈추지 않는다
            m = URL_RE.search(line)
            if m and not shown:
                banner(m.group(0))
                shown = True
            elif "ERR" in line and not shown:
                print(line.rstrip())
    except KeyboardInterrupt:
        pass
    finally:
        p.terminate()
    print("외부 공유를 껐어요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
