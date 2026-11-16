"""Prepend one optional opening AFTER the original video passes acceptance.

The original video is never re-encoded or duration-trimmed. Normalize only the
opening, then concatenate with stream copy. Publish atomically only after
probing the completed file. Callers supply their owned, cancellable runner.
"""
import json
import math
import os
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

POOL = "开头视频"


def failure_hint(lines):
    """Return a bounded reason, never raw paths, URLs or credentials."""
    text = '\n'.join(str(line) for line in lines[-16:]).lower()
    for markers, reason in (
        (("no space left", "disk full", "not enough space"), "临时输出所在磁盘空间不足"),
        (("cannot allocate memory", "out of memory"), "编码进程可用内存不足"),
        (("permission denied", "access is denied"), "文件写入被拒绝或文件被占用"),
        (("no such file", "file not found"), "素材或临时文件不存在"),
        (("invalid data found", "moov atom not found", "error while decoding"), "素材损坏或无法解码"),
        (("unknown encoder", "encoder not found"), "当前 FFmpeg 缺少所需编码器"),
        (("error initializing", "failed to configure", "invalid argument"), "编码或滤镜参数不兼容"),
    ):
        if any(marker in text for marker in markers):
            return reason
    return "请查看本任务的 FFmpeg 日志定位具体原因"


def choose_opening(names, index):
    if not names:
        return None
    if not isinstance(index, int) or index < 1:
        raise ValueError("开头轮换序号无效")
    if not isinstance(names, (list, tuple)) or len(names) > 50:
        raise ValueError("开头视频最多 50 个")
    for name in names:
        if (not isinstance(name, str) or not name or any(c in name for c in '/\\\r\n\x00')
                or Path(name).suffix.lower() not in {".mp4", ".mov"}):
            raise ValueError("开头视频名称无效")
    return names[(index-1) % len(names)]


def _upright_dimensions(video_stream):
    w = int(video_stream["width"])
    h = int(video_stream["height"])
    rot = 0.0
    tags = video_stream.get("tags") or {}
    if "rotate" in tags:
        try:
            rot = float(tags["rotate"])
        except (TypeError, ValueError):
            rot = 0.0
    for sd in video_stream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rot = float(sd["rotation"])
                break
            except (TypeError, ValueError):
                pass
    if int(round(abs(rot))) % 180 == 90:
        return h, w, True
    return w, h, abs(rot) > 1e-3


