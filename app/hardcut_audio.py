#!/usr/bin/env python3
"""8038 硬切配音版专用模块：
1. 原声彻底静音 + 配音（Edge-TTS / 本机 CosyVoice 音色克隆 / macOS 离线兜底 / 现成音频）+ BGM 混流
2. 打乱素材固定秒数硬切后的「单条内零重复 + 多条间最低相似度错位排片」调度器
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

AUDIO_EXTENSIONS = ("mp3", "wav", "m4a", "aac", "flac", "ogg")
DEFAULT_SLICE_SECONDS = 3.0
MIN_SLICE_SECONDS = 0.5
MAX_SLICE_SECONDS = 30.0

APP_DIR = Path(__file__).resolve().parent
COSYVOICE_PYTHON = Path(
    os.environ.get(
        "V17_COSYVOICE_PYTHON",
        os.path.expanduser("~/.openclaw/workspace/yunying/data/cosyvoice-runtime/bin/python"),
    )
)
COSYVOICE_SCRIPT = APP_DIR / "cosyvoice_synthesize_fast.py"
COSYVOICE_DEFAULT_PROMPT = Path(
    os.environ.get(
        "V17_COSYVOICE_PROMPT",
        os.path.expanduser("~/.openclaw/workspace/yunying/data/voice-prompts/desktop-female.wav"),
    )
)
COSYVOICE_FALLBACK_PROMPT = Path(
    os.environ.get(
        "V17_COSYVOICE_FALLBACK_PROMPT",
        os.path.expanduser("~/.openclaw/CosyVoice/asset/zero_shot_prompt.wav"),
    )
)

_COSYVOICE_AVAILABLE = COSYVOICE_PYTHON.is_file() and COSYVOICE_SCRIPT.is_file()
DEFAULT_VOICE_PRESET = "cosy_female" if _COSYVOICE_AVAILABLE else "xiaoxiao"

_ALL_BUILTIN_VOICES = {
    "cosy_female": {
        "label": "本机 CosyVoice · 大模型自然女声（推荐）",
        "engine": "cosyvoice",
        "voice": "cosy_female",
        "macos_voice": "Tingting",
    },
    "custom_clone": {
        "label": "导入参考音色克隆（本机 CosyVoice）",
        "engine": "cosyvoice_custom",
        "voice": "custom_clone",
        "macos_voice": "Tingting",
    },
    "xiaoxiao": {
        "label": "晓晓 · 自然带货女声",
        "engine": "edge",
        "voice": "zh-CN-XiaoxiaoNeural",
        "macos_voice": "Tingting",
    },
    "yunxi": {
        "label": "云希 · 阳光清朗男声",
        "engine": "edge",
        "voice": "zh-CN-YunxiNeural",
        "macos_voice": "Eddy (中文（中国大陆）)",
    },
    "yunjian": {
        "label": "云健 · 沉稳解说男声",
        "engine": "edge",
        "voice": "zh-CN-YunjianNeural",
        "macos_voice": "Reed (中文（中国大陆）)",
    },
    "xiaoyi": {
        "label": "晓伊 · 甜美活力女声",
        "engine": "edge",
        "voice": "zh-CN-XiaoyiNeural",
        "macos_voice": "Meijia",
    },
    "xiaobei": {
        "label": "晓北 · 亲切带货女声",
        "engine": "edge",
        "voice": "zh-CN-liaoning-XiaobeiNeural",
        "macos_voice": "Sandy (中文（中国大陆）)",
    },
    "sinji": {
        "label": "善怡 · 粤语带货女声",
        "engine": "edge",
        "voice": "zh-HK-HiuMaanNeural",
        "macos_voice": "Sinji",
    },
}

BUILTIN_VOICES = (
    dict(_ALL_BUILTIN_VOICES)
    if _COSYVOICE_AVAILABLE
    else {k: v for k, v in _ALL_BUILTIN_VOICES.items() if not str(v.get("engine", "")).startswith("cosyvoice")}
)


def parse_slice_seconds(raw_value, default: float = DEFAULT_SLICE_SECONDS) -> float:
    raw = str(raw_value if raw_value is not None else default).strip()
    if not raw:
        return round(float(default), 2)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("几秒剪辑一刀必须是有效数字（0.5–30.0 秒）。") from exc
    if not math.isfinite(value) or value < MIN_SLICE_SECONDS or value > MAX_SLICE_SECONDS:
        raise ValueError(
            f"几秒剪辑一刀必须在 {MIN_SLICE_SECONDS:.1f}–{MAX_SLICE_SECONDS:.1f} 秒之间。"
        )
    return round(value, 2)


def _as_bool(raw_value, *, default: bool = False) -> bool:
    if raw_value is None:
        return default
    if isinstance(raw_value, bool):
        return raw_value
    text = str(raw_value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on", "keep", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "mute", "disabled"}:
        return False
    return default


def normalize_edition_mode(raw_value, default: str = "v2") -> str:
    text = str(raw_value if raw_value is not None else default).strip().lower()
    if text in {"original", "v1", "legacy", "8018"}:
        return "original"
    if text in {"v2", "hardcut", "8038"}:
        return "v2"
    return "original" if str(default).strip().lower() == "original" else "v2"


def normalize_audio_dub_settings(raw: dict | None) -> dict:
    data = raw if isinstance(raw, dict) else {}
    voice_mode = str(data.get("voice_mode") or "none").strip().lower()
    if voice_mode not in {"none", "upload", "tts"}:
        voice_mode = "none"
    voice_preset = str(data.get("voice_preset") or DEFAULT_VOICE_PRESET).strip()
    if voice_preset not in BUILTIN_VOICES:
        voice_preset = DEFAULT_VOICE_PRESET
    voice_text = str(data.get("voice_text") or "").strip()
    if len(voice_text) > 4000:
        voice_text = voice_text[:4000]
    try:
        voice_speed = float(data.get("voice_speed", 1.0) or 1.0)
    except (TypeError, ValueError):
        voice_speed = 1.0
    voice_speed = round(max(0.6, min(1.6, voice_speed)), 2)

    try:
        voice_volume = int(round(float(data.get("voice_volume", 100))))
    except (TypeError, ValueError):
        voice_volume = 100
    voice_volume = max(0, min(200, voice_volume))

    try:
        bgm_volume = int(round(float(data.get("bgm_volume", 20))))
    except (TypeError, ValueError):
        bgm_volume = 20
    bgm_volume = max(0, min(150, bgm_volume))

    try:
        original_volume = int(round(float(data.get("original_volume", 100))))
    except (TypeError, ValueError):
        original_volume = 100
    original_volume = max(0, min(200, original_volume))

    keep_original_audio = _as_bool(data.get("keep_original_audio"), default=False)
    hardcut_enabled = _as_bool(data.get("hardcut_enabled"), default=True)
    bgm_enabled = _as_bool(data.get("bgm_enabled"), default=True)

    return {
        "voice_mode": voice_mode,
        "voice_preset": voice_preset,
        "voice_text": voice_text,
        "voice_speed": voice_speed,
        "voice_volume": voice_volume,
        "bgm_volume": bgm_volume,
        "original_volume": original_volume,
        "keep_original_audio": keep_original_audio,
        "hardcut_enabled": hardcut_enabled,
        "bgm_enabled": bgm_enabled,
    }


MAX_SUBTITLE_MASK_BOXES = 8
SUBTITLE_MASK_STYLES = ("blur", "delogo", "dark")


def normalize_subtitle_masks(raw, *, strict: bool = False) -> dict:
    default_cfg = {
        "enabled": False,
        "style": "blur",
        "strength": 55,
        "boxes": [],
    }
    if raw is None or raw == "":
        return dict(default_cfg, boxes=[])
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return dict(default_cfg, boxes=[])
        try:
            data = json.loads(text)
        except (TypeError, ValueError) as exc:
            if strict:
                raise ValueError("手动挡字选框数据格式无效，请重新调整选框。") from exc
            return dict(default_cfg, boxes=[])
    elif isinstance(raw, dict):
        data = raw
    else:
        if strict:
            raise ValueError("手动挡字选框参数类型无效。")
        return dict(default_cfg, boxes=[])

    style = str(data.get("style") or "blur").strip().lower()
    if style not in SUBTITLE_MASK_STYLES:
        if strict:
            raise ValueError("手动挡字效果类型无效。")
        style = "blur"

    try:
        strength = int(round(float(data.get("strength", 55))))
    except (TypeError, ValueError) as exc:
        if strict:
            raise ValueError("手动挡字模糊度必须是有效数字。") from exc
        strength = 55
    strength = max(10, min(100, strength))

    raw_boxes = data.get("boxes")
    if raw_boxes is None:
        raw_boxes = []
    if not isinstance(raw_boxes, list):
        if strict:
            raise ValueError("手动挡字选框列表格式无效。")
        raw_boxes = []
    if strict and len(raw_boxes) > MAX_SUBTITLE_MASK_BOXES:
        raise ValueError(f"手动挡字选框最多支持 {MAX_SUBTITLE_MASK_BOXES} 个。")

    boxes: list[dict] = []
    for item in raw_boxes[:MAX_SUBTITLE_MASK_BOXES]:
        if not isinstance(item, dict):
            if strict:
                raise ValueError("手动挡字选框坐标无效。")
            continue
        try:
            bx = float(item.get("x", 0.0))
            by = float(item.get("y", 0.0))
            bw = float(item.get("w", 0.0))
            bh = float(item.get("h", 0.0))
        except (TypeError, ValueError) as exc:
            if strict:
                raise ValueError("手动挡字选框坐标必须是数字。") from exc
            continue
        if not all(math.isfinite(v) for v in (bx, by, bw, bh)):
            if strict:
                raise ValueError("手动挡字选框坐标超出有效范围。")
            continue
        if bw <= 0.005 or bh <= 0.005:
            continue
        bx = max(0.0, min(0.98, bx))
        by = max(0.0, min(0.985, by))
        bw = max(0.02, min(1.0 - bx, bw))
        bh = max(0.012, min(1.0 - by, bh))
        raw_target = str(item.get("target") or "all").strip()
        if not raw_target or raw_target in {"all", "*"}:
            target = "all"
        else:
            target = Path(raw_target.replace("\\", "/")).name[:180] or "all"
        boxes.append(
            {
                "x": round(bx, 4),
                "y": round(by, 4),
                "w": round(bw, 4),
                "h": round(bh, 4),
                "target": target,
            }
        )

    enabled_flag = _as_bool(data.get("enabled"), default=bool(boxes))
    return {
        "enabled": bool(enabled_flag and len(boxes) > 0),
        "style": style,
        "strength": strength,
        "boxes": boxes,
    }


def build_subtitle_mask_vf_filters(
    config: dict | None,
    *,
    width: int = 720,
    height: int = 1280,
    source_name: str | None = None,
) -> list[str]:
    norm = normalize_subtitle_masks(config, strict=False)
    if not norm["enabled"] or not norm["boxes"]:
        return []
    src_base = Path(source_name).name if source_name else ""
    matched = [
        b
        for b in norm["boxes"]
        if b["target"] == "all" or (src_base and b["target"] == src_base)
    ]
    if not matched:
        return []

    style = norm["style"]
    strength = int(norm["strength"])
    filters: list[str] = []
    for idx, b in enumerate(matched):
        if style == "delogo":
            px = max(2, min(width - 6, int(round(b["x"] * width))))
            py = max(2, min(height - 6, int(round(b["y"] * height))))
            pw = max(4, min(width - px - 2, int(round(b["w"] * width))))
            ph = max(4, min(height - py - 2, int(round(b["h"] * height))))
            filters.append(f"delogo=x={px}:y={py}:w={pw}:h={ph}")
        else:
            px = max(0, min(width - 4, int(round(b["x"] * width / 2.0)) * 2))
            py = max(0, min(height - 4, int(round(b["y"] * height / 2.0)) * 2))
            pw = max(4, min(width - px, int(round(b["w"] * width / 2.0)) * 2))
            ph = max(4, min(height - py, int(round(b["h"] * height / 2.0)) * 2))
            if style == "dark":
                alpha = round(0.55 + (strength / 100.0) * 0.40, 2)
                filters.append(
                    f"drawbox=x={px}:y={py}:w={pw}:h={ph}:color=black@{alpha:.2f}:t=fill"
                )
            else:
                sigma = round(6.0 + (strength / 100.0) * 34.0, 2)
                filters.append(
                    f"split=2[_smb{idx}][_smc{idx}];"
                    f"[_smc{idx}]crop={pw}:{ph}:{px}:{py},gblur=sigma={sigma:.2f}:steps=2[_sms{idx}];"
                    f"[_smb{idx}][_sms{idx}]overlay={px}:{py}"
                )
    return filters


def _run_quiet(cmd, **kwargs):
    flags = int(kwargs.pop("creationflags", 0)) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if kwargs.get("text"):
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    return subprocess.run(cmd, creationflags=flags, **kwargs)


def _resolve_bin(name: str, env_key: str) -> str:
    configured = os.environ.get(env_key, "").strip().strip('"')
    if configured and Path(configured).is_file():
        return configured
    exe_name = f"{name}.exe" if os.name == "nt" and not name.lower().endswith(".exe") else name
    for cand_dir in (
        APP_DIR.parent / "runtime" / "ffmpeg" / "bin",
        APP_DIR.parent.parent / "runtime" / "ffmpeg" / "bin",
        APP_DIR.parent / "ffmpeg" / "bin",
    ):
        cand = cand_dir / exe_name
        if cand.is_file():
            return str(cand.resolve())
    found = shutil.which(name)
    if found:
        return found
    if name == "ffmpeg":
        img_ff = os.environ.get("IMAGEIO_FFMPEG_EXE", "").strip().strip('"')
        if img_ff and Path(img_ff).is_file():
            return img_ff
    return configured or name


def probe_audio_duration(path: str | Path, ffprobe: str | None = None) -> float:
    probe_exe = ffprobe or _resolve_bin("ffprobe", "V16_FFPROBE_EXE")
    target = Path(path)
    if not target.is_file() or target.stat().st_size <= 0:
        return 0.0
    stream_check = _run_quiet(
        [
            probe_exe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if stream_check.returncode != 0 or "audio" not in (stream_check.stdout or "").lower():
        return 0.0
    dur_check = _run_quiet(
        [
            probe_exe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    try:
        val = float((dur_check.stdout or "").strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0.0
    return val if math.isfinite(val) and val > 0.0 else 0.0


def _probe_stream_duration(path: Path, selector: str, ffprobe: str) -> float:
    res = _run_quiet(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            selector,
            "-show_entries",
            "stream=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    for line in (res.stdout or "").splitlines():
        try:
            val = float(line.strip())
            if math.isfinite(val) and val > 0:
                return val
        except ValueError:
            continue
    fmt = _run_quiet(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    try:
        val = float((fmt.stdout or "").strip().splitlines()[0])
        return val if math.isfinite(val) and val > 0 else 0.0
    except (ValueError, IndexError):
        return 0.0


def _prepare_cosyvoice_prompt_wav(
    source_audio: Path, out_wav: Path, ffmpeg: str
) -> Path:
    """Convert any user-uploaded reference audio (mp3/m4a/wav/mov) into a clean 3-10s 16kHz mono WAV."""
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(source_audio),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-af",
        "silenceremove=start_periods=1:start_silence=0.15:start_threshold=-45dB,atrim=0:9.5,asetpts=PTS-STARTPTS",
        str(out_wav),
    ]
    proc = _run_quiet(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0 or probe_audio_duration(out_wav) < 0.8:
        # Fallback without silenceremove in case reference audio is quiet
        _run_quiet(
            [
                ffmpeg,
                "-y",
                "-i",
                str(source_audio),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-t",
                "9.5",
                str(out_wav),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    if probe_audio_duration(out_wav) < 0.5:
        raise ValueError("参考音色音频有效时长不足 0.5 秒，请更换清晰的说话音频（建议 3–10 秒）。")
    return out_wav


def synthesize_voiceover(
    text: str,
    voice_preset: str,
    output_wav_path: str | Path,
    *,
    speed: float = 1.0,
    ref_audio_path: str | Path | None = None,
    ffmpeg: str | None = None,
) -> dict:
    clean_text = " ".join(str(text or "").split()).strip()
    if not clean_text:
        raise ValueError("请输入配音文案，或切换为“上传现成配音音频 / 仅取消原声”。")

    ffmpeg_exe = ffmpeg or _resolve_bin("ffmpeg", "V16_FFMPEG_EXE")
    preset_key = voice_preset if voice_preset in BUILTIN_VOICES else "xiaoxiao"
    descriptor = BUILTIN_VOICES[preset_key]
    engine = descriptor["engine"]
    speed = max(0.6, min(1.6, float(speed or 1.0)))
    output_path = Path(output_wav_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="v17_8038_tts_") as tmp_dir_raw:
        tmp_dir = Path(tmp_dir_raw)
        raw_audio: Path | None = None
        engine_used = ""
        errors: list[str] = []

        if engine in {"cosyvoice", "cosyvoice_custom"}:
            prompt_source: Path | None = None
            if engine == "cosyvoice_custom":
                if not ref_audio_path or not Path(ref_audio_path).is_file():
                    raise ValueError("选择了“导入参考音色克隆”，请先上传 1 条参考音色音频。")
                prompt_source = _prepare_cosyvoice_prompt_wav(
                    Path(ref_audio_path), tmp_dir / "ref_prompt.wav", ffmpeg_exe
                )
            else:
                if COSYVOICE_DEFAULT_PROMPT.is_file():
                    prompt_source = COSYVOICE_DEFAULT_PROMPT
                elif COSYVOICE_FALLBACK_PROMPT.is_file():
                    prompt_source = COSYVOICE_FALLBACK_PROMPT

            if (
                prompt_source is not None
                and COSYVOICE_PYTHON.is_file()
                and COSYVOICE_SCRIPT.is_file()
            ):
                cosy_out = tmp_dir / "cosy_out.wav"
                proc = _run_quiet(
                    [
                        str(COSYVOICE_PYTHON),
                        str(COSYVOICE_SCRIPT),
                        f"--text={clean_text}",
                        "--prompt",
                        str(prompt_source),
                        "--output",
                        str(cosy_out),
                        "--speed",
                        f"{speed:.2f}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                if proc.returncode == 0 and probe_audio_duration(cosy_out) > 0.1:
                    raw_audio = cosy_out
                    engine_used = (
                        "CosyVoice音色克隆"
                        if engine == "cosyvoice_custom"
                        else "CosyVoice本机女声"
                    )
                else:
                    tail = (proc.stderr or proc.stdout or "").strip()[-240:]
                    errors.append(f"CosyVoice合成失败: {tail}")
                    if engine == "cosyvoice_custom":
                        raise RuntimeError(f"参考音色克隆合成失败：{tail or '请检查参考音频是否清晰'}")
            elif engine == "cosyvoice_custom":
                raise RuntimeError("本机 CosyVoice 音色克隆环境未就绪，请改用内置音色或直接上传配音音频。")

        if raw_audio is None:
            # Edge-TTS neural voice synthesis
            edge_voice = (
                descriptor["voice"]
                if descriptor["engine"] == "edge"
                else "zh-CN-XiaoxiaoNeural"
            )
            rate_pct = int(round((speed - 1.0) * 100))
            rate_arg = f"{rate_pct:+d}%"
            edge_mp3 = tmp_dir / "edge_out.mp3"
            py_candidates: list[Path] = []
            env_py = os.environ.get("V16_PYTHON_EXE", "").strip()
            if env_py:
                py_candidates.append(Path(env_py))
            if os.name == "nt" and sys.executable.lower().endswith("pythonw.exe"):
                py_candidates.append(Path(sys.executable).with_name("python.exe"))
            py_candidates.extend(
                [
                    Path("/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"),
                    Path(sys.executable),
                ]
            )
            seen_py: set[str] = set()
            for py_bin in py_candidates:
                if not py_bin.is_file():
                    continue
                try:
                    key_str = os.path.normcase(str(py_bin.resolve()))
                except OSError:
                    key_str = os.path.normcase(str(py_bin))
                if key_str in seen_py:
                    continue
                seen_py.add(key_str)
                edge_env = os.environ.copy()
                cert_cand = py_bin.parent / "Lib" / "site-packages" / "certifi" / "cacert.pem"
                if cert_cand.is_file():
                    edge_env.setdefault("SSL_CERT_FILE", str(cert_cand))
                    edge_env.setdefault("REQUESTS_CA_BUNDLE", str(cert_cand))
                try:
                    proc = _run_quiet(
                        [
                            str(py_bin),
                            "-B",
                            "-X",
                            "utf8",
                            "-m",
                            "edge_tts",
                            "--voice",
                            edge_voice,
                            f"--rate={rate_arg}",
                            f"--text={clean_text}",
                            "--write-media",
                            str(edge_mp3),
                        ],
                        env=edge_env,
                        capture_output=True,
                        text=True,
                        timeout=25,
                    )
                    if proc.returncode == 0 and probe_audio_duration(edge_mp3) > 0.1:
                        raw_audio = edge_mp3
                        engine_used = f"Edge-TTS ({edge_voice})"
                        break
                    errors.append((proc.stderr or proc.stdout or "edge-tts error").strip()[-160:])
                except subprocess.TimeoutExpired:
                    errors.append("edge-tts timeout")

        if raw_audio is None and Path("/usr/bin/say").is_file():
            # Offline macOS system voice fallback so local rendering never fails offline
            say_aiff = tmp_dir / "say_out.aiff"
            say_txt = tmp_dir / "say_input.txt"
            say_txt.write_text(clean_text, encoding="utf-8")
            wpm = max(110, min(300, int(round(185 * speed))))
            macos_voice = descriptor.get("macos_voice") or "Tingting"
            proc = _run_quiet(
                [
                    "/usr/bin/say",
                    "-v",
                    macos_voice,
                    "-r",
                    str(wpm),
                    "-f",
                    str(say_txt),
                    "-o",
                    str(say_aiff),
                ],
                capture_output=True,
                text=True,
                timeout=45,
            )
            if proc.returncode == 0 and probe_audio_duration(say_aiff) > 0.1:
                raw_audio = say_aiff
                engine_used = f"macOS离线语音 ({macos_voice})"
            else:
                errors.append((proc.stderr or "say failed").strip()[-120:])

        if raw_audio is None and os.name == "nt":
            # Offline Windows SAPI voice fallback via UTF-16LE -EncodedCommand
            sapi_wav = tmp_dir / "sapi_out.wav"
            sapi_txt = tmp_dir / "sapi_input.txt"
            sapi_txt.write_text(clean_text, encoding="utf-8")
            sapi_rate = max(-5, min(5, int(round((speed - 1.0) * 6))))
            sapi_env = os.environ.copy()
            sapi_env["V17_SAPI_TXT"] = str(sapi_txt)
            sapi_env["V17_SAPI_WAV"] = str(sapi_wav)
            ps_script = (
                "$ProgressPreference = 'SilentlyContinue'; "
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "foreach ($v in $s.GetInstalledVoices()) { "
                "if ($v.Enabled -and $v.VoiceInfo.Culture.Name -like 'zh*') { "
                "$s.SelectVoice($v.VoiceInfo.Name); break } "
                "}; "
                f"$s.Rate = {sapi_rate}; "
                "$txt = [System.IO.File]::ReadAllText($env:V17_SAPI_TXT, [System.Text.Encoding]::UTF8); "
                "$s.SetOutputToWaveFile($env:V17_SAPI_WAV); "
                "$s.Speak($txt); "
                "$s.Dispose()"
            )
            encoded_ps = base64.b64encode(ps_script.encode("utf-16le")).decode("ascii")
            proc = _run_quiet(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-WindowStyle",
                    "Hidden",
                    "-EncodedCommand",
                    encoded_ps,
                ],
                env=sapi_env,
                capture_output=True,
                text=True,
                timeout=45,
            )
            if proc.returncode == 0 and probe_audio_duration(sapi_wav) > 0.1:
                raw_audio = sapi_wav
                engine_used = "Windows离线语音"
            else:
                errors.append((proc.stderr or "Windows SAPI failed").strip()[-120:])

        if raw_audio is None:
            raise RuntimeError(
                "配音合成失败：" + ("；".join(e for e in errors if e) or "未生成有效音频")
            )

        pending_wav = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex[:8]}.wav")
        norm_proc = _run_quiet(
            [
                ffmpeg_exe,
                "-y",
                "-i",
                str(raw_audio),
                "-vn",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                str(pending_wav),
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if norm_proc.returncode != 0:
            pending_wav.unlink(missing_ok=True)
            raise RuntimeError("配音音频标准化失败。")
        os.replace(pending_wav, output_path)

    dur = probe_audio_duration(output_path)
    if dur <= 0.05:
        raise RuntimeError("生成的配音文件时长无效。")
    return {
        "path": str(output_path),
        "duration_seconds": round(dur, 3),
        "engine_used": engine_used,
    }


def apply_final_audio_mix(
    video_path: str | Path,
    *,
    keep_original_audio: bool = False,
    original_volume_percent: int | float = 100,
    voice_path: str | Path | None = None,
    bgm_path: str | Path | None = None,
    voice_volume_percent: int | float = 100,
    bgm_volume_percent: int | float = 20,
    voice_delay_seconds: float = 0.0,
    ffmpeg: str | None = None,
    ffprobe: str | None = None,
) -> dict:
    """Optionally preserve or strip original video audio and mux optional voiceover + looping BGM.

    Preserves the encoded video stream bit-for-bit (-c:v copy) while producing an
    exact-duration 44100Hz stereo AAC track so V17 av_sync checks always pass.
    """
    ffmpeg_exe = ffmpeg or _resolve_bin("ffmpeg", "V16_FFMPEG_EXE")
    ffprobe_exe = ffprobe or _resolve_bin("ffprobe", "V16_FFPROBE_EXE")
    target = Path(video_path).resolve(strict=True)
    v_dur = _probe_stream_duration(target, "v:0", ffprobe_exe)
    if v_dur <= 0.05:
        raise RuntimeError(f"无法读取成片视频时长：{target.name}")

    orig_a_dur = _probe_stream_duration(target, "a:0", ffprobe_exe) if keep_original_audio else 0.0
    has_orig = bool(
        keep_original_audio
        and orig_a_dur > 0.05
        and float(original_volume_percent) > 0.0
    )
    has_voice = bool(
        voice_path
        and Path(voice_path).is_file()
        and float(voice_volume_percent) > 0.0
    )
    has_bgm = bool(
        bgm_path
        and Path(bgm_path).is_file()
        and float(bgm_volume_percent) > 0.0
    )

    # Zero-loss fast pass: keeping 100% original audio with no voiceover or BGM overlay
    if (
        keep_original_audio
        and not has_voice
        and not has_bgm
        and orig_a_dur > 0.05
        and abs(float(original_volume_percent) - 100.0) < 0.5
        and abs(v_dur - orig_a_dur) <= 0.15
    ):
        return {
            "video_duration_seconds": round(v_dur, 3),
            "audio_duration_seconds": round(orig_a_dur, 3),
            "keep_original_audio": True,
            "has_voice": False,
            "has_bgm": False,
            "summary": "已保留原片原声（未叠加额外配音或背景音乐）",
        }

    o_vol = max(0.0, min(2.5, float(original_volume_percent) / 100.0))
    v_vol = max(0.0, min(2.5, float(voice_volume_percent) / 100.0))
    b_vol = max(0.0, min(2.0, float(bgm_volume_percent) / 100.0))
    fade_dur = min(0.8, max(0.15, v_dur * 0.08))
    fade_start = max(0.0, v_dur - fade_dur)

    temp_out = target.with_name(f".{target.stem}.audiomix.{uuid.uuid4().hex[:8]}.mp4")
    cmd = [ffmpeg_exe, "-y", "-i", str(target)]

    input_idx = 1
    filter_chains: list[str] = []
    active_labels: list[str] = []
    desc_parts: list[str] = []

    if has_orig:
        filter_chains.append(
            f"[0:a]aresample=44100:async=1:first_pts=0,"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={o_vol:.4f},apad,atrim=0:{v_dur:.6f},asetpts=PTS-STARTPTS[oa]"
        )
        active_labels.append("[oa]")
        desc_parts.append(f"保留原片原声（{int(round(original_volume_percent))}%）")

    if has_voice:
        voice_resolved = Path(voice_path).resolve(strict=True)
        cmd.extend(["-i", str(voice_resolved)])
        v_delay_sec = max(0.0, min(max(0.0, v_dur - 0.05), float(voice_delay_seconds or 0.0)))
        v_delay_ms = int(round(v_delay_sec * 1000.0))
        v_delay_flt = f"adelay={v_delay_ms}:all=1," if v_delay_ms > 0 else ""
        voice_src_dur = probe_audio_duration(voice_resolved, ffprobe_exe)
        v_tail_fade = ""
        if (v_delay_sec + voice_src_dur) > v_dur + 0.10:
            vf_dur = min(0.35, max(0.12, v_dur * 0.06))
            vf_st = max(0.0, v_dur - vf_dur)
            v_tail_fade = f",afade=t=out:st={vf_st:.6f}:d={vf_dur:.6f}"
        filter_chains.append(
            f"[{input_idx}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={v_vol:.4f},{v_delay_flt}apad,atrim=0:{v_dur:.6f}{v_tail_fade},asetpts=PTS-STARTPTS[va]"
        )
        input_idx += 1
        active_labels.append("[va]")
        desc_parts.append(f"配音（{int(round(voice_volume_percent))}%）")

    if has_bgm:
        cmd.extend(["-stream_loop", "-1", "-i", str(Path(bgm_path).resolve(strict=True))])
        filter_chains.append(
            f"[{input_idx}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume={b_vol:.4f},atrim=0:{v_dur:.6f},"
            f"afade=t=out:st={fade_start:.6f}:d={fade_dur:.6f},asetpts=PTS-STARTPTS[ba]"
        )
        input_idx += 1
        active_labels.append("[ba]")
        desc_parts.append(f"背景音乐（{int(round(bgm_volume_percent))}%）")

    if not active_labels:
        cmd.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-filter_complex",
                f"[{input_idx}:a]atrim=0:{v_dur:.6f},asetpts=PTS-STARTPTS[aout]",
            ]
        )
        mode_desc = (
            "原片无音轨，已补齐静音音轨"
            if keep_original_audio
            else "已取消原片原声（未启用配音或背景音乐，输出纯静音音轨）"
        )
    elif len(active_labels) == 1:
        single_label = active_labels[0]
        filter_chains.append(
            f"{single_label}alimiter=limit=0.95,atrim=0:{v_dur:.6f},asetpts=PTS-STARTPTS[aout]"
        )
        cmd.extend(["-filter_complex", ";".join(filter_chains)])
        prefix = "已" if keep_original_audio else "已取消原声并铺入"
        mode_desc = f"{prefix}{'＋'.join(desc_parts)}"
    else:
        filter_chains.append(
            f"{''.join(active_labels)}amix=inputs={len(active_labels)}:"
            f"duration=first:dropout_transition=0:normalize=0,"
            f"alimiter=limit=0.95,atrim=0:{v_dur:.6f},asetpts=PTS-STARTPTS[aout]"
        )
        cmd.extend(["-filter_complex", ";".join(filter_chains)])
        prefix = "已合成" if keep_original_audio else "已取消原声并合成"
        mode_desc = f"{prefix}{'＋'.join(desc_parts)}"

    cmd.extend(
        [
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(temp_out),
        ]
    )

    try:
        proc = _run_quiet(cmd, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0 or not temp_out.is_file():
            tail = (proc.stderr or proc.stdout or "").strip()[-240:]
            raise RuntimeError(f"最终配音/背景音乐混流失败：{tail}")
        out_v = _probe_stream_duration(temp_out, "v:0", ffprobe_exe)
        out_a = _probe_stream_duration(temp_out, "a:0", ffprobe_exe)
        if out_v <= 0.05 or out_a <= 0.05 or abs(out_v - out_a) > 0.15:
            raise RuntimeError(
                f"混流后音画时长不一致（视频 {out_v:.3f}s / 音频 {out_a:.3f}s）"
            )
        os.replace(temp_out, target)
    finally:
        temp_out.unlink(missing_ok=True)

    return {
        "video_duration_seconds": round(out_v, 3),
        "audio_duration_seconds": round(out_a, 3),
        "keep_original_audio": bool(keep_original_audio),
        "has_voice": has_voice,
        "has_bgm": has_bgm,
        "summary": mode_desc,
    }


def build_shuffled_audio_schedule(
    names: list[str] | tuple[str, ...],
    total_count: int,
    *,
    seed_key: str = "",
) -> list[str | None]:
    """Randomly assign checked/uploaded audio files across N outputs without repeats per round.

    When len(unique_names) >= total_count (e.g. 5 audios for 5 videos), every output
    receives a distinct audio in randomized order. When total_count > len(unique_names),
    full shuffled cycles are chained while preventing back-to-back boundary collisions.
    """
    count = max(0, int(total_count or 0))
    if count <= 0:
        return []
    clean_names: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        item = str(raw or "").strip()
        if item and item not in seen:
            seen.add(item)
            clean_names.append(item)
    if not clean_names:
        return [None] * count
    if len(clean_names) == 1:
        return [clean_names[0]] * count

    seed_material = str(seed_key or uuid.uuid4().hex).encode("utf-8")
    seed_int = int.from_bytes(hashlib.sha256(seed_material).digest()[:16], "big")
    rng = random.Random(seed_int)
    schedule: list[str | None] = []
    while len(schedule) < count:
        batch = list(clean_names)
        rng.shuffle(batch)
        if schedule and len(batch) > 1 and batch[0] == schedule[-1]:
            swap_idx = rng.randrange(1, len(batch))
            batch[0], batch[swap_idx] = batch[swap_idx], batch[0]
        schedule.extend(batch)
    return schedule[:count]


def _clip_seq(clip, default: int = 0) -> int:
    val = getattr(clip, "seq", None)
    if val is None:
        val = getattr(clip, "sequence", None)
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _clip_unique_key(clip) -> str:
    source = str(getattr(clip, "source", "") or Path(getattr(clip, "path", "")).name)
    start = round(float(getattr(clip, "start", 0.0) or 0.0), 2)
    seq = _clip_seq(clip, 0)
    return f"{source}@{start:.2f}#{seq}"


def _load_batch_ledger(ledger_path: Path | None) -> dict:
    if ledger_path is None or not ledger_path.is_file():
        return {"used_counts": {}, "position_bins": []}
    try:
        data = json.loads(ledger_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                "used_counts": dict(data.get("used_counts") or {}),
                "position_bins": list(data.get("position_bins") or []),
            }
    except (OSError, ValueError, TypeError):
        pass
    return {"used_counts": {}, "position_bins": []}


def _save_batch_ledger(ledger_path: Path | None, data: dict) -> None:
    if ledger_path is None:
        return
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = ledger_path.with_name(f".{ledger_path.name}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, ledger_path)
    except OSError:
        pass


def _timeline_bins_for_order(clips: list, num_bins: int = 24) -> list[str]:
    if not clips:
        return []
    durations = [max(0.05, float(getattr(c, "duration", 1.0) or 1.0)) for c in clips]
    total = sum(durations)
    cumulative = []
    acc = 0.0
    for dur in durations:
        acc += dur
        cumulative.append((acc, _clip_unique_key(clips[len(cumulative)])))
    bins: list[str] = []
    for b in range(num_bins):
        probe_t = ((b + 0.5) / num_bins) * total
        chosen_key = cumulative[-1][1]
        for end_t, key in cumulative:
            if probe_t <= end_t + 1e-6:
                chosen_key = key
                break
        bins.append(chosen_key)
    return bins


def _build_staggered_candidate(pool: list, rng: random.Random) -> list:
    """Build a randomized permutation that avoids consecutive same-source or sequential chunks."""
    remaining = list(pool)
    rng.shuffle(remaining)
    ordered = []
    while remaining:
        if not ordered:
            ordered.append(remaining.pop(0))
            continue
        prev = ordered[-1]
        prev_src = getattr(prev, "source", "")
        prev_seq = _clip_seq(prev, -999)
        best_idx = 0
        best_penalty = 10**9
        for idx, cand in enumerate(remaining):
            cand_src = getattr(cand, "source", "")
            cand_seq = _clip_seq(cand, -999)
            penalty = 0
            if cand_src == prev_src:
                penalty += 10
                if cand_seq == prev_seq + 1:
                    penalty += 50
                elif abs(cand_seq - prev_seq) == 1:
                    penalty += 25
            if penalty == 0:
                best_idx = idx
                break
            if penalty < best_penalty:
                best_penalty = penalty
                best_idx = idx
        ordered.append(remaining.pop(best_idx))
    return ordered


def select_non_repeating_slices(
    clips: list,
    target_duration: float,
    *,
    variant_seed: str = "",
    ledger_path: str | Path | None = None,
) -> list:
    """Select N-second slices with STRICT 0% intra-video repetition and minimum cross-variant similarity."""
    unique_clips = []
    seen_keys: set[str] = set()
    for clip in clips or ():
        dur = float(getattr(clip, "duration", 0.0) or 0.0)
        if dur <= 0.05:
            continue
        key = _clip_unique_key(clip)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_clips.append(clip)

    if not unique_clips:
        return []

    ledger_file = Path(ledger_path) if ledger_path else None
    ledger = _load_batch_ledger(ledger_file)
    used_counts: dict[str, int] = {
        str(k): int(v) for k, v in (ledger.get("used_counts") or {}).items()
    }
    prior_bins_list: list[list[str]] = [
        list(b) for b in (ledger.get("position_bins") or []) if isinstance(b, list)
    ]

    seed_material = f"{variant_seed}|n={len(prior_bins_list)}|u={len(unique_clips)}"
    seed_int = int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed_int)

    total_unique_dur = sum(float(c.duration) for c in unique_clips)
    avg_dur = total_unique_dur / max(1, len(unique_clips))
    target = max(avg_dur, float(target_duration or 0.0))

    if total_unique_dur <= target + 0.05:
        chosen_subset = list(unique_clips)
    else:
        # Prioritize slices with lowest prior usage across batch variants so multi-output
        # fission picks completely disjoint slices whenever stock allows.
        decorated = []
        for clip in unique_clips:
            key = _clip_unique_key(clip)
            tie_hash = hashlib.sha256(f"{seed_material}:{key}".encode("utf-8")).hexdigest()
            decorated.append((used_counts.get(key, 0), tie_hash, clip))
        decorated.sort(key=lambda item: (item[0], item[1]))

        chosen_subset = []
        acc_dur = 0.0
        stop_threshold = max(0.4, target - 0.35 * avg_dur)
        for _use_cnt, _tie, clip in decorated:
            chosen_subset.append(clip)
            acc_dur += float(clip.duration)
            if acc_dur >= stop_threshold:
                break

    if len(chosen_subset) <= 1:
        best_order = chosen_subset
    else:
        best_order = None
        best_score = None
        candidate_trials = 160
        for _ in range(candidate_trials):
            cand_order = _build_staggered_candidate(chosen_subset, rng)
            cand_bins = _timeline_bins_for_order(cand_order, num_bins=24)
            pos_collisions = 0
            max_pair_collision = 0
            for prior_bins in prior_bins_list:
                if len(prior_bins) == len(cand_bins):
                    pair_hits = sum(
                        1 for a, b in zip(cand_bins, prior_bins) if a == b
                    )
                    pos_collisions += pair_hits
                    if pair_hits > max_pair_collision:
                        max_pair_collision = pair_hits
            same_src_adj = 0
            seq_adj = 0
            for a, b in zip(cand_order, cand_order[1:]):
                if getattr(a, "source", "") == getattr(b, "source", ""):
                    same_src_adj += 1
                    sa = _clip_seq(a, -999)
                    sb = _clip_seq(b, -999)
                    if sb == sa + 1:
                        seq_adj += 1
            # Minimize worst pairwise timeline collision first, then total collisions,
            # then sequential chunk adjacency, then same-source adjacency.
            score = (max_pair_collision, pos_collisions, seq_adj, same_src_adj)
            if best_score is None or score < best_score:
                best_score = score
                best_order = cand_order
                if score == (0, 0, 0, 0):
                    break

    final_order = list(best_order or chosen_subset)
    for clip in final_order:
        key = _clip_unique_key(clip)
        used_counts[key] = used_counts.get(key, 0) + 1
    prior_bins_list.append(_timeline_bins_for_order(final_order, num_bins=24))
    _save_batch_ledger(
        ledger_file,
        {"used_counts": used_counts, "position_bins": prior_bins_list},
    )
    return final_order
