"""Recover the original 48-sample B-roll hashes saved by the legacy batch worker."""
import hashlib
import json
import math
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
import sqlite3
import subprocess
import tempfile

import numpy as np

from shuffle_similarity import frame_hash_similarity_percent


def legacy_saved_source_hashes(data_root, video):
    """Return authenticated pre-core-interleave track hashes, or None if unavailable."""
    root = Path(data_root).resolve()
    video = Path(video).resolve()
    run_dir = video.parent
    if run_dir.parent != root / "页面裂变成片":
        return None
    try:
        report = json.loads((run_dir / "裂变相似度报告.json").read_bytes())
        saved = json.loads((root / "排队任务成片" / run_dir.name / ".auto-fill.json").read_bytes())
        if (report["metric_version"] != "v17-shuffle-window-natural-clean-dhash-v6"
                or saved["run_id"] != run_dir.name or report["run_id"] != run_dir.name):
            return None
        names = report["outputs"]
        rows = saved["snapshot"]["accepted_hashes"]
        paths = saved["snapshot"]["accepted_paths"]
        files = saved["files"]
        if not len(rows) == len(names) == len(paths) == len(files) == report["video_count"]:
            return None
        if not all(isinstance(row, list) and len(row) == 48
                   and all(type(value) is int and 0 <= value < 2**64 for value in row) for row in rows):
            return None
        index = names.index(video.name)
        def output_key(path):
            return Path(str(path).replace("\\", "/")).parts[-3:]
        if not output_key(files[index]["path"]) == output_key(paths[index]) == output_key(video):
            return None
        for pair in report["pairs"]:
            first, second = pair["first"], pair["second"]
            if not (type(first) is int and type(second) is int and 1 <= first < second <= len(rows)):
                return None
            if frame_hash_similarity_percent(rows[first - 1], rows[second - 1]) != pair["similarity_percent"]:
                return None
        with video.open("rb") as media:
            if hashlib.file_digest(media, "sha256").hexdigest() != files[index]["sha256"]:
                return None
        return tuple(rows[index])
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None


def upper_similarity(candidate48, candidates):
    """Upper bound over every possible choice of one archived frame at each sample position."""
    points = np.asarray(candidate48, dtype=np.uint64)
    bank = np.asarray(candidates, dtype=np.uint64)
    if points.shape != (48,) or bank.ndim != 1 or not len(bank):
        raise ValueError("历史上界需要本条48个样本和完整的历史候选帧。")
    nearest = np.full(48, 64, dtype=np.uint8)
    for start in range(0, len(bank), 4096):
        differences = np.bitwise_xor(points[:, None], bank[None, start:start + 4096])
        np.minimum(nearest, np.bitwise_count(differences).min(axis=1), out=nearest)
    return round(max(0., 100 * (1 - 2 * int(nearest.sum()) / (48 * 64))), 2)


def _all_hashes(path, ffmpeg, run):
    from shuffle_similarity import _decode_gray
    with tempfile.TemporaryDirectory(prefix="8018-history-bound-") as temporary:
        out = Path(temporary) / "frames.gray"
        _decode_gray(path, out, ffmpeg, run)
        pixels = np.frombuffer(out.read_bytes(), dtype=np.uint8)
    if not len(pixels) or len(pixels) % 72:
        raise ValueError("历史视频未能完整解码")
    rows = pixels.reshape(-1, 8, 9)
    packed = np.packbits((rows[:, :, :-1] > rows[:, :, 1:]).reshape(-1, 64), axis=1)
    hashes = packed.view(">u8").ravel().astype(np.uint64)
    return hashes


@lru_cache(maxsize=24)
def _source_hashes(path, digest, expected_count, ffmpeg, run):
    # Scope reuse to identical verified source bytes and the supplied decoder.
    hashes = _all_hashes(path, ffmpeg, run)
    if len(hashes) != expected_count:
        raise ValueError(f"旧打乱来源“{path.name}”未能读取完整帧数")
    return np.unique(hashes)


def _overlapping_sequences(sequence, maximum=60):
    # The production comparator searches a 125%-length interval. An overlap of
    # (interval - 1) keeps every such interval inside at least one long window.
    span = math.ceil(1.25 * maximum)
    size = span * 2
    if len(sequence) <= size:
        return [sequence]
    starts = list(range(0, len(sequence) - size + 1, size - span + 1))
    if starts[-1] != len(sequence) - size:
        starts.append(len(sequence) - size)
    return [sequence[i:i + size] for i in starts]


