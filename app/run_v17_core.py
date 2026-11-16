import os
import random
import shutil
import subprocess
import sys
import traceback
from pathlib import Path


W, H = 720, 1280
FPS = 25
VIDEO_EXTENSIONS = (".mp4", ".mov")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def decode_output(value):
    if not value:
        return ""
    if isinstance(value, str):
        return value
    for encoding in ("utf-8", "gb18030"):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            pass
    return value.decode("utf-8", errors="replace")


def resolve_tool(env_name, fallback_name):
    explicit = os.environ.get(env_name, "").strip().strip('"')
    if explicit and Path(explicit).is_file():
        return explicit
    located = shutil.which(fallback_name)
    if located:
        return located
    raise FileNotFoundError(
        f"找不到 {fallback_name}。请重新安装完整安装包，或检查安全软件隔离区。"
    )


def run_command(command, label, *, check=True):
    print(f"[{label}] {subprocess.list2cmdline([str(item) for item in command])}", flush=True)
    result = subprocess.run(
        [str(item) for item in command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=CREATE_NO_WINDOW,
    )
    stdout = decode_output(result.stdout).strip()
    stderr = decode_output(result.stderr).strip()
    if stdout:
        print(stdout[-4000:], flush=True)
    if result.returncode != 0 and check:
        detail = (stderr or stdout or "没有返回详细信息")[-6000:]
        raise RuntimeError(f"{label}失败（退出码 {result.returncode}）：\n{detail}")
    return result, stdout, stderr


def media_duration(ffprobe, file_path):
    _result, stdout, _stderr = run_command(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            file_path,
        ],
        "读取视频时长",
    )
    try:
        return max(0.0, float(stdout.strip()))
    except (TypeError, ValueError):
        raise RuntimeError(f"无法读取视频时长：{file_path}")


def has_audio_stream(ffprobe, file_path):
    result, stdout, _stderr = run_command(
        [
            ffprobe,
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
        "检查音轨",
        check=False,
    )
    return result.returncode == 0 and bool(stdout.strip())


def files_in(folder, extensions):
    if not folder.is_dir():
        return []
    return sorted(
        path
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() in extensions
    )


def transcode_chunk(ffmpeg, ffprobe, source, target):
    video_filter = (
        f"scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},fps={FPS},format=yuv420p"
    )
    command = [ffmpeg, "-nostdin", "-hide_banner", "-y", "-i", source]
    if has_audio_stream(ffprobe, source):
        command.extend(
            [
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-af",
                "aresample=async=1:first_pts=0,apad",
            ]
        )
    else:
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
            ]
        )
    command.extend(
        [
            "-vf",
            video_filter,
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-ac",
            "2",
            "-ar",
            "44100",
            "-shortest",
            "-avoid_negative_ts",
            "make_zero",
            "-f",
            "mpegts",
            target,
        ]
    )
    run_command(command, f"标准化素材 {source.name}")


def build_final(ffmpeg, base_video, stickers, output_file):
    command = [ffmpeg, "-nostdin", "-hide_banner", "-y", "-i", base_video]
    selected_stickers = list(stickers[:4])
    if selected_stickers:
        while len(selected_stickers) < 4:
            selected_stickers.append(
                selected_stickers[len(selected_stickers) % len(stickers)]
            )

    filter_parts = []
    sticker_labels = []
    for index, sticker in enumerate(selected_stickers, start=1):
        command.extend(["-loop", "1", "-framerate", str(FPS), "-i", sticker])
        label = f"sticker{index}"
        filter_parts.append(f"[{index}:v:0]scale=200:200,format=rgba[{label}]")
        sticker_labels.append(label)

    current_label = "base"
    filter_parts.append("[0:v:0]eq=brightness=0.03[base]")
    corners = ["0:0", f"{W}-w:0", f"0:{H}-h", f"{W}-w:{H}-h"]
    for index, sticker_label in enumerate(sticker_labels):
        next_label = f"overlay{index}"
        filter_parts.append(
            f"[{current_label}][{sticker_label}]"
            f"overlay={corners[index]}:shortest=1[{next_label}]"
        )
        current_label = next_label

    command.extend(
        [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            f"[{current_label}]",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            "-movflags",
            "+faststart",
            output_file,
        ]
    )
    run_command(command, "生成最终视频")


def main():
    base_dir_value = os.environ.get("TASK_WORKSPACE", "").strip()
    output_value = os.environ.get("CUSTOM_OUT_FILE", "").strip()
    if not base_dir_value:
        raise RuntimeError("未找到任务工作区环境变量 TASK_WORKSPACE。")
    if not output_value:
        raise RuntimeError("未找到输出文件路径环境变量 CUSTOM_OUT_FILE。")

    base_dir = Path(base_dir_value).resolve()
    output_file = Path(output_value).resolve()
    temp_dir = base_dir / "tmp_process_v17"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg = resolve_tool("V16_FFMPEG_EXE", "ffmpeg")
    ffprobe = resolve_tool("V16_FFPROBE_EXE", "ffprobe")
    print(f"使用 FFmpeg：{ffmpeg}", flush=True)
    print(f"使用 FFprobe：{ffprobe}", flush=True)

    core_videos = files_in(base_dir / "核心素材", VIDEO_EXTENSIONS)
    quality_videos = files_in(base_dir / "优质素材池", VIDEO_EXTENSIONS)
    stickers = files_in(base_dir / "贴纸素材池", (".png",))
    if not core_videos:
        raise RuntimeError("没有找到核心素材。")

    selected_videos = [core_videos[0]]
    total_duration = media_duration(ffprobe, core_videos[0])
    random.shuffle(quality_videos)
    for quality_video in quality_videos:
        if total_duration >= 180:
            break
        selected_videos.append(quality_video)
        total_duration += media_duration(ffprobe, quality_video)
    print(
        f"总计选择 {len(selected_videos)} 段素材，预估总长 {total_duration:.2f} 秒。",
        flush=True,
    )

    chunks = []
    for index, source in enumerate(selected_videos):
        chunk = temp_dir / f"chunk_{index}.ts"
        transcode_chunk(ffmpeg, ffprobe, source, chunk)
        if not chunk.is_file() or chunk.stat().st_size <= 0:
            raise RuntimeError(f"标准化素材没有生成有效文件：{source.name}")
        chunks.append(chunk)

    concat_file = temp_dir / "concat.txt"
    concat_file.write_text(
        "".join(f"file '{chunk.as_posix()}'\n" for chunk in chunks),
        encoding="utf-8",
    )
    base_video = temp_dir / "base_concat.ts"
    run_command(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_file,
            "-c",
            "copy",
            base_video,
        ],
        "拼接素材",
    )
    build_final(ffmpeg, base_video, stickers, output_file)
    if not output_file.is_file() or output_file.stat().st_size <= 0:
        raise RuntimeError("最终视频没有生成或文件为空。")
    print(f"V17 剪辑完成：{output_file}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"V17_ERROR：{error}", flush=True)
        traceback.print_exc()
        raise SystemExit(1)