def probe(path, ffprobe):
    result = subprocess.run([str(ffprobe), "-v", "error", "-protocol_whitelist", "file,pipe",
        "-show_streams", "-show_format", "-of", "json", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("无法读取开头/核心/成片媒体参数")
    metadata = json.loads(result.stdout)
    videos = [s for s in metadata["streams"] if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    audios = [s for s in metadata["streams"] if s.get("codec_type") == "audio"]
    if not videos:
        raise ValueError("素材文件不包含有效视频轨道")
    primary_video = max(
        videos,
        key=lambda s: (
            1 if s.get("disposition", {}).get("default") else 0,
            int(s.get("width") or 0) * int(s.get("height") or 0),
        ),
    )
    primary_audio = max(
        audios,
        key=lambda s: 1 if s.get("disposition", {}).get("default") else 0,
    ) if audios else None
    v_dur = float(primary_video.get("duration") or 0.0)
    a_dur = float(primary_audio.get("duration") or 0.0) if primary_audio else 0.0
    fmt_dur = float(metadata.get("format", {}).get("duration") or 0.0)
    duration = max(v_dur, a_dur, fmt_dur)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("视频时长无效")
    return primary_video, primary_audio, duration


def prepend_untouched_clips(body, clips, *, ffmpeg, ffprobe, run, mute_clips=False, phase_label="原片前置"):
    if not clips:
        return None
    body = Path(body)
    if not body.is_file() or body.is_symlink():
        raise ValueError(f"{phase_label}目标成片不存在，或属于不安全链接")
    body = body.resolve(strict=True)
    resolved_clips = []
    for raw_clip in clips:
        if raw_clip is None:
            continue
        cp = Path(raw_clip)
        if not cp.is_file() or cp.is_symlink():
            raise ValueError(f"{phase_label}素材不存在，或属于不安全链接")
        cp = cp.resolve(strict=True)
        if cp == body or any(c in str(body) + str(cp) for c in "\r\n"):
            raise ValueError(f"{phase_label}素材不能覆盖自身")
        resolved_clips.append(cp)
    if not resolved_clips:
        return None

    video, audio, original_duration = probe(body, ffprobe)
    clip_specs = [probe(cp, ffprobe) for cp in resolved_clips]
    if video.get("codec_name") != "h264" or video.get("pix_fmt") != "yuv420p":
        raise ValueError("成片编码不在当前已验证范围；已保留原片")
    if video.get("sample_aspect_ratio") not in {None, "1:1", "0:1"}:
        raise ValueError("成片像素宽高比不在当前已验证范围")
    if audio and (audio.get("codec_name") != "aac" or int(audio.get("channels", 0)) not in {1, 2}):
        raise ValueError("成片音频不在当前已验证范围；已保留原片")
    fps = Fraction(video["r_frame_rate"])
    if fps <= 0 or fps > 120:
        raise ValueError("成片帧率无效")
    width, height = int(video["width"]), int(video["height"])
    timescale = Fraction(video["time_base"]).denominator
    profile = {"High": "high", "Main": "main", "Constrained Baseline": "baseline", "Baseline": "baseline"}.get(video.get("profile"))
    if not profile:
        raise ValueError("成片 H264 profile 未被验证")

    any_clip_wants_audio = bool((not mute_clips) and any(ca is not None for _, ca, _ in clip_specs))
    with tempfile.TemporaryDirectory(prefix=".untouched-", dir=body.parent) as directory:
        root = Path(directory).resolve(strict=True)
        if root.parent != body.parent:
            raise RuntimeError(f"{phase_label}临时目录越界")

        body_for_concat = body
        target_sr = int(audio["sample_rate"]) if audio else 44100
        target_ch = int(audio["channels"]) if audio else 2
        has_output_audio = bool(audio is not None or any_clip_wants_audio)

        if audio is None and any_clip_wants_audio:
            body_with_silent = root / "body_with_audio.mp4"
            run([
                str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-loglevel", "warning",
                "-protocol_whitelist", "file,pipe",
                "-i", str(body),
                "-f", "lavfi", "-i", f"anullsrc=r={target_sr}:cl={'stereo' if target_ch == 2 else 'mono'}",
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-ar", str(target_sr), "-ac", str(target_ch), "-b:a", "192k",
                "-t", f"{original_duration:.6f}", "-video_track_timescale", str(timescale),
                "-movflags", "+faststart", str(body_with_silent),
            ], body_with_silent)
            body_for_concat = body_with_silent

        normalized_paths = []
        total_added_duration = 0.0
        for idx, (cp, (c_video, c_audio, c_duration)) in enumerate(zip(resolved_clips, clip_specs)):
            norm_path = root / f"prefix_{idx:03d}.mp4"
            use_clip_audio = bool(c_audio is not None and not mute_clips)
            command = [
                str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-loglevel", "warning", "-threads", "2",
                "-protocol_whitelist", "file,pipe", "-i", str(cp),
            ]
            if has_output_audio and not use_clip_audio:
                command += [
                    "-f", "lavfi", "-i",
                    f"anullsrc=r={target_sr}:cl={'stereo' if target_ch == 2 else 'mono'}",
                ]
            cw, ch, _ = _upright_dimensions(c_video)
            c_sar = c_video.get("sample_aspect_ratio")
            vf_parts = []
            if cw == width and ch == height and c_sar in {None, "1:1", "0:1"}:
                vf_parts.append(f"setsar=1,fps={fps}")
            else:
                if c_sar not in {None, "1:1", "0:1"}:
                    vf_parts.append(
                        "scale='trunc(if(gt(sar,1),iw*sar,iw)/2)*2':'trunc(if(lt(sar,1),ih/sar,ih)/2)*2':flags=lanczos,setsar=1"
                    )
                vf_parts.append(
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}"
                )
            c_vdur = float(c_video.get("duration") or 0.0)
            if c_vdur > 0.0 and c_duration > c_vdur + 0.02:
                vf_parts.append(f"tpad=stop_mode=clone:stop_duration={c_duration - c_vdur:.6f}")

            command += [
                "-map", f"0:{int(c_video['index'])}",
                "-vf", ",".join(vf_parts),
                "-filter_threads", "1", "-c:v", "libx264", "-threads", "2",
                "-preset", "fast", "-crf", "14",
                "-profile:v", profile, "-pix_fmt", "yuv420p",
                "-metadata:s:v:0", "rotate=0",
                "-video_track_timescale", str(timescale),
            ]
            if has_output_audio:
                audio_map = f"0:{int(c_audio['index'])}" if use_clip_audio else "1:a:0"
                command += [
                    "-map", audio_map,
                    "-af", "apad,aresample=async=1:first_pts=0",
                    "-c:a", "aac", "-ar", str(target_sr), "-ac", str(target_ch), "-b:a", "256k",
                ]
            else:
                command += ["-an"]
            command += ["-t", f"{c_duration:.6f}", "-movflags", "+faststart", str(norm_path)]
            run(command, norm_path)
            normalized_paths.append(norm_path)
            total_added_duration += c_duration

        combined = root / "combined.mp4"
        playlist = root / "concat.txt"

        def quoted(p):
            return p.as_posix().replace("'", "'\\''")

        lines = [f"file '{quoted(np)}'" for np in normalized_paths]
        lines.append(f"file '{quoted(body_for_concat)}'")
        playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
        run([
            str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-loglevel", "warning",
            "-f", "concat", "-safe", "0", "-protocol_whitelist", "file,pipe",
            "-i", str(playlist), "-map", "0:v:0", "-map", "0:a?",
            "-c", "copy", "-video_track_timescale", str(timescale),
            "-movflags", "+faststart", str(combined),
        ], combined)
        final_video, final_audio, final_duration = probe(combined, ffprobe)
        tol = max(0.5, (len(resolved_clips) + 2) * max(0.08, 3.0 / float(fps)))
        if (abs(final_duration - original_duration - total_added_duration) > tol
                or any(final_video.get(k) != video.get(k) for k in ("codec_name", "width", "height", "pix_fmt"))
                or Fraction(final_video["r_frame_rate"]) != fps
                or bool(final_audio) != has_output_audio):
            raise RuntimeError(f"{phase_label}拼接验收失败；原成片未修改")
        os.replace(combined, body)
        return {
            "policy": "prepend-untouched-clips-v2",
            "clip_names": [cp.name for cp in resolved_clips],
            "original_duration_seconds": original_duration,
            "added_duration_seconds": total_added_duration,
            "final_duration_seconds": final_duration,
            "body_video_stream_copied": True,
            "audio_omitted": False,
        }


def prepend(body, opening, *, ffmpeg, ffprobe, run, mute_opening=False):
    if opening is None:
        return None  # The no-opening path performs no IO and changes nothing.
    res = prepend_untouched_clips(
        body,
        [opening],
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        run=run,
        mute_clips=bool(mute_opening),
        phase_label="开头",
    )
    if res is None:
        return None
    return {
        "policy": "prepend-after-original-render-v1",
        "opening_name": res["clip_names"][0],
        "original_duration_seconds": res["original_duration_seconds"],
        "opening_duration_seconds": res["added_duration_seconds"],
        "final_duration_seconds": res["final_duration_seconds"],
        "body_video_stream_copied": True,
        "opening_audio_omitted": False,
    }
