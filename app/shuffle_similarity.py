"""The handover's 48-sample dHash metric, applied to the rendered B-roll timeline."""
from pathlib import Path
import tempfile


FISSION_SIMILARITY_LIMIT = 40.0


def frame_hash_similarity_percent(first_hashes, second_hashes):
    pairs = min(len(first_hashes), len(second_hashes))
    if pairs <= 0:
        return 100.0
    raw_similarity = sum(
        (64 - (int(first_hashes[index]) ^ int(second_hashes[index])).bit_count()) / 64.0
        for index in range(pairs)
    ) / pairs
    calibrated = max(0.0, min(1.0, (raw_similarity - 0.5) / 0.5))
    return round(calibrated * 100.0, 2)


def gray_frame_hashes(raw):
    hashes = []
    for offset in range(0, len(raw) - 71, 72):
        value = 0
        for row in range(8):
            start = offset + row * 9
            for column in range(start, start + 8):
                value = (value << 1) | int(raw[column] > raw[column + 1])
        hashes.append(value)
    return hashes


def _decode_gray(path, output, ffmpeg, run, *, max_frames=None):
    # Keep one small frame per displayed picture. Scaling is stateless even when
    # a decoder changes colour metadata and FFmpeg reinitializes its filters.
    command = [str(ffmpeg), "-v", "error", "-nostdin", "-y", "-threads", "2", "-i", str(path),
               "-map", "0:v:0", "-an", "-vf", "scale=9:8:flags=area,format=gray",
               "-filter_threads", "1", "-fps_mode", "passthrough"]
    if max_frames is not None:
        command += ["-frames:v", str(max_frames)]
    run(command + ["-threads", "1", "-pix_fmt", "gray", "-f", "rawvideo", str(output)], output)


def source_shuffle_hashes(gray_track, frames, fps, ffmpeg, run):
    """Sample a verified uniform frame stream with the handover's fps filter."""
    duration = frames / fps
    if duration < 0.5:
        raise ValueError("批量制作时，每条至少需要 0.50 秒打乱画面才能验收相似度。")
    if gray_track.stat().st_size != frames * 72:
        raise ValueError("打乱轨的实际帧数与镜头记录不一致，未通过相似度验收。")
    with tempfile.TemporaryDirectory(prefix="8018-shuffle-hash-") as temp:
        output = Path(temp) / "sample.gray"
        run([str(ffmpeg), "-v", "error", "-nostdin", "-y", "-f", "rawvideo",
             "-pixel_format", "gray", "-video_size", "9x8", "-framerate", str(fps), "-i", str(gray_track),
             "-vf", f"fps={48 / duration:.8f}", "-filter_threads", "1", "-frames:v", "48",
             "-threads", "1", "-pix_fmt", "gray", "-f", "rawvideo", str(output)], output)
        raw = output.read_bytes()
    if len(raw) != 48 * 72:
        raise ValueError("无法读取完整的 48 帧打乱画面指纹，未通过相似度验收。")
    return gray_frame_hashes(raw)


def rendered_shuffle_hashes(path, plan, ffmpeg, run):
    fps = plan["fps"]
    spans = []
    for shot in plan["shots"]:
        if shot["role"] != "shuffle":
            continue
        start = round(shot["timeline_start_seconds"] * fps)
        if start < 0 or type(shot["frame_count"]) is not int or shot["frame_count"] <= 0 or (spans and start < spans[-1][1]):
            raise ValueError("打乱镜头时间或帧数无效，未通过相似度验收。")
        end = start + shot["frame_count"]
        if spans and start == spans[-1][1]:
            spans[-1] = (spans[-1][0], end)
        else:
            spans.append((start, end))
    frames = sum(end - start for start, end in spans)
    if frames / fps < 0.5:
        raise ValueError("批量制作时，每条至少需要 0.50 秒打乱画面才能验收相似度。")
    with tempfile.TemporaryDirectory(prefix="8018-rendered-shuffle-") as temp:
        decoded = Path(temp) / "output.gray"
        broll = Path(temp) / "track.gray"
        last_frame = spans[-1][1]
        _decode_gray(path, decoded, ffmpeg, run, max_frames=last_frame)
        if decoded.stat().st_size != last_frame * 72:
            raise ValueError("成片中实际读到的打乱帧数与镜头记录不一致，未通过相似度验收。")
        with decoded.open("rb") as source, broll.open("wb") as destination:
            for start, end in spans:
                source.seek(start * 72)
                destination.write(source.read((end - start) * 72))
        return source_shuffle_hashes(broll, frames, fps, ffmpeg, run)
