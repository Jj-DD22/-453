"""Independent, audio-first local edit; no AI calls and no legacy effects.

The original opening/core audio runs continuously. B-roll changes pictures only.
All subprocess mutations can be delegated to the caller's cancellable ``run``.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import wave


def _duration_frames(seconds, fps):
    # ffprobe commonly rounds duration to six decimal places. At 30 fps,
    # 34.866667 s means exactly 1046 frames, not 1047. A 0.001-frame tolerance
    # covers that metadata rounding while still padding genuinely partial frames.
    return max(1, math.ceil(float(seconds) * fps - 1e-3))


def _probe(path, ffprobe):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or any(c in str(path) for c in "\r\n\x00"):
        raise ValueError("素材路径无效")
    result = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, timeout=40, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise ValueError(f"无法读取素材：{path.name}")
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio" and s.get("disposition", {}).get("default")), None)
    audio = audio or next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ValueError(f"素材没有视频轨道：{path.name}")
    def seconds(value):
        try:
            value = float(value or 0)
            return value if math.isfinite(value) and value > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0
    video_duration = seconds(video.get("duration"))
    audio_duration = seconds((audio or {}).get("duration"))
    container_duration = seconds(data.get("format", {}).get("duration"))
    duration = max(video_duration, audio_duration)
    duration = duration or container_duration
    if duration <= 0:
        raise ValueError(f"素材时长无效：{path.name}")
    return {
        "source_path": str(path), "duration_seconds": duration,
        "video_duration_seconds": video_duration or duration,
        "audio_duration_seconds": audio_duration,
        "container_duration_seconds": container_duration or duration,
        "video_stream_count": sum(s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic") for s in streams),
        "audio_stream_count": sum(s.get("codec_type") == "audio" for s in streams),
        "video_stream_index": int(video["index"]),
        "container_frame_count": int(video["nb_frames"]) if str(video.get("nb_frames", "")).isdigit() else None,
        "has_audio": audio is not None,
        "audio_stream_index": int(audio["index"]) if audio else None,
        "audio_channels": int(audio.get("channels", 0)) if audio else 0,
    }


def _selected_core_loop_sources(audio_sources, opening_count, body_duration):
    """Repeat selected core sound in order to an exact 48 kHz sample budget."""
    intro = list(audio_sources[:opening_count])
    core = list(audio_sources[opening_count:])
    for source in core:
        if not source["has_audio"]:
            raise ValueError(f"核心素材“{Path(source['source_path']).name}”没有音轨，无法用核心原声覆盖打乱画面")
        start = float(source.get("chosen_start_seconds", 0.0))
        audio_duration = float(source.get("audio_duration_seconds", 0.0))
        if audio_duration > 0 and start >= audio_duration:
            raise ValueError(f"核心素材“{Path(source['source_path']).name}”的所选区间没有可用原声")
    lengths = [max(1, round(source["duration_seconds"] * 48000)) for source in core]
    cycle_samples = sum(lengths)
    if not cycle_samples:
        raise ValueError("没有可用于循环的核心原声")
    remaining = round(body_duration * 48000)
    result = intro
    iteration = 0
    while remaining:
        for index, (source, length) in enumerate(zip(core, lengths)):
            samples = min(length, remaining)
            entry = dict(source)
            start = float(source.get("chosen_start_seconds", 0.0))
            end = float(source.get("chosen_end_seconds", source["original_duration_seconds"]))
            entry.update(role="core_original" if iteration == 0 else "core_repeat",
                         loop_iteration=iteration, core_audio_index=index,
                         duration_seconds=samples / 48000, sample_count=samples,
                         source_start_seconds=start,
                         source_end_seconds=min(end, start + samples / 48000))
            result.append(entry)
            remaining -= samples
            if not remaining:
                break
        iteration += 1
    return result, cycle_samples / 48000, iteration


def _append_shuffle_plan(core, shuffle, opening, *, core_ranges, shuffle_ranges, cut_seconds,
                         width, height, fps, seed, audio_channels, shuffle_budget_seconds,
                         core_audio_policy, fixed_shuffle_cuts=False):
    """Keep selected core picture/audio together, then append silent cuts."""
    if core_ranges is None:
        core_ranges = []
    if not isinstance(core_ranges, (list, tuple)) or len(core_ranges) > len(core):
        raise ValueError("核心素材的裁剪范围须与所选素材顺序对应")
    if shuffle_ranges is None:
        shuffle_ranges = []
    if not isinstance(shuffle_ranges, (list, tuple)) or len(shuffle_ranges) > len(shuffle):
        raise ValueError("打乱素材的裁剪范围须与所选素材顺序对应")
    requested_budget = None
    if shuffle_budget_seconds is not None:
        try:
            requested_budget = float(shuffle_budget_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("打乱素材时长预算必须是有效的非负秒数") from exc
        if isinstance(shuffle_budget_seconds, bool) or not math.isfinite(requested_budget) or requested_budget < 0:
            raise ValueError("打乱素材时长预算必须是有效的非负秒数")
    cut_frames = max(1, round(cut_seconds * fps))
    audio_sources, shots, selected_ranges, selected_core_ranges, pieces = [], [], [], [], []
    timeline = 0
    core_index = 0
    for source in ([opening] if opening else []) + core:
        role = "opening" if source is opening else "core"
        start, end = 0.0, source["duration_seconds"]
        selected = None
        if role == "core":
            selected = core_ranges[core_index] if core_index < len(core_ranges) else None
            if selected is not None:
                if not isinstance(selected, (list, tuple)) or len(selected) != 2 or any(isinstance(v, bool) for v in selected):
                    raise ValueError(f"第 {core_index + 1} 条核心素材的起止秒数无效")
                try:
                    start, end = float(selected[0]), float(selected[1])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"第 {core_index + 1} 条核心素材的起止秒数无效") from exc
                maximum = source["duration_seconds"]
                reported_maximum = max(maximum, source["container_duration_seconds"])
                if (not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start
                        or start >= source["video_duration_seconds"] or end > reported_maximum + 1e-6):
                    raise ValueError(f"第 {core_index + 1} 条核心素材起点须在画面内，终点不能超过 {reported_maximum:g} 秒")
                end = min(end, maximum)
            core_index += 1
        frames = _duration_frames(end - start, fps)
        shot = {"index": len(shots), "role": role, "source_path": source["source_path"],
                      "source_start_seconds": start, "duration_seconds": frames / fps,
                      "frame_count": frames, "timeline_start_seconds": timeline / fps,
                      "core_source_path": source["source_path"] if role == "core" else None,
                      "core_source_start_seconds": start if role == "core" else None,
                      "picture_fit": "contain"}
        if selected is not None:
            shot["source_end_seconds"] = end
        shots.append(shot)
        timeline += frames
        audio_source = dict(source)
        audio_source.update(original_duration_seconds=source["duration_seconds"], duration_seconds=frames / fps,
                            chosen_start_seconds=start, chosen_end_seconds=end)
        if selected is not None:
            audio_source.update(source_start_seconds=start, source_end_seconds=end)
        audio_sources.append(audio_source)
        if role == "core":
            selected_core_ranges.append({"source_path": source["source_path"],
                "source_duration_seconds": source["duration_seconds"],
                "container_duration_seconds": source["container_duration_seconds"],
                "chosen_start_seconds": start, "chosen_end_seconds": end,
                "start_seconds": start, "end_seconds": end,
                "duration_seconds": frames / fps, "frame_count": frames})
    prefix_frames = timeline
    for index, source in enumerate(shuffle):
        maximum = source["video_duration_seconds"]
        # The page reports container duration, which can include AAC padding
        # beyond the last picture. Accept that legitimate full-length choice,
        # but never request frames beyond the actual video stream.
        reported_maximum = max(maximum, source["container_duration_seconds"])
        selected = shuffle_ranges[index] if index < len(shuffle_ranges) else None
        if selected is None:
            start, end = 0.0, maximum
        else:
            if not isinstance(selected, (list, tuple)) or len(selected) != 2 or any(isinstance(v, bool) for v in selected):
                raise ValueError(f"第 {index + 1} 条打乱素材的起止秒数无效")
            try:
                start, end = float(selected[0]), float(selected[1])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"第 {index + 1} 条打乱素材的起止秒数无效") from exc
            if (not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start
                    or start >= maximum or end > reported_maximum + 1e-6):
                raise ValueError(f"第 {index + 1} 条打乱素材起点须在画面内，终点不能超过 {reported_maximum:g} 秒")
            end = min(end, maximum)
        frames = _duration_frames(end - start, fps)
        selected_ranges.append({"source_path": source["source_path"], "start_seconds": start,
                                "end_seconds": end, "frame_count": frames, "duration_seconds": frames / fps})
        if fixed_shuffle_cuts:
            frames = max(0, math.floor((end - start) * fps + 1e-3))
            frames = frames // cut_frames * cut_frames
            selected_ranges[-1].update(frame_count=frames, duration_seconds=frames / fps)
        for cursor in range(0, frames, cut_frames):
            part_frames = min(cut_frames, frames - cursor)
            pieces.append({"role": "shuffle", "source_path": source["source_path"],
                           "source_start_seconds": start + cursor / fps,
                           "source_end_seconds": min(end, start + (cursor + part_frames) / fps),
                           "duration_seconds": part_frames / fps, "frame_count": part_frames,
                           "core_source_path": None, "core_source_start_seconds": None,
                           "picture_fit": "cover", "range_index": index})
    random.Random(str(seed)).shuffle(pieces)
    available_frames = sum(piece["frame_count"] for piece in pieces)
    budget_frame_adjustment = 0.0
    if requested_budget is not None:
        required_frames = 0 if requested_budget == 0 else _duration_frames(requested_budget, fps)
        if required_frames > available_frames:
            # The web budget can include a container's tiny AAC tail while
            # picture ranges end at the last real video frame. Accept at most
            # one frame of that rounding difference by using actual inventory.
            if requested_budget - available_frames / fps <= 1 / fps + 1e-6:
                required_frames = available_frames
            else:
                raise ValueError(
                    f"所选打乱素材可用 {available_frames / fps:.3f} 秒，倍率需要 {requested_budget:.3f} 秒；"
                    "请增加打乱素材、扩大截取范围或降低倍率"
                )
        remaining = required_frames
        selected_pieces = []
        for piece in pieces:
            if remaining <= 0:
                break
            selected_piece = dict(piece)
            if selected_piece["frame_count"] > remaining:
                selected_piece["frame_count"] = remaining
                selected_piece["duration_seconds"] = remaining / fps
                selected_piece["source_end_seconds"] = min(
                    selected_piece["source_end_seconds"],
                    selected_piece["source_start_seconds"] + remaining / fps,
                )
                selected_piece["budget_trimmed"] = True
            selected_pieces.append(selected_piece)
            remaining -= selected_piece["frame_count"]
        pieces = selected_pieces
        budget_frame_adjustment = required_frames / fps - requested_budget
    for piece in pieces:
        piece.update(index=len(shots), timeline_start_seconds=timeline / fps)
        shots.append(piece)
        timeline += piece["frame_count"]
    shuffle_frames = timeline - prefix_frames
    core_audio_repeated = False
    core_audio_cycle_seconds = 0.0
    core_audio_iterations = 1
    if core_audio_policy == "selected_core_loop":
        opening_frames = opening["frame_count"] if opening else 0
        audio_sources, core_audio_cycle_seconds, core_audio_iterations = _selected_core_loop_sources(
            audio_sources, 1 if opening else 0, (timeline - opening_frames) / fps,
        )
        core_audio_repeated = core_audio_iterations > 1
    elif shuffle_frames:
        audio_sources.append({"source_path": None, "has_audio": False, "audio_stream_index": None,
                              "audio_channels": 0, "role": "shuffle_silence", "frame_count": shuffle_frames,
                              "duration_seconds": shuffle_frames / fps})
    all_sources = core + shuffle + ([opening] if opening else [])
    return {
        "version": "original-audio-append-shuffle-v1", "visual_policy": "append_shuffle",
        "fps": fps, "width": width, "height": height, "cut_seconds": cut_seconds,
        "effective_cut_seconds": cut_frames / fps,
        "output_duration_seconds": timeline / fps, "duration_seconds": timeline / fps,
        "opening_duration_seconds": opening["frame_count"] / fps if opening else 0.0,
        "core_duration_seconds": sum(item["frame_count"] for item in selected_core_ranges) / fps,
        "core_ranges": selected_core_ranges,
        "shuffle_duration_seconds": shuffle_frames / fps,
        "shuffle_budget_seconds": requested_budget,
        "shuffle_available_duration_seconds": available_frames / fps,
        "shuffle_budget_frame_adjustment_seconds": budget_frame_adjustment,
        "short_core_adjustment": False, "short_core_note": "",
        "selected_shuffle_paths": [source["source_path"] for source in shuffle],
        "shuffle_ranges": selected_ranges, "shuffle_repeated": False,
        "sources": {source["source_path"]: source for source in all_sources},
        "audio_sources": audio_sources, "shots": shots, "output_audio_channels": audio_channels,
        "core_audio_policy": core_audio_policy,
        "core_audio_repeated": core_audio_repeated,
        "core_audio_cycle_seconds": core_audio_cycle_seconds,
        "core_audio_iterations": core_audio_iterations,
        "audio_policy": (
            "opening-original-once; selected-core-original-in-order-looped-across-entire-body; no-shuffle-source-audio; no-speed-or-volume-change"
            if core_audio_policy == "selected_core_loop" else
            "opening-original-then-selected-core-original-then-shuffle-silence; synchronized-core-time-ranges; no-speed-or-volume-change"
        ),
        "crop_policy": "opening-and-core-contain; shuffle-center-fill",
    }


ALLOWED_CORE_DISPLAY_MODES = {"video_audio", "audio_only", "video_only", "trimmed_video_audio"}
PICTURE_CORE_DISPLAY_MODES = {"video_audio", "video_only", "trimmed_video_audio"}
AUDIO_CORE_DISPLAY_MODES = {"video_audio", "audio_only", "trimmed_video_audio"}


def _validate_core_range_item(source, core_index, selected):
    maximum = source["duration_seconds"]
    if selected is None:
        return 0.0, maximum, False
    if not isinstance(selected, (list, tuple)) or len(selected) != 2 or any(isinstance(v, bool) for v in selected):
        raise ValueError(f"第 {core_index + 1} 条核心素材的起止秒数无效")
    try:
        start, end = float(selected[0]), float(selected[1])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"第 {core_index + 1} 条核心素材的起止秒数无效") from exc
    reported_maximum = max(maximum, source["container_duration_seconds"])
    if (not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start
            or start >= source["video_duration_seconds"] or end > reported_maximum + 1e-6):
        raise ValueError(f"第 {core_index + 1} 条核心素材起点须在画面内，终点不能超过 {reported_maximum:g} 秒")
    end = min(end, maximum)
    return start, end, True


def _kept_frame_span(source, start, end, fps):
    total = source["frame_count"]
    start_frame = min(total - 1, max(0, round(start * fps)))
    end_frame = min(total, max(start_frame + 1, round(end * fps)))
    if start == 0 and end >= source["duration_seconds"] - 1e-6:
        start_frame, end_frame = 0, total
    return start_frame, end_frame


def _core_display_modes_plan(core, shuffle, opening, *, core_display_modes, core_ranges, shuffle_ranges,
                            cut_seconds, width, height, fps, seed, audio_channels,
                            primary_shuffle_budget_seconds=None, fixed_shuffle_cuts=False, loop_first_broll=False):
    """Explicit audio-only cores never emit their original picture; video-only cores never emit audio."""
    if (not isinstance(core_display_modes, (list, tuple)) or len(core_display_modes) != len(core)
            or any(not isinstance(mode, str) or mode not in ALLOWED_CORE_DISPLAY_MODES for mode in core_display_modes)):
        raise ValueError("核心显示选项必须与所选核心顺序对应，且每条须选择有效的音画模式")
    if core_ranges is not None and (not isinstance(core_ranges, (list, tuple)) or len(core_ranges) > len(core)):
        raise ValueError("核心素材的裁剪范围须与所选核心顺序对应")
    plan = _append_shuffle_plan(core, shuffle, opening, core_ranges=None, shuffle_ranges=shuffle_ranges,
                               cut_seconds=cut_seconds, width=width, height=height, fps=fps, seed=seed,
                               audio_channels=audio_channels, shuffle_budget_seconds=None,
                               core_audio_policy="legacy_selected_then_silence", fixed_shuffle_cuts=fixed_shuffle_cuts)
    inventory = [dict(shot) for shot in plan["shots"] if shot["role"] == "shuffle"]
    available = sum(shot["frame_count"] for shot in inventory)
    per_core_selections = []
    for idx, (source, mode) in enumerate(zip(core, core_display_modes)):
        raw_sel = (core_ranges[idx] if core_ranges and idx < len(core_ranges) else None) if mode in PICTURE_CORE_DISPLAY_MODES else None
        c_start, c_end, was_explicit = _validate_core_range_item(source, idx, raw_sel)
        c_frames = _duration_frames(c_end - c_start, fps)
        per_core_selections.append({
            "source_path": source["source_path"],
            "source_duration_seconds": source["duration_seconds"],
            "container_duration_seconds": source["container_duration_seconds"],
            "chosen_start_seconds": c_start,
            "chosen_end_seconds": c_end,
            "start_seconds": c_start,
            "end_seconds": c_end,
            "duration_seconds": c_frames / fps,
            "frame_count": c_frames,
            "_explicit": was_explicit,
        })
    shots = [dict(shot) for shot in plan["shots"] if shot["role"] == "opening"]
    cut_frames = max(1, round(cut_seconds * fps))
    cursor = opening["frame_count"] if opening else 0
    inventory_index = inventory_offset = repeat_cycle = 0
    shuffled_frames = solid_frames = kept_frames = fallback_frames = 0
    primary_used_frames = 0
    primary_budget_frames = (None if primary_shuffle_budget_seconds is None else
                             max(0, math.floor(min(primary_shuffle_budget_seconds * fps,
                                                   core[0]["frame_count"]) + 1e-3)))
    audio_only_frames = first_hole_frames = later_trimmed_hole_frames = 0
    details = []
    repeat_rng = random.Random("core-audio-only:" + str(seed))
    primary_pic_idx = next((i for i, m in enumerate(core_display_modes) if m in PICTURE_CORE_DISPLAY_MODES), 0)

    def append_core(source, core_index, mode, source_frame, count, timeline_frame, fallback=False,
                    fallback_reason="selected-shuffle-inventory-exhausted"):
        nonlocal fallback_frames
        shot = {"index": len(shots), "role": "core", "source_path": source["source_path"],
                "source_start_seconds": source_frame / fps, "duration_seconds": count / fps,
                "frame_count": count, "timeline_start_seconds": timeline_frame / fps,
                "core_source_path": source["source_path"], "core_source_start_seconds": source_frame / fps,
                "picture_fit": "contain", "core_index": core_index, "core_display_mode": mode}
        if source_frame or count != source["frame_count"]:
            shot["source_end_seconds"] = (source_frame + count) / fps
        if fallback:
            shot.update(core_picture_fallback=True, fallback_reason=fallback_reason)
            fallback_frames += count
        shots.append(shot)

    def fill_picture(source, core_index, mode, source_frame, count, timeline_frame, allow_repeat):
        nonlocal inventory_index, inventory_offset, repeat_cycle, shuffled_frames, solid_frames, primary_used_frames
        remaining, covered = count, 0
        while remaining:
            primary_slot = core_index == 0 and mode == "trimmed_video_audio"
            if primary_slot and primary_budget_frames is not None and primary_used_frames >= primary_budget_frames:
                append_core(source, core_index, mode, source_frame + covered, remaining,
                            timeline_frame + covered, fallback=True,
                            fallback_reason="primary-shuffle-budget-exhausted")
                break
            if not allow_repeat and (not inventory or inventory_index >= len(inventory)):
                append_core(source, core_index, mode, source_frame + covered, remaining,
                            timeline_frame + covered, fallback=True)
                break
            if not inventory:
                take = min(remaining, cut_frames)
                shot = {"role": "solid", "source_path": None, "source_start_seconds": 0.0,
                        "solid_color": "0x101014", "picture_fit": "solid"}
                solid_frames += take
            else:
                if inventory_index >= len(inventory):
                    repeat_cycle += 1
                    repeat_rng.shuffle(inventory)
                    inventory_index = inventory_offset = 0
                item = inventory[inventory_index]
                take = min(remaining, cut_frames, item["frame_count"] - inventory_offset)
                if primary_slot and primary_budget_frames is not None:
                    take = min(take, primary_budget_frames - primary_used_frames)
                shot = dict(item)
                start = item["source_start_seconds"] + inventory_offset / fps
                shot.update(source_start_seconds=start,
                            source_end_seconds=min(item["source_end_seconds"], start + take / fps),
                            shuffle_repeat_cycle=repeat_cycle)
                shuffled_frames += take
                if primary_slot:
                    primary_used_frames += take
                inventory_offset += take
                if inventory_offset == item["frame_count"]:
                    inventory_index += 1
                    inventory_offset = 0
            shot.update(index=len(shots), frame_count=take, duration_seconds=take / fps,
                        timeline_start_seconds=(timeline_frame + covered) / fps,
                        core_source_path=source["source_path"], core_source_start_seconds=(source_frame + covered) / fps,
                        core_display_mode=mode, core_index=core_index)
            shots.append(shot)
            remaining -= take
            covered += take

    for core_index, (source, mode) in enumerate(zip(core, core_display_modes)):
        total = source["frame_count"]
        sel = dict(per_core_selections[core_index])
        was_explicit = sel.pop("_explicit", False)
        start, end = _kept_frame_span(source, sel["chosen_start_seconds"], sel["chosen_end_seconds"], fps)
        is_trimmed_audio_core = mode in ("video_audio", "trimmed_video_audio") and (
            mode == "trimmed_video_audio" or was_explicit and (start > 0 or end < total)
        )
        if mode == "video_only":
            if loop_first_broll and core_index != primary_pic_idx:
                span_frames = 0
            else:
                span_frames = end - start
            detail = {"core_index": core_index, "source_path": source["source_path"], "mode": mode,
                      "timeline_start_seconds": cursor / fps, "duration_seconds": span_frames / fps,
                      "kept_start_seconds": start / fps, "kept_end_seconds": end / fps,
                      "picture_visible": span_frames > 0, "audio_full_once": False}
            details.append(detail)
            sel.update(kept_start_frame=start, kept_end_frame=end,
                       kept_picture_duration_seconds=span_frames / fps,
                       core_timeline_start_seconds=cursor / fps,
                       full_core_duration_seconds=total / fps, audio_trimmed=True)
            plan["core_ranges"][core_index] = sel
            if span_frames > 0:
                append_core(source, core_index, mode, start, span_frames, cursor)
                kept_frames += span_frames
                cursor += span_frames
            continue
        detail = {"core_index": core_index, "source_path": source["source_path"], "mode": mode,
                  "timeline_start_seconds": cursor / fps, "duration_seconds": total / fps,
                  "picture_visible": mode != "audio_only", "audio_full_once": True}
        details.append(detail)
        if mode == "video_audio" and not is_trimmed_audio_core:
            append_core(source, core_index, mode, 0, total, cursor)
            kept_frames += total
        elif is_trimmed_audio_core:
            sel.update(kept_start_frame=start, kept_end_frame=end,
                       kept_picture_duration_seconds=(end - start) / fps,
                       core_timeline_start_seconds=cursor / fps,
                       full_core_duration_seconds=total / fps, audio_trimmed=False)
            plan["core_ranges"][core_index] = sel
            detail.update(kept_start_seconds=start / fps, kept_end_seconds=end / fps)
            allow_rep = loop_first_broll if core_index == primary_pic_idx else True
            if start:
                fill_picture(source, core_index, mode, 0, start, cursor, allow_repeat=allow_rep)
            append_core(source, core_index, mode, start, end - start, cursor + start)
            if end < total:
                fill_picture(source, core_index, mode, end, total - end, cursor + end, allow_repeat=allow_rep)
            kept_frames += end - start
            hole = total - (end - start)
            if core_index == primary_pic_idx:
                first_hole_frames += hole
            else:
                later_trimmed_hole_frames += hole
        else:
            fill_picture(source, core_index, mode, 0, total, cursor, allow_repeat=True)
            audio_only_frames += total
        cursor += total
    opening_count = 1 if opening else 0
    raw_audio_sources = [dict(source) for source in plan["audio_sources"] if source.get("role") != "shuffle_silence"]
    filtered_audio_sources = raw_audio_sources[:opening_count] + [
        src for idx, src in enumerate(raw_audio_sources[opening_count:])
        if core_display_modes[idx] in AUDIO_CORE_DISPLAY_MODES
    ]
    total_hole_frames = audio_only_frames + first_hole_frames + later_trimmed_hole_frames
    plan.update({
        "version": "full-core-display-modes-v2", "visual_policy": "full_core_timeline",
        "core_display_modes": list(core_display_modes), "core_display_mode_details": details,
        "duration_seconds": cursor / fps, "output_duration_seconds": cursor / fps,
        "shots": shots, "core_kept_picture_duration_seconds": kept_frames / fps,
        "core_audio_only_duration_seconds": audio_only_frames / fps,
        "first_core_picture_hole_duration_seconds": first_hole_frames / fps,
        "primary_shuffle_budget_seconds": primary_shuffle_budget_seconds,
        "primary_shuffle_effective_budget_seconds": (primary_budget_frames / fps if primary_budget_frames is not None else None),
        "primary_shuffle_used_duration_seconds": primary_used_frames / fps,
        "core_picture_hole_duration_seconds": total_hole_frames / fps,
        "full_core_picture_hole_duration_seconds": total_hole_frames / fps,
        "core_picture_fallback_duration_seconds": fallback_frames / fps, "core_fallback_duration_seconds": fallback_frames / fps,
        "shuffle_available_duration_seconds": available / fps,
        "shuffle_used_duration_seconds": shuffled_frames / fps,
        "shuffle_unused_duration_seconds": max(0, available - shuffled_frames) / fps,
        "shuffle_duration_seconds": shuffled_frames / fps, "shuffle_budget_seconds": shuffled_frames / fps,
        "shuffle_budget_source": "explicit-core-display-modes",
        "shuffle_budget_frame_adjustment_seconds": 0.0,
        "shuffle_repeated": repeat_cycle > 0, "shuffle_repeat_cycles": repeat_cycle,
        "solid_background_duration_seconds": solid_frames / fps,
        "black_picture_duration_seconds": solid_frames / fps,
        "audio_sources": filtered_audio_sources,
        "core_audio_policy": "full_core_once", "core_audio_repeated": False,
        "core_audio_iterations": 1,
        "core_audio_cycle_seconds": sum(core[i]["frame_count"] for i, m in enumerate(core_display_modes) if m in AUDIO_CORE_DISPLAY_MODES) / fps,
        "audio_policy": "opening-original-once; complete-untrimmed-core-original-in-order-once; display-mode-never-changes-audio; no-shuffle-source-audio; no-speed-or-volume-change",
    })
    return plan


def _full_core_timeline_plan(core, shuffle, opening, *, core_ranges, shuffle_ranges,
                             cut_seconds, width, height, fps, seed, audio_channels,
                             core_display_modes=None, primary_shuffle_budget_seconds=None,
                             core_audio_validation_count=None, fixed_shuffle_cuts=False, loop_first_broll=False):
    """Keep full core clocks/audio; selected picture windows leave B-roll holes."""
    audio_check_list = (
        [src for src, mode in zip(core, core_display_modes) if mode in AUDIO_CORE_DISPLAY_MODES]
        if core_display_modes is not None else core
    )
    for source in audio_check_list[:core_audio_validation_count]:
        if not source["has_audio"]:
            raise ValueError(f"核心素材“{Path(source['source_path']).name}”没有音轨，无法保留完整核心原声")
    if core_display_modes is not None:
        return _core_display_modes_plan(core, shuffle, opening, core_display_modes=core_display_modes,
                                       core_ranges=core_ranges, shuffle_ranges=shuffle_ranges, cut_seconds=cut_seconds,
                                       width=width, height=height, fps=fps, seed=seed,
                                       audio_channels=audio_channels,
                                       primary_shuffle_budget_seconds=primary_shuffle_budget_seconds,
                                       fixed_shuffle_cuts=fixed_shuffle_cuts, loop_first_broll=loop_first_broll)
    common = dict(shuffle_ranges=shuffle_ranges, cut_seconds=cut_seconds, width=width,
                  height=height, fps=fps, seed=seed, audio_channels=audio_channels,
                  core_audio_policy="legacy_selected_then_silence")
    # Reuse range validation and the existing once-only randomized inventory.
    # Neither temporary plan is rendered; the final plan owns the full clocks.
    complete = _append_shuffle_plan(core, shuffle, opening, core_ranges=None,
                                   shuffle_budget_seconds=None, **common)
    chosen = _append_shuffle_plan(core, shuffle, opening, core_ranges=core_ranges,
                                 shuffle_budget_seconds=0, **common)
    inventory = [dict(shot) for shot in complete["shots"] if shot["role"] == "shuffle"]
    clocks = []
    kept_ranges = []
    core_offset = opening["frame_count"] if opening else 0
    holes = 0
    for source, selected in zip(core, chosen["core_ranges"]):
        total = source["frame_count"]
        start = min(total - 1, max(0, round(selected["chosen_start_seconds"] * fps)))
        end = min(total, max(start + 1, round(selected["chosen_end_seconds"] * fps)))
        if selected["chosen_start_seconds"] == 0 and selected["chosen_end_seconds"] >= source["duration_seconds"] - 1e-6:
            start, end = 0, total
        for left, right, role in ((0, start, "shuffle"), (start, end, "core"), (end, total, "shuffle")):
            if right > left:
                clocks.append((source, core_offset + left, left, right - left, role))
                if role == "shuffle":
                    holes += right - left
        selected = dict(selected)
        selected.update(kept_start_frame=start, kept_end_frame=end,
                        kept_picture_duration_seconds=(end - start) / fps,
                        core_timeline_start_seconds=core_offset / fps,
                        full_core_duration_seconds=total / fps,
                        audio_trimmed=False)
        kept_ranges.append(selected)
        core_offset += total
    available = sum(shot["frame_count"] for shot in inventory)
    shots = [dict(shot) for shot in complete["shots"] if shot["role"] == "opening"]
    inventory_index = 0
    inventory_offset = 0
    def append_core_picture(source, timeline, source_frame, frames, fallback=False):
        shot = {"index": len(shots), "role": "core", "source_path": source["source_path"],
                "source_start_seconds": source_frame / fps,
                "duration_seconds": frames / fps, "frame_count": frames,
                "timeline_start_seconds": timeline / fps,
                "core_source_path": source["source_path"],
                "core_source_start_seconds": source_frame / fps, "picture_fit": "contain"}
        # Full default windows retain the original no-trim render contract.
        if source_frame or frames != source["frame_count"]:
            shot["source_end_seconds"] = (source_frame + frames) / fps
        if fallback:
            shot["core_picture_fallback"] = True
            shot["fallback_reason"] = "selected-shuffle-inventory-exhausted"
        shots.append(shot)
    for source, timeline, source_frame, frames, role in clocks:
        if role == "core":
            append_core_picture(source, timeline, source_frame, frames)
            continue
        remaining = frames
        filled = 0
        while remaining:
            if inventory_index >= len(inventory):
                # Insufficient B-roll never changes the full core clock or
                # original audio. Continue its picture at the same source time.
                append_core_picture(source, timeline + filled, source_frame + filled, remaining, fallback=True)
                break
            item = inventory[inventory_index]
            count = min(remaining, item["frame_count"] - inventory_offset)
            shot = dict(item)
            start_seconds = item["source_start_seconds"] + inventory_offset / fps
            shot.update(index=len(shots), source_start_seconds=start_seconds,
                        source_end_seconds=min(item["source_end_seconds"], start_seconds + count / fps),
                        frame_count=count, duration_seconds=count / fps,
                        timeline_start_seconds=(timeline + filled) / fps,
                        core_source_path=source["source_path"],
                        core_source_start_seconds=(source_frame + filled) / fps,
                        fills_core_picture_hole=True)
            shots.append(shot)
            remaining -= count
            filled += count
            inventory_offset += count
            if inventory_offset == item["frame_count"]:
                inventory_index += 1
                inventory_offset = 0
    complete.update({
        "version": "full-core-timeline-v1", "visual_policy": "full_core_timeline",
        "duration_seconds": core_offset / fps, "output_duration_seconds": core_offset / fps,
        "core_ranges": kept_ranges, "shots": shots,
        "core_kept_picture_duration_seconds": sum(item["kept_picture_duration_seconds"] for item in kept_ranges),
        "core_picture_hole_duration_seconds": holes / fps,
        "full_core_picture_hole_duration_seconds": holes / fps,
        "core_picture_fallback_duration_seconds": max(0, holes - available) / fps,
        "core_fallback_duration_seconds": max(0, holes - available) / fps,
        "shuffle_available_duration_seconds": available / fps,
        "shuffle_used_duration_seconds": min(holes, available) / fps,
        "shuffle_unused_duration_seconds": max(0, available - holes) / fps,
        "shuffle_duration_seconds": min(holes, available) / fps,
        "shuffle_budget_seconds": min(holes, available) / fps,
        "shuffle_budget_source": "selected-inventory-with-core-picture-fallback", "shuffle_budget_frame_adjustment_seconds": 0.0,
        "audio_sources": [dict(source) for source in complete["audio_sources"] if source.get("role") != "shuffle_silence"],
        "core_audio_policy": "full_core_once", "core_audio_repeated": False,
        "core_audio_iterations": 1, "core_audio_cycle_seconds": complete["core_duration_seconds"],
        "audio_policy": "opening-original-once; complete-untrimmed-core-original-in-order-once; visual-ranges-never-trim-audio; no-shuffle-source-audio; no-speed-or-volume-change",
    })
    return complete


def _timeline_audio_preflight(core, target_samples, lead_audio_start_seconds=0.0, *, fps=30):
    available = 0
    count = 0
    for idx, source in enumerate(core):
        if available >= target_samples:
            break
        raw_start = source.get("audio_start_seconds")
        start_sec = (
            float(lead_audio_start_seconds if idx == 0 else 0.0)
            if raw_start is None else float(raw_start or 0.0)
        )
        start_samples = max(0, round(start_sec * 48000))
        total_src_samples = round(float(source["audio_duration_seconds"]) * 48000)
        usable_samples = total_src_samples - start_samples
        if not source["has_audio"] or usable_samples <= 0:
            raise ValueError(f"核心素材“{Path(source['source_path']).name}”没有足够长可用原声音轨（或起点已超出原声长度）")
        if source.get("is_lead_core_audio") and source.get("lead_kept_frames"):
            min_kept_samples = round(int(source["lead_kept_frames"]) * 48000 / fps)
            if 0 < min_kept_samples - usable_samples <= round(0.25 * 48000):
                usable_samples = min_kept_samples
        available += usable_samples
        count += 1
    if available < target_samples:
        raise ValueError(f"核心原声只有 {available / 48000:.3f} 秒，目标主体需要 {target_samples / 48000:.3f} 秒；请降低倍率或增加有原声的核心素材")
    return count, available


def _limit_core_timeline(plan, core, requested_seconds, target_frames, metadata_samples):
    """Keep a picture prefix; actual original PCM later supplies its soundtrack."""
    fps = plan["fps"]
    opening_frames = round(plan["opening_duration_seconds"] * fps)
    total_frames = opening_frames + target_frames
    shots = []
    for original in plan["shots"]:
        start = round(original["timeline_start_seconds"] * fps)
        if start >= total_frames:
            break
        shot = dict(original)
        frames = min(shot["frame_count"], total_frames - start)
        if frames != shot["frame_count"]:
            shot["timeline_tail_cut"] = True
            if shot["source_path"] is not None:
                shot["source_end_seconds"] = min(
                    shot.get("source_end_seconds", float("inf")), shot["source_start_seconds"] + frames / fps)
        shot.update(index=len(shots), frame_count=frames, duration_seconds=frames / fps)
        shots.append(shot)
    def duration(predicate):
        return sum(s["frame_count"] for s in shots if predicate(s)) / fps
    shuffled = duration(lambda s: s["role"] == "shuffle")
    solid = duration(lambda s: s["role"] == "solid")
    fallback = duration(lambda s: s.get("core_picture_fallback", False))
    first_holes = duration(lambda s: s.get("core_index") == 0 and (s["role"] != "core" or s.get("core_picture_fallback")))
    audio_only = duration(lambda s: s.get("core_display_mode") == "audio_only")
    repeat_cycles = max((s.get("shuffle_repeat_cycle", 0) for s in shots), default=0)
    modes = plan.get("core_display_modes")
    audio_indices = (
        [i for i, m in enumerate(modes) if m in AUDIO_CORE_DISPLAY_MODES]
        if modes is not None else list(range(len(core)))
    )
    primary_idx = int(plan.get("primary_core_index", 0))
    lead_is_candidate_zero = (
        bool(plan.get("primary_selection_first"))
        and bool(audio_indices)
        and audio_indices[0] == primary_idx
    )
    lead_audio_start = float(plan.get("primary_selection_source_start_seconds") or 0.0) if lead_is_candidate_zero else 0.0
    lead_kept_frames = int(plan.get("primary_selection_frame_count") or 0) if lead_is_candidate_zero else 0
    details = []
    for original in plan.get("core_display_mode_details", []):
        detail = dict(original)
        start = round(detail["timeline_start_seconds"] * fps)
        original_frames = round(detail["duration_seconds"] * fps)
        included = max(0, min(original_frames, total_frames - start))
        is_synced_lead = lead_is_candidate_zero and detail.get("core_index") == primary_idx
        detail.update(
            source_full_duration_seconds=detail["duration_seconds"],
            duration_seconds=included / fps,
            included_in_output=included > 0 or detail.get("mode") in AUDIO_CORE_DISPLAY_MODES,
            picture_tail_cut=0 < included < original_frames,
            audio_full_once=False,
            audio_start_seconds=lead_audio_start if is_synced_lead else (0.0 if detail.get("mode") in AUDIO_CORE_DISPLAY_MODES else None),
            audio_policy=(
                "lead-core-synced-from-crop-start-then-tail-to-shuffle"
                if is_synced_lead
                else ("subsequent-full-untrimmed-core-audio-relay" if detail.get("mode") in AUDIO_CORE_DISPLAY_MODES else "muted-video-only")
            ) if modes is not None else "original-source-prefix-until-target",
        )
        details.append(detail)
    candidates = []
    for index, source in enumerate(core):
        is_lead_audio = index == 0 and lead_is_candidate_zero
        a_start = lead_audio_start if is_lead_audio else 0.0
        usable_dur = (
            max(0.0, float(source["audio_duration_seconds"]) - a_start)
            if is_lead_audio
            else float(source["audio_duration_seconds"])
        )
        item = dict(source)
        item.update(
            role="core_unpadded",
            core_audio_index=index,
            core_index=audio_indices[index] if index < len(audio_indices) else index,
            pad_audio=False,
            audio_start_seconds=a_start,
            is_lead_core_audio=is_lead_audio,
            usable_audio_duration_seconds=usable_dur,
            original_duration_seconds=source["duration_seconds"],
            duration_seconds=usable_dur,
        )
        if is_lead_audio:
            item["lead_kept_frames"] = lead_kept_frames
        candidates.append(item)
    opening_audio = plan["audio_sources"][:1] if opening_frames else []
    plan.update({
        "version": "core-original-audio-prefix-v1", "shots": shots,
        "core_timeline_seconds": requested_seconds,
        "core_timeline_duration_seconds": target_frames / fps,
        "full_core_source_duration_seconds": plan["core_duration_seconds"],
        "core_duration_seconds": target_frames / fps,
        "duration_seconds": total_frames / fps, "output_duration_seconds": total_frames / fps,
        "core_target_audio_samples": math.ceil(target_frames * 48000 / fps - 1e-6),
        "core_metadata_audio_available_samples": metadata_samples,
        "core_audio_candidates": candidates, "audio_sources": [*opening_audio, *candidates],
        "core_display_mode_details": details,
        "core_kept_picture_duration_seconds": duration(lambda s: s["role"] == "core" and not s.get("core_picture_fallback")),
        "core_audio_only_duration_seconds": audio_only,
        "first_core_picture_hole_duration_seconds": first_holes,
        "core_picture_hole_duration_seconds": first_holes + audio_only,
        "full_core_picture_hole_duration_seconds": first_holes + audio_only,
        "core_picture_fallback_duration_seconds": fallback, "core_fallback_duration_seconds": fallback,
        "shuffle_duration_seconds": shuffled, "shuffle_used_duration_seconds": shuffled,
        "shuffle_budget_seconds": shuffled,
        "shuffle_unused_duration_seconds": max(0, plan["shuffle_available_duration_seconds"] - shuffled),
        "shuffle_repeated": repeat_cycles > 0, "shuffle_repeat_cycles": repeat_cycles,
        "solid_background_duration_seconds": solid, "black_picture_duration_seconds": solid,
        "primary_shuffle_used_duration_seconds": duration(lambda s: s["role"] == "shuffle" and s.get("core_index") == 0),
        "core_audio_repeated": False, "core_audio_tail_may_be_cut": True,
        "core_audio_policy": "original_core_prefix", "core_audio_cycle_seconds": 0.0,
        "audio_policy": (
            "opening-original-once; lead-core-synced-from-crop-start-then-remaining-tail-to-shuffle; subsequent-cores-full-untrimmed-audio-relay-until-target; final-audio-tail-cut-allowed; no-core-audio-loop-or-speed-change; no-shuffle-source-audio"
            if lead_is_candidate_zero else
            "opening-original-once; original-core-audio-in-source-order-until-target; final-audio-tail-cut-allowed; no-core-audio-padding-or-loop-or-speed-change; no-shuffle-source-audio"
        ),
    })
    return plan


def _move_primary_selection_first(plan, selection_basis_seconds, selection_frames, primary_core_index=0):
    """Move only the primary picture window; original audio clocks stay intact."""
    primary_shots = [dict(shot) for shot in plan["shots"] if shot.get("core_index") == primary_core_index]
    kept = [shot for shot in primary_shots if shot["role"] == "core" and not shot.get("core_picture_fallback")]
    if len(kept) != 1 or kept[0]["frame_count"] != selection_frames:
        raise RuntimeError("首条选区画面与帧范围不一致，不能安全调整开场")
    selected = kept[0]
    original_selected_index = selected["index"]
    prefix = [dict(shot) for shot in plan["shots"] if shot["role"] == "opening"]
    body_rest = [
        dict(shot) for shot in plan["shots"]
        if shot["role"] != "opening" and shot["index"] != original_selected_index
    ]
    ordered = [selected, *body_rest]
    cursor = round(plan["opening_duration_seconds"] * plan["fps"])
    for shot in ordered:
        shot["original_timeline_start_seconds"] = shot["timeline_start_seconds"]
        shot["timeline_start_seconds"] = cursor / plan["fps"]
        if shot.get("core_index") == primary_core_index:
            shot["primary_selected_picture"] = shot["index"] == original_selected_index
        cursor += shot["frame_count"]
    plan["shots"] = [*prefix, *ordered]
    for index, shot in enumerate(plan["shots"]):
        shot["index"] = index
    plan["core_ranges"][primary_core_index]["selected_picture_timeline_start_seconds"] = plan["opening_duration_seconds"]
    if plan.get("core_display_mode_details") and primary_core_index < len(plan["core_display_mode_details"]):
        plan["core_display_mode_details"][primary_core_index]["selected_picture_timeline_start_seconds"] = plan["opening_duration_seconds"]
    plan.update(primary_selection_first=True,
                primary_core_index=primary_core_index,
                primary_selection_basis_seconds=selection_basis_seconds,
                primary_selection_raw_duration_seconds=selection_basis_seconds,
                primary_selection_frame_duration_seconds=selection_frames / plan["fps"],
                primary_selection_frame_count=selection_frames,
                primary_selection_timeline_start_seconds=plan["opening_duration_seconds"],
                primary_selection_body_start_seconds=0.0,
                primary_selection_source_start_seconds=selected["source_start_seconds"])
    return plan


def _compute_tier1_dedup_spec(
    *,
    fps,
    seed,
    unique_batch,
    variant_num,
    base_target_frames,
    selection_frames,
    usable_audio_sec,
    allow_tail_tempo=True,
    max_body_capacity_frames=None,
    min_body_frames_for_shuffle=None,
):
    """Compute staggered body duration, safe tail tempo, and Tier-1 render parameters."""
    batch_key = str(unique_batch[0]) if unique_batch else f"single:{seed}"
    v_zero = int(unique_batch[1]) if unique_batch else max(0, int(variant_num) - 1)
    b_count = int(unique_batch[2]) if unique_batch else 1
    dedup_seed = (
        int.from_bytes(
            hashlib.sha256(f"tier1-dedup:{batch_key}:{v_zero}".encode("utf-8")).digest()[:8],
            "big",
        )
        & 0x7FFFFFFF_FFFFFFFF
    )
    p_rng = random.Random(dedup_seed)

    base_shuffle_frames = max(0, base_target_frames - selection_frames)
    base_target_sec = base_target_frames / fps
    core_kept_sec = selection_frames / fps
    base_shuffle_sec = base_shuffle_frames / fps
    min_broll_trigger_frames = round(0.5 * fps)

    # 1. Choose initial post-core speech tempo (0.95..1.06, avoiding 1.000)
    if allow_tail_tempo and base_shuffle_frames >= min_broll_trigger_frames:
        tempo_rng = random.Random(f"tail-tempo:{batch_key}:{b_count}")
        base_tempos = [0.956, 0.976, 1.018, 1.036, 1.054]
        tempo_rng.shuffle(base_tempos)
        if b_count >= 2:
            raw_t = base_tempos[v_zero % len(base_tempos)] + p_rng.uniform(-0.004, 0.004)
        else:
            raw_t = (
                p_rng.uniform(0.954, 0.986)
                if p_rng.random() < 0.5
                else p_rng.uniform(1.014, 1.056)
            )
        tail_tempo = max(0.95, min(1.06, raw_t))
        if abs(tail_tempo - 1.0) < 0.01:
            tail_tempo = 0.982 if tail_tempo <= 1.0 else 1.018
    else:
        tail_tempo = 1.0

    # 2. Staggered total body duration with strict audio-safe clamping
    if base_shuffle_frames < min_broll_trigger_frames:
        target_frames = base_target_frames
    else:
        if b_count >= 2:
            span = (
                0.18 if b_count >= 5
                else 0.17 if b_count == 4
                else 0.16 if b_count == 3
                else 0.15
            )
            anchors = [-span + 2.0 * span * i / (b_count - 1) for i in range(b_count)]
            order_rng = random.Random(f"dur-order:{batch_key}:{b_count}")
            perm = list(range(b_count))
            order_rng.shuffle(perm)
            slot_rank = perm[v_zero % b_count]
            jit_rng = random.Random(f"dur-jit:{batch_key}:{v_zero}")
            jitter = jit_rng.choice((-1.0, 1.0)) * jit_rng.uniform(0.004, 0.012)
            rel_offset = anchors[slot_rank] + jitter
            u_norm = (slot_rank + 0.5 + jit_rng.uniform(-0.18, 0.18)) / b_count
        else:
            slot_rank = 0
            jit_rng = random.Random(f"dur-single:{batch_key}:{v_zero}")
            rel_offset = jit_rng.choice((-1.0, 1.0)) * jit_rng.uniform(0.02, 0.08)
            u_norm = min(0.9, max(0.1, 0.5 + rel_offset * 3.0))

        safety_sec = 0.12
        if allow_tail_tempo and (
            core_kept_sec + base_shuffle_sec * tail_tempo > usable_audio_sec - safety_sec
        ):
            # Reduce tempo into <=0.99 so B-roll consumes less raw audio than 1.0x
            avail_tail = max(0.0, usable_audio_sec - safety_sec - core_kept_sec)
            max_fit_tempo = avail_tail / max(1e-6, base_shuffle_sec)
            if max_fit_tempo >= 0.95:
                tail_tempo = max(0.95, min(0.985, max_fit_tempo - 0.003))
            else:
                tail_tempo = 0.96

        eff_tempo = tail_tempo if allow_tail_tempo else 1.0
        max_by_tempo_sec = core_kept_sec + max(
            0.0, (usable_audio_sec - safety_sec - core_kept_sec) / max(0.92, eff_tempo)
        )
        # Keep 1.0x preflight ceiling respected as well
        max_by_1x_sec = max(base_target_sec, usable_audio_sec - safety_sec)
        if max_by_1x_sec > usable_audio_sec:
            max_by_1x_sec = usable_audio_sec
        max_safe_target_sec = min(max_by_1x_sec, max_by_tempo_sec)
        if max_safe_target_sec < base_target_sec:
            max_safe_target_sec = min(usable_audio_sec, base_target_sec)
        if max_body_capacity_frames is not None:
            max_safe_target_sec = min(max_safe_target_sec, max_body_capacity_frames / fps)

        min_floor_sec = core_kept_sec + 0.65
        if min_body_frames_for_shuffle is not None:
            min_floor_sec = max(min_floor_sec, min_body_frames_for_shuffle / fps)
        min_safe_target_sec = max(min_floor_sec, base_target_sec * 0.80)
        if min_safe_target_sec > max_safe_target_sec:
            min_safe_target_sec = min(min_floor_sec, max_safe_target_sec)

        min_safe_frames = max(selection_frames + 1, math.ceil(min_safe_target_sec * fps - 1e-6))
        max_safe_frames = max(min_safe_frames, math.floor(max_safe_target_sec * fps + 1e-6))
        if max_body_capacity_frames is not None and max_body_capacity_frames > selection_frames:
            max_safe_frames = min(max_safe_frames, max_body_capacity_frames)
            min_safe_frames = min(min_safe_frames, max_safe_frames)

        if max_safe_target_sec < base_target_sec * 1.15:
            span_f = max_safe_frames - min_safe_frames + 1
            if b_count >= 2 and span_f >= b_count:
                r_idx = slot_rank % b_count
                b_lo = min_safe_frames + (span_f * r_idx) // b_count
                b_hi = min_safe_frames + (span_f * (r_idx + 1)) // b_count - 1
                target_frames = jit_rng.randint(b_lo, max(b_lo, b_hi))
            else:
                desired_sec = min_safe_target_sec + u_norm * max(
                    0.0, max_safe_target_sec - min_safe_target_sec
                )
                target_frames = max(
                    min_safe_frames, min(max_safe_frames, round(desired_sec * fps))
                )
        else:
            desired_sec = base_target_sec * (1.0 + rel_offset)
            target_frames = max(
                min_safe_frames, min(max_safe_frames, round(desired_sec * fps))
            )

    # 3. Staggered encoding & subtle audio/visual anti-duplication parameters
    enc_rng = random.Random(f"enc-stagger:{batch_key}")
    gop_pool = [24, 34, 44, 54, 64]
    crf_pool = [19, 20, 21, 22, 23]
    abr_pool = ["160k", "176k", "192k", "208k", "224k"]
    enc_rng.shuffle(gop_pool)
    enc_rng.shuffle(crf_pool)
    enc_rng.shuffle(abr_pool)

    gop_frames = gop_pool[v_zero % len(gop_pool)] + p_rng.randint(0, 5)
    video_crf = crf_pool[v_zero % len(crf_pool)]
    audio_bitrate = abr_pool[v_zero % len(abr_pool)]
    room_tone_seed = p_rng.randint(1, 2_000_000_000)
    room_tone_amp = p_rng.uniform(0.0028, 0.0042)
    eq_low_hz = p_rng.randint(200, 340)
    eq_low_gain = p_rng.choice((-1.0, 1.0)) * p_rng.uniform(0.5, 1.3)
    eq_high_hz = p_rng.randint(2400, 3900)
    eq_high_gain = p_rng.choice((-1.0, 1.0)) * p_rng.uniform(0.5, 1.4)
    screen_opacity = p_rng.uniform(0.065, 0.115)
    grain_strength = p_rng.randint(3, 5)
    overlay_scale_jitter = 1.0 + p_rng.choice((-1.0, 1.0)) * p_rng.uniform(0.008, 0.035)
    overlay_x_jitter_px = p_rng.choice((-1, 1)) * p_rng.randint(2, 10)
    overlay_y_jitter_px = p_rng.choice(
        tuple(v for v in range(-6, 9) if abs(v) >= 2)
    )

    dedup_meta = {
        "enabled": True,
        "version": "tier1-9items-v1",
        "variant_index": int(variant_num),
        "variant_seed": int(dedup_seed),
        "duration_scale": round(target_frames / max(1, base_target_frames), 4),
        "base_body_duration_seconds": round(base_target_frames / fps, 3),
        "staggered_body_duration_seconds": round(target_frames / fps, 3),
        "tail_audio_tempo": round(tail_tempo, 5),
        "gop_frames": int(gop_frames),
        "video_crf": int(video_crf),
        "audio_bitrate": audio_bitrate,
        "room_tone_seed": int(room_tone_seed),
        "room_tone_amp": round(room_tone_amp, 5),
        "eq_low_hz": int(eq_low_hz),
        "eq_low_gain": round(eq_low_gain, 2),
        "eq_high_hz": int(eq_high_hz),
        "eq_high_gain": round(eq_high_gain, 2),
        "screen_opacity": round(screen_opacity, 3),
        "grain_strength": int(grain_strength),
        "overlay_scale_jitter": round(overlay_scale_jitter, 4),
        "overlay_x_jitter_px": int(overlay_x_jitter_px),
        "overlay_y_jitter_px": int(overlay_y_jitter_px),
    }
    return target_frames, dedup_meta


def _jitter_run_cut_frames(total_frames, cut_frames, fps, rng):
    """Partition a contiguous B-roll run of ``total_frames`` into variable ~2-3s cuts."""
    n = int(total_frames)
    if n <= 0:
        return []
    if cut_frames >= round(1.8 * fps):
        min_cut = max(round(1.4 * fps), round(cut_frames * 0.68))
        max_cut = max(min_cut + 4, round(cut_frames * 1.12))
    else:
        min_cut = max(1, min(cut_frames, max(12, round(cut_frames * 0.80))))
        max_cut = max(min_cut + 2, round(cut_frames * 1.20))

    if n <= max_cut:
        return [n]
    if n < 2 * min_cut:
        delta = rng.randint(1, max(1, min(4, n // 8)))
        first = max(1, min(n - 1, n // 2 - delta))
        return [first, n - first]

    cuts = []
    rem = n
    prev_d = None
    while rem > 0:
        if rem <= max_cut:
            cuts.append(rem)
            break
        if rem <= 2 * max_cut:
            lo = max(min_cut, rem - max_cut)
            hi = min(max_cut, rem - min_cut)
            if lo <= hi:
                candidates = [
                    d for d in range(lo, hi + 1)
                    if (prev_d is None or abs(d - prev_d) >= 2) and abs(rem - 2 * d) >= 2
                ]
                d = rng.choice(candidates) if candidates else rng.randint(lo, hi)
                cuts.append(d)
                cuts.append(rem - d)
            else:
                half = rem // 2
                cuts.append(half)
                cuts.append(rem - half)
            break
        if rem < 3 * min_cut:
            d1 = rem // 3
            d2 = (rem - d1) // 2
            cuts.extend((d1, d2, rem - d1 - d2))
            break
        hi = min(max_cut, rem - 2 * min_cut)
        lo = min_cut
        d = rng.randint(lo, max(lo, hi))
        if prev_d is not None and hi - lo >= 4 and abs(d - prev_d) < 2:
            d = lo + ((d - lo + 3) % (hi - lo + 1))
        cuts.append(d)
        prev_d = d
        rem -= d
    return cuts


def _shared_shuffle_order(inventory, cut_seconds, rng, needed, variant, select=None, slot_durations=None):
    """Collapse duplicate inputs, then draw each output from the same stock."""
    from shuffle_order import choose_window_order
    by_path = {}
    for piece in inventory:
        by_path.setdefault(piece["source_path"], []).append(piece)
    sizes = {}
    for path in by_path:
        sizes.setdefault(os.path.getsize(path), []).append(path)
    sources = {}
    for paths in sizes.values():
        for path in paths:
            identity = path
            if len(paths) > 1:
                with open(path, "rb") as file:
                    identity = hashlib.file_digest(file, "sha256").hexdigest()
            sources.setdefault(identity, []).extend(by_path[path])
    for source, pieces in sources.items():
        unique = []
        end = -1
        for piece in sorted(pieces, key=lambda item: item["start"]):
            if piece["start"] >= end - 1e-8:
                tagged = dict(piece, source_identity=source)
                unique.append(tagged)
                end = piece["start"] + cut_seconds
        sources[source] = unique
    windows = [piece for pieces in sources.values() for piece in pieces]
    available = len(windows)
    if select is not None and needed:
        return select(sources), available
    orders = []
    for v_i in range(variant + 1):
        durs = slot_durations if v_i == variant else None
        orders.append(choose_window_order(windows, needed, rng, orders, slot_durations=durs))
    return [windows[index] for index in orders[-1]], available


def _fixed_shuffle_sequence(plan, seed, unique_batch=None, visual_batch=None, *, tier1_dedup=True, bottom_layers=None):
    """Fill continuous B-roll runs with whole cuts, never shuffled source tails.

    A cut can span a core audio-only boundary; parts retain their original
    timeline metadata while reading adjacent frames from the same picture.
    Only the end of a B-roll run or the final output may shorten a cut.
    """
    fps = plan["fps"]
    cut_frames = max(1, round(plan["cut_seconds"] * fps))
    dedup_active = bool(tier1_dedup)
    phase_key = str(unique_batch[0]) if unique_batch else str(seed)
    phase_rng = random.Random(f"src-phase:{phase_key}")
    inventory = []
    dropped_tails = 0.0
    any_phase_offset = False
    for index, item in enumerate(plan["shuffle_ranges"]):
        # Older plans kept source tails. They still define the original core
        # timeline, but no tail can be allocated as a full source cut.
        length = item["end_seconds"] - item["start_seconds"]
        raw_frames = max(0, math.floor(length * fps + 1e-3))
        if dedup_active:
            usable = (raw_frames // cut_frames) * cut_frames
            slack_frames = max(0, raw_frames - usable)
            if slack_frames < max(2, round(0.2 * fps)) and raw_frames >= max(round(15.0 * fps), 3 * cut_frames):
                usable -= cut_frames
                slack_frames = raw_frames - usable
            phase_offset = phase_rng.randint(1, slack_frames) if slack_frames >= 1 else 0
            if phase_offset > 0:
                any_phase_offset = True
        else:
            frames = min(item["frame_count"], raw_frames)
            usable = (frames // cut_frames) * cut_frames
            phase_offset = 0
        dropped_tails += max(0.0, length - usable / fps)
        for offset in range(0, usable, cut_frames):
            inventory.append({
                "source_path": item["source_path"],
                "start": item["start_seconds"] + (phase_offset + offset) / fps,
                "range_start_seconds": item["start_seconds"],
                "range_end_seconds": item["end_seconds"],
                "range_index": index,
                "phase_offset_frames": phase_offset,
            })

    rng = random.Random("fixed-shuffle:" + str(seed))
    output, originals = [], plan["shots"]
    run_specs = []
    pos_scan = 0
    while pos_scan < len(originals):
        if originals[pos_scan]["role"] != "shuffle":
            pos_scan += 1
            continue
        r_start_frame = round(originals[pos_scan]["timeline_start_seconds"] * fps)
        r_frames = 0
        while pos_scan < len(originals) and originals[pos_scan]["role"] == "shuffle":
            r_frames += originals[pos_scan]["frame_count"]
            pos_scan += 1
        run_specs.append((r_start_frame, r_frames))

    if dedup_active:
        cut_seed_key = (
            f"cut-jitter:{unique_batch[0]}:{unique_batch[1]}"
            if unique_batch
            else f"cut-jitter:single:{seed}"
        )
        cut_rng = random.Random(cut_seed_key)
        run_cut_lists = [_jitter_run_cut_frames(n, cut_frames, fps, cut_rng) for _, n in run_specs]
    else:
        run_cut_lists = [
            [min(cut_frames, n - offset) for offset in range(0, n, cut_frames)]
            for _, n in run_specs
        ]
    durations = [d for cuts in run_cut_lists for d in cuts]

    if visual_batch is not None and (bottom_layers or "shuffle_cut_top_fractions" in plan) and durations:
        if bottom_layers:
            from bottom_overlay import cut_top_fractions, layer_layouts
            first_end = _first_core_end(plan)
            layouts = [
                layout
                for _, _, layout in layer_layouts(
                    bottom_layers, plan["width"], plan["height"], plan["output_duration_seconds"], first_end
                )
            ]
            spans = []
            for (r_start, _), cuts in zip(run_specs, run_cut_lists):
                cur = r_start
                for d in cuts:
                    spans.append((cur, cur + d))
                    cur += d
            plan["shuffle_cut_top_fractions"] = cut_top_fractions(spans, layouts, fps)

    reused_cuts_count = 0
    if unique_batch:
        batch_seed, variant, count = unique_batch
        if not 0 <= variant < count:
            raise ValueError("打乱素材批次序号无效。")
        needed = len(durations)
        select = None
        if visual_batch is not None:
            def select(sources):
                return visual_batch.select(sources, durations, plan, variant, count, batch_seed)
        inventory, available = _shared_shuffle_order(
            inventory,
            cut_frames / fps,
            random.Random(str(batch_seed)),
            needed,
            variant,
            select,
            slot_durations=durations,
        )
        if plan["shuffle_ranges"] and not needed:
            if not available:
                raise ValueError("已选打乱素材没有完整的一刀，请缩短刀长或补充打乱素材。")
            raise ValueError("打乱画面为 0 秒：当前成片已排满保留的核心画面。按当前设置导出纯核心成片时，请取消勾选打乱素材。")
        if visual_batch is not None and needed:
            available = visual_batch.distinct_window_count
            plan["shuffle_distinct_source_files"] = visual_batch.distinct_source_count
            v_stats = (getattr(visual_batch, "reuse_stats_by_variant", None) or [None] * count)[variant]
            if v_stats:
                reused_cuts_count = int(v_stats.get("reused_cuts", 0))
        plan.update(
            shuffle_distinct_cuts_available=available,
            shuffle_distinct_cuts_used=len({(p["source_path"], p["start"]) for p in inventory}),
            shuffle_cuts_used=needed,
            shuffle_shared_pool=True,
        )
    else:
        if dedup_active and inventory and durations:
            from shuffle_order import choose_window_order
            order = choose_window_order(inventory, len(durations), rng, slot_durations=durations)
            if order is not None:
                inventory = [inventory[i] for i in order]
            else:
                rng.shuffle(inventory)
        else:
            rng.shuffle(inventory)

    position = inventory_index = repeat_cycle = group = 0
    run_idx = 0
    cut_source_sequence = []
    while position < len(originals):
        if originals[position]["role"] != "shuffle":
            output.append(dict(originals[position]))
            position += 1
            continue
        run = []
        while position < len(originals) and originals[position]["role"] == "shuffle":
            run.append(originals[position])
            position += 1
        if not inventory:
            raise RuntimeError("固定刀长没有足够长的打乱片段")
        remaining = sum(item["frame_count"] for item in run)
        current_cuts = run_cut_lists[run_idx] if run_idx < len(run_cut_lists) else []
        run_idx += 1
        cut_in_run = 0
        source_index = source_offset = 0
        while remaining:
            if inventory_index >= len(inventory):
                if unique_batch:
                    raise RuntimeError("打乱镜头计划与分配的时长不一致。")
                rng.shuffle(inventory)
                inventory_index = 0
                repeat_cycle += 1
            if unique_batch:
                repeat_cycle = inventory_index // max(1, available)
            piece = inventory[inventory_index]
            inventory_index += 1
            if dedup_active and cut_in_run < len(current_cuts):
                group_frames = min(current_cuts[cut_in_run], remaining)
            else:
                group_frames = min(cut_frames, remaining)
            cut_in_run += 1

            piece_start = float(piece["start"])
            broll_speed = 1.0
            dedup_shot_seed = None
            if dedup_active:
                shot_key = (
                    f"shot-dedup:{unique_batch[0]}:{unique_batch[1]}:{group}"
                    if unique_batch
                    else f"shot-dedup:single:{seed}:{group}"
                )
                dedup_shot_seed = (
                    int.from_bytes(hashlib.sha256(shot_key.encode("utf-8")).digest()[:8], "big")
                    & 0x7FFFFFFF
                )
                sp_rng = random.Random(dedup_shot_seed)
                broll_speed = (
                    sp_rng.uniform(0.90, 0.98)
                    if sp_rng.random() < 0.5
                    else sp_rng.uniform(1.02, 1.10)
                )
                src_info = plan.get("sources", {}).get(piece["source_path"], {})
                video_dur = float(
                    src_info.get("video_duration_seconds")
                    or src_info.get("duration_seconds")
                    or piece.get("range_end_seconds", piece_start + group_frames / fps)
                )
                range_start = float(piece.get("range_start_seconds", 0.0))
                range_end = float(piece.get("range_end_seconds", video_dur))
                max_src_end = min(range_end, video_dur)
                safe_end = max(range_start + 1.0 / fps, max_src_end - 0.02)
                cut_dur_sec = group_frames / fps
                required_span = cut_dur_sec * broll_speed
                if piece_start + required_span > safe_end:
                    fit_speed = (safe_end - piece_start) / max(1e-6, cut_dur_sec)
                    if fit_speed >= 0.90:
                        broll_speed = min(broll_speed, fit_speed)
                        if 0.985 < broll_speed < 1.015:
                            broll_speed = 0.98
                    else:
                        broll_speed = 0.90
                    required_span = cut_dur_sec * broll_speed
                    if piece_start + required_span > safe_end:
                        piece_start = max(range_start, safe_end - required_span)
                        if piece_start + required_span > max_src_end:
                            avail_span = max(1.0 / fps, max_src_end - piece_start)
                            broll_speed = max(0.5, avail_span / max(1e-6, cut_dur_sec))
                broll_speed = round(broll_speed, 5)

            cut_source_sequence.append(piece.get("source_identity") or piece["source_path"])
            group_offset = 0
            while group_offset < group_frames:
                template = run[source_index]
                take = min(group_frames - group_offset, template["frame_count"] - source_offset)
                shot = dict(template)
                start = piece_start + (group_offset / fps) * broll_speed
                end = piece_start + ((group_offset + take) / fps) * broll_speed
                shot.update(
                    source_path=piece["source_path"],
                    source_start_seconds=start,
                    source_end_seconds=end,
                    frame_count=take,
                    duration_seconds=take / fps,
                    range_index=piece["range_index"],
                    timeline_start_seconds=template["timeline_start_seconds"] + source_offset / fps,
                    shuffle_repeat_cycle=repeat_cycle,
                    fixed_cut_group=group,
                    fixed_cut_group_frame_count=group_frames,
                    fixed_cut_part_offset_frames=group_offset,
                )
                if dedup_active:
                    shot["broll_speed"] = broll_speed
                    shot["dedup_shot_seed"] = dedup_shot_seed
                if shot.get("core_source_start_seconds") is not None:
                    shot["core_source_start_seconds"] += source_offset / fps
                output.append(shot)
                source_offset += take
                group_offset += take
                if source_offset == template["frame_count"]:
                    source_index += 1
                    source_offset = 0
            remaining -= group_frames
            group += 1

    for index, shot in enumerate(output):
        shot["index"] = index
    plan.update(
        shots=output,
        fixed_shuffle_cuts=True,
        shuffle_cut_policy="fixed-duration-hard-cuts-no-natural-boundaries",
        shuffle_dropped_tail_seconds=dropped_tails,
        shuffle_repeated=repeat_cycle > 0,
        shuffle_repeat_cycles=repeat_cycle,
    )
    if dedup_active and durations:
        adj_same_cuts = sum(
            1 for a, b in zip(cut_source_sequence, cut_source_sequence[1:]) if a == b
        )
        if not isinstance(plan.get("dongge_tier1_dedup"), dict):
            body_frames = round(
                (plan["output_duration_seconds"] - plan.get("opening_duration_seconds", 0.0)) * fps
            )
            _, fallback_meta = _compute_tier1_dedup_spec(
                fps=fps,
                seed=seed,
                unique_batch=unique_batch,
                variant_num=(unique_batch[1] + 1) if unique_batch else int(plan.get("variant_index") or 1),
                base_target_frames=max(1, body_frames),
                selection_frames=int(plan.get("primary_selection_frame_count") or 0),
                usable_audio_sec=max(1.0, body_frames / fps),
                allow_tail_tempo=False,
            )
            fallback_meta["staggered_body_duration_seconds"] = round(body_frames / fps, 3)
            fallback_meta["duration_scale"] = 1.0
            plan["dongge_tier1_dedup"] = fallback_meta
        plan["dongge_tier1_dedup"].update(
            cut_count=len(durations),
            cut_min_seconds=round(min(durations) / fps, 3),
            cut_max_seconds=round(max(durations) / fps, 3),
            phase_offset_applied=any_phase_offset,
            adjacent_same_source_cuts=adj_same_cuts,
            cross_variant_reused_cuts=reused_cuts_count,
        )
    return plan


def _first_core_end(plan):
    if plan.get("primary_selection_first"):
        fps = plan["fps"]
        return (round(plan["opening_duration_seconds"] * fps) + plan["primary_selection_frame_count"]) / fps
    return None


def _build_shuffle_ranges_metadata(shuffle, shuffle_ranges, fps, cut_frames, fixed_shuffle_cuts):
    if shuffle_ranges is None:
        shuffle_ranges = []
    if not isinstance(shuffle_ranges, (list, tuple)) or len(shuffle_ranges) > len(shuffle):
        raise ValueError("打乱素材的裁剪范围须与所选素材顺序对应")
    selected_ranges = []
    available_frames = 0
    for index, source in enumerate(shuffle):
        maximum = source["video_duration_seconds"]
        reported_maximum = max(maximum, source["container_duration_seconds"])
        selected = shuffle_ranges[index] if index < len(shuffle_ranges) else None
        if selected is None:
            start, end = 0.0, maximum
        else:
            if not isinstance(selected, (list, tuple)) or len(selected) != 2 or any(isinstance(v, bool) for v in selected):
                raise ValueError(f"第 {index + 1} 条打乱素材的起止秒数无效")
            try:
                start, end = float(selected[0]), float(selected[1])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"第 {index + 1} 条打乱素材的起止秒数无效") from exc
            if (not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start
                    or start >= maximum or end > reported_maximum + 1e-6):
                raise ValueError(f"第 {index + 1} 条打乱素材起点须在画面内，终点不能超过 {reported_maximum:g} 秒")
            end = min(end, maximum)
        frames = _duration_frames(end - start, fps)
        selected_ranges.append({"source_path": source["source_path"], "start_seconds": start,
                                "end_seconds": end, "frame_count": frames, "duration_seconds": frames / fps})
        if fixed_shuffle_cuts:
            frames = max(0, math.floor((end - start) * fps + 1e-3))
            frames = frames // cut_frames * cut_frames
            selected_ranges[-1].update(frame_count=frames, duration_seconds=frames / fps)
        available_frames += frames
    return selected_ranges, available_frames


def _rotate_cores_plan(core_paths, shuffle_paths, *, opening_path, cut_seconds, width, height, fps, seed, ffprobe,
                       visual_policy, core_audio_policy, core_display_modes, core_ranges, shuffle_ranges,
                       core_timeline_seconds, duration_multiplier, variant_index, fixed_shuffle_cuts,
                       unique_batch, visual_batch, bottom_layers, primary_shuffle_budget_seconds,
                       tier1_dedup=True):
    if visual_policy != "full_core_timeline" or core_audio_policy != "full_core_once":
        raise ValueError("轮换核心模式须配合完整核心时间轴与完整核心原声使用")
    if not core_paths:
        raise ValueError("请选择核心素材")
    if (not isinstance(core_display_modes, (list, tuple)) or len(core_display_modes) != len(core_paths)
            or any(not isinstance(m, str) or m not in ALLOWED_CORE_DISPLAY_MODES for m in core_display_modes)):
        raise ValueError("核心显示选项必须与所选核心素材一一对应，且每条须选择有效的音画模式")
    if primary_shuffle_budget_seconds is not None:
        raise ValueError("轮换核心模式不能同时设置旧首核心打乱画面额度")
    if fixed_shuffle_cuts and not 1 <= cut_seconds <= 10:
        raise ValueError("固定刀长模式下每刀须为 1–10 秒")
    if unique_batch:
        _, variant_zero, batch_count = unique_batch
        if not 0 <= variant_zero < batch_count:
            raise ValueError("打乱素材批次序号无效。")
        variant_num = variant_zero + 1
    else:
        variant_num = max(1, int(variant_index or 1))
    pic_indices = [i for i, m in enumerate(core_display_modes) if m in PICTURE_CORE_DISPLAY_MODES]
    if not pic_indices:
        raise ValueError("请至少将 1 条核心素材设为「画面＋完整音频」或「仅裁剪画面」。")
    audio_indices = [i for i, m in enumerate(core_display_modes) if m in AUDIO_CORE_DISPLAY_MODES]
    if not audio_indices:
        raise ValueError("请至少保留 1 条带音频的核心素材提供原声。")

    core = [_probe(path, ffprobe) for path in core_paths]
    shuffle = [_probe(path, ffprobe) for path in shuffle_paths]
    opening = _probe(opening_path, ffprobe) if opening_path else None
    audio_check_sources = ([opening] if opening else []) + [core[i] for i in audio_indices]
    if any(s["audio_channels"] > 2 for s in audio_check_sources):
        raise ValueError("原声保留模式目前支持单声道或双声道，请选择普通音轨素材")
    audio_channels = max(1, max(s["audio_channels"] for s in audio_check_sources))
    for source in core + shuffle + ([opening] if opening else []):
        source["frame_count"] = _duration_frames(source["duration_seconds"], fps)

    if core_ranges is not None and (not isinstance(core_ranges, (list, tuple)) or len(core_ranges) > len(core)):
        raise ValueError("核心素材的裁剪范围须与所选素材顺序对应")
    pic_set = set(pic_indices)
    audio_set = set(audio_indices)
    selected_core_ranges = []
    validated_spans = {}
    for idx, source in enumerate(core):
        raw_sel = (core_ranges[idx] if core_ranges and idx < len(core_ranges) else None) if idx in pic_set else None
        c_start, c_end, _ = _validate_core_range_item(source, idx, raw_sel)
        k_start, k_end = _kept_frame_span(source, c_start, c_end, fps)
        validated_spans[idx] = (c_start, c_end, k_start, k_end)
        c_frames = k_end - k_start
        selected_core_ranges.append({
            "source_path": source["source_path"],
            "source_duration_seconds": source["duration_seconds"],
            "container_duration_seconds": source["container_duration_seconds"],
            "chosen_start_seconds": c_start,
            "chosen_end_seconds": c_end,
            "start_seconds": c_start,
            "end_seconds": c_end,
            "duration_seconds": c_frames / fps,
            "frame_count": c_frames,
            "kept_start_frame": k_start,
            "kept_end_frame": k_end,
            "kept_picture_duration_seconds": c_frames / fps,
            "full_core_duration_seconds": source["frame_count"] / fps,
            "audio_trimmed": idx not in audio_set,
        })

    lead_idx = pic_indices[(variant_num - 1) % len(pic_indices)]
    lead_source = core[lead_idx]
    lead_start, lead_end, start_frame, end_frame = validated_spans[lead_idx]
    selection_basis_seconds = lead_end - lead_start
    total_lead_frames = lead_source["frame_count"]
    selection_frames = end_frame - start_frame
    exact_start_sec = start_frame / fps

    if duration_multiplier is not None:
        target_seconds = selection_basis_seconds * float(duration_multiplier)
    elif core_timeline_seconds is not None:
        target_seconds = float(core_timeline_seconds)
    else:
        raise ValueError("轮换核心模式需要指定时长倍率或目标时长")
    if target_seconds + 1e-6 < selection_basis_seconds:
        raise ValueError("主体目标时长不能短于所选核心裁剪片段长度")
    target_frames = max(1, selection_frames, math.floor(target_seconds * fps + 1e-3))
    shuffle_frames = max(0, target_frames - selection_frames)

    def _build_variant_audio_candidates(v_lead):
        _, _, v_ks, v_ke = validated_spans[v_lead]
        v_exact_start = v_ks / fps
        v_kept_frames = v_ke - v_ks
        v_ordered_indices = [
            (v_lead + offset) % len(core)
            for offset in range(len(core))
            if ((v_lead + offset) % len(core)) in audio_set
        ]
        v_ordered_cores = [core[i] for i in v_ordered_indices]
        v_candidates = []
        for a_idx, (a_core_idx, src) in enumerate(zip(v_ordered_indices, v_ordered_cores)):
            is_lead_audio = a_idx == 0 and a_core_idx == v_lead
            a_start = v_exact_start if is_lead_audio else 0.0
            usable_dur = (
                max(0.0, float(src["audio_duration_seconds"]) - a_start)
                if is_lead_audio
                else float(src["audio_duration_seconds"])
            )
            item = dict(src)
            item.update(
                role="core_unpadded",
                core_audio_index=a_idx,
                core_index=a_core_idx,
                pad_audio=False,
                audio_start_seconds=a_start,
                is_lead_core_audio=is_lead_audio,
                usable_audio_duration_seconds=usable_dur,
                original_duration_seconds=src["duration_seconds"],
                duration_seconds=usable_dur,
            )
            if is_lead_audio:
                item["lead_kept_frames"] = v_kept_frames
            v_candidates.append(item)
        return v_ordered_indices, v_ordered_cores, v_candidates

    ordered_audio_indices, ordered_audio_cores, candidates = _build_variant_audio_candidates(lead_idx)
    target_audio_samples = math.ceil(target_frames * 48000 / fps - 1e-6)
    _, metadata_samples = _timeline_audio_preflight(candidates, target_audio_samples, fps=fps)
    if unique_batch and unique_batch[2] > 1 and duration_multiplier is not None:
        for v_offset in range(unique_batch[2]):
            v_lead = pic_indices[v_offset % len(pic_indices)]
            if v_lead == lead_idx:
                continue
            v_start, v_end, v_ks, v_ke = validated_spans[v_lead]
            v_basis = v_end - v_start
            v_sel_frames = v_ke - v_ks
            v_target_sec = v_basis * float(duration_multiplier)
            v_target_frames = max(1, v_sel_frames, math.floor(v_target_sec * fps + 1e-3))
            _, _, v_candidates = _build_variant_audio_candidates(v_lead)
            _timeline_audio_preflight(v_candidates, math.ceil(v_target_frames * 48000 / fps - 1e-6), fps=fps)

    if shuffle_frames > 0 and not shuffle:
        raise ValueError("目标时长大于核心裁剪片段时长，请勾选打乱素材填充剩余画面。")

    cut_frames = max(1, round(cut_seconds * fps))
    selected_shuffle_ranges, available_shuffle_frames = _build_shuffle_ranges_metadata(
        shuffle, shuffle_ranges, fps, cut_frames, fixed_shuffle_cuts,
    )
    if shuffle_frames > 0:
        usable_cuts = sum(
            min(r["frame_count"], math.floor((r["end_seconds"] - r["start_seconds"]) * fps + 1e-3)) // cut_frames
            for r in selected_shuffle_ranges
        )
        if not usable_cuts:
            raise ValueError("已选打乱素材没有完整的一刀，请缩短刀长或补充打乱素材。")

    dedup_meta = None
    if tier1_dedup and (fixed_shuffle_cuts or unique_batch) and shuffle_frames > 0:
        total_usable_audio_sec = 0.0
        for cand_item in candidates:
            u_dur = float(cand_item.get("usable_audio_duration_seconds") or 0.0)
            if not cand_item.get("has_audio") or u_dur <= 0:
                break
            total_usable_audio_sec += u_dur
        total_usable_audio_sec = max(metadata_samples / 48000.0, total_usable_audio_sec)
        base_target_frames = target_frames
        target_frames, dedup_meta = _compute_tier1_dedup_spec(
            fps=fps,
            seed=seed,
            unique_batch=unique_batch,
            variant_num=variant_num,
            base_target_frames=base_target_frames,
            selection_frames=selection_frames,
            usable_audio_sec=total_usable_audio_sec,
            allow_tail_tempo=True,
        )
        shuffle_frames = max(0, target_frames - selection_frames)
        target_audio_samples = math.ceil(target_frames * 48000 / fps - 1e-6)
        _, metadata_samples = _timeline_audio_preflight(candidates, target_audio_samples, fps=fps)

    opening_frames = opening["frame_count"] if opening else 0
    shots = []
    opening_audio = []
    if opening:
        shots.append({
            "index": 0,
            "role": "opening",
            "source_path": opening["source_path"],
            "source_start_seconds": 0.0,
            "duration_seconds": opening_frames / fps,
            "frame_count": opening_frames,
            "timeline_start_seconds": 0.0,
            "core_source_path": None,
            "core_source_start_seconds": None,
            "picture_fit": "contain",
        })
        op_audio = dict(opening)
        op_audio.update(
            original_duration_seconds=opening["duration_seconds"],
            duration_seconds=opening_frames / fps,
            chosen_start_seconds=0.0,
            chosen_end_seconds=opening["duration_seconds"],
        )
        opening_audio.append(op_audio)

    lead_shot = {
        "index": len(shots),
        "role": "core",
        "source_path": lead_source["source_path"],
        "source_start_seconds": start_frame / fps,
        "duration_seconds": selection_frames / fps,
        "frame_count": selection_frames,
        "timeline_start_seconds": opening_frames / fps,
        "core_source_path": lead_source["source_path"],
        "core_source_start_seconds": start_frame / fps,
        "core_index": lead_idx,
        "core_display_mode": core_display_modes[lead_idx],
        "primary_selected_picture": True,
        "picture_fit": "contain",
    }
    if start_frame or selection_frames != total_lead_frames:
        lead_shot["source_end_seconds"] = end_frame / fps
    shots.append(lead_shot)
    selected_core_ranges[lead_idx]["selected_picture_timeline_start_seconds"] = opening_frames / fps

    if shuffle_frames > 0:
        shots.append({
            "index": len(shots),
            "role": "shuffle",
            "source_path": shuffle[0]["source_path"],
            "source_start_seconds": 0.0,
            "duration_seconds": shuffle_frames / fps,
            "frame_count": shuffle_frames,
            "timeline_start_seconds": (opening_frames + selection_frames) / fps,
            "core_source_path": lead_source["source_path"],
            "core_source_start_seconds": 0.0,
            "core_index": lead_idx,
            "core_display_mode": core_display_modes[lead_idx],
            "picture_fit": "cover",
        })

    details = []
    for idx, (src, mode) in enumerate(zip(core, core_display_modes)):
        is_lead = idx == lead_idx
        has_audio_relay = idx in audio_set
        _, _, k_start, k_end = validated_spans[idx]
        details.append({
            "core_index": idx,
            "source_path": src["source_path"],
            "mode": mode,
            "is_lead_picture": is_lead,
            "picture_visible": is_lead,
            "included_in_output": is_lead or has_audio_relay,
            "timeline_start_seconds": (opening_frames / fps) if is_lead else None,
            "selected_picture_timeline_start_seconds": (opening_frames / fps) if is_lead else None,
            "duration_seconds": (selection_frames / fps) if is_lead else 0.0,
            "source_full_duration_seconds": src["duration_seconds"],
            "kept_start_seconds": (k_start / fps) if idx in pic_set else None,
            "kept_end_seconds": (k_end / fps) if idx in pic_set else None,
            "audio_enabled": has_audio_relay,
            "audio_start_seconds": (exact_start_sec if (is_lead and has_audio_relay) else (0.0 if has_audio_relay else None)),
            "audio_relay_rank": (ordered_audio_indices.index(idx) + 1) if has_audio_relay else None,
            "audio_full_once": False,
            "audio_policy": (
                "lead-core-synced-from-crop-start-then-tail-to-shuffle"
                if (is_lead and has_audio_relay)
                else ("subsequent-full-untrimmed-core-audio-relay" if has_audio_relay else "muted-video-only")
            ),
        })

    all_sources = core + shuffle + ([opening] if opening else [])
    plan = {
        "version": "rotate-cores-original-audio-v1",
        "visual_policy": "full_core_timeline",
        "fps": fps,
        "width": width,
        "height": height,
        "cut_seconds": cut_seconds,
        "effective_cut_seconds": cut_frames / fps,
        "output_duration_seconds": (opening_frames + target_frames) / fps,
        "duration_seconds": (opening_frames + target_frames) / fps,
        "opening_duration_seconds": opening_frames / fps,
        "core_timeline_seconds": target_seconds,
        "core_timeline_duration_seconds": target_frames / fps,
        "duration_multiplier": float(duration_multiplier) if duration_multiplier is not None else None,
        "variant_index": variant_num,
        "full_core_source_duration_seconds": sum(c["duration_seconds"] for c in ordered_audio_cores),
        "core_duration_seconds": target_frames / fps,
        "core_kept_picture_duration_seconds": selection_frames / fps,
        "core_audio_only_duration_seconds": 0.0,
        "first_core_picture_hole_duration_seconds": shuffle_frames / fps,
        "core_picture_hole_duration_seconds": shuffle_frames / fps,
        "full_core_picture_hole_duration_seconds": shuffle_frames / fps,
        "core_picture_fallback_duration_seconds": 0.0,
        "core_fallback_duration_seconds": 0.0,
        "shuffle_duration_seconds": shuffle_frames / fps,
        "shuffle_used_duration_seconds": shuffle_frames / fps,
        "shuffle_budget_seconds": shuffle_frames / fps,
        "shuffle_available_duration_seconds": available_shuffle_frames / fps,
        "shuffle_unused_duration_seconds": max(0, available_shuffle_frames - shuffle_frames) / fps,
        "shuffle_budget_source": "rotate-cores-multiplier",
        "shuffle_budget_frame_adjustment_seconds": 0.0,
        "shuffle_repeated": False,
        "shuffle_repeat_cycles": 0,
        "solid_background_duration_seconds": 0.0,
        "black_picture_duration_seconds": 0.0,
        "short_core_adjustment": False,
        "short_core_note": "",
        "selected_shuffle_paths": [source["source_path"] for source in shuffle],
        "shuffle_ranges": selected_shuffle_ranges,
        "core_ranges": selected_core_ranges,
        "core_display_modes": list(core_display_modes),
        "core_display_mode_details": details,
        "batch_picture_policy": "rotate_cores",
        "lead_core_index": lead_idx,
        "lead_core_name": Path(lead_source["source_path"]).name,
        "audio_core_indices": ordered_audio_indices,
        "audio_core_order": [Path(c["source_path"]).name for c in ordered_audio_cores],
        "primary_selection_first": True,
        "primary_selection_frame_count": selection_frames,
        "primary_selection_basis_seconds": selection_basis_seconds,
        "primary_selection_raw_duration_seconds": selection_basis_seconds,
        "primary_selection_frame_duration_seconds": selection_frames / fps,
        "primary_selection_timeline_start_seconds": opening_frames / fps,
        "primary_selection_body_start_seconds": 0.0,
        "primary_selection_source_start_seconds": start_frame / fps,
        "sources": {source["source_path"]: source for source in all_sources},
        "core_target_audio_samples": target_audio_samples,
        "core_metadata_audio_available_samples": metadata_samples,
        "core_audio_candidates": candidates,
        "audio_sources": [*opening_audio, *candidates],
        "shots": shots,
        "output_audio_channels": audio_channels,
        "core_audio_policy": "original_core_prefix",
        "core_audio_repeated": False,
        "core_audio_tail_may_be_cut": True,
        "core_audio_iterations": 1,
        "core_audio_cycle_seconds": 0.0,
        "audio_policy": "opening-original-once; lead-core-synced-from-crop-start-then-remaining-tail-to-shuffle; subsequent-cores-full-untrimmed-audio-relay-until-target; video-only-cores-muted; final-audio-tail-cut-allowed; no-core-audio-loop-or-speed-change; no-shuffle-source-audio",
        "crop_policy": "opening-and-core-contain; shuffle-center-fill",
    }
    if dedup_meta is not None:
        plan["dongge_tier1_dedup"] = dedup_meta
    if shuffle_frames > 0:
        if unique_batch:
            if visual_batch is not None and bottom_layers:
                from bottom_overlay import cut_top_fractions, layer_layouts
                first_end = _first_core_end(plan)
                layouts = [layout for _, _, layout in layer_layouts(
                    bottom_layers, width, height, plan["output_duration_seconds"], first_end)]
                first_frame = round(first_end * fps)
                stride = max(1, round(cut_seconds * fps))
                spans = [(first_frame + offset, first_frame + min(shuffle_frames, offset + stride))
                         for offset in range(0, shuffle_frames, stride)]
                plan["shuffle_cut_top_fractions"] = cut_top_fractions(spans, layouts, fps)
            return _fixed_shuffle_sequence(
                plan, seed, unique_batch, visual_batch,
                tier1_dedup=tier1_dedup, bottom_layers=bottom_layers,
            )
        return _fixed_shuffle_sequence(
            plan, seed, tier1_dedup=tier1_dedup, bottom_layers=bottom_layers,
        )
    return plan


def plan_edit(core_paths, shuffle_paths, *, opening_path=None, cut_seconds=3.0,
              width=720, height=1280, fps=30, seed=0, ffprobe="ffprobe",
              visual_policy="interleave", shuffle_ranges=None, core_ranges=None,
              shuffle_budget_seconds=None, core_audio_policy="legacy_selected_then_silence",
              core_display_modes=None, primary_shuffle_budget_seconds=None, core_timeline_seconds=None,
              primary_selection_first=False, fixed_shuffle_cuts=False, unique_batch=None, visual_batch=None,
              batch_picture_policy="per_core", bottom_layers=None,
              duration_multiplier=None, variant_index=1, tier1_dedup=True):
    """Probe selected files and return a JSON-serializable visual/audio plan."""
    cut_seconds = float(cut_seconds)
    if not math.isfinite(cut_seconds) or not 0.1 <= cut_seconds <= 60:
        raise ValueError("每刀秒数须为 0.1–60 秒")
    if visual_policy not in {"interleave", "append_shuffle", "full_core_timeline"}:
        raise ValueError("画面编排方式无效")
    if core_audio_policy not in {"legacy_selected_then_silence", "selected_core_loop", "full_core_once"}:
        raise ValueError("核心原声播放方式无效")
    if (visual_policy == "full_core_timeline") != (core_audio_policy == "full_core_once"):
        raise ValueError("完整核心原声须与完整核心时间轴配合使用")
    if batch_picture_policy not in ("rotate_cores", "per_core", "first_core_then_shuffle"):
        raise ValueError("批量画面编排选项无效。")
    if not isinstance(primary_selection_first, bool):
        raise ValueError("首条选区前置选项必须是布尔值")
    if not isinstance(fixed_shuffle_cuts, bool):
        raise ValueError("固定刀长选项必须是布尔值")
    if duration_multiplier is not None:
        try:
            dm_val = float(duration_multiplier)
        except (TypeError, ValueError) as exc:
            raise ValueError("时长倍率必须是有效的正数") from exc
        if isinstance(duration_multiplier, bool) or not math.isfinite(dm_val) or dm_val <= 0:
            raise ValueError("时长倍率必须是有效的正数")
        duration_multiplier = dm_val
    if core_timeline_seconds is not None:
        try:
            target_seconds = float(core_timeline_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("主体目标时长必须是有效的正数") from exc
        if isinstance(core_timeline_seconds, bool) or not math.isfinite(target_seconds) or target_seconds <= 0:
            raise ValueError("主体目标时长必须是有效的正数")
        core_timeline_seconds = target_seconds
    fps, width, height = int(fps), int(width), int(height)
    if not 1 <= fps <= 120 or min(width, height) < 16 or width % 2 or height % 2:
        raise ValueError("输出帧率或偶数尺寸无效")

    if batch_picture_policy == "rotate_cores":
        return _rotate_cores_plan(
            core_paths, shuffle_paths, opening_path=opening_path, cut_seconds=cut_seconds,
            width=width, height=height, fps=fps, seed=seed, ffprobe=ffprobe,
            visual_policy=visual_policy, core_audio_policy=core_audio_policy,
            core_display_modes=core_display_modes, core_ranges=core_ranges,
            shuffle_ranges=shuffle_ranges, core_timeline_seconds=core_timeline_seconds,
            duration_multiplier=duration_multiplier, variant_index=variant_index,
            fixed_shuffle_cuts=fixed_shuffle_cuts, unique_batch=unique_batch,
            visual_batch=visual_batch, bottom_layers=bottom_layers,
            primary_shuffle_budget_seconds=primary_shuffle_budget_seconds,
            tier1_dedup=tier1_dedup,
        )

    shuffle_after_first = batch_picture_policy == "first_core_then_shuffle"
    if shuffle_after_first:
        if not (visual_policy == "full_core_timeline" and primary_selection_first and fixed_shuffle_cuts
                and (core_timeline_seconds is not None or duration_multiplier is not None)
                and unique_batch and unique_batch[2] > 1):
            raise ValueError("首段后全打乱须使用首条选区倍率的多条制作。")
        if not shuffle_paths:
            raise ValueError("首段后全打乱需要勾选打乱素材。")
        if core_display_modes is not None:
            if (not isinstance(core_display_modes, (list, tuple)) or len(core_display_modes) != len(core_paths)
                    or any(not isinstance(m, str) or m not in ALLOWED_CORE_DISPLAY_MODES for m in core_display_modes)):
                raise ValueError("核心显示选项必须与所选核心顺序对应，且每条须选择有效的音画模式")
            pic_first = next((i for i, m in enumerate(core_display_modes) if m in PICTURE_CORE_DISPLAY_MODES), None)
            if pic_first is None:
                raise ValueError("请至少将 1 条核心素材设为「画面＋完整音频」或「仅裁剪画面」。")
            core_display_modes = [
                ("video_only" if m == "video_only" else "trimmed_video_audio") if i == pic_first
                else ("video_only" if m == "video_only" else "audio_only")
                for i, m in enumerate(core_display_modes)
            ]
        else:
            core_display_modes = ["trimmed_video_audio"] + ["audio_only"] * (len(core_paths) - 1)
    if core_display_modes is not None:
        if visual_policy != "full_core_timeline":
            raise ValueError("核心显示选项须与完整核心时间轴配合使用")
        if (not isinstance(core_display_modes, (list, tuple)) or len(core_display_modes) != len(core_paths)
                or any(not isinstance(m, str) or m not in ALLOWED_CORE_DISPLAY_MODES for m in core_display_modes)):
            raise ValueError("核心显示选项必须与所选核心顺序对应，且每条须选择有效的音画模式")
        if not any(m in AUDIO_CORE_DISPLAY_MODES for m in core_display_modes):
            raise ValueError("请至少保留 1 条带音频的核心素材提供原声。")
    if fixed_shuffle_cuts and (not primary_selection_first or not 1 <= cut_seconds <= 10):
        raise ValueError("固定刀长仅用于首条选区倍率模式，每刀须为 1–10 秒")
    if primary_selection_first and core_timeline_seconds is None and duration_multiplier is None:
        raise ValueError("首条选区前置须配合主体目标时长使用")
    if core_timeline_seconds is not None:
        if (visual_policy != "full_core_timeline" or not isinstance(core_display_modes, (list, tuple))
                or not core_display_modes or not any(m in PICTURE_CORE_DISPLAY_MODES for m in core_display_modes)):
            raise ValueError("主体目标时长仅适用于核心画面裁剪与完整核心原声模式")
        if primary_shuffle_budget_seconds is not None:
            raise ValueError("主体目标时长不能同时设置旧首核心打乱画面额度")
    if primary_shuffle_budget_seconds is not None:
        try:
            primary_budget = float(primary_shuffle_budget_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("首条核心打乱画面额度必须是有效的非负秒数") from exc
        if isinstance(primary_shuffle_budget_seconds, bool) or not math.isfinite(primary_budget) or primary_budget < 0:
            raise ValueError("首条核心打乱画面额度必须是有效的非负秒数")
        if (visual_policy != "full_core_timeline" or not isinstance(core_display_modes, (list, tuple))
                or not core_display_modes or core_display_modes[0] != "trimmed_video_audio"):
            raise ValueError("首条打乱画面额度仅适用于完整原声模式中的首条画面裁剪")
        primary_shuffle_budget_seconds = primary_budget
    core = [_probe(path, ffprobe) for path in core_paths]
    shuffle = [_probe(path, ffprobe) for path in shuffle_paths]
    opening = _probe(opening_path, ffprobe) if opening_path else None
    if not core or (not shuffle and visual_policy != "full_core_timeline"):
        raise ValueError("请选择核心素材和打乱素材")
    if visual_policy == "interleave" and not any(source["has_audio"] for source in core):
        raise ValueError("所选核心素材没有音轨，无法提取核心原声")
    audio_core_indices = (
        [i for i, m in enumerate(core_display_modes) if m in AUDIO_CORE_DISPLAY_MODES]
        if core_display_modes is not None else list(range(len(core)))
    )
    audio_cores = [core[i] for i in audio_core_indices]
    original_audio = ([opening] if opening else []) + audio_cores
    if any(source["audio_channels"] > 2 for source in original_audio):
        raise ValueError("原声保留模式目前支持单声道或双声道，请选择普通音轨素材")
    audio_channels = max(1, max(source["audio_channels"] for source in original_audio)) if original_audio else 1
    for source in core + shuffle + ([opening] if opening else []):
        source["frame_count"] = _duration_frames(source["duration_seconds"], fps)
    primary_core_idx = 0
    if core_display_modes is not None:
        first_pic = next((i for i, m in enumerate(core_display_modes) if m in PICTURE_CORE_DISPLAY_MODES), None)
        if primary_selection_first and first_pic is None:
            raise ValueError("请至少将 1 条核心素材设为「画面＋完整音频」或「仅裁剪画面」。")
        if first_pic is not None:
            primary_core_idx = first_pic
    if visual_policy == "full_core_timeline" and core_timeline_seconds is None and duration_multiplier is not None:
        if core_ranges is not None and (not isinstance(core_ranges, (list, tuple)) or len(core_ranges) > len(core)):
            raise ValueError("首条核心素材的裁剪范围须与所选核心顺序对应")
        raw_p_range = core_ranges[primary_core_idx] if core_ranges and primary_core_idx < len(core_ranges) else None
        if primary_selection_first:
            p_start, p_end, _ = _validate_core_range_item(core[primary_core_idx], primary_core_idx, raw_p_range)
            core_timeline_seconds = (p_end - p_start) * duration_multiplier
        else:
            core_timeline_seconds = core[primary_core_idx]["duration_seconds"] * duration_multiplier
    validation_count = None
    if core_timeline_seconds is not None:
        selection_frames = 0
        preflight_audio_cores = [dict(src) for src in audio_cores]
        if primary_selection_first:
            if core_ranges is not None and (not isinstance(core_ranges, (list, tuple)) or len(core_ranges) > len(core)):
                raise ValueError("首条核心素材的裁剪范围须与所选核心顺序对应")
            first_range = core_ranges[primary_core_idx] if core_ranges and primary_core_idx < len(core_ranges) else None
            c_start, c_end, _ = _validate_core_range_item(core[primary_core_idx], primary_core_idx, first_range)
            if shuffle_ranges is not None:
                _build_shuffle_ranges_metadata(shuffle, shuffle_ranges, fps, max(1, round(cut_seconds * fps)), False)
            selection_basis_seconds = c_end - c_start
            start, end = _kept_frame_span(core[primary_core_idx], c_start, c_end, fps)
            selection_frames = end - start
            if core_timeline_seconds + 1e-6 < selection_basis_seconds:
                raise ValueError("主体目标时长不能短于第一条核心的选区长度")
            if preflight_audio_cores and audio_core_indices[0] == primary_core_idx:
                preflight_audio_cores[0].update(
                    audio_start_seconds=start / fps,
                    is_lead_core_audio=True,
                    lead_kept_frames=selection_frames,
                )
        elif core_timeline_seconds + 1e-6 < core[primary_core_idx]["duration_seconds"]:
            raise ValueError("主体目标时长不能短于第一条核心原片全长")
        target_frames = max(1, selection_frames, math.floor(core_timeline_seconds * fps + 1e-3))
        validation_count, metadata_samples = _timeline_audio_preflight(
            preflight_audio_cores, math.ceil(target_frames * 48000 / fps - 1e-6), fps=fps)
    if visual_policy == "full_core_timeline":
        plan = _full_core_timeline_plan(core, shuffle, opening, core_ranges=core_ranges,
                                       shuffle_ranges=shuffle_ranges, cut_seconds=cut_seconds,
                                       width=width, height=height, fps=fps, seed=seed,
                                       audio_channels=audio_channels, core_display_modes=core_display_modes,
                                       primary_shuffle_budget_seconds=primary_shuffle_budget_seconds,
                                       core_audio_validation_count=validation_count,
                                       fixed_shuffle_cuts=fixed_shuffle_cuts, loop_first_broll=shuffle_after_first)
        if primary_selection_first:
            plan = _move_primary_selection_first(plan, selection_basis_seconds, selection_frames, primary_core_idx)
        if core_timeline_seconds is None and core_display_modes is not None and "video_only" in core_display_modes:
            audio_total_frames = sum(s["frame_count"] for s in audio_cores)
            body_visual_frames = round((plan["output_duration_seconds"] - plan["opening_duration_seconds"]) * fps)
            if body_visual_frames > audio_total_frames:
                _, vo_meta_samples = _timeline_audio_preflight(
                    audio_cores, math.ceil(audio_total_frames * 48000 / fps - 1e-6))
                plan = _limit_core_timeline(plan, audio_cores, audio_total_frames / fps, audio_total_frames, vo_meta_samples)
        if tier1_dedup and fixed_shuffle_cuts and core_timeline_seconds is not None:
            body_shots_full = [s for s in plan["shots"] if s["role"] != "opening"]
            max_body_cap_frames = sum(s["frame_count"] for s in body_shots_full)
            cum_body = cum_shuf = base_shuf_frames = 0
            min_body_for_shuf = None
            need_shuf_floor = round(0.65 * fps)
            for bs in body_shots_full:
                fc = bs["frame_count"]
                if cum_body < target_frames:
                    take_b = min(fc, target_frames - cum_body)
                    if bs["role"] == "shuffle":
                        base_shuf_frames += take_b
                if bs["role"] == "shuffle":
                    if cum_shuf < need_shuf_floor <= cum_shuf + fc:
                        min_body_for_shuf = cum_body + (need_shuf_floor - cum_shuf)
                    cum_shuf += fc
                cum_body += fc
            opening_f = round(plan["opening_duration_seconds"] * fps)
            has_later_core = any(
                bs["role"] == "core"
                and round(bs["timeline_start_seconds"] * fps) >= opening_f + selection_frames
                for bs in body_shots_full
            )
            if base_shuf_frames > 0:
                total_usable_audio_sec = 0.0
                for c_src in preflight_audio_cores:
                    c_start_s = float(c_src.get("audio_start_seconds") or 0.0)
                    c_usable = float(c_src["audio_duration_seconds"]) - c_start_s
                    if not c_src["has_audio"] or c_usable <= 0:
                        break
                    total_usable_audio_sec += c_usable
                total_usable_audio_sec = max(metadata_samples / 48000.0, total_usable_audio_sec)
                v_num = (unique_batch[1] + 1) if unique_batch else max(1, int(variant_index or 1))
                base_target_frames = target_frames
                target_frames, dedup_meta = _compute_tier1_dedup_spec(
                    fps=fps,
                    seed=seed,
                    unique_batch=unique_batch,
                    variant_num=v_num,
                    base_target_frames=base_target_frames,
                    selection_frames=selection_frames,
                    usable_audio_sec=total_usable_audio_sec,
                    allow_tail_tempo=bool(primary_selection_first and not has_later_core),
                    max_body_capacity_frames=max_body_cap_frames,
                    min_body_frames_for_shuffle=min_body_for_shuf,
                )
                validation_count, metadata_samples = _timeline_audio_preflight(
                    preflight_audio_cores, math.ceil(target_frames * 48000 / fps - 1e-6), fps=fps
                )
                plan["dongge_tier1_dedup"] = dedup_meta
        if unique_batch:
            if core_timeline_seconds is not None:
                plan = _limit_core_timeline(plan, audio_cores, core_timeline_seconds, target_frames, metadata_samples)
            if shuffle_after_first:
                body = [shot for shot in plan["shots"] if shot["role"] != "opening"]
                if not body or not body[0].get("primary_selected_picture") or body[0]["frame_count"] != selection_frames:
                    raise RuntimeError("首条核心画面的选区与成片开场不一致。")
                required = target_frames - selection_frames
                actual = sum(shot["frame_count"] for shot in body[1:] if shot["role"] == "shuffle")
                if required <= 0 or actual != required or any(s["role"] != "shuffle" for s in body[1:]):
                    raise ValueError(f"首段选区结束后需要 {required/fps:.2f} 秒打乱画面，当前可用 {actual/fps:.2f} 秒。"
                                     "请调整倍率、刀长或打乱素材。")
                plan["batch_picture_policy"] = batch_picture_policy
                if visual_batch is not None and bottom_layers:
                    from bottom_overlay import cut_top_fractions, layer_layouts
                    first_end = _first_core_end(plan)
                    layouts = [layout for _, _, layout in layer_layouts(
                        bottom_layers, width, height, plan["output_duration_seconds"], first_end)]
                    first_frame = round(first_end * fps)
                    stride = max(1, round(cut_seconds * fps))
                    spans = [(first_frame + offset, first_frame + min(required, offset + stride))
                             for offset in range(0, required, stride)]
                    plan["shuffle_cut_top_fractions"] = cut_top_fractions(spans, layouts, fps)
            return _fixed_shuffle_sequence(
                plan, seed, unique_batch, visual_batch,
                tier1_dedup=tier1_dedup, bottom_layers=bottom_layers,
            )
        if fixed_shuffle_cuts:
            if core_timeline_seconds is not None:
                plan = _limit_core_timeline(plan, audio_cores, core_timeline_seconds, target_frames, metadata_samples)
            return _fixed_shuffle_sequence(
                plan, seed, tier1_dedup=tier1_dedup, bottom_layers=bottom_layers,
            )
        return (_limit_core_timeline(plan, audio_cores, core_timeline_seconds, target_frames, metadata_samples)
                if core_timeline_seconds is not None else plan)
    if visual_policy == "append_shuffle":
        plan = _append_shuffle_plan(core, shuffle, opening, core_ranges=core_ranges, shuffle_ranges=shuffle_ranges,
                                    cut_seconds=cut_seconds, width=width, height=height,
                                    fps=fps, seed=seed, audio_channels=audio_channels,
                                    shuffle_budget_seconds=shuffle_budget_seconds,
                                    core_audio_policy=core_audio_policy)
        return (
            _fixed_shuffle_sequence(
                plan, seed, unique_batch, visual_batch,
                tier1_dedup=tier1_dedup, bottom_layers=bottom_layers,
            )
            if unique_batch
            else plan
        )
    cut_frames = max(1, round(cut_seconds * fps))
    core_frames = sum(source["frame_count"] for source in core)
    opening_frames = opening["frame_count"] if opening else 0
    short_core = core_frames <= 2 * cut_frames
    # A short source still gets at most one-third B-roll. Its tail is a partial cut.
    short_core_start = math.ceil(core_frames * 2 / 3) if short_core else None
    windows = []
    cursor = 0
    while cursor < core_frames:
        end = min(core_frames, cursor + cut_frames)
        if short_core:
            if cursor < short_core_start < end:
                end = short_core_start
            role = "shuffle" if cursor >= short_core_start else "core"
        else:
            role = "shuffle" if (cursor // cut_frames) % 3 == 2 else "core"
        windows.append((cursor, end, role))
        cursor = end
    rng = random.Random(str(seed))
    source_order = list(shuffle)
    rng.shuffle(source_order)
    shuffle_cursor = 0
    shots = []
    def add_shot(role, source, start, frames, timeline, core_source=None, core_start=None):
        shots.append({
            "index": len(shots), "role": role,
            "source_path": source["source_path"], "source_start_seconds": start,
            "duration_seconds": frames / fps, "frame_count": frames,
            "timeline_start_seconds": timeline / fps,
            "core_source_path": core_source["source_path"] if core_source else None,
            "core_source_start_seconds": core_start,
        })
    if opening:
        add_shot("opening", opening, 0.0, opening_frames, 0)
    source_start_frame = 0
    for source in core:
        source_end_frame = source_start_frame + source["frame_count"]
        for start, end, role in windows:
            left, right = max(start, source_start_frame), min(end, source_end_frame)
            if right <= left:
                continue
            frames = right - left
            source_offset = (left - source_start_frame) / fps
            chosen, selected_start = source, source_offset
            if role == "shuffle":
                chosen = source_order[shuffle_cursor % len(source_order)]
                shuffle_cursor += 1
                max_start_frame = max(0, math.floor(chosen["duration_seconds"] * fps) - frames)
                selected_start = rng.randint(0, max_start_frame) / fps if max_start_frame else 0.0
            add_shot(role, chosen, selected_start, frames, opening_frames + left, source, source_offset)
        source_start_frame = source_end_frame
    all_sources = core + shuffle + ([opening] if opening else [])
    audio_sources = []
    for source in ([opening] if opening else []) + core:
        item = dict(source)
        item["original_duration_seconds"] = item["duration_seconds"]
        item["duration_seconds"] = item["frame_count"] / fps
        audio_sources.append(item)
    return {
        "version": "original-audio-overlay-v1", "visual_policy": "interleave", "fps": fps, "width": width, "height": height,
        "cut_seconds": cut_seconds, "effective_cut_seconds": cut_frames / fps,
        "output_duration_seconds": (opening_frames + core_frames) / fps,
        "duration_seconds": (opening_frames + core_frames) / fps,
        "opening_duration_seconds": opening_frames / fps,
        "core_duration_seconds": core_frames / fps,
        "short_core_adjustment": short_core,
        "short_core_note": "短核心的末刀缩短，核心画面保留至少三分之二" if short_core else "",
        "selected_shuffle_paths": [source["source_path"] for source in shuffle],
        "sources": {source["source_path"]: source for source in all_sources},
        "audio_sources": audio_sources, "shots": shots,
        "output_audio_channels": audio_channels,
        "audio_policy": "opening-original-then-core-original; no-shuffle-audio; no-speed-or-volume-change",
        "crop_policy": "center-fill",
    }


def _validate_selection(original, selected):
    if not isinstance(selected, dict):
        raise ValueError("画面匹配器必须返回剪辑计划")
    for key in original:
        if key != "shots" and key in selected and selected[key] != original[key]:
            raise ValueError(f"画面匹配器不能改变时间轴或音轨：{key}")
    if len(selected.get("shots", [])) != len(original["shots"]):
        raise ValueError("画面匹配器不能改变镜头数量")
    protected = ("index", "frame_count", "duration_seconds", "timeline_start_seconds", "core_source_path", "core_source_start_seconds")
    for before, after in zip(original["shots"], selected["shots"]):
        if any(after.get(key) != before.get(key) for key in protected):
            raise ValueError("画面匹配器不能改变核心时间轴")
        if before["role"] != "shuffle":
            if any(after.get(key) != before.get(key) for key in ("role", "source_path", "source_start_seconds")):
                raise ValueError("画面匹配器不能改变开头或核心镜头")
        elif after.get("role") == "core":
            if after.get("source_path") != before["core_source_path"] or after.get("source_start_seconds") != before["core_source_start_seconds"]:
                raise ValueError("没有匹配画面时只能回归同一时刻的核心画面")
        elif after.get("role") != "shuffle" or after.get("source_path") not in original["selected_shuffle_paths"]:
            raise ValueError("只能从用户勾选的打乱素材中匹配画面")
        position = float(after.get("source_start_seconds", -1))
        source = original["sources"][after["source_path"]]
        if not math.isfinite(position) or position < 0 or position >= source["duration_seconds"]:
            raise ValueError("匹配画面入点越界")
    result = copy.deepcopy(original)
    result.update(selected)
    return result


def _prepare_original_core_prefix(plan, work, execute, command, playlist, emit):
    """Decode before picture work; prove actual PCM covers the requested body."""
    channels = plan["output_audio_channels"]
    outputs = []
    published_sources = []
    def channel_filter(source):
        return "pan=stereo|c0=c0|c1=c0," if source["audio_channels"] == 1 and channels == 2 else ""
    def pcm_samples(path):
        with wave.open(str(path), "rb") as stream:
            if stream.getframerate() != 48000 or stream.getnchannels() != channels or stream.getsampwidth() != 2:
                raise RuntimeError("原声解码后的采样格式不符合预期")
            return stream.getnframes()
    if plan["opening_duration_seconds"] > 0:
        source = plan["audio_sources"][0]
        destination = work / "opening_audio.wav"
        cmd = command()
        if source["has_audio"]:
            cmd += ["-i", source["source_path"], "-map", f"0:{source['audio_stream_index']}"]
        else:
            cmd += ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl={'mono' if channels == 1 else 'stereo'}", "-map", "0:a:0"]
        cmd += ["-vn", "-af", channel_filter(source) + f"aresample=48000,apad,atrim=duration={source['duration_seconds']:.9f},asetpts=PTS-STARTPTS",
                "-ac", str(channels), "-ar", "48000", "-c:a", "pcm_s16le", str(destination)]
        execute(cmd, destination)
        outputs.append(destination)
        published_sources.append(dict(source))
    target_samples = int(plan["core_target_audio_samples"])
    fps = plan["fps"]
    opening_frames = round(plan.get("opening_duration_seconds", 0.0) * fps)
    selection_frames = int(plan.get("primary_selection_frame_count") or 0)
    protected_mode = bool(
        plan.get("primary_selection_first")
        or plan.get("batch_picture_policy") in ("rotate_cores", "first_core_then_shuffle")
    )
    body_shots = [s for s in plan.get("shots", []) if s.get("role") != "opening"]
    tail_shots = [
        s for s in body_shots
        if round(s.get("timeline_start_seconds", 0.0) * fps) >= opening_frames + selection_frames
    ]
    all_tail_shuffle = bool(
        protected_mode and tail_shots and all(s.get("role") == "shuffle" for s in tail_shots)
    )
    head_samples = min(target_samples, max(0, round(selection_frames * 48000 / fps)))
    tail_out_samples = max(0, target_samples - head_samples)
    dedup_cfg = plan.get("dongge_tier1_dedup") if isinstance(plan.get("dongge_tier1_dedup"), dict) else None
    requested_tail_tempo = (
        float(dedup_cfg.get("tail_audio_tempo", 1.0))
        if (dedup_cfg and dedup_cfg.get("enabled"))
        else 1.0
    )
    use_tail_tempo = bool(
        all_tail_shuffle
        and tail_out_samples >= round(0.5 * 48000)
        and abs(requested_tail_tempo - 1.0) >= 0.005
    )
    decode_goal_samples = (
        max(target_samples, head_samples + math.ceil(tail_out_samples * requested_tail_tempo) + 9600)
        if use_tail_tempo
        else target_samples
    )

    total_samples = 0
    core_wavs = []
    decoded_entries = []
    for index, source in enumerate(plan["core_audio_candidates"]):
        if total_samples >= decode_goal_samples:
            break
        start_sec = float(source.get("audio_start_seconds") or 0.0)
        start_sample = max(0, round(start_sec * 48000))
        if not source["has_audio"] or source["audio_duration_seconds"] <= 0:
            raise ValueError(f"核心素材“{Path(source['source_path']).name}”没有可用原声，无法继续覆盖视频")
        declared_samples = round(source["audio_duration_seconds"] * 48000)
        if declared_samples <= start_sample:
            raise ValueError(f"核心素材“{Path(source['source_path']).name}”起点已超出可用原声长度")
        if start_sample > 0:
            emit(f"核对第 {index + 1} 条主核心原声实际长度（从 {start_sec:.2f}s 同步提取至片尾原声，不循环）")
        else:
            emit(f"核对第 {index + 1} 条核心原声实际长度（从 0.00s 提取完整原声接续，不循环）")
        af_chain = (
            channel_filter(source)
            + f"aresample=48000,atrim=start_sample={start_sample}:end_sample={declared_samples},asetpts=PTS-STARTPTS"
        )
        if source.get("is_lead_core_audio") and source.get("lead_kept_frames"):
            min_kept_samples = round(int(source["lead_kept_frames"]) * 48000 / fps)
            expected_usable = declared_samples - start_sample
            if 0 < min_kept_samples - expected_usable <= round(0.25 * 48000):
                af_chain += f",apad=whole_len={min_kept_samples}"
        destination = work / f"core_original_{index:04d}.wav"
        execute(command() + ["-i", source["source_path"], "-map", f"0:{source['audio_stream_index']}", "-vn",
                "-af", af_chain,
                "-ac", str(channels), "-ar", "48000", "-c:a", "pcm_s16le", str(destination)], destination)
        decoded = pcm_samples(destination)
        if decoded <= 0:
            raise ValueError(f"核心素材“{Path(source['source_path']).name}”没有可解码的原声样本")
        decoded_entries.append((source, start_sec, decoded))
        core_wavs.append(destination)
        total_samples += decoded

    effective_tempo = 1.0
    if use_tail_tempo:
        min_floor_raw = head_samples + math.floor(tail_out_samples * 0.92)
        if total_samples < min(target_samples, min_floor_raw):
            raise ValueError(f"实际可解码核心原声只有 {total_samples / 48000:.3f} 秒，视频主体需要 {target_samples / 48000:.3f} 秒；请降低倍率或增加核心素材")
        max_avail_tempo = (total_samples - head_samples - 480) / max(1, tail_out_samples)
        effective_tempo = min(requested_tail_tempo, max(0.92, max_avail_tempo))
        if abs(effective_tempo - 1.0) < 0.005:
            if total_samples >= head_samples + math.ceil(tail_out_samples * 0.98) + 480:
                effective_tempo = 0.98
            else:
                use_tail_tempo = False
                effective_tempo = 1.0
    if not use_tail_tempo and total_samples < target_samples:
        raise ValueError(f"实际可解码核心原声只有 {total_samples / 48000:.3f} 秒，视频主体需要 {target_samples / 48000:.3f} 秒；请降低倍率或增加核心素材")

    raw_needed_exact = (
        head_samples + min(max(0, total_samples - head_samples), math.ceil(tail_out_samples * effective_tempo))
        if use_tail_tempo
        else target_samples
    )
    consumed_so_far = 0
    for source, start_sec, decoded in decoded_entries:
        used = max(0, min(decoded, raw_needed_exact - consumed_so_far))
        consumed_so_far += used
        published_sources.append({
            **source,
            "source_start_seconds": start_sec,
            "source_end_seconds": start_sec + used / 48000,
            "decoded_sample_count": decoded,
            "used_sample_count": used,
            "duration_seconds": used / 48000,
            "audio_tail_cut": used < decoded,
        })

    body_audio = work / "core_body_prefix.wav"
    if use_tail_tempo:
        if len(core_wavs) == 1:
            core_raw_concat = core_wavs[0]
        else:
            core_list = work / "core_original_audio.txt"
            playlist(core_list, core_wavs)
            core_raw_concat = work / "core_raw_concat.wav"
            execute(
                command() + [
                    "-f", "concat", "-safe", "0", "-i", str(core_list), "-vn",
                    "-c:a", "pcm_s16le", "-ar", "48000", "-ac", str(channels),
                    str(core_raw_concat),
                ],
                core_raw_concat,
            )

        def _run_tempo_pass(stretch_expr):
            body_audio.unlink(missing_ok=True)
            if head_samples > 0:
                fc = (
                    f"[0:a]asplit=2[ah][at];"
                    f"[ah]atrim=start_sample=0:end_sample={head_samples},asetpts=PTS-STARTPTS[head];"
                    f"[at]atrim=start_sample={head_samples},asetpts=PTS-STARTPTS,"
                    f"{stretch_expr},aresample=48000,apad,"
                    f"atrim=end_sample={tail_out_samples},asetpts=PTS-STARTPTS[tail];"
                    f"[head][tail]concat=n=2:v=0:a=1,atrim=end_sample={target_samples},asetpts=PTS-STARTPTS[out]"
                )
                execute(
                    command() + [
                        "-i", str(core_raw_concat), "-vn",
                        "-filter_complex", fc, "-map", "[out]",
                        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", str(channels),
                        str(body_audio),
                    ],
                    body_audio,
                )
            else:
                af = f"{stretch_expr},aresample=48000,apad,atrim=end_sample={target_samples},asetpts=PTS-STARTPTS"
                execute(
                    command() + [
                        "-i", str(core_raw_concat), "-vn", "-af", af,
                        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", str(channels),
                        str(body_audio),
                    ],
                    body_audio,
                )

        try:
            _run_tempo_pass(f"rubberband=tempo={effective_tempo:.5f}:pitch=1.0")
        except Exception:
            _run_tempo_pass(f"atempo={effective_tempo:.5f}")
        emit(
            f"原声接力防重：开场核心 {head_samples / 48000:.2f}s 保持 1.000x 原速对口型，"
            f"后续打乱段语速 {effective_tempo:.3f}x（保持原音高）"
        )
        if dedup_cfg is not None:
            dedup_cfg["tail_audio_tempo"] = round(effective_tempo, 5)
        plan["core_head_untouched_samples"] = head_samples
        plan["core_tail_tempo_applied"] = round(effective_tempo, 5)
    else:
        core_list = work / "core_original_audio.txt"
        playlist(core_list, core_wavs)
        execute(command() + ["-f", "concat", "-safe", "0", "-i", str(core_list), "-vn",
                             "-af", f"atrim=end_sample={target_samples},asetpts=PTS-STARTPTS",
                             "-c:a", "pcm_s16le", "-ar", "48000", "-ac", str(channels), str(body_audio)], body_audio)
    if pcm_samples(body_audio) != target_samples:
        raise RuntimeError("核心原声实际样本不能完整覆盖视频主体，未生成成片")
    plan.update(core_pcm_available_samples=total_samples, core_pcm_delivered_samples=target_samples,
                core_audio_coverage_verified=True, audio_sources=published_sources,
                core_audio_sources_used=len(core_wavs))
    outputs.append(body_audio)
    return outputs


def render_edit(core_paths, shuffle_paths, output_path, *, opening_path=None,
                cut_seconds=3.0, width=720, height=1280, fps=30, seed=0,
                ffmpeg="ffmpeg", ffprobe="ffprobe", run=None, log=None,
                shot_selector=None, export_audio=True, visual_policy="interleave", shuffle_ranges=None,
                core_ranges=None, shuffle_budget_seconds=None,
                core_audio_policy="legacy_selected_then_silence", core_display_modes=None,
                primary_shuffle_budget_seconds=None, core_timeline_seconds=None, primary_selection_first=False,
                fixed_shuffle_cuts=False, bottom_layers=None, shuffle_masks=None, unique_batch=None, visual_batch=None,
                batch_picture_policy="per_core", duration_multiplier=None, variant_index=1, tier1_dedup=True):
    """Render pictures and independent original sound, plus optional .m4a.

    ``run(command, output_path)`` must raise on failure and honor cancellation.
    ``shot_selector(plan, work_dir)`` may match only B-roll slots; unmatched slots
    can return to their original core picture. No model is called by this module.
    """
    emit = log or (lambda message: None)
    output = Path(output_path).resolve()
    if output.suffix.lower() != ".mp4":
        raise ValueError("成片文件须使用 .mp4 扩展名")
    output.parent.mkdir(parents=True, exist_ok=True)
    audio_output = output.with_suffix(".m4a")
    manifest_output = output.with_suffix(".manifest.json")
    plan = plan_edit(core_paths, shuffle_paths, opening_path=opening_path, cut_seconds=cut_seconds,
                     width=width, height=height, fps=fps, seed=seed, ffprobe=ffprobe,
                     visual_policy=visual_policy, shuffle_ranges=shuffle_ranges, core_ranges=core_ranges,
                     shuffle_budget_seconds=shuffle_budget_seconds, core_audio_policy=core_audio_policy,
                     core_display_modes=core_display_modes,
                     primary_shuffle_budget_seconds=primary_shuffle_budget_seconds,
                     core_timeline_seconds=core_timeline_seconds, primary_selection_first=primary_selection_first,
                     fixed_shuffle_cuts=fixed_shuffle_cuts, unique_batch=unique_batch, visual_batch=visual_batch,
                     batch_picture_policy=batch_picture_policy, bottom_layers=bottom_layers,
                     duration_multiplier=duration_multiplier, variant_index=variant_index, tier1_dedup=tier1_dedup)
    if visual_policy in {"append_shuffle", "full_core_timeline"} and shot_selector is not None:
        raise ValueError("随机画面模式不使用语义选镜器")
    protected_paths = {os.path.normcase(path) for path in plan["sources"]}
    if os.path.normcase(str(output)) in protected_paths or (export_audio and os.path.normcase(str(audio_output)) in protected_paths):
        raise ValueError("输出路径不能覆盖任何素材")
    def execute(command, destination):
        if run:
            result = run(command, destination)
            if getattr(result, "returncode", 0):
                raise RuntimeError("FFmpeg 处理失败")
        else:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                raise RuntimeError("FFmpeg 处理失败：" + result.stdout.decode("utf-8", "replace")[-1500:])
        if not destination.is_file() or destination.stat().st_size == 0:
            raise RuntimeError("FFmpeg 没有生成有效媒体")
    def command():
        return [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-loglevel", "warning", "-threads", "2"]
    def playlist(path, entries):
        path.write_text("".join("file '" + item.as_posix().replace("'", "'\\''") + "'\n" for item in entries), encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix=".original-audio-", dir=str(output.parent)) as directory:
        work = Path(directory)
        if shot_selector:
            emit("正在按原声内容匹配用户勾选的打乱画面…")
            plan = _validate_selection(plan, shot_selector(copy.deepcopy(plan), work))
        if plan["short_core_adjustment"]:
            emit(plan["short_core_note"])
        prepared_audio = (_prepare_original_core_prefix(plan, work, execute, command, playlist, emit)
                          if plan.get("core_audio_policy") == "original_core_prefix" else None)
        from bottom_overlay import prepare_layers, video_filter
        first_core_end = _first_core_end(plan)
        layers = (prepare_layers(bottom_layers, work, width, height, plan["output_duration_seconds"], first_core_end=first_core_end)
                  if bottom_layers and any(shot["role"] == "shuffle" for shot in plan["shots"]) else [])
        if layers:
            emit(f"按成片时间在打乱镜头底部叠加 {len(layers)} 张图片。")
        shuffle_shots = [shot for shot in plan["shots"] if shot["role"] == "shuffle"]
        capture_source = visual_batch is not None and (bool(shuffle_masks) or any(
            layer["start"] < shot["timeline_start_seconds"] + shot["duration_seconds"]
            and layer["end"] > shot["timeline_start_seconds"] for shot in shuffle_shots for layer in layers))
        gray_track = work / "shuffle_before_effects.gray"
        clips = []
        for index, shot in enumerate(plan["shots"]):
            emit(f"静音画面 {index + 1}/{len(plan['shots'])}：{shot['role']}，{shot['duration_seconds']:.2f} 秒")
            destination = work / f"shot_{index:05d}.mp4"
            cmd = command()
            if shot["role"] == "solid":
                cmd += ["-f", "lavfi", "-i", f"color=c={shot['solid_color']}:s={plan['width']}x{plan['height']}:r={plan['fps']}",
                        ]
                stream = "0:v:0"
                fit = "null"
            else:
                source = plan["sources"][shot["source_path"]]
                if visual_policy == "interleave" and shot["role"] == "shuffle" and shot["source_start_seconds"] + shot["duration_seconds"] > source["duration_seconds"]:
                    cmd += ["-stream_loop", "-1"]
                cmd += ["-ss", f"{shot['source_start_seconds']:.9f}"]
                if shot.get("source_end_seconds") is not None:
                    cmd += ["-t", f"{shot['source_end_seconds'] - shot['source_start_seconds']:.9f}"]
                fit = (f"scale={plan['width']}:{plan['height']}:force_original_aspect_ratio=decrease,pad={plan['width']}:{plan['height']}:(ow-iw)/2:(oh-ih)/2"
                       if shot.get("picture_fit") == "contain" else
                       f"scale={plan['width']}:{plan['height']}:force_original_aspect_ratio=increase,crop={plan['width']}:{plan['height']}")
                cmd += ["-i", shot["source_path"]]
                stream = f"0:{source['video_stream_index']}"
            shot_speed = float(shot.get("broll_speed") or 1.0) if shot["role"] == "shuffle" else 1.0
            speed_stage = (
                f",setpts=(PTS-STARTPTS)/{shot_speed:.6f}"
                if abs(shot_speed - 1.0) > 1e-4
                else ""
            )
            picture_filter = fit + f",setsar=1{speed_stage},fps={plan['fps']},tpad=stop_mode=clone:stop_duration={shot['duration_seconds']:.9f},trim=end_frame={shot['frame_count']},setpts=N/({plan['fps']}*TB),format=yuv420p"
            shot_layers = layers if shot["role"] == "shuffle" else []
            capture_shot = capture_source and shot["role"] == "shuffle"
            cmd += video_filter(shot_layers, stream, picture_filter, shot["timeline_start_seconds"], shot["duration_seconds"], height,
                                masks=shuffle_masks if shot["role"] == "shuffle" else None, width=width,
                                capture_source=capture_shot)
            cmd += ["-an", "-frames:v", str(shot["frame_count"]),
                    "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-crf", "18",
                    "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(plan["fps"]),
                    "-video_track_timescale", "90000", "-movflags", "+faststart", str(destination)]
            if capture_shot:
                gray_shot = work / "current_shuffle.gray"
                cmd += ["-map", "[source_gray]", "-an", "-c:v", "rawvideo", "-threads", "1", "-pix_fmt", "gray",
                        "-fps_mode", "passthrough", "-frames:v", str(shot["frame_count"]), "-f", "rawvideo", str(gray_shot)]
            execute(cmd, destination)
            if visual_batch is not None and _probe(destination, ffprobe)["container_frame_count"] != shot["frame_count"]:
                raise ValueError("编码镜头帧数与制作记录不一致，未发布成片。")
            if capture_shot:
                if gray_shot.stat().st_size != shot["frame_count"] * 72:
                    raise ValueError("打乱镜头的原始指纹帧数不完整，未发布成片。")
                with gray_shot.open("rb") as part, gray_track.open("ab") as combined:
                    shutil.copyfileobj(part, combined)
            clips.append(destination)
        wavs = list(prepared_audio or [])
        for index, source in enumerate([] if prepared_audio is not None else plan["audio_sources"]):
            emit("打乱片段全部静音" if source.get("role") == "shuffle_silence" else (
                f"核心原声第 {source['loop_iteration'] + 1} 轮：覆盖后续画面（打乱素材原声关闭）"
                if source.get("role") == "core_repeat" else
                f"提取连续原声 {index + 1}/{len(plan['audio_sources'])}（保持原速度和音量）"))
            destination = work / f"audio_{index:04d}.wav"
            cmd = command()
            if source["has_audio"]:
                if source.get("source_start_seconds") is not None:
                    cmd += ["-ss", f"{source['source_start_seconds']:.9f}",
                            "-t", f"{source['source_end_seconds'] - source['source_start_seconds']:.9f}"]
                cmd += ["-i", source["source_path"], "-map", f"0:{source['audio_stream_index']}"]
            else:
                layout = "mono" if plan["output_audio_channels"] == 1 else "stereo"
                cmd += ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl={layout}", "-map", "0:a:0"]
            # FFmpeg's implicit mono -> stereo rematrix attenuates by 3 dB.
            # Explicit channel duplication keeps the original speech gain.
            channel_filter = "pan=stereo|c0=c0|c1=c0," if source["audio_channels"] == 1 and plan["output_audio_channels"] == 2 else ""
            audio_trim = f"end_sample={source['sample_count']}" if source.get("sample_count") else f"duration={source['duration_seconds']:.9f}"
            cmd += ["-vn", "-af", channel_filter + f"aresample=48000,apad,atrim={audio_trim},asetpts=PTS-STARTPTS",
                    "-ac", str(plan["output_audio_channels"]), "-ar", "48000", "-c:a", "pcm_s16le", str(destination)]
            execute(cmd, destination)
            wavs.append(destination)
        audio_list, video_list = work / "audio.txt", work / "video.txt"
        playlist(audio_list, wavs)
        playlist(video_list, clips)
        pending_audio = work / "original.m4a"
        execute(command() + ["-f", "concat", "-safe", "0", "-i", str(audio_list), "-vn", "-c:a", "aac", "-b:a", "192k",
                             "-ar", "48000", "-ac", str(plan["output_audio_channels"]), "-movflags", "+faststart", str(pending_audio)], pending_audio)
        pending_video = work / "completed.mp4"
        emit("合并静音画面与开头＋核心原声…")
        execute(command() + ["-f", "concat", "-safe", "0", "-i", str(video_list), "-i", str(pending_audio),
                             "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-movflags", "+faststart",
                             str(pending_video)], pending_video)
        verification = _probe(pending_video, ffprobe)
        tolerance = max(0.12, 2 / plan["fps"])
        if (verification["video_stream_count"] != 1 or verification["audio_stream_count"] != 1
                or abs(verification["video_duration_seconds"] - plan["output_duration_seconds"]) > tolerance
                or abs(verification["audio_duration_seconds"] - plan["output_duration_seconds"]) > tolerance):
            raise RuntimeError("成片音轨或总时长验收失败，未发布结果")
        if (plan.get("core_audio_policy") == "original_core_prefix"
                and verification["audio_duration_seconds"] + 0.002 < verification["video_duration_seconds"]):
            raise RuntimeError("封装后的原声音轨早于视频结束，未发布结果")
        plan.update({"output_path": str(output), "audio_path": str(audio_output) if export_audio else None,
                     "audio_exported": bool(export_audio),
                     "manifest_path": str(manifest_output), "verified_duration_seconds": verification["duration_seconds"],
                     "verified_video_duration_seconds": verification["video_duration_seconds"],
                     "verified_audio_duration_seconds": verification["audio_duration_seconds"],
                     "audio_covers_video_verified": (verification["audio_duration_seconds"] + 0.002 >= verification["video_duration_seconds"]),
                     "shuffle_audio_used": False, "audio_reencoded_once": True,
                     "core_picture_frames": sum(s["frame_count"] for s in plan["shots"] if s["role"] == "core"),
                     "shuffle_picture_frames": sum(s["frame_count"] for s in plan["shots"] if s["role"] == "shuffle")})
        if visual_batch is not None:
            emit("正在从实际成片核对打乱画面…")
            from shuffle_similarity import rendered_shuffle_hashes, source_shuffle_hashes
            frames = plan["shuffle_picture_frames"]
            rendered = rendered_shuffle_hashes(pending_video, plan, ffmpeg, execute)
            source = source_shuffle_hashes(gray_track, frames, fps, ffmpeg, execute) if capture_source else None
            plan["shuffle_visual_verification"] = visual_batch.verify(
                pending_video, plan, rendered_hashes=rendered, source_hashes=source, overlays=layers,
                **({"destination": output} if getattr(visual_batch, "history", None) is not None else {}))
        manifest = work / "manifest.json"
        manifest.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        if export_audio:
            os.replace(pending_audio, audio_output)
        os.replace(manifest, manifest_output)
        os.replace(pending_video, output)
    emit(f"原声剪辑完成：{plan['output_duration_seconds']:.2f} 秒" + ("；音频已单独提取" if export_audio else "；原声仅保留在成片中"))
    return plan
