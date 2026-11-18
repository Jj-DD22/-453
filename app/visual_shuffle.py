"""Plan shared B-roll windows and verify the encoded tracks of each batch."""
from contextlib import contextmanager
from mmap import ACCESS_READ, mmap
from pathlib import Path
import random
import tempfile

import numpy as np
from shuffle_similarity import frame_hash_similarity_percent, rendered_shuffle_hashes


SAMPLE_RATE = 6


def _scene_thumbnails(frames):
    """Keep the original layout descriptors at 36×64 alongside the detailed scene samples."""
    import cv2
    return np.array([cv2.resize(frame, (36, 64), interpolation=cv2.INTER_AREA) for frame in frames])


def _decode_rgb(ffmpeg, run, path, stream, raw, *, spans=None, fps=None):
    cmd = [str(ffmpeg), "-v", "error", "-nostdin", "-y", "-threads", "2",
           "-i", str(path), "-map", f"0:{stream}", "-an"]
    filters = ["scale=144:256:force_original_aspect_ratio=increase", "crop=144:256", "setsar=1"]
    if spans is None:
        filters.insert(0, f"fps={SAMPLE_RATE}:round=near:eof_action=pass")
    else:
        # Decode displayed frames in order. Seeking to a cut can change which
        # colour matrix FFmpeg uses when adjoining clips have different metadata.
        # Stateless PTS selection saves only B-roll to disk and remains accurate
        # across colour changes. Half-frame margins include each exact CFR frame.
        runs = []
        for start, end in spans:
            if runs and runs[-1][1] == start:
                runs[-1] = (runs[-1][0], end)
            else:
                runs.append((start, end))
        ranges = [f"between(t,{(start-.5)/fps:.9f},{(end-.5)/fps:.9f})" for start, end in runs]
        filters.append("select='" + "+".join(ranges) + "'")
        cmd += ["-frames:v", str(sum(end-start for start,end in runs)), "-fps_mode", "passthrough"]
    cmd += ["-vf", ",".join(filters), "-filter_threads", "1", "-threads", "1",
            "-pix_fmt", "rgb24", "-f", "rawvideo", str(raw)]
    run(cmd, raw)


