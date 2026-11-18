"""Cross-platform standalone entry for 8038 dual-edition (original + V2) workbench."""
import argparse
import os
from pathlib import Path
import shutil
import signal
import sys

ROOT = Path(__file__).resolve().parent.parent


def _discover_media_tools() -> tuple[str, str, list[str]]:
    exe_suffix = '.exe' if os.name == 'nt' else ''
    candidate_dirs = [
        ROOT / 'runtime' / 'ffmpeg' / 'bin',
        ROOT.parent / 'runtime' / 'ffmpeg' / 'bin',
        ROOT / 'ffmpeg' / 'bin',
    ]
    if os.name != 'nt':
        candidate_dirs.extend([Path('/opt/homebrew/bin'), Path('/usr/local/bin')])
    extra_dirs = [str(d.resolve()) for d in candidate_dirs if d.is_dir()]
    merged_path = os.pathsep.join(
        p for p in [*extra_dirs, *os.environ.get('PATH', '').split(os.pathsep)] if p
    )
    ffmpeg_bin = os.environ.get('V16_FFMPEG_EXE', '').strip()
    ffprobe_bin = os.environ.get('V16_FFPROBE_EXE', '').strip()
    for d in extra_dirs:
        if not ffmpeg_bin and (Path(d) / f'ffmpeg{exe_suffix}').is_file():
            ffmpeg_bin = str((Path(d) / f'ffmpeg{exe_suffix}').resolve())
        if not ffprobe_bin and (Path(d) / f'ffprobe{exe_suffix}').is_file():
            ffprobe_bin = str((Path(d) / f'ffprobe{exe_suffix}').resolve())
    if not ffmpeg_bin:
        ffmpeg_bin = shutil.which('ffmpeg', path=merged_path) or ''
    if not ffprobe_bin:
        ffprobe_bin = shutil.which('ffprobe', path=merged_path) or ''
    if not ffmpeg_bin:
        try:
            import imageio_ffmpeg
            ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe() or ''
        except Exception:
            pass
    for resolved in (ffmpeg_bin, ffprobe_bin):
        if resolved and Path(resolved).is_file():
            parent = str(Path(resolved).resolve().parent)
            if parent not in extra_dirs:
                extra_dirs.insert(0, parent)
    final_path = os.pathsep.join(
        p for p in [*extra_dirs, *os.environ.get('PATH', '').split(os.pathsep)] if p
    )
    return ffmpeg_bin, ffprobe_bin, [final_path]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default=os.environ.get('V16_HOST', '127.0.0.1' if os.name == 'nt' else '0.0.0.0'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8038')))
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Invalid local port')
    data = args.data_dir.expanduser().resolve()
    data.mkdir(parents=True, exist_ok=True)
    instance = f'v17-{args.port}'
    marker = data / '.v17_instance_id'
    if marker.exists() and marker.read_text(encoding='utf-8').strip() != instance:
        raise SystemExit('数据目录属于其他实例，请核对后再启动。')
    if not marker.exists():
        marker.write_text(instance + '\n', encoding='utf-8')
    for key in tuple(os.environ):
        if key.startswith(('V17_TENANT_', 'V17_MULTIUSER_', 'V17_RENDER_SLOT', 'V17_RENDER_ALLOWED_', 'V17_GLOBAL_FIFO')):
            os.environ.pop(key)
    ffmpeg_bin, ffprobe_bin, path_list = _discover_media_tools()
    os.environ['PATH'] = path_list[0]
    if os.name == 'nt':
        tess = ROOT / 'runtime/tesseract/tesseract.exe'
        if tess.is_file():
            os.environ.setdefault('V16_TESSERACT_EXE', str(tess))
            os.environ.setdefault('TESSDATA_PREFIX', str(tess.parent / 'tessdata'))
            os.environ['PATH'] = str(tess.parent) + os.pathsep + os.environ['PATH']
        os.environ.setdefault('V16_CAPTION_FONT', str(Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts/msyh.ttc'))
        os.environ['OMP_NUM_THREADS'] = '2'
        os.environ['OPENBLAS_NUM_THREADS'] = '2'
    if ffmpeg_bin:
        os.environ['V16_FFMPEG_EXE'] = ffmpeg_bin
    if ffprobe_bin:
        os.environ['V16_FFPROBE_EXE'] = ffprobe_bin
    py_exe = sys.executable
    if os.name == 'nt' and py_exe.lower().endswith('pythonw.exe'):
        console_py = Path(py_exe).with_name('python.exe')
        if console_py.is_file():
            py_exe = str(console_py)
    os.environ.update(
        PORT=str(args.port),
        V16_HOST=str(args.host),
        V16_DATA_DIR=str(data),
        V16_LOCAL_NO_VALIDATION='1',
        V17_TENANT_BACKEND='0',
        V17_STRICT_ERP_RELAY='0',
        V16_ERP_DIRECT_ENABLED='0',
        V17_INSTANCE_ID=instance,
        V17_SESSION_COOKIE_NAME=f'v17_{args.port}_session',
        V17_QUEUE_WORKERS='1',
        V16_PYTHON_EXE=py_exe,
        V16_VIDEO_ENCODER='auto',
        V16_GPU_PIPELINE='auto',
        V17_HARDCUT_DUB_MODE='1',
        V17_DISPLAY_NAME=f'胡文 · 快手背景音乐剪辑 · {args.port}',
        V16_CLIENT_NAME=f'Local-{args.port}',
        PYTHONUTF8='1',
        PYTHONIOENCODING='utf-8',
    )
    for name, filename in [
        ('V16_ERP_TOKEN_FILE', '.v16_erp_bearer_token'),
        ('V16_ERP_IDENTITY_FILE', '.v16_erp_identity.json'),
        ('V16_UPLOAD_ORDER_FILE', '.v16_upload_order.json'),
    ]:
        os.environ[name] = str(data / filename)
    import mac_performance
    os.environ.update(mac_performance.environment())
    os.chdir(data)
    if str(ROOT / 'app') not in sys.path:
        sys.path.insert(0, str(ROOT / 'app'))
    import v16_web_app as web
    web.ensure_dirs()
    web.cleanup_renderer_working_root_if_idle()
    web.ensure_task_queue_worker(create=True)
    for name in ('SIGTERM', 'SIGINT', 'SIGHUP'):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), web._handle_service_shutdown)
    web.app.run(host=str(args.host), port=args.port, debug=False, threaded=True)


if __name__ == '__main__':
    main()
