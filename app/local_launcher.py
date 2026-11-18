"""Start the standalone 8038 workbench once, then open browser cleanly on macOS or Windows."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

from local_edition import RELEASE_ID

ROOT = Path(__file__).resolve().parent.parent
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def healthy(url: str, port: int) -> bool:
    try:
        with opener.open(url + 'status', timeout=2) as r:
            d = json.load(r)
        return d.get('instance_id') == f'v17-{port}' and d.get('release_id') == RELEASE_ID
    except Exception:
        return False


def _open_browser(target_url: str) -> None:
    if os.name == 'nt':
        for chrome_path in (
            Path(os.environ.get('PROGRAMFILES', r'C:\Program Files')) / 'Google/Chrome/Application/chrome.exe',
            Path(os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)')) / 'Google/Chrome/Application/chrome.exe',
            Path(os.environ.get('LOCALAPPDATA', '')) / 'Google/Chrome/Application/chrome.exe',
        ):
            if chrome_path.is_file():
                subprocess.Popen(
                    [str(chrome_path), target_url],
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )
                return
        webbrowser.open(target_url)
    else:
        res = subprocess.run(['open', '-a', 'Google Chrome', target_url], check=False)
        if res.returncode != 0:
            webbrowser.open(target_url)


def _detect_lan_ips() -> list[str]:
    ips: list[str] = []
    if sys.platform == 'darwin':
        for iface in ('en0', 'en1', 'en2', 'en3', 'en4', 'en5', 'en6', 'bridge0'):
            res = subprocess.run(['ipconfig', 'getifaddr', iface], capture_output=True, text=True)
            ip = res.stdout.strip()
            if ip and not ip.startswith(('127.', '169.254.', '198.18.')) and ip not in ips:
                ips.append(ip)
    if not ips:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if ip and not ip.startswith(('127.', '169.254.', '198.18.')) and ip not in ips:
                    ips.append(ip)
        except Exception:
            pass
    return ips


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1' if os.name == 'nt' else '0.0.0.0')
    parser.add_argument('--port', type=int, default=8038)
    parser.add_argument('--no-browser', action='store_true')
    args, _ = parser.parse_known_args()
    port = args.port
    lan_ips = _detect_lan_ips() if args.host == '0.0.0.0' else []
    local_url = f'http://127.0.0.1:{port}/'
    check_url = f'http://{lan_ips[0]}:{port}/' if (args.host == '0.0.0.0' and lan_ips) else local_url

    if not healthy(check_url, port):
        if args.host == '0.0.0.0' and healthy(local_url, port):
            raise SystemExit(f'工作台已在本机运行。如要切换为局域网模式，请先结束该实例：{local_url}')
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1', port)) == 0:
                raise SystemExit(f'{port} 已被其他程序占用，请关闭该实例后重试。')
        data = ROOT / 'data'
        data.mkdir(exist_ok=True)
        cmd = [sys.executable, str(ROOT / 'app/run_local.py'), '--host', args.host, '--port', str(port)]
        popen_kwargs = dict(
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
        )
        if os.name == 'nt':
            popen_kwargs['creationflags'] = (
                getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
                | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)
            )
        else:
            popen_kwargs['start_new_session'] = True
        with (data / f'local{port}.log').open('ab') as log:
            process = subprocess.Popen(cmd, stdout=log, stderr=log, **popen_kwargs)
        for _ in range(60):
            if healthy(check_url, port):
                break
            if process.poll() is not None:
                raise SystemExit(f'工作台启动失败，请查看 {data / f"local{port}.log"}。')
            time.sleep(1)
        else:
            raise SystemExit(f'启动未完成，请查看 data/local{port}.log。')
    open_target = (f'http://{lan_ips[0]}:{port}/#pools' if lan_ips else local_url + '#pools')
    if not args.no_browser:
        _open_browser(open_target)
    print('自动剪辑工作台已启动（本机）：' + local_url + '#pools')
    for ip in lan_ips:
        print(f'自动剪辑工作台局域网地址：http://{ip}:{port}/#pools')


if __name__ == '__main__':
    main()
