"""Hardware-sized local rendering; model and image-quality settings are untouched."""
import json
import os, platform, subprocess
from functools import lru_cache

@lru_cache(maxsize=1)
def detect():
    if platform.system() == 'Windows':
        try:
            summary = subprocess.run([
                'powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                '[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);'
                '$c=Get-CimInstance Win32_Processor | Select-Object -First 1;'
                '$m=Get-CimInstance Win32_ComputerSystem;'
                '@{chip=$c.Name;performance_cores=$c.NumberOfCores;logical=$c.NumberOfLogicalProcessors;'
                'memory_gib=[math]::Round($m.TotalPhysicalMemory/1GB);'
                'gpu=@(Get-CimInstance Win32_VideoController | ForEach-Object {$_.Name})}|ConvertTo-Json -Compress'],
                capture_output=True, encoding='utf-8-sig', timeout=10,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=True)
            return json.loads(summary.stdout)
        except (ValueError, OSError, subprocess.SubprocessError):
            return {'chip':platform.processor() or 'Windows PC','performance_cores':0,
                    'logical':os.cpu_count() or 2,'memory_gib':0,'gpu':[]}
    def value(name, default):
        try:
            return subprocess.check_output(['sysctl','-n',name],text=True,stderr=subprocess.DEVNULL,timeout=2).strip()
        except (OSError,subprocess.SubprocessError):return default
    if platform.system() != 'Darwin':return {'chip':'unknown','performance_cores':0,'memory_gib':0}
    try:
        return {'chip':value('machdep.cpu.brand_string','Apple Mac'),
                'performance_cores':int(value('hw.perflevel0.physicalcpu','0')),
                'memory_gib':int(value('hw.memsize','0'))//(1024**3)}
    except ValueError:return {'chip':'Apple Mac','performance_cores':0,'memory_gib':0}

@lru_cache(maxsize=4)
def _can_encode(ffmpeg, encoder):
    try:
        result = subprocess.run(
            [ffmpeg, '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', 'color=s=720x1280:r=30',
             '-frames:v', '2', '-pix_fmt', 'nv12', '-c:v', encoder, '-f', 'null', '-'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False

def profile(hardware=None):
    h=detect() if hardware is None else hardware
    if platform.system() == 'Windows' and not h['chip'].startswith('Apple'):
        gpus = ' '.join(h.get('gpu', [])).lower()
        ffmpeg = os.environ.get('V16_FFMPEG_EXE') or 'ffmpeg'
        encoder, label = 'cpu', 'CPU 兼容编码'
        for vendor, policy, description in [('intel','qsv','Intel Quick Sync 核显编码'),
                                            ('nvidia','nvenc','NVIDIA NVENC 编码')]:
            if vendor in gpus and _can_encode(ffmpeg, 'h264_' + policy):
                encoder, label = policy, description
                break
        return {**h,'name':'windows-' + encoder,
                'render_workers':1,'filter_threads':2,
                'fallback_threads':min(6,max(2,(h.get('logical') or 4)//2)),
                'final_filter_threads':2,'static_corner_alpha':False,'queue_workers':1,'encoder':encoder,
                'label':label + ' · 单任务模式'}
    fast=('M4 Max' in h['chip'] and h['performance_cores']>=10 and h['memory_gib']>=32)
    return {**h,'name':'m4max-local-fast-v2' if fast else 'mac-local-compatible',
            'render_workers':4 if fast else 2,'filter_threads':2 if fast else 0,
            'fallback_threads':8,'final_filter_threads':8 if fast else 0,'static_corner_alpha':fast,'queue_workers':1,'encoder':'auto',
            'label':'M4 Max 高性能 · 4 路素材 / 8 线程特效' if fast else 'Mac 本机硬件编码'}

def environment():
    p=profile()
    settings = {'V16_RENDER_WORKERS':str(p['render_workers']),
            'V16_FILTER_THREADS':str(p['filter_threads']),
            'V16_FFMPEG_THREADS':str(p['fallback_threads']),
            'V16_FINAL_FILTER_THREADS':str(p['final_filter_threads']),
            'V16_STATIC_CORNER_ALPHA':'1' if p['static_corner_alpha'] else '0',
            'V16_VIDEO_ENCODER':p['encoder'],'V17_QUEUE_WORKERS':'1'}
    if platform.system() == 'Windows' and p['encoder'] != 'nvenc':
        settings['V16_GPU_PIPELINE'] = 'off'
    return settings

def ffmpeg_command(command, threads):
    args=list(command)
    if not threads or not args or os.path.basename(str(args[0])).lower() not in {'ffmpeg','ffmpeg.exe'}:return args
    options=[]
    for option in ('-filter_threads','-filter_complex_threads'):
        if option not in args:options.extend([option,str(threads)])
    return [args[0],*options,*args[1:]]


def static_corner_alpha_enabled():
    return platform.system() == 'Darwin' and os.environ.get(
        'V16_STATIC_CORNER_ALPHA', '1' if profile()['static_corner_alpha'] else '0'
    ) == '1'


def corner_patch_filters(source, patch, crop, alpha, fps):
    """Cache only the invariant alpha plane; retain every moving color frame.

    GEQ's RGB path negotiates planar GBRAP. Preserve that exact conversion
    before splitting so this is pixel-identical to the old RGBA->GEQ path.
    Bound the loop to one small corner frame. PTS follow the normalized mask.
    """
    return [
        f"{source}{crop},format=rgba,format=gbrap,split=2[{patch}_color][{patch}_seed]",
        f"[{patch}_seed]alphaextract,geq=lum='{alpha}',trim=end_frame=1,"
        f"loop=loop=-1:size=1:start=0,setpts=N/({fps}*TB)[{patch}_alpha]",
        f"[{patch}_color][{patch}_alpha]alphamerge=shortest=1[{patch}]",
    ]


def final_render_command(command):
    args=list(command)
    if platform.system() not in {'Darwin','Windows'} or '-filter_complex_threads' in args:
        return args
    try:
        threads=max(0,min(16,int(os.environ.get('V16_FINAL_FILTER_THREADS', profile()['final_filter_threads']))))
    except (TypeError, ValueError):
        threads=0
    if threads and args and os.path.basename(str(args[0])).lower() in {'ffmpeg','ffmpeg.exe'} and '-filter_complex' in args:
        return [args[0],'-filter_complex_threads',str(threads),*args[1:]]
    return args