def _scene_sequences(path, ffmpeg, ffprobe, run, count, viewport):
    from shuffle_scene import scene_frames
    from visual_shuffle import _scene_thumbnails
    probe = subprocess.check_output([str(ffprobe), "-v", "error", "-select_streams", "v:0",
                                     "-show_entries", "packet=pts_time", "-of", "json", str(path)],
                                    timeout=120)
    pts = np.array(sorted(float(x["pts_time"]) for x in json.loads(probe)["packets"]))
    if len(pts) != count or not np.isfinite(pts).all() or (np.diff(pts) <= 0).any():
        raise ValueError("历史视频的真实帧时间戳无法完整核对")
    # Match the post-encode verifier's actual RGB resize and crop, including its
    # OpenCV area resampling. Decode in batches so a long release uses bounded RAM.
    with tempfile.TemporaryDirectory(prefix="8018-legacy-scenes-") as temporary:
        out = Path(temporary) / "pictures.rgb"
        cmd = [str(ffmpeg), "-v", "error", "-nostdin", "-y", "-threads", "2", "-i", str(path),
               "-map", "0:v:0", "-an", "-vf", "scale=144:256:force_original_aspect_ratio=increase,crop=144:256,setsar=1",
               "-filter_threads", "1", "-fps_mode", "passthrough", "-threads", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", str(out)]
        run(cmd, out)
        if out.stat().st_size != count * 256 * 144 * 3:
            raise ValueError("历史成片的真实画面帧数不匹配")
        parts = []
        with out.open("rb") as stream:
            while data := stream.read(128 * 256 * 144 * 3):
                images = np.frombuffer(data, np.uint8).reshape(-1, 256, 144, 3)
                parts.append(scene_frames(_scene_thumbnails(images[:, :max(1, int(256 * viewport))])))
    pictures = np.concatenate(parts)
    stride = 0 if count < 2 else round(1 / (6 * np.median(np.diff(pts))))
    if stride > 0 and np.allclose(pts - pts[0], np.arange(count) / (6 * stride), atol=2e-6, rtol=0):
        return tuple(window for phase in range(min(stride, count))
                     for window in _overlapping_sequences(pictures[phase::stride]))
    # For VFR, anchor the six-Hz samples at every actual displayed picture.
    # Include preceding samples too, so a trailing cut does not become a bogus
    # one-frame reference when the rest of the historical video is available.
    offsets = np.arange(-75, 76) / 6
    sequences = []
    for start in pts:
        times = start + offsets
        times = times[(times >= pts[0] - 1e-8) & (times <= pts[-1] + 1e-8)]
        take = np.searchsorted(pts, times + 1e-8, side="right") - 1
        sequences.append(pictures[take])
    return tuple(sequences)


def _selected_sources(root, run_id, ffprobe):
    from contextlib import closing
    from shuffle_history import content_id
    database = root / ".v17_task_queue.sqlite3"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT config_json FROM task_queue WHERE task_id=?", (run_id,)).fetchone()
    if not row:
        raise ValueError("未找到旧任务的选材记录")
    config = json.loads(row[0])
    names = config["selected_materials"]["打乱素材池"]
    if not isinstance(names, list) or not names:
        raise ValueError("没有可核实的已选打乱来源")
    pool = root / "打乱素材池"
    trash = root / ".素材回收站" / "打乱素材池"
    frozen = root / ".v17_task_assets" / "sets" / run_id / "打乱素材池"
    old = defaultdict(list)
    for p in trash.glob("*/*"):
        if p.is_file():
            old[p.name].append(p)
    chosen = []
    duration = 0.
    for name in names:
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError("旧任务来源文件名无效")
        candidates = [p for p in (frozen / name, pool / name, *old.get(name, [])) if p.is_file()]
        unique = {content_id(p): p for p in candidates}
        if len(unique) != 1:
            raise ValueError(f"旧任务来源“{name}”缺失或同名文件内容不一致")
        digest, path = next(iter(unique.items()))
        probe = json.loads(subprocess.check_output(
            [str(ffprobe), "-v", "error", "-count_packets", "-select_streams", "v:0",
             "-show_entries", "format=duration:stream=nb_read_packets", "-of", "json", str(path)], timeout=90))
        duration += float(probe["format"]["duration"])
        chosen.append((path, digest, int(probe["streams"][0]["nb_read_packets"])))
    recorded = float(config["timeline_budgets"]["shuffle_source"])
    if not math.isclose(duration, recorded, rel_tol=0, abs_tol=.002):
        raise ValueError("恢复出的打乱来源时长与旧任务原选材不一致")
    return chosen


def legacy_bounds(data_root, video, ffmpeg, run):
    """Build conservative evidence only from the actual release and its verified selection."""
    from shuffle_history import HISTORY_VIEWPORT
    video = Path(video).resolve()
    root = Path(data_root).resolve()
    ffprobe = Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix)
    data = dict(source_candidates=None, rendered_candidates=None, scene_sequences=(),
                source_reason="", rendered_reason="")
    if video.parent.parent not in (root / "页面裂变成片", root / "排队任务成片"):
        raise ValueError("历史归档只支持正式成片目录")
    errors = (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError, RuntimeError)
    try:
        hashes = _all_hashes(video, ffmpeg, run)
        data["scene_sequences"] = _scene_sequences(video, ffmpeg, ffprobe, run, len(hashes), HISTORY_VIEWPORT)
        data["rendered_candidates"] = np.unique(hashes)
        data["rendered_reason"] = "归档成片全部真实视频帧，上方可见区按6 Hz覆盖每个起始帧相位"
    except errors as error:
        data["rendered_reason"] = str(error)
    if legacy_saved_source_hashes(root, video) is not None:
        data["source_reason"] = "已有与已交付成片绑定的原轨48帧指纹"
        return data
    try:
        sources = _selected_sources(root, video.parent.name, ffprobe)
        data["source_candidates"] = np.unique(np.concatenate([
            _source_hashes(path, digest, frames, ffmpeg, run) for path, digest, frames in sources]))
        data["source_reason"] = "原任务所选全部打乱源逐帧取样，来源一致且总时长与任务记录相符"
    except errors as error:
        data["source_reason"] = str(error)
    return data
