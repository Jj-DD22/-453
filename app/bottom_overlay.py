"""Timed, translucent stills on the final movie timeline."""
import hashlib
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile

from PIL import Image, ImageChops, ImageOps


@lru_cache(maxsize=40)
def preview_frame(path, file_version, seconds, cover, ffmpeg):
    """Cache reference stills by source revision, crop position and picture fit."""
    fit = ("increase,crop=360:640" if cover else "decrease,pad=360:640:(ow-iw)/2:(oh-ih)/2")
    for position in dict.fromkeys((seconds, 0)):
        result = subprocess.run(
            [ffmpeg, "-nostdin", "-v", "error", "-threads", "2", "-ss", str(position),
             "-i", str(path), "-map", "0:V:0", "-frames:v", "1", "-an", "-filter_threads", "1",
             "-vf", "scale=360:640:force_original_aspect_ratio=" + fit + ",setsar=1",
             "-c:v", "mjpeg", "-threads", "1", "-q:v", "3", "-f", "image2pipe", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode == 0 and result.stdout:
            return result.stdout
    raise ValueError("无法读取视频的参考画面。")


def _store_image(upload, directory):
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, suffix=".png", delete=False) as temp:
        pending = Path(temp.name)
    try:
        try:
            with Image.open(upload.stream, formats=["PNG", "JPEG", "WEBP"]) as source:
                source.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                ImageOps.exif_transpose(source).convert("RGBA").save(pending, format="PNG")
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise ValueError("无法读取图片，请选择有效的 PNG、JPG 或 WebP 图片。") from exc
        with pending.open("rb") as handle:
            key = hashlib.file_digest(handle, "sha256").hexdigest()
        target = directory / (key + ".png")
        if not target.is_file():
            os.replace(pending, target)
        return key
    finally:
        pending.unlink(missing_ok=True)


def read_selection(raw, files, root, *, after_primary=False):
    rows = json.loads(raw)
    if not isinstance(rows, list):
        raise ValueError("底部叠图设置须为图片列表。")
    plan = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("底部叠图设置无效。")
        raw_anchor = row.get("anchor")
        if raw_anchor is None or raw_anchor == "":
            anchor = "first_core_end" if after_primary else None
        elif raw_anchor in ("first_core_end", "movie_clock"):
            anchor = raw_anchor
        else:
            raise ValueError("图片显示起点模式无效。")
        if anchor == "first_core_end":
            try:
                duration, opacity = (float(row[key]) for key in ("duration", "opacity"))
            except (KeyError, ValueError, TypeError, OverflowError) as exc:
                raise ValueError("请填写图片的显示时长和不透明度。") from exc
            if not (math.isfinite(duration) and math.isfinite(opacity)) or duration <= 0 or not 0 <= opacity <= 100:
                raise ValueError("图片显示时长须大于零，不透明度须在 0–100% 之间。")
            start = 0.0
        else:
            try:
                start, duration, opacity = (float(row[key]) for key in ("start", "duration", "opacity"))
            except (KeyError, ValueError, TypeError, OverflowError) as exc:
                raise ValueError("请填写图片的开始秒数、显示时长和不透明度。") from exc
            if (not math.isfinite(start + duration) or not math.isfinite(opacity)
                    or start < 0 or duration <= 0 or not 0 <= opacity <= 100):
                raise ValueError("图片开始秒数须为零或正数，显示时长须大于零，不透明度须在 0–100% 之间。")
        try:
            size_percent = float(row.get("size_percent", 100))
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("图片大小须在 10–200% 之间。") from exc
        if not 10 <= size_percent <= 200:
            raise ValueError("图片大小须在 10–200% 之间。")
        upload = files.get(f"overlay_image_{index}")
        if upload is None or not upload.filename:
            raise ValueError("缺少已勾选的图片，请重新选择。")
        key = _store_image(upload, Path(root) / ".v17_bottom_overlays")
        if anchor == "first_core_end":
            timing = dict(anchor="first_core_end", duration=duration)
        elif anchor == "movie_clock":
            timing = dict(anchor="movie_clock", start=start, end=start + duration)
        else:
            timing = dict(start=start, end=start + duration)
        plan.append(dict(image=key, opacity=opacity, size_percent=size_percent, **timing))
    return plan


def resolve_images(plan, root):
    layers = []
    for row in plan or ():
        key = row["image"]
        if not re.fullmatch("[0-9a-f]{64}", key):
            raise ValueError("底部叠图记录无效。")
        path = Path(root) / ".v17_bottom_overlays" / (key + ".png")
        if not path.is_file():
            raise ValueError("任务所用的底部图片已丢失，请重新添加。")
        layers.append(dict(row, path=path))
    return layers


def layer_layouts(layers, width, height, movie_duration, first_core_end=None, *, scale_jitter=1.0, y_jitter_px=0):
    """Read image dimensions and describe the bottom layers on the output timeline."""
    jitter_scale = float(scale_jitter) if scale_jitter is not None else 1.0
    dy = int(round(y_jitter_px or 0))
    bottom_margin = max(0, round(height * .025) - dy)
    for layer in layers:
        if layer.get("anchor") == "movie_clock":
            start, end = layer["start"], layer["end"]
        elif first_core_end is not None:
            # Older queue snapshots kept an absolute interval. Its length is the requested display time.
            duration = layer["duration"] if "duration" in layer else layer["end"] - layer["start"]
            start, end = first_core_end, first_core_end + duration
        elif layer.get("anchor") == "first_core_end":
            raise ValueError("未找到首条核心选段的成片结束时间。")
        else:
            start, end = layer["start"], layer["end"]
        if start >= movie_duration or layer["opacity"] == 0:
            continue
        with Image.open(layer["path"]) as source:
            base_scale = min(width * .82 / source.width, height * .30 / source.height)
            scale = base_scale * layer.get("size_percent", 100) / 100 * jitter_scale
            size = (
                max(1, min(width, round(source.width * scale))),
                max(1, min(height, round(source.height * scale))),
            )
        top_fraction = max(0.0, min(1.0, (height - size[1] - bottom_margin) / height))
        yield layer, size, dict(start=start, end=min(movie_duration, end),
                                top_fraction=top_fraction)


def unobscured_fraction(layouts, intervals):
    return min((layout["top_fraction"] for layout in layouts
                if any(layout["start"] < end and layout["end"] > start for start, end in intervals)),
               default=1.)


def cut_top_fractions(spans, layouts, fps):
    """Return the unobscured fraction of each shot using actual output frame indices."""
    layers = [(math.ceil(layer["start"] * fps - 1e-8),
               math.ceil(layer["end"] * fps - 1e-8), layer["top_fraction"])
              for layer in layouts]
    return tuple(min((top for first, stop, top in layers
                      if max(start, first) < min(end, stop)), default=1.)
                 for start, end in spans)


def prepare_layers(
    layers,
    directory,
    width,
    height,
    movie_duration,
    *,
    first_core_end=None,
    scale_jitter=1.0,
    x_jitter_px=0,
    y_jitter_px=0,
):
    """Prepare small RGBA inputs once per movie. Keep the original alpha channel."""
    dx = int(round(x_jitter_px or 0))
    dy = int(round(y_jitter_px or 0))
    prepared = []
    for index, (layer, size, layout) in enumerate(
        layer_layouts(
            layers,
            width,
            height,
            movie_duration,
            first_core_end,
            scale_jitter=scale_jitter,
            y_jitter_px=dy,
        )
    ):
        with Image.open(layer["path"]) as source:
            picture = source.convert("RGBA").resize(size, Image.Resampling.LANCZOS)
        feather = max(1, round(size[1] * .10))
        fade = Image.new("L", (1, size[1]))
        fade.putdata([round(255 * layer["opacity"] / 100 * min(1, y / feather)) for y in range(size[1])])
        picture.putalpha(ImageChops.multiply(picture.getchannel("A"), fade.resize(size, Image.Resampling.NEAREST)))
        tile = Path(directory) / f"bottom-{index}.png"
        picture.save(tile)
        item = dict(path=tile, **layout)
        if dx:
            item["x_jitter_px"] = dx
        if dy:
            item["y_jitter_px"] = dy
        prepared.append(item)
    return prepared


def _overlay_filter(previous, image, output, layer, start, height, *, x_jitter_px=0, y_jitter_px=0):
    left = max(0, layer["start"] - start)
    right = layer["end"] - start
    dx = int(round(x_jitter_px if x_jitter_px else layer.get("x_jitter_px", 0)))
    dy = int(round(y_jitter_px if y_jitter_px else layer.get("y_jitter_px", 0)))
    base_margin = round(height * .025)
    if dx == 0 and dy == 0:
        return (f"[{previous}][{image}]overlay=x=(W-w)/2:y=H-h-{base_margin}:"
                f"enable='gte(t,{left:.9f})*lt(t,{right:.9f})'[{output}]")
    x_expr = "(W-w)/2" if dx == 0 else f"max(0,min(W-w,(W-w)/2{dx:+d}))"
    margin = max(0, base_margin - dy)
    y_expr = f"H-h-{base_margin}" if dy == 0 else f"max(0,min(H-h,H-h-{margin}))"
    return (f"[{previous}][{image}]overlay=x='{x_expr}':y='{y_expr}':"
            f"enable='gte(t,{left:.9f})*lt(t,{right:.9f})'[{output}]")


def read_shuffle_masks(raw):
    if len(raw) > 8192:
        raise ValueError("字幕遮挡框设置过大。")
    settings = json.loads(raw)
    if not isinstance(settings, dict) or not isinstance(settings.get("boxes"), list):
        raise ValueError("字幕遮挡框格式无效。")
    raw_style = settings.get("style")
    style = "blur" if raw_style is None else raw_style
    if not isinstance(style, str) or style not in ("blur", "delogo", "dark"):
        raise ValueError("字幕遮挡框样式无效。")
    strength = settings.get("strength")
    if type(strength) is not int or not 1 <= strength <= 100 or not 1 <= len(settings["boxes"]) <= 8:
        raise ValueError("可设置 1 至 8 个遮挡框，模糊强度为 1 至 100。")
    rects = []
    for box in settings["boxes"]:
        if not isinstance(box, list) or len(box) != 4 or any(type(v) not in (int, float) or not 0 <= v <= 1 for v in box):
            raise ValueError("字幕遮挡框坐标无效。")
        left, top, right, bottom = box
        if right <= left or bottom <= top:
            raise ValueError("请拉出有宽度和高度的字幕遮挡框。")
        rects.append(box)
    if style == "blur":
        return dict(strength=strength, boxes=rects)
    return dict(strength=strength, style=style, boxes=rects)


def _blur_boxes(graph, source, masks, width, height):
    """Apply blur, delogo or dark fill masks onto source rectangles."""
    boxes = masks["boxes"]
    style = masks.get("style") or "blur"
    if style == "delogo":
        previous = source
        for i, (left, top, right, bottom) in enumerate(boxes):
            x = max(1, min(width - 4, round(left * width)))
            y = max(1, min(height - 4, round(top * height)))
            w = max(2, min(width - 1 - x, round((right - left) * width)))
            h = max(2, min(height - 1 - y, round((bottom - top) * height)))
            nxt = f"{source}_mask_out{i}"
            graph.append(f"[{previous}]delogo=x={x}:y={y}:w={w}:h={h}:show=0[{nxt}]")
            previous = nxt
        return previous
    if style == "dark":
        alpha = min(0.96, max(0.35, 0.50 + 0.45 * masks["strength"] / 100.0))
        previous = source
        for i, (left, top, right, bottom) in enumerate(boxes):
            x = max(0, min(width - 2, round(left * width)))
            y = max(0, min(height - 2, round(top * height)))
            w = max(2, min(width - x, round((right - left) * width)))
            h = max(2, min(height - y, round((bottom - top) * height)))
            nxt = f"{source}_mask_out{i}"
            graph.append(f"[{previous}]drawbox=x={x}:y={y}:w={w}:h={h}:color=0x0f172a@{alpha:.3f}:t=fill[{nxt}]")
            previous = nxt
        return previous
    if style != "blur":
        raise ValueError("字幕遮挡框样式无效。")
    prefix = source + "_mask"
    graph.append(f"[{source}]split[{prefix}base][{prefix}src]")
    sigma = max(.5, width * .0007 * masks["strength"])
    branches = "".join(f"[{prefix}src{i}]" for i in range(len(boxes)))
    split = f",split={len(boxes)}" if len(boxes) > 1 else ""
    graph.append(f"[{prefix}src]gblur=sigma={sigma:.4f}:steps=2{split}{branches}")
    previous = prefix + "base"
    for i, (left, top, right, bottom) in enumerate(boxes):
        x, y = min(width-2,2*math.floor(left*width/2)), min(height-2,2*math.floor(top*height/2))
        w = min(width-x, max(2,2*math.ceil(right*width/2)-x))
        h = min(height-y, max(2,2*math.ceil(bottom*height/2)-y))
        graph.append(f"[{prefix}src{i}]crop={w}:{h}:{x}:{y}[{prefix}tile{i}]")
        graph.append(f"[{previous}][{prefix}tile{i}]overlay=x={x}:y={y}[{prefix}out{i}]")
        previous = f"{prefix}out{i}"
    return previous


def video_filter(
    layers,
    source,
    base_filter,
    start,
    duration,
    height,
    *,
    masks=None,
    width=None,
    capture_source=False,
    x_jitter_px=0,
    y_jitter_px=0,
):
    """Return input/map options for one base video input and its active stills."""
    active = [row for row in layers if row["start"] < start + duration and row["end"] > start]
    if not active and not masks and not capture_source:
        return ["-map", source, "-vf", base_filter, "-filter_threads", "1"]
    inputs = []
    if capture_source:
        graph = [f"[{source}]{base_filter},split=2[base][source_copy]",
                 "[source_copy]scale=9:8:flags=area,format=gray[source_gray]"]
    else:
        graph = [f"[{source}]{base_filter}[base]"]
    previous = _blur_boxes(graph, "base", masks, width, height) if masks else "base"
    for index, row in enumerate(active, 1):
        inputs += ["-i", str(row["path"])]
        output = f"layer{index}"
        graph.append(
            _overlay_filter(
                previous,
                f"{index}:v",
                output,
                row,
                start,
                height,
                x_jitter_px=x_jitter_px,
                y_jitter_px=y_jitter_px,
            )
        )
        previous = output
    return inputs + ["-filter_complex_threads", "1", "-filter_complex", ";".join(graph), "-map", f"[{previous}]"]


def _merge_intervals(intervals):
    merged = []
    for start, end in sorted((float(s), float(e)) for s, e in intervals if float(e) > float(s)):
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def resolve_shuffle_timeline(clips, durations, shuffle_paths, opening=0.0):
    """Resolve local (base_concat) and finished-movie intervals for shuffle clips."""
    opening = float(opening or 0.0)
    shuffle_set = set(shuffle_paths or ())
    segments = []
    raw_local = []
    local_cursor = 0.0
    for clip, raw_duration in zip(clips, durations):
        duration = max(0.0, float(raw_duration))
        local_start = local_cursor
        local_end = local_cursor + duration
        movie_start = opening + local_start
        movie_end = opening + local_end
        is_shuffle = clip in shuffle_set
        segments.append(dict(
            clip=clip,
            duration=duration,
            is_shuffle=is_shuffle,
            local_start=local_start,
            local_end=local_end,
            movie_start=movie_start,
            movie_end=movie_end,
        ))
        if is_shuffle and duration > 0:
            raw_local.append((local_start, local_end))
        local_cursor = local_end
    shuffle_intervals = _merge_intervals(raw_local)
    movie_intervals = [(opening + s, opening + e) for s, e in shuffle_intervals]
    first_core_end = movie_intervals[0][0] if movie_intervals else None
    return dict(
        segments=segments,
        shuffle_intervals=shuffle_intervals,
        movie_intervals=movie_intervals,
        first_core_end=first_core_end,
        opening=opening,
        base_duration=local_cursor,
        movie_duration=opening + local_cursor,
    )


def shuffle_concat_labels(
    command,
    graph,
    clips,
    durations,
    shuffle_paths,
    layers,
    opening,
    directory,
    width,
    height,
    masks=None,
    *,
    scale_jitter=1.0,
    x_jitter_px=0,
    y_jitter_px=0,
):
    """Attach stills and/or masks to the identified shuffle streams before natural-mode concat."""
    timeline = resolve_shuffle_timeline(clips, durations, shuffle_paths, opening)
    if not timeline["shuffle_intervals"]:
        return [f"[v{index}]" for index in range(len(clips))]
    first_core_end = timeline["first_core_end"]
    resolved_layers = [
        dict(layer, anchor=None, start=first_core_end, end=first_core_end + layer["duration"])
        if layer.get("anchor") == "first_core_end" else layer
        for layer in (layers or ())
    ]
    prepared = (
        prepare_layers(
            resolved_layers,
            directory,
            width,
            height,
            timeline["movie_duration"],
            scale_jitter=scale_jitter,
            x_jitter_px=x_jitter_px,
            y_jitter_px=y_jitter_px,
        )
        if resolved_layers
        else []
    )
    picture_inputs = {}
    labels = []
    for index, seg in enumerate(timeline["segments"]):
        previous = f"v{index}"
        if seg["is_shuffle"]:
            start = seg["movie_start"]
            duration = seg["duration"]
            if masks:
                previous = _blur_boxes(graph, previous, masks, width, height)
            for layer_index, layer in enumerate(prepared):
                if layer["start"] >= start + duration or layer["end"] <= start:
                    continue
                path = layer["path"]
                if path not in picture_inputs:
                    picture_inputs[path] = len(clips) + len(picture_inputs)
                    command.extend(["-i", str(path)])
                output = f"shuffle{index}_layer{layer_index}"
                graph.append(
                    _overlay_filter(
                        previous,
                        f"{picture_inputs[path]}:v",
                        output,
                        layer,
                        start,
                        height,
                        x_jitter_px=x_jitter_px,
                        y_jitter_px=y_jitter_px,
                    )
                )
                previous = output
        labels.append(f"[{previous}]")
    return labels


def append_post_zoom_bottom_layers(
    command, filter_parts, v_out, inputs_count, layers, shuffle_intervals,
    opening, directory, width, height, *, base_duration=None,
    scale_jitter=1.0, x_jitter_px=0, y_jitter_px=0,
):
    """Overlay fixed-geometry bottom stills after TIMEPIN_ZOOM on shuffle intervals only."""
    merged_intervals = _merge_intervals(
        (max(0.0, float(s)), max(0.0, float(e)))
        for s, e in (shuffle_intervals or ())
        if float(e) - float(s) > 1e-4
    )
    if not layers or not merged_intervals:
        return v_out, inputs_count
    opening = float(opening or 0.0)
    first_core_end = opening + merged_intervals[0][0]
    resolved_layers = [
        dict(layer, anchor=None, start=first_core_end, end=first_core_end + float(layer["duration"]))
        if layer.get("anchor") == "first_core_end" else layer
        for layer in layers
    ]
    total_base_duration = max(merged_intervals[-1][1], float(base_duration or 0.0))
    prepared = prepare_layers(
        resolved_layers,
        directory,
        width,
        height,
        opening + total_base_duration,
        scale_jitter=scale_jitter,
        x_jitter_px=x_jitter_px,
        y_jitter_px=y_jitter_px,
    )
    picture_inputs = {}
    dx = int(round(x_jitter_px or 0))
    dy = int(round(y_jitter_px or 0))
    base_margin = round(height * .025)
    for idx, layer in enumerate(prepared):
        ls = layer["start"] - opening
        le = layer["end"] - opening
        active_spans = [
            (a, b)
            for s, e in merged_intervals
            if (b := min(le, e)) - (a := max(ls, s)) > 1e-4
        ]
        if not active_spans:
            continue
        path_str = str(layer["path"])
        if path_str not in picture_inputs:
            picture_inputs[path_str] = inputs_count
            command.extend(["-i", path_str])
            inputs_count += 1
        img_idx = picture_inputs[path_str]
        enable_expr = "+".join(f"gte(t,{a:.6f})*lt(t,{b:.6f})" for a, b in active_spans)
        v_in = v_out if v_out.startswith("[") else f"[{v_out}]"
        next_v = f"[v_dongge_btm_{idx}]"
        if dx == 0 and dy == 0:
            filter_parts.append(
                f"{v_in}[{img_idx}:v]overlay=x=(W-w)/2:y=H-h-{base_margin}:"
                f"enable='{enable_expr}'{next_v}"
            )
        else:
            x_expr = "(W-w)/2" if dx == 0 else f"max(0,min(W-w,(W-w)/2{dx:+d}))"
            margin = max(0, base_margin - dy)
            y_expr = f"H-h-{base_margin}" if dy == 0 else f"max(0,min(H-h,H-h-{margin}))"
            filter_parts.append(
                f"{v_in}[{img_idx}:v]overlay=x='{x_expr}':y='{y_expr}':"
                f"enable='{enable_expr}'{next_v}"
            )
        v_out = next_v
    return v_out, inputs_count