@contextmanager
def _source_frames(ffmpeg, run, path, *, stream):
    """Map a single source while extracting descriptors, then release its temporary file."""
    frame_bytes = 256 * 144 * 3
    with tempfile.TemporaryDirectory(prefix="8018-shuffle-frames-") as tmp:
        raw = Path(tmp)/"frames.rgb"
        _decode_rgb(ffmpeg, run, path, stream, raw)
        size = raw.stat().st_size
        if not size or size % frame_bytes:
            raise ValueError(f"无法检查打乱画面：{Path(path).name}")
        with raw.open("rb") as file, mmap(file.fileno(), 0, access=ACCESS_READ) as mapped:
            yield np.ndarray((size // frame_bytes, 256, 144, 3), dtype=np.uint8, buffer=mapped)


def _rendered_cuts(ffmpeg, run, path, spans, fps):
    """Read only the specified displayed shots from a single, frame-accurate decode."""
    frame_bytes = 256 * 144 * 3
    total = sum(end-start for start,end in spans)
    with tempfile.TemporaryDirectory(prefix="8018-rendered-scenes-") as tmp:
        raw = Path(tmp) / "frames.rgb"
        _decode_rgb(ffmpeg, run, path, "v:0", raw, spans=spans, fps=fps)
        if raw.stat().st_size != total * frame_bytes:
            raise ValueError("成片的实际镜头帧数与打乱时间轴不符，无法逐刀复核。")
        with raw.open("rb") as file:
            for start, end in spans:
                pixels = np.fromfile(file, dtype=np.uint8, count=(end-start)*frame_bytes)
                yield pixels.reshape(-1, 256, 144, 3)


class SimilarShuffleError(ValueError):
    """The candidate can be replaced without changing protected material."""


class VisualShuffleBatch:
    def __init__(self, ffmpeg, run, log, limit):
        self.ffmpeg, self.run, self.log = ffmpeg, run, log
        self.schedule = None
        self.signature = None
        self.windows = None
        self.rng = None
        self.durations_by_variant = None
        self.selected_indices = None
        self.reuse_stats_by_variant = None
        self.distinct_source_count = 0
        self.distinct_window_count = 0
        self.limit = limit
        self.fingerprints = []
        self.attempts = 0
        self.history = None

    def _row_stats(self, row, prior_orders):
        prior_used = {idx for old in prior_orders for idx in old}
        reused = sum(1 for idx in row if idx in prior_used)
        adj_same = sum(
            1
            for a, b in zip(row, row[1:])
            if (self.windows[a].get("source_identity") or self.windows[a]["source_path"])
            == (self.windows[b].get("source_identity") or self.windows[b]["source_path"])
        )
        return {"reused_cuts": reused, "adjacent_same_source_cuts": adj_same}

    def replace_rejected(self, index, tried=()):
        from shuffle_order import choose_window_order

        if self.durations_by_variant is None:
            return False
        count = len(self.durations_by_variant)
        if not 0 <= index < count or self.durations_by_variant[index] is None:
            for k in range(max(0, index), count):
                self.selected_indices[k] = None
                self.schedule[k] = None
                if self.reuse_stats_by_variant is not None:
                    self.reuse_stats_by_variant[k] = None
            return True
        slot_durations = self.durations_by_variant[index]
        slots = len(slot_durations)
        prior_orders = [row for row in self.selected_indices[:index] if row is not None]
        row = choose_window_order(
            self.windows, slots, self.rng, prior_orders, tried, slot_durations=slot_durations
        )
        if row is None:
            return False
        self.selected_indices[index] = row
        self.schedule[index] = [self.windows[i] for i in row]
        if self.reuse_stats_by_variant is not None:
            self.reuse_stats_by_variant[index] = self._row_stats(row, prior_orders)
        for k in range(index + 1, count):
            self.selected_indices[k] = None
            self.schedule[k] = None
            if self.reuse_stats_by_variant is not None:
                self.reuse_stats_by_variant[k] = None
        return True

    def select(self, sources, durations, plan, variant, count, seed):
        from shuffle_order import choose_window_order

        base_signature = (
            tuple((r["source_path"], r["start_seconds"], r["frame_count"]) for r in plan["shuffle_ranges"]),
            plan["fps"],
            count,
            seed,
        )
        if self.signature is None:
            self.signature = base_signature
            self.windows = [piece for pieces in sources.values() for piece in pieces]
            self.distinct_source_count = len(sources)
            self.distinct_window_count = len(self.windows)
            self.rng = random.Random(str(seed))
            self.durations_by_variant = [None] * count
            self.selected_indices = [None] * count
            self.schedule = [None] * count
            self.reuse_stats_by_variant = [None] * count
        elif base_signature != self.signature:
            raise ValueError("同批素材或打乱时段已变更，请重新制作。")

        slot_durations = tuple(durations)
        if self.durations_by_variant[variant] is not None and self.durations_by_variant[variant] != slot_durations:
            self.schedule[variant] = None
            self.selected_indices[variant] = None
        self.durations_by_variant[variant] = slot_durations
        if self.schedule[variant] is None:
            prior_orders = [row for row in self.selected_indices[:variant] if row is not None]
            row = choose_window_order(
                self.windows,
                len(slot_durations),
                self.rng,
                prior_orders,
                slot_durations=slot_durations,
            )
            if row is None:
                raise ValueError("没有可用的打乱片段编排方案。")
            self.selected_indices[variant] = row
            self.schedule[variant] = [self.windows[i] for i in row]
            stats = self._row_stats(row, prior_orders)
            self.reuse_stats_by_variant[variant] = stats
            self.log(
                f"第 {variant + 1}/{count} 条分配打乱片段：共 {len(self.windows)} 个候选窗口"
                f"（{self.distinct_source_count} 条母片），本条抽取 {len(slot_durations)} 刀"
                f"（跨条复用 {stats['reused_cuts']} 刀，同源相邻 {stats['adjacent_same_source_cuts']} 处）。"
            )
        return self.schedule[variant]

    def render_variant(self, index, render, cumulative_limit):
        tried = set()
        while True:
            self.attempts += 1
            try:
                return render()
            except SimilarShuffleError as error:
                self.log(str(error))
                tried.add(tuple(self.selected_indices[index]))
                if self.attempts >= cumulative_limit or not self.replace_rejected(index, tried):
                    raise ValueError(f"{error} 重新编排后仍未达到整段打乱画面的相似度要求。") from error
                self.log(f"为第 {index + 1} 条重新选取打乱镜头。")

    def verify(self, path, plan, *, rendered_hashes=None, source_hashes=None, overlays=(), destination=None):
        """Read the delivered MP4; compare B-roll against every prior delivered variant."""
        shots = [shot for shot in plan["shots"] if shot["role"] == "shuffle"]
        cut_count = sum(shot.get("fixed_cut_part_offset_frames", 0) == 0 for shot in shots)
        if not cut_count:
            raise ValueError("成片没有可核对的打乱镜头，未通过批量相似度验收。")
        if rendered_hashes is None:
            rendered_hashes = rendered_shuffle_hashes(path, plan, self.ffmpeg, self.run)
        source_method = "rendered_before_overlay_and_mask"
        if source_hashes is None:
            if any(layer["start"] < shot["timeline_start_seconds"] + shot["frame_count"] / plan["fps"]
                   and layer["end"] > shot["timeline_start_seconds"] for shot in shots for layer in overlays):
                raise ValueError("有底部叠图的打乱画面缺少叠图前的原画指纹。")
            source_hashes = rendered_hashes
            source_method = "same_encoded_track_without_overlay_or_mask"
        if len(rendered_hashes) != 48 or len(source_hashes) != 48:
            raise ValueError("纯打乱轨和成片打乱轨都需要完整的 48 帧指纹。")
        index = len(self.fingerprints) + 1
        pairs = []
        for prior, (old_source, old_rendered) in enumerate(self.fingerprints, 1):
            rendered_score = frame_hash_similarity_percent(old_rendered, rendered_hashes)
            if source_hashes is rendered_hashes and old_source is old_rendered:
                source_score = rendered_score
            else:
                source_score = frame_hash_similarity_percent(old_source, source_hashes)
            pairs.append({"first": prior, "second": index, "similarity_percent": max(source_score, rendered_score),
                          "source_similarity_percent": source_score, "rendered_similarity_percent": rendered_score})
        worst = max(pairs, key=lambda pair: pair["similarity_percent"], default=None)
        if worst and worst["similarity_percent"] > self.limit:
            raise SimilarShuffleError(f"第 {worst['first']}、{index} 条：纯打乱轨 "
                                     f"{worst['source_similarity_percent']:.2f}%，成片打乱轨 "
                                     f"{worst['rendered_similarity_percent']:.2f}%；相似度须均 ≤{self.limit:g}%。")
        history_check = {}
        if self.history is not None:
            from shuffle_history import history_scenes
            # Preserve per-cut evidence for later history reconstruction. Only
            # the two complete-track scores decide whether a variant is similar.
            scenes = history_scenes(path, plan, self.ffmpeg, self.run)
            history_check = self.history.accept(path, plan, index, source_hashes, rendered_hashes,
                                                scenes, destination, SimilarShuffleError)
        self.fingerprints.append((source_hashes, rendered_hashes))
        if worst:
            self.log(f"第 {index} 条与前 {index-1} 条核对，最高打乱轨相似度 {worst['similarity_percent']:.2f}%（上限 {self.limit:g}%）。")
        return {**history_check, "cuts_checked":cut_count,"earlier_variants_checked":len(pairs),
                "shot_reuse_policy":"allowed_subject_to_track_similarity",
                "scene_pairs_checked":0,"min_cross_variant_distance":None,"separate_source_sets":False,
                "threshold_percent":self.limit,"sample_count":len(rendered_hashes),
                "source_sample_count":len(source_hashes),"source_sampling_method":source_method,
                "pre_overlay_dhashes":[f"{value:016x}" for value in source_hashes],
                "rendered_sampling_method":"encoded_shuffle_frames_at_equal_positions",
                "metric":"calibrated_dhash_48_max_of_source_and_rendered","pair_results":pairs}
