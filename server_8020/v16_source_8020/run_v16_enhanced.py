import base64
import concurrent.futures
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


_app_dir = os.path.dirname(os.path.abspath(__file__))
if _app_dir and _app_dir not in sys.path:
    sys.path.insert(0, _app_dir)
_runtime_tool_dirs = [
    os.path.join(os.path.dirname(_app_dir), "runtime", "ffmpeg", "bin"),
    os.path.join(os.path.dirname(os.path.dirname(_app_dir)), "runtime", "ffmpeg", "bin"),
    os.path.join(os.path.dirname(_app_dir), "ffmpeg", "bin"),
]
for _env_exe_key in ("V16_FFMPEG_EXE", "V16_FFPROBE_EXE"):
    _env_exe_val = os.environ.get(_env_exe_key, "").strip().strip('"')
    if _env_exe_val and os.path.isfile(_env_exe_val):
        _runtime_tool_dirs.insert(0, os.path.dirname(os.path.abspath(_env_exe_val)))
if os.name != "nt":
    _runtime_tool_dirs.extend(
        [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/Library/Frameworks/Python.framework/Versions/3.12/bin",
        ]
    )
_existing_path_dirs = os.environ.get("PATH", "").split(os.pathsep)
os.environ["PATH"] = os.pathsep.join(
    path
    for path in [
        *(d for d in _runtime_tool_dirs if os.path.isdir(d) or os.name != "nt"),
        *_existing_path_dirs,
    ]
    if path
)

if os.name == "nt":
    _orig_subprocess_run = subprocess.run

    def _win_quiet_subprocess_run(*args, **kwargs):
        kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0x08000000
        )
        if kwargs.get("text"):
            kwargs.setdefault("encoding", "utf-8")
            kwargs.setdefault("errors", "replace")
        return _orig_subprocess_run(*args, **kwargs)

    subprocess.run = _win_quiet_subprocess_run

PY312 = (
    os.environ.get("V16_PYTHON_EXE", "").strip()
    or "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
)
BASE_DIR = os.environ.get(
    "BASE_DIR",
    os.path.dirname(os.path.abspath(__file__)),
)
MATERIAL_BASE_DIR = os.environ.get("V16_MATERIAL_BASE_DIR", BASE_DIR)
TMP_CONTAINER_DIR = os.path.abspath(
    os.path.join(BASE_DIR, "tmp_process_v16_enhanced")
)
PERSISTENT_CACHE_ROOT = os.path.abspath(os.path.join(BASE_DIR, ".v16_cache"))
_TASK_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")

# Validate the retired style switch before reserving a temporary directory or
# performing any other filesystem mutation. Invalid configuration must leave no
# stale job root behind.
REQUESTED_FAST_DEDUP_STYLE = os.environ.get(
    "V16_FAST_DEDUP_STYLE", "clean"
).strip().lower()
if REQUESTED_FAST_DEDUP_STYLE != "clean":
    raise ValueError(
        "V16_FAST_DEDUP_STYLE 只允许 clean；黑白、线稿和漫画快速风格已永久停用。"
    )
FAST_DEDUP_STYLE = "clean"
FAST_DEDUP_STYLES = {"clean"}


def _is_path_link(path):
    """Treat both symbolic links and Windows junctions as unsafe roots."""
    is_junction = getattr(os.path, "isjunction", None)
    return os.path.islink(path) or bool(is_junction and is_junction(path))


def _validated_task_id():
    """Return the optional queue task id, rejecting path-shaped values."""
    task_id = os.environ.get("KUAISHOU_TASK_ID", "").strip()
    if task_id and not _TASK_ID_PATTERN.fullmatch(task_id):
        raise RuntimeError(
            "KUAISHOU_TASK_ID 无效；只允许 1–128 位 ASCII 字母、数字、"
            "下划线或连字符，且首位必须是字母或数字。"
        )
    return task_id


def _validated_tmp_container(create=False):
    """Return the fixed task-temp container without following it elsewhere."""
    base = os.path.realpath(os.path.abspath(BASE_DIR))
    container_path = os.path.abspath(TMP_CONTAINER_DIR)
    container = os.path.realpath(container_path)
    if (
        _is_path_link(container_path)
        or os.path.dirname(container) != base
        or os.path.basename(container) != "tmp_process_v16_enhanced"
    ):
        raise RuntimeError("制作临时目录容器越出当前数据目录，已拒绝使用。")
    if create:
        os.makedirs(container_path, exist_ok=True)
        # Re-resolve after creation to catch a pre-existing junction/symlink.
        container = os.path.realpath(container_path)
        if (
            _is_path_link(container_path)
            or os.path.dirname(container) != base
            or os.path.basename(container) != "tmp_process_v16_enhanced"
        ):
            raise RuntimeError("制作临时目录容器不安全，已拒绝使用。")
    return container


def _reserve_task_tmp_directory():
    """Atomically reserve one private temp root for this renderer process.

    ``KUAISHOU_JOB_TMP`` may name an absolute path, or one leaf name, but the
    resolved directory must be a direct child of ``TMP_CONTAINER_DIR`` and
    must not already exist. Requiring a fresh directory turns an accidental
    duplicate assignment into a safe startup failure instead of two workers
    sharing and deleting each other's files.
    """
    task_id = _validated_task_id()
    container = _validated_tmp_container(create=True)
    configured = os.environ.get("KUAISHOU_JOB_TMP", "").strip()
    if configured:
        if os.path.isabs(configured):
            candidate_path = os.path.abspath(configured)
        else:
            if os.path.basename(configured) != configured or configured in {".", ".."}:
                raise RuntimeError(
                    "相对 KUAISHOU_JOB_TMP 只能是临时目录容器下的单个目录名。"
                )
            candidate_path = os.path.abspath(os.path.join(container, configured))
        candidate = os.path.realpath(candidate_path)
        if (
            os.path.dirname(candidate) != container
            or candidate == container
            or _is_path_link(candidate_path)
        ):
            raise RuntimeError(
                "KUAISHOU_JOB_TMP 必须是当前数据目录临时容器下的直属任务目录。"
            )
        try:
            os.mkdir(candidate_path)
        except FileExistsError as exc:
            raise RuntimeError(
                "KUAISHOU_JOB_TMP 已存在；为避免并发任务共享临时目录，"
                "每个进程必须使用一个尚不存在的唯一目录。"
            ) from exc
        return os.path.realpath(candidate_path)

    if task_id:
        task_label = task_id
        if len(task_label) > 48:
            digest = hashlib.sha256(task_label.encode("ascii")).hexdigest()[:16]
            task_label = f"{task_label[:31]}-{digest}"
        prefix = f"task-{task_label}-pid-{os.getpid()}-"
    else:
        prefix = f"pid-{os.getpid()}-"
    return os.path.realpath(tempfile.mkdtemp(prefix=prefix, dir=container))


TMP_DIR = _reserve_task_tmp_directory()
_RESERVED_TMP_DIR = TMP_DIR
OUT_FILE = os.environ.get(
    "OUT_FILE",
    os.path.join(BASE_DIR, "成品_自动化V16_功能优化增强版.mp4"),
)
FISSION_VARIANT_SEED = os.environ.get("V16_FISSION_VARIANT_SEED", "").strip()
FISSION_SHUFFLE_ALL = os.environ.get("V16_FISSION_SHUFFLE_ALL", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
FISSION_CORE_OPENER = os.environ.get(
    "V16_FISSION_CORE_OPENER", "0"
).strip().lower() in {"1", "true", "yes", "on"}
THREE_POOL_MODE = os.environ.get("V16_THREE_POOL_MODE", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUTO_MASK_FROM_SHUFFLE = os.environ.get(
    "V16_AUTO_MASK_FROM_SHUFFLE", "1" if THREE_POOL_MODE else "0"
).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
STABLE_SPLIT_SINGLE_PASS = os.environ.get(
    "V16_STABLE_SPLIT_SINGLE_PASS", "1"
).strip().lower() not in {"0", "false", "no", "off"}
MASK_SOURCE_NAME = os.environ.get("V16_MASK_SOURCE_NAME", "").strip()
EDITION_MODE = os.environ.get("V17_EDITION_MODE", "").strip().lower()
_LEGACY_HARDCUT_DUB_ENV = os.environ.get("V17_HARDCUT_DUB_MODE", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
if EDITION_MODE in {"original", "v1", "legacy", "8018"}:
    HARDCUT_MODE = False
    MUTE_ORIGINAL_AUDIO = False
    PRESERVE_RAW_CORE = os.environ.get("V17_PRESERVE_RAW_CORE", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
else:
    HARDCUT_MODE = os.environ.get(
        "V16_HARDCUT_MODE", "1" if _LEGACY_HARDCUT_DUB_ENV else "0"
    ).strip().lower() in {"1", "true", "yes", "on"}
    MUTE_ORIGINAL_AUDIO = os.environ.get(
        "V16_MUTE_ORIGINAL_AUDIO", "1" if _LEGACY_HARDCUT_DUB_ENV else "0"
    ).strip().lower() in {"1", "true", "yes", "on"}
    PRESERVE_RAW_CORE = os.environ.get(
        "V17_PRESERVE_RAW_CORE",
        "1" if (EDITION_MODE == "v2" or HARDCUT_MODE or _LEGACY_HARDCUT_DUB_ENV) else "0",
    ).strip().lower() in {"1", "true", "yes", "on"}
HARDCUT_DUB_MODE = HARDCUT_MODE
if HARDCUT_MODE or PRESERVE_RAW_CORE:
    import hardcut_audio as _hc_budget_mod

    BUDGET_SLICE_FLOOR_SECONDS = _hc_budget_mod.parse_slice_seconds(
        os.environ.get("V16_FIXED_SLICE_SECONDS", "3.0")
    )
    if HARDCUT_MODE:
        hardcut_audio = _hc_budget_mod
        FIXED_SLICE_SECONDS = BUDGET_SLICE_FLOOR_SECONDS
    else:
        hardcut_audio = None
        FIXED_SLICE_SECONDS = 0.0
else:
    hardcut_audio = None
    FIXED_SLICE_SECONDS = 0.0
    BUDGET_SLICE_FLOOR_SECONDS = 3.0
BATCH_USED_SLICES_FILE = os.environ.get("V16_BATCH_USED_SLICES_FILE", "").strip()
_RAW_SUBTITLE_MASKS_ENV = os.environ.get("V17_SUBTITLE_MASKS_JSON", "").strip()
if _RAW_SUBTITLE_MASKS_ENV:
    import hardcut_audio as _hc_mask_mod

    SUBTITLE_MASKS_CONFIG = _hc_mask_mod.normalize_subtitle_masks(
        _RAW_SUBTITLE_MASKS_ENV, strict=False
    )
else:
    _hc_mask_mod = None
    SUBTITLE_MASKS_CONFIG = {
        "enabled": False,
        "style": "blur",
        "strength": 55,
        "boxes": [],
    }
_DERIVED_SHUFFLE_SOURCE_MAP = {}


def _subtitle_mask_vf_for_source(input_path):
    if not SUBTITLE_MASKS_CONFIG.get("enabled") or not SUBTITLE_MASKS_CONFIG.get("boxes"):
        return []
    mod = _hc_mask_mod
    if mod is None:
        import hardcut_audio as mod
    resolved_name = _DERIVED_SHUFFLE_SOURCE_MAP.get(
        os.path.abspath(input_path), os.path.basename(input_path)
    )
    return mod.build_subtitle_mask_vf_filters(
        SUBTITLE_MASKS_CONFIG,
        width=W,
        height=H,
        source_name=resolved_name,
    )
try:
    FISSION_MATERIAL_PLAN_INDEX = max(
        0, int(os.environ.get("V16_FISSION_MATERIAL_PLAN_INDEX", "0"))
    )
except (TypeError, ValueError):
    FISSION_MATERIAL_PLAN_INDEX = 0
try:
    SHUFFLE_WINDOW_INDEX = max(
        0,
        int(
            os.environ.get(
                "V16_SHUFFLE_WINDOW_INDEX",
                str(FISSION_MATERIAL_PLAN_INDEX),
            )
        ),
    )
except (TypeError, ValueError):
    SHUFFLE_WINDOW_INDEX = FISSION_MATERIAL_PLAN_INDEX
try:
    CORE_WINDOW_INDEX = max(
        0, int(os.environ.get("V16_CORE_WINDOW_INDEX", "0"))
    )
except (TypeError, ValueError):
    CORE_WINDOW_INDEX = 0
SOURCE_WINDOW_AUDIT = []
try:
    MEDIA_ANALYSIS_TIMEOUT_SECONDS = max(
        15.0,
        min(
            300.0,
            float(os.environ.get("V16_MEDIA_ANALYSIS_TIMEOUT_SECONDS", "90")),
        ),
    )
except (TypeError, ValueError):
    MEDIA_ANALYSIS_TIMEOUT_SECONDS = 90.0
CORNER_MASK_REVEAL_PRESETS = {
    "off": 0,
    "gentle": 48,
    "standard": 72,
    "strong": 96,
}
CORNER_MASK_REVEAL_RADIUS_MAX = 300


def _corner_mask_mode_for_radius(radius):
    if radius <= 0:
        return "off"
    if radius <= CORNER_MASK_REVEAL_PRESETS["gentle"]:
        return "gentle"
    if radius <= CORNER_MASK_REVEAL_PRESETS["standard"]:
        return "standard"
    return "strong"


def _validated_corner_mask_radius(value):
    if isinstance(value, bool):
        raise ValueError("四角露底蒙版圆角必须是 0–300px 的整数。")
    try:
        radius_value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("四角露底蒙版圆角必须是 0–300px 的整数。") from exc
    geometry_max = min(
        CORNER_MASK_REVEAL_RADIUS_MAX,
        (720 - 2) // 2,
        (1280 - 2) // 2,
    )
    if (
        not math.isfinite(radius_value)
        or not 0.0 <= radius_value <= geometry_max
        or not math.isclose(radius_value, round(radius_value), abs_tol=1e-9)
    ):
        raise ValueError(
            f"四角露底蒙版圆角必须是 0–{geometry_max}px 的整数。"
        )
    return int(round(radius_value))


CORNER_MASK_REVEAL_MODE = os.environ.get(
    "V16_CORNER_MASK_REVEAL_MODE", "off"
).strip().lower()
if CORNER_MASK_REVEAL_MODE not in CORNER_MASK_REVEAL_PRESETS:
    raise ValueError(
        "四角露底蒙版模式无效；只允许 off、gentle、standard 或 strong。"
    )
_corner_radius_raw = os.environ.get("V16_CORNER_MASK_REVEAL_RADIUS_PX", "").strip()
if _corner_radius_raw:
    CORNER_MASK_REVEAL_RADIUS = _validated_corner_mask_radius(_corner_radius_raw)
    expected_corner_mode = _corner_mask_mode_for_radius(CORNER_MASK_REVEAL_RADIUS)
    if CORNER_MASK_REVEAL_MODE != expected_corner_mode:
        raise ValueError("四角露底蒙版档位和圆角大小不一致。")
else:
    CORNER_MASK_REVEAL_RADIUS = CORNER_MASK_REVEAL_PRESETS[
        CORNER_MASK_REVEAL_MODE
    ]
CORNER_MASK_REVEAL_ACTIVE = CORNER_MASK_REVEAL_RADIUS > 0
DEDUP_HASH_FILE = os.environ.get("V16_DEDUP_HASH_FILE", "").strip()
FIXED_STAGE_CACHE_DIR = os.environ.get("V16_FIXED_STAGE_CACHE_DIR", "").strip()
FIXED_STAGE_CACHE_KEY = os.environ.get("V16_FIXED_STAGE_CACHE_KEY", "").strip()
PRIOR_DEDUP_HASHES_JSON = os.environ.get("V16_PRIOR_DEDUP_HASHES_JSON", "").strip()
try:
    FISSION_SIMILARITY_LIMIT = float(os.environ.get("V16_FISSION_SIMILARITY_LIMIT", "40"))
except (TypeError, ValueError):
    FISSION_SIMILARITY_LIMIT = 40.0

CORE_DIR = os.path.join(MATERIAL_BASE_DIR, "核心素材")
QUAL_DIR = os.path.join(MATERIAL_BASE_DIR, "优质素材池")
DEDUP_DIR = os.path.join(MATERIAL_BASE_DIR, "去重素材池")
SHUFFLE_DIR = os.path.join(MATERIAL_BASE_DIR, "打乱素材池")
DEDUP2_DIR = os.path.join(MATERIAL_BASE_DIR, "去重素材池2")
DEDUP2_ENABLED = os.environ.get("V16_DEDUP2_ENABLED", "0") != "0"
MASK_DIR = os.path.join(MATERIAL_BASE_DIR, "蒙版素材")
STICKER_DIR = os.path.join(MATERIAL_BASE_DIR, "贴纸素材池")

QUAL_REQUIREMENT_FILE = os.path.join(BASE_DIR, "优质素材池_生成要求.txt")
DEDUP_REQUIREMENT_FILE = os.path.join(BASE_DIR, "去重素材池_生成要求.txt")
SHUFFLE_REQUIREMENT_FILE = os.path.join(BASE_DIR, "打乱素材池_生成要求.txt")
DEDUP2_REQUIREMENT_FILE = os.path.join(BASE_DIR, "去重素材池2_生成要求.txt")
PROMO_COPY_FILE = os.path.join(BASE_DIR, "去重素材池2_自动推广文案.txt")
PROMO_AUDIO_FILE = os.path.join(BASE_DIR, "去重素材池2_自动配音.mp3")
PROMO_META_FILE = os.path.join(BASE_DIR, "去重素材池2_AI生成信息.json")
COMPLIANCE_REPORT_FILE = os.environ.get(
    "V16_COMPLIANCE_REPORT_FILE",
    os.path.join(BASE_DIR, "V17_全片合规扫描报告.json"),
)

W, H = 720, 1280


_VIDEO_ENCODER_LOCK = threading.Lock()
_VIDEO_ENCODER_ARGS = None
_VIDEO_ENCODER_REASON = ""
_ENCODER_METRICS_LOCK = threading.Lock()
_ENCODER_METRICS = {
    "h264_nvenc": {"attempts": 0, "successes": 0, "failures": 0, "seconds": 0.0},
    "h264_videotoolbox": {
        "attempts": 0,
        "successes": 0,
        "failures": 0,
        "seconds": 0.0,
    },
    "libx264": {"attempts": 0, "successes": 0, "failures": 0, "seconds": 0.0},
}
_ENCODER_FALLBACK_COUNT = 0
_ENCODER_PROCESS_STARTED = time.perf_counter()
_GPU_PIPELINE_LOCK = threading.Lock()
_GPU_PIPELINE_DISABLED_REASON = ""
_GPU_PIPELINE_METRICS = {
    "attempts": 0,
    "successes": 0,
    "failures": 0,
    "fallbacks": 0,
    "seconds": 0.0,
}
_GPU_CAPABILITIES_LOCK = threading.Lock()
_GPU_CAPABILITIES_CACHE = None
_MEDIA_PROBE_CACHE_LOCK = threading.Lock()
_MEDIA_PROBE_CACHE = {}
_MEDIA_PROBE_CACHE_HITS = 0
_MEDIA_PROBE_CACHE_MISSES = 0
_MODULE_AVAILABILITY_LOCK = threading.Lock()
_MODULE_AVAILABILITY_CACHE = {}
_PERF_STAGE_LOCK = threading.Lock()
_PERF_STAGE_METRICS = {}
_SUBTITLE_TRANSCRIBE_LOCK = threading.Lock()


def _bounded_env_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), min(int(maximum), value))


WINDOWS_CPU_STABILITY_PROFILE = sys.platform == "win32"
FFMPEG_THREADS = _bounded_env_int(
    "V16_FFMPEG_THREADS",
    0,
    0,
    12,
)
NVIDIA_GPU_INDEX = _bounded_env_int(
    "V16_NVIDIA_GPU_INDEX",
    0,
    0,
    15,
)


def _gpu_pipeline_policy():
    """Return the bounded policy for the fully GPU-resident fast path."""
    raw = os.environ.get(
        "V16_GPU_PIPELINE",
        os.environ.get("V16_GPU_DECODE", "auto"),
    ).strip().lower()
    aliases = {
        "": "auto",
        "auto": "auto",
        "1": "auto",
        "true": "auto",
        "yes": "auto",
        "on": "auto",
        "cuda": "auto",
        "nvdec": "auto",
        "0": "off",
        "false": "off",
        "no": "off",
        "disabled": "off",
        "cpu": "off",
        "strict": "strict",
        "cuda-strict": "strict",
        "nvdec-strict": "strict",
    }
    return aliases.get(raw, "off")


def _gpu_pipeline_disabled_reason():
    with _GPU_PIPELINE_LOCK:
        return _GPU_PIPELINE_DISABLED_REASON


def _disable_gpu_pipeline_for_current_run(reason):
    global _GPU_PIPELINE_DISABLED_REASON
    with _GPU_PIPELINE_LOCK:
        if not _GPU_PIPELINE_DISABLED_REASON:
            _GPU_PIPELINE_DISABLED_REASON = str(reason or "CUDA pipeline failed")


def _probe_gpu_pipeline_capabilities():
    """Probe the pinned FFmpeg feature surface once per renderer process."""
    global _GPU_CAPABILITIES_CACHE
    with _GPU_CAPABILITIES_LOCK:
        if _GPU_CAPABILITIES_CACHE is not None:
            return _GPU_CAPABILITIES_CACHE
        checks = (
            ("hwaccels", ["ffmpeg", "-hide_banner", "-hwaccels"], ("cuda",)),
            (
                "filters",
                ["ffmpeg", "-hide_banner", "-filters"],
                ("crop", "scale_cuda", "fps", "setsar"),
            ),
            (
                "encoders",
                ["ffmpeg", "-hide_banner", "-encoders"],
                ("h264_nvenc",),
            ),
        )
        for label, command, required_names in checks:
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                _GPU_CAPABILITIES_CACHE = (False, f"{label}-probe:{exc}")
                return _GPU_CAPABILITIES_CACHE
            listing = f"{result.stdout}\n{result.stderr}".lower()
            missing = [
                name
                for name in required_names
                if not re.search(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])", listing)
            ]
            if result.returncode != 0 or missing:
                detail = ",".join(missing) if missing else f"exit-{result.returncode}"
                _GPU_CAPABILITIES_CACHE = (False, f"{label}-missing:{detail}")
                return _GPU_CAPABILITIES_CACHE
        _GPU_CAPABILITIES_CACHE = (True, "")
        return _GPU_CAPABILITIES_CACHE


def _record_gpu_pipeline_attempt(elapsed_seconds, returncode, fallback=False):
    with _GPU_PIPELINE_LOCK:
        _GPU_PIPELINE_METRICS["attempts"] += 1
        _GPU_PIPELINE_METRICS["seconds"] += max(0.0, float(elapsed_seconds))
        if int(returncode) == 0:
            _GPU_PIPELINE_METRICS["successes"] += 1
        else:
            _GPU_PIPELINE_METRICS["failures"] += 1
        if fallback:
            _GPU_PIPELINE_METRICS["fallbacks"] += 1
    print(
        "V16_GPU_PIPELINE_STEP "
        + json.dumps(
            {
                "pipeline": "nvdec-cuda-nvenc",
                "elapsed_seconds": round(max(0.0, float(elapsed_seconds)), 3),
                "returncode": int(returncode),
                "fallback_planned": bool(fallback),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _record_perf_stage(stage, elapsed_seconds, **details):
    elapsed = max(0.0, float(elapsed_seconds))
    with _PERF_STAGE_LOCK:
        metric = _PERF_STAGE_METRICS.setdefault(
            str(stage),
            {"count": 0, "seconds": 0.0},
        )
        metric["count"] += 1
        metric["seconds"] += elapsed
    payload = {
        "stage": str(stage),
        "elapsed_seconds": round(elapsed, 3),
        **details,
    }
    print(
        "V16_PERF_STAGE "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _video_encoder_policy():
    policy = os.environ.get("V16_VIDEO_ENCODER", "auto").strip().lower()
    aliases = {
        "": "auto",
        "gpu": "nvidia-auto",
        "nvidia": "nvidia-auto",
        "nvenc": "nvidia-auto",
        "nvidia-strict": "nvidia-strict",
        "nvenc-strict": "nvidia-strict",
        "gpu-strict": "nvidia-strict",
        "apple": "videotoolbox",
        "software": "cpu",
        "libx264": "cpu",
        "off": "cpu",
        "disabled": "cpu",
    }
    return aliases.get(policy, policy)


def _hardware_encoder_allowed():
    return os.environ.get("V16_USE_HARDWARE_ENCODER", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _probe_videotoolbox_encoder():
    """Return whether VideoToolbox can create a real compression session."""
    try:
        listed = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=10,
        )
        if listed.returncode != 0 or "h264_videotoolbox" not in listed.stdout:
            return False, "FFmpeg 未提供 h264_videotoolbox"
        probe = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "color=c=black:s=64x64:r=1:d=0.04", "-frames:v", "1",
                "-an", "-c:v", "h264_videotoolbox", "-allow_sw", "1",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if probe.returncode == 0:
        return True, ""
    detail = next(
        (line.strip() for line in probe.stderr.splitlines() if line.strip()),
        f"退出码 {probe.returncode}",
    )
    return False, detail


def _probe_nvenc_encoder():
    """Return whether FFmpeg can open a real NVIDIA H.264 NVENC session."""
    try:
        listed = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=10,
        )
        encoder_listing = f"{listed.stdout}\n{listed.stderr}"
        if listed.returncode != 0 or "h264_nvenc" not in encoder_listing:
            return False, "FFmpeg 未提供 h264_nvenc"
        probe = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=1280x720:r=30:d=1",
                "-frames:v",
                "30",
                "-an",
                "-c:v",
                "h264_nvenc",
                "-gpu",
                str(NVIDIA_GPU_INDEX),
                "-preset",
                "p4",
                "-tune",
                "hq",
                "-rc",
                "vbr",
                "-cq",
                "23",
                "-b:v",
                "0",
                "-profile:v",
                "high",
                "-pix_fmt",
                "yuv420p",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if probe.returncode == 0:
        return True, ""
    detail_lines = [line.strip() for line in probe.stderr.splitlines() if line.strip()]
    return False, "；".join(detail_lines[-3:]) or f"退出码 {probe.returncode}"


def _select_video_encoder():
    policy = _video_encoder_policy()
    hardware_allowed = _hardware_encoder_allowed()
    if policy == "nvidia-strict" and not hardware_allowed:
        raise RuntimeError(
            "NVIDIA NVENC 严格模式已启用，但遗留环境变量 "
            "V16_USE_HARDWARE_ENCODER 禁用了硬件编码；"
            "已拒绝回退 CPU，避免生成伪 GPU 测速样本。"
        )
    if not hardware_allowed or policy == "cpu":
        return "libx264", "已选择 CPU 编码"

    if policy in {"nvidia-auto", "nvidia-strict"} or (
        policy == "auto" and sys.platform == "win32"
    ):
        available, reason = _probe_nvenc_encoder()
        if available:
            return "nvenc", ""
        if policy == "nvidia-strict":
            raise RuntimeError(f"NVIDIA NVENC 严格模式验收失败：{reason}")
        return "libx264", reason

    if policy == "videotoolbox" or (
        policy == "auto" and sys.platform == "darwin"
    ):
        available, reason = _probe_videotoolbox_encoder()
        return ("videotoolbox", "") if available else ("libx264", reason)

    return "libx264", f"当前平台不支持编码策略 {policy}"


def _encoder_codec_name(mode):
    return {
        "nvenc": "h264_nvenc",
        "videotoolbox": "h264_videotoolbox",
        "libx264": "libx264",
    }.get(str(mode or ""), str(mode or "unselected"))


def _video_encoder_args(crf=23, preset="veryfast"):
    """Select a real-probed hardware encoder, with a compatible CPU fallback."""
    global _VIDEO_ENCODER_ARGS, _VIDEO_ENCODER_REASON
    with _VIDEO_ENCODER_LOCK:
        if _VIDEO_ENCODER_ARGS is None:
            _VIDEO_ENCODER_ARGS, _VIDEO_ENCODER_REASON = _select_video_encoder()
            status = {
                "selected": _encoder_codec_name(_VIDEO_ENCODER_ARGS),
                "mode": _VIDEO_ENCODER_ARGS,
                "policy": _video_encoder_policy(),
                "gpu_index": NVIDIA_GPU_INDEX,
                "fallback": _VIDEO_ENCODER_ARGS == "libx264"
                and bool(_VIDEO_ENCODER_REASON)
                and _video_encoder_policy() != "cpu",
                "reason": _VIDEO_ENCODER_REASON,
            }
            print(
                "V16_ENCODER_STATUS "
                + json.dumps(status, ensure_ascii=False, separators=(",", ":"))
            )
            if _VIDEO_ENCODER_ARGS == "nvenc":
                print(
                    "   -> 视频编码：NVIDIA H.264 NVENC 硬件加速"
                    f"（GPU {NVIDIA_GPU_INDEX}，真实编码探测通过）"
                )
            elif _VIDEO_ENCODER_ARGS == "videotoolbox":
                print("   -> 视频编码：Apple VideoToolbox 硬件加速（实测可用）")
            else:
                suffix = f"；{_VIDEO_ENCODER_REASON}" if _VIDEO_ENCODER_REASON else ""
                print(f"   -> 视频编码：CPU libx264 兼容模式{suffix}")
    if _VIDEO_ENCODER_ARGS == "nvenc":
        # NVDEC/CUDA is enabled only by the separately assembled, capability-
        # gated normalize graph. Complex filters and OpenCV remain on CPU so
        # software-only filters never receive incompatible CUDA frames. NVENC
        # CQ and x264 CRF are not an equivalent quality scale; matching the
        # requested number is the balanced starting point for the RTX 4060.
        cq = max(0, min(51, int(crf)))
        return [
            "-c:v",
            "h264_nvenc",
            "-gpu",
            str(NVIDIA_GPU_INDEX),
            "-preset",
            "p4",
            "-tune",
            "hq",
            "-rc",
            "vbr",
            "-cq",
            str(cq),
            "-b:v",
            "0",
            "-multipass",
            "disabled",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
        ]
    if _VIDEO_ENCODER_ARGS == "videotoolbox":
        # VideoToolbox uses a 0-100 quality scale. These values retain the
        # visual quality of the existing CRF settings while moving encoding
        # onto the M2 media engine. Audio/filter parameters remain untouched.
        quality = max(55, min(78, 88 - int(crf)))
        return [
            "-c:v", "h264_videotoolbox",
            "-q:v", str(quality),
            "-profile:v", "high",
            "-pix_fmt", "yuv420p",
            "-realtime", "1",
            "-prio_speed", "1",
            "-allow_sw", "1",
        ]
    arguments = [
        "-c:v", "libx264",
        "-preset", str(preset),
        "-crf", str(crf),
        # Keep every CPU encode in the 8-bit 4:2:0 format supported by the
        # inbox Windows 10 Media Player.  Without this explicit output option,
        # an RGBA sticker/overlay can make libx264 select yuv444p and the
        # resulting MP4 becomes High 4:4:4 Predictive.
        "-pix_fmt", "yuv420p",
    ]
    if FFMPEG_THREADS:
        arguments.extend(["-threads", str(FFMPEG_THREADS)])
    return arguments


RENDER_WORKERS = _bounded_env_int(
    "V16_RENDER_WORKERS",
    1 if WINDOWS_CPU_STABILITY_PROFILE else 2,
    1,
    2 if WINDOWS_CPU_STABILITY_PROFILE or sys.platform == "darwin" else 4,
)


def _normalized_output_fps_mode(raw_mode, environ):
    mode = str(raw_mode or "auto").strip().lower() or "auto"
    # 兼容尚未重启的 8018 旧进程：它会固定传入 30，但同时没有新版阈值参数。
    if (
        mode == "30"
        and environ.get("V16_UPLOAD_ORDER_FILE")
        and "V16_AUTO_FPS_60_THRESHOLD" not in environ
    ):
        return "auto"
    return mode


def _local_speed_profile_enabled():
    """Only the authenticated loopback owner's gateway-issued process lane."""
    return (
        os.environ.get("V17_TENANT_BACKEND") == "1"
        and os.environ.get("V17_RENDER_ALLOWED_SLOTS") == "2"
        and bool(os.environ.get("V17_TENANT_USER_ID", "").strip())
    )


# The owner explicitly chose 30 fps on 2026-09-05. Public auto/60 stays intact.
OUTPUT_FPS_MODE = "30" if _local_speed_profile_enabled() else _normalized_output_fps_mode(
    os.environ.get("V16_OUTPUT_FPS", "auto"),
    os.environ,
)
AUTO_FPS_60_THRESHOLD = max(
    0.5,
    min(1.0, float(os.environ.get("V16_AUTO_FPS_60_THRESHOLD", "0.70"))),
)
if OUTPUT_FPS_MODE == "auto":
    # 自动模式会在读取本次素材后重新判定；先用兼容性最好的 30fps 初始化。
    FPS = 30
else:
    FPS = max(24, min(60, int(float(OUTPUT_FPS_MODE))))
OUTPUT_FPS_INFO = {
    "mode": OUTPUT_FPS_MODE,
    "high_fps_ratio": 0.0,
    "sample_count": 0,
}
_full_spec_raw = os.environ.get("V16_FULL_SPEC_MODE", "0")
FULL_SPEC_MODE = str(_full_spec_raw).strip().lower() not in {"0", "false", "no", "off"}
STRICT_MATERIAL_COUNTS = os.environ.get(
    "V16_STRICT_MATERIAL_COUNTS", "1" if FULL_SPEC_MODE else "0"
).strip().lower() not in {"0", "false", "no", "off"}
SPEC_CORE_SECONDS = 60.0
SPEC_QUALITY_SECONDS = 90.0
SPEC_DEDUP_SECONDS = 60.0
SPEC_TOTAL_SECONDS = SPEC_CORE_SECONDS + SPEC_QUALITY_SECONDS + SPEC_DEDUP_SECONDS
MIN_TARGET_SECONDS = 1.0
DURATION_MULTIPLIER_MIN = 1.0
DURATION_MULTIPLIER_MAX = 10.0
SHUFFLE_TO_CORE_MIN_RATIO = 4.0
CORE_TO_OUTPUT_MAX_RATIO = 0.25
DEFAULT_TARGET_SECONDS = 120.0
MAX_TOTAL_DURATION = float(
    os.environ.get("MAX_TOTAL_DURATION", str(DEFAULT_TARGET_SECONDS))
)
if FULL_SPEC_MODE:
    TARGET_TOTAL_DURATION = SPEC_TOTAL_SECONDS
else:
    try:
        TARGET_TOTAL_DURATION = float(
            os.environ.get("V16_TARGET_SECONDS", str(MAX_TOTAL_DURATION))
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError("参考成片时长必须是有限数值。") from exc
    if (
        not math.isfinite(TARGET_TOTAL_DURATION)
        or TARGET_TOTAL_DURATION < MIN_TARGET_SECONDS
    ):
        raise RuntimeError("参考成片时长必须是大于 0 的有限秒数。")
MAX_TOTAL_DURATION = TARGET_TOTAL_DURATION
FINISH_LAST_CLIP = os.environ.get("V16_FINISH_LAST_CLIP", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
MAX_NATURAL_TAIL_SECONDS = max(
    0.0,
    min(
        18.0,
        float(
            os.environ.get(
                "V16_MAX_NATURAL_TAIL_SECONDS",
                str(TARGET_TOTAL_DURATION * 0.15),
            )
        ),
    ),
)
ALLOW_NATURAL_BOUNDARY_FALLBACK = os.environ.get(
    "V16_ALLOW_NATURAL_BOUNDARY_FALLBACK", "0"
).strip().lower() in {"1", "true", "yes", "on"}
try:
    NATURAL_BOUNDARY_FALLBACK_EXTRA_SECONDS = float(
        os.environ.get(
            "V16_NATURAL_BOUNDARY_FALLBACK_EXTRA_SECONDS",
            str(min(MAX_NATURAL_TAIL_SECONDS, 18.0)),
        )
    )
except (TypeError, ValueError) as exc:
    raise RuntimeError("自然收尾容错时长必须是有限数值。") from exc
if (
    not math.isfinite(NATURAL_BOUNDARY_FALLBACK_EXTRA_SECONDS)
    or NATURAL_BOUNDARY_FALLBACK_EXTRA_SECONDS < 0.0
):
    raise RuntimeError("自然收尾容错时长必须是大于等于 0 的有限数值。")
# The emergency path is only intended for the final fission attempt. Keep its
# extra allowance bounded even if a stale page submits an unexpectedly large
# value; the ordinary natural-tail contract remains unchanged on every strict
# attempt.
NATURAL_BOUNDARY_FALLBACK_EXTRA_SECONDS = min(
    NATURAL_BOUNDARY_FALLBACK_EXTRA_SECONDS,
    18.0,
)
# A complete speech/action block may be retimed only on the explicit final
# natural-boundary attempt. Twelve percent matches the existing minimum-
# duration safeguard and remains small enough to preserve intelligibility and
# visible action cadence. Video and audio always use the same playback rate.
NATURAL_BOUNDARY_MAX_PLAYBACK_RATE = 1.12
NATURAL_BOUNDARY_MIN_PLAYBACK_RATE = 1.0 / NATURAL_BOUNDARY_MAX_PLAYBACK_RATE
# This allowance belongs only to source-window inventory planning.  A source
# whose real EOF is a few frames beyond its equal share is safer to keep whole
# than to reject an otherwise valid candidate.  The finished shuffle story is
# still capped by MAX_NATURAL_TAIL_SECONDS, so this does not widen the delivered
# movie's duration contract.
SHUFFLE_SOURCE_EOF_PLANNING_GRACE_SECONDS = 0.5
SYNC_MODE = os.environ.get("V16_SYNC_MODE", "video_to_script").strip().lower()
if SYNC_MODE not in {"video_to_script", "script_to_video"}:
    SYNC_MODE = "video_to_script"
SYNC_MODE_LABEL = {
    "video_to_script": "视频对接话术",
    "script_to_video": "话术对接视频",
}[SYNC_MODE]
DEDUP_MIN_CHUNK = float(os.environ.get("DEDUP_MIN_CHUNK", "2"))
DEDUP_MAX_CHUNK = float(os.environ.get("DEDUP_MAX_CHUNK", "4"))
AUTO_MICRO_MOTION = os.environ.get("V16_AUTO_MICRO_MOTION", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
AUTO_PERSON_EFFECTS = os.environ.get(
    "V16_AUTO_PERSON_EFFECTS", "1" if FULL_SPEC_MODE else "0"
).strip().lower() not in {"0", "false", "no", "off"}
FULL_COMPLIANCE_SCAN = os.environ.get(
    "V16_FULL_COMPLIANCE_SCAN", "1" if FULL_SPEC_MODE else "0"
).strip().lower() not in {"0", "false", "no", "off"}
COMPLIANCE_SAMPLE_SECONDS = max(
    0.5,
    min(3.0, float(os.environ.get("V16_COMPLIANCE_SAMPLE_SECONDS", "1.0"))),
)
# Final-output visual gate for the retired white-background ``ink`` renderer.
# The centre crop deliberately excludes the normal coloured corner stickers;
# the thresholds describe the old filter's almost-neutral, very bright base
# picture rather than ordinary low-saturation footage.
VISUAL_GRAY_SAT_AVG_MAX = 3.0
VISUAL_GRAY_SAT_HIGH_MAX = 6.0
VISUAL_GRAY_CHROMA_CENTER = 128.0
VISUAL_GRAY_CHROMA_TOLERANCE = 3.0
VISUAL_LINE_ART_Y_AVG_MIN = 185.0
VISUAL_LINE_ART_Y_HIGH_MIN = 220.0
VISUAL_QUALITY_CROP_WIDTH_RATIO = 0.84
VISUAL_QUALITY_CROP_HEIGHT_RATIO = 0.80
VISUAL_QUALITY_GATE_VERSION = "natural-colour-clean-style-invariant-v3"
VISUAL_WHITE_MIN_RUN_SECONDS = 6.0
VISUAL_WHITE_MIN_SECONDS = 8.0
VISUAL_WHITE_MIN_RATIO_DELTA = 0.06
VISUAL_GRAY_MIN_RUN_SECONDS = 8.0
VISUAL_GRAY_MIN_SECONDS = 10.0
VISUAL_GRAY_MIN_RATIO_DELTA = 0.08
MICRO_MOTION_WINDOW_SECONDS = max(
    0.75,
    min(3.0, float(os.environ.get("V16_MICRO_MOTION_WINDOW_SECONDS", "1.0"))),
)
MICRO_MOTION_MAX_FRAME_DELTA = max(
    0.5,
    min(8.0, float(os.environ.get("V16_MICRO_MOTION_MAX_FRAME_DELTA", "4.0"))),
)
MICRO_MOTION_ZOOM = max(
    0.02,
    min(0.18, float(os.environ.get("V16_MICRO_MOTION_ZOOM", "0.12"))),
)
TIMEPIN_ZOOM_ENABLED = os.environ.get(
    "V16_TIMEPIN_ZOOM_ENABLED", "0"
).strip().lower() not in {"0", "false", "no", "off"}
TIMEPIN_ZOOM_DEFAULT_KEYFRAMES = (
    (0.0, 100.0, 0.0),
    (50.0, 108.0, 0.0),
    (100.0, 100.0, 0.0),
)
TIMEPIN_ZOOM_VERTICAL_SAFE_MIN_SCALE = 104.0


def _parse_timepin_zoom_keyframes(raw_value):
    def invalid(message):
        if TIMEPIN_ZOOM_ENABLED:
            raise ValueError(f"时间针关键帧配置无效：{message}")
        return TIMEPIN_ZOOM_DEFAULT_KEYFRAMES

    try:
        payload = json.loads(str(raw_value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return invalid("JSON 无法读取")
    if not isinstance(payload, list) or not 2 <= len(payload) <= 8:
        return invalid("必须包含 2–8 根针")
    schemas = []
    for item in payload:
        if not isinstance(item, dict):
            return invalid("针字段不完整或包含未知字段")
        fields = set(item)
        if fields == {"position_percent", "scale_percent"}:
            schemas.append("legacy")
        elif fields == {
            "position_percent",
            "scale_percent",
            "vertical_percent",
        }:
            schemas.append("current")
        else:
            return invalid("针字段不完整或包含未知字段")
    if len(set(schemas)) != 1:
        return invalid("新旧针格式不能混用")
    legacy_curve = schemas[0] == "legacy"
    keyframes = []
    for item in payload:
        if isinstance(item.get("position_percent"), bool) or isinstance(
            item.get("scale_percent"), bool
        ) or isinstance(item.get("vertical_percent", 0), bool):
            return invalid("针位置、大小和上下位置不能是布尔值")
        try:
            position = float(item.get("position_percent"))
            scale = float(item.get("scale_percent"))
            vertical = float(item.get("vertical_percent", 0))
        except (TypeError, ValueError):
            return invalid("针位置、大小或上下位置不是数字")
        if legacy_curve and 88.0 <= scale < 100.0:
            scale = 200.0 - scale
        if (
            not math.isfinite(position)
            or not math.isfinite(scale)
            or not math.isfinite(vertical)
            or not 0.0 <= position <= 100.0
            or not 100.0 <= scale <= 130.0
            or not -100.0 <= vertical <= 100.0
            or not math.isclose(position, round(position), abs_tol=1e-9)
            or not math.isclose(scale, round(scale), abs_tol=1e-9)
            or not math.isclose(vertical, round(vertical), abs_tol=1e-9)
        ):
            return invalid("针位置、大小或上下位置超出整数范围")
        if not math.isclose(vertical, 0.0, abs_tol=1e-9) and math.isclose(
            scale, 100.0, abs_tol=1e-9
        ):
            scale = TIMEPIN_ZOOM_VERTICAL_SAFE_MIN_SCALE
        keyframes.append(
            (round(position, 1), round(scale, 1), round(vertical, 1))
        )
    if (
        not math.isclose(keyframes[0][0], 0.0, abs_tol=0.01)
        or not math.isclose(keyframes[-1][0], 100.0, abs_tol=0.01)
        or any(
            current[0] <= previous[0]
            for previous, current in zip(keyframes, keyframes[1:])
        )
    ):
        return invalid("必须从 0% 开始、100% 结束且严格递增")
    return tuple(keyframes)


TIMEPIN_ZOOM_KEYFRAMES = _parse_timepin_zoom_keyframes(
    os.environ.get("V16_TIMEPIN_ZOOM_KEYFRAMES_JSON", "")
)
TIMEPIN_ZOOM_ACTIVE = TIMEPIN_ZOOM_ENABLED and any(
    scale > 100.0 for _position, scale, _vertical in TIMEPIN_ZOOM_KEYFRAMES
)
SHORT_EFFECT_WINDOW_SECONDS = max(
    0.5,
    min(3.0, float(os.environ.get("V16_SHORT_EFFECT_WINDOW_SECONDS", "1.0"))),
)
EARLY_EFFECT_OPACITY = max(
    0.10,
    min(1.0, float(os.environ.get("V16_EARLY_EFFECT_OPACITY", "0.30"))),
)
EARLY_EFFECT_SECONDS = max(
    0.0,
    min(60.0, float(os.environ.get("V16_EARLY_EFFECT_SECONDS", "30"))),
)
VISION_MODEL = os.environ.get("V16_VISION_MODEL", "gemini-3.6-flash")
OPENAI_VISION_MODEL = os.environ.get("V16_OPENAI_MODEL", "gpt-5.6-terra")
LOCKED_QWEN_VISION_MODEL = "qwen3.8-max"
QWEN_VISION_MODEL = LOCKED_QWEN_VISION_MODEL
QWEN_API_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
LOCKED_KIMI_VISION_MODEL = "kimi-k3"
KIMI_VISION_MODEL = LOCKED_KIMI_VISION_MODEL
KIMI_API_BASE = "https://api.moonshot.cn/v1"
KIMI_MAX_COMPLETION_TOKENS = max(
    4096,
    min(16384, int(os.environ.get("V16_KIMI_MAX_COMPLETION_TOKENS", "8192"))),
)
VISION_PRICES_CNY_PER_MILLION = {
    "qwen": {"input": 12.0, "cached_input": 1.5, "output": 36.0},
    "kimi": {"input": 20.0, "cached_input": 2.0, "output": 100.0},
}
OPENCLAW_VISION_MODEL = os.environ.get(
    "V16_OPENCLAW_VISION_MODEL",
    "google-vertex/gemini-2.5-flash",
)
VISION_MAX_FRAMES = max(4, min(24, int(os.environ.get("V16_VISION_MAX_FRAMES", "24"))))
VISION_TIMEOUT = max(10, int(os.environ.get("V16_VISION_TIMEOUT", "150")))
TTS_ENABLED = os.environ.get("V16_TTS_ENABLED", "1") != "0"
# Production voice lock: neither the web process nor environment variables may
# replace this cloned speaker. Tests can still patch these module globals.
LOCKED_TTS_PROVIDER = "minimax-bailian"
LOCKED_TTS_MODEL = "MiniMax/speech-2.8-turbo"
LOCKED_TTS_VOICE = "ddsales0817d"
TTS_PROVIDER = LOCKED_TTS_PROVIDER
TTS_STRICT_VOICE = True
TTS_VOICE = os.environ.get("V16_TTS_VOICE", "zh-CN-XiaoxiaoNeural")
TTS_RATE = os.environ.get("V16_TTS_RATE", "+20%")
TTS_TIMEOUT = max(20, int(os.environ.get("V16_TTS_TIMEOUT", "60")))
OPENAI_TTS_MODEL = os.environ.get("V16_OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
OPENAI_TTS_VOICE = os.environ.get("V16_OPENAI_TTS_VOICE", "coral")
OPENAI_TTS_INSTRUCTIONS = os.environ.get(
    "V16_OPENAI_TTS_INSTRUCTIONS",
    "使用自然、可信、专业的普通话电商口播。语速稳定清楚，每秒约五到六个汉字，句间只做短暂停顿，不夸张叫卖。",
)
MINIMAX_TTS_ENDPOINT = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/"
    "multimodal-generation/generation"
)
MINIMAX_TTS_MODEL = LOCKED_TTS_MODEL
MINIMAX_TTS_VOICE = LOCKED_TTS_VOICE
MINIMAX_TTS_SPEED = max(
    0.5,
    min(2.0, float(os.environ.get("V16_MINIMAX_TTS_SPEED", "1.05"))),
)
MINIMAX_TTS_VOLUME = max(
    0.1,
    min(10.0, float(os.environ.get("V16_MINIMAX_TTS_VOLUME", "1.0"))),
)
MINIMAX_TTS_PITCH = max(
    -12,
    min(12, int(os.environ.get("V16_MINIMAX_TTS_PITCH", "0"))),
)
# Bailian transports the MP3 as a hex string, doubling the response body.
# At 256 kbps a normal 80-90 second narration exceeds the upstream 4 MiB
# gRPC message limit. 128 kbps mono stays below 3 MiB while preserving clear
# speech and, unlike request chunking, keeps one exact word-timestamp timeline.
MINIMAX_TTS_BITRATE = 128000
MINIMAX_TTS_TIMEOUT = max(
    20,
    min(600, int(os.environ.get("V16_MINIMAX_TTS_TIMEOUT", "240"))),
)
MINIMAX_TTS_RETRIES = max(
    0,
    min(3, int(os.environ.get("V16_MINIMAX_TTS_RETRIES", "2"))),
)
MINIMAX_TTS_PRICE_PER_10K_CNY = max(
    0.0,
    float(os.environ.get("V16_MINIMAX_TTS_PRICE_PER_10K_CNY", "2.0")),
)
MINIMAX_TTS_MAX_ESTIMATED_CNY = max(
    0.01,
    float(os.environ.get("V16_MINIMAX_TTS_MAX_ESTIMATED_CNY", "0.50")),
)
RANLIANG_LOCAL_ROOT = os.environ.get(
    "V16_RANLIANG_ROOT",
    os.path.expanduser("~/.openclaw/workspace/yunying"),
)
COSYVOICE_PYTHON = os.environ.get(
    "V16_COSYVOICE_PYTHON",
    os.path.join(RANLIANG_LOCAL_ROOT, "data", "cosyvoice-runtime", "bin", "python"),
)
COSYVOICE_SYNTHESIZER = os.environ.get(
    "V16_COSYVOICE_SYNTHESIZER",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "cosyvoice_synthesize_fast.py"),
)
COSYVOICE_PROMPT = os.environ.get(
    "V16_COSYVOICE_PROMPT",
    os.path.join(RANLIANG_LOCAL_ROOT, "data", "voice-prompts", "desktop-female.wav"),
)
COSYVOICE_SPEED = max(
    0.75,
    min(1.35, float(os.environ.get("V16_COSYVOICE_SPEED", "1.0"))),
)
COSYVOICE_TIMEOUT = max(120, int(os.environ.get("V16_COSYVOICE_TIMEOUT", "1800")))
NARRATION_MAX_ATTEMPTS = max(
    1,
    min(3, int(os.environ.get("V16_NARRATION_MAX_ATTEMPTS", "2"))),
)
COSYVOICE_CHUNK_CHARS = max(
    36,
    min(120, int(os.environ.get("V16_COSYVOICE_CHUNK_CHARS", "90"))),
)
COSYVOICE_THREADS = _bounded_env_int(
    "V16_COSYVOICE_THREADS",
    4 if WINDOWS_CPU_STABILITY_PROFILE else 8,
    2,
    12,
)
SUBTITLE_ENABLED = os.environ.get("V16_AUTO_SUBTITLES", "1") != "0"
SUBTITLE_ASR = os.environ.get("V16_SUBTITLE_ASR", "bijian").strip().lower() or "bijian"
SUBTITLE_TIMEOUT = max(60, int(os.environ.get("V16_SUBTITLE_TIMEOUT", "900")))
SUBTITLE_OCR_SAMPLES = max(4, min(12, int(os.environ.get("V16_SUBTITLE_OCR_SAMPLES", "8"))))
DEDUP2_TARGET_SECONDS = max(
    15.0,
    min(90.0, float(os.environ.get("V16_DEDUP2_TARGET_SECONDS", "60"))),
)
DEDUP_SECTION_MIN_SECONDS = 40.0
DEDUP_SECTION_MAX_SECONDS = 90.0
FACE_EFFECT = os.environ.get("FACE_EFFECT", "none").lower()
FACE_EFFECT_RENDER_ORDER = (
    "blur",
    "mosaic",
    "blush",
    "neon",
    "old",
    "sparkle",
    "sticker_sunglasses",
    "phone_cover",
    "sunglasses",
)
_requested_face_effects = [
    value.strip().lower()
    for value in os.environ.get("FACE_EFFECTS", FACE_EFFECT).split(",")
    if value.strip() and value.strip().lower() != "none"
]
FACE_EFFECTS = [
    effect for effect in FACE_EFFECT_RENDER_ORDER
    if effect in _requested_face_effects
]
if AUTO_PERSON_EFFECTS:
    required_person_effects = {"old", "sticker_sunglasses"}
    FACE_EFFECTS = [
        effect
        for effect in FACE_EFFECT_RENDER_ORDER
        if effect in set(FACE_EFFECTS) | required_person_effects
    ]
ENABLE_PRICE_FILTER = os.environ.get("ENABLE_PRICE_FILTER", "1") != "0"
PRICE_SCAN_INTERVAL_SECONDS = max(
    0.25,
    min(1.0, float(os.environ.get("V16_PRICE_SCAN_INTERVAL_SECONDS", "0.50"))),
)
PRICE_SCAN_MAX_SAMPLES = max(
    6,
    min(40, int(os.environ.get("V16_PRICE_SCAN_MAX_SAMPLES", "24"))),
)
PRICE_SCAN_WORKERS = _bounded_env_int(
    "V16_PRICE_SCAN_WORKERS",
    2 if WINDOWS_CPU_STABILITY_PROFILE else 4,
    1,
    4,
)
PRICE_CACHE_DIR = os.environ.get(
    "V16_PRICE_CACHE_DIR",
    os.path.join(PERSISTENT_CACHE_ROOT, "price"),
)
TEXT_CACHE_DIR = os.environ.get(
    "V16_TEXT_CACHE_DIR",
    os.path.join(PERSISTENT_CACHE_ROOT, "text"),
)
SUBTITLE_CACHE_DIR = os.environ.get(
    "V16_SUBTITLE_CACHE_DIR",
    os.path.join(PERSISTENT_CACHE_ROOT, "subtitles"),
)
try:
    SUBTITLE_ASR_BREAKER_TTL_SECONDS = max(
        60,
        min(3600, int(os.environ.get("V16_SUBTITLE_ASR_BREAKER_TTL_SECONDS", "600"))),
    )
except (TypeError, ValueError):
    SUBTITLE_ASR_BREAKER_TTL_SECONDS = 600
_SUBTITLE_ASR_DEGRADED_REASON = ""
_SUBTITLE_ASR_WARNING_PRINTED = False
UPLOAD_ORDER_FILE = os.environ.get(
    "V16_UPLOAD_ORDER_FILE", os.path.join(BASE_DIR, ".v16_upload_order.json")
)
STICKER_CORNER_CONFIG_RAW = os.environ.get("V16_STICKER_CORNER_CONFIG", "")
STICKER_SIZE_PERCENT_RAW = os.environ.get("V16_STICKER_SIZE_PERCENT", "100")
FULLSCREEN_STICKER_CONFIG_RAW = os.environ.get(
    "V16_FULLSCREEN_STICKER_CONFIG", ""
)
STICKER_CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")
DYNAMIC_STICKER_SUFFIXES = (".gif", ".mp4")
DYNAMIC_STICKER_KEY_MODES = {"auto", "green", "off"}

# A provider that already failed during this runner process should not make
# every later shot batch wait for the same timeout again. This resets for each
# new video job, so a temporary outage never disables the provider permanently.
_VISION_PROVIDER_FAILURES = {}
_VISION_USAGE_EVENTS = []


def _vision_provider_enabled(provider_name):
    return provider_name not in _VISION_PROVIDER_FAILURES


def _remember_vision_provider_failure(provider_name, error):
    _VISION_PROVIDER_FAILURES.setdefault(provider_name, str(error))


def _vision_provider_failure_detail(provider_name):
    detail = _VISION_PROVIDER_FAILURES.get(provider_name, "")
    return f"{provider_name}本次任务已跳过重复等待：{detail}" if detail else ""


def _external_api_keys_required():
    """True for the web workflow where department-owned keys are mandatory."""
    return os.environ.get("V16_EXTERNAL_API_KEYS_REQUIRED", "0") == "1"


def _selected_vision_provider():
    return os.environ.get("V16_VISION_PROVIDER", "").strip().lower()


def _qwen_vision_is_selected():
    return _selected_vision_provider() == "qwen"


def _kimi_vision_is_selected():
    return _selected_vision_provider() == "kimi"


def _external_vision_is_selected():
    return _selected_vision_provider() in {"qwen", "kimi"}


def _usage_int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _record_vision_usage(provider, model, usage):
    usage = usage if isinstance(usage, dict) else {}
    prompt_tokens = _usage_int(
        usage.get("prompt_tokens", usage.get("input_tokens"))
    )
    completion_tokens = _usage_int(
        usage.get("completion_tokens", usage.get("output_tokens"))
    )
    prompt_details = usage.get("prompt_tokens_details") or usage.get(
        "input_tokens_details"
    )
    if not isinstance(prompt_details, dict):
        prompt_details = {}
    cached_tokens = min(
        prompt_tokens,
        _usage_int(
            prompt_details.get("cached_tokens", usage.get("cached_input_tokens"))
        ),
    )
    uncached_tokens = max(0, prompt_tokens - cached_tokens)
    rates = VISION_PRICES_CNY_PER_MILLION.get(provider, {})
    estimated_cost = (
        uncached_tokens * float(rates.get("input", 0.0))
        + cached_tokens * float(rates.get("cached_input", rates.get("input", 0.0)))
        + completion_tokens * float(rates.get("output", 0.0))
    ) / 1_000_000.0
    event = {
        "provider": provider,
        "model": model,
        "input_tokens": prompt_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": completion_tokens,
        "total_tokens": _usage_int(
            usage.get("total_tokens", prompt_tokens + completion_tokens)
        ),
        "estimated_cost_cny": round(estimated_cost, 6),
    }
    _VISION_USAGE_EVENTS.append(event)
    print(
        f"   -> {model} 本次调用：输入 {prompt_tokens} tokens，"
        f"输出 {completion_tokens} tokens，预计 {estimated_cost:.4f} 元。"
    )
    return event


def _vision_usage_summary():
    events = list(_VISION_USAGE_EVENTS)
    provider = events[-1]["provider"] if events else _selected_vision_provider()
    model = events[-1]["model"] if events else (
        QWEN_VISION_MODEL if provider == "qwen" else KIMI_VISION_MODEL if provider == "kimi" else None
    )
    return {
        "provider": provider or None,
        "model": model,
        "request_count": len(events),
        "input_tokens": sum(item["input_tokens"] for item in events),
        "cached_input_tokens": sum(item["cached_input_tokens"] for item in events),
        "output_tokens": sum(item["output_tokens"] for item in events),
        "total_tokens": sum(item["total_tokens"] for item in events),
        "estimated_cost_cny": round(
            sum(float(item["estimated_cost_cny"]) for item in events), 4
        ),
    }


def _attach_cost_metadata(metadata, tts_info=None):
    metadata = metadata if isinstance(metadata, dict) else {}
    vision_usage = _vision_usage_summary()
    metadata["vision_usage"] = vision_usage
    metadata["vision_estimated_cost_cny"] = vision_usage["estimated_cost_cny"]
    if isinstance(tts_info, dict):
        tts_cost = float(tts_info.get("estimated_cost_cny") or 0.0)
    else:
        tts_cost = float(metadata.get("tts_estimated_cost_cny") or 0.0)
    metadata["total_estimated_cost_cny"] = round(
        float(vision_usage["estimated_cost_cny"]) + tts_cost,
        4,
    )
    return metadata


@dataclass
class Clip:
    path: str
    source: str
    duration: float
    score: float = 0.0
    source_name: str = ""
    start: float = 0.0
    sequence: int = 0
    source_path: str = ""


class NaturalBoundaryError(RuntimeError):
    """A candidate cannot finish near its reference without cutting a safe block."""


class VisualQualityError(RuntimeError):
    """The visual-quality evidence could not be completed safely."""


class VisualQualityBlockedError(VisualQualityError):
    """The completed evidence proves a destructive visual treatment was added."""


def _validated_tmp_directory():
    container = _validated_tmp_container(create=False)
    candidate_path = os.path.abspath(TMP_DIR)
    candidate = os.path.realpath(candidate_path)
    # Compare against the originally reserved path without re-resolving it.
    # If an attacker swaps the entry for a junction to a sibling directory,
    # ``candidate`` changes while ``reserved`` remains the original root.
    reserved = os.path.abspath(_RESERVED_TMP_DIR)
    if (
        candidate != reserved
        or _is_path_link(candidate_path)
        or os.path.dirname(candidate) != container
        or candidate == container
    ):
        raise RuntimeError("制作临时目录越出本任务独占目录，已拒绝清理。")
    return candidate


def _validated_tmp_subdirectory(path):
    """Return one owned child directory, rejecting traversal and reparse points."""
    root = _validated_tmp_directory()
    candidate_path = os.path.abspath(path)
    candidate = os.path.realpath(candidate_path)
    try:
        inside_root = os.path.commonpath([root, candidate]) == root
    except ValueError:
        inside_root = False
    if (
        not inside_root
        or candidate == root
        or _is_path_link(candidate_path)
        or not os.path.isdir(candidate_path)
    ):
        raise RuntimeError("临时子目录越出本任务独占目录，已拒绝清理。")
    return candidate


def _remove_tmp_tree(best_effort=False):
    try:
        candidate = _validated_tmp_directory()
    except (OSError, RuntimeError):
        if best_effort:
            return False
        raise
    for attempt in range(6):
        try:
            if os.path.lexists(candidate):
                shutil.rmtree(candidate)
            return True
        except OSError:
            if attempt == 5:
                if best_effort:
                    return False
                raise
            time.sleep(0.2 * (attempt + 1))
    return False


def clean_tmp():
    """Confirm the atomically reserved task directory is still private."""
    candidate = _validated_tmp_directory()
    if not os.path.isdir(candidate):
        raise RuntimeError("本任务独占临时目录已丢失，已拒绝继续制作。")
    return candidate


def cleanup_tmp_after_run():
    """Best-effort removal after success, retry, failure, or normal abort."""
    return _remove_tmp_tree(best_effort=True)


def ensure_dirs():
    if THREE_POOL_MODE and not FULL_SPEC_MODE:
        folders = [CORE_DIR, SHUFFLE_DIR, STICKER_DIR]
    else:
        folders = [CORE_DIR, QUAL_DIR, DEDUP_DIR, MASK_DIR, STICKER_DIR]
        if DEDUP2_ENABLED:
            folders.append(DEDUP2_DIR)
    for folder in folders:
        os.makedirs(folder, exist_ok=True)
    if THREE_POOL_MODE and not FULL_SPEC_MODE:
        default_texts = {
            SHUFFLE_REQUIREMENT_FILE: (
                "打乱素材保留原声，只在完整话术或动作结束处切分；"
                "识别重复阶段，跨来源按展示、安装、使用、结果连贯重组；"
                "页面裂变时逐条独立打乱、均匀穿插，不添加文字柔化遮挡条。"
            )
        }
    else:
        default_texts = {
            QUAL_REQUIREMENT_FILE: (
                "单条模式按上传顺序完整保留；页面裂变时每条独立重排；"
                "每个优质素材仍完整保留，不分段剪切。"
            ),
            DEDUP_REQUIREMENT_FILE: (
                "去重素材池保留原声，只在完整话术或动作结束处切分；识别重复阶段，"
                "跨来源按展示、安装、使用、结果连贯重组，禁止整条顺序拼接和逐句机械轮换；"
                "根据真实可用素材在40–90秒内独立分配，不循环凑时长；"
                "去重素材池不添加文字柔化遮挡条。"
            ),
        }
    if DEDUP2_ENABLED:
        default_texts[DEDUP2_REQUIREMENT_FILE] = (
            "根据视频画面内容生成商品软广文案，不要生成价格相关文案内容；"
            "根据真实可用素材在40–90秒内独立分配；"
            "按去重2实际成片时间窗逐句匹配镜头和配音；检测到原画文字时使用紧凑横向柔化条，"
            "不使用马赛克，并在柔化特效之后叠加逐句同步字幕。"
        )
    legacy_texts = {
        QUAL_REQUIREMENT_FILE: {
            "优质素材按上传顺序完整保留，不分段剪切。",
        },
        DEDUP_REQUIREMENT_FILE: {
            "去重素材按镜头/文案逻辑切成2-4秒，画面顺序尽量通顺，删掉重复和拖沓片段。",
            (
                "去重素材池1保留原声，只在完整话术或动作结束处切分；"
                "按展示、安装、使用、结果的叙事进度连贯重组，禁止逐句轮换来源。"
            ),
            (
                "去重素材池1保留原声，只在完整话术或动作结束处切分；识别重复阶段，"
                "跨来源按展示、安装、使用、结果连贯重组，禁止整条顺序拼接和逐句机械轮换。"
            ),
            (
                "去重素材池1保留原声，只在完整话术或动作结束处切分；识别重复阶段，"
                "跨来源按展示、安装、使用、结果连贯重组，禁止整条顺序拼接和逐句机械轮换；"
                "与去重素材池2按真实可用画面对齐时长，不循环凑时长。"
            ),
            (
                "去重素材池1保留原声，只在完整话术或动作结束处切分；识别重复阶段，"
                "跨来源按展示、安装、使用、结果连贯重组，禁止整条顺序拼接和逐句机械轮换；"
                "根据真实可用素材在40–90秒内独立分配，不强制与去重素材池2等长，不循环凑时长。"
            ),
        },
    }
    if THREE_POOL_MODE and not FULL_SPEC_MODE:
        legacy_texts = {SHUFFLE_REQUIREMENT_FILE: set()}
    if DEDUP2_ENABLED:
        legacy_texts[DEDUP2_REQUIREMENT_FILE] = {
            "根据视频画面内容生成商品软广文案，不要生成价格相关文案内容。",
            (
                "根据视频画面内容生成商品软广文案，不要生成价格相关文案内容；"
                "检测到画面文字时使用横向柔化遮挡条，不使用马赛克。"
            ),
            (
                "根据视频画面内容生成商品软广文案，不要生成价格相关文案内容；"
                "根据真实可用素材在40–90秒内独立分配；"
                "检测到画面文字时使用横向柔化遮挡条，不使用马赛克。"
            ),
        }
    for path, text in default_texts.items():
        current = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                current = f.read().strip()
        if not current or current in legacy_texts.get(path, set()):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text + "\n")


def _selected_material_names(pool_name):
    """Read the exact material selection passed by the V17 web process."""
    raw = os.environ.get("V16_SELECTED_MATERIALS_JSON", "").strip()
    if not raw or not pool_name:
        return None
    try:
        manifest = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(manifest, dict) or pool_name not in manifest:
        return None
    names = manifest.get(pool_name)
    if not isinstance(names, list):
        return None
    clean_names = []
    seen = set()
    for name in names:
        if (
            isinstance(name, str)
            and name
            and os.path.basename(name) == name
            and name not in seen
        ):
            seen.add(name)
            clean_names.append(name)
    return clean_names


def get_files(folder, exts, pool_name=None):
    if not os.path.exists(folder):
        return []
    files = sorted(
        os.path.join(folder, f)
        for f in os.listdir(folder)
        if f.lower().endswith(exts) and not f.startswith(".")
    )
    selected_names = _selected_material_names(pool_name)
    if (
        THREE_POOL_MODE
        and not FULL_SPEC_MODE
        and pool_name in {"核心素材", "打乱素材池", "贴纸素材池"}
        and selected_names is None
    ):
        raise RuntimeError(
            f"页面未下发{pool_name}的精确选择清单，已停止以防止扫描未勾选素材。"
        )
    if selected_names is not None:
        by_name = {os.path.basename(path): path for path in files}
        return [by_name[name] for name in selected_names if name in by_name]
    if not pool_name or not os.path.exists(UPLOAD_ORDER_FILE):
        return files
    try:
        with open(UPLOAD_ORDER_FILE, "r", encoding="utf-8") as order_file:
            manifest = json.load(order_file)
        if not isinstance(manifest, dict):
            return files
        wanted = manifest.get(pool_name, [])
        if not isinstance(wanted, list):
            return files
        by_name = {os.path.basename(path): path for path in files}
        ordered = [
            by_name.pop(name)
            for name in wanted
            if isinstance(name, str) and name in by_name
        ]
        ordered.extend(sorted(by_name.values()))
        return ordered
    except (OSError, ValueError, TypeError):
        return files


def _validated_pool_video_durations(paths, pool_dir, pool_label):
    """Validate the exact candidate list and return authoritative durations."""
    root = os.path.realpath(pool_dir)
    durations = []
    for path in paths:
        candidate = os.path.abspath(path)
        resolved = os.path.realpath(candidate)
        try:
            inside_pool = os.path.commonpath([root, resolved]) == root
        except ValueError:
            inside_pool = False
        if (
            not inside_pool
            or os.path.islink(candidate)
            or not os.path.isfile(candidate)
            or os.path.splitext(candidate)[1].lower() not in {".mp4", ".mov"}
        ):
            raise RuntimeError(f"{pool_label}中存在不安全或已失效的视频文件。")
        duration = get_duration(candidate)
        if not math.isfinite(duration) or duration <= 0.10:
            raise RuntimeError(
                f"{pool_label}视频 {os.path.basename(candidate)} 无法读取有效时长。"
            )
        durations.append(float(duration))
    return durations


def _fission_seed_parts():
    batch_id = FISSION_VARIANT_SEED or "single"
    variant_index = 1
    attempt = 0
    if FISSION_VARIANT_SEED:
        try:
            batch_id, variant_text, attempt_text = FISSION_VARIANT_SEED.rsplit(":", 2)
            variant_index = max(1, int(variant_text))
            attempt = max(0, int(attempt_text))
        except (TypeError, ValueError):
            batch_id = FISSION_VARIANT_SEED
    return batch_id, variant_index, attempt


def select_shuffle_mask_source(shuffle_paths):
    """Choose one already-selected shuffle video; never scan the legacy mask pool."""
    if not THREE_POOL_MODE or not AUTO_MASK_FROM_SHUFFLE:
        return None
    candidates = list(shuffle_paths)
    if not candidates:
        raise RuntimeError("打乱素材为空，无法生成自动蒙版。")
    by_name = {os.path.basename(path): path for path in candidates}
    if MASK_SOURCE_NAME:
        if os.path.basename(MASK_SOURCE_NAME) != MASK_SOURCE_NAME:
            raise RuntimeError("页面下发的自动蒙版文件名不安全。")
        chosen = by_name.get(MASK_SOURCE_NAME)
        if not chosen:
            raise RuntimeError("页面下发的自动蒙版不属于本候选已选打乱素材。")
    else:
        ordered = sorted(candidates, key=lambda path: os.path.basename(path))
        batch_id, variant_index, attempt = _fission_seed_parts()
        base = int.from_bytes(
            hashlib.sha256(f"{batch_id}:shuffle-mask".encode("utf-8")).digest()[:8],
            "big",
        )
        chosen = ordered[(base + variant_index - 1 + attempt) % len(ordered)]
    # The whole candidate list has already passed directory, symlink and duration
    # validation. The mask adds one last stream-level check before FFmpeg uses it.
    if not has_video_stream(chosen):
        raise RuntimeError(
            f"自动蒙版来源 {os.path.basename(chosen)} 没有可解码的视频流。"
        )
    visual_note = (
        f"保留 1% 隐藏叠加，并以 {CORNER_MASK_REVEAL_RADIUS}px 圆角只从四角露底"
        if CORNER_MASK_REVEAL_ACTIVE
        else "1% 全屏循环隐藏叠加"
    )
    print(
        f"   -> 自动蒙版：{os.path.basename(chosen)} 来自本候选已验证打乱清单；"
        f"{visual_note}，蒙版音频不进入成片。"
    )
    return chosen


def validate_full_spec_materials(core_vids, qual_vids, dedup_vids, dedup2_vids, mask_vids):
    """Return every blocking mismatch with the IT delivery specification."""
    errors = []
    if len(core_vids) != 1:
        errors.append(f"核心素材必须恰好 1 条（当前 {len(core_vids)} 条）")
    if not 5 <= len(qual_vids) <= 8:
        errors.append(f"优质素材池必须为 5–8 条（当前 {len(qual_vids)} 条）")
    if not 5 <= len(dedup_vids) <= 8:
        errors.append(f"去重素材池必须为 5–8 条（当前 {len(dedup_vids)} 条）")
    if DEDUP2_ENABLED and len(dedup2_vids) != 5:
        errors.append(f"去重素材池2必须恰好 5 条（当前 {len(dedup2_vids)} 条）")
    if len(mask_vids) != 1:
        errors.append(f"蒙版素材必须恰好 1 条（当前 {len(mask_vids)} 条）")

    core_duration = sum(get_duration(path) for path in core_vids)
    quality_duration = sum(get_duration(path) for path in qual_vids)
    dedup_duration = sum(get_duration(path) for path in dedup_vids)
    if core_vids and core_duration + 0.05 < SPEC_CORE_SECONDS:
        errors.append(f"核心素材有效时长至少需要 60 秒（当前 {core_duration:.1f} 秒）")
    if qual_vids and quality_duration + 0.05 < SPEC_QUALITY_SECONDS:
        errors.append(f"优质素材总有效时长至少需要 90 秒（当前 {quality_duration:.1f} 秒）")
    if dedup_vids and dedup_duration + 0.05 < SPEC_DEDUP_SECONDS:
        errors.append(f"去重素材总有效时长至少需要 60 秒（当前 {dedup_duration:.1f} 秒）")
    return errors


def _stable_seed(paths, label=""):
    digest = hashlib.sha256(f"{FISSION_VARIANT_SEED}:{label}".encode("utf-8"))
    for path in paths:
        digest.update(os.path.basename(str(path)).encode("utf-8", errors="ignore"))
        # Derived TMP clips are recreated for every candidate, so their mtime
        # is not a stable identity. In fission mode the explicit variant seed
        # plus deterministic clip names must be enough to replay a plan.
        if not FISSION_VARIANT_SEED:
            try:
                stat = os.stat(path)
                digest.update(f":{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
            except OSError:
                pass
    return int.from_bytes(digest.digest()[:8], "big")


def _path_permutation_by_rank(paths, rank):
    """Return a deterministic factoradic permutation of ``paths``."""
    remaining = list(paths)
    if len(remaining) < 2:
        return remaining
    rank = int(rank) % math.factorial(len(remaining))
    ordered = []
    while remaining:
        block = math.factorial(len(remaining) - 1)
        index, rank = divmod(rank, block)
        ordered.append(remaining.pop(index))
    return ordered


def shuffle_fission_paths(paths, label="segments"):
    """Choose a replayable non-upload-order permutation with varied openers.

    Factoradic ranks are grouped by their first item. Walking those groups in
    round-robin order makes the first ``len(paths)`` variants start from
    different source blocks, instead of adjacent ranks repeatedly sharing the
    same opener. Every non-identity permutation is still visited exactly once
    before the sequence repeats.
    """
    original = list(paths)
    if not FISSION_SHUFFLE_ALL or not FISSION_VARIANT_SEED or len(original) < 2:
        return original
    batch_id = FISSION_VARIANT_SEED
    variant_index = 1
    attempt = 0
    try:
        batch_id, variant_text, attempt_text = FISSION_VARIANT_SEED.rsplit(":", 2)
        variant_index = max(1, int(variant_text))
        attempt = max(0, int(attempt_text))
    except (TypeError, ValueError):
        pass
    permutation_count = math.factorial(len(original))
    base = int.from_bytes(
        hashlib.sha256(f"{batch_id}:{label}".encode("utf-8")).digest()[:8],
        "big",
    )
    # Rank zero is the upload order. Skip it deliberately so "shuffle all"
    # never happens to emit the unchanged order. A factoradic rank block of
    # (n-1)! permutations shares one first item, so enumerate those blocks in
    # round-robin order rather than using adjacent ranks.
    ordinal = attempt * 5 + variant_index - 1
    opener_block = math.factorial(len(original) - 1)
    opener_groups = []
    for opener_index in range(len(original)):
        first_rank = opener_index * opener_block
        ranks = list(range(first_rank, first_rank + opener_block))
        if opener_index == 0:
            ranks = [rank for rank in ranks if rank != 0]
        if ranks:
            inner_offset = (base // max(1, len(original))) % len(ranks)
            ranks = ranks[inner_offset:] + ranks[:inner_offset]
        opener_groups.append(ranks)

    opener_offset = base % len(original)
    opener_order = [
        (opener_offset + index) % len(original)
        for index in range(len(original))
    ]
    ordered_ranks = []
    depth = 0
    while len(ordered_ranks) < permutation_count - 1:
        added = False
        for opener_index in opener_order:
            group = opener_groups[opener_index]
            if depth < len(group):
                ordered_ranks.append(group[depth])
                added = True
        if not added:
            break
        depth += 1
    rank = ordered_ranks[ordinal % len(ordered_ranks)]
    return _path_permutation_by_rank(original, rank)


def staggered_shuffle_clips(clips, label="v17-full-spec"):
    """Deterministically reshuffle shots while avoiding adjacent shots from one source."""
    if len(clips) < 2:
        return list(clips)
    rng = random.Random(_stable_seed([clip.path for clip in clips], label))
    queues = {}
    for clip in clips:
        queues.setdefault(clip.source, []).append(clip)
    for queue in queues.values():
        rng.shuffle(queue)
        queue.sort(key=lambda clip: clip.score, reverse=True)

    ordered = []
    previous_source = None
    while any(queues.values()):
        candidates = [source for source, queue in queues.items() if queue]
        different = [source for source in candidates if source != previous_source]
        if different:
            candidates = different
        candidates.sort(key=lambda source: (-len(queues[source]), source))
        largest = len(queues[candidates[0]])
        tied = [source for source in candidates if len(queues[source]) == largest]
        source = rng.choice(tied)
        ordered.append(queues[source].pop(0))
        previous_source = source
    return ordered


def weave_coherent_dedup1_blocks(clips, minimum_duration=0.0):
    """Remix duplicate source stories by narrative stage, not by whole video."""
    clips = list(clips)
    if len(clips) < 2:
        return clips
    source_order = []
    grouped = {}
    for clip in clips:
        if clip.source not in grouped:
            source_order.append(clip.source)
            grouped[clip.source] = []
        grouped[clip.source].append(clip)

    source_rank = {source: index for index, source in enumerate(source_order)}
    source_totals = {}
    stage_clips = {stage: {} for stage in range(4)}
    clip_stages = {}

    def story_stage(progress):
        if progress <= 0.32:
            return 0
        if progress <= 0.58:
            return 1
        if progress <= 0.82:
            return 2
        return 3

    for source in source_order:
        source_clips = sorted(
            grouped[source], key=lambda clip: (clip.sequence, clip.start, clip.path)
        )
        source_end = max(
            0.001,
            max(float(clip.start or 0.0) + float(clip.duration or 0.0) for clip in source_clips),
        )
        source_totals[source] = source_end
        for clip in source_clips:
            start = max(0.0, float(clip.start or 0.0))
            midpoint = start + max(0.0, float(clip.duration or 0.0)) / 2.0
            stage = story_stage(midpoint / source_end)
            clip_stages[id(clip)] = stage
            stage_clips[stage].setdefault(source, []).append(clip)

    # The longest source carries the middle explanation/use stages. A shorter
    # source supplies opening and closing when available. This produces a
    # small number of meaningful source changes instead of either whole-video
    # concatenation or sentence-by-sentence A/B/A/B jumping.
    primary = max(
        source_order,
        key=lambda source: (source_totals[source], -source_rank[source]),
    )
    secondary = sorted(
        (source for source in source_order if source != primary),
        key=lambda source: (source_totals[source], source_rank[source]),
    )
    intro_source = secondary[0] if secondary else primary
    outro_source = secondary[-1] if secondary else primary
    preferred_sources = (intro_source, primary, primary, outro_source)

    fission_rng = None
    if FISSION_VARIANT_SEED:
        fission_rng = random.Random(
            _stable_seed([clip.path for clip in clips], "dedup1-stage-weave")
        )

    ordered = []
    selected_sources = []
    for stage, preferred in enumerate(preferred_sources):
        available = stage_clips[stage]
        if not available:
            continue
        if fission_rng is not None:
            candidates = sorted(available, key=lambda source: source_rank[source])
            if selected_sources:
                alternatives = [source for source in candidates if source != selected_sources[-1]]
                if alternatives:
                    candidates = alternatives
            chosen = fission_rng.choice(candidates)
        elif preferred in available:
            chosen = preferred
        else:
            chosen = max(
                available,
                key=lambda source: (
                    sum(max(0.0, float(clip.duration or 0.0)) for clip in available[source]),
                    sum(float(clip.score or 0.0) for clip in available[source]),
                    -source_rank[source],
                ),
            )
        stage_run = sorted(
            available[chosen], key=lambda clip: (clip.sequence, clip.start, clip.path)
        )
        ordered.extend(stage_run)
        selected_sources.append(chosen)

    if not ordered:
        return clips

    initial_ids = {id(clip) for clip in ordered}
    selected_duration = sum(max(0.0, float(clip.duration or 0.0)) for clip in ordered)
    minimum_duration = max(0.0, float(minimum_duration or 0.0))
    if selected_duration + 0.01 < minimum_duration:
        extra_runs = []
        for stage in range(4):
            for source in source_order:
                run = [
                    clip for clip in stage_clips[stage].get(source, [])
                    if id(clip) not in initial_ids
                ]
                if run:
                    extra_runs.append((stage, source, run))
        while extra_runs and selected_duration + 0.01 < minimum_duration:
            stage, source, run = max(
                extra_runs,
                key=lambda item: (
                    sum(max(0.0, float(clip.duration or 0.0)) for clip in item[2]),
                    sum(float(clip.score or 0.0) for clip in item[2]),
                    -item[0],
                    -source_rank[item[1]],
                ),
            )
            extra_runs.remove((stage, source, run))
            ordered.extend(run)
            selected_duration += sum(
                max(0.0, float(clip.duration or 0.0)) for clip in run
            )
        ordered.sort(
            key=lambda clip: (
                clip_stages[id(clip)],
                0 if id(clip) in initial_ids else 1,
                source_rank[clip.source],
                clip.sequence,
                clip.start,
            )
        )
    print(
        f"   -> {'打乱轨' if THREE_POOL_MODE else '去重素材池1'}已从 "
        f"{len(clips)} 个完整话术/动作块中选取 {len(ordered)} 个；"
        f"按展示、安装、使用、结果重组，来源路径 {' -> '.join(selected_sources)}，"
        "重复阶段不再整段保留，也不逐句机械轮换。"
    )
    return ordered


def expand_shuffle_story_blocks(clips, minimum_duration, transition=0.10):
    """Replay processed natural blocks when K>5 needs more than source stock.

    Reuse happens only after every source has gone through the normal dedup
    normalization, complete-speech split, price filter and narrative ordering.
    Later cycles are deterministic rotations, so they add no duplicate source
    analysis/encoding work while still producing a replayable rearrangement.
    """
    base = list(clips)
    if not base:
        return []
    transition = max(0.0, float(transition or 0.0))
    required = max(0.0, float(minimum_duration or 0.0))

    def clip_duration(clip):
        value = float(get_duration(clip.path) or clip.duration or 0.0)
        return max(0.0, value)

    expanded = list(base)
    effective = sum(clip_duration(clip) for clip in expanded)
    effective -= transition * max(0, len(expanded) - 1)
    if effective <= 0.05:
        raise RuntimeError("打乱素材没有可用的完整话术或动作段。")
    if effective + 0.01 >= required:
        return expanded

    cycle = 1
    reused_blocks = 0
    base_seed = _stable_seed([clip.path for clip in base], "shuffle-replay")
    while effective + 0.01 < required:
        shift = (base_seed + cycle) % len(base)
        rotated = [*base[shift:], *base[:shift]]
        previous_source = expanded[-1].source_name or expanded[-1].source
        previous_path = expanded[-1].path
        if len(rotated) > 1:
            fallback = None
            for offset in range(len(rotated)):
                candidate = [*rotated[offset:], *rotated[:offset]]
                first_source = candidate[0].source_name or candidate[0].source
                if candidate[0].path != previous_path and fallback is None:
                    fallback = candidate
                if (
                    candidate[0].path != previous_path
                    and first_source != previous_source
                ):
                    rotated = candidate
                    break
            else:
                if fallback is not None:
                    rotated = fallback
        for clip in rotated:
            duration = clip_duration(clip)
            if duration <= transition + 0.05:
                continue
            effective += duration - (transition if expanded else 0.0)
            expanded.append(clip)
            reused_blocks += 1
            if effective + 0.01 >= required:
                break
        cycle += 1
        if cycle > 10000:
            raise RuntimeError("打乱自然段循环重编超出安全上限。")
    print(
        f"   -> 打乱素材库存已通过有效基准校验；为达到本条打乱预算，"
        f"复用已处理自然段 {reused_blocks} 段并按种子重编，不新增源素材分析。"
    )
    return expanded


def dedup_section_duration_target(clips):
    """Allocate one dedup pool independently from its useful, unique footage."""
    available = sum(max(0.0, float(clip.duration or 0.0)) for clip in clips)
    if available <= 0.0:
        return 0.0
    return min(
        DEDUP_SECTION_MAX_SECONDS,
        max(DEDUP_SECTION_MIN_SECONDS, available),
    )


def safe_sticker_size_percent():
    try:
        value = int(float(STICKER_SIZE_PERCENT_RAW))
    except (TypeError, ValueError):
        value = 100
    return max(30, min(500, value))


def parse_sticker_corner_config(sticker_files):
    if not STICKER_CORNER_CONFIG_RAW:
        return None
    try:
        raw_config = json.loads(STICKER_CORNER_CONFIG_RAW)
    except json.JSONDecodeError:
        print("[WARN] 贴纸角位配置无法读取，已回退为自动四角贴纸。")
        return None
    if not isinstance(raw_config, dict):
        return None

    by_name = {os.path.basename(path): path for path in sticker_files}
    config = {}
    for corner in STICKER_CORNERS:
        name = str(raw_config.get(corner, "") or "").strip()
        if not name or name.lower() == "none":
            continue
        if name in by_name:
            config[corner] = by_name[name]
        else:
            print(f"[WARN] 贴纸素材不存在，已跳过 {corner}: {name}")
    return config


def partition_sticker_files(sticker_files):
    """Separate fixed PNG corners from one GIF/MP4 full-frame layer."""
    corner_files = []
    dynamic_files = []
    for path in sticker_files:
        suffix = os.path.splitext(str(path))[1].lower()
        if suffix == ".png":
            corner_files.append(path)
        elif suffix in DYNAMIC_STICKER_SUFFIXES:
            dynamic_files.append(path)
    return corner_files, dynamic_files


def parse_fullscreen_sticker_config(dynamic_sticker_files):
    """Resolve one explicitly selected dynamic sticker without scanning extras."""
    if not FULLSCREEN_STICKER_CONFIG_RAW:
        return None
    try:
        raw_config = json.loads(FULLSCREEN_STICKER_CONFIG_RAW)
    except json.JSONDecodeError:
        print("[WARN] 动态全屏贴纸配置无法读取，已跳过该可选效果。")
        return None
    if not isinstance(raw_config, dict):
        print("[WARN] 动态全屏贴纸配置格式无效，已跳过该可选效果。")
        return None

    name = str(raw_config.get("name", "") or "").strip()
    if not name or name.lower() == "none":
        return None
    if os.path.basename(name) != name:
        print("[WARN] 动态全屏贴纸文件名不安全，已跳过该可选效果。")
        return None

    key_mode = str(raw_config.get("key_mode", "auto") or "auto").strip().lower()
    if key_mode not in DYNAMIC_STICKER_KEY_MODES:
        print(
            f"[WARN] 动态全屏贴纸透明模式无效：{key_mode}；"
            "已按自动识别继续。"
        )
        key_mode = "auto"

    by_name = {
        os.path.basename(path): path
        for path in dynamic_sticker_files
        if os.path.splitext(path)[1].lower() in DYNAMIC_STICKER_SUFFIXES
    }
    path = by_name.get(name)
    if not path:
        print(f"[WARN] 动态全屏贴纸素材不存在，已跳过：{name}")
        return None
    return {
        "name": name,
        "path": path,
        "key_mode": key_mode,
    }


def _dynamic_sticker_has_native_alpha(path):
    """Detect meaningful GIF transparency before considering green keying."""
    if os.path.splitext(str(path))[1].lower() != ".gif":
        return False
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-v",
                "info",
                "-i",
                str(path),
                "-vf",
                "select='eq(n,0)',format=rgba,alphaextract,signalstats,metadata=print",
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=12,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    match = re.search(
        r"lavfi\.signalstats\.YMIN=([0-9.]+)", result.stderr or ""
    )
    if not match:
        return False
    try:
        return float(match.group(1)) < 250.0
    except ValueError:
        return False


def build_dynamic_sticker_filter(
    video_label,
    sticker_input_index,
    render_duration,
    sticker_path,
    key_mode="auto",
    prefix="dynamic_sticker",
):
    """Overlay a looping, silent, full-canvas animation on the current video."""
    selected_key_mode = str(key_mode or "auto").strip().lower()
    if selected_key_mode not in DYNAMIC_STICKER_KEY_MODES:
        selected_key_mode = "auto"
    native_alpha = (
        selected_key_mode == "auto"
        and _dynamic_sticker_has_native_alpha(sticker_path)
    )
    prepared = f"{prefix}_prepared"
    output = f"{prefix}_out"
    sticker_filter = (
        f"[{sticker_input_index}:v]fps={FPS},"
        f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={W}:{H},setsar=1,format=rgba"
    )
    key_strategy = "保留 GIF 原生透明"
    if selected_key_mode == "green" or (
        selected_key_mode == "auto" and not native_alpha
    ):
        sticker_filter += ",colorkey=0x00FF00:0.20:0.08"
        key_strategy = "自动去除绿幕" if selected_key_mode == "auto" else "强制去除绿幕"
    elif selected_key_mode == "off":
        key_strategy = "保留原始背景"
    sticker_filter += (
        f",trim=duration={float(render_duration):.3f},"
        f"setpts=PTS-STARTPTS[{prepared}]"
    )
    filters = [
        sticker_filter,
        (
            f"{video_label}[{prepared}]overlay=0:0:"
            f"shortest=1:format=auto[{output}]"
        ),
    ]
    return filters, f"[{output}]", key_strategy


def read_text(path):
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except OSError as exc:
        # A cloud-backed optional requirement file can temporarily be only a
        # placeholder. Treat it as empty instead of aborting the render job.
        print(f"[WARN] 生成要求文件暂不可读，已按空要求继续: {path} ({exc})")
        return ""


def _remove_promo_artifact(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _clear_promo_artifacts():
    for path in (PROMO_COPY_FILE, PROMO_AUDIO_FILE, PROMO_META_FILE):
        _remove_promo_artifact(path)


def _write_promo_skip_status(message, reason):
    """Replace every prior AI artifact with the status for the current run."""
    _remove_promo_artifact(PROMO_AUDIO_FILE)
    with open(PROMO_COPY_FILE, "w", encoding="utf-8") as handle:
        handle.write(message.rstrip() + "\n")
    metadata = {
        "provider": "none",
        "used_visual_model": False,
        "model": None,
        "frame_count": 0,
        "tts_generated": False,
        "reason": reason,
    }
    with open(PROMO_META_FILE, "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
    return metadata


def _command_video_encoder(cmd):
    for encoder in ("h264_nvenc", "h264_videotoolbox", "libx264"):
        if encoder in cmd:
            return encoder
    return ""


def _is_hardware_encoder_startup_failure(cmd, stderr):
    encoder = _command_video_encoder(cmd)
    if encoder not in {"h264_nvenc", "h264_videotoolbox"}:
        return False
    lowered = str(stderr or "").lower()
    if encoder == "h264_videotoolbox":
        return "videotoolbox" in lowered and any(
            marker in lowered
            for marker in (
                "cannot create compression session",
                "failed to create vtcompressionsession",
                "error while opening encoder",
                "encoder not available",
                "generic error in an external library",
            )
        )
    # Any failed command that explicitly selected h264_nvenc gets one exact
    # CPU retry in auto mode.  This covers new driver/session error text as
    # well as known failures; unrelated input/filter errors fail the retry too
    # and are still surfaced.  Strict benchmark mode never enters this branch.
    return True


def _libx264_fallback_command(cmd, crf=23, preset="veryfast"):
    """Replace hardware-only arguments while preserving filters and mappings."""
    fallback = []
    skip_with_value = {
        "-q:v",
        "-realtime",
        "-prio_speed",
        "-allow_sw",
        "-gpu",
        "-preset",
        "-tune",
        "-rc",
        "-cq",
        "-b:v",
        "-multipass",
        "-spatial-aq",
        "-temporal-aq",
        "-aq-strength",
        "-rc-lookahead",
        "-b_ref_mode",
    }
    index = 0
    while index < len(cmd):
        token = cmd[index]
        if token in skip_with_value:
            index += 2
            continue
        if token in {"h264_videotoolbox", "h264_nvenc"}:
            fallback.extend(["libx264", "-preset", str(preset), "-crf", str(crf)])
            if FFMPEG_THREADS:
                fallback.extend(["-threads", str(FFMPEG_THREADS)])
        else:
            fallback.append(token)
        index += 1
    # Older commands assembled before the encoder helper gained an explicit
    # pixel format can still enter this emergency fallback.  Add the same WMP
    # compatibility contract before the output path rather than relying on
    # FFmpeg's filter-graph negotiation.
    if "-pix_fmt" not in fallback and fallback:
        fallback[-1:-1] = ["-pix_fmt", "yuv420p"]
    return fallback


def _disable_hardware_encoder_for_current_run(reason=""):
    global _VIDEO_ENCODER_ARGS, _VIDEO_ENCODER_REASON
    with _VIDEO_ENCODER_LOCK:
        _VIDEO_ENCODER_ARGS = "libx264"
        _VIDEO_ENCODER_REASON = str(reason or "硬件编码启动失败")


def _record_encoder_attempt(encoder, elapsed_seconds, returncode):
    if encoder not in _ENCODER_METRICS:
        return
    with _ENCODER_METRICS_LOCK:
        metrics = _ENCODER_METRICS[encoder]
        metrics["attempts"] += 1
        metrics["seconds"] += max(0.0, float(elapsed_seconds))
        if int(returncode) == 0:
            metrics["successes"] += 1
        else:
            metrics["failures"] += 1


def _print_encoder_step(encoder, elapsed_seconds, returncode, fallback=False):
    if not encoder:
        return
    payload = {
        "encoder": encoder,
        "elapsed_seconds": round(max(0.0, float(elapsed_seconds)), 3),
        "returncode": int(returncode),
        "fallback": bool(fallback),
    }
    print(
        "V16_ENCODER_STEP "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _emit_encoder_summary():
    with _ENCODER_METRICS_LOCK:
        metrics = {
            name: {
                **values,
                "seconds": round(float(values["seconds"]), 3),
            }
            for name, values in _ENCODER_METRICS.items()
        }
    with _GPU_PIPELINE_LOCK:
        gpu_pipeline = {
            **_GPU_PIPELINE_METRICS,
            "seconds": round(float(_GPU_PIPELINE_METRICS["seconds"]), 3),
            "disabled_reason": _GPU_PIPELINE_DISABLED_REASON,
        }
    payload = {
        "selected": _encoder_codec_name(_VIDEO_ENCODER_ARGS),
        "mode": _VIDEO_ENCODER_ARGS or "unselected",
        "policy": _video_encoder_policy(),
        "gpu_index": NVIDIA_GPU_INDEX,
        "fallback_count": int(_ENCODER_FALLBACK_COUNT),
        "process_elapsed_seconds": round(
            max(0.0, time.perf_counter() - _ENCODER_PROCESS_STARTED),
            3,
        ),
        "metrics": metrics,
        "gpu_pipeline": gpu_pipeline,
    }
    print(
        "V16_ENCODER_SUMMARY "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _emit_performance_summary():
    with _PERF_STAGE_LOCK:
        stages = {
            name: {
                "count": int(values["count"]),
                "seconds": round(float(values["seconds"]), 3),
            }
            for name, values in sorted(_PERF_STAGE_METRICS.items())
        }
    with _MEDIA_PROBE_CACHE_LOCK:
        probe_cache = {
            "entries": len(_MEDIA_PROBE_CACHE),
            "hits": int(_MEDIA_PROBE_CACHE_HITS),
            "misses": int(_MEDIA_PROBE_CACHE_MISSES),
        }
    with _MODULE_AVAILABILITY_LOCK:
        module_cache_entries = len(_MODULE_AVAILABILITY_CACHE)
    print(
        "V16_PERF_SUMMARY "
        + json.dumps(
            {
                "version": "gpu-priority-v26",
                "process_elapsed_seconds": round(
                    max(0.0, time.perf_counter() - _ENCODER_PROCESS_STARTED),
                    3,
                ),
                "stages": stages,
                "media_probe_cache": probe_cache,
                "module_cache_entries": module_cache_entries,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def run_ffmpeg(cmd):
    global _ENCODER_FALLBACK_COUNT
    encoder = _command_video_encoder(cmd)
    started = time.perf_counter()
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    elapsed = time.perf_counter() - started
    _record_encoder_attempt(encoder, elapsed, res.returncode)
    _print_encoder_step(encoder, elapsed, res.returncode)
    if (
        res.returncode != 0
        and _video_encoder_policy() != "nvidia-strict"
        and _is_hardware_encoder_startup_failure(cmd, res.stderr)
    ):
        failed_encoder = encoder
        _disable_hardware_encoder_for_current_run(res.stderr)
        with _ENCODER_METRICS_LOCK:
            _ENCODER_FALLBACK_COUNT += 1
        fallback_cmd = _libx264_fallback_command(cmd)
        print(
            f"[WARN] {failed_encoder} 启动失败，已明确记录并切换 CPU "
            "libx264 重试当前步骤；本次任务不能计作 GPU 测速样本。"
        )
        print(
            "V16_ENCODER_FALLBACK "
            + json.dumps(
                {
                    "from": failed_encoder,
                    "to": "libx264",
                    "stage": "ffmpeg",
                    "reason": "runtime",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        fallback_started = time.perf_counter()
        res = subprocess.run(
            fallback_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        fallback_elapsed = time.perf_counter() - fallback_started
        _record_encoder_attempt("libx264", fallback_elapsed, res.returncode)
        _print_encoder_step(
            "libx264",
            fallback_elapsed,
            res.returncode,
            fallback=True,
        )
        if res.returncode == 0:
            return
        cmd = fallback_cmd
    if res.returncode != 0:
        print(f"[FFMPEG ERROR] Command: {' '.join(cmd)}\nError: {res.stderr}")
        raise RuntimeError(res.stderr)


def _media_probe_cache_key(file_path):
    try:
        absolute = os.path.abspath(os.fspath(file_path))
        stat_result = os.stat(absolute)
    except (OSError, TypeError, ValueError):
        return None
    return absolute, int(stat_result.st_size), int(stat_result.st_mtime_ns)


def _cached_media_probe(namespace, file_path, loader):
    """Cache immutable probe results by path, size and nanosecond mtime."""
    global _MEDIA_PROBE_CACHE_HITS, _MEDIA_PROBE_CACHE_MISSES
    fingerprint = _media_probe_cache_key(file_path)
    if fingerprint is None:
        return loader()
    cache_key = (str(namespace), *fingerprint)
    with _MEDIA_PROBE_CACHE_LOCK:
        if cache_key in _MEDIA_PROBE_CACHE:
            _MEDIA_PROBE_CACHE_HITS += 1
            return _MEDIA_PROBE_CACHE[cache_key]
        _MEDIA_PROBE_CACHE_MISSES += 1
    value = loader()
    with _MEDIA_PROBE_CACHE_LOCK:
        _MEDIA_PROBE_CACHE[cache_key] = value
    return value


def get_duration(file_path):
    def probe():
        res = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        try:
            return float(res.stdout.strip())
        except Exception:
            return 0.0

    return _cached_media_probe("format-duration", file_path, probe)


_WINDOWS_MEDIA_PLAYER_H264_PROFILES = frozenset(
    {"Constrained Baseline", "Baseline", "Main", "High"}
)


def _probe_output_video_contract(file_path):
    """Return the first video stream fields used by the delivery contract."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                (
                    "stream=codec_name,codec_tag_string,profile,pix_fmt,"
                    "width,height,sample_aspect_ratio:stream_tags=rotate:"
                    "stream_side_data=rotation"
                ),
                "-of",
                "json",
                file_path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    streams = payload.get("streams") or []
    if not streams or not isinstance(streams[0], dict):
        return None
    return streams[0]


def _is_square_pixel_ratio(value):
    text = str(value or "").strip()
    separator = ":" if ":" in text else "/" if "/" in text else ""
    if not separator:
        return False
    try:
        numerator, denominator = (float(part) for part in text.split(separator, 1))
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(numerator)
        and math.isfinite(denominator)
        and numerator > 0
        and math.isclose(numerator, denominator, abs_tol=1e-9)
    )


def _output_stream_rotation_values(stream):
    values = []
    if not isinstance(stream, dict):
        return values
    tags = stream.get("tags")
    if isinstance(tags, dict) and "rotate" in tags:
        values.append(tags.get("rotate"))
    side_data_list = stream.get("side_data_list")
    if isinstance(side_data_list, list):
        values.extend(
            item.get("rotation")
            for item in side_data_list
            if isinstance(item, dict) and "rotation" in item
        )
    return values


def _has_nonzero_or_invalid_rotation(stream):
    for value in _output_stream_rotation_values(stream):
        try:
            rotation = float(value)
        except (TypeError, ValueError):
            return True
        if not math.isfinite(rotation) or not math.isclose(
            rotation % 360.0, 0.0, abs_tol=1e-6
        ):
            return True
    return False


def _output_video_contract_violations(stream):
    """Describe every mismatch against the Windows 9:16 delivery contract."""
    if not isinstance(stream, dict):
        return ["ffprobe 无法读取第一路视频流"]
    violations = []
    if stream.get("codec_name") != "h264":
        violations.append(f"编码={stream.get('codec_name') or '未知'}（要求 H.264）")
    if stream.get("codec_tag_string") not in {"avc1", "avc3"}:
        violations.append(
            f"MP4 编码标记={stream.get('codec_tag_string') or '未知'}（要求 avc1/avc3）"
        )
    if stream.get("profile") not in _WINDOWS_MEDIA_PLAYER_H264_PROFILES:
        violations.append(
            f"H.264 profile={stream.get('profile') or '未知'}（Windows 兼容档不匹配）"
        )
    if stream.get("pix_fmt") != "yuv420p":
        violations.append(
            f"像素格式={stream.get('pix_fmt') or '未知'}（要求 yuv420p）"
        )
    try:
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
    except (TypeError, ValueError):
        width, height = 0, 0
    if (width, height) != (W, H):
        violations.append(f"画幅={width}x{height}（要求 {W}x{H}）")
    if not _is_square_pixel_ratio(stream.get("sample_aspect_ratio")):
        violations.append(
            "采样宽高比="
            f"{stream.get('sample_aspect_ratio') or '未知'}（要求 SAR 1:1）"
        )
    if _has_nonzero_or_invalid_rotation(stream):
        rotation_values = _output_stream_rotation_values(stream)
        violations.append(f"旋转元数据={rotation_values}（要求 0°）")
    return violations


def _is_windows_media_player_h264_compatible(file_path):
    """Return whether an MP4 video stream satisfies the full delivery contract.

    The no-overlay finalization path normally remuxes the already-normalized
    timeline without another lossy generation.  Probe that assumption before
    stream-copying: a legacy/stale timeline may still have an incompatible
    codec, pixel format or canvas.  Any probe failure deliberately returns
    False so the caller takes the safe normalized transcode path.
    """
    return not _output_video_contract_violations(
        _probe_output_video_contract(file_path)
    )


def ensure_final_output_contract(file_path):
    """Verify the published geometry and repair only a mismatching candidate.

    A normal, already-compliant render pays for exactly one additional ffprobe
    and its bytes are left untouched.  A mismatch is corrected into a sibling
    temporary MP4, verified again, then atomically replaces only the pending
    candidate; the prior published output is never modified on failure.
    """
    stream = _probe_output_video_contract(file_path)
    violations = _output_video_contract_violations(stream)
    if not violations:
        print(
            f"   -> 成片合同：H.264/yuv420p，{W}x{H}，SAR 1:1（无需重编码）。"
        )
        return False

    print("[WARN] 成片合同不匹配，执行一次安全纠正：" + "；".join(violations))
    absolute_path = os.path.abspath(os.fspath(file_path))
    output_directory = os.path.dirname(absolute_path)
    descriptor, corrected_path = tempfile.mkstemp(
        # Keep the sibling name short for Windows installations already close
        # to the legacy MAX_PATH boundary.
        prefix=".v16-contract-",
        suffix=".mp4",
        dir=output_directory,
    )
    os.close(descriptor)
    try:
        run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-i",
                absolute_path,
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-vf",
                (
                    f"scale={W}:{H}:force_original_aspect_ratio=increase:"
                    f"flags=lanczos,crop={W}:{H},setsar=1,format=yuv420p"
                ),
                *_video_encoder_args(23, "fast"),
                "-fps_mode",
                "cfr",
                "-video_track_timescale",
                "90000",
                "-tag:v",
                "avc1",
                "-metadata:s:v:0",
                "rotate=0",
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                corrected_path,
            ]
        )
        corrected_stream = _probe_output_video_contract(corrected_path)
        corrected_violations = _output_video_contract_violations(corrected_stream)
        if corrected_violations:
            raise RuntimeError(
                "成片合同纠正后仍不合格：" + "；".join(corrected_violations)
            )
        os.replace(corrected_path, absolute_path)
    finally:
        if os.path.exists(corrected_path):
            os.remove(corrected_path)
    print(
        f"   -> 成片合同已纠正并复验：H.264/yuv420p，{W}x{H}，SAR 1:1。"
    )
    return True


def _parse_frame_rate(value):
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            denominator = float(denominator)
            return float(numerator) / denominator if denominator else 0.0
        return float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def _probe_source_fps_duration(file_path):
    def probe():
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=avg_frame_rate,r_frame_rate,duration:format=duration",
                "-of",
                "json",
                file_path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return 0.0, 0.0
        streams = payload.get("streams") or []
        stream = streams[0] if streams else {}
        fps = _parse_frame_rate(stream.get("avg_frame_rate"))
        if fps <= 0:
            fps = _parse_frame_rate(stream.get("r_frame_rate"))
        try:
            duration = float(
                stream.get("duration")
                or payload.get("format", {}).get("duration")
                or 0
            )
        except (TypeError, ValueError):
            duration = 0.0
        return max(0.0, fps), max(0.0, duration)

    return _cached_media_probe("source-fps-duration", file_path, probe)


def _choose_auto_output_fps(samples, threshold=AUTO_FPS_60_THRESHOLD):
    """按素材时长加权；真实高帧率素材达到阈值时才输出 60fps。"""
    usable = [(float(fps), max(0.0, float(duration))) for fps, duration in samples if fps > 0]
    if not usable:
        return 30, 0.0
    total_weight = sum(duration if duration > 0 else 1.0 for _fps, duration in usable)
    high_fps_weight = sum(
        duration if duration > 0 else 1.0
        for fps, duration in usable
        if fps >= 50.0
    )
    ratio = high_fps_weight / total_weight if total_weight > 0 else 0.0
    return (60 if ratio + 1e-9 >= threshold else 30), ratio


def _resolve_output_fps(video_paths):
    if OUTPUT_FPS_MODE != "auto":
        return FPS, {
            "mode": "fixed",
            "high_fps_ratio": 1.0 if FPS >= 50 else 0.0,
            "sample_count": len(video_paths),
        }
    samples = [_probe_source_fps_duration(path) for path in video_paths]
    chosen_fps, ratio = _choose_auto_output_fps(samples)
    return chosen_fps, {
        "mode": "auto",
        "high_fps_ratio": ratio,
        "sample_count": sum(1 for fps, _duration in samples if fps > 0),
    }


def get_stream_duration(file_path, stream_selector):
    def probe():
        res = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                stream_selector,
                "-show_entries",
                "stream=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        try:
            return float(res.stdout.strip().splitlines()[0])
        except (ValueError, IndexError):
            return 0.0

    return _cached_media_probe(
        f"stream-duration:{stream_selector}",
        file_path,
        probe,
    )


def timeline_video_duration(file_path, stage="时间线"):
    """Return real visible video time; container/audio time may include padding."""
    video_duration = get_stream_duration(file_path, "v:0")
    if video_duration <= 0:
        raise RuntimeError(
            f"{stage}无法读取视频流时长：{os.path.basename(file_path)}"
        )
    return video_duration


def require_av_duration_parity(file_path, target_duration, stage):
    """Reject a media stage whose video or audio silently ended early."""
    target = max(1.0 / FPS, float(target_duration or 0.0))
    video_duration = get_stream_duration(file_path, "v:0")
    audio_duration = get_stream_duration(file_path, "a:0")
    tolerance = max(0.15, 2.0 / FPS)
    if (
        video_duration <= 0
        or audio_duration <= 0
        or abs(video_duration - target) > tolerance
        or abs(audio_duration - target) > tolerance
        or abs(video_duration - audio_duration) > tolerance
    ):
        raise RuntimeError(
            f"{stage}音画时长校验失败："
            f"视频 {video_duration:.3f}s，音频 {audio_duration:.3f}s，"
            f"目标 {target:.3f}s。"
        )
    return video_duration, audio_duration


_VISUAL_SIGNAL_KEYS = frozenset(
    {"SATAVG", "SATHIGH", "YAVG", "YHIGH", "UAVG", "VAVG"}
)


def _parse_visual_signalstats(raw_text, *, segment=0):
    """Parse FFmpeg metadata=print output into complete one-second samples."""
    samples = []
    incomplete_frames = 0
    current = None
    frame_pattern = re.compile(
        r"\bframe:\s*\d+.*?\bpts_time:\s*"
        r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))"
    )
    value_pattern = re.compile(
        r"lavfi\.signalstats\."
        r"(SATAVG|SATHIGH|YAVG|YHIGH|UAVG|VAVG)="
        r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))"
    )

    def finish_frame():
        nonlocal incomplete_frames
        if current is None:
            return
        if _VISUAL_SIGNAL_KEYS.issubset(current):
            samples.append(dict(current))
        else:
            incomplete_frames += 1

    for line in str(raw_text or "").splitlines():
        frame_match = frame_pattern.search(line)
        if frame_match:
            finish_frame()
            current = {
                "time": float(frame_match.group(1)),
                "segment": int(segment),
            }
            continue
        value_match = value_pattern.search(line)
        if value_match and current is not None:
            current[value_match.group(1)] = float(value_match.group(2))
    finish_frame()
    return samples, incomplete_frames


def _visual_signal_sample_flags(sample):
    gray = (
        float(sample["SATAVG"]) <= VISUAL_GRAY_SAT_AVG_MAX
        and float(sample["SATHIGH"]) <= VISUAL_GRAY_SAT_HIGH_MAX
        and abs(float(sample["UAVG"]) - VISUAL_GRAY_CHROMA_CENTER)
        <= VISUAL_GRAY_CHROMA_TOLERANCE
        and abs(float(sample["VAVG"]) - VISUAL_GRAY_CHROMA_CENTER)
        <= VISUAL_GRAY_CHROMA_TOLERANCE
    )
    white_ink = (
        gray
        and float(sample["YAVG"]) >= VISUAL_LINE_ART_Y_AVG_MIN
        and float(sample["YHIGH"]) >= VISUAL_LINE_ART_Y_HIGH_MIN
    )
    return gray, white_ink


def _visual_signal_summary(samples, *, duration):
    """Summarize low-colour and bright line-art runs on one rendered timeline."""
    ordered = sorted(
        (dict(sample) for sample in samples),
        key=lambda item: (int(item.get("segment", 0)), float(item["time"])),
    )
    duration = max(0.0, float(duration or 0.0))
    totals = {"gray": 0.0, "white_ink": 0.0}
    longest = {"gray": 0.0, "white_ink": 0.0}
    intervals = {"gray": [], "white_ink": []}
    active = {
        "gray": {"segment": None, "start": None, "last": None},
        "white_ink": {"segment": None, "start": None, "last": None},
    }

    def close_run(kind):
        run = active[kind]
        if run["start"] is None:
            return
        run_seconds = max(1.0, float(run["last"]) - float(run["start"]) + 1.0)
        longest[kind] = max(longest[kind], run_seconds)
        intervals[kind].append(
            {
                "segment": int(run["segment"]),
                "start_seconds": round(float(run["start"]), 3),
                "end_seconds": round(float(run["last"]) + 1.0, 3),
                "duration_seconds": round(run_seconds, 3),
            }
        )
        run.update({"segment": None, "start": None, "last": None})

    for sample in ordered:
        segment = int(sample.get("segment", 0))
        sample_time = float(sample["time"])
        try:
            sample_weight = float(sample.get("weight_seconds", 1.0))
        except (TypeError, ValueError):
            sample_weight = 0.0
        if not math.isfinite(sample_weight) or sample_weight <= 0.0:
            raise VisualQualityError(
                "成片画质验收的素材基线权重无效，已停止交付。"
            )
        gray, white_ink = _visual_signal_sample_flags(sample)
        for kind, matched in (("gray", gray), ("white_ink", white_ink)):
            run = active[kind]
            contiguous = (
                run["start"] is not None
                and run["segment"] == segment
                and 0.0 <= sample_time - float(run["last"]) <= 1.5
            )
            if not matched:
                close_run(kind)
                continue
            totals[kind] += sample_weight
            if not contiguous:
                close_run(kind)
                run = active[kind]
                run["segment"] = segment
                run["start"] = sample_time
            run["last"] = sample_time
    close_run("gray")
    close_run("white_ink")

    sample_count = len(ordered)
    sampled_seconds = sum(
        float(sample.get("weight_seconds", 1.0)) for sample in ordered
    )
    denominator = max(1.0, sampled_seconds)
    return {
        "sample_count": sample_count,
        "sampled_seconds": round(sampled_seconds, 3),
        "declared_duration_seconds": round(duration, 3),
        "gray_seconds": round(totals["gray"], 3),
        "gray_ratio": totals["gray"] / denominator,
        "gray_longest_run_seconds": round(longest["gray"], 3),
        "gray_intervals": intervals["gray"],
        "white_ink_seconds": round(totals["white_ink"], 3),
        "white_ink_ratio": totals["white_ink"] / denominator,
        "white_ink_longest_run_seconds": round(longest["white_ink"], 3),
        "white_ink_intervals": intervals["white_ink"],
    }


def _visual_quality_decision(candidate, source, *, duration):
    """Return the source-relative decision for two precomputed summaries."""
    duration = max(0.0, float(duration or 0.0))
    white_seconds = float(candidate["white_ink_ratio"]) * duration
    source_white_seconds = float(source["white_ink_ratio"]) * duration
    gray_seconds = float(candidate["gray_ratio"]) * duration
    source_gray_seconds = float(source["gray_ratio"]) * duration
    white_blocked = (
        float(candidate["white_ink_longest_run_seconds"])
        >= VISUAL_WHITE_MIN_RUN_SECONDS
        and white_seconds >= VISUAL_WHITE_MIN_SECONDS
        and white_seconds - source_white_seconds >= max(5.0, 0.04 * duration)
        and float(candidate["white_ink_ratio"])
        - float(source["white_ink_ratio"])
        >= VISUAL_WHITE_MIN_RATIO_DELTA
    )
    gray_blocked = (
        float(candidate["gray_longest_run_seconds"])
        >= VISUAL_GRAY_MIN_RUN_SECONDS
        and gray_seconds >= VISUAL_GRAY_MIN_SECONDS
        and gray_seconds - source_gray_seconds >= max(8.0, 0.06 * duration)
        and float(candidate["gray_ratio"]) - float(source["gray_ratio"])
        >= VISUAL_GRAY_MIN_RATIO_DELTA
    )
    status = (
        "blocked-white-ink"
        if white_blocked
        else "blocked-gray"
        if gray_blocked
        else "passed"
    )
    return {
        "status": status,
        "passed": status == "passed",
        "candidate_white_seconds": round(white_seconds, 3),
        "source_white_expected_seconds": round(source_white_seconds, 3),
        "candidate_gray_seconds": round(gray_seconds, 3),
        "source_gray_expected_seconds": round(source_gray_seconds, 3),
    }


def _scan_visual_signalstats(
    path,
    *,
    start_seconds=0.0,
    duration_seconds=None,
    segment=0,
    timeout_seconds=120.0,
):
    """Read one signalstats frame per second from the centre picture area."""
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "warning"]
    start_seconds = max(0.0, float(start_seconds or 0.0))
    if start_seconds > 0.001:
        command.extend(["-ss", f"{start_seconds:.3f}"])
    command.extend(["-i", str(path)])
    if duration_seconds is not None:
        command.extend(["-t", f"{max(0.05, float(duration_seconds)):.3f}"])
    crop_width = int(round(720 * VISUAL_QUALITY_CROP_WIDTH_RATIO))
    crop_height = int(round(1280 * VISUAL_QUALITY_CROP_HEIGHT_RATIO))
    crop_width -= crop_width % 2
    crop_height -= crop_height % 2
    crop_x = (720 - crop_width) // 2
    crop_y = (1280 - crop_height) // 2
    command.extend(
        [
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            (
                "fps=1,"
                "scale=720:1280:force_original_aspect_ratio=increase,"
                "crop=720:1280,"
                f"crop={crop_width}:{crop_height}:{crop_x}:{crop_y},"
                "scale=144:256:flags=area,"
                "signalstats,metadata=mode=print:file=-"
            ),
            "-f",
            "null",
            "-",
        ]
    )
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=max(5.0, float(timeout_seconds)),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VisualQualityError(
            "成片画质验收未能完成（画面抽样工具不可用或超时）；"
            "为避免交付异常成片，本候选已停止。"
        ) from exc
    if result.returncode != 0:
        raise VisualQualityError(
            "成片画质验收未能完成（画面抽样读取失败）；"
            "为避免交付异常成片，本候选已停止。"
        )
    samples, incomplete_frames = _parse_visual_signalstats(
        f"{result.stdout or ''}\n{result.stderr or ''}",
        segment=segment,
    )
    if incomplete_frames or not samples:
        raise VisualQualityError(
            "成片画质验收未能完成（画面统计字段不完整）；"
            "为避免交付异常成片，本候选已停止。"
        )
    return samples


def _resolve_visual_audit_source(item):
    """Resolve a path-free audit entry only inside its declared material pool."""
    source_name = str(item.get("source") or "")
    if not source_name or os.path.basename(source_name) != source_name:
        raise VisualQualityError("成片画质验收的素材窗口记录不安全，已停止交付。")
    role = str(item.get("role") or "")
    role_directories = {
        "core": (CORE_DIR,),
        "shuffle": (SHUFFLE_DIR,),
        "timeline": (CORE_DIR, QUAL_DIR, DEDUP_DIR, SHUFFLE_DIR),
    }
    directories = role_directories.get(role)
    if not directories:
        raise VisualQualityError("成片画质验收的素材窗口角色无效，已停止交付。")
    matches = []
    for directory in directories:
        root = os.path.realpath(directory)
        candidate = os.path.join(directory, source_name)
        if os.path.islink(candidate) or not os.path.isfile(candidate):
            continue
        resolved = os.path.realpath(candidate)
        if os.path.dirname(resolved) == root:
            matches.append(resolved)
    matches = sorted(set(matches))
    if len(matches) != 1:
        raise VisualQualityError(
            "成片画质验收无法唯一对应素材窗口，已停止交付。"
        )
    return matches[0]


_DESTRUCTIVE_STYLE_FILTER_FRAGMENTS = (
    "format=gray",
    "hue=s=0",
    "edgedetect",
    "negate",
    "threshold",
    "monochrome",
    "colorchannelmixer",
    "saturation=0",
)


def _clean_render_style_invariant():
    """Attest configuration safety without judging legitimate source colours.

    The former source/candidate colour-ratio comparison could not distinguish a
    naturally gray product shot from a destructive style after zooms, stickers
    and other layout passes. This invariant blocks the known cause at the
    configuration boundary and deliberately makes no claim about source pixels.
    """
    if REQUESTED_FAST_DEDUP_STYLE != "clean" or FAST_DEDUP_STYLE != "clean":
        raise VisualQualityError("检测到已停用的黑白/线稿快速风格，已停止交付。")
    style_filters = _fast_dedup_style_filters(FAST_DEDUP_STYLE)
    if style_filters != []:
        raise VisualQualityError("clean 快速风格意外产生画面滤镜，已停止交付。")

    checked_filters = []
    for preserve_geometry in (False, True):
        for variant in range(8):
            checked_filters.extend(
                _dedup_visual_filters(
                    variant,
                    preserve_geometry=preserve_geometry,
                )
            )
    normalized_filters = [str(item).replace(" ", "").lower() for item in checked_filters]
    allowed_filter_prefixes = ("scale=", "crop=", "eq=")
    if any(
        not filter_expression.startswith(allowed_filter_prefixes)
        for filter_expression in normalized_filters
    ):
        raise VisualQualityError("镜头去重链出现未审核的画面滤镜，已停止交付。")
    if any(
        fragment in filter_expression
        for filter_expression in normalized_filters
        for fragment in _DESTRUCTIVE_STYLE_FILTER_FRAGMENTS
    ):
        raise VisualQualityError("镜头去重链出现破坏自然色彩的滤镜，已停止交付。")
    return style_filters, len(checked_filters)


def _visual_quality_report(candidate_path, source_windows=None):
    """Return a clean-style invariant attestation for one final candidate.

    The legacy function name is retained for release compatibility. This is a
    configuration and output-contract gate, not a colour classifier: natural
    grayscale/white source footage is valid and must not be rejected.
    """
    style_filters, checked_filter_count = _clean_render_style_invariant()
    stream = _probe_output_video_contract(candidate_path)
    violations = _output_video_contract_violations(stream)
    if violations:
        raise VisualQualityError("成片输出合同验收失败：" + "；".join(violations))
    windows = list(SOURCE_WINDOW_AUDIT if source_windows is None else source_windows)[:128]
    return {
        "version": VISUAL_QUALITY_GATE_VERSION,
        "gate_kind": "clean-style-invariant",
        "status": "passed",
        "passed": True,
        "policy": "natural-color-preserving-clean-only-v1",
        "requested_style": REQUESTED_FAST_DEDUP_STYLE,
        "effective_style": FAST_DEDUP_STYLE,
        "style_filters": list(style_filters),
        "dedup_filters_checked": checked_filter_count,
        "source_window_count": len(windows),
        "candidate_contract": {
            "codec_name": stream.get("codec_name"),
            "codec_tag_string": stream.get("codec_tag_string"),
            "profile": stream.get("profile"),
            "pix_fmt": stream.get("pix_fmt"),
            "width": int(stream.get("width") or 0),
            "height": int(stream.get("height") or 0),
            "sample_aspect_ratio": stream.get("sample_aspect_ratio"),
            "rotation": list(_output_stream_rotation_values(stream)),
        },
    }


def require_final_visual_quality(candidate_path, source_windows=None):
    report = _visual_quality_report(candidate_path, source_windows=source_windows)
    update_dedup_sidecar_visual_quality(report)
    print(
        "[CleanStyleInvariant] clean-only 渲染配置与成片输出合同验收通过；"
        "自然灰白素材不会被颜色比例误杀。"
    )
    return report


def has_audio_stream(file_path):
    def probe():
        res = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                file_path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        return res.returncode == 0 and bool(res.stdout.strip())

    return _cached_media_probe("has-audio", file_path, probe)


def has_video_stream(file_path):
    def probe():
        try:
            res = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=index",
                    "-of",
                    "csv=p=0",
                    file_path,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return res.returncode == 0 and bool(res.stdout.strip())

    return _cached_media_probe("has-video", file_path, probe)


def has_subtitle_stream(file_path):
    def probe():
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "s",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                file_path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    return _cached_media_probe("has-subtitle", file_path, probe)


def detect_burned_subtitles(file_path):
    """Sample the lower frame and require repeated OCR hits before declaring captions."""
    if not has_module("cv2") or not has_module("pytesseract"):
        return False
    code = r"""
import cv2, re, sys, pytesseract
path=sys.argv[1]
sample_count=max(4,int(sys.argv[2]))
cap=cv2.VideoCapture(path)
frames=int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
if frames<=0:
    print('NO'); raise SystemExit(0)
hits=0
for index in range(sample_count):
    frame_no=int(round((index+.5)*max(frames-1,1)/sample_count))
    cap.set(cv2.CAP_PROP_POS_FRAMES,frame_no)
    ok,frame=cap.read()
    if not ok: continue
    h,w=frame.shape[:2]
    band=frame[int(h*.48):int(h*.94),int(w*.04):int(w*.96)]
    gray=cv2.cvtColor(band,cv2.COLOR_BGR2GRAY)
    gray=cv2.resize(gray,None,fx=1.35,fy=1.35,interpolation=cv2.INTER_CUBIC)
    enhanced=cv2.morphologyEx(gray,cv2.MORPH_TOPHAT,cv2.getStructuringElement(cv2.MORPH_RECT,(19,5)))
    variants=(gray,enhanced,cv2.threshold(gray,0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)[1])
    matched=False
    for image in variants:
        text=pytesseract.image_to_string(image,lang='chi_sim+eng',config='--psm 6')
        compact=re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]','',text)
        if len(compact)>=4 and (re.search(r'[\u4e00-\u9fff]{2}',compact) or len(compact)>=8):
            matched=True; break
    hits+=int(matched)
cap.release()
print('YES' if hits>=max(2,int(round(sample_count*.25))) else 'NO')
"""
    exe = PY312 if os.path.exists(PY312) else sys.executable
    result = subprocess.run(
        [exe, "-c", code, file_path, str(SUBTITLE_OCR_SAMPLES)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        env=runtime_service_env(),
    )
    return result.returncode == 0 and "YES" in result.stdout


def video_has_visible_subtitles(file_path):
    return has_subtitle_stream(file_path) or detect_burned_subtitles(file_path)


def _subtitle_cache_key(file_path):
    stat = os.stat(file_path)
    raw = f"{os.path.realpath(file_path)}:{stat.st_size}:{stat.st_mtime_ns}:{SUBTITLE_ASR}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:18]


def _subtitle_asr_breaker_path():
    engine = re.sub(r"[^a-z0-9_-]+", "_", SUBTITLE_ASR.lower()).strip("_") or "default"
    return os.path.join(SUBTITLE_CACHE_DIR, f".{engine}_temporarily_unavailable.json")


def reset_subtitle_asr_degraded_state(*, clear_shared_marker=False):
    """Reset the process-local ASR circuit breaker (primarily for tests)."""
    global _SUBTITLE_ASR_DEGRADED_REASON, _SUBTITLE_ASR_WARNING_PRINTED
    _SUBTITLE_ASR_DEGRADED_REASON = ""
    _SUBTITLE_ASR_WARNING_PRINTED = False
    if clear_shared_marker:
        try:
            os.unlink(_subtitle_asr_breaker_path())
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _remember_subtitle_asr_degraded(reason):
    global _SUBTITLE_ASR_DEGRADED_REASON
    clean_reason = "HTTP 412" if "412" in str(reason) else str(reason).strip()[:80]
    _SUBTITLE_ASR_DEGRADED_REASON = clean_reason or "字幕服务暂时不可用"
    marker_path = _subtitle_asr_breaker_path()
    temp_path = f"{marker_path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(SUBTITLE_CACHE_DIR, exist_ok=True)
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "engine": SUBTITLE_ASR,
                    "reason": _SUBTITLE_ASR_DEGRADED_REASON,
                    "created_at": time.time(),
                },
                handle,
                ensure_ascii=False,
            )
        os.replace(temp_path, marker_path)
    except OSError:
        pass
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _active_subtitle_asr_degraded_reason():
    global _SUBTITLE_ASR_DEGRADED_REASON
    if _SUBTITLE_ASR_DEGRADED_REASON:
        return _SUBTITLE_ASR_DEGRADED_REASON
    marker_path = _subtitle_asr_breaker_path()
    try:
        with open(marker_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        created_at = float(payload.get("created_at") or 0.0)
        age = max(0.0, time.time() - created_at)
        if (
            payload.get("engine") == SUBTITLE_ASR
            and age <= SUBTITLE_ASR_BREAKER_TTL_SECONDS
        ):
            reason = str(payload.get("reason") or "字幕服务暂时不可用").strip()
            _SUBTITLE_ASR_DEGRADED_REASON = reason[:80]
            return _SUBTITLE_ASR_DEGRADED_REASON
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    try:
        os.unlink(marker_path)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return ""


def _clear_subtitle_asr_degraded_state():
    reset_subtitle_asr_degraded_state(clear_shared_marker=True)


def _warn_subtitle_asr_degraded_once():
    global _SUBTITLE_ASR_WARNING_PRINTED
    if _SUBTITLE_ASR_WARNING_PRINTED:
        return
    _SUBTITLE_ASR_WARNING_PRINTED = True
    print(
        "[WARN] 字幕云服务暂时不可用（HTTP 412）；本次制作已保留原视频和原声，"
        "后续字幕识别自动跳过。"
    )


def _sanitize_subtitle_failure_detail(raw_detail):
    text = str(raw_detail or "").replace("\r", "\n")
    candidates = []
    for line in text.splitlines():
        clean = line.strip()
        if not clean:
            continue
        lowered = clean.lower()
        if (
            clean.startswith("Traceback")
            or clean.startswith("File ")
            or clean.startswith("^")
            or "member.bilibili.com" in lowered
            or "rubick-interface" in lowered
        ):
            continue
        clean = re.sub(r"https?://\S+", "[远程服务]", clean)
        candidates.append(clean)
    detail = candidates[-1] if candidates else "远程字幕服务没有返回可用结果"
    return detail[:180]


def _atomic_publish_cache_file(source_path, destination_path):
    """Publish a complete cache file atomically across renderer processes."""
    source_path = os.path.abspath(source_path)
    destination_path = os.path.abspath(destination_path)
    destination_dir = os.path.dirname(destination_path)
    pending_path = ""
    try:
        os.makedirs(destination_dir, exist_ok=True)
        if os.path.lexists(destination_path) and (
            os.path.islink(destination_path)
            or not os.path.isfile(destination_path)
        ):
            return False
        descriptor, pending_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(destination_path)}.",
            suffix=".partial",
            dir=destination_dir,
        )
        os.close(descriptor)
        shutil.copyfile(source_path, pending_path)
        with open(pending_path, "r+b") as handle:
            os.fsync(handle.fileno())
        for attempt in range(40):
            try:
                os.replace(pending_path, destination_path)
                pending_path = ""
                return True
            except PermissionError:
                if attempt >= 39:
                    raise
                time.sleep(0.01)
    except OSError:
        return False
    finally:
        if pending_path:
            try:
                os.unlink(pending_path)
            except OSError:
                pass


def _copy_complete_cache_file(source_path, destination_path, minimum_size=16):
    """Copy one atomic cache generation, retrying brief Windows replace locks."""
    for attempt in range(20):
        try:
            if not os.path.isfile(source_path):
                return False
            if os.path.getsize(source_path) <= int(minimum_size):
                return False
            shutil.copyfile(source_path, destination_path)
            return os.path.getsize(destination_path) > int(minimum_size)
        except (FileNotFoundError, PermissionError):
            if attempt >= 19:
                return False
            time.sleep(0.01)
        except OSError:
            return False
    return False


def transcribe_video_to_srt(file_path, label):
    # Source-level rendering can prepare two independent materials at once.
    # Keep cache publication and the remote-ASR circuit breaker serialized so
    # two workers can never overwrite the same audio/SRT cache entry or stampede
    # the subtitle service. Once one worker starts its CPU-filter/NVENC render,
    # the other can transcribe in parallel with it.
    with _SUBTITLE_TRANSCRIBE_LOCK:
        return _transcribe_video_to_srt_locked(file_path, label)


def _transcribe_video_to_srt_locked(file_path, label):
    """Use the VideoCaptioner backend directly so a broken wrapper CLI cannot block V16."""
    if not has_audio_stream(file_path):
        print(f"   -> {label}没有音轨，无法自动匹配字幕。")
        return None
    subtitle_dir = os.path.join(TMP_DIR, "auto_subtitles")
    os.makedirs(subtitle_dir, exist_ok=True)
    cache_key = _subtitle_cache_key(file_path)
    audio_path = os.path.join(subtitle_dir, f"{cache_key}.mp3")
    srt_path = os.path.join(subtitle_dir, f"{cache_key}.srt")
    persistent_srt_path = os.path.join(SUBTITLE_CACHE_DIR, f"{cache_key}.srt")
    if os.path.exists(srt_path) and os.path.getsize(srt_path) > 16:
        return srt_path
    if _copy_complete_cache_file(persistent_srt_path, srt_path, minimum_size=16):
        print(f"   -> {label}已复用字幕识别缓存。")
        return srt_path
    if _active_subtitle_asr_degraded_reason():
        _warn_subtitle_asr_degraded_once()
        return None
    try:
        run_ffmpeg(
            [
                "ffmpeg", "-y", "-i", file_path, "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "libmp3lame", "-b:a", "64k", audio_path,
            ]
        )
    except (OSError, RuntimeError, ValueError):
        return None

    backend_code = r"""
import os, sys
sys.path.insert(0, sys.argv[4])
engine=sys.argv[3].strip().lower()
if engine == 'bijian':
    from v16_subtitle_asr import SubtitleServiceUnavailable, transcribe_to_srt
    try:
        transcribe_to_srt(sys.argv[1], sys.argv[2])
    except SubtitleServiceUnavailable as exc:
        print('V16_SUBTITLE_ASR_UNAVAILABLE:' + str(exc).replace('\n', ' '), file=sys.stderr)
        raise SystemExit(75)
    except Exception as exc:
        print(
            'V16_SUBTITLE_ASR_FAILED:' + type(exc).__name__ + ':'
            + str(exc).replace('\n', ' '),
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(sys.argv[2])
    raise SystemExit(0)
cache_root=os.environ.get('V16_VIDEOCAPTIONER_DATA_DIR','').strip()
if cache_root:
    import platformdirs
    platformdirs.user_data_dir=lambda *args,**kwargs: cache_root
from videocaptioner.core.entities import TranscribeConfig, TranscribeModelEnum
from videocaptioner.core.asr import transcribe
model_map={
    'bijian':TranscribeModelEnum.BIJIAN,
    'jianying':TranscribeModelEnum.JIANYING,
    'whisper-api':TranscribeModelEnum.WHISPER_API,
    'whisper-cpp':TranscribeModelEnum.WHISPER_CPP,
}
config=TranscribeConfig(
    transcribe_model=model_map.get(engine,TranscribeModelEnum.BIJIAN),
    transcribe_language='zh',
    need_word_time_stamp=False,
    whisper_api_key=os.environ.get('V16_OPENAI_API_KEY') or os.environ.get('OPENAI_API_KEY') or '',
    whisper_api_base=os.environ.get('V16_OPENAI_API_BASE') or os.environ.get('OPENAI_BASE_URL') or '',
    whisper_api_model=os.environ.get('V16_TRANSCRIBE_MODEL','gpt-4o-mini-transcribe'),
)
data=transcribe(sys.argv[1],config)
data.save(save_path=sys.argv[2])
print(sys.argv[2])
"""
    exe = PY312 if os.path.exists(PY312) else sys.executable
    env = runtime_service_env()
    try:
        result = subprocess.run(
            [
                exe,
                "-c",
                backend_code,
                audio_path,
                srt_path,
                SUBTITLE_ASR,
                os.path.dirname(os.path.abspath(__file__)),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=SUBTITLE_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        print(f"[WARN] {label}字幕识别超过 {SUBTITLE_TIMEOUT} 秒，已保留原视频。")
        return None
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(
            f"[WARN] {label}字幕识别暂不可用，已保留原视频和原声："
            f"{_sanitize_subtitle_failure_detail(exc)}"
        )
        return None
    if result.returncode != 0 or not os.path.exists(srt_path) or os.path.getsize(srt_path) <= 16:
        raw_detail = result.stderr or result.stdout or "未知错误"
        lowered_detail = raw_detail.lower()
        if (
            result.returncode == 75
            or "v16_subtitle_asr_unavailable" in lowered_detail
            or "412 client error" in lowered_detail
            or "precondition failed" in lowered_detail
            or "http 412" in lowered_detail
        ):
            _remember_subtitle_asr_degraded("HTTP 412")
            _warn_subtitle_asr_degraded_once()
            return None
        detail = _sanitize_subtitle_failure_detail(raw_detail)
        print(f"[WARN] {label}字幕识别失败，已保留原视频和原声：{detail}")
        return None
    _clear_subtitle_asr_degraded_state()
    try:
        _atomic_publish_cache_file(srt_path, persistent_srt_path)
    except OSError:
        pass
    print(f"   -> {label}已通过 VideoCaptioner/{SUBTITLE_ASR} 生成匹配字幕。")
    return srt_path


def burn_subtitles(video_path, subtitle_path, output_path, style=None):
    escaped = subtitle_path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    default_font = "Microsoft YaHei" if os.name == "nt" else "PingFang SC"
    style = style or (
        f"FontName={default_font},FontSize=18,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00161616,BorderStyle=1,Outline=2,Shadow=0,MarginV=76,Alignment=2"
    )
    run_ffmpeg(
        [
            "ffmpeg", "-y", "-i", video_path,
            "-vf", f"subtitles=filename='{escaped}':force_style='{style}'",
            "-map", "0:v:0", "-map", "0:a?",
            *_video_encoder_args(23, "veryfast"),
            "-c:a", "copy", "-movflags", "+faststart", output_path,
        ]
    )
    return output_path


def _srt_timestamp(seconds):
    milliseconds = max(0, int(round(float(seconds or 0.0) * 1000.0)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _wrap_dedup2_caption(text, max_chars=18):
    clean = re.sub(r"\s+", "", str(text or "")).strip()
    if len(clean) <= max_chars:
        return clean
    candidates = [
        index + 1
        for index, char in enumerate(clean[:-1])
        if char in "，、；："
        and max(6, len(clean) // 3) <= index + 1 <= min(len(clean) - 6, len(clean) * 2 // 3)
    ]
    split_at = min(candidates, key=lambda value: abs(value - len(clean) / 2.0)) if candidates else len(clean) // 2
    return clean[:split_at] + "\n" + clean[split_at:]


def write_dedup2_caption_srt(sentence_plan, output_path):
    """Write the exact post-retime sentence windows used by the voice bridge."""
    cursor = 0.0
    blocks = []
    for index, (sentence, duration) in enumerate(sentence_plan, 1):
        start = cursor
        cursor += max(1.0 / FPS, float(duration or 0.0))
        blocks.append(
            f"{index}\n{_srt_timestamp(start)} --> {_srt_timestamp(cursor)}\n"
            f"{_wrap_dedup2_caption(sentence)}\n"
        )
    with open(output_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(blocks))
    return output_path


def _dedup2_caption_font_path():
    configured = os.environ.get("V16_CAPTION_FONT", "").strip()
    candidates = [
        configured,
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    ]
    return next((path for path in candidates if path and os.path.isfile(path)), "")


def _render_dedup2_caption_layers(sentence_plan):
    """Create transparent Chinese caption cards without FFmpeg drawtext/libass."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("去重素材池2字幕渲染需要 Pillow。") from exc
    font_path = _dedup2_caption_font_path()
    if not font_path:
        raise RuntimeError("未找到可用于去重素材池2字幕的中文字体。")
    layer_dir = os.path.join(TMP_DIR, "dedup2_caption_layers")
    os.makedirs(layer_dir, exist_ok=True)
    font = ImageFont.truetype(font_path, 44)
    layers = []
    cursor = 0.0
    for index, (sentence, duration) in enumerate(sentence_plan):
        start = cursor
        cursor += max(1.0 / FPS, float(duration or 0.0))
        text = _wrap_dedup2_caption(sentence)
        canvas = Image.new("RGBA", (660, 150), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        bounds = draw.multiline_textbbox(
            (0, 0),
            text,
            font=font,
            spacing=8,
            align="center",
            stroke_width=4,
        )
        text_width = bounds[2] - bounds[0]
        text_height = bounds[3] - bounds[1]
        draw.multiline_text(
            ((canvas.width - text_width) / 2.0, (canvas.height - text_height) / 2.0 - bounds[1]),
            text,
            font=font,
            fill=(255, 255, 255, 255),
            stroke_width=4,
            stroke_fill=(20, 20, 20, 255),
            spacing=8,
            align="center",
        )
        layer_path = os.path.join(layer_dir, f"caption_{index:03d}.png")
        canvas.save(layer_path)
        layers.append((layer_path, start, cursor))
    return layers


def _overlay_dedup2_caption_layers(video_path, sentence_plan, output_path):
    layers = _render_dedup2_caption_layers(sentence_plan)
    if not layers:
        return video_path
    duration = get_duration(video_path)
    command = ["ffmpeg", "-y", "-i", video_path]
    for layer_path, _start, _end in layers:
        command.extend(["-loop", "1", "-framerate", str(FPS), "-i", layer_path])
    filters = []
    current = "[0:v]"
    for index, (_layer_path, start, end) in enumerate(layers, 1):
        filters.append(f"[{index}:v]format=rgba[caption_{index}]")
        next_label = f"[captioned_{index}]"
        filters.append(
            f"{current}[caption_{index}]overlay=x=(W-w)/2:y=H-h-92:"
            f"enable='between(t,{start:.3f},{end:.3f})':eof_action=pass{next_label}"
        )
        current = next_label
    filters.append(f"{current}fps={FPS},format=yuv420p[v_captioned]")
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[v_captioned]",
            "-map",
            "0:a:0?",
            "-t",
            f"{duration:.3f}",
            *_video_encoder_args(23, "veryfast"),
            "-g",
            str(FPS),
            "-fps_mode",
            "cfr",
            "-video_track_timescale",
            "90000",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            output_path,
        ]
    )
    run_ffmpeg(command)
    return output_path


def burn_dedup2_captions(video_path, sentence_plan, output_path):
    """Render generated dedup2 captions after all original-text cover effects."""
    if not sentence_plan:
        return video_path
    subtitle_path = os.path.join(TMP_DIR, "dedup2_generated_captions.srt")
    write_dedup2_caption_srt(sentence_plan, subtitle_path)
    _overlay_dedup2_caption_layers(video_path, sentence_plan, output_path)
    print("   -> 去重素材池2已在文字柔化特效之后叠加逐句同步字幕。")
    return output_path


def extract_embedded_subtitles(video_path, prefix):
    subtitle_dir = os.path.join(TMP_DIR, "auto_subtitles")
    os.makedirs(subtitle_dir, exist_ok=True)
    output_path = os.path.join(subtitle_dir, f"{prefix}_embedded.srt")
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-map", "0:s:0", output_path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 8:
        return output_path
    return None


def ensure_matching_subtitles(source_path, normalized_path, prefix):
    if not SUBTITLE_ENABLED:
        return normalized_path
    if detect_burned_subtitles(source_path):
        print(f"   -> {os.path.basename(source_path)} 已检测到画面字幕，直接保留。")
        return normalized_path
    subtitle_path = None
    if has_subtitle_stream(source_path):
        subtitle_path = extract_embedded_subtitles(source_path, prefix)
        if subtitle_path:
            print(f"   -> {os.path.basename(source_path)} 已提取内嵌字幕并转为成片字幕。")
    if not subtitle_path:
        subtitle_path = transcribe_video_to_srt(source_path, os.path.basename(source_path))
    if not subtitle_path:
        return normalized_path
    output_path = os.path.join(TMP_DIR, f"{prefix}_subtitled.mp4")
    try:
        burn_subtitles(normalized_path, subtitle_path, output_path)
    except RuntimeError:
        print(f"[WARN] {os.path.basename(source_path)} 字幕合成失败，已保留原视频。")
        return normalized_path
    return output_path


def _dotenv_values(path):
    values = {}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                    value = value[1:-1]
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                    values[key] = value
    except OSError:
        pass
    return values


def runtime_service_env():
    """Return subprocess/network settings without ever logging secret values."""
    env = os.environ.copy()
    env.setdefault(
        "V16_VIDEOCAPTIONER_DATA_DIR",
        os.path.join(TMP_DIR, "videocaptioner_data"),
    )
    openclaw_env = _dotenv_values(os.path.expanduser("~/.openclaw/.env"))
    for key in (
        "V16_DASHSCOPE_API_KEY",
        "DASHSCOPE_API_KEY",
        "V16_OPENAI_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    ):
        if not env.get(key) and openclaw_env.get(key):
            env[key] = openclaw_env[key]
    return env


def has_module(module_name):
    exe = PY312 if os.path.exists(PY312) else sys.executable
    cache_key = (os.path.abspath(exe), str(module_name))
    with _MODULE_AVAILABILITY_LOCK:
        cached = _MODULE_AVAILABILITY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    res = subprocess.run(
        [exe, "-c", f"import {module_name}"],
        capture_output=True,
        text=True,
    )
    available = res.returncode == 0
    with _MODULE_AVAILABILITY_LOCK:
        _MODULE_AVAILABILITY_CACHE[cache_key] = available
    return available


def _dedup_visual_filters(variant, preserve_geometry=False):
    """Return a mild, deterministic per-shot treatment for dedup pools."""
    variant = max(0, int(variant or 0)) % 8
    zoom_pixels = (8, 12, 16, 20, 10, 14, 18, 22)[variant]
    scaled_width = W + zoom_pixels
    scaled_height = H + int(round(zoom_pixels * H / W))
    scaled_width += scaled_width % 2
    scaled_height += scaled_height % 2
    x_fraction = (0.15, 0.50, 0.82, 0.34, 0.68, 0.22, 0.75, 0.45)[variant]
    y_fraction = (0.25, 0.62, 0.38, 0.76, 0.18, 0.52, 0.70, 0.34)[variant]
    contrast = (1.018, 1.026, 1.034, 1.022, 1.030, 1.020, 1.036, 1.024)[variant]
    saturation = (1.025, 1.040, 1.032, 1.048, 1.028, 1.044, 1.035, 1.050)[variant]
    brightness = (-0.002, 0.002, 0.004, 0.000, 0.003, -0.001, 0.005, 0.001)[variant]
    filters = []
    if not preserve_geometry:
        filters.extend(
            [
                f"scale={scaled_width}:{scaled_height}:flags=lanczos",
                (
                    f"crop={W}:{H}:"
                    f"x='(iw-ow)*{x_fraction:.3f}':y='(ih-oh)*{y_fraction:.3f}'"
                ),
            ]
        )
    filters.append(
        f"eq=contrast={contrast:.3f}:saturation={saturation:.3f}:"
        f"brightness={brightness:.3f}"
    )
    return filters


def _fast_dedup_style_filters(style):
    """Return no colour transform for the sole supported ``clean`` style."""
    normalized = str(style or "").strip().lower()
    if normalized != "clean":
        raise ValueError(
            "快速去重风格只允许 clean；黑白、线稿和漫画风格已永久停用。"
        )
    return []


def _probe_gpu_source_contract(file_path):
    """Return the conservative stream contract required by the CUDA graph."""

    def probe():
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                (
                    "stream=codec_name,width,height,pix_fmt,field_order,"
                    "sample_aspect_ratio:stream_tags=rotate:"
                    "stream_side_data=rotation"
                ),
                "-of",
                "json",
                file_path,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return {}
        streams = payload.get("streams") or []
        if result.returncode != 0 or not streams or not isinstance(streams[0], dict):
            return {}
        stream = streams[0]
        rotation = (stream.get("tags") or {}).get("rotate", 0)
        for side_data in stream.get("side_data_list") or []:
            if isinstance(side_data, dict) and side_data.get("rotation") is not None:
                rotation = side_data.get("rotation")
                break
        try:
            rotation = float(rotation or 0.0)
        except (TypeError, ValueError):
            rotation = 999.0
        try:
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
        except (TypeError, ValueError):
            width, height = 0, 0
        return {
            "codec_name": str(stream.get("codec_name") or "").lower(),
            "width": width,
            "height": height,
            "pix_fmt": str(stream.get("pix_fmt") or "").lower(),
            "field_order": str(stream.get("field_order") or "").lower(),
            "sample_aspect_ratio": str(
                stream.get("sample_aspect_ratio") or ""
            ).lower(),
            "rotation": rotation,
        }

    return _cached_media_probe("gpu-source-contract", file_path, probe)


def _gpu_normalize_eligibility(
    input_path,
    video_encoder_args,
    *,
    trim=None,
    trim_frames=None,
    micro_motion=False,
    dedup_variant=None,
    preserve_text_geometry=False,
    fast_dedup_style=None,
    force_keyframe_frames=None,
    output_frames=None,
    has_manual_cover=False,
):
    """Gate the CUDA graph so every unsupported job stays on the proven graph."""
    if _gpu_pipeline_policy() == "off":
        return False, "policy-off", None
    if sys.platform != "win32":
        return False, "non-windows", None
    if "h264_nvenc" not in video_encoder_args:
        return False, "nvenc-not-selected", None
    if _gpu_pipeline_disabled_reason():
        return False, "disabled-after-runtime-failure", None
    if any(
        (
            trim,
            trim_frames,
            micro_motion,
            dedup_variant is not None,
            preserve_text_geometry,
            fast_dedup_style,
            force_keyframe_frames,
            output_frames is not None,
            has_manual_cover,
        )
    ):
        return False, "software-filter-contract", None

    capable, capability_reason = _probe_gpu_pipeline_capabilities()
    if not capable:
        return False, f"ffmpeg-capability:{capability_reason}", None

    contract = _probe_gpu_source_contract(input_path)
    if not contract:
        return False, "source-probe-failed", None
    if contract.get("codec_name") != "h264":
        return False, "source-codec", contract
    if contract.get("pix_fmt") != "yuv420p":
        return False, "source-pixel-format", contract
    if contract.get("field_order") != "progressive":
        return False, "source-interlace", contract
    if contract.get("sample_aspect_ratio") != "1:1":
        return False, "source-sar", contract
    rotation = float(contract.get("rotation") or 0.0) % 360.0
    if min(rotation, 360.0 - rotation) > 0.01:
        return False, "source-rotation", contract
    if int(contract.get("width") or 0) < 2 or int(contract.get("height") or 0) < 2:
        return False, "source-dimensions", contract
    return True, "eligible", contract


def _centered_even_cuda_crop(source_width, source_height):
    """Match scale=increase,crop=center without downloading CUDA frames."""
    source_width = int(source_width)
    source_height = int(source_height)
    target_ratio = W / H
    source_ratio = source_width / source_height
    if source_ratio > target_ratio:
        crop_height = source_height - source_height % 2
        crop_width = int(round((crop_height * target_ratio) / 2.0) * 2)
    else:
        crop_width = source_width - source_width % 2
        crop_height = int(round((crop_width / target_ratio) / 2.0) * 2)
    crop_width = max(2, min(source_width - source_width % 2, crop_width))
    crop_height = max(2, min(source_height - source_height % 2, crop_height))
    x = max(0, ((source_width - crop_width) // 2) // 2 * 2)
    y = max(0, ((source_height - crop_height) // 2) // 2 * 2)
    return crop_width, crop_height, x, y


def _cuda_nvenc_args(video_encoder_args):
    """Keep the selected NVENC settings but accept GPU-resident NV12 frames."""
    converted = []
    index = 0
    while index < len(video_encoder_args):
        token = video_encoder_args[index]
        if token == "-pix_fmt" and index + 1 < len(video_encoder_args):
            index += 2
            continue
        converted.append(token)
        index += 1
    converted.extend(["-pix_fmt", "cuda"])
    return converted


def _build_gpu_normalize_command(
    input_path,
    output_path,
    *,
    video_encoder_args,
    source_contract,
    mute=False,
    source_has_audio=True,
    video_only=False,
):
    crop_width, crop_height, crop_x, crop_y = _centered_even_cuda_crop(
        source_contract["width"],
        source_contract["height"],
    )
    cuda_device_name = "v16cuda"
    command = [
        "ffmpeg",
        "-y",
        "-init_hw_device",
        f"cuda={cuda_device_name}:{NVIDIA_GPU_INDEX}",
        "-filter_hw_device",
        cuda_device_name,
        "-hwaccel",
        "cuda",
        "-hwaccel_device",
        cuda_device_name,
        "-hwaccel_output_format",
        "cuda",
        "-i",
        input_path,
    ]
    if not video_only and not source_has_audio:
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
            ]
        )
    video_filter = ",".join(
        [
            f"crop={crop_width}:{crop_height}:{crop_x}:{crop_y}",
            f"scale_cuda=w={W}:h={H}:format=nv12:interp_algo=lanczos",
            f"fps={FPS}",
            "setsar=1",
        ]
    )
    command.extend(["-vf", video_filter])
    if video_only:
        command.extend(["-an", "-map", "0:v:0"])
    elif source_has_audio:
        audio_filter = "volume=0" if mute else "volume=1.0"
        command.extend(
            [
                "-af",
                f"{audio_filter},aresample=44100",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
            ]
        )
    else:
        audio_filter = "volume=0" if mute else "volume=1.0"
        command.extend(
            [
                "-af",
                f"{audio_filter},aresample=44100",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-shortest",
            ]
        )
    command.extend(
        [
            *_cuda_nvenc_args(video_encoder_args),
            "-g",
            str(FPS),
            "-video_track_timescale",
            "90000",
        ]
    )
    if not video_only:
        command.extend(["-c:a", "aac", "-ac", "2", "-ar", "44100"])
    command.append(output_path)
    return command


def _run_gpu_normalize_command(command, output_path):
    """Run one isolated CUDA graph; never mutate the proven CPU graph."""
    started = time.perf_counter()
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        returncode = int(result.returncode)
        stderr = result.stderr
    except OSError as exc:
        returncode = -1
        stderr = str(exc)
    elapsed = time.perf_counter() - started
    fallback = returncode != 0 and _gpu_pipeline_policy() != "strict"
    _record_gpu_pipeline_attempt(elapsed, returncode, fallback=fallback)
    _record_encoder_attempt("h264_nvenc", elapsed, returncode)
    _print_encoder_step("h264_nvenc", elapsed, returncode)
    if returncode == 0:
        print(
            "   -> GPU 主通道：NVDEC 硬解码 + CUDA 缩放裁剪 + NVENC 编码，"
            "视频帧全程留在显存。"
        )
        return True

    try:
        os.remove(output_path)
    except FileNotFoundError:
        pass
    detail_lines = [
        line.strip() for line in str(stderr or "").splitlines() if line.strip()
    ]
    detail = (
        " | ".join(detail_lines[-3:])
        if detail_lines
        else f"FFmpeg 退出码 {returncode}"
    )[:500]
    if _gpu_pipeline_policy() == "strict":
        raise RuntimeError(
            "CUDA 严格模式执行失败，已拒绝回退 CPU 滤镜图：" + detail
        )

    _disable_gpu_pipeline_for_current_run(detail)
    print(
        "[WARN] CUDA 全显存通道未通过首个真实素材验收，"
        "本次任务其余步骤已自动改用原 CPU 滤镜图；NVENC 编码仍保留。"
    )
    print(
        "V16_GPU_PIPELINE_FALLBACK "
        + json.dumps(
            {
                "from": "nvdec-cuda-nvenc",
                "to": "cpu-filter-nvenc",
                "stage": "normalize-video",
                "reason": "runtime-canary",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return False


def normalize_video(
    input_path,
    output_path,
    mute=False,
    trim=None,
    trim_frames=None,
    micro_motion=False,
    dedup_variant=None,
    preserve_text_geometry=False,
    fast_dedup_style=None,
    force_keyframe_frames=None,
    output_frames=None,
    video_only=False,
    manual_cover_vf=None,
):
    if trim and trim_frames:
        raise ValueError("trim and trim_frames cannot be used together")
    if micro_motion and not trim_frames:
        raise ValueError("micro_motion requires an exact trim_frames range")
    if output_frames is not None and int(output_frames) <= 0:
        raise ValueError("output_frames must be a positive integer")
    keyframe_frames = sorted(
        {
            int(value)
            for value in (force_keyframe_frames or [])
            if int(value) > 0
        }
    )

    vf_parts = [
        f"scale={W}:{H}:force_original_aspect_ratio=increase",
        f"crop={W}:{H}",
        f"fps={FPS}",
    ]
    af = "volume=0" if mute else "volume=1.0"
    af += ",aresample=44100"
    video_encoder_args = _video_encoder_args(23, "veryfast")
    # Build the long-proven software-filter command independently.  The CUDA
    # candidate is assembled later and can never partially mutate this graph.
    cmd = ["ffmpeg", "-y"]
    if trim:
        start, dur = trim
        cmd.extend(["-ss", f"{start:.3f}", "-t", f"{dur:.3f}"])
    elif trim_frames:
        start_frame, end_frame = (int(value) for value in trim_frames)
        if start_frame < 0 or end_frame <= start_frame:
            raise ValueError("trim_frames must be an increasing non-negative range")
        start = start_frame / FPS
        dur = (end_frame - start_frame) / FPS
        vf_parts.extend(
            [
                f"trim=start_frame={start_frame}:end_frame={end_frame}",
                "setpts=PTS-STARTPTS",
            ]
        )
    if manual_cover_vf:
        vf_parts.extend(manual_cover_vf)
    if trim_frames and micro_motion:
        frame_count = end_frame - start_frame
        vf_parts.extend(
            [
                (
                    "zoompan="
                    f"z='min({1.0 + MICRO_MOTION_ZOOM:.6f},"
                    f"1+{MICRO_MOTION_ZOOM:.6f}*on/{max(1, frame_count - 1)})':"
                    f"x='(iw-iw/zoom)*on/{max(1, frame_count - 1)}':"
                    "y='ih/2-(ih/zoom/2)':"
                    f"d=1:s={W}x{H}:fps={FPS}"
                ),
                "setpts=PTS-STARTPTS",
            ]
        )
    if dedup_variant is not None:
        # Apply the split-shot dedup treatment in this existing encode pass;
        # no extra render, no mirror, and no change to output size or FPS.
        vf_parts.extend(
            _dedup_visual_filters(
                dedup_variant,
                preserve_geometry=preserve_text_geometry,
            )
        )
    if fast_dedup_style:
        vf_parts.extend(_fast_dedup_style_filters(fast_dedup_style))
    # VideoToolbox can otherwise preserve tiny, source-dependent SAR rounding
    # differences that make FFmpeg's concat filter reject equal-sized clips.
    vf_parts.append("setsar=1")
    cmd.extend(["-i", input_path])
    source_has_audio = False if video_only else has_audio_stream(input_path)
    if not video_only and not source_has_audio:
        cmd.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])
    vf_parts.append("format=yuv420p")
    cmd.extend(["-vf", ",".join(vf_parts)])
    if trim_frames and not video_only:
        if source_has_audio:
            af += f",atrim=start={start:.6f}:end={start + dur:.6f}"
        else:
            af += f",atrim=duration={dur:.6f}"
        af += ",asetpts=PTS-STARTPTS"
    if video_only:
        cmd.extend(["-an", "-map", "0:v:0"])
    elif source_has_audio:
        cmd.extend(["-af", af, "-map", "0:v:0", "-map", "0:a:0"])
    else:
        cmd.extend(["-af", af, "-map", "0:v:0", "-map", "1:a:0", "-shortest"])
    cmd.extend(
        [
            *video_encoder_args,
            "-g",
            str(FPS),
            "-video_track_timescale",
            "90000",
        ]
    )
    if not video_only:
        cmd.extend(["-c:a", "aac", "-ac", "2", "-ar", "44100"])
    if keyframe_frames:
        expression = "+".join(
            f"eq(n,{frame})" for frame in keyframe_frames
        )
        cmd.extend(
            [
                "-force_key_frames",
                f"expr:{expression}",
            ]
        )
        if "h264_nvenc" in video_encoder_args:
            cmd.extend(["-forced-idr", "1"])
    if output_frames is not None:
        cmd.extend(["-frames:v", str(int(output_frames))])
    cmd.append(output_path)
    gpu_eligible, gpu_reason, source_contract = _gpu_normalize_eligibility(
        input_path,
        video_encoder_args,
        trim=trim,
        trim_frames=trim_frames,
        micro_motion=micro_motion,
        dedup_variant=dedup_variant,
        preserve_text_geometry=preserve_text_geometry,
        fast_dedup_style=fast_dedup_style,
        force_keyframe_frames=keyframe_frames,
        output_frames=output_frames,
        has_manual_cover=bool(manual_cover_vf),
    )
    if not gpu_eligible and _gpu_pipeline_policy() == "strict":
        raise RuntimeError(
            "CUDA 严格模式要求全显存通道，但当前步骤不满足资格："
            + str(gpu_reason)
        )
    gpu_pipeline_used = False
    stage_started = time.perf_counter()
    try:
        if gpu_eligible:
            gpu_command = _build_gpu_normalize_command(
                input_path,
                output_path,
                video_encoder_args=video_encoder_args,
                source_contract=source_contract,
                mute=mute,
                source_has_audio=source_has_audio,
                video_only=video_only,
            )
            gpu_pipeline_used = _run_gpu_normalize_command(
                gpu_command,
                output_path,
            )
        if not gpu_pipeline_used:
            run_ffmpeg(cmd)
    finally:
        _record_perf_stage(
            "normalize-video",
            time.perf_counter() - stage_started,
            source=os.path.basename(input_path),
            output=os.path.basename(output_path),
            frame_count=(
                int(output_frames)
                if output_frames is not None
                else int(trim_frames[1] - trim_frames[0])
                if trim_frames
                else None
            ),
            gpu_pipeline=gpu_pipeline_used,
            gpu_skip_reason=None if gpu_eligible else gpu_reason,
        )


def _needs_micro_motion(path):
    """Detect a sustained low-motion hold without treating it as a duplicated freeze."""
    if not AUTO_MICRO_MOTION or not os.path.isfile(path) or not has_module("cv2"):
        return False
    code = r"""
import cv2, sys
path=sys.argv[1]
window=max(2,int(sys.argv[2]))
threshold=float(sys.argv[3])
cap=cv2.VideoCapture(path)
previous=None
diffs=[]
while True:
    ok,frame=cap.read()
    if not ok: break
    gray=cv2.cvtColor(cv2.resize(frame,(90,160),interpolation=cv2.INTER_AREA),cv2.COLOR_BGR2GRAY)
    if previous is not None:
        diffs.append(float(cv2.absdiff(gray,previous).mean()))
    previous=gray
cap.release()
if len(diffs)<window:
    print('NO'); raise SystemExit(0)
rolling=sum(diffs[:window])
minimum=rolling/window
for index in range(window,len(diffs)):
    rolling+=diffs[index]-diffs[index-window]
    minimum=min(minimum,rolling/window)
print(('YES' if minimum<threshold else 'NO')+f' {minimum:.6f}')
"""
    exe = PY312 if os.path.exists(PY312) else sys.executable
    result = subprocess.run(
        [
            exe,
            "-c",
            code,
            path,
            str(max(2, int(round(MICRO_MOTION_WINDOW_SECONDS * FPS)))),
            str(MICRO_MOTION_MAX_FRAME_DELTA),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    return result.returncode == 0 and result.stdout.strip().startswith("YES")


def _local_analysis_cache_path(kind, input_path, start, duration):
    """Optional batch-local metadata cache, exclusively for trusted slot 2.

    Kept below the existing task cache so normal task cleanup removes it.
    Public workers do no cache I/O. File identity and exact query range are
    part of the key; no scene/speech thresholds or output settings change.
    """
    if (
        not _local_speed_profile_enabled()
        or not FIXED_STAGE_CACHE_DIR
        or kind not in {"scene-0.16", "silence--33db-0.20"}
    ):
        return None
    try:
        base = os.path.realpath(os.path.abspath(BASE_DIR))
        root = os.path.abspath(FIXED_STAGE_CACHE_DIR)
        if os.path.commonpath([base, root]) != base:
            return None
        # The existing core cache may have a per-core-window subdirectory.
        if os.path.basename(root) != ".fixed-stage-cache":
            root = os.path.dirname(root)
        if os.path.basename(root) != ".fixed-stage-cache":
            return None
        cursor = root
        while cursor != base:
            if _is_path_link(cursor):
                return None
            parent = os.path.dirname(cursor)
            if parent == cursor:
                return None
            cursor = parent
        if _is_path_link(base):
            return None
        source = os.path.realpath(os.path.abspath(input_path))
        if os.path.commonpath([base, source]) != base or not os.path.isfile(source):
            return None
        info = os.stat(source)
        key = hashlib.sha256(json.dumps(
            ["local-8021-analysis-v1", kind, source, info.st_dev, info.st_ino,
             info.st_size, info.st_mtime_ns, info.st_ctime_ns, start, duration],
            ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        cache = os.path.join(root, ".local-analysis-v1")
        if _is_path_link(cache):
            return None
        path = os.path.join(cache, key + ".json")
        return None if _is_path_link(path) else path
    except (OSError, ValueError, TypeError):
        return None


def _load_local_analysis_cache(path):
    if not path:
        return None
    try:
        if os.path.getsize(path) > 65536:
            return None
        with open(path, "r", encoding="utf-8") as handle:
            item = json.load(handle)
        if not isinstance(item, dict) or item.get("key") != os.path.basename(path)[:-5]:
            return None
        points = item.get("points")
        if not isinstance(points, list) or len(points) > 4096:
            return None
        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in points):
            return None
        encoded = json.dumps(points, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if item.get("sha256") != hashlib.sha256(encoded).hexdigest():
            return None
        print("V16_LOCAL_ANALYSIS_CACHE hit " + os.path.basename(path)[:12])
        return list(points)
    except (OSError, ValueError, TypeError, OverflowError):
        return None


def _store_local_analysis_cache(path, points):
    if not path or len(points) > 4096:
        return
    pending = None
    try:
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        if _is_path_link(directory) or _is_path_link(path):
            return
        # At most 256 small JSON entries per task; never cache video bytes.
        with os.scandir(directory) as entries:
            if sum(1 for _ in entries) >= 256:
                return
        encoded = json.dumps(points, separators=(",", ":"), allow_nan=False).encode("utf-8")
        payload = json.dumps({"key": os.path.basename(path)[:-5], "points": points,
                              "sha256": hashlib.sha256(encoded).hexdigest()},
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(payload) > 65536:
            return
        descriptor, pending = tempfile.mkstemp(prefix=".pending-", suffix=".json", dir=directory)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        os.replace(pending, path)
        pending = None
    except (OSError, ValueError, TypeError):
        pass  # Optional optimization must never stop production.
    finally:
        if pending:
            try:
                os.unlink(pending)
            except OSError:
                pass


def extract_scenes(input_path, start_seconds=0.0, duration_seconds=None):
    """Return scene boundaries from one bounded source window."""
    start = max(0.0, float(start_seconds or 0.0))
    duration = (
        None
        if duration_seconds is None
        else max(0.05, float(duration_seconds))
    )
    cache_path = _local_analysis_cache_path("scene-0.16", input_path, start, duration)
    cached = _load_local_analysis_cache(cache_path)
    if cached is not None:
        return cached
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info"]
    if start > 0.0:
        cmd.extend(["-ss", f"{start:.3f}"])
    cmd.extend(["-i", input_path])
    if duration is not None:
        cmd.extend(["-t", f"{duration:.3f}"])
    cmd.extend(
        [
            "-vf",
            "setpts=PTS-STARTPTS,select='gt(scene,0.16)',showinfo",
            "-an",
            "-f",
            "null",
            "-",
        ]
    )
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=MEDIA_ANALYSIS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[WARN] 镜头边界分析已跳过：{exc}")
        return []
    scenes = []
    for line in res.stderr.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            point = start + float(m.group(1))
            if duration is None or point <= start + duration + 0.05:
                scenes.append(point)
    if res.returncode == 0 and cache_path == _local_analysis_cache_path("scene-0.16", input_path, start, duration):
        _store_local_analysis_cache(cache_path, scenes)
    return scenes


def extract_speech_pause_points(
    input_path,
    start_seconds=0.0,
    duration_seconds=None,
):
    """Return pause midpoints from one bounded source window."""
    if not has_audio_stream(input_path):
        return []
    start = max(0.0, float(start_seconds or 0.0))
    duration = (
        None
        if duration_seconds is None
        else max(0.05, float(duration_seconds))
    )
    cache_path = _local_analysis_cache_path("silence--33db-0.20", input_path, start, duration)
    cached = _load_local_analysis_cache(cache_path)
    if cached is not None:
        return cached
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info"]
    if start > 0.0:
        command.extend(["-ss", f"{start:.3f}"])
    command.extend(["-i", input_path])
    if duration is not None:
        command.extend(["-t", f"{duration:.3f}"])
    command.extend(
        [
            "-af",
            "asetpts=PTS-STARTPTS,silencedetect=noise=-33dB:d=0.20",
            "-vn",
            "-f",
            "null",
            "-",
        ]
    )
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=MEDIA_ANALYSIS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[WARN] 语音停顿分析已跳过：{exc}")
        return []
    starts = []
    points = []
    for line in result.stderr.splitlines():
        start_match = re.search(r"silence_start:\s*([\d.]+)", line)
        if start_match:
            starts.append(start + float(start_match.group(1)))
        end_match = re.search(r"silence_end:\s*([\d.]+)", line)
        if end_match:
            end = start + float(end_match.group(1))
            silence_start = (
                starts.pop(0) if starts else max(start, end - 0.25)
            )
            point = (silence_start + end) / 2.0
            if duration is None or point <= start + duration + 0.05:
                points.append(point)
    if result.returncode == 0 and cache_path == _local_analysis_cache_path("silence--33db-0.20", input_path, start, duration):
        _store_local_analysis_cache(cache_path, points)
    return points


def plan_core_cut_points(input_path, total_duration=None):
    """Plan only a few natural core cuts while preserving the complete source."""
    total = float(total_duration if total_duration is not None else get_duration(input_path))
    if total <= 45.0:
        return []

    # The requirement says "very few simple cuts".  Use no more than four
    # ordered sections even for a long core clip, rather than the 2–4 second
    # fragmentation used by the dedup pools.
    segment_count = min(4, max(2, int((total + 44.999) // 45.0)))
    scene_points = [point for point in extract_scenes(input_path) if 0 < point < total]
    pause_points = [
        point for point in extract_speech_pause_points(input_path) if 0 < point < total
    ]
    ideal_section = total / segment_count
    minimum_section = min(20.0, ideal_section * 0.55)
    search_radius = min(10.0, ideal_section * 0.30)
    cuts = []
    previous = 0.0
    for section_index in range(1, segment_count):
        target = ideal_section * section_index
        remaining_sections = segment_count - section_index
        lower = max(previous + minimum_section, target - search_radius)
        upper = min(total - minimum_section * remaining_sections, target + search_radius)
        if upper < lower:
            lower = upper = max(previous + 1.0 / FPS, min(total - 1.0 / FPS, target))
        scene_candidates = [point for point in scene_points if lower <= point <= upper]
        pause_candidates = [point for point in pause_points if lower <= point <= upper]
        # A visible shot boundary is best for the core picture; a speech pause
        # is the fallback so a spoken sentence is not cut in the middle.
        if scene_candidates:
            chosen = min(scene_candidates, key=lambda point: abs(point - target))
        elif pause_candidates:
            chosen = min(pause_candidates, key=lambda point: abs(point - target))
        else:
            chosen = target
        chosen = round(chosen * FPS) / FPS
        if chosen > previous and chosen < total:
            cuts.append(chosen)
            previous = chosen
    return cuts


def split_core_video(input_path, prefix):
    """Create a few ordered, lossless core segments without dropping content."""
    total = get_duration(input_path)
    cut_points = plan_core_cut_points(input_path, total)
    if not cut_points:
        return [input_path]

    output_pattern = os.path.join(TMP_DIR, f"{prefix}_part_%03d.mp4")
    try:
        run_ffmpeg(
            [
                "ffmpeg", "-y", "-i", input_path,
                "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                "-f", "segment", "-segment_format", "mp4",
                "-segment_times", ",".join(f"{point:.6f}" for point in cut_points),
                "-segment_time_delta", f"{0.5 / FPS:.6f}",
                "-reset_timestamps", "1", "-avoid_negative_ts", "make_zero",
                output_pattern,
            ]
        )
    except RuntimeError as exc:
        print(f"[WARN] 核心素材智能分割失败，已回退完整播放: {exc}")
        return [input_path]

    expected_prefix = f"{prefix}_part_"
    parts = [
        os.path.join(TMP_DIR, name)
        for name in sorted(os.listdir(TMP_DIR))
        if name.startswith(expected_prefix) and name.lower().endswith(".mp4")
    ]
    if len(parts) != len(cut_points) + 1:
        print(
            f"[WARN] 核心素材分割段数异常（预期 {len(cut_points) + 1}，"
            f"实际 {len(parts)}），已回退完整播放。"
        )
        return [input_path]
    print(
        f"   -> 核心素材已在 {len(cut_points)} 个自然镜头/语音边界少量分割，"
        f"共 {len(parts)} 段；无重编码，"
        f"{'随后按裂变种子乱序' if FISSION_SHUFFLE_ALL and FISSION_VARIANT_SEED else '保持完整原序'}。"
    )
    return parts


def plan_dedup1_narrative_cut_points(input_path, total_duration=None):
    """Choose 2-4 story blocks using real ASR sentence ends whenever possible."""
    total = float(total_duration if total_duration is not None else get_duration(input_path))
    if total <= 8.0:
        return []
    source_has_audio = has_audio_stream(input_path)
    boundary_points = []
    if source_has_audio:
        subtitle_path = transcribe_video_to_srt(
            input_path, f"{os.path.basename(input_path)}去重1语句边界"
        )
        if subtitle_path:
            try:
                with open(subtitle_path, "r", encoding="utf-8-sig", errors="ignore") as handle:
                    content = handle.read()
                for match in re.finditer(r"-->\s*(\d+:\d+:\d+[,.]\d+)", content):
                    point = _srt_time_seconds(match.group(1))
                    if 0.0 < point < total - 0.25:
                        boundary_points.append(point)
            except OSError:
                boundary_points = []
        if not boundary_points:
            boundary_points = [
                point for point in extract_speech_pause_points(input_path)
                if 0.0 < point < total
            ]
    else:
        boundary_points = [
            point for point in extract_scenes(input_path) if 0.0 < point < total
        ]

    if not boundary_points:
        return []

    segment_count = min(5, max(2, int(math.ceil(total / 5.0))))
    min_piece = min(3.0, total / (segment_count * 1.5))
    cuts = []
    last = 0.0
    for index in range(1, segment_count):
        target = total * index / segment_count
        remaining_pieces = segment_count - index
        lower = last + min_piece
        upper = total - remaining_pieces * min_piece
        candidates = [point for point in boundary_points if lower <= point <= upper]
        if not candidates:
            continue
        chosen = round(min(candidates, key=lambda point: abs(point - target)) * FPS) / FPS
        if chosen <= last or total - chosen < min_piece:
            continue
        cuts.append(chosen)
        last = chosen
    return cuts


def _get_video_frame_count(file_path):
    def probe():
        try:
            packet_probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-count_packets",
                    "-show_entries",
                    "stream=nb_frames,nb_read_packets",
                    "-of",
                    "json",
                    file_path,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=30,
            )
            payload = json.loads(packet_probe.stdout or "{}")
            streams = payload.get("streams") or []
            stream = streams[0] if streams and isinstance(streams[0], dict) else {}
            for field in ("nb_frames", "nb_read_packets"):
                value = stream.get(field)
                if packet_probe.returncode == 0 and value not in {None, "", "N/A"}:
                    count = int(value)
                    if count > 0:
                        return count
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-count_frames",
                    "-show_entries",
                    "stream=nb_read_frames",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    file_path,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=60,
            )
            return int((result.stdout or "").strip().splitlines()[0])
        except (
            OSError,
            ValueError,
            IndexError,
            json.JSONDecodeError,
            subprocess.TimeoutExpired,
        ):
            return 0

    return _cached_media_probe("video-frame-count", file_path, probe)


def _remux_stable_chunk_audio(
    video_path,
    source_path,
    output_path,
    *,
    start,
    duration,
    mute,
    source_has_audio,
):
    command = ["ffmpeg", "-y", "-i", video_path]
    if source_has_audio:
        command.extend(["-i", source_path])
        audio_filter = "volume=0" if mute else "volume=1.0"
        audio_filter += (
            ",aresample=44100"
            f",atrim=start={start:.6f}:end={start + duration:.6f}"
            ",asetpts=PTS-STARTPTS"
        )
    else:
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
            ]
        )
        audio_filter = (
            f"volume=0,aresample=44100,atrim=duration={duration:.6f},"
            "asetpts=PTS-STARTPTS"
        )
    command.extend(
        [
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-af",
            audio_filter,
            "-c:a",
            "aac",
            "-ac",
            "2",
            "-ar",
            "44100",
        ]
    )
    if not source_has_audio:
        command.append("-shortest")
    command.extend(
        [
            "-video_track_timescale",
            "90000",
            "-movflags",
            "+faststart",
            output_path,
        ]
    )
    run_ffmpeg(command)


def _stable_single_pass_has_expected_benefit(render_jobs, render_workers=None):
    """Avoid the master/remux overhead when a short split has little reuse."""
    if len(render_jobs) < 4:
        return False
    ranges = [(int(job[1]), int(job[2])) for job in render_jobs]
    if not ranges or ranges[0][0] != 0:
        return False
    total_frames = ranges[-1][1]
    if total_frames < max(1, int(FPS * 8)):
        return False
    if any(
        left_end != right_start
        for (_left_start, left_end), (right_start, _right_end)
        in zip(ranges, ranges[1:])
    ):
        return False
    # Each old trim_frames command decoded from frame zero up to its own end.
    # The new route decodes once to total_frames, then stream-copies video.
    legacy_decode_ratio = sum(end for _start, end in ranges) / total_frames
    redundant_frames = sum(end for _start, end in ranges[:-1])
    effective_workers = max(
        1,
        int(RENDER_WORKERS if render_workers is None else render_workers),
    )
    effective_redundant_frames = redundant_frames / effective_workers
    minimum_redundant_frames = FPS * (60 + 5 * len(ranges))
    return (
        legacy_decode_ratio >= 2.25
        and effective_redundant_frames >= minimum_redundant_frames
    )


def _render_stable_dedup_chunks_single_pass(
    input_path,
    prefix,
    render_jobs,
    *,
    mute=False,
):
    """Normalize one stable source once, then losslessly split at forced IDRs.

    Stable dedup modes use one visual treatment for every chunk from a source.
    The retired path reopened and decoded that source once per chunk.  This
    path performs the expensive scale/fps/effect encode once, forces exact
    frame-quantized keyframes at the planned boundaries, and uses the segment
    muxer only to remux the resulting chunks.
    """
    if len(render_jobs) <= 1:
        return False
    variants = {int(job[6]) for job in render_jobs}
    if len(variants) != 1:
        return False
    expected_ranges = [
        (int(job[1]), int(job[2]))
        for job in render_jobs
    ]
    if not expected_ranges or expected_ranges[0][0] != 0:
        return False
    if any(
        left_end != right_start
        for (_left_start, left_end), (right_start, _right_end)
        in zip(expected_ranges, expected_ranges[1:])
    ):
        return False

    os.makedirs(TMP_DIR, exist_ok=True)
    safe_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(prefix))[:48] or "dedup"
    work_dir = tempfile.mkdtemp(
        prefix=f"{safe_prefix}_single_pass_",
        dir=TMP_DIR,
    )
    normalized_path = os.path.join(work_dir, "normalized-video.mp4")
    cut_frames = [end_frame for _start_frame, end_frame in expected_ranges[:-1]]
    video_pattern = os.path.join(work_dir, "video-part-%03d.mp4")
    video_parts = [
        os.path.join(work_dir, f"video-part-{index:03d}.mp4")
        for index in range(len(render_jobs))
    ]
    expected_outputs = [job[5] for job in render_jobs]
    pending_outputs = [
        os.path.join(work_dir, f"validated-output-{index:03d}.mp4")
        for index in range(len(render_jobs))
    ]
    published_outputs = []

    stage_started = time.perf_counter()
    try:
        normalize_video(
            input_path,
            normalized_path,
            mute=mute,
            trim_frames=(0, expected_ranges[-1][1]),
            dedup_variant=next(iter(variants)),
            preserve_text_geometry=bool(mute or HARDCUT_MODE),
            force_keyframe_frames=cut_frames,
            video_only=True,
            manual_cover_vf=_subtitle_mask_vf_for_source(input_path),
        )
        run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-i",
                normalized_path,
                "-map",
                "0:v:0",
                "-c",
                "copy",
                "-f",
                "segment",
                "-segment_format",
                "mp4",
                "-segment_frames",
                ",".join(str(frame) for frame in cut_frames),
                "-reset_timestamps",
                "1",
                "-avoid_negative_ts",
                "make_zero",
                video_pattern,
            ]
        )
        missing = [path for path in video_parts if not os.path.isfile(path)]
        if missing:
            raise RuntimeError(
                f"single-pass segment count mismatch: missing {len(missing)} chunks"
            )
        frame_counts = [_get_video_frame_count(path) for path in video_parts]
        expected_counts = [end - start for start, end in expected_ranges]
        if frame_counts != expected_counts:
            raise RuntimeError(
                "single-pass frame validation failed: "
                f"expected={expected_counts} actual={frame_counts}"
            )
        source_has_audio = has_audio_stream(input_path)
        for job, video_path, pending_output in zip(
            render_jobs,
            video_parts,
            pending_outputs,
        ):
            _idx, _start_frame, _end_frame, start, duration, _output_path, _variant = job
            _remux_stable_chunk_audio(
                video_path,
                input_path,
                pending_output,
                start=start,
                duration=duration,
                mute=mute,
                source_has_audio=source_has_audio,
            )
        invalid_outputs = [
            output_path
            for output_path, expected_frames in zip(pending_outputs, expected_counts)
            if not os.path.isfile(output_path)
            or _get_video_frame_count(output_path) != expected_frames
            or not has_audio_stream(output_path)
        ]
        if invalid_outputs:
            raise RuntimeError(
                f"single-pass A/V validation failed for {len(invalid_outputs)} chunks"
            )
        for pending_output, expected_output in zip(
            pending_outputs,
            expected_outputs,
        ):
            os.replace(pending_output, expected_output)
            published_outputs.append(expected_output)
        print(
            f"   -> {prefix} 单次规范化收益路径：CPU 兼容滤镜 + 当前编码器"
            "只处理源视频 1 次，"
            f"随后按精确帧边界无损切为 {len(expected_outputs)} 段。"
        )
        return True
    except Exception:
        for path in published_outputs:
            try:
                os.remove(path)
            except OSError:
                pass
        raise
    finally:
        _record_perf_stage(
            "stable-dedup-single-pass",
            time.perf_counter() - stage_started,
            source=os.path.basename(input_path),
            chunk_count=len(render_jobs),
            output_frames=expected_ranges[-1][1],
        )
        try:
            shutil.rmtree(
                _validated_tmp_subdirectory(work_dir),
                ignore_errors=True,
            )
        except (OSError, RuntimeError):
            # Antivirus/indexer handles on Windows must not turn best-effort
            # cleanup into a failed render or suppress the legacy fallback.
            pass


def split_dedup_video(
    input_path,
    prefix,
    mute=False,
    coherent_audio=False,
    render_workers=None,
):
    effective_render_workers = max(
        1,
        min(
            RENDER_WORKERS,
            int(RENDER_WORKERS if render_workers is None else render_workers),
        ),
    )
    total = get_duration(input_path)
    if HARDCUT_MODE and FIXED_SLICE_SECONDS > 0:
        mute = bool(mute or MUTE_ORIGINAL_AUDIO)
        coherent_audio = False
        total_frames = max(1, int(round(total * FPS)))
        step_frames = max(1, int(round(FIXED_SLICE_SECONDS * FPS)))
        min_tail_frames = max(1, int(round(0.5 * FPS)))
        if total_frames <= step_frames + min_tail_frames:
            cut_frames = [0, total_frames]
        else:
            cut_frames = list(range(0, total_frames + 1, step_frames))
            remainder = total_frames - cut_frames[-1]
            if remainder >= min_tail_frames:
                cut_frames.append(total_frames)
            if len(cut_frames) < 2:
                cut_frames = [0, total_frames]
    else:
        if coherent_audio:
            cuts = [0.0, *plan_dedup1_narrative_cut_points(input_path, total), total]
        else:
            scene_points = [p for p in extract_scenes(input_path) if 0 < p < total]
            pause_points = [p for p in extract_speech_pause_points(input_path) if 0 < p < total]
            cuts = [0.0]
            last = 0.0
            while last < total - 0.5:
                target = last + (DEDUP_MIN_CHUNK + DEDUP_MAX_CHUNK) / 2.0
                lower = last + DEDUP_MIN_CHUNK
                upper = last + DEDUP_MAX_CHUNK
                pause_candidates = [p for p in pause_points if lower <= p <= upper]
                scene_candidates = [p for p in scene_points if lower <= p <= upper]
                # A detected speech pause is the safest boundary. Scene changes are
                # second choice; the deterministic midpoint is the final fallback.
                if pause_candidates:
                    next_cut = min(pause_candidates, key=lambda p: abs(p - target))
                elif scene_candidates:
                    next_cut = min(scene_candidates, key=lambda p: abs(p - target))
                else:
                    next_cut = target
                if next_cut >= total:
                    break
                cuts.append(next_cut)
                last = next_cut
            cuts.append(total)

        # Quantize the complete cut map once, not each segment independently.
        # This keeps all chunk frame counts equal to the source frame count.
        total_frames = max(1, int(round(total * FPS)))
        cut_frames = [0]
        for cut_time in cuts[1:-1]:
            frame = int(round(cut_time * FPS))
            frame = max(cut_frames[-1] + 1, min(total_frames - 1, frame))
            if frame > cut_frames[-1]:
                cut_frames.append(frame)
        cut_frames.append(total_frames)

        # Merge a sub-second tail into the previous chunk instead of dropping it.
        if len(cut_frames) >= 3 and total_frames - cut_frames[-2] < FPS:
            cut_frames.pop(-2)

    render_jobs = []
    stable_source_geometry = coherent_audio or mute or HARDCUT_MODE
    for idx in range(len(cut_frames) - 1):
        start_frame = cut_frames[idx]
        end_frame = cut_frames[idx + 1]
        start = start_frame / FPS
        dur = (end_frame - start_frame) / FPS
        if end_frame <= start_frame:
            continue
        out = os.path.join(TMP_DIR, f"{prefix}_{idx:03d}.mp4")
        variant_inputs = (
            [input_path, prefix]
            if stable_source_geometry
            else [input_path, prefix, str(idx)]
        )
        variant_label = (
            "dedup-source-stable-filter"
            if stable_source_geometry
            else "dedup-shot-filter"
        )
        dedup_variant = _stable_seed(variant_inputs, variant_label) % 8
        render_jobs.append(
            (idx, start_frame, end_frame, start, dur, out, dedup_variant)
        )

    source_cover_vf = _subtitle_mask_vf_for_source(input_path)

    def render_chunk(job):
        idx, start_frame, end_frame, start, dur, out, dedup_variant = job
        normalize_video(
            input_path,
            out,
            mute=mute,
            trim_frames=(start_frame, end_frame),
            dedup_variant=dedup_variant,
            preserve_text_geometry=bool(mute or HARDCUT_MODE),
            manual_cover_vf=source_cover_vf,
        )
        return job

    rendered_jobs = None
    if (
        STABLE_SPLIT_SINGLE_PASS
        and stable_source_geometry
        and _stable_single_pass_has_expected_benefit(
            render_jobs,
            render_workers=effective_render_workers,
        )
    ):
        try:
            if _render_stable_dedup_chunks_single_pass(
                input_path,
                prefix,
                render_jobs,
                mute=mute,
            ):
                rendered_jobs = list(render_jobs)
        except (RuntimeError, OSError, ValueError) as exc:
            print(
                f"[WARN] {prefix} 单次规范化/无损分段失败，"
                f"已回退逐段兼容渲染: {exc}"
            )
            for job in render_jobs:
                output_path = job[5]
                try:
                    if os.path.isfile(output_path):
                        os.remove(output_path)
                except OSError:
                    pass

    if rendered_jobs is not None:
        pass
    elif len(render_jobs) > 1 and effective_render_workers > 1:
        print(f"   -> {prefix} 启用 {effective_render_workers} 路并行镜头渲染。")
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(effective_render_workers, len(render_jobs))
        ) as executor:
            rendered_jobs = list(executor.map(render_chunk, render_jobs))
    else:
        rendered_jobs = [render_chunk(job) for job in render_jobs]

    if rendered_jobs:
        if mute:
            print(
                f"   -> {prefix} 已启用原声静音与几何稳定：共切出 {len(rendered_jobs)} 段。"
            )
        elif HARDCUT_MODE:
            print(
                f"   -> {prefix} 已启用固定秒数硬切并保留原声：共切出 {len(rendered_jobs)} 段。"
            )
        elif coherent_audio:
            print(
                f"   -> {prefix} 已启用字幕稳定保护："
                "保持原画面几何位置，不做推拉、横移或逐段变焦。"
            )
    chunks = []
    for job in rendered_jobs:
        idx, start_frame, end_frame, start, dur, out, dedup_variant = job
        score = 1.0 if HARDCUT_MODE else content_score(out)
        chunks.append(
            Clip(
                out,
                prefix,
                get_duration(out),
                score,
                os.path.basename(input_path),
                start,
                idx,
                input_path,
            )
        )
    return chunks


def split_dedup_sources_parallel(
    input_paths,
    prefix,
    mute=False,
    coherent_audio=False,
    stage_label="素材池",
):
    """Render independent sources in two bounded CPU-filter/NVENC lanes."""
    indexed_paths = list(enumerate(input_paths))
    if not indexed_paths:
        return []

    def render_source(item, inner_workers):
        source_index, input_path = item
        return split_dedup_video(
            input_path,
            f"{prefix}_{source_index}",
            mute=mute,
            coherent_audio=coherent_audio,
            render_workers=inner_workers,
        )

    source_workers = min(RENDER_WORKERS, len(indexed_paths))
    if source_workers <= 1:
        grouped_chunks = [
            render_source(item, RENDER_WORKERS) for item in indexed_paths
        ]
    else:
        print(
            f"   -> {stage_label}启用 {source_workers} 路源级流水："
            "CPU 分析/软件滤镜与 GPU NVENC 编码并行。"
        )
        # Exactly one renderer per source prevents an outer 2 x inner 2 fanout
        # from oversubscribing the 28 CPU threads and the single NVENC engine.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=source_workers
        ) as executor:
            grouped_chunks = list(
                executor.map(
                    lambda item: render_source(item, 1),
                    indexed_paths,
                )
            )
    # executor.map preserves source order, keeping seeds and downstream
    # similarity decisions deterministic even though rendering is concurrent.
    return [clip for chunks in grouped_chunks for clip in chunks]


def split_quality_video(input_path, prefix):
    """Lightly split quality footage at natural boundaries into 8–15 second shots."""
    total = get_duration(input_path)
    if total <= 0.05:
        return []
    scene_points = [point for point in extract_scenes(input_path) if 0 < point < total]
    pause_points = [point for point in extract_speech_pause_points(input_path) if 0 < point < total]
    cuts = [0.0]
    last = 0.0
    while last < total - 1.0:
        lower = last + 8.0
        target = last + 11.5
        upper = last + 15.0
        candidates = [point for point in scene_points if lower <= point <= upper]
        if not candidates:
            candidates = [point for point in pause_points if lower <= point <= upper]
        next_cut = min(candidates, key=lambda point: abs(point - target)) if candidates else target
        if next_cut >= total - 1.0:
            break
        cuts.append(next_cut)
        last = next_cut
    cuts.append(total)

    total_frames = max(1, int(round(total * FPS)))
    cut_frames = [0]
    for cut_time in cuts[1:-1]:
        frame = max(cut_frames[-1] + 1, min(total_frames - 1, int(round(cut_time * FPS))))
        if frame > cut_frames[-1]:
            cut_frames.append(frame)
    cut_frames.append(total_frames)

    clips = []
    for sequence, (start_frame, end_frame) in enumerate(zip(cut_frames, cut_frames[1:])):
        if end_frame <= start_frame:
            continue
        output_path = os.path.join(TMP_DIR, f"{prefix}_{sequence:03d}.mp4")
        normalize_video(
            input_path,
            output_path,
            mute=False,
            trim_frames=(start_frame, end_frame),
        )
        clips.append(
            Clip(
                output_path,
                prefix,
                get_duration(output_path),
                content_score(output_path),
                os.path.basename(input_path),
                start_frame / FPS,
                sequence,
                input_path,
            )
        )
    return clips


def _select_paths_for_transition(paths, target_duration, transition_duration, prefix):
    selected = []
    effective_duration = 0.0
    transition = max(0.0, float(transition_duration))
    for index, path in enumerate(paths):
        duration = timeline_video_duration(path, f"{prefix}转场候选")
        if duration <= transition + 0.05:
            continue
        added_duration = duration if not selected else duration - transition
        remaining = target_duration - effective_duration
        if added_duration <= remaining + 0.01:
            selected.append(path)
            effective_duration += added_duration
            if effective_duration >= target_duration - 0.01:
                break
            continue
        required_duration = remaining if not selected else remaining + transition
        if required_duration <= 0.05:
            break
        trimmed = os.path.join(TMP_DIR, f"{prefix}_trim_{index:03d}.mp4")
        normalize_video(path, trimmed, mute=False, trim=(0.0, required_duration))
        selected.append(trimmed)
        effective_duration = target_duration
        break
    if effective_duration < target_duration - 0.05:
        raise RuntimeError(
            f"{prefix} 可用素材不足：需要 {target_duration:.1f} 秒，"
            f"转场后仅有 {effective_duration:.1f} 秒。"
        )
    return selected


def concat_clips_with_transitions(
    paths,
    output_path,
    target_duration,
    transition_duration=0.28,
    prefix="section",
    fast_dedup_style=None,
):
    """Create real post-60s transitions without duplicating or freezing footage."""
    selected = _select_paths_for_transition(
        paths,
        float(target_duration),
        float(transition_duration),
        prefix,
    )
    if len(selected) == 1:
        single_target = min(
            float(target_duration),
            timeline_video_duration(selected[0], f"{prefix}单段转场"),
        )
        normalize_video(
            selected[0],
            output_path,
            mute=False,
            trim=(0.0, single_target),
            fast_dedup_style=fast_dedup_style,
        )
        require_av_duration_parity(output_path, single_target, f"{prefix}单段转场")
        return output_path

    durations = [
        timeline_video_duration(path, f"{prefix}转场输入")
        for path in selected
    ]
    available_duration = max(
        1.0 / FPS,
        sum(durations) - float(transition_duration) * (len(selected) - 1),
    )
    render_target = min(float(target_duration), available_duration)
    command = ["ffmpeg", "-y"]
    for path in selected:
        command.extend(["-i", path])

    filters = []
    for index, duration in enumerate(durations):
        filters.append(
            f"[{index}:v]fps={FPS},settb=AVTB,setpts=PTS-STARTPTS,"
            f"trim=duration={duration:.6f},setsar=1,format=yuv420p[v{index}]"
        )
        filters.append(
            f"[{index}:a]aresample=44100:async=1:first_pts=0,apad,"
            f"atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[a{index}]"
        )

    video_label = "[v0]"
    audio_label = "[a0]"
    accumulated = durations[0]
    transitions = ("fade", "smoothleft", "smoothright", "dissolve")
    for index in range(1, len(selected)):
        next_video = f"[vx{index}]"
        next_audio = f"[ax{index}]"
        offset = max(0.0, accumulated - transition_duration)
        transition_name = transitions[(index - 1) % len(transitions)]
        filters.append(
            f"{video_label}[v{index}]xfade=transition={transition_name}:"
            f"duration={transition_duration:.3f}:offset={offset:.3f}{next_video}"
        )
        filters.append(
            f"{audio_label}[a{index}]acrossfade=d={transition_duration:.3f}:"
            f"c1=tri:c2=tri{next_audio}"
        )
        video_label = next_video
        audio_label = next_audio
        accumulated += durations[index] - transition_duration

    style_chain = _fast_dedup_style_filters(fast_dedup_style)
    style_suffix = "," + ",".join(style_chain) if style_chain else ""
    filters.append(
        f"{video_label}trim=duration={render_target:.3f},setpts=PTS-STARTPTS"
        f"{style_suffix}[vout]"
    )
    filters.append(
        f"{audio_label}atrim=duration={render_target:.3f},asetpts=PTS-STARTPTS[aout]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            *_video_encoder_args(23, "veryfast"),
            "-g",
            str(FPS),
            "-fps_mode",
            "cfr",
            "-video_track_timescale",
            "90000",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-t",
            f"{render_target:.3f}",
            output_path,
        ]
    )
    run_ffmpeg(command)
    require_av_duration_parity(output_path, render_target, f"{prefix}自然转场")
    return output_path


def _render_hardcut_slices_fast(paths, output_path, mute_audio=True):
    """Concat N-second normalized slices cleanly with zero transition overlap."""
    if not paths:
        return None
    list_file = f"{output_path}.hardcut_concat.txt"
    total_dur = 0.0
    with open(list_file, "w", encoding="utf-8") as handle:
        for p in paths:
            dur = timeline_video_duration(p, "硬切切片")
            if dur <= 0.01:
                continue
            escaped = Path(p).resolve().as_posix().replace("'", "'\\''")
            handle.write(f"file '{escaped}'\n")
            total_dur += dur
    if total_dur <= 0.01:
        return None
    style_chain = _fast_dedup_style_filters(FAST_DEDUP_STYLE)
    vf_parts = [f"fps={FPS}", "setpts=PTS-STARTPTS", "setsar=1", "format=yuv420p", *style_chain]
    if mute_audio:
        audio_input_args = [
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
        ]
        audio_map_args = ["-map", "0:v:0", "-map", "1:a:0"]
        audio_filter_args = []
    else:
        audio_input_args = []
        audio_map_args = ["-map", "0:v:0", "-map", "0:a:0"]
        audio_filter_args = [
            "-af",
            "aresample=44100:async=1:first_pts=0,apad,asetpts=PTS-STARTPTS",
        ]
    try:
        run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                list_file,
                *audio_input_args,
                *audio_map_args,
                "-vf",
                ",".join(vf_parts),
                *audio_filter_args,
                *_video_encoder_args(23, "veryfast"),
                "-g",
                str(FPS),
                "-fps_mode",
                "cfr",
                "-video_track_timescale",
                "90000",
                "-c:a",
                "aac",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-b:a",
                "128k",
                "-t",
                f"{total_dur:.6f}",
                output_path,
            ]
        )
    finally:
        try:
            os.unlink(list_file)
        except OSError:
            pass
    actual_v = timeline_video_duration(output_path, "硬切打乱段")
    require_av_duration_parity(output_path, actual_v, "硬切打乱段")
    return output_path


def render_dedup1_story_section(clips, output_path, max_duration=None):
    """Render the selected story blocks with a subtle source-change transition."""
    paths = [clip.path for clip in clips if timeline_video_duration(clip.path, "完整块规划") > 0.01]
    if not paths:
        return None
    if HARDCUT_MODE:
        return _render_hardcut_slices_fast(paths, output_path, mute_audio=MUTE_ORIGINAL_AUDIO)
    if len(paths) == 1:
        duration = timeline_video_duration(paths[0], "单段规划")
        target = min(duration, float(max_duration)) if max_duration else duration
        normalize_video(
            paths[0],
            output_path,
            mute=MUTE_ORIGINAL_AUDIO,
            trim=(0.0, target),
            fast_dedup_style=FAST_DEDUP_STYLE,
        )
        return output_path
    transition = 0.10
    natural_duration = max(
        1.0 / FPS,
        sum(
            timeline_video_duration(path, "完整话术/动作段")
            for path in paths
        )
        - transition * (len(paths) - 1),
    )
    target = natural_duration
    if max_duration is not None:
        target = min(target, max(1.0 / FPS, float(max_duration)))
    try:
        return concat_clips_with_transitions(
            paths,
            output_path,
            target,
            transition_duration=transition,
            prefix="dedup1_story",
            fast_dedup_style=FAST_DEDUP_STYLE,
        )
    except RuntimeError as exc:
        if FINISH_LAST_CLIP:
            raise NaturalBoundaryError(
                f"{'打乱轨' if THREE_POOL_MODE else '去重'}完整话术/动作段的"
                "自然转场失败；为避免硬切尾段，本候选已停止。"
            ) from exc
        # A hard duration budget may consume all useful footage, leaving no
        # overlap allowance for xfade. Preserve the requested duration with a
        # direct ordered concat instead of stretching or looping footage.
        print(
            f"[WARN] {'打乱轨' if THREE_POOL_MODE else '去重'}转场余量不足，"
            f"改用直接拼接保持固定时长: {exc}"
        )
        unstyled_output = f"{output_path}.unstyled.mp4"
        concat_clips(
            paths,
            unstyled_output,
            target_duration=target,
            finish_last_clip=False,
            repeat=False,
            preserve_all=False,
        )
        normalize_video(
            unstyled_output,
            output_path,
            mute=MUTE_ORIGINAL_AUDIO,
            trim=(0.0, target),
            fast_dedup_style=FAST_DEDUP_STYLE,
        )
        try:
            os.unlink(unstyled_output)
        except OSError:
            pass
        return output_path



def plan_complete_story_duration(
    clips,
    reference_duration,
    max_extension=MAX_NATURAL_TAIL_SECONDS,
    transition=0.10,
):
    """Choose the first complete story-block end at or after a soft reference."""
    reference = max(1.0 / FPS, float(reference_duration or 0.0))
    extension_limit = max(0.0, float(max_extension or 0.0))
    transition = max(0.0, float(transition or 0.0))
    effective_duration = 0.0
    selected_count = 0

    for clip in clips:
        duration = timeline_video_duration(clip.path, "完整块规划")
        if duration <= transition + 0.05:
            continue
        effective_duration += duration if selected_count == 0 else duration - transition
        selected_count += 1
        if effective_duration < reference - 0.15:
            continue
        extension = max(0.0, effective_duration - reference)
        if extension <= extension_limit + 0.5 / FPS:
            return effective_duration
        raise NaturalBoundaryError(
            f"参考点后 {extension_limit:.1f} 秒内没有完整话术或动作结束点；"
            "为避免硬切尾段，本候选已停止。"
        )

    raise NaturalBoundaryError(
        f"{'打乱轨' if THREE_POOL_MODE else '去重素材'}不足以到达 "
        f"{reference:.1f} 秒参考用量后的完整话术或动作结束点。"
    )



def select_complete_source_window(paths, reference_duration):
    """Keep whole source files through the first file-end after a soft target.

    This is deliberately used only by the last-attempt natural-boundary
    fallback. A source file's end is a deterministic safe boundary; retaining
    it lets the normal speech/action splitter discover finer boundaries without
    introducing the arbitrary hard cut that triggered the retry in the first
    place.
    """
    reference = max(0.0, float(reference_duration or 0.0))
    selected = []
    total = 0.0
    for path in paths:
        duration = max(0.0, float(get_duration(path) or 0.0))
        if duration <= 0.01:
            continue
        selected.append(path)
        total += duration
        if total >= reference - 0.01:
            break
    if not selected:
        raise NaturalBoundaryError(
            "自然收尾容错无法找到可读取的完整打乱源文件。"
        )
    return selected, total


def plan_complete_story_fallback(
    clips,
    reference_duration,
    max_extension=MAX_NATURAL_TAIL_SECONDS,
    fallback_extra=NATURAL_BOUNDARY_FALLBACK_EXTRA_SECONDS,
    transition=0.10,
    max_states=4096,
):
    """Build a deterministic, ordered plan from complete story blocks only.

    The strict planner intentionally considers a prefix and rejects the first
    boundary outside its normal tail. On the final retry we may instead skip an
    oversized block and continue with later whole blocks, while preserving the
    relative order of every selected block. A bounded beam keeps this search
    predictable. The ordinary tail is preferred; the explicit extra allowance
    is used only when no ordinary-tail plan exists.

    Returns ``(selected_clips, effective_duration, used_extra_allowance)``.
    No clip is trimmed, split, reordered or synthesized here.
    """
    reference = max(1.0 / FPS, float(reference_duration or 0.0))
    normal_extension = max(0.0, float(max_extension or 0.0))
    extra_extension = max(0.0, min(18.0, float(fallback_extra or 0.0)))
    transition = max(0.0, float(transition or 0.0))
    frame_tolerance = 0.5 / max(1, int(FPS))
    lower = reference - 0.15
    strict_upper = reference + normal_extension + frame_tolerance
    fallback_upper = strict_upper + extra_extension
    max_states = max(32, min(8192, int(max_states or 4096)))

    eligible = []
    for original_index, clip in enumerate(clips):
        duration = max(
            0.0,
            float(timeline_video_duration(clip.path, "完整块规划") or clip.duration or 0.0),
        )
        if duration <= transition + 0.05:
            continue
        eligible.append((clip, duration, original_index))
    if not eligible:
        raise NaturalBoundaryError(
            "自然收尾容错没有可用的完整话术或动作块。"
        )

    # Each state is (effective duration, tuple of eligible-list indices).
    # States at/above the reference no longer need expansion because all added
    # durations are positive. Separate quotas prevent early overshoots from
    # crowding out a closer later combination.
    states = [(0.0, ())]

    def state_identity(state):
        total, indices = state
        frame = int(round(total * max(1, int(FPS))))
        starts_with_first = bool(indices) and indices[0] == 0
        return frame, starts_with_first

    def state_rank(state):
        total, indices = state
        starts_late = 0 if indices and indices[0] == 0 else 1
        return (
            abs(total - reference),
            starts_late,
            -len(indices),
            indices,
        )

    for eligible_index, (_clip, duration, _original_index) in enumerate(eligible):
        candidates = list(states)
        for total, indices in states:
            if total >= lower:
                continue
            added = duration if not indices else duration - transition
            candidate_total = total + added
            if candidate_total <= fallback_upper + frame_tolerance:
                candidates.append((candidate_total, (*indices, eligible_index)))

        unique = {}
        for state in candidates:
            identity = state_identity(state)
            previous = unique.get(identity)
            if previous is None or state_rank(state) < state_rank(previous):
                unique[identity] = state
        reached = [state for state in unique.values() if state[0] >= lower]
        unreached = [state for state in unique.values() if state[0] < lower]
        reached.sort(key=state_rank)
        unreached.sort(key=state_rank)
        reached_quota = max_states // 2
        unreached_quota = max_states - reached_quota
        states = [
            *reached[:reached_quota],
            *unreached[:unreached_quota],
        ]
        if not any(not indices for _total, indices in states):
            states.append((0.0, ()))

    strict_candidates = [
        state
        for state in states
        if state[1] and lower <= state[0] <= strict_upper
    ]
    relaxed_candidates = [
        state
        for state in states
        if state[1] and lower <= state[0] <= fallback_upper
    ]
    if strict_candidates:
        chosen_total, chosen_indices = min(strict_candidates, key=state_rank)
        used_extra = False
    elif relaxed_candidates:
        chosen_total, chosen_indices = min(relaxed_candidates, key=state_rank)
        used_extra = True
    else:
        raise NaturalBoundaryError(
            "自然收尾容错在保留完整话术/动作块且不改变顺序的"
            f"前提下，仍无法在参考 {reference:.1f} 秒后 "
            f"{normal_extension + extra_extension:.1f} 秒内找到完整结束点。"
        )

    selected = [eligible[index][0] for index in chosen_indices]
    return selected, chosen_total, used_extra



def dedup_story_interleave_split_times(clips, target_duration, transition=0.10):
    """Return completed story-block boundaries from the same xfade plan."""
    paths = [clip.path for clip in clips if timeline_video_duration(clip.path, "完整块规划") > 0.01]
    if len(paths) < 2:
        return []
    target = max(0.0, float(target_duration or 0.0))
    selected = _select_paths_for_transition(
        paths, target, float(transition), "dedup_interleave_plan"
    )
    if len(selected) < 2:
        return []
    cuts = []
    cursor = timeline_video_duration(selected[0], "穿插边界")
    for path in selected[1:]:
        if 1.0 < cursor < target - 1.0:
            cuts.append(round(cursor, 6))
        cursor += timeline_video_duration(path, "穿插边界") - float(transition)
    return cuts



def _pause_safe_split_times(path, target_piece_seconds):
    """Pick sentence-like audio gaps; return no cuts rather than break speech."""
    duration = get_duration(path)
    if duration <= target_piece_seconds + 1.0 or not has_audio_stream(path):
        return []
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "info",
                "-i",
                path,
                "-vn",
                "-af",
                "silencedetect=noise=-38dB:d=0.35",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=max(10.0, min(60.0, duration * 0.25)),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    pauses = []
    for end_value, duration_value in re.findall(
        r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)\s*\|\s*"
        r"silence_duration:\s*([0-9]+(?:\.[0-9]+)?)",
        result.stderr,
    ):
        silence_end = float(end_value)
        silence_duration = float(duration_value)
        midpoint = round(silence_end - silence_duration / 2.0, 6)
        if silence_duration >= 0.35 and 0.5 < midpoint < duration - 0.5:
            pauses.append(midpoint)
    pauses = sorted(set(pauses))
    cuts = []
    cursor = 0.0
    while cursor + target_piece_seconds < duration - 1.0:
        lower = cursor + max(5.0, target_piece_seconds * 0.65)
        ideal = cursor + target_piece_seconds
        upper = min(duration - 1.0, cursor + target_piece_seconds * 1.55)
        candidates = [point for point in pauses if lower <= point <= upper]
        if not candidates:
            break
        chosen = min(candidates, key=lambda point: abs(point - ideal))
        cuts.append(chosen)
        cursor = chosen
    return cuts


def split_normalized_path_for_interleave(
    path,
    prefix,
    piece_seconds=10.0,
    pause_safe=True,
    preferred_split_times=None,
):
    """Split at audio pauses using stream copy, never another video encode."""
    duration = get_duration(path)
    piece_seconds = max(3.0, float(piece_seconds or 7.0))
    split_times = [
        float(value)
        for value in (preferred_split_times or [])
        if 0.5 < float(value) < duration - 0.5
    ]
    if not split_times and duration <= piece_seconds + 0.25:
        return [path]
    if not split_times and pause_safe:
        split_times = _pause_safe_split_times(path, piece_seconds)
    if pause_safe and not split_times:
        return [path]
    pattern = os.path.join(TMP_DIR, f"{prefix}_%03d.mp4")
    command = [
        "ffmpeg",
        "-y",
        "-i",
        path,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-f",
        "segment",
    ]
    if split_times:
        command.extend(
            [
                "-segment_times",
                ",".join(f"{value:.3f}" for value in split_times),
                "-segment_time_delta",
                "0.500",
            ]
        )
    else:
        command.extend(["-segment_time", f"{piece_seconds:.3f}"])
    command.extend(["-reset_timestamps", "1", pattern])
    run_ffmpeg(command)
    prefix_name = f"{prefix}_"
    parts = sorted(
        os.path.join(TMP_DIR, name)
        for name in os.listdir(TMP_DIR)
        if name.startswith(prefix_name) and name.endswith(".mp4")
    )
    return [part for part in parts if get_duration(part) > 0.05] or [path]


def split_and_shuffle_fission_paths(paths, prefix, piece_seconds=8.0):
    """Stream-copy a fixed track into at least three pieces, then shuffle it.

    The canonical normalized files remain untouched so the batch cache can be
    reused by later variants.  Only the cheap per-candidate stream-copy pieces
    are reordered.
    """
    originals = list(paths)
    if not FISSION_SHUFFLE_ALL or not FISSION_VARIANT_SEED or not originals:
        return originals

    durations = [max(0.0, get_duration(path)) for path in originals]
    total_duration = sum(durations)
    if total_duration <= 0.15:
        return shuffle_fission_paths(originals, f"{prefix}-segments")

    piece_seconds = max(1.0, float(piece_seconds or 8.0))
    desired_count = max(
        len(originals),
        3,
        min(8, int(math.ceil(total_duration / piece_seconds))),
    )
    allocation = [1 for _path in originals]
    while sum(allocation) < desired_count:
        index = max(
            range(len(originals)),
            key=lambda item: durations[item] / max(1, allocation[item]),
        )
        allocation[index] += 1

    pieces = []
    for index, (path, duration, segment_count) in enumerate(
        zip(originals, durations, allocation)
    ):
        if segment_count <= 1 or duration <= 0.15:
            pieces.append(path)
            continue
        split_times = [
            duration * part / segment_count
            for part in range(1, segment_count)
        ]
        pieces.extend(
            split_normalized_path_for_interleave(
                path,
                f"{prefix}_{index:03d}",
                piece_seconds=piece_seconds,
                pause_safe=False,
                preferred_split_times=split_times,
            )
        )
    return shuffle_fission_paths(pieces, f"{prefix}-segments")


def interleave_ordered_quality_and_dedup(quality_paths, dedup_paths, seed=""):
    """Evenly insert ordered dedup shots after ordered protected anchors.

    The historical function name is kept for compatibility, but in 8018
    fission mode ``quality_paths`` contains the already-shuffled whole
    core/quality anchor list. No dedup shot is placed before the first anchor;
    the final shot remains dedup so natural-tail trimming never cuts a protected
    block. Both input orders are preserved exactly.
    """
    quality = list(quality_paths)
    dedup = list(dedup_paths)
    if not quality:
        return dedup
    if not dedup:
        return quality
    slots = [[] for _ in range(len(quality))]
    digest = hashlib.sha256(str(seed or "default").encode("utf-8")).digest()
    base_count, remainder = divmod(len(dedup), len(quality))
    slot_counts = [base_count for _path in quality]
    if remainder:
        offset = int.from_bytes(digest[:4], "big") % len(quality)
        extra_slots = [
            (offset + int(index * len(quality) / remainder)) % len(quality)
            for index in range(remainder)
        ]
        # When there are fewer dedup shots than anchors, explicitly reserve the
        # last shot for the final anchor so the completed timeline ends in the
        # variable track.
        if base_count == 0 and len(quality) - 1 not in extra_slots:
            extra_slots[-1] = len(quality) - 1
        for slot_index in extra_slots:
            slot_counts[slot_index] += 1

    cursor = 0
    for slot_index, count in enumerate(slot_counts):
        slots[slot_index].extend(dedup[cursor : cursor + count])
        cursor += count
    timeline = []
    for index, path in enumerate(quality):
        timeline.append(path)
        timeline.extend(slots[index])
    return timeline


def interleave_core_into_ordered_shuffle(
    core_paths,
    shuffle_paths,
    core_first=False,
):
    """Interleave whole core blocks without changing shuffle fingerprint order."""
    core = list(core_paths)
    shuffle = list(shuffle_paths)
    if not shuffle:
        return core
    if not core:
        return shuffle
    if core_first:
        opener = core[0]
        remaining_core = core[1:]
        if len(shuffle) == 1:
            return [opener, *remaining_core, shuffle[0]]
        slots = [[] for _path in range(len(shuffle) - 1)]
        for index, path in enumerate(remaining_core):
            slot_index = min(
                len(slots) - 1,
                int(index * len(slots) / max(1, len(remaining_core))),
            )
            slots[slot_index].append(path)
        timeline = [opener]
        for index, path in enumerate(shuffle):
            timeline.append(path)
            if index < len(slots):
                timeline.extend(slots[index])
        return timeline
    if len(shuffle) == 1:
        # There is no safe natural gap inside this track. Opening on the sole
        # complete shuffle block still has priority; the core remains intact.
        return [shuffle[0], *core]

    # Slots are after shuffle items 0..n-2. The final shuffle block is never
    # followed by core, preserving the variable-track natural ending.
    slots = [[] for _path in range(len(shuffle) - 1)]
    for index, path in enumerate(core):
        slot_index = min(
            len(slots) - 1,
            int(index * len(slots) / max(1, len(core))),
        )
        slots[slot_index].append(path)
    timeline = []
    for index, path in enumerate(shuffle):
        timeline.append(path)
        if index < len(slots):
            timeline.extend(slots[index])
    return timeline


def _video_dhashes(path, sample_count=48):
    """Build a compact visual fingerprint for the rendered dedup-only track."""
    track_label = "打乱轨道" if THREE_POOL_MODE else "去重轨道"
    duration = get_duration(path)
    if duration < 0.35:
        raise RuntimeError(f"{track_label}太短，无法生成裂变相似度指纹。")
    samples = max(16, min(72, int(sample_count)))
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            path,
            "-vf",
            f"fps={samples / duration:.8f},scale=9:8:flags=area,format=gray",
            "-frames:v",
            str(samples),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ],
        capture_output=True,
        timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError(f"无法读取{track_label}生成裂变相似度指纹。")
    frame_size = 9 * 8
    hashes = []
    for offset in range(0, len(result.stdout) - frame_size + 1, frame_size):
        frame = result.stdout[offset : offset + frame_size]
        value = 0
        for row in range(8):
            row_offset = row * 9
            for column in range(8):
                value = (value << 1) | int(
                    frame[row_offset + column] > frame[row_offset + column + 1]
                )
        hashes.append(value)
        if len(hashes) >= samples:
            break
    if len(hashes) < 8:
        raise RuntimeError(f"{track_label}没有足够画面生成裂变相似度指纹。")
    return hashes


def write_dedup_hash_file(path, output_path=DEDUP_HASH_FILE):
    """Atomically publish only hashes/metadata; never expose source paths."""
    if not output_path:
        return None
    payload = {
        "version": 2 if THREE_POOL_MODE else 1,
        "track": "shuffle" if THREE_POOL_MODE else "dedup",
        "style": FAST_DEDUP_STYLE,
        "visual_quality_policy": "natural-color-preserving-clean-only-v1",
        "duration": round(get_duration(path), 3),
        "hashes": _video_dhashes(path),
        "source_windows": [dict(item) for item in SOURCE_WINDOW_AUDIT[:128]],
    }
    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    pending_path = f"{output_path}.partial"
    try:
        with open(pending_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(pending_path, output_path)
    finally:
        if os.path.exists(pending_path):
            try:
                os.remove(pending_path)
            except OSError:
                pass
    return payload


def update_dedup_sidecar_visual_quality(report, output_path=DEDUP_HASH_FILE):
    """Attach the path-free final gate evidence to the existing sidecar."""
    if not output_path:
        return
    output_path = os.path.abspath(output_path)
    pending_path = f"{output_path}.visual.partial"
    try:
        with open(output_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict) or "source_windows" not in payload:
            raise ValueError("missing source window audit")
        payload["visual_quality_gate"] = dict(report)
        with open(pending_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(pending_path, output_path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VisualQualityError(
            "成片画质验收证据无法安全写入；为避免无审计交付，"
            "本候选已停止。"
        ) from exc
    finally:
        if os.path.exists(pending_path):
            try:
                os.remove(pending_path)
            except OSError:
                pass


def _calibrated_dhash_similarity(first_hashes, second_hashes):
    pairs = min(len(first_hashes), len(second_hashes))
    if pairs <= 0:
        return 100.0
    raw = sum(
        (64 - (int(first_hashes[index]) ^ int(second_hashes[index])).bit_count()) / 64.0
        for index in range(pairs)
    ) / pairs
    return round(max(0.0, min(1.0, (raw - 0.5) / 0.5)) * 100.0, 2)


def early_dedup_similarity_percent(current_hashes, prior_json=PRIOR_DEDUP_HASHES_JSON):
    """Fail closed only for well-formed, parent-supplied prior fingerprints."""
    if not prior_json:
        return 0.0
    try:
        prior_sets = json.loads(prior_json)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"页面下发的既有{'打乱' if THREE_POOL_MODE else '去重'}指纹无效。"
        )
    if not isinstance(prior_sets, list) or len(prior_sets) > 5:
        raise RuntimeError(
            f"页面下发的既有{'打乱' if THREE_POOL_MODE else '去重'}指纹无效。"
        )
    clean_sets = []
    for hashes in prior_sets:
        if (
            not isinstance(hashes, list)
            or not 8 <= len(hashes) <= 72
            or any(
                not isinstance(value, int) or value < 0 or value >= (1 << 64)
                for value in hashes
            )
        ):
            raise RuntimeError(
                f"页面下发的既有{'打乱' if THREE_POOL_MODE else '去重'}指纹无效。"
            )
        clean_sets.append(hashes)
    return max(
        (_calibrated_dhash_similarity(current_hashes, hashes) for hashes in clean_sets),
        default=0.0,
    )


def ensure_dedup1_minimum_duration(
    input_path,
    output_path,
    minimum_duration=DEDUP_SECTION_MIN_SECONDS,
):
    """Reach the minimum with a gentle A/V-synchronous retime, never a loop."""
    source_duration = get_duration(input_path)
    minimum = max(0.5, float(minimum_duration or 0.0))
    if source_duration + (2.0 / FPS) >= minimum:
        return input_path
    stretch = minimum / max(source_duration, 1.0 / FPS)
    if stretch > 1.12:
        raise RuntimeError(
            "去重素材池1有效内容不足40秒，且需要超过12%的慢放才能补足；"
            f"当前仅 {source_duration:.1f}s。请增加真实素材，系统不会循环或停帧凑时长。"
        )

    command = ["ffmpeg", "-y", "-i", input_path]
    filters = [
        f"[0:v]setpts={stretch:.8f}*PTS,fps={FPS},"
        f"trim=duration={minimum:.3f},format=yuv420p,setpts=PTS-STARTPTS[vout]"
    ]
    if has_audio_stream(input_path):
        audio_input = "[0:a]"
    else:
        command.extend(
            [
                "-f",
                "lavfi",
                "-t",
                f"{minimum:.3f}",
                "-i",
                "anullsrc=r=44100:cl=stereo",
            ]
        )
        audio_input = "[1:a]"
    filters.append(
        f"{audio_input}{_atempo_filter(1.0 / stretch)},aresample=44100,apad,"
        f"atrim=duration={minimum:.3f},asetpts=PTS-STARTPTS[aout]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            *_video_encoder_args(23, "veryfast"),
            "-fps_mode",
            "cfr",
            "-video_track_timescale",
            "90000",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            "-t",
            f"{minimum:.3f}",
            output_path,
        ]
    )
    run_ffmpeg(command)
    rendered_duration = get_duration(output_path)
    if abs(rendered_duration - minimum) > max(0.12, 2.0 / FPS):
        raise RuntimeError(
            "去重素材池1最短时长校验失败："
            f"输出 {rendered_duration:.3f}s / 目标 {minimum:.3f}s。"
        )
    print(
        "   -> 去重素材池1有效画面略低于40秒，已做音画同步轻微舒缓："
        f"{source_duration:.1f}s → {minimum:.1f}s；未循环、未停帧。"
    )
    return output_path


def apply_spec_section_style(input_path, output_path, section_name):
    """Apply the document's staged filter and effect-density rules."""
    if section_name == "core":
        strength = 0.30
        style_filter = "eq=contrast=1.025:saturation=1.045:brightness=0.004"
    elif section_name == "quality":
        strength = 0.42
        style_filter = (
            "eq=contrast=1.045:saturation=1.085:"
            "brightness='if(lt(mod(t,19),1),0.012,0.004)'"
        )
    else:
        strength = 0.55
        style_filter = (
            "eq=contrast=1.06:saturation=1.11:"
            "brightness='if(lt(mod(t,13),1),0.018,0.005)',"
            "vignette=PI/9"
        )
    filter_complex = (
        f"[0:v]split=2[base][effect];[effect]{style_filter}[styled];"
        f"[base][styled]blend=all_expr='A*{1.0-strength:.3f}+B*{strength:.3f}',"
        f"fps={FPS},format=yuv420p[vout];"
        "[0:a]aresample=44100,asetpts=PTS-STARTPTS[aout]"
    )
    run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-filter_complex",
            filter_complex,
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            *_video_encoder_args(23, "veryfast"),
            "-fps_mode",
            "cfr",
            "-video_track_timescale",
            "90000",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            output_path,
        ]
    )
    return output_path


def content_score(path):
    # Cheap local smoothness score: prefer brighter, less static clips.
    if not has_module("cv2"):
        return random.Random(_stable_seed([path], "content-score-fallback")).random()
    code = r"""
import cv2, sys
p=sys.argv[1]
cap=cv2.VideoCapture(p)
vals=[]
last=None
for _ in range(8):
    ok, frame=cap.read()
    if not ok: break
    small=cv2.resize(frame,(64,64))
    gray=cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    motion=0 if last is None else cv2.absdiff(gray,last).mean()
    vals.append(gray.mean()/255.0 + motion/255.0)
    last=gray
print(sum(vals)/len(vals) if vals else 0)
"""
    exe = PY312 if os.path.exists(PY312) else sys.executable
    res = subprocess.run([exe, "-c", code, path], capture_output=True, text=True)
    try:
        return float(res.stdout.strip())
    except Exception:
        return random.Random(_stable_seed([path], "content-score-error")).random()


_OCR_PRICE_PATTERN = (
    r"((?:¥|￥|\$)\s*\d{1,6}(?:\.\d{1,2})?|到手价?|券后|价格|优惠券|折扣|立减|"
    r"(?:只要|仅需)\s*(?:¥|￥|\$)?\s*\d{1,6}(?:\.\d{1,2})?\s*(?:元|块)?|"
    r"\d{1,6}(?:\.\d{1,2})?\s*(?:多|来)?\s*(?:元|块)(?:钱)?(?![A-Za-z])|"
    r"\d{1,3}(?:\.\d{1,2})?\s*折|"
    r"(?:一|二|两|三|四|五|六|七|八|九|十|百|千)+\s*(?:多|来)?\s*(?:元|块)(?:钱)?(?![A-Za-z]))"
)
_EXTREME_COPY_PATTERN = re.compile(
    r"(全网最低|世界级|国家级|顶级|极品|第一品牌|销量第一|"
    r"永久(?:有效|使用|保证)?|100%|百分之百|绝对(?:安全|有效|保证)?|"
    r"唯一(?:选择|品牌|正品)?|万能|零风险|无敌|最便宜|最先进|最有效|"
    r"最强|最好|最高级|史上(?:最|第一)|包治|根治|神效|稳赚|"
    r"立即抢购|错过不再)",
    re.IGNORECASE,
)


def detect_visual_price_regions(path):
    """Return OCR-confirmed price rectangles with observed time windows."""
    if not ENABLE_PRICE_FILTER or not has_module("pytesseract") or not has_module("cv2"):
        return []
    cache_path = ""
    try:
        digest = hashlib.sha256()
        digest.update(b"price-events-v4\0")
        digest.update(_OCR_PRICE_PATTERN.encode("utf-8"))
        digest.update(f"{PRICE_SCAN_INTERVAL_SECONDS:.3f}:{PRICE_SCAN_MAX_SAMPLES}".encode("ascii"))
        with open(path, "rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        cache_path = os.path.join(PRICE_CACHE_DIR, f"{digest.hexdigest()}.json")
        if os.path.isfile(cache_path):
            with open(cache_path, "r", encoding="utf-8") as handle:
                cached_regions = json.load(handle)
            return [
                (
                    *tuple(max(0, int(value)) for value in region[:4]),
                    max(0.0, float(region[4])),
                    max(0.0, float(region[5])),
                )
                for region in cached_regions
                if isinstance(region, list) and len(region) >= 6
                and int(region[2]) >= 4 and int(region[3]) >= 4
                and float(region[5]) > float(region[4])
            ]
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        cache_path = ""
    code = r"""
import json, re, sys, cv2, pytesseract
p=sys.argv[1]
cap=cv2.VideoCapture(p)
total=int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
fps=float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
interval=max(0.25,float(sys.argv[3]))
max_samples=max(6,int(sys.argv[4]))
step=max(1,int(round(fps*interval)))
idxs=list(range(0,max(total,1),step))
if total>0 and total-1 not in idxs: idxs.append(total-1)
if len(idxs)>max_samples:
    idxs=[idxs[round(i*(len(idxs)-1)/(max_samples-1))] for i in range(max_samples)]
idxs=sorted(set(idxs))
price=re.compile(sys.argv[2])
events=[]
sample_times=[]
for idx in idxs:
    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
    ok, frame=cap.read()
    if not ok: continue
    height,width=frame.shape[:2]
    sample_time=idx/max(fps,1.0)
    sample_times.append(sample_time)
    gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
    data=pytesseract.image_to_data(
        gray,lang='chi_sim+eng',config='--psm 6',output_type=pytesseract.Output.DICT
    )
    lines={}
    for item_index,text in enumerate(data.get('text') or []):
        clean=str(text or '').strip()
        if not clean: continue
        key=(data['block_num'][item_index],data['par_num'][item_index],data['line_num'][item_index])
        lines.setdefault(key,[]).append((
            clean,int(data['left'][item_index]),int(data['top'][item_index]),
            int(data['width'][item_index]),int(data['height'][item_index])
        ))
    for words in lines.values():
        line_text=''.join(word[0] for word in words)
        if not price.search(line_text): continue
        left=min(word[1] for word in words)
        top=min(word[2] for word in words)
        right=max(word[1]+word[3] for word in words)
        bottom=max(word[2]+word[4] for word in words)
        pad_x=max(8,int((right-left)*.10))
        pad_y=max(6,int((bottom-top)*.30))
        events.append([
            max(0,left-pad_x),max(0,top-pad_y),
            min(width,right+pad_x)-max(0,left-pad_x),
            min(height,bottom+pad_y)-max(0,top-pad_y),
            sample_time,
        ])
cap.release()
duration=(total/max(fps,1.0)) if total>0 else 0.0
gaps=[right-left for left,right in zip(sample_times,sample_times[1:]) if right>left]
sample_span=max(interval,(sum(gaps)/len(gaps)) if gaps else interval)
lead=sample_span*.60
trail=sample_span*.80
tracks=[]
for x,y,w,h,sample_time in sorted(events,key=lambda item:(item[4],item[1],item[0])):
    if w<4 or h<4: continue
    best=None
    best_distance=1e9
    for track in tracks:
        tx,ty,tw,th,start,end,last=track
        center_distance=abs((y+h/2.0)-(ty+th/2.0))
        same_row=center_distance<=max(18.0,min(h,th)*.60)
        close_in_time=sample_time-last<=sample_span*1.60
        if same_row and close_in_time and center_distance<best_distance:
            best=track;best_distance=center_distance
    if best is None:
        tracks.append([
            x,y,w,h,max(0.0,sample_time-lead),
            min(duration,sample_time+trail),sample_time,
        ])
    else:
        tx,ty,tw,th,start,end,last=best
        nx,ny=min(x,tx),min(y,ty)
        nr,nb=max(x+w,tx+tw),max(y+h,ty+th)
        best[:]=[
            nx,ny,nr-nx,nb-ny,start,
            min(duration,sample_time+trail),sample_time,
        ]
ranked=sorted(tracks,key=lambda item:((item[5]-item[4]),item[2]*item[3]),reverse=True)[:12]
print(json.dumps([[*item[:4],item[4],item[5]] for item in ranked]))
"""
    exe = PY312 if os.path.exists(PY312) else sys.executable
    try:
        res = subprocess.run(
            [
                exe,
                "-c",
                code,
                path,
                _OCR_PRICE_PATTERN,
                str(PRICE_SCAN_INTERVAL_SECONDS),
                str(PRICE_SCAN_MAX_SAMPLES),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=120,
        )
        regions = json.loads(res.stdout or "[]") if res.returncode == 0 else []
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    normalized_regions = [
        (
            *tuple(max(0, int(value)) for value in region[:4]),
            max(0.0, float(region[4])),
            max(0.0, float(region[5])),
        )
        for region in regions
        if isinstance(region, list) and len(region) >= 6
        and int(region[2]) >= 4 and int(region[3]) >= 4
        and float(region[5]) > float(region[4])
    ]
    if cache_path and res.returncode == 0:
        try:
            os.makedirs(PRICE_CACHE_DIR, exist_ok=True)
            pending_cache = f"{cache_path}.{os.getpid()}.tmp"
            with open(pending_cache, "w", encoding="utf-8") as handle:
                json.dump(normalized_regions, handle, ensure_ascii=False)
            os.replace(pending_cache, cache_path)
        except OSError:
            pass
    return normalized_regions


def detect_visual_text_regions(path):
    """Return timed text regions, including stylized text OCR cannot read.

    Every result is ``(x, y, width, height, start, end)``.  Tesseract supplies
    semantic text lines while a conservative stroke-density fallback catches
    outlined title cards such as ``静电膜``.  Keeping the time window prevents
    a cover band found on one frame from remaining visible for the whole shot.
    """
    if not ENABLE_PRICE_FILTER or not has_module("pytesseract") or not has_module("cv2"):
        return []
    cache_path = ""
    try:
        digest = hashlib.sha256()
        digest.update(b"visual-text-events-v6\0")
        digest.update(_OCR_PRICE_PATTERN.encode("utf-8"))
        digest.update(f"{PRICE_SCAN_INTERVAL_SECONDS:.3f}:{PRICE_SCAN_MAX_SAMPLES}".encode("ascii"))
        with open(path, "rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        cache_path = os.path.join(TEXT_CACHE_DIR, f"{digest.hexdigest()}.json")
        if os.path.isfile(cache_path):
            with open(cache_path, "r", encoding="utf-8") as handle:
                cached_regions = json.load(handle)
            return [
                (
                    *tuple(max(0, int(value)) for value in region[:4]),
                    max(0.0, float(region[4])),
                    max(0.0, float(region[5])),
                )
                for region in cached_regions
                if isinstance(region, list) and len(region) >= 6
                and int(region[2]) >= 4 and int(region[3]) >= 4
            ]
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        cache_path = ""
    code = r"""
import json, re, sys, cv2, pytesseract
p=sys.argv[1]
cap=cv2.VideoCapture(p)
total=int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
fps=float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
interval=max(0.25,float(sys.argv[2]))
max_samples=max(6,int(sys.argv[3]))
price=re.compile(sys.argv[4])
step=max(1,int(round(fps*interval)))
idxs=list(range(0,max(total,1),step))
if total>0 and total-1 not in idxs: idxs.append(total-1)
if len(idxs)>max_samples:
    idxs=[idxs[round(i*(len(idxs)-1)/(max_samples-1))] for i in range(max_samples)]
events=[]
duration=(total/max(fps,1.0)) if total>0 else 0.0
for idx in sorted(set(idxs)):
    cap.set(cv2.CAP_PROP_POS_FRAMES,idx)
    ok,frame=cap.read()
    if not ok: continue
    height,width=frame.shape[:2]
    gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
    data=pytesseract.image_to_data(gray,lang='chi_sim+eng',config='--psm 6',output_type=pytesseract.Output.DICT)
    lines={}
    for item_index,text in enumerate(data.get('text') or []):
        clean=str(text or '').strip()
        if not clean: continue
        try: confidence=float(data.get('conf',[0])[item_index])
        except (TypeError,ValueError,IndexError): confidence=0.0
        # Do not discard low-confidence glyphs here.  We are locating text,
        # not trusting the recognized wording; outlined Chinese titles often
        # produce several confidence-0 Latin fragments whose combined row is
        # geometrically accurate.
        if confidence<0.0: continue
        key=(data['block_num'][item_index],data['par_num'][item_index],data['line_num'][item_index])
        lines.setdefault(key,[]).append((
            clean,int(data['left'][item_index]),int(data['top'][item_index]),
            int(data['width'][item_index]),int(data['height'][item_index]),confidence
        ))
    frame_regions=[]
    for words in lines.values():
        line_text=''.join(word[0] for word in words)
        readable=re.sub(r'[^0-9A-Za-z\u4e00-\u9fff]+','',line_text)
        if len(readable)<2: continue
        line_confidence=sum(word[5] for word in words)/len(words)
        has_cjk=bool(re.search(r'[\u4e00-\u9fff]',line_text))
        has_long_latin=bool(re.search(r'[A-Za-z]{4,}',line_text)) and line_confidence>=50.0
        is_price=bool(price.search(line_text))
        left=min(word[1] for word in words)
        top=min(word[2] for word in words)
        right=max(word[1]+word[3] for word in words)
        bottom=max(word[2]+word[4] for word in words)
        line_width=right-left
        line_height=bottom-top
        likely_overlay_zone=top<=int(height*.22) or bottom>=int(height*.58)
        plausible_line_height=max(10,int(height*.009))<=line_height<=int(height*.145)
        # Stylized Chinese titles are often returned as low-confidence Latin
        # gibberish.  A wide, multi-glyph OCR row in an overlay zone is still
        # a reliable text-shape signal even when recognition is semantically
        # wrong (for example 静电膜 -> "nyBe").
        looks_like_overlay_shape=(
            line_width>=int(width*.16)
            and line_width<=int(width*.86)
            and (len(readable)>=4 or len(words)>=2)
            and line_confidence>=5.0
        )
        if not (has_cjk or has_long_latin or is_price or looks_like_overlay_shape): continue
        if (not likely_overlay_zone and not is_price) or not plausible_line_height: continue
        if line_width<max(36,int(width*.05)): continue
        frame_regions.append([max(0,left),max(0,top),min(width,right)-max(0,left),min(height,bottom)-max(0,top)])

    # OCR often misses thick white characters with black outlines.  Detect
    # their connected stroke shape without pretending to understand the text.
    edges=cv2.Canny(gray,80,180)
    closed=cv2.morphologyEx(edges,cv2.MORPH_CLOSE,cv2.getStructuringElement(cv2.MORPH_RECT,(17,3)))
    count,_labels,stats,_centroids=cv2.connectedComponentsWithStats((closed>0).astype('uint8'),8)
    for component in range(1,count):
        left,top,line_width,line_height,area=[int(v) for v in stats[component]]
        bottom=top+line_height
        likely_overlay_zone=top<=int(height*.22) or bottom>=int(height*.55)
        plausible_height=max(12,int(height*.010))<=line_height<=int(height*.145)
        density=area/max(1.0,float(line_width*line_height))
        center_x=left+line_width/2.0
        if not likely_overlay_zone or not plausible_height: continue
        if (
            line_width<max(72,int(width*.12))
            or line_width<line_height*2.0
            or density<.18
            or center_x<int(width*.10)
            or center_x>int(width*.90)
        ): continue
        frame_regions.append([left,top,line_width,line_height])

    # Collapse OCR and geometry duplicates on this sample while keeping
    # genuinely separate title/subtitle rows as separate cover bands.
    deduped=[]
    for region in sorted(frame_regions,key=lambda item:(item[1],item[0])):
        x,y,w,h=region
        merged=False
        for existing in deduped:
            ex,ey,ew,eh=existing
            center_distance=abs((y+h/2.0)-(ey+eh/2.0))
            vertical_match=center_distance<=max(12.0,min(h,eh)*.45)
            horizontal_overlap=max(0,min(x+w,ex+ew)-max(x,ex))
            if vertical_match and horizontal_overlap>=min(w,ew)*.25:
                nx,ny=min(x,ex),min(y,ey)
                nr,nb=max(x+w,ex+ew),max(y+h,ey+eh)
                existing[:]=[nx,ny,nr-nx,nb-ny]
                merged=True
                break
        if not merged: deduped.append(region)
    sample_time=idx/max(fps,1.0)
    for x,y,w,h in deduped:
        events.append([x,y,w,h,sample_time])
cap.release()
tracks=[]
lead=interval*.80
trail=interval*1.20
for x,y,w,h,sample_time in sorted(events,key=lambda item:(item[4],item[1],item[0])):
    best=None
    best_distance=1e9
    for track in tracks:
        tx,ty,tw,th,start,end,last=track
        center_distance=abs((y+h/2.0)-(ty+th/2.0))
        same_row=center_distance<=max(18.0,min(h,th)*.55)
        # Allow one missed sample so a subtitle row does not blink between
        # adjacent half-second scans.
        close_in_time=sample_time-last<=interval*2.10
        if same_row and close_in_time and center_distance<best_distance:
            best=track;best_distance=center_distance
    if best is None:
        tracks.append([x,y,w,h,max(0.0,sample_time-lead),min(duration,sample_time+trail),sample_time])
    else:
        tx,ty,tw,th,start,end,last=best
        nx,ny=min(x,tx),min(y,ty)
        nr,nb=max(x+w,tx+tw),max(y+h,ty+th)
        best[:]=[nx,ny,nr-nx,nb-ny,start,min(duration,sample_time+trail),sample_time]
frame_area=max(1.0,float(width*height))
reliable=[]
for item in tracks:
    x,y,w,h,start,end,last=item
    persistent=(end-start)>=interval*2.50
    large_title_card=(
        w*h>=frame_area*.015
        and h>=height*.035
        and w<=width*.86
    )
    touches_edge=x<int(width*.04) or x+w>int(width*.96)
    centered_subtitle_at_edge=(
        w>=width*.45
        and height*.58<=(y+h/2.0)<=height*.78
    )
    if (persistent or large_title_card) and (not touches_edge or centered_subtitle_at_edge):
        reliable.append(item)
ranked=sorted(
    reliable,
    key=lambda item:(
        item[2]*item[3]>=frame_area*.015 and item[3]>=height*.035,
        item[5]-item[4],
        item[2]*item[3],
    ),
    reverse=True,
)[:8]
print(json.dumps([[*item[:4],item[4],item[5]] for item in ranked]))
"""
    exe = PY312 if os.path.exists(PY312) else sys.executable
    try:
        res = subprocess.run(
            [
                exe,
                "-c",
                code,
                path,
                str(PRICE_SCAN_INTERVAL_SECONDS),
                str(PRICE_SCAN_MAX_SAMPLES),
                _OCR_PRICE_PATTERN,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=120,
        )
        regions = json.loads(res.stdout or "[]") if res.returncode == 0 else []
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return []
    normalized_regions = [
        (
            *tuple(max(0, int(value)) for value in region[:4]),
            max(0.0, float(region[4])),
            max(0.0, float(region[5])),
        )
        for region in regions
        if isinstance(region, list) and len(region) >= 6
        and int(region[2]) >= 4 and int(region[3]) >= 4
    ]
    if cache_path and res.returncode == 0:
        try:
            os.makedirs(TEXT_CACHE_DIR, exist_ok=True)
            pending_cache = f"{cache_path}.{os.getpid()}.tmp"
            with open(pending_cache, "w", encoding="utf-8") as handle:
                json.dump(normalized_regions, handle, ensure_ascii=False)
            os.replace(pending_cache, cache_path)
        except OSError:
            pass
    return normalized_regions


def _detect_visual_price_regions_batch(paths):
    """Run independent OCR scans concurrently while preserving input order."""
    unique_paths = list(dict.fromkeys(str(path) for path in paths))
    if len(unique_paths) <= 1 or PRICE_SCAN_WORKERS <= 1:
        return {path: detect_visual_price_regions(path) for path in unique_paths}
    workers = min(PRICE_SCAN_WORKERS, len(unique_paths))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        detected = list(executor.map(detect_visual_price_regions, unique_paths))
    return dict(zip(unique_paths, detected))


def _detect_visual_text_regions_batch(paths):
    """Run full-text OCR scans concurrently while preserving input order."""
    unique_paths = list(dict.fromkeys(str(path) for path in paths))
    if len(unique_paths) <= 1 or PRICE_SCAN_WORKERS <= 1:
        return {path: detect_visual_text_regions(path) for path in unique_paths}
    workers = min(PRICE_SCAN_WORKERS, len(unique_paths))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        detected = list(executor.map(detect_visual_text_regions, unique_paths))
    return dict(zip(unique_paths, detected))


def maybe_contains_price(path):
    return bool(detect_visual_price_regions(path))


def _srt_time_seconds(value):
    match = re.fullmatch(r"(\d+):(\d+):(\d+)[,.](\d+)", value.strip())
    if not match:
        return 0.0
    hours_text, minutes_text, seconds_text, fraction_text = match.groups()
    return (
        int(hours_text) * 3600
        + int(minutes_text) * 60
        + int(seconds_text)
        + int(fraction_text) / (10 ** len(fraction_text))
    )


def detect_spoken_price_intervals(source_path):
    """Transcribe one source once and return only price-bearing speech windows."""
    if not ENABLE_PRICE_FILTER or not has_audio_stream(source_path):
        return []
    subtitle_path = transcribe_video_to_srt(
        source_path, f"{os.path.basename(source_path)}价格语音检测"
    )
    if not subtitle_path:
        return []
    try:
        with open(subtitle_path, "r", encoding="utf-8-sig", errors="ignore") as handle:
            content = handle.read()
    except OSError:
        return []
    intervals = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        time_index = next((index for index, line in enumerate(lines) if "-->" in line), -1)
        if time_index < 0:
            continue
        text = "".join(lines[time_index + 1 :])
        if not re.search(_OCR_PRICE_PATTERN, text, re.IGNORECASE):
            continue
        start_text, end_text = (part.strip() for part in lines[time_index].split("-->", 1))
        intervals.append((_srt_time_seconds(start_text), _srt_time_seconds(end_text)))
    return intervals


def classify_compliance_text(text):
    normalized = re.sub(r"\s+", "", str(text or ""))
    violations = []
    if re.search(_OCR_PRICE_PATTERN, normalized, re.IGNORECASE):
        violations.append("price")
    if _EXTREME_COPY_PATTERN.search(normalized):
        violations.append("extreme_claim")
    return violations


def scan_media_compliance(path, sample_seconds=COMPLIANCE_SAMPLE_SECONDS, max_samples=0):
    """OCR the whole timeline at a dense fixed cadence and return timestamped violations."""
    if not FULL_COMPLIANCE_SCAN:
        return []
    if not has_module("pytesseract") or not has_module("cv2"):
        raise RuntimeError("完整合规扫描需要 pytesseract、OpenCV 与中文 OCR 语言包。")
    code = r"""
import cv2, json, os, sys, pytesseract
path=sys.argv[1]
sample_seconds=max(.25,float(sys.argv[2]))
max_samples=max(0,int(sys.argv[3]))
suffix=os.path.splitext(path)[1].lower()
records=[]
if suffix in {'.png','.jpg','.jpeg','.webp','.bmp'}:
    frame=cv2.imread(path)
    if frame is not None:
        text=pytesseract.image_to_string(frame,lang='chi_sim+eng',config='--psm 6')
        records.append({'time':0.0,'text':text})
else:
    cap=cv2.VideoCapture(path)
    fps=float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
    total=int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step=max(1,int(round(fps*sample_seconds)))
    indexes=list(range(0,total,step))
    if total and (not indexes or indexes[-1] != total-1): indexes.append(total-1)
    if max_samples and len(indexes)>max_samples:
        indexes=[indexes[round(i*(len(indexes)-1)/(max_samples-1))] for i in range(max_samples)] if max_samples>1 else [indexes[0]]
    for index in indexes:
        cap.set(cv2.CAP_PROP_POS_FRAMES,index)
        ok,frame=cap.read()
        if not ok: continue
        h,w=frame.shape[:2]
        zones=[frame[:max(1,int(h*.34)),:],frame[max(0,int(h*.64)):,:],frame]
        texts=[]
        for zone in zones:
            gray=cv2.cvtColor(zone,cv2.COLOR_BGR2GRAY)
            texts.append(pytesseract.image_to_string(gray,lang='chi_sim+eng',config='--psm 6'))
        records.append({'time':round(index/fps,3),'text':' '.join(texts)})
    cap.release()
print(json.dumps(records,ensure_ascii=False))
"""
    exe = PY312 if os.path.exists(PY312) else sys.executable
    result = subprocess.run(
        [exe, "-c", code, path, str(sample_seconds), str(max_samples)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "OCR 未知错误").strip()[-500:]
        raise RuntimeError(f"合规扫描失败：{detail}")
    try:
        records = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("合规扫描没有返回有效结果。") from exc
    violations = []
    for record in records:
        text = str(record.get("text") or "").strip()
        for violation_type in classify_compliance_text(text):
            violations.append(
                {
                    "file": os.path.basename(path),
                    "time": round(float(record.get("time") or 0.0), 3),
                    "type": violation_type,
                    "text": re.sub(r"\s+", " ", text)[:160],
                }
            )
    return violations


def filter_compliance_clips(clips, stage_label):
    if not FULL_COMPLIANCE_SCAN:
        return list(clips), []
    kept = []
    violations = []
    for clip in clips:
        found = scan_media_compliance(clip.path, sample_seconds=1.0, max_samples=8)
        removable = [item for item in found if item.get("type") != "price"]
        price_items = [item for item in found if item.get("type") == "price"]
        if price_items:
            violations.extend(
                {**item, "stage": stage_label, "action": "price_cover_verification"}
                for item in price_items
            )
            print(
                f"   -> {stage_label}价格文字遮挡待成片复检，"
                f"保留镜头：{os.path.basename(clip.path)}"
            )
        if removable:
            violations.extend({**item, "stage": stage_label, "action": "clip_removed"} for item in removable)
            print(f"   -> 合规扫描已移除片段：{os.path.basename(clip.path)}")
        else:
            kept.append(clip)
    return kept, violations


def write_compliance_report(status, violations, output_path=COMPLIANCE_REPORT_FILE):
    payload = {
        "status": status,
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "sample_interval_seconds": COMPLIANCE_SAMPLE_SECONDS,
        "violation_count": len(violations),
        "violations": violations,
    }
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return payload


def _face_effect_cache_path(input_path, selected_effect, asset_paths):
    digest = hashlib.sha256()
    # Bump this whenever the embedded renderer/tracker changes so an older
    # flickering face-effect render is never mistaken for a valid cache hit.
    digest.update(b"face-stable-v5-human-only-cover\0")
    digest.update(selected_effect.encode("utf-8"))
    digest.update(f":{SHORT_EFFECT_WINDOW_SECONDS:.3f}".encode("ascii"))
    digest.update(f":{EARLY_EFFECT_SECONDS:.3f}".encode("ascii"))
    digest.update(f":{EARLY_EFFECT_OPACITY:.3f}".encode("ascii"))
    digest.update(os.environ.get("V16_FACE_DETECT_INTERVAL", "3").encode("ascii"))
    digest.update(os.environ.get("V16_FACE_IDLE_DETECT_INTERVAL", "6").encode("ascii"))
    with open(input_path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    for asset_path in asset_paths:
        try:
            stat = os.stat(asset_path)
            digest.update(
                f"{asset_path}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
            )
        except OSError:
            digest.update(str(asset_path).encode("utf-8"))
    cache_dir = os.environ.get(
        "V16_FACE_CACHE_DIR",
        os.path.join(PERSISTENT_CACHE_ROOT, "face"),
    )
    return os.path.join(cache_dir, f"{digest.hexdigest()}.mp4")


_FACE_EFFECT_CACHE_LOCK = threading.RLock()
_FACE_EFFECT_CACHE_NAME_PATTERN = re.compile(r"^[0-9a-f]{64}\.mp4$")
_MIB = 1024 * 1024
_GIB = 1024 * _MIB


def _face_effect_cache_limit_bytes():
    """Return the bounded face-render cache budget for this process."""
    return _bounded_env_int(
        "V16_FACE_CACHE_MAX_BYTES",
        4 * _GIB if sys.platform == "win32" else 8 * _GIB,
        512 * _MIB,
        16 * _GIB,
    )


def _face_effect_cache_entries(cache_dir):
    """List only cache files owned by this feature, never links or user files."""
    entries = []
    try:
        with os.scandir(cache_dir) as directory_entries:
            for entry in directory_entries:
                if not _FACE_EFFECT_CACHE_NAME_PATTERN.fullmatch(entry.name):
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                entries.append((stat.st_mtime_ns, entry.name, entry.path, stat.st_size))
    except OSError:
        return []
    return entries


def _prune_face_effect_cache(cache_dir, incoming_bytes=0, protected_path=""):
    """Evict oldest cache entries until a new atomic write fits the budget."""
    try:
        incoming_bytes = max(0, int(incoming_bytes))
    except (TypeError, ValueError):
        return False
    limit_bytes = _face_effect_cache_limit_bytes()
    if incoming_bytes > limit_bytes:
        return False
    protected_path = os.path.abspath(protected_path) if protected_path else ""
    with _FACE_EFFECT_CACHE_LOCK:
        entries = _face_effect_cache_entries(cache_dir)
        total_bytes = sum(entry[3] for entry in entries)
        protected_size = sum(
            entry[3]
            for entry in entries
            if protected_path and os.path.abspath(entry[2]) == protected_path
        )
        projected_bytes = total_bytes - protected_size + incoming_bytes
        for _mtime_ns, _name, path, size in sorted(entries):
            if projected_bytes <= limit_bytes:
                break
            if protected_path and os.path.abspath(path) == protected_path:
                continue
            try:
                os.unlink(path)
            except OSError:
                continue
            projected_bytes -= size
        return projected_bytes <= limit_bytes


def _store_face_effect_cache(output_path, cache_path):
    """Atomically cache a completed render without allowing unbounded growth."""
    try:
        output_size = os.path.getsize(output_path)
    except OSError:
        return False
    cache_dir = os.path.dirname(cache_path)
    pending_path = ""
    with _FACE_EFFECT_CACHE_LOCK:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            # A cache-named symlink or special file is not ours to replace.
            if os.path.lexists(cache_path) and (
                os.path.islink(cache_path) or not os.path.isfile(cache_path)
            ):
                return False
            if not _prune_face_effect_cache(
                cache_dir,
                incoming_bytes=output_size,
                protected_path=cache_path,
            ):
                return False
            descriptor, pending_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(cache_path)}.",
                suffix=".partial",
                dir=cache_dir,
            )
            os.close(descriptor)
            shutil.copyfile(output_path, pending_path)
            os.replace(pending_path, cache_path)
            pending_path = ""
            try:
                os.utime(cache_path, None)
            except OSError:
                pass
            _prune_face_effect_cache(
                cache_dir,
                incoming_bytes=output_size,
                protected_path=cache_path,
            )
            return True
        except OSError:
            return False
        finally:
            if pending_path:
                try:
                    os.unlink(pending_path)
                except OSError:
                    pass


def _restore_face_effect_cache(cache_path, output_path):
    """Copy a valid cache hit and refresh its mtime as the LRU signal."""
    try:
        if os.path.islink(cache_path) or not os.path.isfile(cache_path):
            return False
        if os.path.getsize(cache_path) <= 1024:
            return False
        shutil.copyfile(cache_path, output_path)
        try:
            os.utime(cache_path, None)
        except OSError:
            pass
        return True
    except OSError:
        return False


def apply_face_effect(input_path, output_path, selected_effect=None):
    global _ENCODER_FALLBACK_COUNT
    selected_effect = (selected_effect or FACE_EFFECT).lower()
    if selected_effect not in {"sunglasses", "sticker_sunglasses", "phone_cover", "blur", "mosaic", "sparkle", "blush", "neon", "old"}:
        shutil.copyfile(input_path, output_path)
        return
    if not has_module("cv2"):
        shutil.copyfile(input_path, output_path)
        return
    sunglasses_asset_path = os.environ.get(
        "SUNGLASSES_ASSET_PATH",
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "assets",
            "natural_wear_glasses_v7.png",
        ),
    )
    age_texture_path = os.environ.get(
        "AGE_TEXTURE_PATH",
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "assets",
            "age_luma_texture_v41.png",
        ),
    )
    age_reference_path = os.environ.get(
        "AGE_REFERENCE_PATH",
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "assets",
            "age_canonical_residual_v50.png",
        ),
    )
    face_landmarker_model_path = os.environ.get(
        "FACE_LANDMARKER_MODEL_PATH",
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "assets",
            "face_landmarker.task",
        ),
    )
    haarcascade_path = os.environ.get(
        "OPENCV_HAAR_CASCADE_PATH",
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "assets",
            "haarcascade_frontalface_default.xml",
        ),
    )
    eye_cascade_path = os.environ.get(
        "OPENCV_EYE_CASCADE_PATH",
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "assets",
            "haarcascade_eye_tree_eyeglasses.xml",
        ),
    )
    cache_path = _face_effect_cache_path(
        input_path,
        selected_effect,
        (
            sunglasses_asset_path,
            age_texture_path,
            age_reference_path,
            face_landmarker_model_path,
            haarcascade_path,
            eye_cascade_path,
        ),
    )
    if _restore_face_effect_cache(cache_path, output_path):
        print(f"   -> 人脸特效复用缓存: {selected_effect}")
        return
    code = r"""
import cv2, os, sys, tempfile, subprocess, math, time
import numpy as np
try:
    import mediapipe as mp
except Exception:
    mp = None
try:
    from insightface.app import FaceAnalysis
    import onnxruntime as ort
except Exception:
    FaceAnalysis = None
    ort = None
src, dst, effect = sys.argv[1], sys.argv[2], sys.argv[3]
sunglasses_asset_path = sys.argv[4] if len(sys.argv) > 4 else ""
age_texture_path = sys.argv[5] if len(sys.argv) > 5 else ""
age_reference_path = sys.argv[6] if len(sys.argv) > 6 else ""
face_landmarker_model_path = sys.argv[7] if len(sys.argv) > 7 else ""
haarcascade_path = sys.argv[8] if len(sys.argv) > 8 else ""
eye_cascade_path = sys.argv[9] if len(sys.argv) > 9 else ""
short_effect_window_seconds = float(sys.argv[10]) if len(sys.argv) > 10 else 1.0
early_effect_seconds = float(sys.argv[11]) if len(sys.argv) > 11 else 30.0
early_effect_opacity = float(sys.argv[12]) if len(sys.argv) > 12 else 0.30
encoder_mode = sys.argv[13] if len(sys.argv) > 13 else "libx264"
try:
    ffmpeg_threads = max(0, min(12, int(sys.argv[14]))) if len(sys.argv) > 14 else 0
except (TypeError, ValueError):
    ffmpeg_threads = 0
allow_hardware_fallback = len(sys.argv) <= 15 or sys.argv[15] == "1"
try:
    nvidia_gpu_index = max(0, min(15, int(sys.argv[16]))) if len(sys.argv) > 16 else 0
except (TypeError, ValueError):
    nvidia_gpu_index = 0
sunglasses_master = None
if sunglasses_asset_path and os.path.exists(sunglasses_asset_path):
    try:
        encoded_asset = np.fromfile(sunglasses_asset_path, dtype=np.uint8)
        sunglasses_master = cv2.imdecode(encoded_asset, cv2.IMREAD_UNCHANGED)
        if sunglasses_master is None or sunglasses_master.ndim != 3 or sunglasses_master.shape[2] != 4:
            sunglasses_master = None
    except Exception:
        sunglasses_master = None
age_texture_master = None
if age_texture_path and os.path.exists(age_texture_path):
    try:
        encoded_age = np.fromfile(age_texture_path,dtype=np.uint8)
        age_texture_master = cv2.imdecode(encoded_age,cv2.IMREAD_UNCHANGED)
        if age_texture_master is None or age_texture_master.ndim != 3 or age_texture_master.shape[2] != 4:
            age_texture_master = None
    except Exception:
        age_texture_master = None
age_reference_master = None
if age_reference_path and os.path.exists(age_reference_path):
    try:
        encoded_reference = np.fromfile(age_reference_path,dtype=np.uint8)
        age_reference_master = cv2.imdecode(encoded_reference,cv2.IMREAD_UNCHANGED)
        if age_reference_master is None or age_reference_master.ndim != 3 or age_reference_master.shape[2] != 4:
            age_reference_master = None
    except Exception:
        age_reference_master = None
cap=cv2.VideoCapture(src)
fps=cap.get(cv2.CAP_PROP_FPS) or 25
active_detection_interval=max(1, min(6, int(os.environ.get("V16_FACE_DETECT_INTERVAL", "3"))))
idle_detection_interval=max(
    active_detection_interval,
    min(12, int(os.environ.get("V16_FACE_IDLE_DETECT_INTERVAL", "6"))),
)
w=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
h=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
tmp=tempfile.NamedTemporaryFile(delete=False, suffix='.mp4').name
fourcc=cv2.VideoWriter_fourcc(*'mp4v')
out=cv2.VideoWriter(tmp, fourcc, fps, (w,h))
default_cascade_path = ""
try:
    default_cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
except Exception:
    default_cascade_path = ""
cascade_candidates = [haarcascade_path, default_cascade_path]
cascade = cv2.CascadeClassifier()
for candidate in cascade_candidates:
    if candidate and os.path.exists(candidate):
        loaded = cascade.load(candidate)
        if loaded and not cascade.empty():
            break
eye_cascade = cv2.CascadeClassifier()
if eye_cascade_path and os.path.exists(eye_cascade_path):
    eye_cascade.load(eye_cascade_path)
face_mesh = None
if mp is not None and hasattr(mp, "solutions") and hasattr(mp.solutions, "face_mesh"):
    face_mesh = mp.solutions.face_mesh.FaceMesh(
        static_image_mode=False,
        max_num_faces=3,
        refine_landmarks=True,
        min_detection_confidence=0.35,
        min_tracking_confidence=0.55,
    )
face_landmarker = None
if (
    mp is not None
    and hasattr(mp,"tasks")
    and hasattr(mp.tasks,"vision")
    and face_landmarker_model_path
    and os.path.exists(face_landmarker_model_path)
    and effect == "old"
    and FaceAnalysis is None
):
    try:
        landmarker_options=mp.tasks.vision.FaceLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(
                model_asset_path=face_landmarker_model_path,
                delegate=mp.tasks.BaseOptions.Delegate.CPU,
            ),
            running_mode=mp.tasks.vision.RunningMode.VIDEO,
            num_faces=3,
            min_face_detection_confidence=.52,
            min_face_presence_confidence=.52,
            min_tracking_confidence=.55,
        )
        face_landmarker=mp.tasks.vision.FaceLandmarker.create_from_options(landmarker_options)
    except Exception:
        face_landmarker=None
face_app = None
if FaceAnalysis is not None:
    provider_options = []
    if ort is not None and "CoreMLExecutionProvider" in ort.get_available_providers():
        provider_options.append(["CoreMLExecutionProvider", "CPUExecutionProvider"])
    provider_options.append(["CPUExecutionProvider"])
    for providers in provider_options:
        try:
            face_app = FaceAnalysis(
                name="buffalo_l",
                root=os.environ.get("V16_INSIGHTFACE_ROOT", "~/.insightface"),
                allowed_modules=["detection"],
                providers=providers,
            )
            # Product fittings and hands can score like weak faces. Keep only
            # high-confidence detections for sticker-style effects; missing a
            # marginal face is better than putting glasses on the drain pipe.
            detector_threshold = .32 if effect in {"sticker_sunglasses", "phone_cover", "sunglasses"} else .50
            face_app.prepare(ctx_id=-1, det_size=(640, 640), det_thresh=detector_threshold)
            break
        except Exception:
            face_app = None

def alpha_blend(frame, sticker, x, y):
    sh, sw = sticker.shape[:2]
    if sw <= 0 or sh <= 0:
        return
    x1=max(0,x); y1=max(0,y); x2=min(frame.shape[1],x+sw); y2=min(frame.shape[0],y+sh)
    if x1 >= x2 or y1 >= y2:
        return
    sx1=x1-x; sy1=y1-y; sx2=sx1+(x2-x1); sy2=sy1+(y2-y1)
    rgb=sticker[sy1:sy2, sx1:sx2, :3].astype(np.float32)
    alpha=sticker[sy1:sy2, sx1:sx2, 3:4].astype(np.float32)/255.0
    roi=frame[y1:y2, x1:x2].astype(np.float32)
    frame[y1:y2, x1:x2]=(rgb*alpha + roi*(1-alpha)).astype(np.uint8)

def rotate_rgba(sticker, angle):
    if abs(angle) < 1:
        return sticker
    sh, sw = sticker.shape[:2]
    m = cv2.getRotationMatrix2D((sw/2, sh/2), angle, 1.0)
    cos = abs(m[0,0]); sin = abs(m[0,1])
    nw = int((sh*sin) + (sw*cos))
    nh = int((sh*cos) + (sw*sin))
    m[0,2] += nw/2 - sw/2
    m[1,2] += nh/2 - sh/2
    return cv2.warpAffine(sticker, m, (nw, nh), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(0,0,0,0))

def render_sunglasses(frame, face):
    eye_mid=np.array(face["eye_mid"], dtype=np.float32)
    eye_dist=max(1.0, float(face["eye_dist"]))
    x,y,fw,fh=face.get("box", (0,0,eye_dist/.38,eye_dist/.38))
    fit_eye_dist=max(eye_dist, float(fw)*.39)
    angle=float(face.get("angle", 0.0))
    yaw=float(np.clip(face.get("yaw", 0.0), -.65, .65))
    opacity=float(np.clip(face.get("opacity", 1.0), 0.0, 1.0))

    if sunglasses_master is None:
        sw=max(48, int(min(w,h)*.065), int(eye_dist*2.28))
        sh=max(24, int(sw*.27))
        sticker=rotate_rgba(make_sunglasses(sw, sh), -angle)
        alpha_blend(frame, sticker, int(eye_mid[0]-sticker.shape[1]/2), int(eye_mid[1]-sticker.shape[0]*.51))
        return

    # Natural eye asymmetry can report a small roll even when the head is
    # upright. Damp only tiny angles so the glasses do not look accidentally
    # crooked while preserving real head turns.
    render_angle=angle*.62 if abs(angle)<4.5 else angle
    theta=math.radians(render_angle)
    axis=np.array([math.cos(theta), math.sin(theta)], dtype=np.float32)
    down=np.array([-axis[1], axis[0]], dtype=np.float32)
    # Seat the bridge on the nose instead of letting the frame hover above it.
    center=eye_mid + down*(fit_eye_dist*.050) + axis*(yaw*fit_eye_dist*.035)
    # Preserve the approved oversized frame. Some detectors under-report the
    # eye distance on narrow or far-away faces, so face width provides a floor.
    half_w=fit_eye_dist*1.20
    half_h=fit_eye_dist*.540

    left_w=1.0 - .28*max(yaw,0.0) + .08*max(-yaw,0.0)
    right_w=1.0 - .28*max(-yaw,0.0) + .08*max(yaw,0.0)
    left_h=1.0 - .20*max(yaw,0.0) + .05*max(-yaw,0.0)
    right_h=1.0 - .20*max(-yaw,0.0) + .05*max(yaw,0.0)

    top_left=center-axis*(half_w*left_w)-down*(half_h*left_h)
    top_right=center+axis*(half_w*right_w)-down*(half_h*right_h)
    bottom_right=center+axis*(half_w*right_w)+down*(half_h*right_h)
    bottom_left=center-axis*(half_w*left_w)+down*(half_h*left_h)
    target=np.float32([top_left, top_right, bottom_right, bottom_left])

    margin=max(5, int(fit_eye_dist*.14))
    x1=max(0, int(math.floor(target[:,0].min()))-margin)
    y1=max(0, int(math.floor(target[:,1].min()))-margin)
    x2=min(w, int(math.ceil(target[:,0].max()))+margin)
    y2=min(h, int(math.ceil(target[:,1].max()))+margin)
    if x1>=x2 or y1>=y2:
        return

    ah,aw=sunglasses_master.shape[:2]
    source=np.float32([[0,0],[aw-1,0],[aw-1,ah-1],[0,ah-1]])
    local_target=target-np.array([x1,y1], dtype=np.float32)
    transform=cv2.getPerspectiveTransform(source, local_target)
    roi_w=x2-x1; roi_h=y2-y1

    local_face=frame[max(0,int(eye_mid[1]-fit_eye_dist)):min(h,int(eye_mid[1]+fit_eye_dist)), max(0,int(eye_mid[0]-fit_eye_dist)):min(w,int(eye_mid[0]+fit_eye_dist))]
    source_rgb=sunglasses_master[:,:,:3].astype(np.float32)
    source_alpha=sunglasses_master[:,:,3].astype(np.float32)/255.0
    source_gray=cv2.cvtColor(sunglasses_master[:,:,:3], cv2.COLOR_BGR2GRAY)

    # Remove bright matte pixels along a generated cutout's alpha boundary.
    # The operation is limited to the outer band, so lens text/highlights stay.
    silhouette=(source_alpha>.04).astype(np.uint8)
    inner=cv2.erode(silhouette, np.ones((9,9),np.uint8), iterations=1)
    outer_band=(silhouette>0)&(inner==0)
    fringe=outer_band&(source_alpha<.985)&(source_gray>85)
    if np.any(fringe):
        fringe_gain=np.clip(68.0/np.maximum(source_gray[fringe].astype(np.float32),1.0), .18, 1.0)
        source_rgb[fringe]*=fringe_gain[:,None]

    if local_face.size==0:
        light=145.0
        color_gain=np.ones(3,dtype=np.float32)
    else:
        light=float(cv2.cvtColor(local_face,cv2.COLOR_BGR2GRAY).mean())
        scene_mean=local_face.reshape(-1,3).mean(axis=0).astype(np.float32)
        neutral=max(1.0,float(scene_mean.mean()))
        color_gain=np.clip(np.power(np.clip(scene_mean/neutral,.75,1.25),.28),.94,1.06)
    tone=float(np.clip(.92+light/255.0*.16,.92,1.08))

    yaw_fade=float(np.clip((.78-abs(yaw))/.18, .45, 1.0))
    source_alpha*=opacity*yaw_fade
    premult=source_rgb*color_gain[None,None,:]*source_alpha[:,:,None]*tone
    warped_alpha=cv2.warpPerspective(source_alpha, transform, (roi_w,roi_h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    warped_premult=cv2.warpPerspective(premult, transform, (roi_w,roi_h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)

    # A subpixel optical soft edge removes the cut-paper boundary while the
    # premultiplied color remains synchronized with alpha.
    edge_sigma=float(np.clip(fit_eye_dist*.0025,.35,.70))
    warped_alpha=cv2.GaussianBlur(warped_alpha,(0,0),edge_sigma)
    warped_premult=cv2.GaussianBlur(warped_premult,(0,0),edge_sigma)

    shadow_shift=max(1, int(round(fit_eye_dist*.018)))
    shadow_matrix=np.float32([[1,0,0],[0,1,shadow_shift]])
    shifted_alpha=cv2.warpAffine(warped_alpha, shadow_matrix, (roi_w,roi_h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    shadow=cv2.GaussianBlur(shifted_alpha, (0,0), max(.9, fit_eye_dist*.016))
    shadow_alpha=np.clip((shadow-warped_alpha*.88)*.34, 0.0, .13)

    # A short bridge shadow makes the frame sit on the nose instead of float.
    contact=np.zeros_like(warped_alpha)
    contact_center=(int(round(center[0]-x1)),int(round(center[1]-y1+half_h*.12)))
    cv2.ellipse(contact,contact_center,(max(2,int(fit_eye_dist*.09)),max(1,int(fit_eye_dist*.030))),-render_angle,0,360,.14,-1,cv2.LINE_AA)
    # Two very soft nose-pad contacts make the frame feel supported by skin.
    for side in (-1.0, 1.0):
        pad=center + axis*(side*fit_eye_dist*.095) + down*(half_h*.17)
        cv2.ellipse(contact,(int(round(pad[0]-x1)),int(round(pad[1]-y1))),(max(1,int(fit_eye_dist*.035)),max(1,int(fit_eye_dist*.020))),-render_angle,0,360,.10,-1,cv2.LINE_AA)
    contact=cv2.GaussianBlur(contact,(0,0),max(.8,fit_eye_dist*.012))
    shadow_alpha=np.maximum(shadow_alpha,contact*(1.0-warped_alpha))

    roi=frame[y1:y2,x1:x2].astype(np.float32)
    roi*=1.0-shadow_alpha[:,:,None]
    roi=warped_premult + roi*(1.0-warped_alpha[:,:,None])
    frame[y1:y2,x1:x2]=np.clip(roi,0,255).astype(np.uint8)

def rounded_rect(mask, p1, p2, radius, value):
    x1,y1=p1; x2,y2=p2
    cv2.rectangle(mask, (x1+radius,y1), (x2-radius,y2), value, -1)
    cv2.rectangle(mask, (x1,y1+radius), (x2,y2-radius), value, -1)
    for cx,cy in [(x1+radius,y1+radius),(x2-radius,y1+radius),(x1+radius,y2-radius),(x2-radius,y2-radius)]:
        cv2.circle(mask, (cx,cy), radius, value, -1)

def make_sunglasses(sw, sh):
    sticker=np.zeros((sh, sw, 4), dtype=np.uint8)
    mask=np.zeros((sh, sw), dtype=np.uint8)
    pad=max(5, sw//30)
    gap=max(6, sw//18)
    lens_w=(sw-gap-pad*2)//2
    lens_h=max(20, int(sh*.72))
    top=max(1, int(sh*.10))
    left_c=(pad+lens_w//2, top+lens_h//2)
    right_c=(pad+lens_w+gap+lens_w//2, top+lens_h//2)
    axes=(max(13,lens_w//2), max(10,lens_h//2))
    shadow=np.zeros_like(mask)
    cv2.ellipse(shadow, (left_c[0]+4,left_c[1]+5), axes, -7, 0, 360, 105, -1)
    cv2.ellipse(shadow, (right_c[0]+4,right_c[1]+5), axes, 7, 0, 360, 105, -1)
    sticker[:,:,3]=np.maximum(sticker[:,:,3], cv2.GaussianBlur(shadow, (0,0), max(2, sw//60)))
    cv2.ellipse(mask, left_c, axes, -9, 0, 360, 216, -1)
    cv2.ellipse(mask, right_c, axes, 9, 0, 360, 216, -1)
    yy=np.linspace(0,1,sh)[:,None]
    xx=np.linspace(0,1,sw)[None,:]
    lens_tint=np.zeros((sh,sw,3), dtype=np.uint8)
    lens_tint[:,:,0]=(10 + 22*yy).astype(np.uint8)
    lens_tint[:,:,1]=(12 + 24*yy).astype(np.uint8)
    lens_tint[:,:,2]=(16 + 30*xx).astype(np.uint8)
    sticker[mask>0, :3]=lens_tint[mask>0]
    sticker[:,:,3]=np.maximum(sticker[:,:,3], cv2.GaussianBlur(mask, (0,0), .7))
    outline=max(2, sw//55)
    cv2.ellipse(sticker, left_c, axes, -9, 0, 360, (5,7,9,236), outline)
    cv2.ellipse(sticker, right_c, axes, 9, 0, 360, (5,7,9,236), outline)
    bridge_y=top+lens_h//2
    cv2.line(sticker, (left_c[0]+axes[0]-1, bridge_y), (right_c[0]-axes[0]+1, bridge_y), (5,6,8,224), max(2, sw//42))
    cv2.line(sticker, (pad+3, bridge_y-1), (max(0,pad-sw//18), bridge_y-sw//22), (7,8,10,116), max(1, sw//70))
    cv2.line(sticker, (sw-pad-3, bridge_y-1), (min(sw-1,sw-pad+sw//18), bridge_y-sw//22), (7,8,10,116), max(1, sw//70))
    cv2.line(sticker, (left_c[0]-axes[0]//2, top+lens_h//4), (left_c[0]+axes[0]//4, top+lens_h//5), (255,255,255,95), max(1, sw//95))
    cv2.line(sticker, (right_c[0]-axes[0]//2, top+lens_h//4), (right_c[0]+axes[0]//4, top+lens_h//5), (255,255,255,80), max(1, sw//95))
    
    # 模拟真实墨镜镜腿 (连接眼睛外侧向耳后延伸)
    temple_thick = max(2, sw//35)
    temple_y = bridge_y - sw//20
    # 左镜腿
    cv2.line(sticker, (pad, bridge_y-2), (0, temple_y), (25, 25, 25, 255), temple_thick)
    # 右镜腿
    cv2.line(sticker, (sw-pad, bridge_y-2), (sw, temple_y), (25, 25, 25, 255), temple_thick)
    
    # 添加“I DON'T CARE”字样
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = max(0.24, sw / 520.0)
    font_thick = max(1, sw // 180)
    text1 = "I DON'T"
    text2 = "CARE"
    
    # 获取文字大小以便居中
    (w1, h1), _ = cv2.getTextSize(text1, font, font_scale, font_thick)
    (w2, h2), _ = cv2.getTextSize(text2, font, font_scale, font_thick)
    
    # 在左镜片写 I DON'T
    txt_x1 = left_c[0] - w1 // 2
    txt_y1 = left_c[1] + h1 // 2
    cv2.putText(sticker, text1, (txt_x1, txt_y1), font, font_scale, (255, 255, 255, 240), font_thick, cv2.LINE_AA)
    
    # 在右镜片写 CARE
    txt_x2 = right_c[0] - w2 // 2
    txt_y2 = right_c[1] + h2 // 2
    cv2.putText(sticker, text2, (txt_x2, txt_y2), font, font_scale, (255, 255, 255, 240), font_thick, cv2.LINE_AA)

    sticker[:,:,3]=cv2.GaussianBlur(sticker[:,:,3], (0,0), .35)
    return sticker

def make_phone(sw, sh):
    sticker=np.zeros((sh, sw, 4), dtype=np.uint8)
    radius=max(10, sw//12)
    shadow=np.zeros((sh, sw), dtype=np.uint8)
    rounded_rect(shadow, (9,8), (sw-2,sh-2), radius, 105)
    sticker[:,:,3]=cv2.GaussianBlur(shadow, (0,0), max(2, sw//42))
    body=np.zeros((sh, sw), dtype=np.uint8)
    rounded_rect(body, (2,0), (sw-10,sh-10), radius, 255)
    sticker[body>0, :3]=(24,198,245)
    sticker[:,:,3]=np.maximum(sticker[:,:,3], body)
    glare=np.zeros((sh, sw), dtype=np.uint8)
    cv2.line(glare, (max(8,sw//10), max(7,sh//5)), (sw-max(14,sw//7), max(10,sh//3)), 110, max(3, sw//32))
    sticker[glare>0, :3]=(85,218,255)
    sticker[:,:,3]=np.maximum(sticker[:,:,3], (glare*.55).astype(np.uint8))
    eye_y=max(16, int(sh*.46))
    eye_w=max(14, sw//8)
    cv2.ellipse(sticker, (int(sw*.37), eye_y), (eye_w, max(5,sh//10)), 0, 0, 180, (32,40,48,255), max(2, sw//42))
    cv2.ellipse(sticker, (int(sw*.66), eye_y), (eye_w, max(5,sh//10)), 0, 0, 180, (32,40,48,255), max(2, sw//42))
    cv2.circle(sticker, (sw-22, max(12, sh//4)), max(4, sw//24), (23,128,185,255), -1)
    cv2.circle(sticker, (sw-22, max(12, sh//4)), max(2, sw//48), (190,245,255,255), -1)
    pts=np.array([[2,0],[sw-11,3],[sw-5,sh-9],[0,sh-15]], np.int32)
    warp=np.zeros((sh,sw), dtype=np.uint8)
    cv2.polylines(sticker, [pts], True, (3,5,8,210), max(2, sw//38))
    return sticker

def draw_wrinkle(frame, p1, p2, color, thickness=1, alpha=.32):
    overlay=frame.copy()
    cv2.line(overlay, p1, p2, color, thickness, cv2.LINE_AA)
    cv2.addWeighted(overlay, alpha, frame, 1-alpha, 0, frame)

old_texture_master=None

def apply_cover_reference_age(frame,face):
    # Transfer the approved cover's real age relief in canonical face space.
    if age_reference_master is None:
        return False
    lms=face.get("lms")
    if lms is not None and len(lms)>374:
        def landmark_mean(indices):
            points=np.float32([[lms[index].x*w,lms[index].y*h] for index in indices])
            return points.mean(axis=0)
        source_points=np.float32([
            landmark_mean([33,133,159,145]),
            landmark_mean([362,263,386,374]),
            [lms[1].x*w,lms[1].y*h],
            [lms[61].x*w,lms[61].y*h],
            [lms[291].x*w,lms[291].y*h],
        ])
    elif all(face.get(key) is not None for key in ("left_eye","right_eye","nose_point","mouth_left","mouth_right")):
        source_points=np.float32([
            face["left_eye"],face["right_eye"],face["nose_point"],face["mouth_left"],face["mouth_right"],
        ])
    else:
        return False

    canonical_points=np.float32([[166,190],[346,190],[256,270],[198,340],[314,340]])
    transform,_=cv2.estimateAffinePartial2D(source_points,canonical_points,method=cv2.LMEDS)
    if transform is None:
        return False
    canonical=cv2.warpAffine(
        frame,transform,(512,512),flags=cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT_101,
    )

    # Match the existing product rule: any cloth mask across the lower face
    # disables the entire aging effect. This prevents texture on the mask.
    source_hsv=cv2.cvtColor(canonical,cv2.COLOR_BGR2HSV).astype(np.float32)
    source_lab=cv2.cvtColor(canonical,cv2.COLOR_BGR2LAB).astype(np.float32)
    forehead=np.zeros((512,512),dtype=np.uint8)
    cv2.ellipse(forehead,(256,142),(54,25),0,0,360,255,-1,cv2.LINE_AA)
    lower=np.zeros((512,512),dtype=np.uint8)
    cv2.ellipse(lower,(256,326),(92,76),0,0,360,255,-1,cv2.LINE_AA)
    forehead_values=forehead>96
    lower_values=lower>96
    if np.count_nonzero(forehead_values)>24 and np.count_nonzero(lower_values)>24:
        reference_s=float(np.median(source_hsv[:,:,1][forehead_values]))
        reference_a=float(np.median(source_lab[:,:,1][forehead_values]))
        reference_b=float(np.median(source_lab[:,:,2][forehead_values]))
        chroma_distance=np.sqrt((source_lab[:,:,1]-reference_a)**2+(source_lab[:,:,2]-reference_b)**2)
        saturation_floor=max(9.0,reference_s*.38)
        saturation_gate=np.clip((source_hsv[:,:,1]-saturation_floor)/max(8.0,reference_s*.42),0.0,1.0)
        chroma_gate=np.clip((18.0-chroma_distance)/10.0,0.0,1.0)
        skin_gate=np.minimum(saturation_gate,chroma_gate)
        visible_fraction=float(np.mean(skin_gate[lower_values]>.34))
        lower_s=float(np.median(source_hsv[:,:,1][lower_values]))
        lower_face_covered=visible_fraction<.31 or lower_s<max(12.0,reference_s*.52)
        if lower_face_covered:
            return True

    reference=age_reference_master
    if reference.shape[:2]!=(512,512):
        reference=cv2.resize(reference,(512,512),interpolation=cv2.INTER_LINEAR)
    age_alpha=reference[:,:,3].astype(np.float32)/255.0

    # The cover contains a true lower-face volume change. Recreate it by
    # moving the subject's own pixels in canonical space, never by replacing
    # their identity or painting a differently coloured face.
    yy,xx=np.mgrid[0:512,0:512].astype(np.float32)
    rel_x=(xx-256.0)/180.0
    rel_y=(yy-190.0)/180.0
    lower_gate=np.clip((rel_y-.48)/.32,0.0,1.0)*np.clip((1.72-rel_y)/.34,0.0,1.0)
    left_jowl=np.exp(-(((rel_x+.48)/.34)**2+((rel_y-1.05)/.42)**2))
    right_jowl=np.exp(-(((rel_x-.53)/.31)**2+((rel_y-1.10)/.38)**2))
    centre_sag=np.exp(-((rel_x/.52)**2+((rel_y-.98)/.48)**2))
    outward=(8.0*left_jowl+5.5*right_jowl)*np.where(rel_x<0,-1.0,1.0)
    downward=6.0*centre_sag+3.5*left_jowl+2.5*right_jowl
    relaxed=cv2.remap(
        canonical,(xx-outward).astype(np.float32),(yy-downward).astype(np.float32),
        cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT_101,
    ).astype(np.float32)
    geometry_alpha=np.clip(age_alpha*lower_gate*.68,0.0,.68)[:,:,None]
    canonical_f=canonical.astype(np.float32)*(1.0-geometry_alpha)+relaxed*geometry_alpha

    canonical_lab=cv2.cvtColor(np.clip(canonical_f,0,255).astype(np.uint8),cv2.COLOR_BGR2LAB).astype(np.float32)
    residual=(reference[:,:,0].astype(np.float32)-128.0)/3.0
    # Keep the second approved preview's broad tone and face placement. Only
    # lift the fine/mid-frequency wrinkle component a little; broad shading is
    # unchanged so the face cannot drift to a different colour or exposure.
    broad_residual=cv2.GaussianBlur(residual,(0,0),9.0)
    wrinkle_residual=residual-broad_residual
    change=(broad_residual+wrinkle_residual*1.50)*age_alpha
    weight=float(np.sum(age_alpha))
    if weight>1e-4:
        change-=float(np.sum(change)/weight)*age_alpha

    # Add three broken, unequal forehead folds. A skin-colour/brightness gate
    # rejects dark hair, while paired highlights keep the marks dimensional
    # rather than looking like lines drawn over the face.
    forehead_region=np.zeros((512,512),dtype=np.float32)
    cv2.ellipse(forehead_region,(256,151),(108,39),0,0,360,1.0,-1,cv2.LINE_AA)
    forehead_sample=np.zeros((512,512),dtype=np.uint8)
    cv2.ellipse(forehead_sample,(256,142),(48,20),0,0,360,255,-1,cv2.LINE_AA)
    sample_values=forehead_sample>96
    sample_a=float(np.median(canonical_lab[:,:,1][sample_values]))
    sample_b=float(np.median(canonical_lab[:,:,2][sample_values]))
    sample_l=float(np.median(canonical_lab[:,:,0][sample_values]))
    forehead_chroma=np.sqrt((canonical_lab[:,:,1]-sample_a)**2+(canonical_lab[:,:,2]-sample_b)**2)
    forehead_skin=np.clip((22.0-forehead_chroma)/12.0,0.0,1.0)
    forehead_skin*=np.clip((canonical_lab[:,:,0]-(sample_l-48.0))/25.0,0.0,1.0)
    forehead_alpha=cv2.GaussianBlur(forehead_region*forehead_skin,(0,0),2.0)
    fold_shadow=np.zeros((512,512),dtype=np.float32)
    fold_light=np.zeros((512,512),dtype=np.float32)
    fold_specs=(
        (np.array([[175,143],[199,141],[224,143]],np.int32),.74),
        (np.array([[238,149],[271,147],[306,150],[333,148]],np.int32),.58),
        (np.array([[194,161],[228,159],[263,162],[291,160]],np.int32),.46),
    )
    for points,strength in fold_specs:
        cv2.polylines(fold_shadow,[points.reshape((-1,1,2))],False,strength,2,cv2.LINE_AA)
        lit=points.copy(); lit[:,1]+=2
        cv2.polylines(fold_light,[lit.reshape((-1,1,2))],False,strength*.52,1,cv2.LINE_AA)
    fold_shadow=cv2.GaussianBlur(fold_shadow,(0,0),1.15)
    fold_light=cv2.GaussianBlur(fold_light,(0,0),1.0)
    change+=(-fold_shadow*7.0+fold_light*3.0)*forehead_alpha
    change=np.clip(change,-35.0,25.0)
    canonical_lab[:,:,0]=np.clip(canonical_lab[:,:,0]+change,0,255)
    aged=cv2.cvtColor(canonical_lab.astype(np.uint8),cv2.COLOR_LAB2BGR)

    inverse=cv2.invertAffineTransform(transform)
    aged_frame=cv2.warpAffine(
        aged,inverse,(w,h),flags=cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT,borderValue=0,
    ).astype(np.float32)
    output_alpha=np.maximum(age_alpha,forehead_alpha*.82)
    warped_alpha=cv2.warpAffine(
        output_alpha,inverse,(w,h),flags=cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT,borderValue=0,
    )
    opacity=float(np.clip(face.get("opacity",1.0),0.0,1.0))
    blend=np.clip(warped_alpha*opacity,0.0,.96)[:,:,None]
    frame[:]=np.clip(frame.astype(np.float32)*(1.0-blend)+aged_frame*blend,0,255).astype(np.uint8)
    return True

def apply_old_age(frame, face):
    # Natural, feathered age treatment that follows every detected face.
    global old_texture_master
    # The reference layer supplies high-frequency skin texture, but it is not
    # a complete age effect by itself. The previous early return skipped every
    # structural cheek, mouth and jaw pass whenever the reference was present,
    # which is why only fragments of the old-age treatment appeared.
    reference_applied=apply_cover_reference_age(frame,face)
    x,y,fw,fh=face["box"]
    if fw < 36 or fh < 36:
        return
    pad_x=max(4,int(fw*.10)); pad_y=max(4,int(fh*.08))
    x1=max(0,x-pad_x); y1=max(0,y-pad_y)
    x2=min(w,x+fw+pad_x); y2=min(h,y+fh+pad_y)
    if x1>=x2 or y1>=y2:
        return
    roi=frame[y1:y2,x1:x2].copy()
    rh,rw=roi.shape[:2]

    eye_mid=np.array(face.get("eye_mid",(x+fw*.5,y+fh*.39)),dtype=np.float32)
    eye_dist=max(1.0,float(face.get("eye_dist",fw*.38)))
    theta=math.radians(float(face.get("angle",0.0)))
    axis=np.array([math.cos(theta),math.sin(theta)],dtype=np.float32)
    down=np.array([-axis[1],axis[0]],dtype=np.float32)
    left_eye=np.array(face.get("left_eye",eye_mid-axis*(eye_dist*.5)),dtype=np.float32)
    right_eye=np.array(face.get("right_eye",eye_mid+axis*(eye_dist*.5)),dtype=np.float32)
    nose=np.array(face.get("nose_point",eye_mid+down*(eye_dist*.56)),dtype=np.float32)
    mouth_left=np.array(face.get("mouth_left",eye_mid-axis*(eye_dist*.34)+down*(eye_dist*1.22)),dtype=np.float32)
    mouth_right=np.array(face.get("mouth_right",eye_mid+axis*(eye_dist*.34)+down*(eye_dist*1.22)),dtype=np.float32)

    # Grade only an oval face region. This removes the old rectangular color
    # patch around the head and keeps hair/background untouched.
    face_mask=np.zeros((rh,rw),dtype=np.float32)
    face_center=(int(x+fw*.50-x1),int(y+fh*.51-y1))
    face_axes=(max(8,int(fw*.48)),max(10,int(fh*.52)))
    cv2.ellipse(face_mask,face_center,face_axes,0,0,360,1.0,-1,cv2.LINE_AA)
    face_mask=cv2.GaussianBlur(face_mask,(0,0),max(2.0,fw*.045))

    # Occlusion-safe skin mask. A face detector still returns a complete oval
    # when someone is wearing a mask, so the old implementation aged the cloth
    # as if it were skin. Learn the person's colour/saturation from the visible
    # forehead, then protect non-skin pixels and the whole covered lower face.
    source_hsv=cv2.cvtColor(roi,cv2.COLOR_BGR2HSV).astype(np.float32)
    source_lab=cv2.cvtColor(roi,cv2.COLOR_BGR2LAB).astype(np.float32)
    forehead=np.zeros((rh,rw),dtype=np.uint8)
    forehead_point=eye_mid-down*(eye_dist*.48)-np.array([x1,y1],dtype=np.float32)
    cv2.ellipse(forehead,(int(forehead_point[0]),int(forehead_point[1])),(max(3,int(eye_dist*.30)),max(2,int(eye_dist*.16))),-float(face.get("angle",0.0)),0,360,255,-1,cv2.LINE_AA)
    forehead_values=forehead>96
    lower_face_covered=False
    occlusion_keep=np.ones((rh,rw),dtype=np.float32)
    reference_l=165.0
    if np.count_nonzero(forehead_values)>12:
        reference_l=float(np.median(source_lab[:,:,0][forehead_values]))
        reference_s=float(np.median(source_hsv[:,:,1][forehead_values]))
        reference_a=float(np.median(source_lab[:,:,1][forehead_values]))
        reference_b=float(np.median(source_lab[:,:,2][forehead_values]))
        chroma_distance=np.sqrt((source_lab[:,:,1]-reference_a)**2+(source_lab[:,:,2]-reference_b)**2)
        saturation_floor=max(9.0,reference_s*.38)
        saturation_gate=np.clip((source_hsv[:,:,1]-saturation_floor)/max(8.0,reference_s*.42),0.0,1.0)
        chroma_gate=np.clip((18.0-chroma_distance)/10.0,0.0,1.0)
        skin_gate=np.minimum(saturation_gate,chroma_gate)
        skin_gate=cv2.GaussianBlur(skin_gate,(0,0),max(.7,eye_dist*.012))

        mouth_region=np.zeros((rh,rw),dtype=np.uint8)
        mouth_center=eye_mid+down*(eye_dist*.94)-np.array([x1,y1],dtype=np.float32)
        cv2.ellipse(mouth_region,(int(mouth_center[0]),int(mouth_center[1])),(max(4,int(eye_dist*.46)),max(4,int(eye_dist*.43))),-float(face.get("angle",0.0)),0,360,255,-1,cv2.LINE_AA)
        mouth_values=mouth_region>96
        visible_fraction=float(np.mean(skin_gate[mouth_values]>.34)) if np.count_nonzero(mouth_values)>12 else 1.0
        mouth_s=float(np.median(source_hsv[:,:,1][mouth_values])) if np.count_nonzero(mouth_values)>12 else reference_s
        lower_face_covered=visible_fraction<.31 or mouth_s<max(12.0,reference_s*.52)
        if lower_face_covered:
            yy,xx=np.mgrid[0:rh,0:rw].astype(np.float32)
            global_x=xx+x1; global_y=yy+y1
            along_down=(global_x-eye_mid[0])*down[0]+(global_y-eye_mid[1])*down[1]
            # Stop above the top edge of a typical mask. A narrow feather keeps
            # the boundary soft while guaranteeing that cloth receives zero age texture.
            upper_keep=np.clip((eye_dist*.43-along_down)/max(2.0,eye_dist*.13),0.0,1.0)
            occlusion_keep=upper_keep
            skin_gate*=upper_keep
            skin_gate=np.clip(skin_gate*1.35,0.0,1.0)
        else:
            skin_gate=np.clip(skin_gate*2.25,0.0,1.0)
        face_mask*=skin_gate

    # Product rule: aging is an all-or-nothing face effect. If a mask covers
    # the lower face, leave the entire frame untouched for this effect instead
    # of aging the cloth or showing a partial forehead-only treatment.
    if lower_face_covered:
        return

    # The chroma skin gate can be too cautious on pale studio portraits, which
    # makes the aging nearly invisible. Add back a protected central skin oval:
    # bright enough to be face skin, saturated enough to avoid the grey
    # background, and still too restrictive to touch black hair or clothing.
    inner_skin=np.zeros((rh,rw),dtype=np.float32)
    inner_center=(int(x+fw*.50-x1),int(y+fh*.49-y1))
    inner_axes=(max(7,int(fw*.34)),max(9,int(fh*.43)))
    cv2.ellipse(inner_skin,inner_center,inner_axes,0,0,360,1.0,-1,cv2.LINE_AA)
    brightness_gate=np.clip((source_lab[:,:,0]-70.0)/46.0,0.0,1.0)
    saturation_gate=np.clip((source_hsv[:,:,1]-8.0)/28.0,0.0,1.0)
    central_skin=cv2.GaussianBlur(inner_skin*brightness_gate*(.70+.30*saturation_gate),(0,0),max(.9,eye_dist*.012))
    # Never let the pale-skin fallback paint back over a detected cloth mask.
    # This was the exact path that made the mask appear to age in the output.
    central_skin*=occlusion_keep
    face_mask=np.maximum(face_mask,central_skin)
    # Strong sunlight and beauty filters wash out low-contrast age texture.
    # Compensate only on bright uncovered faces; indoor/studio faces keep the
    # approved v41 strength, and covered faces never receive this boost.
    bright_face_boost=float(np.clip((reference_l-158.0)/78.0,0.0,.60))

    # Do not grade, brighten, desaturate or sharpen the whole face. Even a
    # small global value shift becomes an obvious face/neck colour break in
    # bright phone footage. Aging below is therefore luminance-only and local.
    roi_f=roi.astype(np.float32)

    # Apply a real, irregular aging-luminance texture extracted from the
    # approved black-hair reference. Only luminance is transferred, so the
    # person's identity, skin colour and hair colour remain their own.
    if age_texture_master is not None and not reference_applied:
        top_left=eye_mid-axis*(eye_dist*1.36)-down*(eye_dist*.94)
        top_right=eye_mid+axis*(eye_dist*1.36)-down*(eye_dist*.94)
        bottom_right=eye_mid+axis*(eye_dist*1.36)+down*(eye_dist*1.78)
        bottom_left=eye_mid-axis*(eye_dist*1.36)+down*(eye_dist*1.78)
        target=np.float32([top_left,top_right,bottom_right,bottom_left])-np.array([x1,y1],dtype=np.float32)
        ah,aw=age_texture_master.shape[:2]
        source=np.float32([[0,0],[aw-1,0],[aw-1,ah-1],[0,ah-1]])
        transform=cv2.getPerspectiveTransform(source,target)
        encoded_delta=cv2.warpPerspective(age_texture_master[:,:,0].astype(np.float32),transform,(rw,rh),flags=cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT,borderValue=128)
        texture_alpha=cv2.warpPerspective(age_texture_master[:,:,3].astype(np.float32)/255.0,transform,(rw,rh),flags=cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT,borderValue=0)
        encoded_delta=cv2.GaussianBlur(encoded_delta,(0,0),max(.6,eye_dist*.012))
        texture_alpha=cv2.GaussianBlur(texture_alpha,(0,0),max(.8,eye_dist*.018))
        texture_alpha*=face_mask
        delta_l=(encoded_delta-128.0)/3.5
        lab=cv2.cvtColor(np.clip(roi_f,0,255).astype(np.uint8),cv2.COLOR_BGR2LAB).astype(np.float32)
        # The v41 map comes from the new natural reference. Keep it restrained:
        # it supplies irregular pore/crease detail while the broad facial
        # relaxation below supplies age structure. This avoids drawn wrinkles.
        texture_strength=1.72+bright_face_boost*.70
        texture_change=delta_l*texture_alpha*texture_strength
        # The reference texture may contain a small luminance bias. Remove it
        # per face so detail is added without making the whole face lighter or
        # darker than the neck.
        texture_weight=float(np.sum(texture_alpha))
        if texture_weight>1e-4:
            texture_change-=float(np.sum(texture_change)/texture_weight)*texture_alpha
        texture_change=np.clip(texture_change,-8.5,6.5)
        lab[:,:,0]=np.clip(lab[:,:,0]+texture_change,0,255)
        roi_f=cv2.cvtColor(lab.astype(np.uint8),cv2.COLOR_LAB2BGR).astype(np.float32)

    # CapCut-style natural aging: broad, feathered volume changes instead of
    # hand-drawn crease paths. The two sides deliberately use different sizes,
    # positions and opacity so the result never looks mirrored or stamped on.
    depth=np.zeros((rh,rw),dtype=np.float32)
    lift=np.zeros((rh,rw),dtype=np.float32)
    angle=-float(face.get("angle",0.0))

    def local_soft(point):
        return (int(round(point[0]-x1)),int(round(point[1]-y1)))

    eye_specs=((left_eye,-1.0,.23,.34,.115,.28),(right_eye,1.0,.255,.29,.092,.21))
    for eye,side,drop,wide,tall,strength in eye_specs:
        center=eye+down*(eye_dist*drop)+axis*(side*eye_dist*.018)
        cv2.ellipse(
            depth,local_soft(center),
            (max(3,int(eye_dist*wide)),max(2,int(eye_dist*tall))),
            angle+side*4,8,172,strength,-1,cv2.LINE_AA,
        )
        upper=center-down*(eye_dist*.075)
        cv2.ellipse(
            lift,local_soft(upper),
            (max(3,int(eye_dist*(wide*.72))),max(1,int(eye_dist*(tall*.42)))),
            angle+side*3,8,172,strength*.22,-1,cv2.LINE_AA,
        )

    # Wide, soft nasolabial and mouth-corner volume loss. These are intentionally
    # blurred regions, not visible lines, so expressions and identity survive.
    for corner,side,strength,bend in (
        (mouth_left,-1.0,.98,.145),
        (mouth_right,1.0,.72,.082),
    ):
        start=nose+axis*(side*eye_dist*.20)+down*(eye_dist*.12)
        middle=(start+corner)*.5+axis*(side*eye_dist*bend)+down*(eye_dist*.035)
        end=corner+axis*(side*eye_dist*.035)+down*(eye_dist*(.24 if side < 0 else .18))
        pts=np.array([local_soft(start),local_soft(middle),local_soft(end)],dtype=np.int32).reshape((-1,1,2))
        cv2.polylines(depth,[pts],False,strength,max(3,int(eye_dist*.135)),cv2.LINE_AA)

        # A displaced soft highlight beside each shadow makes a rounded fold
        # instead of a dark drawn line. The two sides deliberately differ.
        highlight_shift=axis*(-side*eye_dist*(.045 if side < 0 else .032))+down*(eye_dist*.012)
        highlight_pts=np.array(
            [local_soft(start+highlight_shift),local_soft(middle+highlight_shift),local_soft(end+highlight_shift)],
            dtype=np.int32,
        ).reshape((-1,1,2))
        cv2.polylines(
            lift,[highlight_pts],False,strength*(.30 if side < 0 else .25),
            max(2,int(eye_dist*.090)),cv2.LINE_AA,
        )

        lower=corner+axis*(side*eye_dist*(.10 if side < 0 else .055))+down*(eye_dist*(.34 if side < 0 else .27))
        cv2.ellipse(
            depth,local_soft(lower),
            (max(3,int(eye_dist*(.19 if side < 0 else .15))),max(3,int(eye_dist*(.26 if side < 0 else .20)))),
            angle+side*8,0,360,strength*.82,-1,cv2.LINE_AA,
        )

        # Continue the fold below the mouth corner as an irregular marionette
        # shadow. This remains visible when oversized sunglasses hide the eye
        # bags, while the nearby highlight keeps it from looking drawn on.
        marionette_start=corner+down*(eye_dist*(.07 if side < 0 else .10))
        marionette_end=corner+axis*(side*eye_dist*(.07 if side < 0 else .035))+down*(eye_dist*(.43 if side < 0 else .34))
        marionette=np.array([local_soft(marionette_start),local_soft(marionette_end)],dtype=np.int32).reshape((-1,1,2))
        cv2.polylines(
            depth,[marionette],False,strength*(.52 if side < 0 else .40),
            max(2,int(eye_dist*(.085 if side < 0 else .070))),cv2.LINE_AA,
        )
        relief_shift=axis*(-side*eye_dist*.045)
        relief=np.array(
            [local_soft(marionette_start+relief_shift),local_soft(marionette_end+relief_shift)],
            dtype=np.int32,
        ).reshape((-1,1,2))
        cv2.polylines(
            lift,[relief],False,strength*(.18 if side < 0 else .14),
            max(2,int(eye_dist*.058)),cv2.LINE_AA,
        )

    # Uneven cheek hollowing and gentle lower-jaw relaxation provide the main
    # age cue visible in the approved reference without changing hair or body.
    for side,cx,cy,axv,ayv,strength in (
        (-1.0,.46,.76,.33,.24,.56),
        (1.0,.52,.84,.27,.20,.385),
    ):
        cheek=eye_mid+axis*(side*eye_dist*cx)+down*(eye_dist*cy)
        cv2.ellipse(depth,local_soft(cheek),(max(4,int(eye_dist*axv)),max(3,int(eye_dist*ayv))),angle+side*11,0,360,strength,-1,cv2.LINE_AA)

    if not lower_face_covered:
        # The sunglasses hide most eye-age cues, so a very broad, low-contrast
        # lower-jaw relaxation keeps the effect readable without drawing lines.
        jaw=eye_mid-axis*(eye_dist*.035)+down*(eye_dist*1.42)
        cv2.ellipse(
            depth,local_soft(jaw),
            (max(5,int(eye_dist*.58)),max(3,int(eye_dist*.15))),
            angle-3,8,172,.16,-1,cv2.LINE_AA,
        )

    depth=cv2.GaussianBlur(depth,(0,0),max(2.0,eye_dist*.058))*face_mask
    lift=cv2.GaussianBlur(lift,(0,0),max(1.7,eye_dist*.046))*face_mask
    original_lab=cv2.cvtColor(roi,cv2.COLOR_BGR2LAB).astype(np.float32)
    final_lab=cv2.cvtColor(np.clip(roi_f,0,255).astype(np.uint8),cv2.COLOR_BGR2LAB).astype(np.float32)
    opacity=float(np.clip(face.get("opacity",1.0),0.0,1.0))
    natural_alpha=np.clip(face_mask*opacity,0.0,1.0)
    # The canonical reference already contains mature skin tone and texture.
    # When it was applied, this pass only fills the missing cheek/mouth/jaw
    # structure at low strength instead of aging the same pixels twice.
    depth_strength=(44.0 if reference_applied else 112.0)+bright_face_boost*(12.0 if reference_applied else 30.0)
    target_l=final_lab[:,:,0]-depth*depth_strength+lift*42.0
    local_delta=(target_l-original_lab[:,:,0])*natural_alpha
    # Preserve the mean face luminance exactly. The effect can create local
    # relief, but it may not shift the overall face away from the original
    # skin tone. This is the hard guard against the recurring face/neck patch.
    tone_weight=float(np.sum(natural_alpha))
    if tone_weight>1e-4:
        mean_delta=float(np.sum(local_delta)/tone_weight)
        local_delta=(local_delta-mean_delta*natural_alpha)
    local_delta=np.clip(local_delta,-21.0,13.0)
    final_lab[:,:,0]=np.clip(original_lab[:,:,0]+local_delta,0,255)
    final_lab[:,:,1]=original_lab[:,:,1]
    final_lab[:,:,2]=original_lab[:,:,2]
    natural=cv2.cvtColor(np.clip(final_lab,0,255).astype(np.uint8),cv2.COLOR_LAB2BGR).astype(np.float32)

    # Oversized opaque glasses hide nearly every eye-age cue. Add a restrained
    # lower-face relaxation by moving the person's own pixels (no recolouring,
    # no generated skin). The asymmetric field gently lowers the mouth/jaw and
    # broadens one jowl by only a few pixels, which remains readable after the
    # glasses are rendered while preserving identity and skin tone.
    yy,xx=np.mgrid[0:rh,0:rw].astype(np.float32)
    global_x=xx+x1
    global_y=yy+y1
    rel_axis=((global_x-eye_mid[0])*axis[0]+(global_y-eye_mid[1])*axis[1])/eye_dist
    rel_down=((global_x-eye_mid[0])*down[0]+(global_y-eye_mid[1])*down[1])/eye_dist
    lower_gate=np.clip((rel_down-.68)/.36,0.0,1.0)*np.clip((1.72-rel_down)/.34,0.0,1.0)
    left_jowl=np.exp(-(((rel_axis+.48)/.34)**2+((rel_down-1.23)/.38)**2))
    right_jowl=np.exp(-(((rel_axis-.53)/.30)**2+((rel_down-1.28)/.34)**2))
    centre_sag=np.exp(-((rel_axis/.50)**2+((rel_down-1.18)/.46)**2))
    outward=eye_dist*(.050*left_jowl+.037*right_jowl)*np.where(rel_axis<0,-1.0,1.0)
    downward=eye_dist*(.034*centre_sag+.018*left_jowl+.012*right_jowl)
    map_x=xx-axis[0]*outward-down[0]*downward
    map_y=yy-axis[1]*outward-down[1]*downward
    relaxed=cv2.remap(
        natural.astype(np.uint8),map_x.astype(np.float32),map_y.astype(np.float32),
        cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT_101,
    ).astype(np.float32)
    relaxation_limit=.40 if reference_applied else .72
    relaxation_alpha=np.clip(face_mask*lower_gate*relaxation_limit,0.0,relaxation_limit)[:,:,None]
    natural=natural*(1.0-relaxation_alpha)+relaxed*relaxation_alpha
    frame[y1:y2,x1:x2]=np.clip(natural,0,255).astype(np.uint8)
    return

    def local(point):
        return (int(round(point[0]-x1)),int(round(point[1]-y1)))
    def curve(mask,points,value,thickness,phase=0.0,wobble=.012):
        dense=[]
        for start_p,end_p in zip(points[:-1],points[1:]):
            for step in range(4):
                t=step/4.0
                p=start_p*(1.0-t)+end_p*t
                wave=math.sin((phase+t)*13.7+len(dense)*.91)*wobble*eye_dist
                drift=math.sin((phase+t)*7.1+len(dense)*1.37)*wobble*eye_dist*.38
                dense.append(p+down*wave+axis*drift)
        dense.append(points[-1])
        pts=np.array([local(p) for p in dense],dtype=np.int32).reshape((-1,1,2))
        cv2.polylines(mask,[pts],False,value,max(1,thickness),cv2.LINE_AA)

    wrinkle=np.zeros((rh,rw),dtype=np.float32)
    highlight=np.zeros((rh,rw),dtype=np.float32)
    thickness=max(1,int(round(eye_dist*.014)))
    # Broken, asymmetric forehead creases. Real wrinkles do not run in three
    # ruler-straight rows across the entire forehead.
    forehead_specs=(
        (-.63,-.50,-.18,.78,0.3),
        (-.55,-.24,.30,.66,1.8),
        (-.45,.04,.44,.53,3.4),
        (-.35,-.38,.02,.42,5.1),
    )
    for idx,(offset,start_t,end_t,strength,phase) in enumerate(forehead_specs):
        c=eye_mid+down*(eye_dist*offset)
        pts=[]
        for t in np.linspace(start_t,end_t,7):
            u=(t-start_t)/max(end_t-start_t,.001)
            bend=math.sin(u*math.pi)*eye_dist*(.018+.004*idx)+math.sin(u*math.pi*3.0+phase)*eye_dist*.010
            side_sag=axis*(math.sin(u*math.pi*2.0+phase)*eye_dist*.010)
            pts.append(c+axis*(eye_dist*t)+down*bend+side_sag)
        # Break the forehead marks into uneven fragments instead of drawing
        # ruler-straight mirrored stripes.
        curve(wrinkle,pts[:4],strength,thickness,phase=phase,wobble=.010)
        if idx != 0:
            curve(wrinkle,pts[4:],strength*.82,thickness,phase=phase+2.0,wobble=.014)
        curve(highlight,[p+down*max(.7,eye_dist*.008) for p in pts[:4]],strength*.22,thickness,phase=phase+1.0,wobble=.007)
    # One restrained, off-centre brow fold instead of symmetrical vertical bars.
    start=eye_mid-axis*(eye_dist*.045)-down*(eye_dist*.36)
    curve(wrinkle,[start,start+axis*(eye_dist*.018)+down*(eye_dist*.15),start-axis*(eye_dist*.014)+down*(eye_dist*.30)],.48,thickness,phase=2.7,wobble=.010)
    # Eye bags and crow's feet.
    for eye,side in ((left_eye,-1.0),(right_eye,1.0)):
        bag_strength=.96 if side < 0 else .72
        bag_y=.19 if side < 0 else .235
        bag_center=eye+down*(eye_dist*bag_y)-axis*(side*eye_dist*.010)
        cv2.ellipse(wrinkle,local(bag_center),(max(3,int(eye_dist*(.31 if side < 0 else .25))),max(1,int(eye_dist*(.083 if side < 0 else .064)))),-float(face.get("angle",0.0))+side*5,10 if side < 0 else 24,166 if side < 0 else 146,bag_strength,thickness,cv2.LINE_AA)
        outer=eye+axis*(side*eye_dist*(.55 if side < 0 else .50))
        spreads=(-.15,-.02,.11) if side < 0 else (-.04,.12)
        for line_idx,spread in enumerate(spreads):
            end=outer+axis*(side*eye_dist*(.27 if side < 0 else .20))+down*(eye_dist*spread)
            strength=(.84 if side < 0 else .58)-abs(spread)*(.45 if side < 0 else .32)
            curve(wrinkle,[outer,end],strength,thickness,phase=side*2.0+line_idx,wobble=.008)
    # Nasolabial folds that run naturally from the nose toward mouth corners.
    for corner,side in ((mouth_left,-1.0),(mouth_right,1.0)):
        start=nose+axis*(side*eye_dist*(.18 if side < 0 else .22))+down*(eye_dist*(.10 if side < 0 else .15))
        mid=(start+corner)*.5+axis*(side*eye_dist*(.12 if side < 0 else .045))+down*(eye_dist*(.020 if side < 0 else -.010))
        end=corner+down*(eye_dist*(.28 if side < 0 else .18))
        strength=1.05 if side < 0 else .72
        curve(wrinkle,[start,mid,end],strength,thickness,phase=4.0+side,wobble=.012)
        curve(highlight,[start+axis*(side*eye_dist*.025),mid+axis*(side*eye_dist*.025),end+axis*(side*eye_dist*.025)],strength*.18,thickness,phase=5.5+side,wobble=.007)
        # Marionette fold below each mouth corner.
        lower_mid=corner+axis*(side*eye_dist*(.08 if side < 0 else .04))+down*(eye_dist*(.27 if side < 0 else .20))
        lower_end=corner+axis*(side*eye_dist*(.05 if side < 0 else .015))+down*(eye_dist*(.50 if side < 0 else .38))
        curve(wrinkle,[corner,lower_mid,lower_end],.76 if side < 0 else .46,thickness,phase=6.0+side,wobble=.010)
    # A few restrained cheek texture marks; deterministic positions avoid
    # flicker from frame to frame.
    for side in (-1.0,1.0):
        rows=3 if side < 0 else 2
        for row in range(rows):
            c=eye_mid+axis*(side*eye_dist*((.46 if side < 0 else .52)+.08*row))+down*(eye_dist*((.50 if side < 0 else .58)+.18*row))
            start=c-axis*(eye_dist*.09); end=c+axis*(eye_dist*.09)+down*(eye_dist*.025)
            curve(wrinkle,[start,end],(.34+.055*row) if side < 0 else (.20+.038*row),thickness,phase=8.0+side+row,wobble=.010)

    wrinkle=cv2.GaussianBlur(wrinkle,(0,0),max(.80,eye_dist*.010))*face_mask
    highlight=cv2.GaussianBlur(highlight,(0,0),max(.70,eye_dist*.008))*face_mask
    # Keep hand-drawn guide creases subordinate to the real reference texture;
    # this removes the old straight/cross-hatched look.
    dark_alpha=np.clip(wrinkle*.250,0,.182)[:,:,None]
    light_alpha=np.clip(highlight*.026,0,.020)[:,:,None]
    dark_color=np.array([88.0,98.0,122.0],dtype=np.float32)
    light_color=np.array([190.0,196.0,206.0],dtype=np.float32)
    roi_f=roi_f*(1.0-dark_alpha)+dark_color*dark_alpha
    roi_f=roi_f*(1.0-light_alpha)+light_color*light_alpha

    # Fine irregular skin creases, generated once in normalized face space so
    # they stay attached instead of flickering. Anisotropic noise produces
    # broken organic ridges rather than repeated horizontal bars.
    if old_texture_master is None:
        rng=np.random.default_rng(20260718)
        noise=rng.normal(0,1,(256,256)).astype(np.float32)
        elongated=cv2.GaussianBlur(noise,(0,0),sigmaX=7.5,sigmaY=.65)
        broad=cv2.GaussianBlur(elongated,(0,0),sigmaX=17.0,sigmaY=2.6)
        ridges=np.abs(elongated-broad)
        low=float(np.percentile(ridges,64)); high=float(np.percentile(ridges,98.5))
        old_texture_master=np.clip((ridges-low)/max(high-low,1e-5),0,1)
        old_texture_master=cv2.GaussianBlur(old_texture_master,(0,0),.35)
    texture=cv2.resize(old_texture_master,(rw,rh),interpolation=cv2.INTER_LINEAR)
    texture_zone=np.zeros((rh,rw),dtype=np.float32)
    # Unequal zones keep the distribution anatomical and asymmetric.
    forehead_c=eye_mid-down*(eye_dist*.46)-np.array([x1,y1],dtype=np.float32)
    cv2.ellipse(texture_zone,(int(forehead_c[0]),int(forehead_c[1])),(max(5,int(eye_dist*.62)),max(4,int(eye_dist*.38))),-float(face.get("angle",0.0)),0,360,.72,-1,cv2.LINE_AA)
    for eye,side in ((left_eye,-1.0),(right_eye,1.0)):
        p=eye+down*(eye_dist*.18)-np.array([x1,y1],dtype=np.float32)
        cv2.ellipse(texture_zone,(int(p[0]),int(p[1])),(max(4,int(eye_dist*.36)),max(3,int(eye_dist*.17))),-float(face.get("angle",0.0)),0,360,.62 if side<0 else .52,-1,cv2.LINE_AA)
    for corner,side in ((mouth_left,-1.0),(mouth_right,1.0)):
        p=corner+axis*(side*eye_dist*.12)-np.array([x1,y1],dtype=np.float32)
        cv2.ellipse(texture_zone,(int(p[0]),int(p[1])),(max(3,int(eye_dist*.22)),max(4,int(eye_dist*.40))),-float(face.get("angle",0.0))+side*16,0,360,.48 if side<0 else .40,-1,cv2.LINE_AA)
    texture_zone=cv2.GaussianBlur(texture_zone,(0,0),max(1.2,eye_dist*.022))*face_mask
    texture_alpha=np.clip(texture*texture_zone*.074,0,.060)[:,:,None]
    texture_color=np.array([90.0,100.0,122.0],dtype=np.float32)
    roi_f=roi_f*(1.0-texture_alpha)+texture_color*texture_alpha

    # Soft under-eye depth and subtle age spots add realism without making the
    # subject look dirty or cartoonishly wrinkled.
    depth=np.zeros((rh,rw),dtype=np.float32)
    for eye,side in ((left_eye,-1.0),(right_eye,1.0)):
        cv2.ellipse(depth,local(eye+down*(eye_dist*(.19 if side < 0 else .23))),(max(3,int(eye_dist*(.38 if side < 0 else .30))),max(2,int(eye_dist*(.16 if side < 0 else .12)))),-float(face.get("angle",0.0))+side*5,0,360,.66 if side < 0 else .46,-1,cv2.LINE_AA)
    # Wide, feathered facial folds carry the visible aging structure. These
    # are intentionally blurred shadows rather than dark drawn lines.
    for corner,side in ((mouth_left,-1.0),(mouth_right,1.0)):
        start=nose+axis*(side*eye_dist*(.18 if side < 0 else .23))+down*(eye_dist*(.12 if side < 0 else .16))
        middle=(start+corner)*.5+axis*(side*eye_dist*(.10 if side < 0 else .045))
        end=corner+down*(eye_dist*(.27 if side < 0 else .18))
        pts=np.array([local(start),local(middle),local(end)],dtype=np.int32).reshape((-1,1,2))
        cv2.polylines(depth,[pts],False,.34 if side < 0 else .22,max(2,int(eye_dist*.068)),cv2.LINE_AA)
    # Cheek hollows and a soft lower-face shadow supply actual age structure.
    for side in (-1.0,1.0):
        cheek=eye_mid+axis*(side*eye_dist*(.44 if side < 0 else .52))+down*(eye_dist*(.76 if side < 0 else .86))
        cv2.ellipse(depth,local(cheek),(max(4,int(eye_dist*(.34 if side < 0 else .25))),max(3,int(eye_dist*(.23 if side < 0 else .18)))),-float(face.get("angle",0.0))+side*12,0,360,.30 if side < 0 else .17,-1,cv2.LINE_AA)
        # Soft jowl depth below the mouth corners gives the lower face the
        # relaxed shape in the approved example without warping the jaw.
        corner=mouth_left if side < 0 else mouth_right
        jowl=corner+axis*(side*eye_dist*(.13 if side < 0 else .07))+down*(eye_dist*(.38 if side < 0 else .31))
        cv2.ellipse(depth,local(jowl),(max(3,int(eye_dist*(.22 if side < 0 else .17))),max(3,int(eye_dist*(.28 if side < 0 else .22)))),-float(face.get("angle",0.0))+side*8,0,360,.24 if side < 0 else .14,-1,cv2.LINE_AA)
    chin=eye_mid+down*(eye_dist*1.54)
    cv2.ellipse(depth,local(chin),(max(5,int(eye_dist*.36)),max(2,int(eye_dist*.10))),-float(face.get("angle",0.0)),0,360,.05,-1,cv2.LINE_AA)
    spots=np.zeros((rh,rw),dtype=np.float32)
    for sx,sy,sr in ((-.42,.55,.025),(.38,.62,.021),(-.30,.84,.018),(.47,.88,.016)):
        p=eye_mid+axis*(eye_dist*sx)+down*(eye_dist*sy)
        cv2.circle(spots,local(p),max(1,int(eye_dist*sr)),.18,-1,cv2.LINE_AA)
    depth=cv2.GaussianBlur(depth,(0,0),max(1.0,eye_dist*.025))*face_mask
    spots=cv2.GaussianBlur(spots,(0,0),max(.6,eye_dist*.008))*face_mask
    extra=np.clip(depth*1.18+spots*.32,0,.260)[:,:,None]
    roi_f=roi_f*(1.0-extra)+np.array([88.0,98.0,120.0])*extra
    # Aging changes luminance/structure only. Restore the original A/B chroma
    # inside the processed skin mask so the face cannot become a different
    # colour from the neck and hands.
    final_lab=cv2.cvtColor(np.clip(roi_f,0,255).astype(np.uint8),cv2.COLOR_BGR2LAB).astype(np.float32)
    original_lab=cv2.cvtColor(roi,cv2.COLOR_BGR2LAB).astype(np.float32)
    final_lab[:,:,0]=final_lab[:,:,0]*(1.0-face_mask)+np.maximum(final_lab[:,:,0],original_lab[:,:,0]-28.0)*face_mask
    line_detail=np.clip((wrinkle*.21 + depth*.18)*face_mask,0.0,.34)
    final_lab[:,:,0]=np.clip(final_lab[:,:,0]-line_detail*76.0,0,255)
    final_lab[:,:,1]=final_lab[:,:,1]*(1.0-face_mask)+original_lab[:,:,1]*face_mask
    final_lab[:,:,2]=final_lab[:,:,2]*(1.0-face_mask)+original_lab[:,:,2]*face_mask
    roi_f=cv2.cvtColor(final_lab.astype(np.uint8),cv2.COLOR_LAB2BGR).astype(np.float32)
    yy,xx=np.mgrid[0:rh,0:rw].astype(np.float32)
    global_x=xx+x1; global_y=yy+y1
    lateral=((global_x-eye_mid[0])*axis[0]+(global_y-eye_mid[1])*axis[1])/max(eye_dist,1.0)
    vertical=((global_x-eye_mid[0])*down[0]+(global_y-eye_mid[1])*down[1])/max(eye_dist,1.0)
    asymmetry=np.clip(1.0-.20*np.tanh(lateral*1.25)+.08*np.sin(lateral*4.8+vertical*2.7),.70,1.30)
    line_mask=np.clip((wrinkle*.48 + depth*.46 + texture*texture_zone*.180)*face_mask*asymmetry,0.0,.34)[:,:,None]
    line_color=np.array([92.0,102.0,122.0],dtype=np.float32)
    roi_f=roi_f*(1.0-line_mask)+line_color*line_mask
    frame[y1:y2,x1:x2]=np.clip(roi_f,0,255).astype(np.uint8)

def lm_point(lms, idx):
    p = lms[idx]
    return np.array([p.x*w, p.y*h], dtype=np.float32)

def mesh_detections(frame,timestamp_ms=0):
    rgb = np.ascontiguousarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    if face_landmarker is not None:
        try:
            task_result=face_landmarker.detect_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB,data=rgb),int(timestamp_ms),
            )
            landmark_sets=task_result.face_landmarks
        except Exception:
            landmark_sets=[]
    elif face_mesh is not None:
        res = face_mesh.process(rgb)
        landmark_sets=[item.landmark for item in res.multi_face_landmarks] if res.multi_face_landmarks else []
    else:
        return []
    detections = []
    if not landmark_sets:
        return detections
    for lms in landmark_sets:
        xs = [p.x*w for p in lms]
        ys = [p.y*h for p in lms]
        x1 = max(0, int(min(xs)))
        y1 = max(0, int(min(ys)))
        x2 = min(w, int(max(xs)))
        y2 = min(h, int(max(ys)))
        if x2 - x1 < 40 or y2 - y1 < 40:
            continue
        left_eye = (lm_point(lms, 33) + lm_point(lms, 133)) / 2
        right_eye = (lm_point(lms, 362) + lm_point(lms, 263)) / 2
        eye_mid = (left_eye + right_eye) / 2
        eye_dist = float(np.linalg.norm(right_eye - left_eye))
        angle = math.degrees(math.atan2(right_eye[1] - left_eye[1], right_eye[0] - left_eye[0]))
        nose = lm_point(lms, 1)
        eye_axis = (right_eye-left_eye)/max(eye_dist,1.0)
        yaw = float(np.clip(np.dot(nose-eye_mid, eye_axis)/max(eye_dist,1.0)*2.2, -.65, .65))
        detections.append({
            "box": (x1, y1, x2 - x1, y2 - y1),
            "lms": lms,
            "detector": "mediapipe",
            "left_eye": left_eye,
            "right_eye": right_eye,
            "eye_mid": eye_mid,
            "eye_dist": eye_dist,
            "angle": angle,
            "yaw": yaw,
            "nose_point": nose,
            "mouth_left": lm_point(lms,61),
            "mouth_right": lm_point(lms,291),
        })
    return detections

def cascade_detections(gray, require_eyes=False):
    if cascade.empty():
        return []
    min_face = max(54, int(min(w, h) * (0.16 if require_eyes else 0.075)))
    faces = cascade.detectMultiScale(gray, 1.08, 4, minSize=(min_face,min_face))
    detections = []
    for x,y,fw,fh in faces:
        detection = {"box": (x,y,fw,fh), "lms": None, "angle": 0.0, "detector": "haar"}
        if require_eyes:
            if eye_cascade.empty():
                continue
            upper = gray[y:y+max(1,int(fh*.62)), x:x+fw]
            eye_min = max(10, int(fw*.075))
            eyes = eye_cascade.detectMultiScale(
                upper,
                1.08,
                4,
                minSize=(eye_min, eye_min),
            )
            if len(eyes) < 1:
                continue
            eye_rows = []
            for ex,ey,ew,eh in eyes:
                center = np.array([x+ex+ew*.5, y+ey+eh*.5], dtype=np.float32)
                eye_rows.append((float(ew*eh), center))
            eye_rows.sort(key=lambda item: item[0], reverse=True)
            candidates = [item[1] for item in eye_rows[:4]]
            best_pair = None
            best_distance = 0.0
            for first_index in range(len(candidates)):
                for second_index in range(first_index+1,len(candidates)):
                    first = candidates[first_index]
                    second = candidates[second_index]
                    horizontal = abs(float(second[0]-first[0]))
                    vertical = abs(float(second[1]-first[1]))
                    if horizontal > best_distance and horizontal >= fw*.18 and vertical <= fh*.18:
                        best_pair = (first,second)
                        best_distance = horizontal
            if best_pair is None:
                observed = candidates[0]
                mirrored_x = x + fw*.5 + (x + fw*.5 - observed[0])
                mirrored = np.array([mirrored_x, observed[1]], dtype=np.float32)
                best_pair = (observed,mirrored)
            left_eye,right_eye = sorted(best_pair,key=lambda point: float(point[0]))
            eye_mid=(left_eye+right_eye)/2
            eye_dist=float(np.linalg.norm(right_eye-left_eye))
            if eye_dist < fw*.18 or eye_dist > fw*.72:
                continue
            nose=np.array([x+fw*.5,y+fh*.52],dtype=np.float32)
            detection.update({
                "detector": "haar_eye",
                "left_eye": left_eye,
                "right_eye": right_eye,
                "eye_mid": eye_mid,
                "eye_dist": eye_dist,
                "nose_point": nose,
                "mouth_left": np.array([x+fw*.35,y+fh*.72],dtype=np.float32),
                "mouth_right": np.array([x+fw*.65,y+fh*.72],dtype=np.float32),
            })
        detections.append(detection)
    return detections

def insight_detections(frame):
    if face_app is None:
        return []
    try:
        faces = face_app.get(frame)
    except Exception:
        return []
    detections = []
    for item in faces:
        x1,y1,x2,y2 = item.bbox.astype(int).tolist()
        x1=max(0,x1); y1=max(0,y1); x2=min(w,x2); y2=min(h,y2)
        fw=x2-x1; fh=y2-y1
        if fw < 40 or fh < 40:
            continue
        det = {
            "box": (x1,y1,fw,fh),
            "lms": None,
            "angle": 0.0,
            "detector": "insightface",
            "confidence": float(getattr(item, "det_score", 1.0)),
        }
        if getattr(item, "kps", None) is not None and len(item.kps) >= 2:
            left_eye = np.array(item.kps[0], dtype=np.float32)
            right_eye = np.array(item.kps[1], dtype=np.float32)
            eye_mid = (left_eye + right_eye) / 2
            eye_dist = float(np.linalg.norm(right_eye - left_eye))
            angle = math.degrees(math.atan2(right_eye[1] - left_eye[1], right_eye[0] - left_eye[0]))
            yaw = 0.0
            if len(item.kps) >= 3:
                nose = np.array(item.kps[2], dtype=np.float32)
                eye_axis = (right_eye-left_eye)/max(eye_dist,1.0)
                yaw = float(np.clip(np.dot(nose-eye_mid, eye_axis)/max(eye_dist,1.0)*2.2, -.65, .65))
            det.update({"left_eye": left_eye, "right_eye": right_eye, "eye_mid": eye_mid, "eye_dist": eye_dist, "angle": angle, "yaw": yaw})
            if len(item.kps) >= 5:
                det.update({
                    "nose_point": np.array(item.kps[2],dtype=np.float32),
                    "mouth_left": np.array(item.kps[3],dtype=np.float32),
                    "mouth_right": np.array(item.kps[4],dtype=np.float32),
                })
        detections.append(det)
    return detections

def cover_face_has_human_skin(image, face):
    # Reject gray product fittings that form a face-like circle/eye pattern.
    if image is None:
        return True
    x,y,fw,fh = [int(round(value)) for value in face.get("box", (0,0,0,0))]
    x1=max(0,x); y1=max(0,y); x2=min(w,x+fw); y2=min(h,y+fh)
    if x2-x1 < 20 or y2-y1 < 20:
        return False
    roi=image[y1:y2,x1:x2]
    ycrcb=cv2.cvtColor(roi,cv2.COLOR_BGR2YCrCb)
    hsv=cv2.cvtColor(roi,cv2.COLOR_BGR2HSV)
    luminance,cr,cb=cv2.split(ycrcb)
    saturation=hsv[:,:,1]
    skin=(
        (cr>=132)&(cr<=180)&(cb>=75)&(cb<=135)&
        (luminance>=20)&(saturation>=10)
    )
    rh,rw=skin.shape
    if rh < 8 or rw < 8:
        return False
    left=skin[int(rh*.36):max(int(rh*.82),int(rh*.36)+1), int(rw*.06):max(int(rw*.45),int(rw*.06)+1)]
    right=skin[int(rh*.36):max(int(rh*.82),int(rh*.36)+1), int(rw*.55):max(int(rw*.94),int(rw*.55)+1)]
    total=float(skin.mean())
    left_ratio=float(left.mean()) if left.size else 0.0
    right_ratio=float(right.mean()) if right.size else 0.0
    balanced_cheeks=min(left_ratio,right_ratio)>=.015
    strong_face=total>=.12 and max(left_ratio,right_ratio)>=.04
    return total>=.04 and (balanced_cheeks or strong_face)

def plausible_face_for_cover(face, image=None, min_confidence=.45):
    # Cover stickers are visually destructive when a product is mistaken for
    # a face. Only accept modern landmark/embedding detectors here. Haar is
    # still available for non-cover legacy effects, but never for glasses.
    if face.get("detector") not in {"mediapipe", "insightface"}:
        return False
    x,y,fw,fh = face.get("box", (0,0,0,0))
    if fw < 45 or fh < 45 or fw > w*.92 or fh > h*.92:
        return False
    if face.get("eye_mid") is None or face.get("left_eye") is None or face.get("right_eye") is None:
        return False
    if face.get("detector") == "insightface" and float(face.get("confidence", 1.0)) < min_confidence:
        return False
    left_eye = np.array(face["left_eye"], dtype=np.float32)
    right_eye = np.array(face["right_eye"], dtype=np.float32)
    eye_mid = np.array(face["eye_mid"], dtype=np.float32)
    eye_dist = float(np.linalg.norm(right_eye-left_eye))
    if eye_dist < max(18.0, min(w,h)*.025):
        return False
    eye_to_box = eye_dist / max(float(fw), 1.0)
    if eye_to_box < .18 or eye_to_box > .70:
        return False
    box_aspect = float(fw) / max(float(fh), 1.0)
    if box_aspect < .38 or box_aspect > 1.35:
        return False
    eye_y_ratio = (float(eye_mid[1]) - float(y)) / max(float(fh), 1.0)
    if eye_y_ratio < .12 or eye_y_ratio > .55:
        return False
    axis = (right_eye-left_eye) / max(eye_dist, 1.0)
    down = np.array([-axis[1], axis[0]], dtype=np.float32)
    nose = face.get("nose_point")
    if nose is not None:
        nose = np.array(nose, dtype=np.float32)
        if float(np.dot(nose-eye_mid, down)) < 0:
            down = -down
        nose_down = float(np.dot(nose-eye_mid, down)) / max(eye_dist, 1.0)
        nose_side = abs(float(np.dot(nose-eye_mid, axis)) / max(eye_dist, 1.0))
        if nose_down < .10 or nose_down > 1.10 or nose_side > .78:
            return False
    mouth_left = face.get("mouth_left")
    mouth_right = face.get("mouth_right")
    if mouth_left is not None and mouth_right is not None:
        mouth_left = np.array(mouth_left, dtype=np.float32)
        mouth_right = np.array(mouth_right, dtype=np.float32)
        mouth_mid = (mouth_left + mouth_right) / 2
        if nose is None and float(np.dot(mouth_mid-eye_mid, down)) < 0:
            down = -down
        mouth_down = float(np.dot(mouth_mid-eye_mid, down)) / max(eye_dist, 1.0)
        mouth_width = float(np.linalg.norm(mouth_right-mouth_left)) / max(eye_dist, 1.0)
        if mouth_down < .45 or mouth_down > 1.95 or mouth_width < .25 or mouth_width > 1.70:
            return False
    return cover_face_has_human_skin(image, face)

tracks = {}
next_track_id = 1
cover_effects = {"sticker_sunglasses", "phone_cover", "sunglasses"}
# A skipped detector frame is not a missed face. Count only real detection
# attempts, keep one failed attempt at full opacity, then fade over the next
# two attempts. Scene cuts still clear tracks immediately, so stickers cannot
# drift onto product-only shots.
max_missed_frames = 3 if effect in cover_effects else max(5, active_detection_interval)
full_opacity_missed_frames = 1 if effect in cover_effects else 2
# Require three detector hits before showing a cover sticker. Together with
# the modern-detector-only rule this prevents short-lived product false
# positives from flashing while keeping confirmed faces tracked smoothly.
cover_confirmation_frames = 3
tracked_point_names = ("left_eye", "right_eye", "nose_point", "mouth_left", "mouth_right")

def angle_delta(start, end):
    return (end - start + 180.0) % 360.0 - 180.0

def face_measurement(face):
    x,y,fw,fh = face["box"]
    if face.get("eye_mid") is not None:
        center = np.array(face["eye_mid"], dtype=np.float32)
        eye_dist = max(1.0, float(face.get("eye_dist", fw*.38)))
        angle = float(face.get("angle", 0.0))
        yaw = float(face.get("yaw", 0.0))
    else:
        center = np.array([x + fw*.5, y + fh*.39], dtype=np.float32)
        eye_dist = max(1.0, float(fw*.38))
        angle = 0.0
        yaw = 0.0
    box = np.array([x,y,fw,fh], dtype=np.float32)
    return center, eye_dist, angle, yaw, box

def box_iou(first, second):
    ax1,ay1,aw,ah = first
    bx1,by1,bw,bh = second
    ax2,ay2 = ax1+aw, ay1+ah
    bx2,by2 = bx1+bw, by1+bh
    iw=max(0.0, min(ax2,bx2)-max(ax1,bx1))
    ih=max(0.0, min(ay2,by2)-max(ay1,by1))
    intersection=iw*ih
    union=max(aw*ah + bw*bh - intersection, 1.0)
    return intersection/union

def tracked_face(state, source=None):
    result = dict(source or {})
    result["box"] = tuple(int(round(v)) for v in state["box"])
    result["eye_mid"] = state["center"].copy()
    result["eye_dist"] = float(state["eye_dist"])
    result["angle"] = float(state["angle"])
    result["yaw"] = float(state["yaw"])
    for name, point in state.get("points", {}).items():
        result[name] = point.copy()
    missed = int(state["missed"])
    if missed <= full_opacity_missed_frames:
        opacity = 1.0
    else:
        fade = (missed-full_opacity_missed_frames) / max(1, max_missed_frames-full_opacity_missed_frames)
        opacity = 1.0-fade*.88
    result["opacity"] = float(np.clip(opacity, .12, 1.0))
    result.setdefault("lms", None)
    return result

def smooth_tracked_faces(detections, measurement_attempted=True):
    global next_track_id
    measurements = [(face, *face_measurement(face)) for face in detections]
    measurements.sort(key=lambda item: item[2], reverse=True)
    available = set(tracks)
    output = []

    for face, center, eye_dist, angle, yaw, box in measurements:
        best_id = None
        best_score = float("inf")
        for track_id in available:
            state = tracks[track_id]
            predicted = state["center"] + state["velocity"]
            scale = max(eye_dist, state["eye_dist"], 1.0)
            distance = float(np.linalg.norm(center - predicted)) / scale
            scale_change = abs(math.log(max(eye_dist, 1.0) / max(state["eye_dist"], 1.0)))
            overlap = box_iou(box, state["box"])
            score = distance + scale_change*.30 + (1.0-overlap)*.18
            if (distance <= 1.10 or overlap >= .08) and scale_change <= .65 and score < best_score:
                best_id = track_id
                best_score = score

        if best_id is None:
            best_id = next_track_id
            next_track_id += 1
            tracks[best_id] = {
                "center": center.copy(),
                "eye_dist": eye_dist,
                "angle": angle,
                "yaw": yaw,
                "box": box.copy(),
                "velocity": np.zeros(2, dtype=np.float32),
                "last_measurement": center.copy(),
                "missed": 0,
                "hits": 1,
                "points": {
                    name: np.array(face[name], dtype=np.float32).copy()
                    for name in tracked_point_names
                    if face.get(name) is not None
                },
            }
        else:
            available.remove(best_id)
            state = tracks[best_id]
            old_center = state["center"].copy()
            predicted = old_center + state["velocity"]
            motion = float(np.linalg.norm(center - predicted)) / max(eye_dist, state["eye_dist"], 1.0)
            # Smooth detector noise without making the sticker lag behind a
            # moving head. Velocity prediction supplies the fast response.
            position_alpha = float(np.clip(.72 + motion*.32, .72, .92))
            scale_change = abs(math.log(max(eye_dist, 1.0) / max(state["eye_dist"], 1.0)))
            scale_alpha = float(np.clip(.52 + scale_change*.55, .52, .80))
            turn = angle_delta(state["angle"], angle)
            angle_alpha = float(np.clip(.55 + abs(turn)/42.0, .55, .82))
            yaw_change=float(np.clip(yaw-state["yaw"], -.18, .18))
            yaw_alpha=float(np.clip(.42+abs(yaw_change)*1.4, .42, .68))
            state["center"] = predicted*(1.0-position_alpha) + center*position_alpha
            frames_since_measurement = state["missed"] + 1
            observed_velocity = (center - state["last_measurement"]) / frames_since_measurement
            state["velocity"] = state["velocity"]*.48 + observed_velocity*.52
            state["last_measurement"] = center.copy()
            bounded_eye_dist = float(np.clip(eye_dist, state["eye_dist"]*.86, state["eye_dist"]*1.16))
            state["eye_dist"] = state["eye_dist"]*(1.0-scale_alpha) + bounded_eye_dist*scale_alpha
            state["angle"] += turn*angle_alpha
            state["yaw"] += yaw_change*yaw_alpha
            state["box"] = state["box"]*(1.0-position_alpha) + box*position_alpha
            for name in tracked_point_names:
                if face.get(name) is None:
                    continue
                measured_point = np.array(face[name], dtype=np.float32)
                previous_point = state["points"].get(name)
                state["points"][name] = (
                    measured_point.copy()
                    if previous_point is None
                    else previous_point*(1.0-position_alpha) + measured_point*position_alpha
                )
            state["missed"] = 0
            state["hits"] = min(4, int(state.get("hits", 1)) + 1)

        state = tracks[best_id]
        if effect not in cover_effects or int(state.get("hits", 1)) >= cover_confirmation_frames:
            output.append(tracked_face(state, face))

    for track_id in list(available):
        state = tracks[track_id]
        if measurement_attempted:
            state["missed"] += 1
            if effect in cover_effects and int(state.get("hits", 1)) < cover_confirmation_frames:
                del tracks[track_id]
                continue
            if state["missed"] > max_missed_frames:
                del tracks[track_id]
                continue
        carried_velocity = state["velocity"]*.72
        state["center"] += carried_velocity
        state["box"][:2] += carried_velocity
        for name in state.get("points", {}):
            state["points"][name] += carried_velocity
        state["velocity"] *= .68
        if effect not in cover_effects or int(state.get("hits", 1)) >= cover_confirmation_frames:
            output.append(tracked_face(state))

    return output

previous_scene_gray = None
frame_index = 0
detected_frame_count = 0
rendered_frame_count = 0
short_effects = {"sparkle", "blush", "neon"}
short_effect_window_frames = max(1, int(round(short_effect_window_seconds * fps)))
scene_start_frame = 0
while True:
    ok, frame=cap.read()
    if not ok: break
    original_frame = frame.copy()
    gray=cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    scene_gray=cv2.resize(gray, (64,64), interpolation=cv2.INTER_AREA)
    scene_changed=False
    if previous_scene_gray is not None and cv2.absdiff(scene_gray, previous_scene_gray).mean() > 24:
        tracks.clear()
        scene_changed=True
        scene_start_frame=frame_index
    previous_scene_gray=scene_gray
    timestamp_ms=int(round(frame_index*1000.0/max(fps,1.0)))
    detection_interval=active_detection_interval if tracks else idle_detection_interval
    should_detect=(frame_index % detection_interval == 0) or scene_changed
    primary_faces=(mesh_detections(frame,timestamp_ms) or insight_detections(frame)) if should_detect else []
    frame_index += 1
    if effect in cover_effects:
        detected_faces=[face for face in primary_faces if plausible_face_for_cover(face,frame)]
        if not detected_faces:
            # Never fall back to Haar here: faucets and product fittings can
            # resemble a face to Haar and make glasses flash on product shots.
            # Confirmed modern-detector tracks may coast briefly through a
            # dropout so glasses on a real face remain visually stable.
            pass
    elif face_mesh is not None or face_landmarker is not None or face_app is not None:
        # Do not fall back to Haar for face effects: on product-only shots it
        # can interpret round fittings as faces. The modern detector handles
        # multiple real people and supplies stable eye/nose/mouth keypoints.
        detected_faces=primary_faces
    else:
        detected_faces=primary_faces or (cascade_detections(gray) if should_detect else [])
    if effect in {"sticker_sunglasses", "phone_cover", "sunglasses", "old", "blur", "mosaic", "sparkle", "blush", "neon"}:
        faces=smooth_tracked_faces(
            detected_faces,
            measurement_attempted=should_detect,
        )
    else:
        faces=detected_faces
    # Creative style effects are one-second shot accents. Face-cover and
    # aging effects remain face-tracked for the complete useful shot.
    if effect in short_effects and frame_index - scene_start_frame > short_effect_window_frames:
        faces=[]
    if detected_faces:
        detected_frame_count += 1
    if faces:
        rendered_frame_count += 1
    for face in faces:
        x,y,fw,fh = face["box"]
        if effect == 'sticker_sunglasses':
            if face.get("eye_mid") is not None:
                render_sunglasses(frame, face)
                continue
            sw=max(82, int(fw*.96)); sh=max(30, int(fh*.23))
            sticker=make_sunglasses(sw, sh)
            alpha_blend(frame, sticker, x+fw//2-sw//2, y+int(fh*.255))
        elif effect == 'phone_cover':
            if face.get("eye_mid") is not None:
                eye_mid = face["eye_mid"]
                sw=max(118, int(face["eye_dist"]*2.72))
                sh=max(52, int(sw*.38))
                sticker=rotate_rgba(make_phone(sw, sh), -face["angle"])
                alpha_blend(frame, sticker, int(eye_mid[0]-sticker.shape[1]/2), int(eye_mid[1]-sticker.shape[0]*.53))
                continue
            sw=max(108, int(fw*.92)); sh=max(58, int(sw*.45))
            sticker=make_phone(sw, sh)
            alpha_blend(frame, sticker, x+fw//2-sw//2, y+int(fh*.245))
        elif effect == 'sunglasses':
            # CapCut-like local sticker: soft shadow, rounded dark lenses, bridge and highlight.
            overlay=frame.copy()
            shadow=frame.copy()
            lx1=max(0, x+int(fw*.06)); ly1=max(0, y+int(fh*.27))
            lx2=min(w, x+int(fw*.47)); ly2=min(h, y+int(fh*.56))
            rx1=max(0, x+int(fw*.53)); ry1=ly1
            rx2=min(w, x+int(fw*.94)); ry2=ly2
            bridge_y=y+int(fh*.40)
            radius=max(8, int(fw*.10))
            cv2.rectangle(shadow, (lx1+3, ly1+5), (lx2+3, ly2+5), (0,0,0), -1)
            cv2.rectangle(shadow, (rx1+3, ry1+5), (rx2+3, ry2+5), (0,0,0), -1)
            cv2.addWeighted(shadow, .18, frame, .82, 0, frame)
            cv2.rectangle(overlay, (lx1, ly1), (lx2, ly2), (5,7,10), -1)
            cv2.rectangle(overlay, (rx1, ry1), (rx2, ry2), (5,7,10), -1)
            for cx, cy in [(lx1,ly1),(lx2,ly1),(lx1,ly2),(lx2,ly2),(rx1,ry1),(rx2,ry1),(rx1,ry2),(rx2,ry2)]:
                cv2.circle(overlay, (cx,cy), radius, (5,7,10), -1)
            cv2.line(overlay, (lx2-int(fw*.02), bridge_y), (rx1+int(fw*.02), bridge_y), (5,7,10), max(3,int(fw*.045)))
            cv2.line(overlay, (lx1, ly1+int((ly2-ly1)*.18)), (lx2, ly1+int((ly2-ly1)*.18)), (72,86,102), max(1,int(fw*.018)))
            cv2.line(overlay, (rx1, ry1+int((ry2-ry1)*.18)), (rx2, ry1+int((ry2-ry1)*.18)), (72,86,102), max(1,int(fw*.018)))
            cv2.addWeighted(overlay, .84, frame, .16, 0, frame)
        elif effect == 'blur':
            pad=int(fw*.12)
            x1=max(0,x-pad); y1=max(0,y-pad); x2=min(w,x+fw+pad); y2=min(h,y+fh+pad)
            roi=frame[y1:y2, x1:x2]
            if roi.size:
                frame[y1:y2, x1:x2]=cv2.GaussianBlur(roi, (51,51), 0)
        elif effect == 'mosaic':
            pad=int(fw*.10)
            x1=max(0,x-pad); y1=max(0,y-pad); x2=min(w,x+fw+pad); y2=min(h,y+fh+pad)
            roi=frame[y1:y2, x1:x2]
            if roi.size:
                small=cv2.resize(roi, (max(8,roi.shape[1]//14), max(8,roi.shape[0]//14)), interpolation=cv2.INTER_LINEAR)
                frame[y1:y2, x1:x2]=cv2.resize(small, (roi.shape[1], roi.shape[0]), interpolation=cv2.INTER_NEAREST)
        elif effect == 'old':
            apply_old_age(frame,face)
        elif effect == 'sparkle':
            overlay=frame.copy()
            pts=[
                (x+int(fw*.08), y+int(fh*.08), max(7, fw//13)),
                (x+int(fw*.92), y+int(fh*.18), max(6, fw//15)),
                (x+int(fw*.18), y+int(fh*.72), max(5, fw//18)),
                (x+int(fw*.82), y+int(fh*.66), max(5, fw//18)),
            ]
            for cx,cy,r in pts:
                cv2.line(overlay, (cx-r,cy), (cx+r,cy), (80,230,255), max(2,r//4))
                cv2.line(overlay, (cx,cy-r), (cx,cy+r), (80,230,255), max(2,r//4))
                cv2.line(overlay, (cx-r//2,cy-r//2), (cx+r//2,cy+r//2), (255,255,255), 1)
                cv2.line(overlay, (cx-r//2,cy+r//2), (cx+r//2,cy-r//2), (255,255,255), 1)
            cv2.addWeighted(overlay, .72, frame, .28, 0, frame)
        elif effect == 'blush':
            overlay=frame.copy()
            cv2.ellipse(overlay, (x+int(fw*.28), y+int(fh*.56)), (max(8,fw//9), max(5,fh//16)), 0, 0, 360, (150,95,255), -1)
            cv2.ellipse(overlay, (x+int(fw*.72), y+int(fh*.56)), (max(8,fw//9), max(5,fh//16)), 0, 0, 360, (150,95,255), -1)
            cv2.addWeighted(overlay, .28, frame, .72, 0, frame)
            roi=frame[y:y+fh, x:x+fw]
            if roi.size:
                soft=cv2.bilateralFilter(roi, 7, 45, 45)
                frame[y:y+fh, x:x+fw]=cv2.addWeighted(soft,.38,roi,.62,0)
        elif effect == 'neon':
            overlay=frame.copy()
            cv2.ellipse(overlay, (x+fw//2, y+fh//2), (int(fw*.62), int(fh*.68)), 0, 0, 360, (255,70,210), max(3, fw//24))
            cv2.ellipse(overlay, (x+fw//2, y+fh//2), (int(fw*.68), int(fh*.74)), 0, 0, 360, (70,230,255), max(2, fw//34))
            cv2.addWeighted(overlay, .55, frame, .45, 0, frame)
    frame_time=max(0.0, (frame_index-1)/max(fps,1.0))
    if frame_time < early_effect_seconds and faces:
        frame=cv2.addWeighted(
            frame,
            early_effect_opacity,
            original_frame,
            1.0-early_effect_opacity,
            0,
        )
    out.write(frame)
cap.release(); out.release()
print(
    "V16_FACE_EFFECT_STATS "
    f"effect={effect} frames={frame_index} detect_active={active_detection_interval} "
    f"detect_idle={idle_detection_interval} "
    f"detected={detected_frame_count} rendered={rendered_frame_count}"
)
if face_landmarker is not None:
    try:
        face_landmarker.close()
    except Exception:
        pass
cpu_encoder_args=['-c:v','libx264','-preset','veryfast','-crf','23','-profile:v','high','-pix_fmt','yuv420p']
if ffmpeg_threads:
    cpu_encoder_args.extend(['-threads',str(ffmpeg_threads)])
if encoder_mode == 'nvenc':
    encoder_args=[
        '-c:v','h264_nvenc','-gpu',str(nvidia_gpu_index),'-preset','p4',
        '-tune','hq','-rc','vbr','-cq','23','-b:v','0',
        '-multipass','disabled','-profile:v','high','-pix_fmt','yuv420p'
    ]
    active_encoder='h264_nvenc'
elif encoder_mode == 'videotoolbox':
    encoder_args=[
        '-c:v','h264_videotoolbox','-q:v','65','-profile:v','high',
        '-pix_fmt','yuv420p','-realtime','1','-prio_speed','1','-allow_sw','1'
    ]
    active_encoder='h264_videotoolbox'
else:
    encoder_args=cpu_encoder_args
    active_encoder='libx264'
merge_command=['ffmpeg','-y','-i',tmp,'-i',src,'-map','0:v','-map','1:a?',*encoder_args,'-c:a','aac','-shortest',dst]
merge_started=time.perf_counter()
merge_result=subprocess.run(
    merge_command,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
merge_elapsed=time.perf_counter()-merge_started
print(
    'V16_ENCODER_STEP '
    f'encoder={active_encoder} elapsed_seconds={merge_elapsed:.3f} '
    f'returncode={merge_result.returncode} fallback=0'
)
if merge_result.returncode != 0 and encoder_mode in {'nvenc','videotoolbox'} and allow_hardware_fallback:
    print(
        'V16_ENCODER_FALLBACK '
        f'from={active_encoder} to=libx264 stage=face_renderer reason=runtime'
    )
    print('[WARN] 人脸特效硬件编码启动失败，已明确记录并切换 CPU 编码重试。')
    merge_command=['ffmpeg','-y','-i',tmp,'-i',src,'-map','0:v','-map','1:a?',*cpu_encoder_args,'-c:a','aac','-shortest',dst]
    fallback_started=time.perf_counter()
    merge_result=subprocess.run(merge_command,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    fallback_elapsed=time.perf_counter()-fallback_started
    print(
        'V16_ENCODER_STEP '
        f'encoder=libx264 elapsed_seconds={fallback_elapsed:.3f} '
        f'returncode={merge_result.returncode} fallback=1'
    )
os.unlink(tmp)
if merge_result.returncode != 0:
    sys.stderr.write(merge_result.stderr.decode("utf-8",errors="ignore") if isinstance(merge_result.stderr,bytes) else str(merge_result.stderr))
    sys.exit(4)
"""
    exe = PY312 if os.path.exists(PY312) else sys.executable
    # Do not pass this large renderer through ``python -c``. On Windows the
    # complete command line is limited to 32,767 characters; this embedded
    # renderer is larger and CreateProcess fails with WinError 206 before
    # Python or FFmpeg can start. A short temporary script avoids that limit.
    os.makedirs(TMP_DIR, exist_ok=True)
    renderer_script = os.path.join(TMP_DIR, "face_renderer.py")
    with open(renderer_script, "w", encoding="utf-8", newline="\n") as script_file:
        script_file.write(code)
    try:
        res = subprocess.run(
            [
                exe,renderer_script,input_path,output_path,selected_effect,
                sunglasses_asset_path,age_texture_path,age_reference_path,face_landmarker_model_path,
                haarcascade_path,eye_cascade_path,
                str(SHORT_EFFECT_WINDOW_SECONDS),str(EARLY_EFFECT_SECONDS),
                str(EARLY_EFFECT_OPACITY),
                _VIDEO_ENCODER_ARGS or "libx264",
                str(FFMPEG_THREADS),
                "0" if _video_encoder_policy() == "nvidia-strict" else "1",
                str(NVIDIA_GPU_INDEX),
            ],
            capture_output=True,
            text=True,
        )
    finally:
        try:
            os.unlink(renderer_script)
        except OSError:
            pass
    stdout_text = res.stdout or ""
    if stdout_text.strip():
        print(stdout_text.strip())
    if "V16_ENCODER_FALLBACK from=h264_nvenc" in stdout_text:
        _disable_hardware_encoder_for_current_run(
            "人脸特效 NVENC 合成失败，后续步骤改用 CPU"
        )
        with _ENCODER_METRICS_LOCK:
            _ENCODER_FALLBACK_COUNT += 1
    if res.returncode != 0 or not os.path.exists(output_path):
        if (
            _video_encoder_policy() == "nvidia-strict"
            and (_VIDEO_ENCODER_ARGS or "") == "nvenc"
        ):
            raise RuntimeError(
                "严格 GPU 模式下人脸特效 NVENC 编码失败；"
                "本次任务已终止，未把 CPU 或原片结果伪装成 GPU 测速样本。\n"
                + (res.stderr or "输出文件未生成")
            )
        print(f"[WARN] 人脸特效失败，保留原片段: {res.stderr}")
        shutil.copyfile(input_path, output_path)
    else:
        _store_face_effect_cache(output_path, cache_path)


def apply_face_effect_chain(input_path, output_path):
    """Render every selected effect in a deterministic order.

    The web UI has always submitted FACE_EFFECTS, but older runners read only
    FACE_EFFECT (the first item), which is why selecting aging + sunglasses
    produced aging alone. Cover effects are now ordered last so glasses remain
    visible after all colour/style passes.
    """
    if not FACE_EFFECTS:
        shutil.copyfile(input_path, output_path)
        return
    current_path = input_path
    temporary_paths = []
    try:
        for index, effect in enumerate(FACE_EFFECTS):
            is_last = index == len(FACE_EFFECTS) - 1
            next_path = output_path if is_last else os.path.join(TMP_DIR, f"face_effect_{index:02d}_{effect}.mp4")
            print(f"   -> 叠加人脸特效 {index + 1}/{len(FACE_EFFECTS)}: {effect}")
            apply_face_effect(current_path, next_path, effect)
            if not is_last:
                temporary_paths.append(next_path)
            current_path = next_path
    finally:
        for temporary_path in temporary_paths:
            if temporary_path != output_path and os.path.exists(temporary_path):
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass


def _clone_clip(clip, path, duration=None):
    return Clip(
        path,
        clip.source,
        float(duration if duration is not None else clip.duration),
        clip.score,
        clip.source_name,
        clip.start,
        clip.sequence,
        clip.source_path,
    )


def _mute_clip_audio(clip, output_path, intervals=None):
    """Silence only price-bearing speech windows without touching video frames."""
    if not has_audio_stream(clip.path):
        return clip
    local_intervals = [
        (max(0.0, float(start)), min(float(clip.duration), float(end)))
        for start, end in (intervals or [])
        if float(end) > float(start) + 0.02
    ]
    audio_filter = (
        ",".join(
            f"volume=0:enable='between(t,{start:.3f},{end:.3f})'"
            for start, end in local_intervals
        )
        if local_intervals
        else "volume=0"
    )
    run_ffmpeg(
        [
            "ffmpeg", "-y", "-i", clip.path,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "copy", "-af", audio_filter, "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", output_path,
        ]
    )
    return _clone_clip(clip, output_path, get_duration(output_path) or clip.duration)


def _soft_text_cover_bands(regions, frame_width=W, frame_height=H):
    """Build compact bands that fully cover real title/subtitle rows."""
    band_x = max(0, int(round(frame_width * 0.04)))
    band_width = max(4, frame_width - 2 * band_x)
    # Large outlined title cards can legitimately be taller than subtitles.
    # Fourteen percent still prevents an OCR outlier from hiding half a frame.
    max_band_height = max(48, int(round(frame_height * 0.14)))
    bands = []
    for _x_pos, y_pos, _width, height in regions:
        height = max(4, int(height))
        center = max(0.0, min(float(frame_height), float(y_pos) + height / 2.0))
        padded_height = min(
            max_band_height,
            max(48, height + 2 * max(10, int(round(height * 0.35)))),
        )
        top = max(0, min(frame_height - padded_height, int(round(center - padded_height / 2.0))))
        bands.append([band_x, top, band_width, padded_height])
    merged = []
    for band in sorted(bands, key=lambda item: item[1]):
        band_center = band[1] + band[3] / 2.0
        merge_distance = max(18.0, min(band[3], merged[-1][3]) * 0.42) if merged else 0.0
        if merged and abs(band_center - (merged[-1][1] + merged[-1][3] / 2.0)) <= merge_distance:
            previous_center = merged[-1][1] + merged[-1][3] / 2.0
            merged_height = min(max_band_height, max(merged[-1][3], band[3]))
            merged_center = (previous_center + band_center) / 2.0
            merged[-1][1] = max(
                0,
                min(frame_height - merged_height, int(round(merged_center - merged_height / 2.0))),
            )
            merged[-1][3] = merged_height
        else:
            merged.append(band)
    return [tuple(band) for band in merged[:3]]


def _cover_price_text(clip, regions, output_path):
    """Cover detected text only during its observed time window."""
    if not regions:
        return clip
    filters = []
    current = "[0:v]"
    cover_events = []
    for region in regions:
        if len(region) < 4:
            continue
        bands = _soft_text_cover_bands([region[:4]])
        if not bands:
            continue
        start = float(region[4]) if len(region) >= 6 else None
        end = float(region[5]) if len(region) >= 6 else None
        cover_events.append((*bands[0], start, end))
    for index, (x_pos, y_pos, width, height, start, end) in enumerate(cover_events):
        width = max(4, int(width))
        height = max(4, int(height))
        sigma = max(18.0, min(42.0, height / 2.4))
        filters.append(
            f"{current}split=2[price_base_{index}][price_crop_{index}]"
        )
        filters.append(
            f"[price_crop_{index}]crop={width}:{height}:{int(x_pos)}:{int(y_pos)},"
            f"gblur=sigma={sigma:.2f}:steps=2,"
            f"eq=saturation=0.55:brightness=0.035[price_soft_{index}]"
        )
        overlay_label = f"[price_overlay_{index}]"
        enable = (
            f":enable='between(t,{max(0.0, start):.3f},{max(max(0.0, start), end):.3f})'"
            if start is not None and end is not None
            else ""
        )
        filters.append(
            f"[price_base_{index}][price_soft_{index}]overlay={int(x_pos)}:{int(y_pos)}"
            f"{enable}{overlay_label}"
        )
        # Do not tint the blurred crop white: the tint was perceived as an
        # unexplained white rectangle even when the original text had gone.
        current = overlay_label
    filters.append(f"{current}format=yuv420p[price_out]")
    run_ffmpeg(
        [
            "ffmpeg", "-y", "-i", clip.path,
            "-filter_complex", ";".join(filters),
            "-map", "[price_out]", "-map", "0:a:0?",
            *_video_encoder_args(23, "veryfast"),
            "-c:a", "copy", "-movflags", "+faststart", output_path,
        ]
    )
    return _clone_clip(clip, output_path, get_duration(output_path) or clip.duration)


def _clip_overlaps_intervals(clip, intervals):
    clip_start = max(0.0, float(clip.start or 0.0))
    clip_end = clip_start + max(0.0, float(clip.duration or 0.0))
    return any(start < clip_end - 0.02 and end > clip_start + 0.02 for start, end in intervals)


def _clip_local_intervals(clip, intervals):
    """Translate source-timeline intervals into this clip's local timeline."""
    clip_start = max(0.0, float(clip.start or 0.0))
    clip_duration = max(0.0, float(clip.duration or 0.0))
    clip_end = clip_start + clip_duration
    local = []
    for start, end in intervals:
        overlap_start = max(clip_start, float(start))
        overlap_end = min(clip_end, float(end))
        if overlap_end > overlap_start + 0.02:
            local.append((overlap_start - clip_start, overlap_end - clip_start))
    return local


def _price_speech_cover_regions(intervals, clip_duration, frame_width=W, frame_height=H):
    """Cover the subtitle row when ASR hears a price, even if OCR misses it."""
    subtitle_top = int(round(frame_height * 0.70))
    subtitle_height = int(round(frame_height * 0.16))
    duration = max(0.0, float(clip_duration or 0.0))
    return [
        (
            0,
            subtitle_top,
            frame_width,
            subtitle_height,
            max(0.0, float(start) - 0.18),
            min(duration, float(end) + 0.18),
        )
        for start, end in intervals
        if float(end) > float(start) + 0.02
    ]


def filter_price_clips(
    clips,
    mute_spoken_price=False,
    stage_label="素材",
    cover_visible_price=True,
):
    """Keep every shot while muting price speech and soft-covering price text."""
    clips = list(clips)
    if not ENABLE_PRICE_FILTER or not clips:
        return clips
    speech_intervals = {}
    muted_count = 0
    covered_count = 0
    processed = []
    # Audio muting uses stream copy for video, so visual OCR can safely run on
    # every original chunk up front. Two concurrent Tesseract jobs keep an 8GB
    # M2 responsive while removing the old one-process-at-a-time bottleneck.
    visual_regions = (
        _detect_visual_price_regions_batch(clip.path for clip in clips)
        if cover_visible_price
        else {}
    )
    for index, clip in enumerate(clips):
        current = clip
        local_price_intervals = []
        if mute_spoken_price:
            source_path = clip.source_path or clip.path
            if source_path not in speech_intervals:
                speech_intervals[source_path] = detect_spoken_price_intervals(source_path)
            local_price_intervals = _clip_local_intervals(
                clip, speech_intervals[source_path]
            )
            if local_price_intervals:
                muted_path = os.path.join(TMP_DIR, f"price_muted_{stage_label}_{index:03d}.mp4")
                current = _mute_clip_audio(current, muted_path, local_price_intervals)
                muted_count += 1
                print(f"   -> {stage_label}语音价格已静音：{os.path.basename(clip.path)}")

        regions = list(visual_regions.get(clip.path, []))
        if cover_visible_price and local_price_intervals:
            regions.extend(
                _price_speech_cover_regions(local_price_intervals, clip.duration)
            )
        if regions:
            covered_path = os.path.join(TMP_DIR, f"price_covered_{stage_label}_{index:03d}.mp4")
            current = _cover_price_text(current, regions, covered_path)
            covered_count += 1
            print(f"   -> {stage_label}画面价格已使用横向柔化条遮挡：{os.path.basename(clip.path)}")
        processed.append(current)
    if not has_module("pytesseract"):
        print("   -> 未安装 pytesseract，价格文字遮挡暂未启用。")
    if muted_count or covered_count:
        print(
            f"   -> {stage_label}价格处理完成：语音静音 {muted_count} 段，"
            f"文字特效遮挡 {covered_count} 段，未删除镜头。"
        )
    return processed


def filter_price_paths(paths, stage_label="素材"):
    """Apply the same mandatory price policy to complete-path material pools."""
    clips = [
        Clip(
            str(path),
            stage_label,
            get_duration(path),
            source_name=os.path.basename(str(path)),
            source_path=str(path),
        )
        for path in paths
    ]
    return [
        clip.path
        for clip in filter_price_clips(
            clips,
            mute_spoken_price=not MUTE_ORIGINAL_AUDIO,
            stage_label=stage_label,
            cover_visible_price=True,
        )
    ]


def cover_visual_text_clips(clips, stage_label="去重素材池2"):
    """Soft-cover every OCR text line in dedup2 without deleting the shot."""
    clips = list(clips)
    if not ENABLE_PRICE_FILTER or not clips:
        return clips
    visual_regions = _detect_visual_text_regions_batch(clip.path for clip in clips)
    processed = []
    covered_count = 0
    for index, clip in enumerate(clips):
        regions = list(visual_regions.get(clip.path, []))
        if not regions:
            processed.append(clip)
            continue
        covered_path = os.path.join(TMP_DIR, f"text_soft_covered_{index:03d}.mp4")
        processed.append(_cover_price_text(clip, regions, covered_path))
        covered_count += 1
        print(
            f"   -> {stage_label}画面文字已使用横向柔化条遮挡："
            f"{os.path.basename(clip.path)}"
        )
    if not has_module("pytesseract"):
        print("   -> 未安装 pytesseract，画面文字柔化遮挡暂未启用。")
    if covered_count:
        print(
            f"   -> {stage_label}文字处理完成：横向柔化遮挡 {covered_count} 段，"
            "未使用马赛克、未删除镜头。"
        )
    return processed


def order_dedup_clips(clips, requirement_text):
    if not clips:
        return []
    # Speech continuity is more important than a small visual-score gain. The
    # previous global 20% score cut could remove the middle of a spoken
    # sentence. Keep every source in upload/sequence order and only repeat a
    # complete narrative cycle when the user explicitly requests a minimum.
    ordered = sorted(clips, key=lambda c: (c.source, c.sequence, c.start, c.path))
    if "至少一分钟" in requirement_text or "1分钟" in requirement_text:
        min_duration = 60.0
    else:
        min_duration = 0.0
    total = sum(c.duration for c in ordered)
    narrative_cycle = list(ordered)
    while min_duration and total < min_duration and narrative_cycle:
        for extra in narrative_cycle:
            if total >= min_duration:
                break
            ordered.append(extra)
            total += extra.duration
    return ordered


_COPY_PRICE_PATTERN = re.compile(
    r"(¥|￥|\$|到手价|券后|价格|优惠券|折扣|立减|"
    r"(?:只要|仅需)\s*(?:¥|￥|\$)?\s*\d{1,6}(?:\.\d{1,2})?|"
    r"\d{1,6}(?:\.\d{1,2})?\s*(?:多|来)?\s*(?:元|块)(?:钱)?|"
    r"\d{1,3}(?:\.\d{1,2})?\s*折|"
    r"(?:一|二|两|三|四|五|六|七|八|九|十|百|千)+\s*(?:多|来)?\s*(?:元|块)(?:钱)?)",
    re.IGNORECASE,
)


def _clean_generated_copy(text):
    text = (text or "").replace("```", "").strip()
    text = re.sub(r"^(?:自动)?(?:商品)?推广文案(?:草稿)?\s*[:：]\s*", "", text)
    text = re.sub(r"^文案\s*[:：]\s*", "", text)
    pieces = re.split(r"(?<=[。！？!?])|[\r\n]+", text)
    kept = []
    for piece in pieces:
        piece = re.sub(r"^[\s#>*\-\d.、]+", "", piece).strip()
        if (
            piece
            and not _COPY_PRICE_PATTERN.search(piece)
            and not _EXTREME_COPY_PATTERN.search(piece)
        ):
            kept.append(piece)
    cleaned = "".join(kept)
    cleaned = re.sub(r"\s+", "", cleaned)
    return cleaned.strip("\"'“”‘’ ")


def _fallback_promo_copy(
    clips, requirement_text, section_duration=0, product_hint="", target_char_count=None
):
    """Produce enough safe narration for the visible section when AI is unavailable."""
    target = int(target_char_count or _target_copy_character_count(section_duration))
    product = product_hint or "画面中的这款商品"
    sentences = [
        f"先看实际使用场景，{product}在镜头里解决的是画面正在展示的具体操作需求。",
        f"接着从近景看{product}的主体结构、连接位置和手部操作顺序，每一步都能和画面对应。",
        f"拿取{product}之后先确认正反方向，再对准镜头展示的位置完成安装或放置，动作不会跳步。",
        f"完成第一步以后，画面继续展示调整过程，这里能看清{product}各部位怎样配合，而不是只给结果。",
        f"换到侧面角度，{product}的尺寸比例、接触位置和使用后的状态更加明确，也方便判断是否适合自己的场景。",
        f"再回到正面操作，前一个动作完成后才进入下一步，{product}的使用逻辑因此能够连贯看懂。",
        "镜头中没有展示的参数和效果不作推断，文案只说明真实出现的结构、动作和完成状态。",
        f"如果第一次接触{product}，可以跟着画面从准备、对准、固定到调整依次操作，减少反复尝试。",
        f"细节镜头补充了{product}关键部位的实际形态，和前面的完整操作形成对应，不会只讲笼统感受。",
        f"操作完成后的画面还展示了{product}与周围环境的关系，能够据此判断摆放、收取或继续使用是否顺手。",
        f"从问题出现到使用{product}处理，再到最后状态确认，整段演示形成完整过程，前后信息可以接得上。",
        f"适合的人群也应以画面为准：需要完成同类操作、并且重视步骤清楚的人，可以重点参考{product}的实际演示。",
        f"决定之前再核对一次{product}的结构、操作空间和完成效果，比只听概括性的卖点更容易作出准确判断。",
        f"最后按照镜头中的顺序收尾，{product}的用途、操作路径和最终状态都已经交代清楚。",
    ]
    if requirement_text:
        sentences.insert(4, f"本段还会落实这项定制要求：{requirement_text}，但不会补写画面无法证明的功能。")
    copy = ""
    for sentence in sentences:
        copy += sentence
        if len(copy) >= target * 0.96:
            break
    upper_limit = max(5, int(round(target * 1.05)))
    if len(copy) > upper_limit:
        sentence_end = copy.rfind("。", 0, upper_limit + 1)
        if sentence_end >= int(target * 0.90):
            copy = copy[: sentence_end + 1]
    return copy


def _target_copy_character_count(section_duration):
    """Budget narration at the real 1.2x delivery rate, about 5.7 Chinese chars/s."""
    duration = max(0.0, float(section_duration or 0.0))
    return max(5, min(2000, int(round(duration * 5.7))))


def _minimum_copy_character_count(section_duration, target_char_count=None):
    """Reject responses that cannot plausibly narrate the requested section."""
    target = int(target_char_count or _target_copy_character_count(section_duration))
    return max(5, int(round(target * 0.90)))


def _maximum_copy_character_count(section_duration, target_char_count=None):
    """Reject overlong narration before expensive local voice synthesis."""
    target = int(target_char_count or _target_copy_character_count(section_duration))
    return max(5, int(round(target * 1.05)))


def _rebalance_timed_model_copy(text, expected_sentences, maximum_characters):
    """Preserve model wording while making one ordered sentence per video window.

    A reasoning model can return useful ordered copy that misses only the exact
    sentence count. Reflowing that same text is safer than inventing local
    product claims and prevents a later empty retry from discarding a paid,
    usable first response.
    """
    sentence_count = max(0, int(expected_sentences or 0))
    if sentence_count <= 0:
        return (text or "").strip()
    body = re.sub(r"[。！？!?]+", "，", (text or "").strip())
    body = re.sub(r"\s+", "", body).strip("，；、：,. ")
    if len(body) < sentence_count * 2:
        return ""
    max_body_characters = max(sentence_count * 2, int(maximum_characters) - sentence_count)
    if len(body) > max_body_characters:
        body = body[:max_body_characters].rstrip("，；、：,. ")

    punctuation_positions = [
        index + 1 for index, character in enumerate(body) if character in "，；、：,;:"
    ]
    cuts = []
    previous = 0
    for sentence_index in range(1, sentence_count):
        remaining_sentences = sentence_count - sentence_index
        minimum_cut = previous + 2
        maximum_cut = len(body) - remaining_sentences * 2
        ideal_cut = int(round(len(body) * sentence_index / sentence_count))
        candidates = [
            position
            for position in punctuation_positions
            if minimum_cut <= position <= maximum_cut
        ]
        if candidates:
            cut = min(candidates, key=lambda position: abs(position - ideal_cut))
            # A distant comma is worse than a balanced character boundary.
            if abs(cut - ideal_cut) > max(4, len(body) // (sentence_count * 2)):
                cut = max(minimum_cut, min(maximum_cut, ideal_cut))
        else:
            cut = max(minimum_cut, min(maximum_cut, ideal_cut))
        cuts.append(cut)
        previous = cut

    chunks = []
    start = 0
    for end in [*cuts, len(body)]:
        chunk = body[start:end].strip("，；、：,. ")
        if not chunk:
            return ""
        chunks.append(chunk + "。")
        start = end
    return "".join(chunks) if len(chunks) == sentence_count else ""


def _product_hint_from_clips(clips):
    names = []
    for clip in clips:
        raw_name = str(getattr(clip, "source_name", "") or "").strip()
        if not raw_name:
            continue
        name = os.path.splitext(os.path.basename(raw_name))[0]
        name = re.sub(r"^[\d_\-\s]{6,}", "", name).strip(" _-.")
        if not name or name.lower() in {"video", "image", "素材", "视频"}:
            continue
        if name not in names:
            names.append(name)
    return "、".join(names[:4])


def extract_vision_frames(clips, max_frames=VISION_MAX_FRAMES):
    if not clips:
        return []
    frame_dir = os.path.join(TMP_DIR, "dedup2_vision_frames")
    os.makedirs(frame_dir, exist_ok=True)
    durations = [max(0.1, float(clip.duration or get_duration(clip.path) or 0.1)) for clip in clips]
    total_duration = sum(durations)
    count = min(max_frames, max(4, int(round(total_duration / 3.5))))
    sample_times = [(index + .5) * total_duration / count for index in range(count)]
    frames = []
    clip_index = 0
    clip_start = 0.0
    for frame_index, sample_time in enumerate(sample_times):
        while clip_index < len(clips) - 1 and sample_time >= clip_start + durations[clip_index]:
            clip_start += durations[clip_index]
            clip_index += 1
        clip = clips[clip_index]
        local_time = sample_time - clip_start
        local_time = max(0.05, min(local_time, max(durations[clip_index] - 0.05, 0.05)))
        frame_path = os.path.join(frame_dir, f"frame_{frame_index:03d}.jpg")
        try:
            run_ffmpeg(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    f"{local_time:.3f}",
                    "-i",
                    clip.path,
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=640:-2",
                    "-q:v",
                    "3",
                    frame_path,
                ]
            )
        except RuntimeError:
            continue
        if os.path.exists(frame_path) and os.path.getsize(frame_path) > 0:
            frames.append(frame_path)
    return frames


def extract_timeline_vision_frames(
    video_path,
    section_duration,
    max_frames=VISION_MAX_FRAMES,
):
    """Sample the exact rendered master timeline instead of pre-trim clip totals."""
    available_duration = max(0.0, get_duration(video_path))
    duration = min(
        available_duration,
        max(0.0, float(section_duration or 0.0)),
    )
    if duration <= 0.05:
        return []
    frame_dir = os.path.join(TMP_DIR, "dedup2_timeline_vision_frames")
    os.makedirs(frame_dir, exist_ok=True)
    count = min(max_frames, max(4, int(round(duration / 3.5))))
    sample_times = [(index + 0.5) * duration / count for index in range(count)]
    frames = []
    for frame_index, sample_time in enumerate(sample_times):
        frame_path = os.path.join(frame_dir, f"window_{frame_index:03d}.jpg")
        try:
            run_ffmpeg(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    f"{sample_time:.3f}",
                    "-i",
                    video_path,
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=640:-2",
                    "-q:v",
                    "3",
                    frame_path,
                ]
            )
        except RuntimeError:
            continue
        if os.path.exists(frame_path) and os.path.getsize(frame_path) > 0:
            frames.append(frame_path)
    return frames


def _build_vision_prompt(
    requirement_text,
    section_duration,
    product_hint="",
    target_char_count=None,
    sync_mode=SYNC_MODE,
    frame_count=0,
):
    target_chars = int(target_char_count or _target_copy_character_count(section_duration))
    hint_text = (
        f"素材文件名提供的商品线索是“{product_hint}”，请结合画面核实后围绕该商品写作。"
        if product_hint
        else "请先从全部画面确认具体商品名称，再开始写作。"
    )
    sync_label = "话术对接视频" if sync_mode == "script_to_video" else "视频对接话术"
    sync_instruction = (
        "先完成口播逻辑，再让每句话对应一组连续镜头；句号就是换镜头边界。"
        if sync_mode == "script_to_video"
        else "严格按图片出现的时间顺序写口播，使每句话描述当时可见的商品、动作或结果。"
    )
    timed_sentence_instruction = ""
    narrative_structure_instruction = (
        "内容按画面顺序形成完整逻辑：开头指出具体使用问题，中段逐步说明商品结构和操作，"
        "随后说明可见结果与适用人群，结尾给出自然收束。"
    )
    if sync_mode == "video_to_script" and frame_count:
        interval_seconds = max(0.1, float(section_duration) / int(frame_count))
        chars_per_sentence = max(8, int(round(target_chars / int(frame_count))))
        timed_sentence_instruction = (
            f"共有{int(frame_count)}张等间隔采样图，相邻画面约{interval_seconds:.1f}秒。"
            f"正文必须恰好写{int(frame_count)}句，每句约{chars_per_sentence}个汉字，"
            "第1句只对应第1张图，第2句只对应第2张图，依此类推；"
            "每句话必须用句号结束，画面没有出现的痛点、功效、动作一律不说；"
            "动作出现时就要说到，禁止提前讲尚未出现的动作，也禁止画面过去后才补讲。"
        )
        narrative_structure_instruction = (
            "不要套用固定的痛点、卖点、结尾结构；只按每个时间窗真实出现的画面和画面字幕逐句口播。"
        )
    return (
        "你是电商短视频文案导演。以下图片按照成片时间顺序取自同一段商品视频。"
        f"{hint_text}"
        "请识别具体商品、使用场景、用户痛点、结构细节、操作步骤和画面可证明的结果，"
        f"写一段适合自然语速中文配音的软广，正文必须达到{target_chars}个汉字左右，控制在目标字数的95%至105%，覆盖约{section_duration:.0f}秒画面。"
        f"{narrative_structure_instruction}"
        "相邻句必须用上一步动作、部件或结果承接下一步，不能突然换话题。"
        f"当前同步策略是“{sync_label}”：{sync_instruction}"
        f"{timed_sentence_instruction}"
        "每句话都必须紧扣画面里的同一件商品及其真实操作；禁止使用提升品质、解放双手、居家必备、方便实用等可套在任何商品上的笼统话术。"
        "只输出配音正文，不要标题、列表、Markdown、解释和引号。"
        "不得编造画面无法支持的功效，不得出现任何价格、金额、优惠券、折扣或到手价。"
        f"用户自定义要求：{requirement_text or '突出真实体验，语言自然，不要价格信息。'}"
    )


def _parse_json_tail(output):
    for index in range(len(output) - 1, -1, -1):
        if output[index] != "{":
            continue
        try:
            return json.loads(output[index:])
        except json.JSONDecodeError:
            continue
    raise RuntimeError("OpenClaw 没有返回可解析的 JSON")


def _openclaw_generate_copy(frame_paths, prompt):
    openclaw = shutil.which("openclaw")
    if not openclaw:
        raise RuntimeError("未找到 OpenClaw 命令")
    command = [
        openclaw,
        "--no-color",
        "infer",
        "model",
        "run",
        "--gateway",
        "--model",
        OPENCLAW_VISION_MODEL,
        "--prompt",
        prompt,
        "--json",
    ]
    for frame_path in frame_paths:
        command.extend(["--file", frame_path])
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=VISION_TIMEOUT,
            env=runtime_service_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"OpenClaw 视觉模型超过 {VISION_TIMEOUT} 秒未响应") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "未知错误").strip()[-400:]
        raise RuntimeError(f"OpenClaw 视觉模型调用失败: {detail}")
    envelope = _parse_json_tail(result.stdout)
    outputs = envelope.get("outputs") or []
    text = "".join(str(item.get("text") or "") for item in outputs if isinstance(item, dict)).strip()
    if not text:
        raise RuntimeError("OpenClaw 视觉模型返回了空文案")
    provider = envelope.get("provider") or "openclaw"
    model = envelope.get("model") or OPENCLAW_VISION_MODEL
    return text, f"openclaw:{provider}/{model}"


def _codex_generate_copy(frame_paths, prompt):
    codex_bin = os.environ.get("V16_CODEX_BIN", "").strip() or shutil.which("codex")
    if not codex_bin:
        bundled_codex = "/Applications/ChatGPT.app/Contents/Resources/codex"
        codex_bin = bundled_codex if os.path.isfile(bundled_codex) else ""
    if not codex_bin:
        raise RuntimeError("未找到本机 Codex 命令")

    service_env = runtime_service_env()
    bridge_home = os.environ.get("V16_CODEX_BRIDGE_HOME", "").strip()
    if not bridge_home:
        bridge_home = os.path.join(
            tempfile.gettempdir(), f"v16-codex-bridge-{os.getuid()}"
        )
    source_codex_home = os.environ.get("CODEX_HOME", "").strip() or os.path.expanduser("~/.codex")
    source_auth = os.path.join(source_codex_home, "auth.json")
    bridge_auth = os.path.join(bridge_home, "auth.json")
    try:
        os.makedirs(bridge_home, mode=0o700, exist_ok=True)
        os.chmod(bridge_home, 0o700)
        if not os.path.exists(bridge_auth) and os.path.isfile(source_auth):
            os.symlink(source_auth, bridge_auth)
        if os.path.exists(bridge_auth):
            service_env["CODEX_HOME"] = bridge_home
    except OSError as exc:
        raise RuntimeError(f"无法准备 Codex 独立运行目录: {exc}") from exc

    os.makedirs(TMP_DIR, exist_ok=True)
    output_path = os.path.join(TMP_DIR, f"codex_copy_{os.getpid()}.txt")
    command = [
        codex_bin,
        "--ask-for-approval",
        "never",
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--output-last-message",
        output_path,
        "--cd",
        BASE_DIR,
    ]
    model = os.environ.get("V16_CODEX_MODEL", "").strip()
    if model:
        command.extend(["--model", model])
    for frame_path in frame_paths:
        command.extend(["--image", frame_path])
    prompt = (
        prompt
        + "\n你处于只读文案任务，不得调用工具、修改文件或执行命令。"
        + "只输出最终配音正文，不要解释、标题、列表、Markdown或引号。"
    )
    # --image accepts one or more values and can consume a following positional
    # prompt. Match the proven Ranliang bridge: terminate image parsing with
    # the stdin marker and send the prompt through stdin.
    command.append("-")
    try:
        result = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=VISION_TIMEOUT,
            env=service_env,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "未知错误").strip()[-600:]
            raise RuntimeError(f"本机 Codex 调用失败: {detail}")
        try:
            with open(output_path, "r", encoding="utf-8", errors="ignore") as handle:
                text = handle.read().strip()
        except OSError as exc:
            raise RuntimeError(f"本机 Codex 没有生成结果文件: {exc}") from exc
        if not text:
            raise RuntimeError("本机 Codex 返回了空文案")
        return text, f"codex:{model or 'default'}"
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"本机 Codex 超过 {VISION_TIMEOUT} 秒未响应") from exc
    finally:
        try:
            os.remove(output_path)
        except OSError:
            pass


def _extract_openai_output_text(result):
    direct_text = str(result.get("output_text") or "").strip()
    if direct_text:
        return direct_text

    text_parts = []
    refusal_parts = []
    for item in result.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "output_text" and part.get("text"):
                text_parts.append(str(part["text"]))
            elif part.get("type") == "refusal" and part.get("refusal"):
                refusal_parts.append(str(part["refusal"]))
    text = "".join(text_parts).strip()
    if text:
        return text
    if refusal_parts:
        raise RuntimeError(f"OpenAI 视觉模型拒绝了请求: {' '.join(refusal_parts)[:300]}")
    raise RuntimeError("OpenAI 视觉模型没有返回有效文案")


def _openai_generate_copy(frame_paths, prompt):
    service_env = runtime_service_env()
    api_key = (
        service_env.get("V16_OPENAI_API_KEY")
        or service_env.get("OPENAI_API_KEY")
    )
    if os.environ.get("V16_VISION_USE_GEMINI", "1") == "0":
        raise RuntimeError("已关闭 Gemini 视觉兜底")
    if not api_key:
        raise RuntimeError("未配置 OPENAI_API_KEY")

    content = [{"type": "input_text", "text": prompt}]
    for frame_path in frame_paths:
        with open(frame_path, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("ascii")
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{encoded}",
                "detail": "auto",
            }
        )

    model = os.environ.get("V16_OPENAI_MODEL", OPENAI_VISION_MODEL).strip()
    payload = {
        "model": model,
        "input": [{"role": "user", "content": content}],
        "reasoning": {
            "effort": os.environ.get("V16_OPENAI_REASONING_EFFORT", "low"),
            "context": "current_turn",
        },
        "text": {"verbosity": "low"},
        "max_output_tokens": 4096,
        "store": False,
    }
    api_base = os.environ.get(
        "V16_OPENAI_API_BASE",
        service_env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1",
    ).rstrip("/")
    request = urllib.request.Request(
        f"{api_base}/responses",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    proxy_url = service_env.get("HTTPS_PROXY") or service_env.get("https_proxy")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        if proxy_url
        else urllib.request.ProxyHandler()
    )
    try:
        with opener.open(request, timeout=VISION_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"OpenAI 视觉接口返回 HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"OpenAI 视觉接口调用失败: {exc}") from exc

    text = _extract_openai_output_text(result)
    return text, f"openai:{model}"


def _extract_qwen_output_text(result, provider_label="千问"):
    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"{provider_label}视觉接口没有返回有效文案") from exc
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        text = "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict)
        ).strip()
    else:
        text = ""
    if not text:
        try:
            finish_reason = result["choices"][0].get("finish_reason") or "unknown"
        except (KeyError, IndexError, TypeError, AttributeError):
            finish_reason = "unknown"
        raise RuntimeError(
            f"{provider_label}视觉接口返回了空文案（finish_reason={finish_reason}）"
        )
    return text


def _qwen_generate_copy(frame_paths, prompt):
    """Call the department-owned Qwen vision API without local fallbacks."""
    api_key = os.environ.get("V16_QWEN_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未导入千问 API Key")
    if not frame_paths:
        raise RuntimeError("没有可交给千问分析的画面")

    content = [{"type": "text", "text": prompt}]
    for frame_path in frame_paths:
        with open(frame_path, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            }
        )
    payload = {
        "model": QWEN_VISION_MODEL,
        "messages": [{"role": "user", "content": content}],
        "enable_thinking": False,
        "temperature": 0.45,
        "max_tokens": 4096,
        "stream": False,
    }
    request = urllib.request.Request(
        f"{QWEN_API_BASE.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    service_env = runtime_service_env()
    proxy_url = service_env.get("HTTPS_PROXY") or service_env.get("https_proxy")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        if proxy_url
        else urllib.request.ProxyHandler()
    )
    try:
        with opener.open(request, timeout=VISION_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"千问视觉接口返回 HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"千问视觉接口调用失败: {exc}") from exc

    _qwen_generate_copy.last_usage = (
        result.get("usage") if isinstance(result.get("usage"), dict) else {}
    )
    _record_vision_usage("qwen", QWEN_VISION_MODEL, _qwen_generate_copy.last_usage)
    return _extract_qwen_output_text(result), f"qwen:{QWEN_VISION_MODEL}"


_qwen_generate_copy.last_usage = {}


def _kimi_generate_copy(frame_paths, prompt):
    """Call the department-owned Kimi K3 vision API without local fallbacks."""
    api_key = os.environ.get("V16_KIMI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未导入 Kimi API Key")
    if not frame_paths:
        raise RuntimeError("没有可交给 Kimi 分析的画面")

    content = []
    for frame_path in frame_paths:
        with open(frame_path, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            }
        )
    content.append({"type": "text", "text": prompt})
    payload = {
        "model": KIMI_VISION_MODEL,
        "messages": [{"role": "user", "content": content}],
        "reasoning_effort": "low",
        # kimi-k3 can spend most of the completion budget on internal
        # reasoning for a 24-frame request. 4096 occasionally ended with an
        # empty final message; leave enough room for the actual copy.
        "max_completion_tokens": KIMI_MAX_COMPLETION_TOKENS,
        "stream": False,
    }
    request = urllib.request.Request(
        f"{KIMI_API_BASE.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    service_env = runtime_service_env()
    proxy_url = service_env.get("HTTPS_PROXY") or service_env.get("https_proxy")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        if proxy_url
        else urllib.request.ProxyHandler()
    )
    try:
        with opener.open(request, timeout=VISION_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"Kimi 视觉接口返回 HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Kimi 视觉接口调用失败: {exc}") from exc

    _kimi_generate_copy.last_usage = (
        result.get("usage") if isinstance(result.get("usage"), dict) else {}
    )
    _record_vision_usage("kimi", KIMI_VISION_MODEL, _kimi_generate_copy.last_usage)
    return _extract_qwen_output_text(
        result, provider_label="Kimi"
    ), f"kimi:{KIMI_VISION_MODEL}"


_kimi_generate_copy.last_usage = {}


def _selected_external_vision_generate_copy(frame_paths, prompt):
    provider = _selected_vision_provider()
    if provider == "qwen":
        return _qwen_generate_copy(frame_paths, prompt)
    if provider == "kimi":
        return _kimi_generate_copy(frame_paths, prompt)
    raise RuntimeError("没有选择有效的外部视觉模型")


def _gemini_generate_copy(
    frame_paths,
    requirement_text,
    section_duration,
    product_hint="",
    target_char_count=None,
    sync_mode=SYNC_MODE,
):
    reuse_copy_file = os.environ.get("V16_VISION_REUSE_COPY_FILE", "").strip()
    if reuse_copy_file:
        try:
            with open(reuse_copy_file, "r", encoding="utf-8") as handle:
                reused_copy = handle.read().strip()
        except OSError as exc:
            raise RuntimeError(f"无法读取复用的 Codex 文案: {exc}") from exc
        if reused_copy:
            return reused_copy, "codex:cached"
    mock_copy = os.environ.get("V16_VISION_MOCK_RESPONSE", "").strip()
    if mock_copy:
        return mock_copy, "mock"
    if not frame_paths:
        raise RuntimeError("未能从去重素材池2提取有效画面")

    prompt = _build_vision_prompt(
        requirement_text,
        section_duration,
        product_hint,
        target_char_count,
        sync_mode,
        len(frame_paths),
    )
    if _external_vision_is_selected():
        try:
            return _selected_external_vision_generate_copy(frame_paths, prompt)
        except RuntimeError as exc:
            provider_name = _selected_vision_provider()
            _remember_vision_provider_failure(provider_name, exc)
            raise RuntimeError(
                f"{provider_name.upper()} 画面分析失败，已按设置停止且未切换本机模型：{exc}"
            ) from exc
    provider_errors = []
    if os.environ.get("V16_VISION_USE_CODEX", "1") != "0" and _vision_provider_enabled("codex"):
        try:
            return _codex_generate_copy(frame_paths, prompt)
        except RuntimeError as exc:
            _remember_vision_provider_failure("codex", exc)
            provider_errors.append(str(exc))
    elif not _vision_provider_enabled("codex"):
        provider_errors.append(_vision_provider_failure_detail("codex"))

    if os.environ.get("V16_VISION_USE_OPENAI", "1") != "0" and _vision_provider_enabled("openai"):
        try:
            return _openai_generate_copy(frame_paths, prompt)
        except RuntimeError as exc:
            _remember_vision_provider_failure("openai", exc)
            provider_errors.append(str(exc))
    elif not _vision_provider_enabled("openai"):
        provider_errors.append(_vision_provider_failure_detail("openai"))

    openclaw_error = ""
    if os.environ.get("V16_VISION_USE_OPENCLAW", "1") != "0" and _vision_provider_enabled("openclaw"):
        try:
            return _openclaw_generate_copy(frame_paths, prompt)
        except RuntimeError as exc:
            openclaw_error = str(exc)
            _remember_vision_provider_failure("openclaw", exc)
            provider_errors.append(openclaw_error)
    elif not _vision_provider_enabled("openclaw"):
        provider_errors.append(_vision_provider_failure_detail("openclaw"))

    service_env = runtime_service_env()
    api_key = (
        service_env.get("V16_GEMINI_API_KEY")
        or service_env.get("GEMINI_API_KEY")
        or service_env.get("GOOGLE_API_KEY")
    )
    if not api_key:
        detail = f"；{'；'.join(provider_errors)}" if provider_errors else ""
        raise RuntimeError(f"未找到可用的视觉模型接口{detail}")
    if not _vision_provider_enabled("gemini"):
        detail = _vision_provider_failure_detail("gemini")
        raise RuntimeError(detail or "Gemini 本次任务已停止重复等待")

    parts = [{"text": prompt}]
    for frame_path in frame_paths:
        with open(frame_path, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("ascii")
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": encoded}})

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.45,
            "maxOutputTokens": 4096,
            "responseMimeType": "text/plain",
        },
    }
    api_base = os.environ.get(
        "V16_GEMINI_API_BASE",
        "https://generativelanguage.googleapis.com/v1beta",
    ).rstrip("/")
    model = urllib.parse.quote(VISION_MODEL, safe="-_.")
    endpoint = f"{api_base}/models/{model}:generateContent?key={urllib.parse.quote(api_key, safe='')}"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    proxy_url = service_env.get("HTTPS_PROXY") or service_env.get("https_proxy")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        if proxy_url
        else urllib.request.ProxyHandler()
    )
    try:
        with opener.open(request, timeout=VISION_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:300]
        error = RuntimeError(f"Gemini 视觉接口返回 HTTP {exc.code}: {detail}")
        _remember_vision_provider_failure("gemini", error)
        raise error from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        error = RuntimeError(f"Gemini 视觉接口调用失败: {exc}")
        _remember_vision_provider_failure("gemini", error)
        raise error from exc

    try:
        response_parts = result["candidates"][0]["content"]["parts"]
        text = "".join(part.get("text", "") for part in response_parts)
    except (KeyError, IndexError, TypeError) as exc:
        error = RuntimeError("Gemini 视觉接口没有返回有效文案")
        _remember_vision_provider_failure("gemini", error)
        raise error from exc
    if not text.strip():
        error = RuntimeError("Gemini 视觉接口返回了空文案")
        _remember_vision_provider_failure("gemini", error)
        raise error
    return text, f"gemini:{VISION_MODEL}"


def write_promo_copy(
    dedup2_clips,
    requirement_text,
    section_duration,
    target_char_count=None,
    sync_mode=SYNC_MODE,
    vision_video_path=None,
):
    frame_paths = (
        extract_timeline_vision_frames(vision_video_path, section_duration)
        if vision_video_path
        else extract_vision_frames(dedup2_clips)
    )
    product_hint = _product_hint_from_clips(dedup2_clips)
    used_ai = False
    provider = "fallback"
    warning = ""
    try:
        raw_copy, provider = _gemini_generate_copy(
            frame_paths,
            requirement_text,
            section_duration,
            product_hint,
            target_char_count,
            sync_mode,
        )
        copy = _clean_generated_copy(raw_copy)
        minimum_characters = _minimum_copy_character_count(
            section_duration, target_char_count
        )
        maximum_characters = _maximum_copy_character_count(
            section_duration, target_char_count
        )
        expected_sentences = len(frame_paths) if sync_mode == "video_to_script" else 0
        sentence_count = len(_narration_sentences(copy))
        invalid_length = not minimum_characters <= len(copy) <= maximum_characters
        if invalid_length or (
            expected_sentences and sentence_count != expected_sentences
        ):
            first_copy = copy
            first_provider = provider
            length_feedback = (
                f"上一版正文有{len(copy)}字，要求严格控制在"
                f"{minimum_characters}至{maximum_characters}字之间，"
            )
            retry_requirement = (
                f"{requirement_text} {length_feedback}"
                f"请重新完整写作，且只能围绕"
                f"{product_hint or '画面中的商品'}和画面展示的操作。"
            )
            if expected_sentences:
                per_sentence_max = max(8, maximum_characters // expected_sentences)
                retry_requirement += (
                    f"必须严格写成{expected_sentences}句，每句用句号结束，"
                    f"每句最多{per_sentence_max}字，"
                    "每句只说对应顺序采样图当时可见的内容。"
                )
            retry_copy = ""
            retry_provider = provider
            retry_error = None
            try:
                raw_copy, retry_provider = _gemini_generate_copy(
                    frame_paths,
                    retry_requirement,
                    section_duration,
                    product_hint,
                    target_char_count,
                    sync_mode,
                )
                retry_copy = _clean_generated_copy(raw_copy)
            except Exception as exc:
                retry_error = exc
            if retry_copy:
                copy = retry_copy
                provider = retry_provider
            sentence_count = len(_narration_sentences(copy))
            retry_invalid = not minimum_characters <= len(copy) <= maximum_characters or (
                expected_sentences and sentence_count != expected_sentences
            )
            if retry_error or retry_invalid:
                candidates = [(first_copy, first_provider)]
                if retry_copy:
                    candidates.append((retry_copy, retry_provider))
                target_characters = (minimum_characters + maximum_characters) // 2
                recovery_copy, recovery_provider = min(
                    candidates,
                    key=lambda item: abs(len(item[0]) - target_characters),
                )
                repaired = (
                    _rebalance_timed_model_copy(
                        recovery_copy,
                        expected_sentences,
                        maximum_characters,
                    )
                    if expected_sentences
                    else ""
                )
                if not repaired:
                    if retry_error:
                        raise retry_error
                    raise RuntimeError(
                        "视觉模型两次都未生成合格的逐镜头话术："
                        f"需要{minimum_characters}至{maximum_characters}字"
                        + (
                            f"且必须为{expected_sentences}句，实际{sentence_count}句"
                            if expected_sentences
                            else ""
                        )
                    )
                copy = repaired
                provider = recovery_provider
                warning = (
                    "模型重试未返回合格终稿，已保留首次有效画面文案并按原顺序"
                    f"重排为{expected_sentences}个时间窗；未切换 Codex 或其他模型。"
                )
                if retry_error:
                    warning += f" 重试错误：{retry_error}"
                print(f"[WARN] {warning}")
        used_ai = True
        print(f"   -> 视觉模型已分析 {len(frame_paths)} 张画面并生成推广文案。")
    except Exception as exc:
        warning = str(exc)
        if _external_api_keys_required() or (
            sync_mode == "video_to_script" and frame_paths
        ):
            raise RuntimeError(
                "外部视觉模型文案生成失败，为避免再次出现话术与画面错配，本次已停止："
                f"{warning}"
            ) from exc
        provider = "fallback"
        copy = _clean_generated_copy(
            _fallback_promo_copy(
                dedup2_clips,
                requirement_text,
                section_duration,
                product_hint,
                target_char_count,
            )
        )
        print(f"[WARN] 视觉文案未调用成功，已使用安全备用文案: {warning}")

    with open(PROMO_COPY_FILE, "w", encoding="utf-8") as handle:
        handle.write(copy.rstrip() + "\n")
    metadata = {
        "provider": provider,
        "used_visual_model": used_ai,
        "model": _provider_model(provider) if used_ai else None,
        "frame_count": len(frame_paths),
        "product_hint": product_hint,
        "section_duration": round(section_duration, 3),
        "target_character_count": int(
            target_char_count or _target_copy_character_count(section_duration)
        ),
        "character_count": len(copy),
        "warning": warning,
        "sync_mode": sync_mode,
        "sync_mode_label": (
            "话术对接视频" if sync_mode == "script_to_video" else "视频对接话术"
        ),
        "vision_timeline_source": (
            "rendered-master" if vision_video_path else "selected-clips"
        ),
    }
    if provider.startswith(("qwen:", "kimi:")):
        _attach_cost_metadata(metadata)
    with open(PROMO_META_FILE, "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
    return copy, metadata


def _provider_model(provider):
    if (
        provider.startswith("codex:")
        or provider.startswith("openai:")
        or provider.startswith("gemini:")
        or provider.startswith("qwen:")
        or provider.startswith("kimi:")
    ):
        return provider.split(":", 1)[1]
    if provider.startswith("openclaw:"):
        return provider.rsplit("/", 1)[-1]
    return None


def _openai_generate_tts(copy_text, output_path):
    service_env = runtime_service_env()
    api_key = service_env.get("V16_OPENAI_API_KEY") or service_env.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("未配置 OPENAI_API_KEY")
    payload = {
        "model": OPENAI_TTS_MODEL,
        "voice": OPENAI_TTS_VOICE,
        "input": copy_text,
        "instructions": OPENAI_TTS_INSTRUCTIONS,
        "response_format": "mp3",
    }
    api_base = os.environ.get(
        "V16_OPENAI_API_BASE",
        service_env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1",
    ).rstrip("/")
    request = urllib.request.Request(
        f"{api_base}/audio/speech",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    proxy_url = service_env.get("HTTPS_PROXY") or service_env.get("https_proxy")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        if proxy_url
        else urllib.request.ProxyHandler()
    )
    try:
        with opener.open(request, timeout=TTS_TIMEOUT) as response:
            audio_data = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"OpenAI 配音接口返回 HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"OpenAI 配音接口调用失败: {exc}") from exc
    if len(audio_data) < 256:
        raise RuntimeError("OpenAI 配音接口没有返回有效音频")
    with open(output_path, "wb") as handle:
        handle.write(audio_data)
    return {
        "provider": "openai",
        "model": OPENAI_TTS_MODEL,
        "voice": OPENAI_TTS_VOICE,
        "rate": "natural",
    }


def _minimax_tts_cache_path(copy_text):
    cache_key = hashlib.sha256(
        (
            "minimax-bailian-v2-word-timeline\0"
            + copy_text
            + f"\0{MINIMAX_TTS_MODEL}\0{MINIMAX_TTS_VOICE}"
            + f"\0{MINIMAX_TTS_SPEED:.3f}\0{MINIMAX_TTS_VOLUME:.3f}"
            + f"\0{MINIMAX_TTS_PITCH}"
            + f"\0Chinese\0sample_rate=44100\0bitrate={MINIMAX_TTS_BITRATE}"
        ).encode("utf-8")
    ).hexdigest()
    cache_dir = os.environ.get(
        "V16_TTS_CACHE_DIR",
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            ".v16_tts_cache",
        ),
    )
    return os.path.join(cache_dir, f"minimax_{cache_key}.mp3")


def _decode_minimax_audio(encoded_audio):
    if not isinstance(encoded_audio, str) or not encoded_audio.strip():
        raise RuntimeError("MiniMax 配音接口没有返回音频数据")
    encoded_audio = encoded_audio.strip()
    try:
        return bytes.fromhex(encoded_audio)
    except ValueError:
        try:
            return base64.b64decode(encoded_audio, validate=True)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("MiniMax 配音接口返回了无法解析的音频数据") from exc


def _minimax_word_timeline(copy_text, subtitle_payload, audio_duration):
    """Convert MiniMax word offsets into one exact interval per narration sentence."""
    if not isinstance(subtitle_payload, list) or not subtitle_payload:
        return None
    timed_words = []
    for segment in subtitle_payload:
        if not isinstance(segment, dict):
            continue
        try:
            segment_text_begin = int(segment.get("text_begin") or 0)
        except (TypeError, ValueError):
            segment_text_begin = 0
        words = segment.get("timestamped_words") or []
        for word in words:
            if not isinstance(word, dict):
                continue
            try:
                word_begin = int(word.get("word_begin"))
                word_end = int(word.get("word_end"))
                time_begin = float(word.get("time_begin")) / 1000.0
                time_end = float(word.get("time_end")) / 1000.0
            except (TypeError, ValueError):
                continue
            if segment_text_begin > 0 and word_begin < segment_text_begin:
                word_begin += segment_text_begin
                word_end += segment_text_begin
            if word_end > word_begin and time_end > time_begin:
                timed_words.append(
                    {
                        "text_begin": word_begin,
                        "text_end": word_end,
                        "time_begin": max(0.0, time_begin),
                        "time_end": max(0.0, time_end),
                    }
                )
    timed_words.sort(key=lambda item: (item["text_begin"], item["time_begin"]))
    if not timed_words:
        return None

    sentence_spans = []
    for match in re.finditer(r"[^。！？!?]+[。！？!?]?", copy_text or ""):
        raw_sentence = match.group(0)
        sentence = raw_sentence.strip()
        if not sentence:
            continue
        leading = len(raw_sentence) - len(raw_sentence.lstrip())
        start = match.start() + leading
        sentence_spans.append((sentence, start, start + len(sentence)))
    if len(sentence_spans) != len(_narration_sentences(copy_text)):
        return None

    phrases = []
    cursor = 0.0
    for index, (sentence, _start, end) in enumerate(sentence_spans):
        if index == len(sentence_spans) - 1:
            phrase_end = float(audio_duration)
        else:
            eligible = [
                word["time_end"]
                for word in timed_words
                if word["text_end"] <= end
            ]
            if not eligible:
                return None
            phrase_end = max(eligible)
        phrase_end = min(float(audio_duration), max(cursor, phrase_end))
        if phrase_end <= cursor:
            return None
        phrases.append(
            {
                "text": sentence,
                "start": round(cursor, 6),
                "end": round(phrase_end, 6),
                "duration": round(phrase_end - cursor, 6),
            }
        )
        cursor = phrase_end
    return {
        "source": "minimax-word-timestamps",
        "phrases": phrases,
    }


def _minimax_generate_tts(copy_text, output_path, preserve_sentences=False):
    """Generate the configured cloned voice through Alibaba Cloud Bailian."""
    text = (copy_text or "").strip()
    if not text:
        raise RuntimeError("MiniMax 配音文案为空")
    if len(text) >= 10000:
        raise RuntimeError("MiniMax 单次配音文案必须少于 10000 字符")

    estimated_cost = len(text) * MINIMAX_TTS_PRICE_PER_10K_CNY / 10000.0
    if estimated_cost > MINIMAX_TTS_MAX_ESTIMATED_CNY:
        raise RuntimeError(
            "MiniMax 本次配音预计费用 "
            f"{estimated_cost:.3f} 元，超过单条上限 "
            f"{MINIMAX_TTS_MAX_ESTIMATED_CNY:.2f} 元"
        )

    service_env = runtime_service_env()
    api_key = service_env.get("V16_VOICE_API_KEY", "").strip()
    if not api_key and not _external_api_keys_required():
        # Keep older command-line workflows working, but the web app explicitly
        # requires the department-owned voice key and never reaches this branch.
        api_key = service_env.get("V16_DASHSCOPE_API_KEY") or service_env.get(
            "DASHSCOPE_API_KEY"
        )
    if not api_key:
        raise RuntimeError(
            "未导入声音 API Key；固定音色 ddsales0817d 未调用，且不会切换其他声音"
        )

    cache_path = _minimax_tts_cache_path(text)
    cache_metadata_path = os.path.splitext(cache_path)[0] + ".meta.json"
    cache_timeline_path = os.path.splitext(cache_path)[0] + ".timeline.json"
    phrase_timeline_path = os.path.join(TMP_DIR, "dedup2_tts_phrase_timeline.json")
    _remove_promo_artifact(phrase_timeline_path)
    cached_info = {}
    cache_has_timeline = os.path.isfile(cache_timeline_path)
    if (
        os.path.isfile(cache_path)
        and os.path.getsize(cache_path) > 512
        and (not preserve_sentences or cache_has_timeline)
    ):
        shutil.copyfile(cache_path, output_path)
        if cache_has_timeline:
            shutil.copyfile(cache_timeline_path, phrase_timeline_path)
        try:
            with open(cache_metadata_path, "r", encoding="utf-8") as handle:
                cached_info = json.load(handle)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            cached_info = {}
        info = {
            "provider": "minimax-bailian",
            "model": MINIMAX_TTS_MODEL,
            "voice": MINIMAX_TTS_VOICE,
            "rate": f"{MINIMAX_TTS_SPEED:.2f}x",
            "usage_characters": int(cached_info.get("usage_characters") or len(text)),
            "estimated_cost_cny": float(
                cached_info.get("estimated_cost_cny") or estimated_cost
            ),
            "cached": True,
        }
        if os.path.isfile(phrase_timeline_path):
            info["timeline_path"] = phrase_timeline_path
        return info

    payload = {
        "model": MINIMAX_TTS_MODEL,
        "input": {
            "action": "speech_synthesis",
            "text": text,
            "voice_setting": {
                "voice_id": MINIMAX_TTS_VOICE,
                "speed": MINIMAX_TTS_SPEED,
                "vol": MINIMAX_TTS_VOLUME,
                "pitch": MINIMAX_TTS_PITCH,
            },
            "audio_setting": {
                "sample_rate": 44100,
                "bitrate": MINIMAX_TTS_BITRATE,
                "format": "mp3",
                "channel": 1,
            },
            "language_boost": "Chinese",
            "text_normalization": True,
            "subtitle_enable": True,
            "subtitle_type": "word",
            "output_format": "hex",
        },
    }
    request = urllib.request.Request(
        MINIMAX_TTS_ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    proxy_url = service_env.get("HTTPS_PROXY") or service_env.get("https_proxy")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        if proxy_url
        else urllib.request.ProxyHandler()
    )

    response_payload = None
    last_error = None
    transient_http_codes = {408, 429, 500, 502, 503, 504}
    for attempt in range(MINIMAX_TTS_RETRIES + 1):
        try:
            with opener.open(request, timeout=MINIMAX_TTS_TIMEOUT) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:300]
            last_error = RuntimeError(
                f"MiniMax 配音接口返回 HTTP {exc.code}: {detail}"
            )
            if exc.code not in transient_http_codes or attempt >= MINIMAX_TTS_RETRIES:
                raise last_error from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = RuntimeError(f"MiniMax 配音接口调用失败: {exc}")
            if attempt >= MINIMAX_TTS_RETRIES:
                raise last_error from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("MiniMax 配音接口返回的不是有效 JSON") from exc
        time.sleep(0.5 * (attempt + 1))
    if response_payload is None:
        raise last_error or RuntimeError("MiniMax 配音接口调用失败")

    output = response_payload.get("output") or {}
    base_response = output.get("base_resp") or {}
    try:
        status_code = int(base_response.get("status_code") or 0)
    except (TypeError, ValueError):
        status_code = -1
    if status_code != 0:
        status_message = base_response.get("status_msg") or "未知业务错误"
        raise RuntimeError(f"MiniMax 配音失败（{status_code}）：{status_message}")
    data = output.get("data") or {}
    audio_data = _decode_minimax_audio(data.get("audio"))
    if len(audio_data) < 512:
        raise RuntimeError("MiniMax 配音接口没有返回有效音频")
    with open(output_path, "wb") as handle:
        handle.write(audio_data)

    usage = response_payload.get("usage") or output.get("usage") or {}
    extra_info = output.get("extra_info") or data.get("extra_info") or {}
    usage_characters = usage.get("characters") or extra_info.get("usage_characters")
    try:
        usage_characters = int(usage_characters or len(text))
    except (TypeError, ValueError):
        usage_characters = len(text)
    actual_estimated_cost = (
        usage_characters * MINIMAX_TTS_PRICE_PER_10K_CNY / 10000.0
    )
    try:
        audio_duration = float(extra_info.get("audio_length")) / 1000.0
    except (TypeError, ValueError):
        audio_duration = get_duration(output_path)
    timeline_payload = None
    subtitle_url = data.get("subtitle_file")
    if isinstance(subtitle_url, str) and subtitle_url.startswith("https://"):
        for attempt in range(2):
            try:
                with opener.open(subtitle_url, timeout=min(60, MINIMAX_TTS_TIMEOUT)) as response:
                    subtitle_payload = json.loads(response.read().decode("utf-8"))
                timeline_payload = _minimax_word_timeline(
                    text,
                    subtitle_payload,
                    audio_duration,
                )
                break
            except (
                OSError,
                TimeoutError,
                urllib.error.URLError,
                UnicodeDecodeError,
                json.JSONDecodeError,
            ):
                if attempt == 0:
                    time.sleep(0.3)
    if preserve_sentences and not timeline_payload:
        raise RuntimeError(
            "MiniMax 未返回可用的词级时间戳；为避免音画错位，本次已中止"
        )
    if timeline_payload:
        with open(phrase_timeline_path, "w", encoding="utf-8") as handle:
            json.dump(timeline_payload, handle, ensure_ascii=False, indent=2)
    info = {
        "provider": "minimax-bailian",
        "model": MINIMAX_TTS_MODEL,
        "voice": MINIMAX_TTS_VOICE,
        "rate": f"{MINIMAX_TTS_SPEED:.2f}x",
        "usage_characters": usage_characters,
        "estimated_cost_cny": round(actual_estimated_cost, 4),
        "cached": False,
    }
    if os.path.isfile(phrase_timeline_path):
        info["timeline_path"] = phrase_timeline_path
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        shutil.copyfile(output_path, cache_path)
        with open(cache_metadata_path, "w", encoding="utf-8") as handle:
            json.dump(info, handle, ensure_ascii=False, indent=2)
        if os.path.isfile(phrase_timeline_path):
            shutil.copyfile(phrase_timeline_path, cache_timeline_path)
    except OSError:
        pass
    print(
        "   -> MiniMax 克隆音色 API："
        f"{usage_characters} 个计费字符，预计 {actual_estimated_cost:.3f} 元。"
    )
    return info


def _edge_generate_tts(output_path):
    edge_tts = shutil.which("edge-tts")
    if edge_tts:
        command = [edge_tts]
    else:
        python_exe = PY312 if os.path.exists(PY312) else sys.executable
        command = [python_exe, "-m", "edge_tts"]
    service_env = runtime_service_env()
    command.extend(
        [
            "--file",
            PROMO_COPY_FILE,
            "--voice",
            TTS_VOICE,
            "--rate",
            TTS_RATE,
            "--write-media",
            output_path,
        ]
    )
    proxy_url = service_env.get("HTTPS_PROXY") or service_env.get("https_proxy")
    if proxy_url:
        command.extend(["--proxy", proxy_url])
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=TTS_TIMEOUT,
            env=service_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"edge-tts 超过 {TTS_TIMEOUT} 秒未完成") from exc
    if result.returncode != 0 or not os.path.exists(output_path):
        detail = (result.stderr or result.stdout or "未知错误").strip()[-500:]
        raise RuntimeError(f"edge-tts 配音失败: {detail}")
    return {
        "provider": "edge-tts",
        "model": None,
        "voice": TTS_VOICE,
        "rate": TTS_RATE,
    }


def _compact_cosyvoice_phrases(copy_text, max_chars=COSYVOICE_CHUNK_CHARS):
    """Reduce per-sentence model calls while retaining natural phrase breaks."""
    text = (copy_text or "").strip()
    if not text:
        return "", 0
    sentences = [
        part.strip()
        for part in re.findall(r"[^。！？!?；;]+[。！？!?；;]?", text)
        if part.strip()
    ] or [text]
    groups = []
    current = []
    current_length = 0
    for sentence in sentences:
        plain = sentence.rstrip("。！？!?；;").strip()
        if not plain:
            continue
        separator_cost = 1 if current else 0
        if current and current_length + separator_cost + len(plain) > max_chars:
            groups.append("，".join(current) + "。")
            current = []
            current_length = 0
        current.append(plain)
        current_length += separator_cost + len(plain)
    if current:
        groups.append("，".join(current) + "。")
    return "".join(groups), len(groups)


def _cosyvoice_cache_path(copy_text, preserve_sentences=False):
    prompt_signature = COSYVOICE_PROMPT
    try:
        prompt_stat = os.stat(COSYVOICE_PROMPT)
        prompt_signature += f":{prompt_stat.st_size}:{prompt_stat.st_mtime_ns}"
    except OSError:
        pass
    try:
        synthesizer_stat = os.stat(COSYVOICE_SYNTHESIZER)
        prompt_signature += (
            f":synth:{synthesizer_stat.st_size}:{synthesizer_stat.st_mtime_ns}"
        )
    except OSError:
        pass
    cache_key = hashlib.sha256(
        (
            "cosyvoice-timed-v3\0"
            + copy_text
            + "\0"
            + prompt_signature
            + f"\0{COSYVOICE_SPEED:.3f}\0{COSYVOICE_CHUNK_CHARS}"
            + f"\0preserve={int(bool(preserve_sentences))}"
        ).encode("utf-8")
    ).hexdigest()
    cache_dir = os.environ.get(
        "V16_TTS_CACHE_DIR",
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            ".v16_tts_cache",
        ),
    )
    return os.path.join(cache_dir, f"{cache_key}.mp3")


def _cosyvoice_generate_tts(copy_text, output_path, preserve_sentences=False):
    """Reuse the installed Ranliang local voice-cloning runtime, without an API key."""
    required = (COSYVOICE_PYTHON, COSYVOICE_SYNTHESIZER, COSYVOICE_PROMPT)
    missing = [path for path in required if not os.path.isfile(path)]
    if missing:
        raise RuntimeError("燃量本机 CosyVoice 运行环境不完整")
    cache_path = _cosyvoice_cache_path(copy_text, preserve_sentences)
    cache_timeline_path = os.path.splitext(cache_path)[0] + ".json"
    phrase_timeline_path = os.path.join(TMP_DIR, "dedup2_tts_phrase_timeline.json")
    _remove_promo_artifact(phrase_timeline_path)
    if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 512:
        shutil.copyfile(cache_path, output_path)
        if os.path.isfile(cache_timeline_path):
            shutil.copyfile(cache_timeline_path, phrase_timeline_path)
        info = {
            "provider": "local-cosyvoice-cache",
            "model": "CosyVoice-300M",
            "voice": "燃量参考女声",
            "rate": f"{COSYVOICE_SPEED:.2f}x",
        }
        if os.path.isfile(phrase_timeline_path):
            info["timeline_path"] = phrase_timeline_path
        return info
    raw_audio = os.path.join(TMP_DIR, "dedup2_tts_cosyvoice_raw.wav")
    _remove_promo_artifact(raw_audio)
    if preserve_sentences:
        compact_text = copy_text.strip()
        phrase_count = len(_narration_sentences(compact_text))
    else:
        compact_text, phrase_count = _compact_cosyvoice_phrases(copy_text)
    command = [
        COSYVOICE_PYTHON,
        COSYVOICE_SYNTHESIZER,
        "--text",
        compact_text,
        "--prompt",
        COSYVOICE_PROMPT,
        "--output",
        raw_audio,
        "--timeline-output",
        phrase_timeline_path,
        "--speed",
        f"{COSYVOICE_SPEED:.2f}",
        "--chunk-chars",
        str(COSYVOICE_CHUNK_CHARS),
    ]
    cosy_env = runtime_service_env()
    for thread_variable in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        cosy_env[thread_variable] = str(COSYVOICE_THREADS)
    cache_paths = {
        "NUMBA_CACHE_DIR": os.path.join(TMP_DIR, "cosyvoice_numba_cache"),
        "MPLCONFIGDIR": os.path.join(TMP_DIR, "cosyvoice_mpl_cache"),
        "XDG_CACHE_HOME": os.path.join(TMP_DIR, "cosyvoice_xdg_cache"),
    }
    for key, runtime_cache_path in cache_paths.items():
        os.makedirs(runtime_cache_path, exist_ok=True)
        cosy_env.setdefault(key, runtime_cache_path)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=COSYVOICE_TIMEOUT,
            env=cosy_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"燃量 CosyVoice 超过 {COSYVOICE_TIMEOUT} 秒未完成") from exc
    if result.returncode != 0 or not os.path.isfile(raw_audio):
        detail = (result.stderr or result.stdout or "未知错误").strip()[-500:]
        raise RuntimeError(f"燃量 CosyVoice 配音失败: {detail}")
    try:
        run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-i",
                raw_audio,
                "-ac",
                "1",
                "-ar",
                "48000",
                "-af",
                "adeclick,highpass=f=70,lowpass=f=11000,"
                "deesser=i=0.12:m=0.35:f=0.55,"
                "acompressor=threshold=-14dB:ratio=1.55:attack=20:release=180:makeup=1,"
                "loudnorm=I=-17:TP=-2:LRA=8",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                output_path,
            ]
        )
    except RuntimeError as exc:
        raise RuntimeError("燃量参考女声自然化处理失败") from exc
    try:
        if os.path.getsize(output_path) > 512:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            shutil.copyfile(output_path, cache_path)
            if os.path.isfile(phrase_timeline_path):
                shutil.copyfile(phrase_timeline_path, cache_timeline_path)
    except OSError:
        pass
    print(f"   -> 燃量配音已合并为 {phrase_count} 个自然长句，减少重复模型推理。")
    info = {
        "provider": "local-cosyvoice",
        "model": "CosyVoice-300M",
        "voice": "燃量参考女声",
        "rate": f"{COSYVOICE_SPEED:.2f}x",
    }
    if os.path.isfile(phrase_timeline_path):
        info["timeline_path"] = phrase_timeline_path
    return info


def generate_tts_audio(copy_text, preserve_sentences=False):
    """Generate narration without silently replacing the selected voice."""
    generate_tts_audio.last_info = {}
    if not TTS_ENABLED:
        _remove_promo_artifact(PROMO_AUDIO_FILE)
        print("   -> 自动配音已按配置关闭，去重素材池2保持静音。")
        return None
    if not copy_text.strip():
        _remove_promo_artifact(PROMO_AUDIO_FILE)
        print("[WARN] 推广文案为空，未生成配音。")
        return None

    temp_audio = os.path.join(TMP_DIR, "dedup2_tts_raw.mp3")
    _remove_promo_artifact(temp_audio)
    reuse_audio = os.environ.get("V16_TTS_REUSE_AUDIO", "").strip()
    mock_audio = os.environ.get("V16_TTS_MOCK_AUDIO", "").strip()
    if reuse_audio:
        if not os.path.exists(reuse_audio):
            print("[WARN] V16_TTS_REUSE_AUDIO 指向的文件不存在。")
            return None
        shutil.copyfile(reuse_audio, temp_audio)
        info = {
            "provider": "local-cosyvoice-cache",
            "model": "CosyVoice-300M",
            "voice": "燃量参考女声",
            "rate": f"{COSYVOICE_SPEED:.2f}x",
        }
    elif mock_audio:
        if not os.path.exists(mock_audio):
            print("[WARN] V16_TTS_MOCK_AUDIO 指向的文件不存在。")
            return None
        shutil.copyfile(mock_audio, temp_audio)
        info = {"provider": "mock", "model": None, "voice": "mock", "rate": "mock"}
    else:
        provider_order = []
        local_providers = {"auto", "local", "cosyvoice", "ranliang"}
        minimax_providers = {
            "minimax",
            "minimax-bailian",
            "bailian",
            "dashscope",
            "api",
        }
        if TTS_PROVIDER in local_providers:
            provider_order.append("local-cosyvoice")
        if TTS_PROVIDER in minimax_providers:
            provider_order.append("minimax-bailian")
        if TTS_PROVIDER == "openai" or (
            TTS_PROVIDER == "auto" and not TTS_STRICT_VOICE
        ):
            provider_order.append("openai")
        if TTS_PROVIDER in {"edge", "edge-tts"} or (
            TTS_PROVIDER == "auto" and not TTS_STRICT_VOICE
        ):
            provider_order.append("edge-tts")
        if not provider_order:
            provider_order = ["local-cosyvoice"]
        errors = []
        info = None
        for provider in provider_order:
            _remove_promo_artifact(temp_audio)
            try:
                if provider == "local-cosyvoice":
                    if preserve_sentences:
                        info = _cosyvoice_generate_tts(
                            copy_text,
                            temp_audio,
                            preserve_sentences=True,
                        )
                    else:
                        info = _cosyvoice_generate_tts(copy_text, temp_audio)
                elif provider == "minimax-bailian":
                    info = _minimax_generate_tts(
                        copy_text,
                        temp_audio,
                        preserve_sentences=preserve_sentences,
                    )
                elif provider == "openai":
                    info = _openai_generate_tts(copy_text, temp_audio)
                else:
                    info = _edge_generate_tts(temp_audio)
                break
            except RuntimeError as exc:
                errors.append(str(exc))
        if info is None:
            strict_hint = (
                "；已禁止换成其他音色"
                if TTS_STRICT_VOICE
                and TTS_PROVIDER in (local_providers | minimax_providers)
                else ""
            )
            print(
                f"[WARN] 自动配音失败，去重素材池2保持静音: "
                f"{'；'.join(errors)}{strict_hint}"
            )
            return None

    if get_duration(temp_audio) <= 0:
        print("[WARN] 配音输出没有有效时长，去重素材池2保持静音。")
        return None
    shutil.copyfile(temp_audio, PROMO_AUDIO_FILE)
    generate_tts_audio.last_info = info
    print(
        f"   -> {info['provider']} 配音已生成，声音：{info['voice']}，"
        f"语速：{info['rate']}。"
    )
    return temp_audio


generate_tts_audio.last_info = {}


def _atempo_filter(factor):
    factor = max(0.5, factor)
    filters = []
    while factor > 2.0:
        filters.append("atempo=2.0")
        factor /= 2.0
    while factor < 0.5:
        filters.append("atempo=0.5")
        factor /= 0.5
    filters.append(f"atempo={factor:.6f}")
    return ",".join(filters)


def _load_phrase_timeline(timeline_path, copy_text, voice_duration):
    """Load exact local-TTS phrase times, or estimate safe sequential times."""
    sentences = _narration_sentences(copy_text)
    if timeline_path:
        try:
            with open(timeline_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            phrases = payload.get("phrases") if isinstance(payload, dict) else None
            if isinstance(phrases, list) and len(phrases) == len(sentences):
                parsed = []
                for phrase in phrases:
                    start = max(0.0, float(phrase.get("start", 0.0)))
                    end = max(start, float(phrase.get("end", start)))
                    if end <= start:
                        raise ValueError("empty phrase")
                    parsed.append({"start": start, "end": end, "duration": end - start})
                return parsed, "exact-tts-phrases"
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    weights = [
        max(1, len(re.sub(r"[\s，。！？!?；;、]", "", sentence)))
        for sentence in sentences
    ]
    total_weight = float(sum(weights))
    cursor = 0.0
    estimated = []
    for index, weight in enumerate(weights):
        end = (
            float(voice_duration)
            if index == len(weights) - 1
            else cursor + float(voice_duration) * weight / total_weight
        )
        estimated.append({"start": cursor, "end": end, "duration": end - cursor})
        cursor = end
    return estimated, "estimated-tts-phrases"


def _timed_voice_audio_filter(sentence_plan, phrase_timeline, input_label="1:a"):
    """Reset narration timing at every video window instead of drifting globally."""
    if not sentence_plan or len(sentence_plan) != len(phrase_timeline):
        raise RuntimeError("逐句配音时间轴与视频分镜数量不一致")
    filters = []
    labels = []
    for index, ((_sentence, target_duration), phrase) in enumerate(
        zip(sentence_plan, phrase_timeline)
    ):
        source_duration = max(0.05, float(phrase["duration"]))
        target_duration = max(1.0 / FPS, float(target_duration))
        tempo = max(0.82, min(2.0, source_duration / target_duration))
        label = f"voice_window_{index}"
        filters.append(
            f"[{input_label}]atrim=start={float(phrase['start']):.6f}:"
            f"end={float(phrase['end']):.6f},asetpts=PTS-STARTPTS,"
            f"{_atempo_filter(tempo)},aresample=44100,"
            f"apad=pad_dur={target_duration:.6f},"
            f"atrim=duration={target_duration:.6f}[{label}]"
        )
        labels.append(f"[{label}]")
    filters.append(
        f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[a_voice_timed]"
    )
    return ";".join(filters)


def _render_concat_list_cfr(concat_list, output_path, duration, input_paths=None):
    """Rebuild mixed time bases and H.264 headers as one monotonic CFR timeline."""
    input_paths = list(input_paths or [])
    if not input_paths:
        try:
            with open(concat_list, "r", encoding="utf-8") as handle:
                input_paths = [
                    match.group(1).replace("'\\''", "'")
                    for line in handle
                    if (match := re.match(r"^file '(.*)'\s*$", line.strip()))
                ]
        except OSError:
            input_paths = []
    if not input_paths:
        raise RuntimeError("拼接列表中没有可用视频。")

    command = ["ffmpeg", "-y"]
    filters = []
    concat_inputs = []
    for index, path in enumerate(input_paths):
        command.extend(["-i", path])
        filters.extend(
            [
                f"[{index}:v]setpts=PTS-STARTPTS,fps={FPS},setsar=1,"
                f"format=yuv420p[v{index}]",
                f"[{index}:a]aresample=44100:async=1:first_pts=0,"
                f"asetpts=PTS-STARTPTS[a{index}]",
            ]
        )
        concat_inputs.extend([f"[v{index}]", f"[a{index}]"])
    filters.append(
        f"{''.join(concat_inputs)}concat=n={len(input_paths)}:v=1:a=1[v_concat][a_concat]"
    )
    filters.extend(
        [
            f"[v_concat]trim=duration={float(duration):.3f},"
            f"setpts=PTS-STARTPTS,fps={FPS},setsar=1,format=yuv420p[v_exact]",
            f"[a_concat]aresample=44100,apad,atrim=duration={float(duration):.3f},"
            "asetpts=PTS-STARTPTS[a_exact]",
        ]
    )
    command.extend(
        [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[v_exact]",
        "-map",
        "[a_exact]",
        "-t",
        f"{max(1.0 / FPS, float(duration)):.3f}",
        *_video_encoder_args(23, "veryfast"),
        "-g",
        str(FPS),
        "-fps_mode",
        "cfr",
        "-video_track_timescale",
        "90000",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        output_path,
        ]
    )
    run_ffmpeg(command)


def _concat_clip_objects(clips, output_path, list_name):
    concat_list = os.path.join(TMP_DIR, list_name)
    with open(concat_list, "w", encoding="utf-8") as handle:
        for clip in clips:
            escaped = clip.path.replace("'", "'\\''")
            handle.write(f"file '{escaped}'\n")
    duration = sum(max(0.0, get_duration(clip.path)) for clip in clips)
    _render_concat_list_cfr(
        concat_list,
        output_path,
        duration,
        input_paths=[clip.path for clip in clips],
    )


def _fit_clips_to_duration(clips, max_duration, finish_last_clip=FINISH_LAST_CLIP):
    """Select to a soft budget and keep the final visible action complete."""
    remaining = max(0.0, float(max_duration or 0.0))
    selected = []
    for index, clip in enumerate(clips):
        if remaining <= 0.01:
            break
        duration = get_duration(clip.path) or max(0.0, float(clip.duration or 0.0))
        if duration <= 0:
            continue
        if duration <= remaining:
            selected.append(
                Clip(
                    clip.path,
                    clip.source,
                    duration,
                    clip.score,
                    clip.source_name,
                    clip.start,
                    clip.sequence,
                    clip.source_path,
                )
            )
            remaining -= duration
            continue

        if finish_last_clip:
            selected.append(
                Clip(
                    clip.path,
                    clip.source,
                    duration,
                    clip.score,
                    clip.source_name,
                    clip.start,
                    clip.sequence,
                    clip.source_path,
                )
            )
            break

        trimmed_path = os.path.join(TMP_DIR, f"dedup2_budget_trim_{index:03d}.mp4")
        normalize_video(clip.path, trimmed_path, mute=True, trim=(0.0, remaining))
        trimmed_duration = min(get_duration(trimmed_path) or remaining, remaining)
        if trimmed_duration > 0:
            selected.append(
                Clip(
                    trimmed_path,
                    clip.source,
                    trimmed_duration,
                    clip.score,
                    clip.source_name,
                    clip.start,
                    clip.sequence,
                    clip.source_path,
                )
            )
        break
    return selected


def _voice_section_target_duration(base_duration, voice_duration, max_section_duration):
    """Never invent visual time: narration cannot outlive the available footage."""
    available = max(0.0, float(base_duration or 0.0))
    requested = max(0.0, float(max_section_duration or 0.0))
    return max(0.5, min(available, requested))


def _voice_requires_regeneration(voice_duration, target_duration):
    """Regenerate only when narration is materially short; lightly stretch near misses."""
    target = max(0.5, float(target_duration or 0.0))
    return float(voice_duration or 0.0) < target * 0.86


def _narration_sentences(copy_text):
    sentences = [
        part.strip()
        for part in re.findall(r"[^。！？!?]+[。！？!?]?", copy_text or "")
        if part.strip()
    ]
    return sentences or ([copy_text.strip()] if (copy_text or "").strip() else ["画面继续展示商品细节。"])


def _sentence_duration_plan(copy_text, target_duration):
    """Build an exact sentence timeline used as shot boundaries."""
    target = max(0.5, float(target_duration))
    sentences = _narration_sentences(copy_text)
    weights = [max(1, len(re.sub(r"[\s，。！？!?；;、]", "", sentence))) for sentence in sentences]
    total_weight = float(sum(weights))
    total_frames = max(len(sentences), int(round(target * FPS)))
    raw_frames = [total_frames * weight / total_weight for weight in weights]
    frame_counts = [max(1, int(value)) for value in raw_frames]
    remainder = total_frames - sum(frame_counts)
    if remainder > 0:
        order = sorted(
            range(len(frame_counts)),
            key=lambda index: raw_frames[index] - int(raw_frames[index]),
            reverse=True,
        )
        for offset in range(remainder):
            frame_counts[order[offset % len(order)]] += 1
    elif remainder < 0:
        order = sorted(range(len(frame_counts)), key=lambda index: frame_counts[index], reverse=True)
        for _ in range(-remainder):
            for index in order:
                if frame_counts[index] > 1:
                    frame_counts[index] -= 1
                    break
    durations = [frames / FPS for frames in frame_counts]
    return list(zip(sentences, durations))


def _equal_window_sentence_plan(copy_text, target_duration):
    """Give every ordered video sample one fixed narration window."""
    target = max(0.5, float(target_duration))
    sentences = _narration_sentences(copy_text)
    total_frames = max(len(sentences), int(round(target * FPS)))
    base_frames, remainder = divmod(total_frames, len(sentences))
    durations = [
        (base_frames + (1 if index < remainder else 0)) / FPS
        for index in range(len(sentences))
    ]
    return list(zip(sentences, durations))


def _extract_alignment_frames(clips):
    frame_dir = os.path.join(TMP_DIR, "dedup2_alignment_frames")
    os.makedirs(frame_dir, exist_ok=True)
    frames = []
    for index, clip in enumerate(clips):
        duration = max(0.1, get_duration(clip.path) or float(clip.duration or 0.1))
        sample_time = max(0.05, min(duration * 0.5, max(0.05, duration - 0.05)))
        frame_path = os.path.join(frame_dir, f"shot_{index:03d}.jpg")
        try:
            run_ffmpeg(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    f"{sample_time:.3f}",
                    "-i",
                    clip.path,
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=640:-2",
                    "-q:v",
                    "3",
                    frame_path,
                ]
            )
        except RuntimeError:
            continue
        if os.path.exists(frame_path) and os.path.getsize(frame_path) > 0:
            frames.append((index, frame_path))
    return frames


def _parse_shot_alignment(raw_text, sentence_count, shot_count):
    cleaned = (raw_text or "").replace("```json", "").replace("```", "").strip()
    payload = None
    for start_char, end_char in (("{", "}"), ("[", "]")):
        start = cleaned.find(start_char)
        end = cleaned.rfind(end_char)
        if start < 0 or end <= start:
            continue
        try:
            payload = json.loads(cleaned[start : end + 1])
            break
        except json.JSONDecodeError:
            continue
    if isinstance(payload, dict):
        payload = payload.get("alignments") or payload.get("plan") or payload.get("items")
    if not isinstance(payload, list):
        raise RuntimeError("Codex 没有返回可解析的镜头绑定表")

    parsed = []
    seen_sentences = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            sentence_index = int(item.get("sentence_index"))
        except (TypeError, ValueError):
            continue
        if sentence_index < 0 or sentence_index >= sentence_count or sentence_index in seen_sentences:
            continue
        raw_indices = item.get("shot_indices") or item.get("shots") or []
        if not isinstance(raw_indices, list):
            raw_indices = [raw_indices]
        shot_indices = []
        for value in raw_indices:
            try:
                shot_index = int(value)
            except (TypeError, ValueError):
                continue
            if 0 <= shot_index < shot_count and shot_index not in shot_indices:
                shot_indices.append(shot_index)
        if not shot_indices:
            continue
        seen_sentences.add(sentence_index)
        parsed.append(
            {
                "sentence_index": sentence_index,
                "shot_indices": shot_indices,
                "reason": str(item.get("reason") or "").strip()[:200],
            }
        )
    if len(parsed) != sentence_count:
        raise RuntimeError(
            f"Codex 镜头绑定不完整：需要 {sentence_count} 句，实际 {len(parsed)} 句"
        )
    return sorted(parsed, key=lambda item: item["sentence_index"])


def _fallback_shot_alignment(sentence_count, shot_count):
    if sentence_count <= 0 or shot_count <= 0:
        return []
    return [
        {
            "sentence_index": sentence_index,
            "shot_indices": [
                min(shot_count - 1, int(sentence_index * shot_count / sentence_count))
            ],
            "reason": "顺序兜底",
        }
        for sentence_index in range(sentence_count)
    ]


def generate_shot_alignment(clips, copy_text):
    sentences = _narration_sentences(copy_text)
    fallback = _fallback_shot_alignment(len(sentences), len(clips))
    indexed_frames = _extract_alignment_frames(clips)
    if len(indexed_frames) != len(clips):
        return fallback, "sequential-fallback"
    sentence_lines = "\n".join(
        f"sentence_{index}: {sentence}" for index, sentence in enumerate(sentences)
    )
    prompt = (
        "你是短视频音画匹配剪辑师。附件图片与镜头一一对应，按上传顺序分别是 "
        + "、".join(f"shot_{index}" for index, _path in indexed_frames)
        + "。每张图包含该镜头中点画面，画面已有字幕也必须作为动作证据。\n"
        "请把下面每句口播绑定到真正能证明这句话的镜头，而不是按句子序号机械分配。"
        "安装、套管、按压、水流测试、闭合状态、成品状态必须匹配对应动作；"
        "禁止把仍在缠胶带的画面分给‘套住红色接头并按压固定’。"
        "每句话可以使用1到3个镜头，镜头可以复用。\n"
        f"口播：\n{sentence_lines}\n"
        "只输出JSON对象，不要Markdown："
        '{"alignments":[{"sentence_index":0,"shot_indices":[0,1],"reason":"画面依据"}]}。'
        "必须覆盖全部sentence_index，shot_indices只能使用以上现有编号。"
    )
    provider_name = (
        _selected_vision_provider() if _external_vision_is_selected() else "codex"
    )
    try:
        mock_alignment = os.environ.get("V16_ALIGNMENT_MOCK_RESPONSE", "").strip()
        if mock_alignment:
            raw_alignment, provider = mock_alignment, "mock"
        elif _external_vision_is_selected():
            raw_alignment, provider = _selected_external_vision_generate_copy(
                [path for _index, path in indexed_frames], prompt
            )
        elif (
            os.environ.get("V16_VISION_USE_CODEX", "1") != "0"
            and _vision_provider_enabled("codex")
        ):
            raw_alignment, provider = _codex_generate_copy(
                [path for _index, path in indexed_frames], prompt
            )
        else:
            return fallback, "sequential-fallback"
        alignment = _parse_shot_alignment(
            raw_alignment, len(sentences), len(clips)
        )
        return alignment, provider
    except Exception as exc:
        _remember_vision_provider_failure(provider_name, exc)
        if _external_api_keys_required():
            raise RuntimeError(
                f"外部模型镜头语义绑定失败，已停止且未使用本地顺序兜底：{exc}"
            ) from exc
        print(f"[WARN] 视觉模型镜头语义绑定失败，暂按顺序兜底: {exc}")
        return fallback, "sequential-fallback"


def _parse_useful_shot_selection(raw_text, shot_count):
    cleaned = (raw_text or "").replace("```json", "").replace("```", "").strip()
    payload = None
    for start_char, end_char in (("{", "}"), ("[", "]")):
        start = cleaned.find(start_char)
        end = cleaned.rfind(end_char)
        if start < 0 or end <= start:
            continue
        try:
            payload = json.loads(cleaned[start : end + 1])
            break
        except json.JSONDecodeError:
            continue
    if isinstance(payload, dict):
        raw_indices = payload.get("selected_indices")
        if raw_indices is None:
            raw_indices = payload.get("useful_shots") or payload.get("shots") or []
    else:
        raw_indices = payload
    if not isinstance(raw_indices, list):
        raise RuntimeError("镜头反分析没有返回可解析的选镜列表")
    selected = []
    for item in raw_indices:
        if isinstance(item, dict):
            if item.get("useful") is False or str(item.get("decision") or "").lower() in {"drop", "reject"}:
                continue
            value = item.get("shot_index", item.get("index"))
        else:
            value = item
        try:
            index = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= index < shot_count and index not in selected:
            selected.append(index)
    if not selected:
        raise RuntimeError("镜头反分析未选出任何可用镜头")
    return selected


def _run_useful_shot_analysis(frame_paths, prompt):
    mock_response = os.environ.get("V16_SHOT_ANALYSIS_MOCK_RESPONSE", "").strip()
    if mock_response:
        return mock_response, "mock"
    if _external_vision_is_selected():
        try:
            return _selected_external_vision_generate_copy(frame_paths, prompt)
        except RuntimeError as exc:
            provider_name = _selected_vision_provider()
            _remember_vision_provider_failure(provider_name, exc)
            raise RuntimeError(
                f"{provider_name.upper()} 镜头反分析失败，已停止且未切换本机模型：{exc}"
            ) from exc
    provider_errors = []
    if os.environ.get("V16_VISION_USE_CODEX", "1") != "0" and _vision_provider_enabled("codex"):
        try:
            return _codex_generate_copy(frame_paths, prompt)
        except RuntimeError as exc:
            _remember_vision_provider_failure("codex", exc)
            provider_errors.append(str(exc))
    elif not _vision_provider_enabled("codex"):
        provider_errors.append(_vision_provider_failure_detail("codex"))
    if os.environ.get("V16_VISION_USE_OPENAI", "1") != "0" and _vision_provider_enabled("openai"):
        try:
            return _openai_generate_copy(frame_paths, prompt)
        except RuntimeError as exc:
            _remember_vision_provider_failure("openai", exc)
            provider_errors.append(str(exc))
    elif not _vision_provider_enabled("openai"):
        provider_errors.append(_vision_provider_failure_detail("openai"))
    if os.environ.get("V16_VISION_USE_OPENCLAW", "1") != "0" and _vision_provider_enabled("openclaw"):
        try:
            return _openclaw_generate_copy(frame_paths, prompt)
        except RuntimeError as exc:
            _remember_vision_provider_failure("openclaw", exc)
            provider_errors.append(str(exc))
    elif not _vision_provider_enabled("openclaw"):
        provider_errors.append(_vision_provider_failure_detail("openclaw"))
    detail = "；".join(provider_errors[-3:]) or "没有启用可用的视觉模型"
    raise RuntimeError(detail)


def _local_useful_shot_fallback(clips):
    """Keep the strongest 75% when remote visual analysis is unavailable."""
    clips = list(clips)
    if len(clips) <= 1:
        return clips
    keep_count = max(1, (len(clips) * 3 + 3) // 4)
    ranked = sorted(
        enumerate(clips),
        key=lambda item: (float(item[1].score or 0.0), -item[0]),
        reverse=True,
    )
    kept_indices = {index for index, _clip in ranked[:keep_count]}
    return [clip for index, clip in enumerate(clips) if index in kept_indices]


def select_useful_dedup2_clips(clips, minimum_duration=0.0):
    """Use Ranliang-style midpoint-frame reverse analysis before narration."""
    usable = [clip for clip in clips if float(clip.duration or 0.0) >= 0.35]
    if not usable:
        return []
    selected = []
    providers = []
    # Fewer, fuller model requests preserve one-frame-per-shot analysis while
    # avoiding an extra round trip for common five-source material sets.
    batch_size = min(24, VISION_MAX_FRAMES)
    for batch_start in range(0, len(usable), batch_size):
        batch = usable[batch_start : batch_start + batch_size]
        indexed_frames = _extract_alignment_frames(batch)
        if len(indexed_frames) != len(batch):
            selected.extend(batch)
            continue
        prompt = (
            "你是燃量的电商短视频镜头反分析器。不要调用工具，不要访问网络。"
            "附件按顺序对应 shot_0 到 shot_"
            f"{len(batch) - 1}，每张都是一个真实分镜的中间帧。"
            "只选择适合后续重组并配音的有用镜头：商品主体清楚、真实操作、结构细节、"
            "使用演示、前后结果或场景证据可保留；纯黑/过曝/严重模糊、无关画面、过度重复、空镜或无法支持口播的镜头不选。"
            "不得因为画面里有价格文字就删掉镜头，价格由后续特效遮挡。"
            "只输出JSON：{\"selected_indices\":[0,2],\"shots\":[{\"shot_index\":0,\"useful\":true,\"reason\":\"真实操作\"}]}。"
            "selected_indices必须使用现有0起编号，且与shots中useful=true一致。"
        )
        try:
            raw_selection, provider = _run_useful_shot_analysis(
                [path for _index, path in indexed_frames], prompt
            )
            selected_indices = _parse_useful_shot_selection(raw_selection, len(batch))
            selected.extend(batch[index] for index in selected_indices)
            providers.append(provider)
        except Exception as exc:
            if _external_api_keys_required():
                raise RuntimeError(
                    f"外部模型镜头反分析失败，已停止且未使用本地筛选：{exc}"
                ) from exc
            fallback = _local_useful_shot_fallback(batch)
            selected.extend(fallback)
            providers.append("local-safe-fallback")
            print(
                "[WARN] 去重素材池2镜头反分析失败，"
                f"已按本地清晰度/动态评分保留 {len(fallback)}/{len(batch)} 段: {exc}"
            )
    if not selected:
        selected = usable
    minimum_duration = max(0.0, float(minimum_duration or 0.0))
    selected_ids = {id(clip) for clip in selected}
    selected_duration = sum(
        max(0.0, float(clip.duration or 0.0)) for clip in selected
    )
    if minimum_duration > 0.0 and selected_duration + 0.01 < minimum_duration:
        # The vision model is deliberately strict about repetitive shots, but a
        # strict selection must not make a 40+ second real source fail merely
        # because it retained fewer than 40 seconds. Backfill the strongest
        # rejected *real* shots; downstream staggering still performs the final
        # remix, and no frame freezing or looping is introduced here.
        rejected = sorted(
            (clip for clip in usable if id(clip) not in selected_ids),
            key=lambda clip: (
                float(clip.score or 0.0),
                max(0.0, float(clip.duration or 0.0)),
                -int(clip.sequence or 0),
            ),
            reverse=True,
        )
        supplemented = []
        for clip in rejected:
            selected.append(clip)
            supplemented.append(clip)
            selected_duration += max(0.0, float(clip.duration or 0.0))
            if selected_duration + 0.01 >= minimum_duration:
                break
        if supplemented:
            print(
                "   -> 去重素材池2模型优选时长不足，已从被筛镜头中按质量补回 "
                f"{len(supplemented)} 段真实画面；当前 {selected_duration:.1f}s，"
                "不循环、不停帧。"
            )
    print(
        f"   -> 去重素材池2镜头反分析完成："
        f"从 {len(clips)} 段中提取 {len(selected)} 段有用镜头"
        f"（{','.join(dict.fromkeys(providers)) or 'local'}）。"
    )
    return selected


def _loop_video_to_duration(input_path, output_path, target_duration):
    """Fit a shot with modest interpolation, but never freeze or replay it."""
    source_duration = max(1.0 / FPS, get_duration(input_path))
    if target_duration > source_duration * 1.25 + (2.0 / FPS):
        raise RuntimeError(
            "镜头时长不足，已拒绝用停帧或循环画面补时长："
            f"原镜头 {source_duration:.3f}s，要求 {target_duration:.3f}s。"
        )
    stretch = min(1.25, max(1.0, target_duration / source_duration))
    frame_rate_filter = f"fps={FPS}"
    if stretch > 1.001:
        frame_rate_filter = (
            f"minterpolate=fps={FPS}:mi_mode=mci:mc_mode=aobmc:"
            "me_mode=bidir:vsbmc=1"
        )
    video_filter = (
        f"setpts={stretch:.8f}*PTS,"
        f"{frame_rate_filter},"
        f"trim=duration={target_duration:.3f},"
        "format=yuv420p,setpts=PTS-STARTPTS"
    )
    run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-t",
            f"{target_duration:.3f}",
            "-an",
            "-vf",
            video_filter,
            *_video_encoder_args(23, "veryfast"),
            "-g",
            str(FPS),
            "-video_track_timescale",
            "90000",
            output_path,
        ]
    )


def _build_script_master_video(
    clips,
    copy_text,
    target_duration,
    output_path,
    shot_alignment=None,
):
    """Cut at sentence boundaries so narration timing becomes the video master."""
    plan = _sentence_duration_plan(copy_text, target_duration)
    segment_paths = []
    alignment_by_sentence = {
        int(item.get("sentence_index")): list(item.get("shot_indices") or [])
        for item in (shot_alignment or [])
        if isinstance(item, dict)
    }
    output_index = 0
    for index, (_sentence, segment_duration) in enumerate(plan):
        source_indices = alignment_by_sentence.get(index) or [
            min(len(clips) - 1, int(index * len(clips) / max(1, len(plan))))
        ]
        source_indices = [
            source_index
            for position, source_index in enumerate(source_indices)
            if 0 <= int(source_index) < len(clips)
            and int(source_index) not in [int(value) for value in source_indices[:position]]
        ]
        # A semantic match may name only one short shot. Add nearby unused
        # shots until their real motion can cover the sentence with at most a
        # gentle 1.25x stretch. This avoids both frozen tails and loop jumps.
        source_capacity = sum(
            max(1.0 / FPS, float(clips[source_index].duration or get_duration(clips[source_index].path)))
            * 1.25
            for source_index in source_indices
        )
        candidates = sorted(
            (candidate for candidate in range(len(clips)) if candidate not in source_indices),
            key=lambda candidate: (
                min(abs(candidate - existing) for existing in source_indices),
                candidate,
            ),
        )
        for candidate in candidates:
            if source_capacity + (2.0 / FPS) >= segment_duration:
                break
            source_indices.append(candidate)
            source_capacity += max(
                1.0 / FPS,
                float(clips[candidate].duration or get_duration(clips[candidate].path)),
            ) * 1.25
        if source_capacity + (2.0 / FPS) < segment_duration:
            raise RuntimeError(
                "可用动态镜头不足，拒绝用停帧拼接口播句："
                f"第 {index + 1} 句需要 {segment_duration:.3f}s。"
            )
        sentence_frames = max(1, int(round(segment_duration * FPS)))
        source_durations = [
            max(1.0 / FPS, float(clips[source_index].duration or get_duration(clips[source_index].path)))
            for source_index in source_indices
        ]
        total_source_duration = sum(source_durations)
        raw_frames = [
            sentence_frames * source_duration / total_source_duration
            for source_duration in source_durations
        ]
        frame_counts = [max(1, int(value)) for value in raw_frames]
        remainder = sentence_frames - sum(frame_counts)
        if remainder > 0:
            order = sorted(
                range(len(frame_counts)),
                key=lambda value: raw_frames[value] - int(raw_frames[value]),
                reverse=True,
            )
            for offset in range(remainder):
                frame_counts[order[offset % len(order)]] += 1
        elif remainder < 0:
            order = sorted(range(len(frame_counts)), key=lambda value: frame_counts[value], reverse=True)
            for _ in range(-remainder):
                for value in order:
                    if frame_counts[value] > 1:
                        frame_counts[value] -= 1
                        break
        for local_index, source_index in enumerate(source_indices):
            source_index = max(0, min(len(clips) - 1, int(source_index)))
            shot_frames = frame_counts[local_index]
            if shot_frames <= 0:
                continue
            source_path = clips[source_index].path
            segment_path = os.path.join(TMP_DIR, f"script_shot_{output_index:03d}.mp4")
            _loop_video_to_duration(source_path, segment_path, shot_frames / FPS)
            segment_paths.append(segment_path)
            output_index += 1
    concat_list = os.path.join(TMP_DIR, "script_master_concat.txt")
    with open(concat_list, "w", encoding="utf-8") as handle:
        for path in segment_paths:
            escaped = path.replace("'", "'\\''")
            handle.write(f"file '{escaped}'\n")
    run_ffmpeg(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list, "-c", "copy", output_path]
    )
    return plan


def build_dedup2_voice_section(
    clips,
    requirement_text,
    max_section_duration,
    sync_mode=SYNC_MODE,
):
    _clear_promo_artifacts()
    if not clips or max_section_duration <= 0.5:
        _write_promo_skip_status(
            "本次未生成：去重素材池2没有足够的可用时长。",
            "去重素材池2无素材或可用时长不足",
        )
        return None
    selected_clips = _fit_clips_to_duration(clips, max_section_duration)
    if not selected_clips:
        _write_promo_skip_status(
            "本次未生成：去重素材池2没有可读取的有效视频片段。",
            "去重素材池2片段无有效时长",
        )
        return None
    silent_section = os.path.join(TMP_DIR, "dedup2_section_silent.mp4")
    _concat_clip_objects(selected_clips, silent_section, "dedup2_section_concat.txt")
    base_duration = min(get_duration(silent_section), max_section_duration)
    if base_duration <= 0:
        _write_promo_skip_status(
            "本次未生成：去重素材池2拼接后没有有效时长。",
            "去重素材池2拼接结果无有效时长",
        )
        return None

    requested_duration = min(
        DEDUP_SECTION_MAX_SECONDS,
        max(0.5, float(max_section_duration or 0.0)),
    )
    if requested_duration > base_duration + (2.0 / FPS):
        stretch = requested_duration / max(base_duration, 1.0 / FPS)
        if stretch > 1.12:
            raise RuntimeError(
                "去重素材池2有效内容不足40秒，且需要超过12%的慢放才能补足；"
                f"当前仅 {base_duration:.1f}s。请增加真实素材，系统不会循环或停帧凑时长。"
            )
        target_duration = requested_duration
    else:
        target_duration = _voice_section_target_duration(
            base_duration, base_duration, requested_duration
        )
    video_for_voice = silent_section
    if (
        sync_mode == "video_to_script"
        and abs(base_duration - target_duration) > 0.05
    ):
        video_for_voice = os.path.join(TMP_DIR, "dedup2_video_master.mp4")
        _loop_video_to_duration(silent_section, video_for_voice, target_duration)
    copy_text, metadata = write_promo_copy(
        selected_clips,
        requirement_text,
        target_duration,
        sync_mode=sync_mode,
        vision_video_path=(
            video_for_voice if sync_mode == "video_to_script" else None
        ),
    )

    sentence_plan = []
    shot_alignment = []
    alignment_provider = "none"
    if sync_mode == "script_to_video":
        shot_alignment, alignment_provider = generate_shot_alignment(
            selected_clips, copy_text
        )
        video_for_voice = os.path.join(TMP_DIR, "dedup2_script_master.mp4")
        sentence_plan = _build_script_master_video(
            selected_clips,
            copy_text,
            target_duration,
            video_for_voice,
            shot_alignment,
        )
        print(
            f"   -> 已按 {len(sentence_plan)} 句口播和 Codex 镜头绑定表重切画面"
            f"（{alignment_provider}）。"
        )
    else:
        # In video-master mode the source timeline remains untouched. The
        # model writes one equally timed sentence per sampled visual window.
        sentence_plan = _equal_window_sentence_plan(copy_text, target_duration)

    voice_path = generate_tts_audio(
        copy_text,
        preserve_sentences=(sync_mode == "video_to_script"),
    )
    if not voice_path:
        metadata.update(
            {
                "tts_generated": False,
                "final_section_duration": round(target_duration, 3),
                "sentence_timeline": [
                    {"text": sentence, "duration": round(duration, 3)}
                    for sentence, duration in sentence_plan
                ],
                "shot_alignment": shot_alignment,
                "alignment_provider": alignment_provider,
            }
        )
        _attach_cost_metadata(metadata)
        with open(PROMO_META_FILE, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False, indent=2)
        if TTS_ENABLED and TTS_STRICT_VOICE and TTS_PROVIDER in {
            "auto",
            "local",
            "cosyvoice",
            "ranliang",
            "minimax",
            "minimax-bailian",
            "bailian",
            "dashscope",
            "api",
        }:
            raise RuntimeError(
                "指定的克隆音色合成失败，已中止制作；"
                "为避免音色用错，系统没有换成晓晓或其他声音。"
            )
        silent_captioned = os.path.join(TMP_DIR, "dedup2_section_captioned_silent.mp4")
        return burn_dedup2_captions(
            video_for_voice,
            sentence_plan,
            silent_captioned,
        )

    voice_duration = get_duration(voice_path)
    narration_attempts = 1
    # Measure the synthesized file rather than guessing from character count.
    # If it does not cover the footage, ask the visual model for a precisely
    # longer script and synthesize again. This closes the old 60s-video/30s-
    # narration gap while keeping the copy tied to the same product frames.
    while (
        _voice_requires_regeneration(voice_duration, target_duration)
        and narration_attempts < NARRATION_MAX_ATTEMPTS
        and len(copy_text) < 2000
    ):
        ratio_guard = 1.04
        required_characters = max(
            5,
            min(
                2000,
                int(
                    round(
                        len(copy_text)
                        * target_duration
                        / max(voice_duration, 1.0)
                        * ratio_guard
                    )
                ),
            ),
        )
        if abs(required_characters - len(copy_text)) < 12:
            break
        print(
            "   -> 配音未铺满画面，按实测音频补写："
            f"{len(copy_text)}字/{voice_duration:.1f}s → 目标{required_characters}字/{target_duration:.1f}s。"
        )
        copy_text, metadata = write_promo_copy(
            selected_clips,
            requirement_text,
            target_duration,
            target_char_count=required_characters,
            sync_mode=sync_mode,
            vision_video_path=(
                video_for_voice if sync_mode == "video_to_script" else None
            ),
        )
        if sync_mode == "script_to_video":
            shot_alignment, alignment_provider = generate_shot_alignment(
                selected_clips, copy_text
            )
            sentence_plan = _build_script_master_video(
                selected_clips,
                copy_text,
                target_duration,
                video_for_voice,
                shot_alignment,
            )
        else:
            sentence_plan = _equal_window_sentence_plan(copy_text, target_duration)
        voice_path = generate_tts_audio(
            copy_text,
            preserve_sentences=(sync_mode == "video_to_script"),
        )
        if not voice_path:
            break
        voice_duration = get_duration(voice_path)
        narration_attempts += 1

    if not voice_path:
        metadata.update(
            {
                "tts_generated": False,
                "narration_attempts": narration_attempts,
                "final_section_duration": round(base_duration, 3),
            }
        )
        _attach_cost_metadata(metadata)
        with open(PROMO_META_FILE, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False, indent=2)
        if TTS_ENABLED and TTS_STRICT_VOICE and TTS_PROVIDER in {
            "auto",
            "local",
            "cosyvoice",
            "ranliang",
            "minimax",
            "minimax-bailian",
            "bailian",
            "dashscope",
            "api",
        }:
            raise RuntimeError(
                "指定的克隆音色重试失败，已中止制作；"
                "为避免音色用错，系统没有产出静音或其他声音的成片。"
            )
        silent_captioned = os.path.join(TMP_DIR, "dedup2_section_captioned_silent.mp4")
        return burn_dedup2_captions(
            video_for_voice,
            sentence_plan,
            silent_captioned,
        )

    tempo = voice_duration / target_duration
    if tempo < 1.0:
        # A small slowdown is much faster and more natural than running the
        # local voice model a second time for a two- or three-second shortfall.
        tempo = max(0.86, tempo)
    elif tempo > 1.6:
        print("[WARN] 配音明显长于可用画面，将以 1.6 倍速处理并截去超出总时长的尾部。")
        tempo = 1.6

    voiced_section = os.path.join(TMP_DIR, "dedup2_section_voiced.mp4")
    fade_start = max(0.0, target_duration - 0.25)
    tts_info = dict(getattr(generate_tts_audio, "last_info", {}) or {})
    phrase_alignment_source = "global-audio-fit"
    if sync_mode == "video_to_script":
        phrase_timeline, phrase_alignment_source = _load_phrase_timeline(
            tts_info.get("timeline_path"),
            copy_text,
            voice_duration,
        )
        audio_filter = (
            _timed_voice_audio_filter(sentence_plan, phrase_timeline)
            + ";[a_voice_timed]loudnorm=I=-16:TP=-1.5:LRA=7,aresample=44100,"
            + f"afade=t=out:st={fade_start:.3f}:d=0.25[a_voice]"
        )
    else:
        audio_filter = (
            f"[1:a]{_atempo_filter(tempo)},"
            "loudnorm=I=-16:TP=-1.5:LRA=7,aresample=44100,"
            f"apad=pad_dur={target_duration:.3f},atrim=duration={target_duration:.3f},"
            f"afade=t=out:st={fade_start:.3f}:d=0.25[a_voice]"
        )
    video_master_duration = get_duration(video_for_voice)
    if video_master_duration + (2.0 / FPS) < target_duration:
        raise RuntimeError(
            "去重素材2配音画面不足，拒绝用停帧或循环凑时长："
            f"画面 {video_master_duration:.3f}s / 目标 {target_duration:.3f}s。"
        )
    audio_filter += (
        f";[0:v]trim=duration={target_duration:.3f},setpts=PTS-STARTPTS,"
        f"fps={FPS},format=yuv420p[v_voice_exact]"
    )
    run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            video_for_voice,
            "-i",
            voice_path,
            "-filter_complex",
            audio_filter,
            "-map",
            "[v_voice_exact]",
            "-map",
            "[a_voice]",
            "-t",
            f"{target_duration:.3f}",
            *_video_encoder_args(23, "veryfast"),
            "-fps_mode",
            "cfr",
            "-video_track_timescale",
            "90000",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            voiced_section,
        ]
    )
    if os.path.exists(voiced_section):
        rendered_video_duration = get_stream_duration(voiced_section, "v:0")
        rendered_audio_duration = get_stream_duration(voiced_section, "a:0")
        tolerance = max(0.12, 2.0 / FPS)
        if (
            rendered_video_duration <= 0
            or rendered_audio_duration <= 0
            or abs(rendered_video_duration - target_duration) > tolerance
            or abs(rendered_audio_duration - target_duration) > tolerance
            or abs(rendered_video_duration - rendered_audio_duration) > tolerance
        ):
            raise RuntimeError(
                "去重素材2配音音画桥接校验失败："
                f"视频 {rendered_video_duration:.3f}s / "
                f"音频 {rendered_audio_duration:.3f}s / "
                f"目标 {target_duration:.3f}s。"
            )
    captioned_section = os.path.join(TMP_DIR, "dedup2_section_captioned.mp4")
    captioned_section = burn_dedup2_captions(
        voiced_section,
        sentence_plan,
        captioned_section,
    )
    captioned_duration = get_duration(captioned_section)
    if abs(captioned_duration - target_duration) > max(0.12, 2.0 / FPS):
        raise RuntimeError(
            "去重素材池2字幕成片时长校验失败："
            f"输出 {captioned_duration:.3f}s / 目标 {target_duration:.3f}s。"
        )
    metadata.update(
        {
            "tts_generated": True,
            "tts_provider": tts_info.get("provider"),
            "tts_model": tts_info.get("model"),
            "tts_voice": tts_info.get("voice"),
            "tts_rate": tts_info.get("rate"),
            "tts_usage_characters": tts_info.get("usage_characters"),
            "tts_estimated_cost_cny": tts_info.get("estimated_cost_cny"),
            "tts_cache_hit": bool(tts_info.get("cached")),
            "narration_attempts": narration_attempts,
            "raw_voice_duration": round(voice_duration, 3),
            "duration_coverage_ratio": round(
                min(1.0, voice_duration / max(target_duration, 0.001)), 3
            ),
            "tempo_factor": round(tempo, 3),
            "source_section_duration": round(base_duration, 3),
            "final_section_duration": round(target_duration, 3),
            "video_extended": target_duration > base_duration + 0.05,
            "sentence_timeline": [
                {"text": sentence, "duration": round(duration, 3)}
                for sentence, duration in sentence_plan
            ],
            "shot_alignment": shot_alignment,
            "alignment_provider": alignment_provider,
            "voice_alignment": (
                "per-video-window" if sync_mode == "video_to_script" else "global-fit"
            ),
            "voice_alignment_timing": phrase_alignment_source,
            "generated_captions": True,
            "caption_render_order": "after-text-cover",
        }
    )
    _attach_cost_metadata(metadata, tts_info)
    with open(PROMO_META_FILE, "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
    print(
        "   -> 本条 API 预计总费用："
        f"视觉 {metadata['vision_estimated_cost_cny']:.4f} 元 + "
        f"配音 {float(metadata.get('tts_estimated_cost_cny') or 0.0):.4f} 元 = "
        f"{metadata['total_estimated_cost_cny']:.4f} 元。"
    )
    print(
        "   -> 配音已铺到去重素材池2："
        f"原画面 {base_duration:.1f}s，原配音 {voice_duration:.1f}s，"
        f"适配后 {target_duration:.1f}s，"
        + (
            f"逐镜头重置 {len(sentence_plan)} 次配音时间轴。"
            if sync_mode == "video_to_script"
            else f"语速倍率 {tempo:.2f}。"
        )
    )
    return captioned_section


def _natural_cut_duration(
    path,
    desired_duration,
    source_duration=None,
    max_extension=MAX_NATURAL_TAIL_SECONDS,
):
    """Choose the first pause or scene boundary just after the soft target."""
    source_duration = float(source_duration or get_duration(path) or 0.0)
    desired = max(0.5, min(float(desired_duration), source_duration))
    upper = min(source_duration, desired + max(0.0, float(max_extension)))
    if source_duration <= upper + 0.01:
        return source_duration

    analysis_start = max(0.0, desired - 0.25)
    analysis_duration = max(0.05, upper - analysis_start)
    pause_candidates = [
        point
        for point in extract_speech_pause_points(
            path,
            start_seconds=analysis_start,
            duration_seconds=analysis_duration,
        )
        if desired <= point <= upper
    ]
    if pause_candidates:
        return min(pause_candidates)
    scene_candidates = [
        point
        for point in extract_scenes(
            path,
            start_seconds=analysis_start,
            duration_seconds=analysis_duration,
        )
        if desired <= point <= upper
    ]
    if scene_candidates:
        return min(scene_candidates)
    return upper


def _natural_points_in_window(path, lower, upper):
    lower = max(0.0, float(lower or 0.0))
    upper = max(lower, float(upper or lower))
    duration = max(0.05, upper - lower)
    pauses = extract_speech_pause_points(
        path,
        start_seconds=lower,
        duration_seconds=duration,
    )
    scenes = extract_scenes(
        path,
        start_seconds=lower,
        duration_seconds=duration,
    )
    return sorted(
        {
            round(float(point), 6)
            for point in [*pauses, *scenes]
            if lower <= float(point) <= upper
        }
    )


def _natural_source_window(
    path,
    desired_duration,
    source_duration,
    *,
    window_index=0,
    max_extension=MAX_NATURAL_TAIL_SECONDS,
    require_natural_boundary=False,
    finish_before_budget=False,
    source_end_grace=0.0,
    spread_windows=False,
):
    """Select a deterministic source window and one natural end boundary."""
    source_duration = max(0.0, float(source_duration or 0.0))
    desired = max(0.05, min(float(desired_duration), source_duration))
    extension = max(0.0, float(max_extension or 0.0))
    source_end_grace = max(0.0, float(source_end_grace or 0.0))
    latest_start = max(0.0, source_duration - desired)
    window_index = max(0, int(window_index or 0))

    if latest_start <= 0.01 or window_index == 0:
        start = 0.0
        start_kind = "source-start"
    else:
        stride = max(8.0, desired * 0.65)
        # Keep 24 non-zero exploration slots: five outputs can each consume up
        # to four candidates, and stable per-source rank offsets add at most
        # four more ordinals. Positive ordinals never wrap to the 0-second slot.
        slot_count = max(25, int(math.ceil(latest_start / stride)) + 1)
        exploration_slots = slot_count - 1
        step = 1
        if spread_windows:
            # Spread adjacent candidates over the source instead of repeatedly
            # snapping nearby nominal starts to the same speech/scene boundary.
            # A coprime step visits every existing non-zero slot exactly once.
            step = max(1, round(exploration_slots * 0.61803398875))
            while math.gcd(step, exploration_slots) != 1:
                step += 1
        slot = 1 + (((window_index - 1) * step) % exploration_slots)
        nominal_start = latest_start * slot / max(1, slot_count - 1)
        radius = min(24.0, max(6.0, desired * 0.25))
        lower = max(0.0, nominal_start - radius)
        upper = min(latest_start, nominal_start + radius)
        start_candidates = _natural_points_in_window(path, lower, upper)
        if start_candidates:
            start = min(
                start_candidates,
                key=lambda point: (abs(point - nominal_start), point),
            )
            start_kind = "natural-start"
        else:
            if require_natural_boundary:
                # When the source is only slightly longer than the requested
                # window, the real file boundaries are safer than inventing a
                # non-zero cut.  Positive fission window ordinals previously
                # failed here before the existing source-end branch could run,
                # even though the whole file still fit inside the allowed
                # natural tail.  Keep strict before-budget paths unchanged.
                if (
                    not finish_before_budget
                    and source_duration
                    <= desired + extension + source_end_grace + 0.01
                ):
                    print(
                        "[NaturalBoundaryFallback] 未找到可用的非零自然起点；"
                        "已保留完整源文件，使用文件开头和文件结尾两个真实边界。"
                    )
                    return 0.0, source_duration, "source-start+source-end"
                raise NaturalBoundaryError(
                    f"源文件 {lower:.1f}–{upper:.1f} 秒内没有可用的自然起点。"
                )
            start = nominal_start
            start_kind = "deterministic-start-fallback"

    desired_end = min(source_duration, start + desired)
    if source_duration <= desired_end + 0.01:
        return start, source_duration, f"{start_kind}+source-end"

    if finish_before_budget:
        lower = max(start + 0.05, desired_end - max(extension, 1.0))
        upper = desired_end
        end_candidates = _natural_points_in_window(path, lower, upper)
        if end_candidates:
            return start, max(end_candidates), f"{start_kind}+natural-end-before"
        # A precise cap is the last safety net for sources with no detectable
        # speech pause or scene cut (for example, a synthetic single-shot clip).
        return start, desired_end, f"{start_kind}+frame-boundary-fallback"

    upper = min(source_duration, desired_end + extension)
    end_candidates = _natural_points_in_window(path, desired_end, upper)
    if end_candidates:
        return start, min(end_candidates), f"{start_kind}+natural-end-after"
    if source_duration <= upper + source_end_grace + 0.01:
        return start, source_duration, f"{start_kind}+source-end"
    if require_natural_boundary:
        raise NaturalBoundaryError(
            f"源窗口 {start:.1f}–{upper:.1f} 秒内没有自然语音停顿或镜头边界。"
        )
    return start, upper, f"{start_kind}+frame-boundary-fallback"


def _fit_paths_to_duration(
    paths,
    target_duration,
    prefix="timeline",
    repeat=False,
    finish_last_clip=FINISH_LAST_CLIP,
    require_natural_boundary=False,
    window_index=0,
    max_extension=MAX_NATURAL_TAIL_SECONDS,
    finish_before_budget=False,
    audit_role="timeline",
):
    """Select to a soft budget and finish the last included clip naturally."""
    target = max(0.0, float(target_duration or 0.0))
    valid = [(path, get_duration(path)) for path in paths]
    valid = [(path, duration) for path, duration in valid if duration > 0.01]
    if not valid or target <= 0.01:
        return []
    fitted = []
    total = 0.0
    index = 0
    while total < target - 0.01:
        if index >= len(valid) and not repeat:
            break
        path, duration = valid[index % len(valid)]
        remaining = target - total
        if duration <= remaining + 0.01:
            fitted.append(path)
            total += duration
            SOURCE_WINDOW_AUDIT.append(
                {
                    "role": str(audit_role),
                    "source": os.path.basename(path),
                    "start_seconds": 0.0,
                    "end_seconds": round(duration, 3),
                    "duration_seconds": round(duration, 3),
                    "window_index": int(window_index or 0),
                    "boundary": "source-end",
                }
            )
        elif finish_last_clip:
            overrun = duration - remaining
            if (
                not finish_before_budget
                and int(window_index or 0) == 0
                and overrun <= max_extension + 0.01
            ):
                fitted.append(path)
                total += duration
                SOURCE_WINDOW_AUDIT.append(
                    {
                        "role": str(audit_role),
                        "source": os.path.basename(path),
                        "start_seconds": 0.0,
                        "end_seconds": round(duration, 3),
                        "duration_seconds": round(duration, 3),
                        "window_index": int(window_index or 0),
                        "boundary": "source-end",
                    }
                )
            else:
                start, end, boundary = _natural_source_window(
                    path,
                    remaining,
                    duration,
                    window_index=window_index,
                    max_extension=max_extension,
                    require_natural_boundary=require_natural_boundary,
                    finish_before_budget=finish_before_budget,
                )
                natural_duration = max(0.0, end - start)
                if natural_duration <= 0.01:
                    index += 1
                    continue
                trimmed = os.path.join(
                    TMP_DIR,
                    f"{prefix}_natural_{index:03d}_w{int(window_index or 0):03d}.mp4",
                )
                normalize_video(
                    path,
                    trimmed,
                    mute=False,
                    trim=(start, natural_duration),
                )
                _DERIVED_SHUFFLE_SOURCE_MAP[os.path.abspath(trimmed)] = (
                    _DERIVED_SHUFFLE_SOURCE_MAP.get(
                        os.path.abspath(path), os.path.basename(path)
                    )
                )
                fitted.append(trimmed)
                total += natural_duration
                SOURCE_WINDOW_AUDIT.append(
                    {
                        "role": str(audit_role),
                        "source": os.path.basename(path),
                        "start_seconds": round(start, 3),
                        "end_seconds": round(end, 3),
                        "duration_seconds": round(natural_duration, 3),
                        "window_index": int(window_index or 0),
                        "boundary": boundary,
                    }
                )
        else:
            trimmed = os.path.join(TMP_DIR, f"{prefix}_trim_{index:03d}.mp4")
            normalize_video(path, trimmed, mute=False, trim=(0.0, remaining))
            _DERIVED_SHUFFLE_SOURCE_MAP[os.path.abspath(trimmed)] = (
                _DERIVED_SHUFFLE_SOURCE_MAP.get(
                    os.path.abspath(path), os.path.basename(path)
                )
            )
            fitted.append(trimmed)
            total = target
        index += 1
    return fitted


def _complete_shuffle_candidate(
    path,
    source_index,
    start,
    end,
    boundary,
):
    """Describe one source window whose two ends are known-safe boundaries."""
    start = max(0.0, float(start or 0.0))
    end = max(start, float(end or start))
    return {
        "path": path,
        "source_index": int(source_index),
        "start": start,
        "end": end,
        "duration": max(0.0, end - start),
        "boundary": str(boundary),
    }


def _complete_narrative_points_in_window(
    path,
    lower,
    upper,
    source_has_audio=None,
):
    """Return only boundaries that cannot cut spoken audio or visible action."""
    lower = max(0.0, float(lower or 0.0))
    upper = max(lower, float(upper or lower))
    duration = max(0.05, upper - lower)
    if source_has_audio is None:
        source_has_audio = has_audio_stream(path)
    if source_has_audio:
        # A visual scene change can occur under one continuous sentence. For a
        # voiced source, only a real audio pause is safe for this final fallback.
        points = extract_speech_pause_points(
            path,
            start_seconds=lower,
            duration_seconds=duration,
        )
    else:
        points = extract_scenes(
            path,
            start_seconds=lower,
            duration_seconds=duration,
        )
    return sorted(
        {
            round(float(point), 6)
            for point in points
            if lower <= float(point) <= upper
        }
    )


def _complete_shuffle_candidate_sets(
    valid,
    target_duration,
    max_extension,
    *,
    include_natural_points=False,
):
    """Return one bounded set of safe windows for each source.

    Full files are always candidates. The optional second pass probes only
    bounded neighborhoods around useful share sizes; it never analyzes an
    entire 6–8 minute source merely to find a fallback. Partial candidates use
    a real file boundary at one end and a detected pause/scene boundary at the
    other, so no arbitrary frame cut is introduced.
    """
    target = max(0.0, float(target_duration or 0.0))
    extension = max(0.0, float(max_extension or 0.0))
    source_count = max(1, len(valid))
    candidate_sets = []
    for source_index, (path, duration) in enumerate(valid):
        duration = max(0.0, float(duration or 0.0))
        full = _complete_shuffle_candidate(
            path,
            source_index,
            0.0,
            duration,
            "source-start+source-end",
        )
        candidates = [full]
        if include_natural_points and duration > 1.0:
            source_has_audio = has_audio_stream(path)
            # Plans using k sources need windows near target/k. Include the
            # relaxed upper edge as an additional anchor, but keep every probe
            # local and merge overlapping neighborhoods before analysis.
            anchors = {
                min(duration - 0.5, max(0.5, target / divisor))
                for divisor in range(1, min(source_count, 5) + 1)
                if target > 0.0
            }
            if target + extension > 0.5:
                anchors.add(
                    min(duration - 0.5, max(0.5, target + extension))
                )
            intervals = []
            for desired_length in sorted(anchors):
                radius = min(12.0, max(3.0, desired_length * 0.12))
                for center in (desired_length, duration - desired_length):
                    lower = max(0.05, center - radius)
                    upper = min(duration - 0.05, center + radius)
                    if upper > lower + 0.05:
                        intervals.append((lower, upper))
            merged = []
            for lower, upper in sorted(intervals):
                if merged and lower <= merged[-1][1] + 0.05:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], upper))
                else:
                    merged.append((lower, upper))
            points = set()
            for lower, upper in merged:
                points.update(
                    _complete_narrative_points_in_window(
                        path,
                        lower,
                        upper,
                        source_has_audio,
                    )
                )
            partials = []
            for point in sorted(points):
                point = float(point)
                if not 0.5 <= point <= duration - 0.5:
                    continue
                partials.extend(
                    [
                        _complete_shuffle_candidate(
                            path,
                            source_index,
                            0.0,
                            point,
                            "source-start+natural-end-after",
                        ),
                        _complete_shuffle_candidate(
                            path,
                            source_index,
                            point,
                            duration,
                            "natural-start+source-end",
                        ),
                    ]
                )

            # Retain the closest prefix and suffix for each useful share. This
            # bounds the later beam without throwing away a source or a safe
            # edge that could complete the target in combination with others.
            chosen = {}
            for anchor in sorted(anchors):
                for boundary in (
                    "source-start+natural-end-after",
                    "natural-start+source-end",
                ):
                    matching = [
                        item for item in partials if item["boundary"] == boundary
                    ]
                    if not matching:
                        continue
                    item = min(
                        matching,
                        key=lambda value: (
                            abs(value["duration"] - anchor),
                            value["start"],
                            value["end"],
                        ),
                    )
                    identity = (
                        int(round(item["start"] * FPS)),
                        int(round(item["end"] * FPS)),
                    )
                    chosen[identity] = item
            candidates.extend(
                sorted(
                    chosen.values(),
                    key=lambda item: (
                        item["duration"],
                        item["start"],
                        item["end"],
                    ),
                )
            )
        candidate_sets.append(candidates)
    return candidate_sets


def _select_complete_shuffle_plan(
    candidate_sets,
    target_duration,
    max_extension,
    *,
    allow_retime=False,
    transition=0.10,
    max_states=4096,
):
    """Select ordered complete windows near a soft target.

    Each source contributes at most one contiguous safe window. The beam may
    skip an oversized source, but preserves the relative order of every source
    it keeps. When requested, one uniform bounded playback rate is applied to
    every selected window; this keeps speech, action and audio synchronized.
    """
    target = max(1.0 / FPS, float(target_duration or 0.0))
    extension = max(0.0, min(36.0, float(max_extension or 0.0)))
    transition = max(0.0, float(transition or 0.0))
    lower = target - 0.15
    upper = target + extension + 0.5 / max(1, int(FPS))
    max_states = max(32, min(8192, int(max_states or 4096)))
    minimum_rate = NATURAL_BOUNDARY_MIN_PLAYBACK_RATE if allow_retime else 1.0
    maximum_rate = NATURAL_BOUNDARY_MAX_PLAYBACK_RATE if allow_retime else 1.0

    # Each state is (sum of pre-transition source durations, chosen windows).
    states = [(0.0, ())]

    def state_values(state):
        raw, chosen = state
        transition_total = transition * max(0, len(chosen) - 1)
        ideal_rate = raw / max(target + transition_total, 1.0 / FPS)
        rate = min(maximum_rate, max(minimum_rate, ideal_rate))
        if not allow_retime:
            rate = 1.0
        effective = raw / rate - transition_total
        return effective, rate

    def choice_identity(chosen):
        return tuple(
            (
                item["source_index"],
                int(round(item["start"] * FPS)),
                int(round(item["end"] * FPS)),
            )
            for item in chosen
        )

    def state_rank(state):
        effective, rate = state_values(state)
        _raw, chosen = state
        starts_with_first = 0 if chosen and chosen[0]["source_index"] == 0 else 1
        return (
            abs(effective - target),
            abs(rate - 1.0),
            -len(chosen),
            starts_with_first,
            choice_identity(chosen),
        )

    for source_candidates in candidate_sets:
        candidates = list(states)
        for raw, chosen in states:
            for item in source_candidates:
                if item["duration"] <= transition + 0.05:
                    continue
                candidate = (raw + item["duration"], (*chosen, item))
                effective_fastest = (
                    candidate[0] / maximum_rate
                    - transition * max(0, len(candidate[1]) - 1)
                )
                # Added windows have positive net duration, so a state that is
                # already too long at the fastest allowed rate can never recover.
                if effective_fastest <= upper + 0.5 / max(1, int(FPS)):
                    candidates.append(candidate)

        unique = {}
        for state in candidates:
            raw, chosen = state
            identity = (
                int(round(raw * max(1, int(FPS)))),
                len(chosen),
                bool(chosen) and chosen[0]["source_index"] == 0,
            )
            previous = unique.get(identity)
            if previous is None or state_rank(state) < state_rank(previous):
                unique[identity] = state
        below = []
        reached = []
        for state in unique.values():
            effective, _rate = state_values(state)
            (below if effective < lower else reached).append(state)
        below.sort(key=state_rank)
        reached.sort(key=state_rank)
        reached_quota = max_states // 2
        states = [
            *reached[:reached_quota],
            *below[: max_states - reached_quota],
        ]
        if not any(not chosen for _raw, chosen in states):
            states.append((0.0, ()))

    feasible = []
    for state in states:
        _raw, chosen = state
        if not chosen:
            continue
        effective, rate = state_values(state)
        if lower <= effective <= upper:
            feasible.append((state, effective, rate))
    if not feasible:
        return None
    state, effective, rate = min(feasible, key=lambda item: state_rank(item[0]))
    return list(state[1]), rate, effective


def _render_complete_shuffle_window(candidate, playback_rate, output_path):
    """Render one safe window, keeping video/audio on one bounded timebase."""
    path = candidate["path"]
    start = max(0.0, float(candidate["start"] or 0.0))
    duration = max(0.0, float(candidate["duration"] or 0.0))
    rate = float(playback_rate or 1.0)
    if duration <= 0.05:
        raise NaturalBoundaryError("自然收尾容错选中了无效的完整素材窗口。")
    if not (
        NATURAL_BOUNDARY_MIN_PLAYBACK_RATE - 1e-9
        <= rate
        <= NATURAL_BOUNDARY_MAX_PLAYBACK_RATE + 1e-9
    ):
        raise NaturalBoundaryError("自然收尾容错所需变速超过 12% 安全上限。")

    is_full_source = candidate["boundary"] == "source-start+source-end"
    if abs(rate - 1.0) <= 1e-6:
        if is_full_source:
            return path
        normalize_video(
            path,
            output_path,
            mute=False,
            trim=(start, duration),
        )
        return output_path

    output_duration = duration / rate
    command = ["ffmpeg", "-y"]
    if start > 0.0:
        command.extend(["-ss", f"{start:.6f}"])
    command.extend(["-t", f"{duration:.6f}", "-i", path])
    source_has_audio = has_audio_stream(path)
    if not source_has_audio:
        command.extend(
            [
                "-f",
                "lavfi",
                "-t",
                f"{output_duration:.6f}",
                "-i",
                "anullsrc=r=44100:cl=stereo",
            ]
        )
    video_filter = (
        f"[0:v]setpts=(PTS-STARTPTS)/{rate:.8f},"
        f"fps={FPS},trim=duration={output_duration:.6f},"
        f"scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},setsar=1,format=yuv420p,setpts=PTS-STARTPTS[vout]"
    )
    audio_input = "[0:a]" if source_has_audio else "[1:a]"
    audio_filter = (
        f"{audio_input}asetpts=PTS-STARTPTS,{_atempo_filter(rate)},"
        f"aresample=44100,apad,atrim=duration={output_duration:.6f},"
        "asetpts=PTS-STARTPTS[aout]"
    )
    command.extend(
        [
            "-filter_complex",
            f"{video_filter};{audio_filter}",
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            *_video_encoder_args(23, "veryfast"),
            "-fps_mode",
            "cfr",
            "-video_track_timescale",
            "90000",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-movflags",
            "+faststart",
            "-t",
            f"{output_duration:.6f}",
            output_path,
        ]
    )
    run_ffmpeg(command)
    rendered_duration = get_duration(output_path)
    video_duration = get_stream_duration(output_path, "v:0")
    audio_duration = get_stream_duration(output_path, "a:0")
    tolerance = max(0.15, 2.0 / max(1, int(FPS)))
    if (
        abs(rendered_duration - output_duration) > tolerance
        or video_duration <= 0.0
        or audio_duration <= 0.0
        or abs(video_duration - audio_duration) > tolerance
    ):
        raise RuntimeError(
            "自然收尾音画同步变速校验失败："
            f"目标 {output_duration:.3f}s，封装 {rendered_duration:.3f}s，"
            f"视频 {video_duration:.3f}s，音频 {audio_duration:.3f}s。"
        )
    return output_path


def _fit_shuffle_paths_to_complete_boundaries(
    valid,
    target_duration,
    *,
    window_index,
    max_extension,
    strict_error=None,
):
    """Bounded source fallback using only safe ends and bounded retime."""
    full_sets = _complete_shuffle_candidate_sets(
        valid,
        target_duration,
        max_extension,
        include_natural_points=False,
    )
    plan = _select_complete_shuffle_plan(
        full_sets,
        target_duration,
        max_extension,
        allow_retime=False,
    )
    plan_kind = "完整源文件 EOF 组合"
    natural_sets = None
    if plan is None:
        natural_sets = _complete_shuffle_candidate_sets(
            valid,
            target_duration,
            max_extension,
            include_natural_points=True,
        )
        plan = _select_complete_shuffle_plan(
            natural_sets,
            target_duration,
            max_extension,
            allow_retime=False,
        )
        plan_kind = "源文件边界 + 真实停顿/镜头点"
    if plan is None:
        plan = _select_complete_shuffle_plan(
            full_sets,
            target_duration,
            max_extension,
            allow_retime=True,
        )
        plan_kind = "完整源文件 EOF 组合 + 音画同步轻微变速"
    if plan is None and natural_sets is not None:
        plan = _select_complete_shuffle_plan(
            natural_sets,
            target_duration,
            max_extension,
            allow_retime=True,
        )
        plan_kind = "真实自然点组合 + 音画同步轻微变速"
    if plan is None:
        detail = f"（严格方案：{strict_error}）" if strict_error else ""
        raise NaturalBoundaryError(
            "安全自然边界容错已尝试完整文件 EOF、真实停顿/镜头点与最多 12% "
            "音画同步变速，仍无法在参考时长附近安全结束；"
            "系统未硬切半句话或动作。" + detail
        )

    selected, playback_rate, effective_duration = plan
    fitted = []
    audit_entries = []
    for output_index, candidate in enumerate(selected):
        output_path = os.path.join(
            TMP_DIR,
            f"shuffle_complete_fallback_{output_index:03d}_"
            f"w{int(window_index or 0):03d}.mp4",
        )
        fitted_path = _render_complete_shuffle_window(
            candidate,
            playback_rate,
            output_path,
        )
        _DERIVED_SHUFFLE_SOURCE_MAP[os.path.abspath(fitted_path)] = (
            _DERIVED_SHUFFLE_SOURCE_MAP.get(
                os.path.abspath(candidate["path"]),
                os.path.basename(candidate["path"]),
            )
        )
        fitted.append(fitted_path)
        audit_entries.append(
            {
                "role": "shuffle",
                "source": os.path.basename(candidate["path"]),
                "start_seconds": round(candidate["start"], 3),
                "end_seconds": round(candidate["end"], 3),
                # Audit the original safe source span. With at most 12% slow
                # down it still covers >80% of the rendered baseline contract.
                "duration_seconds": round(candidate["duration"], 3),
                "window_index": int(window_index or 0),
                "boundary": candidate["boundary"],
            }
        )
    SOURCE_WINDOW_AUDIT.extend(
        audit_entries[: max(0, 128 - len(SOURCE_WINDOW_AUDIT))]
    )
    rate_note = (
        "原速"
        if abs(playback_rate - 1.0) <= 1e-6
        else f"统一 {playback_rate:.4f}x 音画同步变速"
    )
    print(
        "[NaturalBoundaryFallback] 多素材按比例时间窗无法安全落点；"
        f"已改用 {plan_kind}，保留 {len(selected)}/{len(valid)} 个不同源，"
        f"预计打乱轨 {effective_duration:.3f}s，{rate_note}；"
        "未硬切、未循环补帧、未生成黑帧。"
    )
    return fitted


def fit_core_paths_to_budget(paths, target_duration, window_index=0):
    """Give every selected core a proportional, naturally bounded window."""
    valid = [(path, get_duration(path)) for path in paths]
    valid = [(path, duration) for path, duration in valid if duration > 0.01]
    target = max(0.0, float(target_duration or 0.0))
    if not valid or target <= 0.01:
        return [], 0.0
    total_source = sum(duration for _path, duration in valid)
    if total_source <= target + 0.01:
        for path, duration in valid:
            SOURCE_WINDOW_AUDIT.append(
                {
                    "role": "core",
                    "source": os.path.basename(path),
                    "start_seconds": 0.0,
                    "end_seconds": round(duration, 3),
                    "duration_seconds": round(duration, 3),
                    "window_index": int(window_index or 0),
                    "boundary": "source-end",
                }
            )
        return [path for path, _duration in valid], total_source

    fitted = []
    remaining_budget = target
    remaining_source = total_source
    stable_names = sorted(os.path.basename(path) for path, _duration in valid)
    stable_rank = {name: index for index, name in enumerate(stable_names)}
    cycle = max(1, int(window_index or 1)) - 1
    for source_index, (path, duration) in enumerate(valid):
        if remaining_budget <= 0.05:
            break
        proportional_budget = remaining_budget * duration / max(
            duration,
            remaining_source,
        )
        proportional_budget = min(duration, max(0.05, proportional_budget))
        # Advance the same source by exactly one slot per output. Multiplying
        # by source count would wrap a five-source/five-output job after only
        # four candidates when the minimum slot grid is used.
        ordinal = cycle + stable_rank[os.path.basename(path)] + 1
        source_paths = _fit_paths_to_duration(
            [path],
            proportional_budget,
            prefix=f"core_budget_{source_index:03d}",
            repeat=False,
            finish_last_clip=True,
            require_natural_boundary=False,
            window_index=ordinal,
            max_extension=min(18.0, max(4.0, proportional_budget * 0.25)),
            finish_before_budget=True,
            audit_role="core",
        )
        fitted.extend(source_paths)
        actual = sum(get_duration(item) for item in source_paths)
        remaining_budget = max(0.0, remaining_budget - actual)
        remaining_source = max(0.0, remaining_source - duration)
    actual_total = sum(get_duration(path) for path in fitted)
    if actual_total <= 0.05:
        raise NaturalBoundaryError("核心素材无法形成可用的自然窗口。")
    return fitted, actual_total


def fit_shuffle_paths_to_budget(
    paths,
    target_duration,
    *,
    window_index=1,
    max_extension=MAX_NATURAL_TAIL_SECONDS,
    require_natural_boundary=True,
    allow_complete_fallback=False,
):
    """Take one bounded natural window from every usable shuffle source."""
    valid = [(path, get_duration(path)) for path in paths]
    valid = [(path, duration) for path, duration in valid if duration > 0.01]
    target = max(0.0, float(target_duration or 0.0))
    if not valid or target <= 0.01:
        return []
    audit_start = len(SOURCE_WINDOW_AUDIT)
    try:
        if len(valid) == 1:
            return _fit_paths_to_duration(
                [valid[0][0]],
                target,
                prefix="shuffle_source_budget_000",
                repeat=False,
                finish_last_clip=True,
                require_natural_boundary=require_natural_boundary,
                window_index=max(1, int(window_index or 1)),
                max_extension=max_extension,
                audit_role="shuffle",
            )

        total_source = sum(duration for _path, duration in valid)
        stable_names = sorted(os.path.basename(path) for path, _duration in valid)
        stable_rank = {name: index for index, name in enumerate(stable_names)}
        # Plan every source window before rendering any of them.  Previously a
        # late source could reject the candidate after earlier source windows
        # had already consumed CPU filters and NVENC time.  Planning first keeps
        # the exact same deterministic windows while making a rejected plan
        # effectively analysis-only.
        planned = []
        remaining_target = target
        remaining_source = total_source
        per_source_extension = max(0.0, float(max_extension or 0.0)) / len(valid)
        cycle = max(1, int(window_index or 1)) - 1
        for source_index, (path, duration) in enumerate(valid):
            allocation = remaining_target * duration / max(duration, remaining_source)
            allocation = min(duration, max(0.05, allocation))
            basename_rank = stable_rank[os.path.basename(path)]
            # One step per candidate keeps every individual source on a fresh slot
            # across the full 5 outputs × 4 attempts search space. The basename
            # rank remains a stable per-source offset independent of input order.
            ordinal = cycle + basename_rank + 1
            prefix = (
                f"shuffle_source_budget_{source_index:03d}_"
                f"o{ordinal:03d}"
            )
            if duration <= allocation + 0.01:
                start = 0.0
                end = duration
                boundary = "source-end"
                output_path = None
            else:
                start, end, boundary = _natural_source_window(
                    path,
                    allocation,
                    duration,
                    window_index=ordinal,
                    max_extension=per_source_extension,
                    require_natural_boundary=require_natural_boundary,
                    source_end_grace=SHUFFLE_SOURCE_EOF_PLANNING_GRACE_SECONDS,
                    spread_windows=True,
                )
                if end - start <= 0.01:
                    remaining_source = max(0.0, remaining_source - duration)
                    continue
                output_path = os.path.join(
                    TMP_DIR,
                    f"{prefix}_natural_000_w{ordinal:03d}.mp4",
                )
            planned_duration = max(0.0, end - start)
            planned.append(
                {
                    "path": path,
                    "output_path": output_path,
                    "start": start,
                    "end": end,
                    "duration": planned_duration,
                    "window_index": ordinal,
                    "boundary": boundary,
                }
            )
            # A natural end is never earlier than the requested allocation.  A
            # whole short source consumes only its real duration.  Therefore the
            # next proportional share is independent of encoded frame rounding.
            remaining_target = max(
                0.0,
                remaining_target - min(allocation, planned_duration),
            )
            remaining_source = max(0.0, remaining_source - duration)
        if not planned:
            raise NaturalBoundaryError("打乱素材无法形成可用的自然时间窗。")

        fitted = []
        audit_entries = []
        for item in planned:
            output_path = item["output_path"]
            if output_path:
                normalize_video(
                    item["path"],
                    output_path,
                    mute=False,
                    trim=(item["start"], item["duration"]),
                )
                _DERIVED_SHUFFLE_SOURCE_MAP[os.path.abspath(output_path)] = (
                    os.path.basename(item["path"])
                )
                fitted.append(output_path)
            else:
                fitted.append(item["path"])
            audit_entries.append(
                {
                    "role": "shuffle",
                    "source": os.path.basename(item["path"]),
                    "start_seconds": round(item["start"], 3),
                    "end_seconds": round(item["end"], 3),
                    "duration_seconds": round(item["duration"], 3),
                    "window_index": int(item["window_index"]),
                    "boundary": item["boundary"],
                }
            )
        SOURCE_WINDOW_AUDIT.extend(
            audit_entries[: max(0, 128 - len(SOURCE_WINDOW_AUDIT))]
        )
        return fitted
    except NaturalBoundaryError as exc:
        # A preceding source may already have appended an audit record before a
        # later proportional window failed. The fallback publishes one atomic
        # replacement plan, so remove only this call's partial evidence.
        del SOURCE_WINDOW_AUDIT[audit_start:]
        if not allow_complete_fallback:
            raise
        return _fit_shuffle_paths_to_complete_boundaries(
            valid,
            target,
            window_index=max(1, int(window_index or 1)),
            max_extension=max_extension,
            strict_error=exc,
        )


def allocate_timeline_budgets(target_duration, core_duration, quality_duration, dedup_duration):
    """Compatibility wrapper for the 8018 core + unified-shuffle budget."""
    target = float(target_duration)
    core_source = max(0.0, float(core_duration or 0.0))
    shuffle = max(0.0, float(quality_duration or 0.0)) + max(
        0.0, float(dedup_duration or 0.0)
    )
    if not math.isfinite(target) or target < MIN_TARGET_SECONDS:
        raise RuntimeError("参考成片时长必须是大于 0 的有限秒数。")
    if core_source <= 0.0:
        raise RuntimeError("没有可读取的核心素材，请重新选择或上传有效视频。")
    if shuffle <= 0.0:
        raise RuntimeError("没有可读取的打乱素材，请重新选择或上传有效视频。")
    if HARDCUT_DUB_MODE or PRESERVE_RAW_CORE:
        core_effective = core_source
        core_budget = core_source
        shuffle_budget = max(0.0, target - core_budget)
        return {
            "target": target,
            "core": core_budget,
            "core_source": core_source,
            "core_effective": core_effective,
            "core_budget": core_budget,
            "core_trimmed": 0.0,
            "quality": 0.0,
            "dedup": shuffle_budget,
            "shuffle": shuffle_budget,
            "shuffle_source": shuffle,
            "minimum_shuffle": min(shuffle, max(0.5, float(BUDGET_SLICE_FLOOR_SECONDS or 3.0))),
            "fixed_prefix": core_budget,
        }
    core_effective = min(
        core_source,
        shuffle / SHUFFLE_TO_CORE_MIN_RATIO,
    )
    if target + 0.001 < core_effective * DURATION_MULTIPLIER_MIN:
        raise RuntimeError(
            f"时长条件未满足：T={target:.1f}秒，必须 ≥ C×1="
            f"{core_effective:.1f}秒。"
        )
    if target > core_effective * DURATION_MULTIPLIER_MAX + 0.001:
        raise RuntimeError(
            f"时长条件未满足：T={target:.1f}秒，不能超过 C×10="
            f"{core_effective * 10.0:.1f}秒。"
        )
    core_budget = min(core_effective, target * CORE_TO_OUTPUT_MAX_RATIO)
    shuffle_budget = max(0.0, target - core_budget)
    return {
        "target": target,
        "core": core_budget,
        "core_source": core_source,
        "core_effective": core_effective,
        "core_budget": core_budget,
        "core_trimmed": max(0.0, core_source - core_budget),
        "quality": 0.0,
        "dedup": shuffle_budget,
        "shuffle": shuffle_budget,
        "shuffle_source": shuffle,
        "minimum_shuffle": core_effective * SHUFFLE_TO_CORE_MIN_RATIO,
        "fixed_prefix": core_budget,
    }


def allocate_three_pool_budgets(target_duration, core_duration, shuffle_duration):
    """Recompute and verify C_raw/C_eff/C_use/T against probed media."""
    target = float(target_duration)
    core_source = max(0.0, float(core_duration or 0.0))
    shuffle = max(0.0, float(shuffle_duration or 0.0))
    contract_epsilon = 0.001
    formula_tolerance = 0.05
    if not math.isfinite(target) or target < MIN_TARGET_SECONDS:
        raise RuntimeError("参考成片时长必须是大于 0 的有限秒数。")
    if not math.isfinite(core_source) or core_source <= 0.10:
        raise RuntimeError("核心素材没有可用的有效时长。")
    if not math.isfinite(shuffle) or shuffle <= 0.10:
        raise RuntimeError("打乱素材没有可用的有效时长。")
    if HARDCUT_DUB_MODE or PRESERVE_RAW_CORE:
        multiplier_raw = os.environ.get("V16_DURATION_MULTIPLIER", "").strip() or "4.5"
        try:
            multiplier = float(multiplier_raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("页面下发的时长倍率 K 无效。") from exc
        if not math.isfinite(multiplier) or not (
            DURATION_MULTIPLIER_MIN <= multiplier <= DURATION_MULTIPLIER_MAX
        ):
            raise RuntimeError("时长倍率 K 必须在 1.0–10.0 之间。")
        core_effective = core_source
        core_budget = core_source
        slice_s = max(0.5, float(BUDGET_SLICE_FLOOR_SECONDS or 3.0))
        desired_shuffle = max(
            min(shuffle, slice_s),
            core_source * multiplier - core_source,
        )
        shuffle_budget = max(min(shuffle, 0.5), min(shuffle, desired_shuffle))
        expected_target = core_budget + shuffle_budget
        if abs(target - expected_target) > 0.50:
            target = expected_target
        return {
            "target": target,
            "core": core_budget,
            "core_source": core_source,
            "core_effective": core_effective,
            "core_budget": core_budget,
            "quality": 0.0,
            "dedup": shuffle_budget,
            "shuffle": shuffle_budget,
            "shuffle_source": shuffle,
            "required_shuffle": min(shuffle, slice_s),
            "multiplier": multiplier,
            "fixed_prefix": core_budget,
        }
    core_effective = min(
        core_source,
        shuffle / SHUFFLE_TO_CORE_MIN_RATIO,
    )
    required_shuffle = core_effective * SHUFFLE_TO_CORE_MIN_RATIO

    multiplier_raw = os.environ.get("V16_DURATION_MULTIPLIER", "").strip()
    if not multiplier_raw:
        raise RuntimeError("页面未下发时长倍率 K，已停止以防止绕过时长契约。")
    try:
        multiplier = float(multiplier_raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("页面下发的时长倍率 K 无效。") from exc
    if not math.isfinite(multiplier) or not (
        DURATION_MULTIPLIER_MIN <= multiplier <= DURATION_MULTIPLIER_MAX
    ):
        raise RuntimeError("时长倍率 K 必须在 1.0–10.0 之间。")
    formula_target = core_effective * multiplier
    if abs(target - formula_target) > formula_tolerance:
        raise RuntimeError(
            f"时长公式未通过：T={target:.3f}s，但 C_eff×K="
            f"{core_effective:.3f}×"
            f"{multiplier:.3f}={formula_target:.3f}s。"
        )

    core_budget = min(core_effective, target * CORE_TO_OUTPUT_MAX_RATIO)
    shuffle_budget = max(0.0, target - core_budget)
    for env_name, expected, label in (
        ("V16_CORE_EFFECTIVE_SECONDS", core_effective, "有效核心基准"),
        ("V16_CORE_BUDGET_SECONDS", core_budget, "核心纳入预算"),
    ):
        posted_raw = os.environ.get(env_name, "").strip()
        if not posted_raw:
            continue
        try:
            posted = float(posted_raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"页面下发的{label}无效。") from exc
        if not math.isfinite(posted) or abs(posted - expected) > formula_tolerance:
            raise RuntimeError(f"页面下发的{label}与素材探测结果不一致。")
    posted_budget_raw = os.environ.get("V16_SHUFFLE_BUDGET_SECONDS", "").strip()
    if posted_budget_raw:
        try:
            posted_budget = float(posted_budget_raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("页面下发的打乱时长预算无效。") from exc
        if (
            not math.isfinite(posted_budget)
            or abs(posted_budget - shuffle_budget) > formula_tolerance
        ):
            raise RuntimeError(
                "页面下发的打乱预算与 T-C 不一致，已停止以防止绕过。"
            )
    return {
        "target": target,
        "core": core_budget,
        "core_source": core_source,
        "core_effective": core_effective,
        "core_budget": core_budget,
        "quality": 0.0,
        "dedup": shuffle_budget,
        "shuffle": shuffle_budget,
        "shuffle_source": shuffle,
        "required_shuffle": required_shuffle,
        "multiplier": multiplier,
        "fixed_prefix": core_budget,
    }


def _budget_override(name, fallback):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return float(fallback)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float(fallback)
    return max(0.0, value)


def _voice_budget_for_target(target_duration, has_voice_material=True):
    if not has_voice_material:
        return 0.0
    target = max(0.0, float(target_duration or 0.0))
    return min(target, DEDUP2_TARGET_SECONDS, max(12.0, target * 0.60))


def concat_clips(
    clips,
    output_path,
    target_duration=TARGET_TOTAL_DURATION,
    finish_last_clip=FINISH_LAST_CLIP,
    repeat=False,
    preserve_all=False,
):
    """Join real clips without padding; optionally preserve every supplied clip."""
    concat_list = os.path.join(TMP_DIR, "concat.txt")
    valid = [(clip, get_duration(clip)) for clip in clips]
    valid = [(clip, duration) for clip, duration in valid if duration > 0.01]
    if not valid:
        raise RuntimeError("时间线没有可用片段，无法生成成片。")
    target = (
        sum(duration for _clip, duration in valid)
        if preserve_all
        else max(0.5, float(target_duration))
    )
    total = 0.0
    selected_paths = []
    with open(concat_list, "w", encoding="utf-8") as f:
        index = 0
        while total < target - 0.01:
            if index >= len(valid) and not repeat:
                break
            clip, dur = valid[index % len(valid)]
            remaining = target - total
            if dur > remaining + 0.01:
                if finish_last_clip:
                    if dur - remaining <= MAX_NATURAL_TAIL_SECONDS + 0.01:
                        selected_path = clip
                        selected_duration = dur
                    else:
                        selected_duration = _natural_cut_duration(
                            clip,
                            remaining,
                            dur,
                        )
                        selected_path = os.path.join(
                            TMP_DIR,
                            f"natural_final_{index:03d}.mp4",
                        )
                        normalize_video(
                            clip,
                            selected_path,
                            mute=False,
                            trim=(0.0, selected_duration),
                        )
                    escaped = selected_path.replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")
                    selected_paths.append(selected_path)
                    total += selected_duration
                    break
                trimmed = os.path.join(TMP_DIR, f"trim_final_{index:03d}.mp4")
                normalize_video(clip, trimmed, mute=False, trim=(0, remaining))
                escaped = trimmed.replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")
                selected_paths.append(trimmed)
                total = target
                break
            escaped = clip.replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")
            selected_paths.append(clip)
            total += dur
            index += 1
    _render_concat_list_cfr(
        concat_list,
        output_path,
        total,
        input_paths=selected_paths,
    )
    return get_duration(output_path) or total


def prepare_full_spec_mask_track(mask_path):
    """Split, reshuffle and mildly restyle one hidden mask source."""
    duration = get_duration(mask_path)
    if duration <= 0.1:
        raise RuntimeError("蒙版素材无法读取有效时长。")
    boundaries = [0.0]
    cursor = 0.0
    segment_lengths = (4.0, 5.5, 3.5, 6.0)
    index = 0
    while cursor < duration - 1.0:
        cursor = min(duration, cursor + segment_lengths[index % len(segment_lengths)])
        boundaries.append(cursor)
        index += 1
    if boundaries[-1] < duration:
        boundaries.append(duration)
    if len(boundaries) >= 3 and boundaries[-1] - boundaries[-2] < 1.0:
        boundaries.pop(-2)
    segments = list(zip(boundaries, boundaries[1:]))
    random.Random(_stable_seed([mask_path], "mask-track")).shuffle(segments)

    filters = []
    labels = []
    for index, (start, end) in enumerate(segments):
        label = f"[mask{index}]"
        hue = (-7, 5, 9, -4)[index % 4]
        saturation = (0.92, 1.04, 0.96, 1.08)[index % 4]
        filters.append(
            f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,"
            f"hue=h={hue}:s={saturation:.3f},eq=contrast=1.03{label}"
        )
        labels.append(label)
    filters.append(
        f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0,"
        f"fps={FPS},format=yuv420p[maskout]"
    )
    output_path = os.path.join(TMP_DIR, "full_spec_mask_shuffled.mp4")
    run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            mask_path,
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[maskout]",
            "-an",
            *_video_encoder_args(25, "veryfast"),
            output_path,
        ]
    )
    return output_path


def create_full_spec_heart_asset():
    """Create the four-corner, text-free heart overlay."""
    if not has_module("PIL"):
        raise RuntimeError("爱心贴纸需要 Pillow。")
    heart_path = os.path.join(TMP_DIR, "full_spec_heart.png")
    code = r"""
from PIL import Image, ImageDraw
import sys
heart_path=sys.argv[1]
heart=Image.new('RGBA',(34,32),(0,0,0,0))
h=ImageDraw.Draw(heart)
h.ellipse((3,2,18,17),fill=(255,182,203,188))
h.ellipse((16,2,31,17),fill=(255,182,203,188))
h.polygon([(3,10),(31,10),(17,30)],fill=(255,182,203,188))
heart.save(heart_path)
"""
    exe = PY312 if os.path.exists(PY312) else sys.executable
    result = subprocess.run(
        [exe, "-c", code, heart_path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    if result.returncode != 0 or not os.path.exists(heart_path):
        raise RuntimeError(f"爱心贴纸生成失败：{result.stderr[-400:]}")
    return heart_path


def final_render_full_spec(base_concat, output_path, render_duration):
    """Render retained visual layers while preserving the timeline audio."""
    mask_vids = get_files(MASK_DIR, (".mp4", ".mov"), "蒙版素材")
    if not mask_vids:
        raise RuntimeError("完整规范模式缺少蒙版素材。")

    command = ["ffmpeg", "-y", "-i", base_concat]
    filters = []
    input_count = 1
    video_label = "[0:v]"

    shuffled_mask = prepare_full_spec_mask_track(mask_vids[0])
    command.extend(["-stream_loop", "-1", "-i", shuffled_mask])
    filters.append(
        f"[{input_count}:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},fps={FPS},format=yuv420p[masklayer]"
    )
    filters.append(
        f"{video_label}[masklayer]blend=all_mode=overlay:all_opacity=0.010[v_masked]"
    )
    video_label = "[v_masked]"
    input_count += 1

    # Full-spec mode never trusts arbitrary marketing stickers. Four fixed,
    # text-free hearts are generated here, which also caps size and quantity.
    heart_overlay = create_full_spec_heart_asset()
    command.extend(["-loop", "1", "-i", heart_overlay])
    filters.append(
        f"[{input_count}:v]format=rgba,split=4[heart0][heart1][heart2][heart3]"
    )
    input_count += 1
    heart_positions = (
        ("12", "12"),
        ("W-w-12", "12"),
        ("12", "H-h-12"),
        ("W-w-12", "H-h-12"),
    )
    for index, (x_pos, y_pos) in enumerate(heart_positions):
        next_label = f"[v_heart{index}]"
        filters.append(
            f"{video_label}[heart{index}]overlay=x='{x_pos}':y='{y_pos}'{next_label}"
        )
        video_label = next_label

    filters.append(
        f"{video_label}trim=duration={render_duration:.3f},fps={FPS},"
        "setpts=PTS-STARTPTS,format=yuv420p[v_exact]"
    )
    filters.append(
        f"[0:a]aresample=44100,apad,atrim=duration={render_duration:.3f},"
        "asetpts=PTS-STARTPTS[a_exact]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[v_exact]",
            "-map",
            "[a_exact]",
            *_video_encoder_args(23, "fast"),
            "-fps_mode",
            "cfr",
            "-video_track_timescale",
            "90000",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            "-t",
            f"{render_duration:.3f}",
            output_path,
        ]
    )
    run_ffmpeg(command)



def _timepin_component_expression(render_duration, component_index, divisor):
    frame_count = max(2, int(round(float(render_duration) * FPS)))
    last_frame = frame_count - 1
    positioned = [
        (frame[0] / 100.0 * last_frame, frame[component_index] / divisor)
        for frame in TIMEPIN_ZOOM_KEYFRAMES
    ]
    expression = f"{positioned[-1][1]:.6f}"
    for (start_frame, start_value), (end_frame, end_value) in reversed(
        list(zip(positioned, positioned[1:]))
    ):
        span = max(0.000001, end_frame - start_frame)
        progress = f"(n-{start_frame:.6f})/{span:.6f}"
        eased = f"(0.5-0.5*cos(PI*{progress}))"
        interpolated = (
            f"({start_value:.6f}+({end_value - start_value:.6f})*{eased})"
        )
        expression = (
            f"if(lt(n\\,{end_frame:.6f})\\,{interpolated}\\,{expression})"
        )
    return expression


def _timepin_scale_expression(render_duration):
    return _timepin_component_expression(render_duration, 1, 100.0)


def _timepin_vertical_expression(render_duration):
    return _timepin_component_expression(render_duration, 2, 100.0)


def build_timepin_zoom_filter(
    render_duration, video_label="[0:v]", prefix="timepin"
):
    """Build a cover-zoom curve with safe vertical travel and no black edge.

    The foreground is always 100%–130% of the fixed 720x1280 canvas. Vertical
    movement is a fraction of the extra height created by the current zoom, so
    the overlay stays between H-h and 0 at every frame. The unchanged source
    remains underneath as a safety canvas. Audio, frame count, timeline length
    and final-encode count are unchanged.
    """
    factor = _timepin_scale_expression(render_duration)
    vertical = _timepin_vertical_expression(render_duration)
    background = f"{prefix}_background"
    foreground = f"{prefix}_foreground"
    background_ready = f"{prefix}_background_ready"
    foreground_scaled = f"{prefix}_foreground_scaled"
    output = f"{prefix}_out"
    filters = [
        f"{video_label}split=2[{background}][{foreground}]",
        f"[{background}]setsar=1[{background_ready}]",
        (
            f"[{foreground}]scale="
            f"w='ceil(iw*{factor}/2)*2':"
            f"h='ceil(ih*{factor}/2)*2':"
            f"eval=frame:flags=lanczos,setsar=1[{foreground_scaled}]"
        ),
        (
            f"[{background_ready}][{foreground_scaled}]"
            f"overlay=x='(W-w)/2':"
            f"y='(H-h)/2+((h-H)/2)*({vertical})':"
            f"eval=frame:shortest=1[{output}]"
        ),
    ]
    return filters, f"[{output}]"


def build_corner_mask_reveal_filter(
    video_label,
    mask_input_index,
    mode=None,
    radius_px=None,
    prefix="corner_mask",
):
    """Expose the looping mask through four rounded corner windows only.

    The foreground timeline is never resized or cropped.  In enabled modes we
    retain the legacy 1% full-frame hidden mask, then place four small rounded
    wedges from the same mask over the matching corners.  There are no audio
    filters here; final_render continues to map audio exclusively from input 0.
    """
    selected_mode = CORNER_MASK_REVEAL_MODE if mode is None else str(mode).strip().lower()
    if selected_mode not in CORNER_MASK_REVEAL_PRESETS:
        raise ValueError(
            "四角露底蒙版模式无效；只允许 off、gentle、standard 或 strong。"
        )
    if radius_px is None:
        radius = (
            CORNER_MASK_REVEAL_RADIUS
            if mode is None
            else CORNER_MASK_REVEAL_PRESETS[selected_mode]
        )
    else:
        radius = _validated_corner_mask_radius(radius_px)
        if selected_mode != _corner_mask_mode_for_radius(radius):
            raise ValueError("四角露底蒙版档位和圆角大小不一致。")

    hidden_source = f"{prefix}_hidden_source"
    hidden_layer = f"{prefix}_hidden_layer"
    hidden_out = f"{prefix}_hidden_out"
    normalize = (
        f"[{mask_input_index}:v]scale={W}:{H}:"
        "force_original_aspect_ratio=increase,"
        f"crop={W}:{H},fps={FPS},setpts=PTS-STARTPTS,setsar=1"
    )
    if radius <= 0:
        return [
            (
                f"{normalize},format=yuva420p,"
                f"colorchannelmixer=aa=0.01[{hidden_layer}]"
            ),
            (
                f"{video_label}[{hidden_layer}]"
                f"overlay=0:0:shortest=1:format=auto[{hidden_out}]"
            ),
        ], f"[{hidden_out}]"

    feather = max(4, min(18, radius // 12))
    corners = (
        ("tl", "0", "0", radius, radius, "0", "0"),
        ("tr", f"iw-{radius}", "0", 0, radius, "W-w", "0"),
        ("bl", "0", f"ih-{radius}", radius, 0, "0", "H-h"),
        ("br", f"iw-{radius}", f"ih-{radius}", 0, 0, "W-w", "H-h"),
    )
    split_labels = "".join(
        f"[{prefix}_{name}_source]" for name, *_ in corners
    )
    filters = [
        (
            f"{normalize},format=yuv420p,split=5"
            f"[{hidden_source}]{split_labels}"
        ),
        (
            f"[{hidden_source}]format=yuva420p,"
            f"colorchannelmixer=aa=0.01[{hidden_layer}]"
        ),
        (
            f"{video_label}[{hidden_layer}]"
            f"overlay=0:0:shortest=1:format=auto[{hidden_out}]"
        ),
    ]
    current_label = f"[{hidden_out}]"
    for name, crop_x, crop_y, center_x, center_y, overlay_x, overlay_y in corners:
        patch_label = f"{prefix}_{name}_patch"
        next_label = f"{prefix}_{name}_out"
        alpha = (
            "clip((hypot(X-"
            f"{center_x},Y-{center_y})-{radius}+{feather})"
            f"*255/{feather},0,255)"
        )
        filters.append(
            f"[{prefix}_{name}_source]"
            f"crop=w={radius}:h={radius}:x='{crop_x}':y='{crop_y}',"
            "format=rgba,"
            "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
            f"a='{alpha}'[{patch_label}]"
        )
        filters.append(
            f"{current_label}[{patch_label}]"
            f"overlay=x='{overlay_x}':y='{overlay_y}':"
            f"shortest=1:format=auto[{next_label}]"
        )
        current_label = f"[{next_label}]"
    return filters, current_label


def final_render(
    base_concat,
    output_path,
    render_duration=None,
    mask_source=None,
    corner_mask_reveal_mode=None,
    corner_mask_reveal_radius_px=None,
    dynamic_sticker_enabled=True,
):
    render_duration = max(
        0.5,
        float(render_duration or get_duration(base_concat) or TARGET_TOTAL_DURATION),
    )
    selected_corner_mode = (
        CORNER_MASK_REVEAL_MODE
        if corner_mask_reveal_mode is None
        else str(corner_mask_reveal_mode).strip().lower()
    )
    if selected_corner_mode not in CORNER_MASK_REVEAL_PRESETS:
        raise ValueError(
            "四角露底蒙版模式无效；只允许 off、gentle、standard 或 strong。"
        )
    if corner_mask_reveal_radius_px is None:
        selected_corner_radius = (
            CORNER_MASK_REVEAL_RADIUS
            if corner_mask_reveal_mode is None
            else CORNER_MASK_REVEAL_PRESETS[selected_corner_mode]
        )
    else:
        selected_corner_radius = _validated_corner_mask_radius(
            corner_mask_reveal_radius_px
        )
        if selected_corner_mode != _corner_mask_mode_for_radius(
            selected_corner_radius
        ):
            raise ValueError("四角露底蒙版档位和圆角大小不一致。")
    if FULL_SPEC_MODE:
        if TIMEPIN_ZOOM_ACTIVE:
            raise RuntimeError("完整规范模式暂不支持时间针推拉，已停止以避免静默忽略。")
        if selected_corner_radius > 0:
            raise RuntimeError(
                "完整规范模式暂不支持四角露底蒙版，已停止以避免静默忽略。"
            )
        return final_render_full_spec(base_concat, output_path, render_duration)
    all_sticker_files = get_files(
        STICKER_DIR,
        (".png", ".gif", ".mp4"),
        "贴纸素材池",
    )
    sticker_files, dynamic_sticker_files = partition_sticker_files(
        all_sticker_files
    )
    dynamic_sticker = (
        parse_fullscreen_sticker_config(dynamic_sticker_files)
        if dynamic_sticker_enabled
        else None
    )
    if THREE_POOL_MODE:
        # The selected source is an extra read-only FFmpeg input. It stays in
        # the main shuffle timeline and its audio is never mapped below.
        mask_vids = [mask_source] if mask_source else []
    else:
        mask_vids = get_files(MASK_DIR, (".mp4", ".mov"), "蒙版素材")
    if selected_corner_radius > 0 and not mask_vids:
        print(
            "[WARN] 已选择四角露底蒙版，但本次没有可用蒙版；"
            "已安全关闭该可选特效。"
        )
        selected_corner_mode = "off"
        selected_corner_radius = 0
    if (
        not sticker_files
        and not dynamic_sticker
        and not mask_vids
        and not TIMEPIN_ZOOM_ACTIVE
    ):
        # Preserve the zero-generation fast path only after checking the real
        # stream.  Most timelines come from _render_concat_list_cfr and are
        # already H.264/yuv420p; stale outputs from an older build can be
        # yuv444p and must not be copied into a Windows delivery unchanged.
        if _is_windows_media_player_h264_compatible(base_concat):
            run_ffmpeg(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    base_concat,
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-c",
                    "copy",
                    "-t",
                    f"{render_duration:.3f}",
                    "-movflags",
                    "+faststart",
                    output_path,
                ]
            )
        else:
            print(
                "[WARN] 快速定稿输入不是 Windows 播放器安全的 "
                "H.264/yuv420p；已自动执行兼容 H.264/yuv420p 重编码。"
            )
            run_ffmpeg(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    base_concat,
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-vf",
                    (
                        f"scale={W}:{H}:force_original_aspect_ratio=increase:"
                        f"flags=lanczos,crop={W}:{H},setsar=1,"
                        f"fps={FPS},format=yuv420p"
                    ),
                    *_video_encoder_args(23, "fast"),
                    "-fps_mode",
                    "cfr",
                    "-video_track_timescale",
                    "90000",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-movflags",
                    "+faststart",
                    "-t",
                    f"{render_duration:.3f}",
                    output_path,
                ]
            )
        return
    sticker_size_width = max(12, int(round(40 * safe_sticker_size_percent() / 100)))

    cmd = ["ffmpeg", "-y", "-i", base_concat]
    filter_parts = []
    v_out = "[0:v]"
    a_out = "0:a"
    inputs_count = 1

    if TIMEPIN_ZOOM_ACTIVE:
        timepin_filters, v_out = build_timepin_zoom_filter(render_duration, v_out)
        filter_parts.extend(timepin_filters)
        keyframe_summary = " → ".join(
            f"{position:.0f}%:{scale:.0f}%/上下{vertical:+.0f}%"
            for position, scale, vertical in TIMEPIN_ZOOM_KEYFRAMES
        )
        print(
            f"   -> 时间针推拉：只放大无黑边模式 {keyframe_summary}；"
            "缩放与上下位置平滑插值，不跳帧、不改变时长。"
        )

    if mask_vids:
        cmd.extend(["-stream_loop", "-1", "-i", mask_vids[0]])
        corner_filters, v_out = build_corner_mask_reveal_filter(
            v_out,
            inputs_count,
            mode=selected_corner_mode,
            radius_px=selected_corner_radius,
        )
        filter_parts.extend(corner_filters)
        if selected_corner_radius > 0:
            print(
                f"   -> 四角露底蒙版：{selected_corner_mode} 档，"
                f"圆角 {selected_corner_radius}px，"
                "全片持续；中心和四边中点不变，蒙版音频未映射。"
            )
        inputs_count += 1

    if dynamic_sticker:
        dynamic_input_index = inputs_count
        cmd.extend(
            [
                "-stream_loop",
                "-1",
                "-i",
                dynamic_sticker["path"],
            ]
        )
        dynamic_filters, v_out, key_strategy = build_dynamic_sticker_filter(
            v_out,
            dynamic_input_index,
            render_duration,
            dynamic_sticker["path"],
            key_mode=dynamic_sticker["key_mode"],
        )
        filter_parts.extend(dynamic_filters)
        inputs_count += 1
        print(
            f"   -> 动态全屏贴纸：{dynamic_sticker['name']}；"
            f"{key_strategy}，全片循环，素材音频永不映射。"
        )

    if sticker_files:
        unique_stickers = []
        configured_stickers = parse_sticker_corner_config(sticker_files)
        if configured_stickers is None:
            sticker_plan = [
                ("top_left", sticker_files[0]),
                ("top_right", sticker_files[1] if len(sticker_files) > 1 else sticker_files[0]),
                ("bottom_left", sticker_files[2] if len(sticker_files) > 2 else sticker_files[0]),
                ("bottom_right", sticker_files[3] if len(sticker_files) > 3 else sticker_files[0]),
            ]
        else:
            sticker_plan = [
                (corner, configured_stickers[corner])
                for corner in STICKER_CORNERS
                if corner in configured_stickers
            ]
        for _, s in sticker_plan:
            if s not in unique_stickers:
                unique_stickers.append(s)
        stk_idx_map = {}
        for s in unique_stickers:
            cmd.extend(["-i", s])
            stk_idx_map[s] = inputs_count
            filter_parts.append(
                f"[{inputs_count}:v]scale={sticker_size_width}:-1[stk{inputs_count}]"
            )
            inputs_count += 1
        corner_positions = {
            "top_left": "0:0",
            "top_right": "W-w:0",
            "bottom_left": "0:H-h",
            "bottom_right": "W-w:H-h",
        }
        v_curr = v_out
        for i, (corner, path) in enumerate(sticker_plan):
            idx = stk_idx_map[path]
            next_v = f"[v_stk_c{i}]"
            filter_parts.append(
                f"{v_curr}[stk{idx}]overlay={corner_positions[corner]}{next_v}"
            )
            v_curr = next_v
        v_out = v_curr

    filter_parts.extend(
        [
        f"{v_out}tpad=stop_mode=clone:stop_duration=1,"
        f"trim=duration={render_duration:.3f},fps={FPS},"
        "setpts=PTS-STARTPTS,format=yuv420p[v_exact]",
        f"[0:a]aresample=44100,apad,atrim=duration={render_duration:.3f},"
        "asetpts=PTS-STARTPTS[a_exact]",
        ]
    )
    v_out = "[v_exact]"
    a_out = "[a_exact]"

    filter_complex = ";".join(filter_parts)
    if filter_complex:
        cmd.extend(["-filter_complex", filter_complex])
    cmd.extend(
        [
            "-map",
            v_out,
            "-map",
            a_out,
            *_video_encoder_args(
                18
                if (
                    TIMEPIN_ZOOM_ACTIVE
                    or selected_corner_radius > 0
                    or dynamic_sticker
                )
                else 23,
                "fast",
            ),
            "-fps_mode",
            "cfr",
            "-video_track_timescale",
            "90000",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            "-t",
            f"{render_duration:.3f}",
            output_path,
        ]
    )
    try:
        run_ffmpeg(cmd)
    except RuntimeError:
        if dynamic_sticker and dynamic_sticker_enabled:
            print(
                "[WARN] 动态全屏贴纸渲染失败，已只关闭该可选层并重试；"
                "主视频、时间针、蒙版和四角 PNG 保持不变。"
            )
            return final_render(
                base_concat,
                output_path,
                render_duration=render_duration,
                mask_source=mask_source,
                corner_mask_reveal_mode=selected_corner_mode,
                corner_mask_reveal_radius_px=selected_corner_radius,
                dynamic_sticker_enabled=False,
            )
        if selected_corner_radius > 0:
            print(
                "[WARN] 四角露底特效渲染失败，已关闭该可选特效，"
                "并恢复 1% 隐藏蒙版重试一次。"
            )
            return final_render(
                base_concat,
                output_path,
                render_duration=render_duration,
                mask_source=mask_source,
                corner_mask_reveal_mode="off",
                corner_mask_reveal_radius_px=0,
                dynamic_sticker_enabled=dynamic_sticker_enabled,
            )
        raise


def apply_full_spec_person_effects(input_path, output_path):
    """Automatically apply person effects only after the protected 0–60s core block."""
    if not FACE_EFFECTS:
        shutil.copyfile(input_path, output_path)
        return output_path
    fully_effected = os.path.join(TMP_DIR, "full_spec_person_effects_all.mp4")
    apply_face_effect_chain(input_path, fully_effected)
    duration = get_duration(input_path)
    if duration <= SPEC_CORE_SECONDS + 0.05:
        shutil.copyfile(input_path, output_path)
        return output_path
    filter_complex = (
        f"[0:v]trim=start=0:end={SPEC_CORE_SECONDS:.3f},setpts=PTS-STARTPTS[v_core];"
        f"[1:v]trim=start={SPEC_CORE_SECONDS:.3f}:end={duration:.3f},"
        "setpts=PTS-STARTPTS[v_effected];"
        "[v_core][v_effected]concat=n=2:v=1:a=0,"
        f"fps={FPS},format=yuv420p[vout]"
    )
    run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-i",
            fully_effected,
            "-filter_complex",
            filter_complex,
            "-map",
            "[vout]",
            "-map",
            "0:a:0",
            *_video_encoder_args(23, "veryfast"),
            "-c:a",
            "copy",
            "-t",
            f"{duration:.3f}",
            output_path,
        ]
    )
    return output_path


def run_full_spec_pipeline(
    core_vids,
    qual_vids,
    dedup_vids,
    dedup2_vids,
    mask_vids,
    dedup_requirement,
    dedup2_requirement,
):
    """Build the exact 60 + 90 + 60 second IT specification timeline."""
    material_errors = validate_full_spec_materials(
        core_vids,
        qual_vids,
        dedup_vids,
        dedup2_vids,
        mask_vids,
    )
    if STRICT_MATERIAL_COUNTS and material_errors:
        raise RuntimeError("完整规范模式素材不合格：" + "；".join(material_errors))

    removed_violations = []
    blocking_violations = []
    if FULL_COMPLIANCE_SCAN:
        for path, stage in [
            *[(path, "核心素材") for path in core_vids],
            *[(path, "蒙版素材") for path in mask_vids],
        ]:
            blocking_violations.extend(
                {**item, "stage": stage, "action": "manual_review_required"}
                for item in scan_media_compliance(path, max_samples=90)
                if item.get("type") != "price"
            )
    if blocking_violations:
        write_compliance_report("blocked_before_edit", blocking_violations)
        first = blocking_violations[0]
        raise RuntimeError(
            "合规扫描需要人工修正："
            f"{first['stage']}/{first['file']} 在 {first['time']:.1f}s "
            f"检测到 {first['type']}，共 {len(blocking_violations)} 处。"
        )

    print("2. 完整规范第 1 段：核心素材固定 0–60 秒，禁止转场...")
    core_normalized = os.path.join(TMP_DIR, "spec_core_normalized.mp4")
    normalize_video(core_vids[0], core_normalized, mute=False)
    core_subtitled = ensure_matching_subtitles(
        core_vids[0], core_normalized, "spec_core"
    )
    core_price_filtered = filter_price_clips(
        [
            Clip(
                core_subtitled,
                "spec_core",
                get_duration(core_subtitled),
                source_path=core_subtitled,
            )
        ],
        mute_spoken_price=True,
        stage_label="核心素材",
        cover_visible_price=True,
    )[0].path
    core_paths = _fit_paths_to_duration(
        [core_price_filtered],
        SPEC_CORE_SECONDS,
        prefix="spec_core",
        repeat=False,
        finish_last_clip=False,
    )
    core_plain = os.path.join(TMP_DIR, "spec_core_plain.mp4")
    concat_clips(
        core_paths,
        core_plain,
        SPEC_CORE_SECONDS,
        finish_last_clip=False,
    )
    core_section = os.path.join(TMP_DIR, "spec_core_styled.mp4")
    apply_spec_section_style(core_plain, core_section, "core")

    print("3. 完整规范第 2 段：优质素材轻切重排，固定 60–150 秒，主画面不加转场...")
    quality_clips = []
    for index, video in enumerate(qual_vids):
        normalized = os.path.join(TMP_DIR, f"spec_quality_norm_{index:03d}.mp4")
        normalize_video(video, normalized, mute=False)
        subtitled = ensure_matching_subtitles(
            video, normalized, f"spec_quality_{index:03d}"
        )
        quality_clips.extend(
            split_quality_video(subtitled, f"spec_quality_{index:03d}")
        )
    quality_clips, quality_violations = filter_compliance_clips(
        quality_clips, "优质素材"
    )
    removed_violations.extend(quality_violations)
    quality_clips = filter_price_clips(
        quality_clips,
        mute_spoken_price=True,
        stage_label="优质素材池",
        cover_visible_price=True,
    )
    quality_clips = staggered_shuffle_clips(quality_clips, "quality-60-150")
    quality_plain = os.path.join(TMP_DIR, "spec_quality_plain.mp4")
    concat_clips(
        [clip.path for clip in quality_clips],
        quality_plain,
        SPEC_QUALITY_SECONDS,
        finish_last_clip=False,
    )
    quality_section = os.path.join(TMP_DIR, "spec_quality_styled.mp4")
    apply_spec_section_style(quality_plain, quality_section, "quality")

    if DEDUP2_ENABLED:
        print("4. 完整规范第 3 段：去重1保留原声打乱，去重2反分析选镜后配音...")
    else:
        print("4. 完整规范第 3 段：去重素材池保留原声并连贯重组...")
    dedup_clips = []
    for index, video in enumerate(dedup_vids):
        normalized = os.path.join(TMP_DIR, f"spec_dedup_norm_{index:03d}.mp4")
        normalize_video(video, normalized, mute=False)
        subtitled = ensure_matching_subtitles(
            video, normalized, f"spec_dedup_{index:03d}"
        )
        dedup_clips.extend(
            split_dedup_video(
                subtitled,
                f"spec_dedup_{index:03d}",
                mute=False,
                coherent_audio=True,
            )
        )
    dedup_clips = filter_price_clips(
        dedup_clips,
        mute_spoken_price=True,
        stage_label="去重素材池",
        cover_visible_price=True,
    )
    dedup_clips, dedup_violations = filter_compliance_clips(
        dedup_clips, "去重素材池"
    )
    removed_violations.extend(dedup_violations)

    dedup2_clips = []
    if DEDUP2_ENABLED:
        for index, video in enumerate(dedup2_vids):
            dedup2_clips.extend(
                split_dedup_video(video, f"spec_dedup2_{index:03d}", mute=True)
            )
        dedup2_clips = select_useful_dedup2_clips(dedup2_clips)
        dedup2_clips = cover_visual_text_clips(
            dedup2_clips, stage_label="去重素材池2"
        )
        dedup2_clips, dedup2_violations = filter_compliance_clips(
            dedup2_clips, "去重素材池2"
        )
        removed_violations.extend(dedup2_violations)
        dedup2_clips = staggered_shuffle_clips(
            dedup2_clips, "dedup2-silent-voice-150-210"
        )

    dedup2_section_raw = None
    dedup2_section_duration = 0.0
    if dedup2_clips:
        dedup2_budget = min(
            SPEC_DEDUP_SECONDS / 2.0,
            sum(max(0.0, float(clip.duration or 0.0)) for clip in dedup2_clips),
        )
        dedup2_section_raw = build_dedup2_voice_section(
            dedup2_clips,
            dedup2_requirement,
            dedup2_budget,
            sync_mode=SYNC_MODE,
        )
        if dedup2_section_raw:
            dedup2_section_duration = min(
                SPEC_DEDUP_SECONDS / 2.0,
                get_duration(dedup2_section_raw),
            )

    dedup1_duration = max(0.5, SPEC_DEDUP_SECONDS - dedup2_section_duration)
    dedup_clips = weave_coherent_dedup1_blocks(
        dedup_clips, minimum_duration=dedup1_duration
    )
    dedup1_section_raw = os.path.join(TMP_DIR, "spec_dedup1_original_audio.mp4")
    render_dedup1_story_section(
        dedup_clips,
        dedup1_section_raw,
        max_duration=dedup1_duration,
    )
    dedup_section_raw = os.path.join(TMP_DIR, "spec_dedup_combined.mp4")
    dedup_section_inputs = [dedup1_section_raw]
    if dedup2_section_raw:
        dedup_section_inputs.append(dedup2_section_raw)
    concat_clips(
        dedup_section_inputs,
        dedup_section_raw,
        SPEC_DEDUP_SECONDS,
        finish_last_clip=False,
    )
    dedup_section = os.path.join(TMP_DIR, "spec_dedup_styled.mp4")
    apply_spec_section_style(dedup_section_raw, dedup_section, "dedup")

    print("5. 拼接固定 210 秒三段式主时间线...")
    base_concat = os.path.join(TMP_DIR, "spec_timeline_210s.mp4")
    concat_clips(
        [core_section, quality_section, dedup_section],
        base_concat,
        SPEC_TOTAL_SECONDS,
        finish_last_clip=False,
    )

    print("6. 60 秒后自动识别人像并挂载墨镜、变老特效...")
    effected = os.path.join(TMP_DIR, "spec_person_effects.mp4")
    apply_full_spec_person_effects(base_concat, effected)

    print("7. 分层画面处理：随机蒙版与四角爱心...")
    pending_output = f"{OUT_FILE}.partial.mp4"
    if os.path.exists(pending_output):
        os.remove(pending_output)
    final_render(
        effected,
        pending_output,
        render_duration=SPEC_TOTAL_SECONDS,
    )
    ensure_final_output_contract(pending_output)

    print("8. 全片合规扫描与音画导出校验...")
    final_violations = scan_media_compliance(
        pending_output,
        sample_seconds=COMPLIANCE_SAMPLE_SECONDS,
        max_samples=0,
    )
    if final_violations:
        all_violations = [*removed_violations, *final_violations]
        write_compliance_report("blocked_after_render", all_violations)
        os.remove(pending_output)
        first = final_violations[0]
        raise RuntimeError(
            "成片合规扫描未通过："
            f"{first['time']:.1f}s 检测到 {first['type']}，"
            f"请按 {os.path.basename(COMPLIANCE_REPORT_FILE)} 人工修正。"
        )

    final_video_duration = get_stream_duration(pending_output, "v:0")
    final_audio_duration = get_stream_duration(pending_output, "a:0")
    if (
        abs(final_video_duration - SPEC_TOTAL_SECONDS) > 0.15
        or abs(final_audio_duration - SPEC_TOTAL_SECONDS) > 0.15
        or abs(final_video_duration - final_audio_duration) > 0.15
    ):
        os.remove(pending_output)
        raise RuntimeError(
            "完整规范成片音画时长校验失败："
            f"视频 {final_video_duration:.3f}s / 音频 {final_audio_duration:.3f}s。"
        )

    os.replace(pending_output, OUT_FILE)
    report = write_compliance_report("passed", removed_violations)
    if DEDUP2_ENABLED and os.path.exists(PROMO_META_FILE):
        try:
            with open(PROMO_META_FILE, "r", encoding="utf-8") as handle:
                metadata = json.load(handle)
        except (OSError, json.JSONDecodeError):
            metadata = {}
        metadata.update(
            {
                "full_spec_mode": True,
                "timeline_sections": {
                    "core": [0, 60],
                    "quality": [60, 150],
                    "dedup": [150, 210],
                    "dedup1_original_audio": [150, round(210 - dedup2_section_duration, 3)],
                    "dedup2_ai_voice": [round(210 - dedup2_section_duration, 3), 210],
                },
                "compliance_status": report["status"],
                "compliance_removed_count": len(removed_violations),
                "final_video_duration": round(final_video_duration, 3),
                "final_audio_duration": round(final_audio_duration, 3),
                "output_fps": FPS,
            }
        )
        with open(PROMO_META_FILE, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False, indent=2)
    print(f"\n[Success] V17 完整规范模式完成: {OUT_FILE}")
    print(f"[Info] 合规扫描报告: {COMPLIANCE_REPORT_FILE}")
    return OUT_FILE


def load_fixed_stage_cache(core_duration, quality_duration):
    """Load this batch's protected core/quality render outputs when complete."""
    if not FIXED_STAGE_CACHE_DIR or not FIXED_STAGE_CACHE_KEY:
        return None
    cache_dir = os.path.abspath(FIXED_STAGE_CACHE_DIR)
    manifest_path = os.path.join(cache_dir, "manifest.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    if (
        payload.get("version") != 1
        or payload.get("key") != FIXED_STAGE_CACHE_KEY
        or int(payload.get("fps", 0)) != int(FPS)
        or abs(float(payload.get("target", 0.0)) - TARGET_TOTAL_DURATION) > 0.01
        or bool(payload.get("hardcut_mode", False)) != bool(HARDCUT_MODE)
        or bool(payload.get("mute_original_audio", False)) != bool(MUTE_ORIGINAL_AUDIO)
    ):
        return None

    def validated_paths(names, expected_duration):
        if not isinstance(names, list):
            return None
        paths = []
        for name in names:
            if not isinstance(name, str) or os.path.basename(name) != name:
                return None
            path = os.path.join(cache_dir, name)
            if not os.path.isfile(path) or get_duration(path) <= 0.05:
                return None
            paths.append(path)
        actual = sum(get_duration(path) for path in paths)
        if abs(actual - max(0.0, float(expected_duration))) > 0.20:
            return None
        return paths

    try:
        core_paths = validated_paths(payload.get("core"), core_duration)
        quality_paths = validated_paths(payload.get("quality"), quality_duration)
    except (OSError, TypeError, ValueError):
        return None
    if core_paths is None or quality_paths is None:
        return None
    return core_paths, quality_paths


def publish_fixed_stage_cache(core_paths, quality_paths):
    """Atomically publish protected clips for later variants in this batch."""
    if not FIXED_STAGE_CACHE_DIR or not FIXED_STAGE_CACHE_KEY:
        return
    cache_dir = os.path.abspath(FIXED_STAGE_CACHE_DIR)
    os.makedirs(cache_dir, exist_ok=True)

    def copy_group(paths, prefix):
        names = []
        for index, source in enumerate(paths):
            name = f"{prefix}_{index:03d}.mp4"
            destination = os.path.join(cache_dir, name)
            pending = f"{destination}.partial"
            try:
                shutil.copyfile(source, pending)
                os.replace(pending, destination)
            finally:
                if os.path.exists(pending):
                    os.remove(pending)
            names.append(name)
        return names

    core_names = copy_group(core_paths, "core")
    quality_names = copy_group(quality_paths, "quality")
    payload = {
        "version": 1,
        "key": FIXED_STAGE_CACHE_KEY,
        "fps": int(FPS),
        "target": round(TARGET_TOTAL_DURATION, 3),
        "hardcut_mode": bool(HARDCUT_MODE),
        "mute_original_audio": bool(MUTE_ORIGINAL_AUDIO),
        "core": core_names,
        "quality": quality_names,
    }
    manifest_path = os.path.join(cache_dir, "manifest.json")
    pending_manifest = f"{manifest_path}.partial"
    try:
        with open(pending_manifest, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(pending_manifest, manifest_path)
    finally:
        if os.path.exists(pending_manifest):
            os.remove(pending_manifest)


def main():
    global FPS, OUTPUT_FPS_INFO, TARGET_TOTAL_DURATION
    three_pool_active = THREE_POOL_MODE and not FULL_SPEC_MODE
    active_dedup2 = DEDUP2_ENABLED and not three_pool_active
    natural_boundary_fallback_used = False
    print("1. 启动 V17 功能优化增强版...")
    if _local_speed_profile_enabled():
        print("V16_LOCAL_RENDER_PROFILE local-8021-30fps-analysis-cache-v1；本机速度优先，公共入口不变。")
    print(
        f"   -> 参考成片时长 {TARGET_TOTAL_DURATION:.0f}s，"
        f"完整片段自然收尾最多顺延 {MAX_NATURAL_TAIL_SECONDS:.1f}s，"
        f"{'指定核心开场、其余核心完整穿插' if three_pool_active and FISSION_SHUFFLE_ALL and FISSION_VARIANT_SEED and FISSION_CORE_OPENER else '打乱轨开场、核心完整块穿插' if three_pool_active and FISSION_SHUFFLE_ALL and FISSION_VARIANT_SEED else '核心完整、打乱自然段可重排' if three_pool_active else '核心/优质完整块交错乱序、去重重组' if FISSION_SHUFFLE_ALL and FISSION_VARIANT_SEED else '核心完整、优质原序、去重可重排'}，"
        f"同步策略：{SYNC_MODE_LABEL}。"
    )
    ensure_dirs()
    clean_tmp()
    if active_dedup2:
        _clear_promo_artifacts()

    qual_req = "" if three_pool_active else read_text(QUAL_REQUIREMENT_FILE)
    dedup_req = read_text(
        SHUFFLE_REQUIREMENT_FILE if three_pool_active else DEDUP_REQUIREMENT_FILE
    )
    dedup2_req = read_text(DEDUP2_REQUIREMENT_FILE) if active_dedup2 else ""

    core_vids = get_files(CORE_DIR, (".mp4", ".mov"), "核心素材")
    if three_pool_active:
        qual_vids = []
        dedup_vids = get_files(
            SHUFFLE_DIR, (".mp4", ".mov"), "打乱素材池"
        )
        dedup2_vids = []
        # The normal three-pool path must never scan the legacy mask folder.
        mask_vids = []
    else:
        qual_vids = get_files(QUAL_DIR, (".mp4", ".mov"), "优质素材池")
        dedup_vids = get_files(DEDUP_DIR, (".mp4", ".mov"), "去重素材池")
        dedup2_vids = (
            get_files(DEDUP2_DIR, (".mp4", ".mov"), "去重素材池2")
            if active_dedup2
            else []
        )
        mask_vids = get_files(MASK_DIR, (".mp4", ".mov"), "蒙版素材")

    if three_pool_active:
        core_durations = _validated_pool_video_durations(
            core_vids, CORE_DIR, "核心素材"
        )
        shuffle_durations = _validated_pool_video_durations(
            dedup_vids, SHUFFLE_DIR, "打乱素材"
        )
        source_core_duration = sum(core_durations)
        source_quality_duration = 0.0
        source_dedup_duration = sum(shuffle_durations)
    else:
        source_core_duration = sum(get_duration(path) for path in core_vids)
        source_quality_duration = sum(get_duration(path) for path in qual_vids)
        source_dedup_duration = sum(get_duration(path) for path in dedup_vids)
    automatic_mask_source = None
    if not FULL_SPEC_MODE:
        if three_pool_active:
            timeline_budgets = allocate_three_pool_budgets(
                TARGET_TOTAL_DURATION,
                source_core_duration,
                source_dedup_duration,
            )
            quality_budget = 0.0
            dedup_budget = timeline_budgets["shuffle"]
            automatic_mask_source = select_shuffle_mask_source(dedup_vids)
            print(
                f"   -> 三池时长契约已二次校验：C_raw={source_core_duration:.3f}s，"
                f"C_eff={timeline_budgets['core_effective']:.3f}s，"
                f"核心预算={timeline_budgets['core_budget']:.3f}s，"
                f"K={timeline_budgets['multiplier']:.3f}，T={TARGET_TOTAL_DURATION:.3f}s，"
                f"S={source_dedup_duration:.3f}s（要求≥{timeline_budgets['required_shuffle']:.3f}s）；"
                f"计划打乱轨预算={dedup_budget:.3f}s。"
            )
            print(
                "   -> 多源自然窗口采用先规划、后编码；真实文件结尾每源仅允许 "
                f"额外 ≤{SHUFFLE_SOURCE_EOF_PLANNING_GRACE_SECONDS:.1f}s "
                "选材库存宽限。普通候选最终自然尾段上限不变；最后显式安全兜底"
                "仍沿用既有额外上限。"
            )
        else:
            timeline_budgets = allocate_timeline_budgets(
                TARGET_TOTAL_DURATION,
                source_core_duration,
                source_quality_duration,
                source_dedup_duration,
            )
            quality_budget = _budget_override(
                "V16_QUALITY_BUDGET_SECONDS", timeline_budgets["quality"]
            )
            dedup_budget = _budget_override(
                "V16_DEDUP_BUDGET_SECONDS", timeline_budgets["dedup"]
            )
            if (
                quality_budget > source_quality_duration + 0.05
                or dedup_budget > source_dedup_duration + 0.05
                or abs(
                    source_core_duration + quality_budget + dedup_budget
                    - TARGET_TOTAL_DURATION
                ) > 0.10
            ):
                raise RuntimeError("页面下发的参考时长预算无效，已停止以保护素材完整性。")
        protected_material_duration = source_core_duration + quality_budget

    FPS, OUTPUT_FPS_INFO = _resolve_output_fps(
        [*core_vids, *qual_vids, *dedup_vids, *dedup2_vids]
    )
    if OUTPUT_FPS_INFO["mode"] == "auto":
        print(
            f"   -> 自动帧率：检测 {OUTPUT_FPS_INFO['sample_count']} 个视频，"
            f"真实高帧率素材时长占比 {OUTPUT_FPS_INFO['high_fps_ratio']:.1%}；"
            f"阈值 {AUTO_FPS_60_THRESHOLD:.0%}，本次恒定输出 {FPS}fps。"
        )
    else:
        print(f"   -> 固定帧率：本次恒定输出 {FPS}fps。")

    if not core_vids:
        raise RuntimeError("缺少核心素材，请先放 mp4/mov 到核心素材。")

    if FULL_SPEC_MODE:
        print(
            "   -> 已启用《剪辑要求》完整规范模式：固定 210 秒，"
            "严格执行 0–60 / 60–150 / 150–210 三段式时间线。"
        )
        return run_full_spec_pipeline(
            core_vids,
            qual_vids,
            dedup_vids,
            dedup2_vids,
            mask_vids,
            dedup_req,
            dedup2_req,
        )

    if three_pool_active and PRESERVE_RAW_CORE:
        TARGET_TOTAL_DURATION = float(timeline_budgets["target"])
        actual_core_duration = sum(
            max(0.05, get_duration(c_path)) for c_path in core_vids
        )
        max_core_audit = max(0, 120 - len(SOURCE_WINDOW_AUDIT))
        for c_path in core_vids[:max_core_audit]:
            c_dur = max(0.05, get_duration(c_path))
            SOURCE_WINDOW_AUDIT.append(
                {
                    "role": "core",
                    "source": os.path.basename(c_path),
                    "start_seconds": 0.0,
                    "end_seconds": round(c_dur, 3),
                    "duration_seconds": round(c_dur, 3),
                    "window_index": 1,
                    "boundary": "source-start+source-end",
                }
            )
        source_core_duration = actual_core_duration
        protected_material_duration = actual_core_duration
        dedup_budget = max(0.15, float(timeline_budgets["shuffle"]))
        if TARGET_TOTAL_DURATION < actual_core_duration + dedup_budget - 0.05:
            TARGET_TOTAL_DURATION = actual_core_duration + dedup_budget
        print(
            f"   -> 核心原片保留：实测 {actual_core_duration:.3f}s（100% 原时长不裁剪）；"
            f"打乱轨独立渲染目标 {dedup_budget:.3f}s。"
        )
        print(
            "2. 核心素材已设为 100% 保留原视频（不裁剪、不加蒙版/贴纸、保留原声，将在打乱段混剪混音后无损前置）..."
        )
        timeline = []
        core_timeline = []
        quality_timeline = []
    else:
        if three_pool_active:
            if HARDCUT_DUB_MODE:
                TARGET_TOTAL_DURATION = float(timeline_budgets["target"])
            planned_core_budget = timeline_budgets["core_budget"]
            core_vids, actual_core_duration = fit_core_paths_to_budget(
                core_vids,
                planned_core_budget,
                window_index=CORE_WINDOW_INDEX,
            )
            min_shuffle_room = 0.05 if HARDCUT_DUB_MODE else 0.50
            if actual_core_duration >= TARGET_TOTAL_DURATION - min_shuffle_room:
                if HARDCUT_DUB_MODE:
                    TARGET_TOTAL_DURATION = actual_core_duration + max(
                        0.15, float(timeline_budgets["shuffle"])
                    )
                else:
                    raise NaturalBoundaryError(
                        "核心自然窗口占满参考时长，无法保留可校验的打乱轨道。"
                    )
            source_core_duration = actual_core_duration
            dedup_budget = TARGET_TOTAL_DURATION - actual_core_duration
            protected_material_duration = actual_core_duration
            print(
                f"   -> 核心窗口：计划≤{planned_core_budget:.3f}s，"
                f"实际 {actual_core_duration:.3f}s；打乱轨目标 "
                f"{dedup_budget:.3f}s。"
            )

        timeline = []
        cached_fixed = load_fixed_stage_cache(source_core_duration, quality_budget)
        if cached_fixed:
            core_timeline, quality_timeline = cached_fixed
            if three_pool_active:
                print(
                    "2. 已命中本批核心段高速缓存：跳过重复归一化、"
                    "字幕和价格分析；本条仅重编打乱轨与穿插清单。"
                )
            else:
                print(
                    "2–3. 已命中本批固定段高速缓存：核心与优质预处理片段直接复用，"
                    "跳过重复归一化、字幕和价格分析；本条顺序仍重新编排。"
                )
        else:
            print(
                (
                    "2. 核心素材整段保留并取消原声；稍后穿插进打乱硬切轨..."
                    if MUTE_ORIGINAL_AUDIO
                    else "2. 核心素材整段保留并保留原声；稍后穿插进打乱硬切轨..."
                )
                if HARDCUT_MODE
                else (
                    "2. 核心素材完整处理；裂变模式稍后将完整处理块穿插进打乱轨..."
                    if three_pool_active
                    else "2. 核心素材完整处理；裂变模式会将完整处理块与优质块交错乱序..."
                )
            )
            core_timeline = []
            for i, video in enumerate(core_vids):
                core_norm = os.path.join(TMP_DIR, f"core_norm_{i:03d}.mp4")
                normalize_video(video, core_norm, mute=MUTE_ORIGINAL_AUDIO)
                if HARDCUT_MODE:
                    core_processed = (
                        core_norm
                        if MUTE_ORIGINAL_AUDIO
                        else ensure_matching_subtitles(video, core_norm, f"core_{i:03d}")
                    )
                    core_timeline.append(core_processed)
                else:
                    core_subtitled = (
                        core_norm
                        if MUTE_ORIGINAL_AUDIO
                        else ensure_matching_subtitles(video, core_norm, f"core_{i:03d}")
                    )
                    core_timeline.extend(
                        split_core_video(core_subtitled, f"core_{i:03d}")
                    )
            core_timeline = filter_price_paths(core_timeline, stage_label="核心素材")

            quality_timeline = []
            if not three_pool_active:
                print(
                    f"3. 优质素材池：按固定预算取用 {quality_budget:.1f}s，"
                    "检测并补齐字幕；裂变模式随后按完整处理块交错乱序..."
                )
                quality_inputs = _fit_paths_to_duration(
                    qual_vids,
                    quality_budget,
                    prefix="quality_budget",
                    repeat=False,
                    finish_last_clip=False,
                )
                for i, video in enumerate(quality_inputs):
                    out = os.path.join(TMP_DIR, f"qual_full_{i:03d}.mp4")
                    normalize_video(video, out, mute=MUTE_ORIGINAL_AUDIO)
                    quality_timeline.append(
                        out
                        if MUTE_ORIGINAL_AUDIO
                        else ensure_matching_subtitles(video, out, f"qual_{i:03d}")
                    )
                quality_timeline = filter_price_paths(
                    quality_timeline, stage_label="优质素材池"
                )
            publish_fixed_stage_cache(core_timeline, quality_timeline)
        if three_pool_active:
            processed_core_duration = sum(
                max(0.0, get_duration(path)) for path in core_timeline
            )
            if processed_core_duration <= 0.05:
                raise RuntimeError("核心自然窗口在处理后没有可用画面。")
            min_shuffle_room = 0.05 if HARDCUT_MODE else 0.50
            if HARDCUT_MODE and processed_core_duration >= TARGET_TOTAL_DURATION - min_shuffle_room:
                TARGET_TOTAL_DURATION = processed_core_duration + max(
                    0.15, float(timeline_budgets["shuffle"])
                )
            if processed_core_duration >= TARGET_TOTAL_DURATION - min_shuffle_room:
                raise NaturalBoundaryError(
                    "处理后的核心窗口占满参考时长，无法保留打乱轨道。"
                )
            source_core_duration = processed_core_duration
            protected_material_duration = processed_core_duration
            dedup_budget = TARGET_TOTAL_DURATION - processed_core_duration
            print(
                f"   -> 核心处理后实测 {processed_core_duration:.3f}s；"
                f"本条打乱轨参考预算已回算为 {dedup_budget:.3f}s。"
            )
        if FISSION_SHUFFLE_ALL and FISSION_VARIANT_SEED:
            print(
                (
                    f"   -> 核心保留为 {len(core_timeline)} 个完整处理块；稍后穿插进打乱轨。"
                    if HARDCUT_MODE
                    else f"   -> 核心保留为 {len(core_timeline)} 个完整处理块；稍后穿插进打乱轨，避免硬切半句话或动作。"
                )
                if three_pool_active
                else f"   -> 核心保留为 {len(core_timeline)} 个完整处理块；稍后与优质完整块"
                "共同编排，避免硬切半句话或动作。"
            )
        else:
            timeline.extend(core_timeline)

    print(
        (
            f"3. 打乱素材：{'取消原声' if MUTE_ORIGINAL_AUDIO else '保留原声'}，"
            f"按 {FIXED_SLICE_SECONDS:g} 秒一刀直接硬切，单条内绝不重复、自动压最低相似度拼接..."
        )
        if HARDCUT_MODE
        else (
            f"3. 打乱素材：{'取消原声' if MUTE_ORIGINAL_AUDIO else '保留原声'}，按完整话术/动作切分，"
            "经价格、风格和指纹链后连贯重组..."
            if three_pool_active
            else f"4. 去重素材池：{'取消原声' if MUTE_ORIGINAL_AUDIO else '保留原声'}，按完整话术/动作切分并连贯重组..."
        )
    )
    dedup_chunks = []
    dedup_source_window = (
        min(source_dedup_duration, dedup_budget)
        if dedup_budget > 0.01
        else 0.0
    )
    dedup_inputs = []
    if HARDCUT_MODE:
        if dedup_budget > 0.01:
            dedup_inputs = list(dedup_vids)
            max_audit_sources = max(0, 120 - len(SOURCE_WINDOW_AUDIT))
            for v_path in dedup_inputs[:max_audit_sources]:
                v_dur = max(0.05, get_duration(v_path))
                SOURCE_WINDOW_AUDIT.append(
                    {
                        "role": "shuffle",
                        "source": os.path.basename(v_path),
                        "start_seconds": 0.0,
                        "end_seconds": round(v_dur, 3),
                        "duration_seconds": round(v_dur, 3),
                        "window_index": max(1, int(SHUFFLE_WINDOW_INDEX or 1)),
                        "boundary": "source-start+source-end",
                    }
                )
    elif dedup_source_window > 0.01:
        try:
            if three_pool_active:
                dedup_inputs = fit_shuffle_paths_to_budget(
                    dedup_vids,
                    dedup_source_window,
                    window_index=SHUFFLE_WINDOW_INDEX,
                    max_extension=(
                        MAX_NATURAL_TAIL_SECONDS
                        + (
                            NATURAL_BOUNDARY_FALLBACK_EXTRA_SECONDS
                            if ALLOW_NATURAL_BOUNDARY_FALLBACK
                            else 0.0
                        )
                    ),
                    require_natural_boundary=True,
                    allow_complete_fallback=ALLOW_NATURAL_BOUNDARY_FALLBACK,
                )
            else:
                dedup_inputs = _fit_paths_to_duration(
                    dedup_vids,
                    dedup_source_window,
                    prefix="dedup_source_budget",
                    repeat=False,
                    finish_last_clip=True,
                    require_natural_boundary=True,
                    max_extension=MAX_NATURAL_TAIL_SECONDS,
                    audit_role="timeline",
                )
        except NaturalBoundaryError as exc:
            raise NaturalBoundaryError(
                f"有界自然时间窗选择失败：{exc}"
            ) from exc
    dedup_chunks.extend(
        split_dedup_sources_parallel(
            dedup_inputs,
            "dedup",
            mute=MUTE_ORIGINAL_AUDIO,
            coherent_audio=not HARDCUT_MODE,
            stage_label="打乱素材" if three_pool_active else "去重素材池",
        )
    )
    if not HARDCUT_MODE:
        dedup_chunks = order_dedup_clips(dedup_chunks, dedup_req)
    dedup_chunks = filter_price_clips(
        dedup_chunks,
        mute_spoken_price=not MUTE_ORIGINAL_AUDIO,
        stage_label="打乱素材" if three_pool_active else "去重素材池",
        cover_visible_price=True,
    )
    dedup2_chunks = []
    if active_dedup2:
        print("6. 去重素材池2：去原声，反分析提取有用镜头、打乱后再配音...")
        dedup2_chunks.extend(
            split_dedup_sources_parallel(
                dedup2_vids,
                "dedup2",
                mute=True,
                stage_label="去重素材池2",
            )
        )
        dedup2_chunks = order_dedup_clips(dedup2_chunks, dedup2_req)
        dedup2_chunks = select_useful_dedup2_clips(
            dedup2_chunks,
            minimum_duration=DEDUP_SECTION_MIN_SECONDS,
        )
        dedup2_chunks = cover_visual_text_clips(dedup2_chunks, stage_label="去重素材池2")
        dedup2_chunks = staggered_shuffle_clips(
            dedup2_chunks, "dedup2-silent-before-voice"
        )
    else:
        print("6. 当前部门流程不包含额外素材阶段。")

    dedup1_target = dedup_budget
    dedup2_target = dedup_section_duration_target(dedup2_chunks)
    planned_dedup_duration = 0.0
    if HARDCUT_DUB_MODE:
        dedup_chunks = hardcut_audio.select_non_repeating_slices(
            dedup_chunks,
            dedup1_target,
            variant_seed=(
                f"{FISSION_VARIANT_SEED}:plan={FISSION_MATERIAL_PLAN_INDEX}"
                if FISSION_VARIANT_SEED
                else f"single:{os.getpid()}"
            ),
            ledger_path=BATCH_USED_SLICES_FILE or None,
        )
        if not dedup_chunks and dedup1_target > 0.01:
            raise RuntimeError("打乱素材硬切后没有可用切片，请检查上传的打乱视频是否有效。")
        dedup_story_cut_times = []
        acc_cut = 0.0
        for c in dedup_chunks[:-1]:
            c_dur = max(0.0, timeline_video_duration(c.path, "硬切排片"))
            if c_dur > 0.01:
                acc_cut += c_dur
                dedup_story_cut_times.append(round(acc_cut, 3))
        planned_dedup_duration = sum(
            max(0.0, timeline_video_duration(c.path, "硬切排片"))
            for c in dedup_chunks
        )
        print(
            f"   -> 硬切无重复排片完成：共选 {len(dedup_chunks)} 刀（单条内 0 重复），"
            f"打乱轨实测合计 {planned_dedup_duration:.2f}s。"
        )
    else:
        dedup_chunks = weave_coherent_dedup1_blocks(
            dedup_chunks,
            minimum_duration=min(
                dedup1_target + MAX_NATURAL_TAIL_SECONDS,
                source_dedup_duration,
            )
            if three_pool_active
            else dedup1_target + MAX_NATURAL_TAIL_SECONDS,
        )
        if three_pool_active:
            dedup_chunks = expand_shuffle_story_blocks(
                dedup_chunks,
                dedup1_target + MAX_NATURAL_TAIL_SECONDS,
            )
        if dedup1_target > 0.01:
            try:
                planned_dedup_duration = plan_complete_story_duration(
                    dedup_chunks,
                    dedup1_target,
                    max_extension=MAX_NATURAL_TAIL_SECONDS,
                )
            except NaturalBoundaryError as exc:
                if not ALLOW_NATURAL_BOUNDARY_FALLBACK:
                    raise
                original_block_count = len(dedup_chunks)
                (
                    dedup_chunks,
                    planned_dedup_duration,
                    used_extra_allowance,
                ) = plan_complete_story_fallback(
                    dedup_chunks,
                    dedup1_target,
                    max_extension=MAX_NATURAL_TAIL_SECONDS,
                    fallback_extra=NATURAL_BOUNDARY_FALLBACK_EXTRA_SECONDS,
                )
                natural_boundary_fallback_used = used_extra_allowance
                extension = max(0.0, planned_dedup_duration - dedup1_target)
                allowance_note = (
                    f"已使用安全自然边界容错顺延（总顺延 {extension:.1f} 秒）"
                    if used_extra_allowance
                    else f"仍在原 {MAX_NATURAL_TAIL_SECONDS:.1f} 秒自然顺延内"
                )
                print(
                    f"[NaturalBoundaryFallback] {exc} "
                    f"已从 {original_block_count} 个完整块中按原相对顺序确定 "
                    f"{len(dedup_chunks)} 个，最接近的安全结束点为 "
                    f"{planned_dedup_duration:.1f} 秒；{allowance_note}，未硬切半句话或动作。"
                )
        dedup_story_cut_times = dedup_story_interleave_split_times(
            dedup_chunks,
            planned_dedup_duration,
        )
    dedup1_section = os.path.join(TMP_DIR, "dedup1_story_remix.mp4")
    rendered_dedup1 = None
    if dedup1_target > 0.01:
        rendered_dedup1 = render_dedup1_story_section(
            dedup_chunks,
            dedup1_section,
            max_duration=planned_dedup_duration,
        )
        if not rendered_dedup1:
            raise RuntimeError(
                f"{'打乱素材' if three_pool_active else '去重素材'}"
                "无法在参考时长附近生成完整的可变片段。"
            )
    dedup1_timeline = [rendered_dedup1] if rendered_dedup1 else []
    dedup1_actual_duration = (
        get_duration(rendered_dedup1) if rendered_dedup1 else 0.0
    )
    if rendered_dedup1:
        hash_payload = write_dedup_hash_file(rendered_dedup1)
        if hash_payload:
            early_similarity = early_dedup_similarity_percent(hash_payload["hashes"])
            if HARDCUT_DUB_MODE:
                print(
                    f"   -> 本条打乱轨与前序成片画面相似度：{early_similarity:.2f}%（已解除 40% 限制，按最低相似度自由拼接）。"
                )
            elif early_similarity > FISSION_SIMILARITY_LIMIT:
                print(
                    f"[FissionRetry] {'打乱轨道' if three_pool_active else '去重轨道'}"
                    f"相似度 {early_similarity:.2f}% > "
                    f"{FISSION_SIMILARITY_LIMIT:.0f}%，已在最终拼接前快速停止本候选。"
                )
                raise SystemExit(42)

    interleave_piece_seconds = max(
        8.0,
        min(12.0, max(quality_budget, dedup_budget, 8.0) / 3.0),
    )
    dedup_interleave_paths = []
    for index, path in enumerate(dedup1_timeline):
        dedup_interleave_paths.extend(
            split_normalized_path_for_interleave(
                path,
                f"dedup_interleave_{index:03d}",
                piece_seconds=interleave_piece_seconds,
                preferred_split_times=dedup_story_cut_times,
            )
        )
    if three_pool_active and PRESERVE_RAW_CORE:
        quality_interleave_paths = []
        if not dedup_interleave_paths:
            raise RuntimeError("打乱轨道未生成有效可变片段，已停止输出。")
        mixed_tail = list(dedup_interleave_paths)
        timeline.extend(mixed_tail)
        print(
            f"   -> 打乱轨独立编排完成：共 {len(dedup_interleave_paths)} 段保持指纹顺序；"
            "核心原视频不参与穿插与去重加工（稍后原画原声完整前置）。"
        )
    else:
        if FISSION_SHUFFLE_ALL and FISSION_VARIANT_SEED:
            if three_pool_active and FISSION_CORE_OPENER:
                quality_interleave_paths = [*core_timeline, *quality_timeline]
                opener_name = os.path.basename(quality_interleave_paths[0])
                print(
                    f"   -> 核心 {len(quality_interleave_paths)} 个完整块保持本条分配顺序；"
                    f"指定核心开场块 {opener_name}，其余核心仍完整保留。"
                )
            else:
                quality_interleave_paths = shuffle_fission_paths(
                    [*core_timeline, *quality_timeline],
                    "core_anchor_mix" if three_pool_active else "protected_anchor_mix",
                )
            if three_pool_active and not FISSION_CORE_OPENER:
                print(
                    f"   -> 核心 {len(quality_interleave_paths)} 个完整块已按本条种子编排；"
                    "开场位保留给打乱轨首个自然段。"
                )
            elif not three_pool_active:
                opener_name = os.path.basename(quality_interleave_paths[0])
                print(
                    f"   -> 核心/优质共 {len(quality_interleave_paths)} 个完整块已按本条方案"
                    f"交错乱序；本条开场块 {opener_name}，不新增编码。"
                )
        else:
            quality_interleave_paths = []
            for index, path in enumerate(quality_timeline):
                quality_interleave_paths.extend(
                    split_normalized_path_for_interleave(
                        path,
                        f"quality_interleave_{index:03d}",
                        piece_seconds=interleave_piece_seconds,
                    )
                )
        if three_pool_active and FISSION_SHUFFLE_ALL and FISSION_VARIANT_SEED:
            mixed_tail = interleave_core_into_ordered_shuffle(
                quality_interleave_paths,
                dedup_interleave_paths,
                core_first=FISSION_CORE_OPENER,
            )
        else:
            mixed_tail = interleave_ordered_quality_and_dedup(
                quality_interleave_paths,
                dedup_interleave_paths,
                seed=FISSION_VARIANT_SEED,
            )
        if three_pool_active and FISSION_SHUFFLE_ALL and FISSION_VARIANT_SEED:
            core_path_set = set(quality_interleave_paths)
            shuffle_path_set = set(dedup_interleave_paths)
            observed_core_order = [
                path for path in mixed_tail if path in core_path_set
            ]
            observed_shuffle_order = [
                path for path in mixed_tail if path in shuffle_path_set
            ]
            expected_opener = (
                quality_interleave_paths[0]
                if FISSION_CORE_OPENER
                else dedup_interleave_paths[0]
            )
            if (
                not mixed_tail
                or not dedup_interleave_paths
                or mixed_tail[0] != expected_opener
                or observed_core_order != quality_interleave_paths
                or observed_shuffle_order != dedup_interleave_paths
            ):
                raise RuntimeError(
                    "核心开场或核心/打乱轨相对顺序校验失败，已阻止候选输出。"
                )
        timeline.extend(mixed_tail)
        was_interleaved = mixed_tail != [
            *quality_interleave_paths,
            *dedup_interleave_paths,
        ]
        if quality_interleave_paths and dedup_interleave_paths and was_interleaved:
            if three_pool_active and FISSION_SHUFFLE_ALL and FISSION_VARIANT_SEED:
                print(
                    f"   -> 已均匀混剪：打乱段 {len(dedup_interleave_paths)} 个保持"
                    f"指纹相对顺序，核心完整块 {len(quality_interleave_paths)} 个穿插其中。"
                )
            elif FISSION_SHUFFLE_ALL and FISSION_VARIANT_SEED:
                print(
                    f"   -> 已均匀混剪：核心/优质完整块 {len(quality_interleave_paths)} 个，"
                    f"去重自然段 {len(dedup_interleave_paths)} 个；各轨道内部内容不丢失，"
                    "仅调整拼接清单。"
                )
            else:
                print(
                    f"   -> 核心之后已高速穿插：优质 {len(quality_interleave_paths)} 段保持原序，"
                    f"去重 {len(dedup_interleave_paths)} 段按方案插入；拆分使用流复制，不重复编码。"
                )
        elif quality_interleave_paths and dedup_interleave_paths:
            if three_pool_active and FISSION_SHUFFLE_ALL and FISSION_VARIANT_SEED:
                print(
                    "打乱轨单段后接/穿插完整核心块。"
                )
            elif FISSION_SHUFFLE_ALL and FISSION_VARIANT_SEED:
                print("   -> 核心/优质完整块已乱序；本条去重段接在尾部，继续保持去重指纹顺序一致。")
            else:
                print("   -> 本条未找到安全句尾穿插点，保持优质原序后接去重，避免切断话术。")
    if FAST_DEDUP_STYLE != "clean":
        print(
            f"   -> {'打乱轨' if three_pool_active else '去重专属'}快速风格: "
            f"{FAST_DEDUP_STYLE}（已并入镜头编码，"
            f"不处理{'核心' if three_pool_active else '核心/优质'}）。"
        )

    dedup2_section = None
    if dedup2_chunks:
        available_voice_duration = sum(
            max(0.0, get_duration(clip.path) or float(clip.duration or 0.0))
            for clip in dedup2_chunks
        )
        voice_budget = dedup2_target
        preserved_pre_voice_duration = sum(
            max(0.0, get_duration(path)) for path in timeline
        )
        print(
            f"   -> 去重池独立分配：去重1实际 {dedup1_actual_duration:.1f}s，"
            f"去重2目标 {voice_budget:.1f}s；各自在40–90秒内按有效素材决定，不强制等长。"
        )
        print(
            f"   -> 参考时长不截断必保留素材：核心、优质、去重1共 "
            f"{preserved_pre_voice_duration:.1f}s 完整保留；去重2可用画面 "
            f"{available_voice_duration:.1f}s。"
        )
        dedup2_section = build_dedup2_voice_section(
            dedup2_chunks,
            dedup2_req,
            voice_budget,
            sync_mode=SYNC_MODE,
        )
        if dedup2_section:
            timeline.append(dedup2_section)
    elif active_dedup2:
        _write_promo_skip_status(
            "本次未生成：去重素材池2没有可用视频素材。",
            "去重素材池2无素材",
        )

    effective_natural_tail_limit = MAX_NATURAL_TAIL_SECONDS + (
        NATURAL_BOUNDARY_FALLBACK_EXTRA_SECONDS
        if natural_boundary_fallback_used
        else 0.0
    )
    active_render_reference = (
        max(0.5, float(dedup_budget) + (float(dedup2_target) if dedup2_section else 0.0))
        if (three_pool_active and PRESERVE_RAW_CORE)
        else TARGET_TOTAL_DURATION
    )
    print(
        f"7. 拼接{'打乱混剪' if (three_pool_active and PRESERVE_RAW_CORE) else '成片'}时间线："
        f"参考 {active_render_reference:.1f} 秒..."
    )
    base_concat = os.path.join(TMP_DIR, "base_concat.mp4")
    planned_timeline_duration = sum(get_duration(path) for path in timeline)
    if three_pool_active and PRESERVE_RAW_CORE:
        if HARDCUT_DUB_MODE:
            minimum_allowed_duration = max(
                0.3,
                min(active_render_reference, dedup1_actual_duration) - 0.35,
            )
            maximum_allowed_duration = (
                max(active_render_reference, planned_timeline_duration)
                + max(effective_natural_tail_limit, FIXED_SLICE_SECONDS + 2.5)
                + 0.5
            )
        else:
            minimum_allowed_duration = max(0.3, active_render_reference - 0.25)
            maximum_allowed_duration = (
                active_render_reference + effective_natural_tail_limit + 0.35
            )
    elif HARDCUT_DUB_MODE:
        minimum_allowed_duration = max(
            0.5,
            protected_material_duration + min(0.2, dedup1_actual_duration) - 0.35,
        )
        maximum_allowed_duration = (
            max(active_render_reference, planned_timeline_duration)
            + max(effective_natural_tail_limit, FIXED_SLICE_SECONDS + 2.5)
            + 0.5
        )
    else:
        minimum_allowed_duration = active_render_reference - 0.15
        maximum_allowed_duration = (
            active_render_reference + effective_natural_tail_limit + 0.15
        )
    if not minimum_allowed_duration <= planned_timeline_duration <= maximum_allowed_duration:
        raise RuntimeError(
            "自然收尾时间线超出允许范围："
            f"参考 {active_render_reference:.3f}s，计划 {planned_timeline_duration:.3f}s，"
            f"允许最多顺延 {effective_natural_tail_limit:.3f}s。"
        )
    natural_timeline_duration = concat_clips(
        timeline,
        base_concat,
        active_render_reference,
        finish_last_clip=True,
        repeat=False,
        preserve_all=True,
    )
    if not minimum_allowed_duration <= natural_timeline_duration <= maximum_allowed_duration:
        raise RuntimeError(
            "自然收尾拼接失败："
            f"参考 {active_render_reference:.3f}s，实际 {natural_timeline_duration:.3f}s，"
            f"允许最多顺延 {effective_natural_tail_limit:.3f}s。"
        )
    if three_pool_active and PRESERVE_RAW_CORE:
        print(
            f"   -> 核心原视频 {protected_material_duration:.2f}s 已独立保护；"
            f"打乱混剪段实际 {natural_timeline_duration:.2f}s（相对顺序与指纹一致）。"
        )
    elif three_pool_active:
        print(
            f"   -> 核心完整块 {protected_material_duration:.2f}s（唯一排除40%指标）；"
            f"打乱轨 {dedup1_actual_duration:.2f}s 已穿插且相对顺序与指纹一致；"
            f"自然收尾成片 {natural_timeline_duration:.2f}s。"
        )
    else:
        print(
            f"   -> 参考用量合计 {protected_material_duration:.2f}s（核心+优质，"
            f"{'完整块已交错乱序' if FISSION_SHUFFLE_ALL and FISSION_VARIANT_SEED else '保持既定顺序'}）；"
            f"去重镜头 {dedup1_actual_duration:.2f}s 已穿插；自然收尾成片 "
            f"{natural_timeline_duration:.2f}s。"
        )

    print("8. 可选人脸特效与最终压制...")
    effected = os.path.join(TMP_DIR, "face_effect.mp4")
    apply_face_effect_chain(base_concat, effected)
    render_duration = get_duration(effected) or natural_timeline_duration
    if abs(render_duration - natural_timeline_duration) > 0.15:
        raise RuntimeError(
            "特效处理破坏自然收尾时长："
            f"处理前 {natural_timeline_duration:.3f}s，处理后 {render_duration:.3f}s。"
        )
    pending_output = f"{OUT_FILE}.partial.mp4"
    if os.path.exists(pending_output):
        os.remove(pending_output)
    try:
        final_render(
            effected,
            pending_output,
            render_duration=render_duration,
            mask_source=automatic_mask_source,
        )
        ensure_final_output_contract(pending_output)

        final_container_duration = get_duration(pending_output)
        final_video_duration = get_stream_duration(pending_output, "v:0")
        final_audio_duration = get_stream_duration(pending_output, "a:0")
        duration_error = max(
            abs(final_video_duration - render_duration),
            abs(final_audio_duration - render_duration),
        )
        av_duration_delta = abs(final_video_duration - final_audio_duration)
        av_sync_verified = (
            final_video_duration > 0
            and final_audio_duration > 0
            and duration_error <= 0.15
            and av_duration_delta <= 0.15
        )
        if not av_sync_verified:
            raise RuntimeError(
                "音画时长校验失败："
                f"视频 {final_video_duration:.3f}s，音频 {final_audio_duration:.3f}s，"
                f"自然收尾目标 {render_duration:.3f}s。"
            )

        # Attest the clean-only style invariant and the final output contract
        # before publishing. Source windows remain path-free reporting evidence;
        # they are not used to reject legitimately gray or white source footage.
        visual_quality_report = require_final_visual_quality(pending_output)

        # 只有新成品的视频流、音频流和时长全部校验通过后，才原子替换旧成品。
        # 生成中断、音画或画质校验失败都不会碰原素材，也不会损坏
        # 上一次正常导出的文件。
        os.replace(pending_output, OUT_FILE)
    except Exception:
        if os.path.exists(pending_output):
            try:
                os.remove(pending_output)
            except OSError:
                pass
        raise
    if DEDUP2_ENABLED:
        try:
            metadata = {}
            if os.path.exists(PROMO_META_FILE):
                with open(PROMO_META_FILE, "r", encoding="utf-8") as handle:
                    metadata = json.load(handle)
            metadata.update(
                {
                    "target_total_duration": round(TARGET_TOTAL_DURATION, 3),
                    "requested_reference_duration": round(TARGET_TOTAL_DURATION, 3),
                    "reference_target_is_hard_cap": False,
                    "preserved_pool_durations": {
                        "core": round(sum(get_duration(path) for path in core_timeline), 3),
                        "quality": round(sum(get_duration(path) for path in quality_timeline), 3),
                        "dedup1": round(sum(get_duration(path) for path in dedup1_timeline), 3),
                        "dedup2_voice": round(get_duration(dedup2_section), 3)
                        if dedup2_section else 0.0,
                    },
                    "natural_timeline_duration": round(render_duration, 3),
                    "natural_tail_limit": round(effective_natural_tail_limit, 3),
                    "natural_boundary_fallback_used": natural_boundary_fallback_used,
                    "natural_tail_extension": round(
                        max(0.0, render_duration - TARGET_TOTAL_DURATION), 3
                    ),
                    "finish_last_clip": True,
                    "final_container_duration": round(final_container_duration, 3),
                    "final_video_duration": round(final_video_duration, 3),
                    "final_audio_duration": round(final_audio_duration, 3),
                    "duration_error": round(duration_error, 3),
                    "av_duration_delta": round(av_duration_delta, 3),
                    "av_sync_verified": av_sync_verified,
                    "visual_quality_gate": visual_quality_report,
                    "sync_mode": SYNC_MODE,
                    "sync_mode_label": SYNC_MODE_LABEL,
                }
            )
            with open(PROMO_META_FILE, "w", encoding="utf-8") as handle:
                json.dump(metadata, handle, ensure_ascii=False, indent=2)
        except (OSError, ValueError, TypeError) as exc:
            print(f"[WARN] 无法写入音画同步校验信息: {exc}")
    print(
        f"   -> 音画时长校验：参考 {TARGET_TOTAL_DURATION:.3f}s，"
        f"实际成片 {render_duration:.3f}s，"
        f"视频 {final_video_duration:.3f}s，音频 {final_audio_duration:.3f}s，"
        f"音画差 {av_duration_delta:.3f}s。"
    )
    print(f"\n[Success] V16 功能优化增强版完成: {OUT_FILE}")
    if DEDUP2_ENABLED:
        print(f"[Info] 去重素材池2文案: {PROMO_COPY_FILE}")
        if os.path.exists(PROMO_AUDIO_FILE):
            print(f"[Info] 去重素材池2配音: {PROMO_AUDIO_FILE}")


if __name__ == "__main__":
    try:
        try:
            main()
        except VisualQualityBlockedError as exc:
            print(f"[VisualQualityBlocked] {exc}")
            raise SystemExit(44)
        except VisualQualityError as exc:
            print(f"[VisualQualityEvidenceError] {exc}")
            raise SystemExit(45)
        except NaturalBoundaryError as exc:
            print(f"[NaturalBoundaryRetry] {exc}")
            raise SystemExit(43)
        except Exception as exc:
            # One machine-readable line survives cleanup and the web task's
            # in-memory log eviction. Keep the useful end of FFmpeg stderr;
            # the parent applies its existing path/credential redaction.
            detail = " ".join(str(exc).split())[-650:]
            print(f"[RenderProcessError] {type(exc).__name__}: {detail}", flush=True)
            raise

    finally:
        # Cleanup must still run if either diagnostic summary unexpectedly
        # fails; otherwise a failed reporter could strand a whole task tree.
        try:
            _emit_encoder_summary()
        finally:
            try:
                _emit_performance_summary()
            finally:
                cleanup_tmp_after_run()
