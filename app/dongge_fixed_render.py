"""The 8038 renderer's exact-cut timeline with one uninterrupted core soundtrack."""
from contextlib import contextmanager
import hashlib
from itertools import groupby
import json
import os
from pathlib import Path
import random
import re
import shutil
import wave

from audio_preserving_edit import _first_core_end, _prepare_original_core_prefix
from bottom_overlay import _blur_boxes, _overlay_filter, prepare_layers, video_filter


def _clamp(value, low, high):
    return max(low, min(high, value))


def _deterministic_u31(tag: str) -> int:
    digest = hashlib.sha256(tag.encode("utf-8")).digest()
    return (int.from_bytes(digest[:4], "big") & 0x7FFFFFFE) or 1


def resolve_tier1_dedup(plan, engine=None):
    """Resolve Tier-1 anti-duplication settings from plan or deterministic variant fallback."""
    raw = plan.get("dongge_tier1_dedup")
    raw = raw if isinstance(raw, dict) else {}
    enabled = bool(raw.get("enabled", True))
    fps = int(plan.get("fps") or getattr(engine, "FPS", 30) or 30)
    try:
        variant_index = max(1, int(plan.get("variant_index") or 1))
    except (TypeError, ValueError):
        variant_index = 1

    raw_seed = raw.get("variant_seed")
    if raw_seed is not None:
        try:
            variant_seed = int(raw_seed) or 1
        except (TypeError, ValueError):
            variant_seed = _deterministic_u31(f"dongge-tier1:{raw_seed}:{variant_index}")
    else:
        seed_token = (
            os.environ.get("V16_FISSION_VARIANT_SEED", "").strip()
            or str(getattr(engine, "FISSION_VARIANT_SEED", "") or "").strip()
            or str(plan.get("seed") or "")
        )
        variant_seed = _deterministic_u31(f"dongge-tier1-v1:{seed_token}:{variant_index}")

    rng = random.Random(variant_seed)
    slot = (variant_index - 1) % 5
    gop_pool = (28, 37, 46, 55, 63)
    crf_pool = (19, 20, 21, 22, 23)
    abitrate_pool = ("160k", "176k", "192k", "208k", "224k")

    default_gop = _clamp(gop_pool[slot] + rng.randint(-2, 2), 26, 64)
    default_crf = crf_pool[(slot + rng.randrange(5)) % 5]
    default_abitrate = abitrate_pool[(slot + rng.randrange(5)) % 5]

    raw_ab = raw.get("audio_bitrate", default_abitrate)
    if isinstance(raw_ab, (int, float)):
        audio_bitrate = f"{_clamp(int(round(raw_ab)), 128, 256)}k"
    else:
        ab_text = str(raw_ab or "").strip().lower()
        if re.fullmatch(r"\d+k", ab_text):
            kbps = _clamp(int(ab_text[:-1]), 128, 256)
            audio_bitrate = f"{kbps}k"
        elif ab_text.isdigit():
            audio_bitrate = f"{_clamp(int(ab_text), 128, 256)}k"
        else:
            audio_bitrate = default_abitrate

    if not enabled:
        return {
            "enabled": False,
            "variant_seed": variant_seed,
            "gop_frames": fps,
            "video_crf": 23,
            "audio_bitrate": "192k",
            "room_tone_seed": 13579,
            "room_tone_amp": 0.0,
            "eq_low_hz": 200,
            "eq_low_gain": 0.0,
            "eq_high_hz": 4000,
            "eq_high_gain": 0.0,
            "screen_opacity": 0.0,
            "grain_strength": 0,
            "overlay_scale_jitter": 1.0,
            "overlay_x_jitter_px": 0,
            "overlay_y_jitter_px": 0,
        }

    room_tone_seed = (int(raw.get("room_tone_seed", rng.randint(1, 0x7FFFFFFE))) & 0x7FFFFFFE) or 13579
    return {
        "enabled": True,
        "variant_seed": variant_seed,
        "gop_frames": _clamp(int(raw.get("gop_frames", default_gop)), 24, 72),
        "video_crf": _clamp(int(raw.get("video_crf", default_crf)), 18, 25),
        "audio_bitrate": audio_bitrate,
        "room_tone_seed": room_tone_seed,
        "room_tone_amp": round(_clamp(float(raw.get("room_tone_amp", rng.uniform(0.0028, 0.0036))), 0.0005, 0.0080), 5),
        "eq_low_hz": _clamp(int(raw.get("eq_low_hz", rng.choice((165, 190, 215, 245, 275)))), 120, 360),
        "eq_low_gain": round(_clamp(float(raw.get("eq_low_gain", rng.choice((-1.35, -0.95, 0.85, 1.15, 1.40)))), -2.5, 2.5), 2),
        "eq_high_hz": _clamp(int(raw.get("eq_high_hz", rng.choice((3300, 3700, 4200, 4700, 5200)))), 2000, 6500),
        "eq_high_gain": round(_clamp(float(raw.get("eq_high_gain", rng.choice((-1.40, -1.00, 0.90, 1.20, 1.45)))), -2.5, 2.5), 2),
        "screen_opacity": round(_clamp(float(raw.get("screen_opacity", rng.uniform(0.068, 0.112))), 0.050, 0.150), 3),
        "grain_strength": _clamp(int(raw.get("grain_strength", rng.randint(3, 5))), 2, 7),
        "overlay_scale_jitter": round(_clamp(float(raw.get("overlay_scale_jitter", rng.uniform(0.968, 1.032))), 0.940, 1.060), 4),
        "overlay_x_jitter_px": _clamp(int(raw.get("overlay_x_jitter_px", rng.randint(-10, 10))), -16, 16),
        "overlay_y_jitter_px": _clamp(int(raw.get("overlay_y_jitter_px", rng.randint(-6, 8))), -12, 12),
    }


def _variant_color_anchor(variant_seed: int):
    rng = random.Random(variant_seed ^ 0x51C7A9)
    profiles = (
        # (dr1, dr2, dg, db1, db2, rs, bs, rh, bh) — warm-amber, cool-teal, soft-film, crisp-clean, golden-neutral
        (+0.011, +0.014, +0.004, -0.010, -0.013, +0.016, -0.018, +0.020, -0.016),
        (-0.010, -0.008, +0.005, +0.012, +0.015, -0.015, +0.019, -0.014, +0.018),
        (+0.008, -0.009, +0.003, +0.007, -0.008, +0.010, +0.012, +0.014, -0.010),
        (-0.009, +0.012, -0.004, -0.008, +0.011, -0.012, -0.010, +0.015, +0.016),
        (+0.006, +0.010, +0.006, -0.007, -0.011, +0.014, -0.014, +0.018, -0.012),
    )
    base = profiles[rng.randrange(len(profiles))]
    hue_sign = rng.choice((-1.0, 1.0))
    hue_mag = rng.uniform(2.3, 4.5)
    sat = rng.uniform(0.965, 1.045)
    return (*base, hue_sign, hue_mag, sat)


def _shot_color_filters(anchor, shot_rng: random.Random):
    b_dr1, b_dr2, b_dg, b_db1, b_db2, b_rs, b_bs, b_rh, b_bh, hue_sign, hue_mag, b_sat = anchor
    dr1 = _clamp(b_dr1 + shot_rng.uniform(-0.009, 0.009), -0.028, 0.028)
    dr2 = _clamp(b_dr2 + shot_rng.uniform(-0.009, 0.009), -0.028, 0.028)
    dg = _clamp(b_dg + shot_rng.uniform(-0.007, 0.007), -0.028, 0.028)
    db1 = _clamp(b_db1 + shot_rng.uniform(-0.009, 0.009), -0.028, 0.028)
    db2 = _clamp(b_db2 + shot_rng.uniform(-0.009, 0.009), -0.028, 0.028)

    eff_hue_mag = _clamp(hue_mag + shot_rng.uniform(-0.45, 0.85), 1.80, 5.50)
    hue_deg = hue_sign * eff_hue_mag
    sat = _clamp(b_sat + shot_rng.uniform(-0.022, 0.028), 0.940, 1.080)

    rs = _clamp(b_rs + shot_rng.uniform(-0.011, 0.011), -0.035, 0.035)
    bs = _clamp(b_bs + shot_rng.uniform(-0.011, 0.011), -0.035, 0.035)
    rh = _clamp(b_rh + shot_rng.uniform(-0.011, 0.011), -0.035, 0.035)
    bh = _clamp(b_bh + shot_rng.uniform(-0.011, 0.011), -0.035, 0.035)

    return [
        f"hue=h={hue_deg:.2f}:s={sat:.3f}",
        (
            f"curves=r='0/0 0.25/{0.25 + dr1:.3f} 0.75/{0.75 + dr2:.3f} 1/1':"
            f"g='0/0 0.50/{0.50 + dg:.3f} 1/1':"
            f"b='0/0 0.25/{0.25 + db1:.3f} 0.75/{0.75 + db2:.3f} 1/1'"
        ),
        f"colorbalance=rs={rs:.3f}:bs={bs:.3f}:rh={rh:.3f}:bh={bh:.3f}",
        "format=yuv420p",
    ]


@contextmanager
def _variant_encode_override(engine, video_crf: int, gop_frames: int):
    """Keep opening/core pixels 100% untouched while aligning variant H.264 GOP/CRF."""
    orig_enc = getattr(engine, "_video_encoder_args", None)
    orig_run = getattr(engine, "run_ffmpeg", None)
    if orig_enc is None or orig_run is None:
        yield
        return
    fps_str = str(getattr(engine, "FPS", 30))
    gop_str = str(gop_frames)

    def wrapped_enc(_crf=23, preset="veryfast"):
        return orig_enc(video_crf, preset)

    def wrapped_run(cmd, *args, **kwargs):
        patched = list(cmd)
        for idx in range(len(patched) - 1):
            if patched[idx] == "-g" and patched[idx + 1] == fps_str:
                patched[idx + 1] = gop_str
        return orig_run(patched, *args, **kwargs)

    engine._video_encoder_args = wrapped_enc
    engine.run_ffmpeg = wrapped_run
    try:
        yield
    finally:
        engine._video_encoder_args = orig_enc
        engine.run_ffmpeg = orig_run


def _playlist(path, files, frame_counts=None, fps=None):
    with path.open("w", encoding="utf-8", newline="\n") as listing:
        for i, file in enumerate(files):
            escaped = Path(file).as_posix().replace("'", "'\\''")
            listing.write(f"file '{escaped}'\n")
            if frame_counts is not None:
                listing.write(f"duration {frame_counts[i] / fps:.9f}\n")


def _join_pictures(engine, work, name, clips, audio, output, *, strip_metadata=True):
    listing = work / (name + ".txt")
    _playlist(listing, [p for p, _ in clips], [n for _, n in clips], engine.FPS)
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
        "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
    ]
    if strip_metadata:
        cmd += ["-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact", "-tag:v", "avc1"]
    cmd += ["-video_track_timescale", "90000", "-movflags", "+faststart", str(output)]
    engine.run_ffmpeg(cmd)


def _pcm_total_samples(wavs) -> int:
    total = 0
    for wav_path in wavs:
        with wave.open(str(wav_path), "rb") as stream:
            total += stream.getnframes()
    return total


def _soundtrack(engine, plan, work, dedup=None):
    dedup = resolve_tier1_dedup(plan, engine) if dedup is None else dedup

    def run(cmd, destination):
        engine.run_ffmpeg([os.fspath(arg) for arg in cmd])
        if not destination.stat().st_size:
            raise RuntimeError("核心原声解码未产生可用音频")

    wavs = _prepare_original_core_prefix(plan, work, run, lambda: ["ffmpeg", "-y"], _playlist, print)
    listing = work / "original_sound.txt"
    _playlist(listing, wavs)
    audio = work / "soundtrack.m4a"
    channels = str(plan["output_audio_channels"])

    if not dedup["enabled"]:
        run(
            [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listing,
                "-vn", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", channels, audio,
            ],
            audio,
        )
        return audio

    exact_samples = _pcm_total_samples(wavs)
    total_audio_dur = max(0.1, exact_samples / 48000.0)
    room_seed = dedup["room_tone_seed"]
    room_amp = dedup["room_tone_amp"]
    eq_low_hz = dedup["eq_low_hz"]
    eq_low_gain = dedup["eq_low_gain"]
    eq_high_hz = dedup["eq_high_hz"]
    eq_high_gain = dedup["eq_high_gain"]

    af_graph = (
        "[0:a]aresample=48000[voice];"
        "[1:a]highpass=f=95,lowpass=f=2400[room];"
        "[voice][room]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
        f"equalizer=f={eq_low_hz}:t=q:w=1.2:g={eq_low_gain:.2f},"
        f"equalizer=f={eq_high_hz}:t=q:w=1.4:g={eq_high_gain:.2f},"
        f"alimiter=limit=0.98:level=false:latency=true,"
        f"apad=pad_dur=0.02,atrim=end_sample={exact_samples},asetpts=PTS-STARTPTS[aout]"
    )
    run(
        [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", listing,
            "-f", "lavfi", "-i",
            f"anoisesrc=r=48000:c=pink:s={room_seed}:a={room_amp:.5f}:d={total_audio_dur + 0.25:.6f}",
            "-filter_complex_threads", "1",
            "-filter_complex", af_graph,
            "-map", "[aout]", "-vn", "-c:a", "aac",
            "-b:a", dedup["audio_bitrate"], "-ar", "48000", "-ac", channels, audio,
        ],
        audio,
    )
    return audio


def _resolve_broll_speed_and_read_duration(shot, frames: int, fps: int, source_info: dict, shot_rng: random.Random, enabled: bool):
    start = float(shot["source_start_seconds"])
    end = shot.get("source_end_seconds")
    out_dur = frames / fps
    if not enabled:
        read_dur = float(end) - start if end is not None else out_dur
        return max(1.0 / fps, read_dur), 1.0

    source_video_dur = float(
        source_info.get("video_duration_seconds")
        or source_info.get("duration_seconds")
        or 0.0
    )
    explicit_speed = shot.get("broll_speed")
    if end is not None and abs((float(end) - start) - out_dur) > 1e-4:
        read_dur = max(1.0 / fps, float(end) - start)
        if source_video_dur > 0:
            read_dur = min(read_dur, max(1.0 / fps, source_video_dur - start))
        speed = _clamp(read_dur / out_dur, 0.5, 2.0)
        return read_dur, speed

    if explicit_speed is not None:
        desired = _clamp(float(explicit_speed), 0.90, 1.10)
    else:
        fast_candidate = shot_rng.uniform(1.025, 1.085)
        slow_candidate = shot_rng.uniform(0.915, 0.975)
        if shot_rng.random() < 0.55 and (source_video_dur <= 0 or start + out_dur * fast_candidate <= source_video_dur - 1e-3):
            desired = fast_candidate
        else:
            desired = slow_candidate

    read_dur = out_dur * desired
    if source_video_dur > 0 and start + read_dur > source_video_dur - 1e-3:
        avail = max(1.0 / fps, source_video_dur - start)
        if avail >= out_dur * 0.91:
            read_dur = min(avail, out_dur * shot_rng.uniform(0.915, 0.975))
        else:
            read_dur = avail
    speed = _clamp(read_dur / out_dur, 0.5, 2.0)
    return max(1.0 / fps, read_dur), speed


def _fixed_pictures(engine, plan, work, gray_file, masks=None, dedup=None, stats_out=None):
    dedup = resolve_tier1_dedup(plan, engine) if dedup is None else dedup
    fps = int(plan["fps"])
    width, height = int(engine.W), int(engine.H)
    video_crf = dedup["video_crf"]
    gop_frames = dedup["gop_frames"]
    color_anchor = _variant_color_anchor(dedup["variant_seed"])

    clips = []
    broll_count = 0
    cadence_count = 0
    if gray_file:
        gray_file.write_bytes(b"")

    for i, shot in enumerate(plan["shots"]):
        role, frames = shot["role"], int(shot["frame_count"])
        output = work / f"shot_{i:05d}.mp4"
        print(f"8038 分镜 {i + 1}/{len(plan['shots'])} · {role} · {frames} 帧", flush=True)
        if role == "solid":
            engine.run_ffmpeg([
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                f"color=c={shot['solid_color']}:s={width}x{height}:r={fps}",
                "-frames:v", str(frames), "-an",
                *engine._video_encoder_args(video_crf, "veryfast"),
                "-g", str(gop_frames),
                "-video_track_timescale", "90000", str(output),
            ])
        elif role != "shuffle":
            # UNTOUCHED CORE REDLINE: opening and core keep 100% original normalize_video geometry & color.
            source = shot["source_path"]
            start = shot["source_start_seconds"]
            end = shot.get("source_end_seconds")
            duration = end - start if end is not None else frames / fps
            with _variant_encode_override(engine, video_crf, gop_frames):
                engine.normalize_video(
                    source, str(output), trim=(start, duration), video_only=True,
                    output_frames=frames, exact_tail=True, source_gray=None,
                    video_stream=plan["sources"][source]["video_stream_index"],
                    picture_fit=shot.get("picture_fit", "cover"),
                )
        else:
            broll_count += 1
            source = shot["source_path"]
            source_info = plan["sources"][source]
            video_stream = int(source_info["video_stream_index"])
            start = float(shot["source_start_seconds"])
            raw_shot_seed = shot.get("dedup_shot_seed")
            if raw_shot_seed is not None:
                shot_seed = (int(raw_shot_seed) & 0x7FFFFFFE) or 1
            else:
                shot_seed = _deterministic_u31(f"{dedup['variant_seed']}:shot:{i}:{start:.6f}")
            shot_rng = random.Random(shot_seed)

            read_dur, speed = _resolve_broll_speed_and_read_duration(
                shot, frames, fps, source_info, shot_rng, dedup["enabled"]
            )
            sample = work / "before-effects.gray" if gray_file else None
            if shot.get("picture_fit", "cover") == "contain":
                fit_expr = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
            else:
                fit_expr = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"

            base_parts = []
            if abs(speed - 1.0) > 1e-6:
                base_parts.append(f"setpts=(PTS-STARTPTS)/{speed:.6f}")
            base_parts.extend([
                fit_expr,
                "setsar=1",
                f"fps={fps}",
                f"tpad=stop_mode=clone:stop_duration={frames / fps:.9f}",
                f"trim=end_frame={frames}",
                f"setpts=N/({fps}*TB)",
                "format=yuv420p",
            ])
            base_chain = ",".join(base_parts)

            if sample is not None:
                graph = [
                    f"[0:{video_stream}]{base_chain},split=2[clean][tap]",
                    "[tap]scale=9:8:flags=area,format=gray[raw]",
                ]
            else:
                graph = [f"[0:{video_stream}]{base_chain}[clean]"]

            cur = "clean"
            if masks:
                # Apply burned-in subtitle masks BEFORE micro-zoom/XY shift so coordinates never drift.
                cur = _blur_boxes(graph, cur, masks, width, height)

            post_filters = []
            if dedup["enabled"]:
                # Item #6: 25<->30 fps frame cadence conversion on ~65% deterministic subset of B-roll shots
                if shot_rng.random() < 0.65:
                    cadence_count += 1
                    post_filters.append(
                        f"fps=25,fps={fps},"
                        f"tpad=stop_mode=clone:stop_duration={frames / fps:.9f},"
                        f"trim=end_frame={frames},setpts=N/({fps}*TB)"
                    )
                # Item #5: Micro-zoom (1.055..1.148x) + 2%-5% edge crop + random XY shift
                zoom = shot_rng.uniform(1.055, 1.148)
                sw = max(width + 36, int(round(width * zoom)) // 2 * 2)
                sh = max(height + 64, int(round(height * zoom)) // 2 * 2)
                x_frac = shot_rng.uniform(0.12, 0.88)
                y_frac = shot_rng.uniform(0.12, 0.88)
                x_off = max(0, min(sw - width, int(round((sw - width) * x_frac)) // 2 * 2))
                y_off = max(0, min(sh - height, int(round((sh - height) * y_frac)) // 2 * 2))
                post_filters.append(f"scale={sw}:{sh}:flags=lanczos,crop={width}:{height}:{x_off}:{y_off},setsar=1")
                # Item #7: Curves + HSL (hue) + colorbalance (subtle natural redo, NO eq=)
                post_filters.extend(_shot_color_filters(color_anchor, shot_rng))
            else:
                post_filters.append("format=yuv420p")

            graph.append(f"[{cur}]{','.join(post_filters)}[vout]")
            cmd = [
                "ffmpeg", "-y",
                "-ss", f"{start:.9f}", "-t", f"{read_dur:.9f}",
                "-i", str(source),
                "-filter_complex_threads", "2",
                "-filter_complex", ";".join(graph),
                "-map", "[vout]", "-an",
                *engine._video_encoder_args(video_crf, "veryfast"),
                "-g", str(gop_frames),
                "-video_track_timescale", "90000",
                "-frames:v", str(frames),
                str(output),
            ]
            if sample is not None:
                cmd.extend([
                    "-map", "[raw]", "-an", "-c:v", "rawvideo", "-threads", "1",
                    "-pix_fmt", "gray", "-fps_mode", "passthrough",
                    "-frames:v", str(frames), "-f", "rawvideo", os.fspath(sample),
                ])
            engine.run_ffmpeg(cmd)
            if sample is not None:
                if sample.stat().st_size != frames * 72:
                    raise RuntimeError("打乱原画指纹没有覆盖本刀所有帧")
                with sample.open("rb") as part, gray_file.open("ab") as track:
                    shutil.copyfileobj(part, track)

        if engine._get_video_frame_count(str(output)) != frames:
            raise RuntimeError(f"分镜 {i + 1} 编码帧数与时间轴不一致")
        clips.append((output, frames))

    if isinstance(stats_out, dict):
        stats_out["broll_shots_processed"] = broll_count
        stats_out["broll_cadence_converted_shots"] = cadence_count
    return clips


def _build_bokeh_loop(engine, work: Path, plan: dict, dedup: dict) -> tuple[Path, float]:
    """Pre-render a seamless ping-pong procedural soft bokeh / light-leak clip once per movie."""
    fps = int(plan["fps"])
    width, height = int(engine.W), int(engine.H)
    rng = random.Random(dedup["variant_seed"] ^ 0xB04E41)
    perlin_seed = rng.randint(1, 0x7FFFFFFE)
    xscale = rng.uniform(1.85, 3.15)
    yscale = rng.uniform(1.85, 3.15)
    tscale = rng.uniform(0.55, 0.95)
    palettes = (
        (+0.11, +0.04, -0.07),  # warm golden-amber
        (-0.06, +0.04, +0.10),  # cool moonlight-cyan
        (+0.09, +0.02, +0.04),  # soft rose-peach
        (+0.07, +0.06, -0.04),  # natural cream-sunlight
        (-0.04, +0.06, +0.07),  # soft aqua-mint
    )
    rm, gm, bm = palettes[rng.randrange(len(palettes))]
    half_frames = max(30, fps * 2)
    loop_frames = half_frames * 2 - 2
    bokeh_path = work / "bokeh_loop.mp4"

    graph = (
        f"[0:v]trim=end_frame={half_frames},setpts=PTS-STARTPTS,format=gbrp,"
        "curves=all='0/0 0.46/0 0.78/0.44 1/0.86',"
        f"colorbalance=rm={rm:.3f}:gm={gm:.3f}:bm={bm:.3f}:rh={rm * 1.2:.3f}:gh={gm * 1.2:.3f}:bh={bm * 1.2:.3f},"
        f"scale={width}:{height}:flags=lanczos,gblur=sigma=16:steps=2,format=yuv420p,"
        "split=2[fwd][revsrc];"
        f"[revsrc]trim=start_frame=1:end_frame={half_frames - 1},reverse,setpts=PTS-STARTPTS[rev];"
        f"[fwd][rev]concat=n=2:v=1:a=0,setpts=N/({fps}*TB),setsar=1,format=yuv420p[bout]"
    )
    engine.run_ffmpeg([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", (
            f"perlin=s=90x160:r={fps}:octaves=2:persistence=0.60:"
            f"xscale={xscale:.2f}:yscale={yscale:.2f}:tscale={tscale:.2f}:"
            f"random_mode=seed:seed={perlin_seed}"
        ),
        "-t", f"{(half_frames + 2) / fps:.6f}",
        "-filter_complex_threads", "2",
        "-filter_complex", graph,
        "-map", "[bout]", "-an",
        "-frames:v", str(loop_frames),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
        "-pix_fmt", "yuv420p", "-g", str(fps),
        "-video_track_timescale", "90000",
        str(bokeh_path),
    ])
    return bokeh_path, loop_frames / fps


def _shuffle_effects(engine, plan, clips, native, layers, masks, work, dedup=None, bokeh_info=None):
    """Keep the protected clips' encoded packets and apply PiP bokeh screen blend + bottom overlays on B-roll."""
    dedup = resolve_tier1_dedup(plan, engine) if dedup is None else dedup
    fps = plan["fps"]
    width, height = int(engine.W), int(engine.H)
    video_crf = dedup["video_crf"]
    gop_frames = dedup["gop_frames"]
    dx = dedup["overlay_x_jitter_px"]
    dy = dedup["overlay_y_jitter_px"]

    result = []
    cursor = 0
    indices = range(len(clips))
    for is_shuffle, group in groupby(indices, key=lambda i: plan["shots"][i]["role"] == "shuffle"):
        members = list(group)
        frames = sum(clips[i][1] for i in members)
        if not is_shuffle:
            result.extend(clips[i] for i in members)
        else:
            output = work / f"effects_{members[0]}.mp4"
            start, duration = cursor / fps, frames / fps
            cmd = ["ffmpeg", "-y", "-ss", f"{start:.9f}", "-i", str(native)]
            if dedup["enabled"] and bokeh_info is not None:
                bokeh_path, bokeh_dur = bokeh_info
                bokeh_offset = (start % bokeh_dur) if bokeh_dur > 0 else 0.0
                cmd += ["-stream_loop", "-1", "-ss", f"{bokeh_offset:.6f}", "-i", str(bokeh_path)]
                graph = [f"[0:v:0]trim=end_frame={frames},setpts=PTS-STARTPTS[broll_in]"]
                cur = _blur_boxes(graph, "broll_in", masks, width, height) if masks else "broll_in"
                graph.extend([
                    f"[{cur}]format=gbrp[broll_rgb]",
                    f"[1:v:0]trim=end_frame={frames},setpts=PTS-STARTPTS,format=gbrp[bokeh_rgb]",
                    (
                        f"[broll_rgb][bokeh_rgb]blend=all_mode=screen:all_opacity={dedup['screen_opacity']:.3f},"
                        f"format=yuv420p,noise=alls={dedup['grain_strength']}:allf=t[broll_fx]"
                    ),
                ])
                previous = "broll_fx"
                active = [row for row in layers if row["start"] < start + duration and row["end"] > start]
                for idx, row in enumerate(active):
                    inp_idx = 2 + idx
                    cmd += ["-i", str(row["path"])]
                    out_lbl = f"layer{idx}"
                    graph.append(
                        _overlay_filter(
                            previous, f"{inp_idx}:v", out_lbl, row, start, height,
                            x_jitter_px=dx, y_jitter_px=dy,
                        )
                    )
                    previous = out_lbl
                if active:
                    graph.append(f"[{previous}]format=yuv420p[vfinal]")
                    previous = "vfinal"
                cmd += [
                    "-filter_complex_threads", "4",
                    "-filter_complex", ";".join(graph),
                    "-map", f"[{previous}]",
                ]
            else:
                cmd += video_filter(
                    layers, "0:v:0", f"trim=end_frame={frames},setpts=PTS-STARTPTS",
                    start, duration, height, masks=masks, width=width,
                    x_jitter_px=dx, y_jitter_px=dy,
                )
            cmd += [
                "-an", "-frames:v", str(frames),
                *engine._video_encoder_args(video_crf, "veryfast"),
                "-g", str(gop_frames),
                "-video_track_timescale", "90000", str(output),
            ]
            engine.run_ffmpeg(cmd)
            if engine._get_video_frame_count(str(output)) != frames:
                raise RuntimeError("打乱特效区间帧数不足")
            result.append((output, frames))
        cursor += frames
    return result


def render_fixed_timeline(engine, request_file):
    request = json.loads(Path(request_file).read_text(encoding="utf-8"))
    plan = request["plan"]
    if (plan["width"], plan["height"], plan["fps"]) != (engine.W, engine.H, engine.FPS):
        raise ValueError("规划帧率或画布与 8038 制作规格不一致")
    if not plan.get("shots"):
        raise ValueError("核心画面和打乱画面的制作时间轴为空")
    engine.ensure_dirs()
    work = Path(engine.clean_tmp())
    output = Path(request["output"]).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = Path(request["pre_effects_gray"]) if request.get("pre_effects_gray") else None
    print("8038 第一版渲染引擎 · 栋哥组首条选区倍率与固定刀长", flush=True)

    dedup = resolve_tier1_dedup(plan, engine)
    masks = request.get("shuffle_masks")
    audio = _soundtrack(engine, plan, work, dedup=dedup)
    shot_stats = {}
    clips = _fixed_pictures(engine, plan, work, raw, masks=masks, dedup=dedup, stats_out=shot_stats)
    base = work / "timeline.mp4"
    _join_pictures(engine, work, "clips", clips, audio, base)

    duration, fps = plan["output_duration_seconds"], plan["fps"]
    shuffled = [s for s in plan["shots"] if s["role"] == "shuffle"]
    prepared = (
        prepare_layers(
            request.get("bottom_layers") or [],
            work,
            engine.W,
            engine.H,
            duration,
            first_core_end=_first_core_end(plan),
            scale_jitter=dedup["overlay_scale_jitter"],
            x_jitter_px=dedup["overlay_x_jitter_px"],
            y_jitter_px=dedup["overlay_y_jitter_px"],
        )
        if shuffled
        else []
    )
    mask_source = None
    if shuffled and engine.CORNER_MASK_REVEAL_ACTIVE:
        mask_source = engine.select_shuffle_mask_source(plan["selected_shuffle_paths"])
    all_sticker_files = engine.get_files(engine.STICKER_DIR, (".png", ".gif", ".mp4"), "贴纸素材池")
    sticker_files, dynamic_sticker_files = engine.partition_sticker_files(all_sticker_files)
    corner_cfg = engine.parse_sticker_corner_config(sticker_files)
    has_corner = bool(sticker_files) if corner_cfg is None else bool(corner_cfg)
    has_dynamic = bool(engine.parse_fullscreen_sticker_config(dynamic_sticker_files))
    stickers = has_corner or has_dynamic
    native_effects = bool(engine.FACE_EFFECTS or engine.TIMEPIN_ZOOM_ACTIVE or mask_source or stickers)

    face_source = base
    if engine.FACE_EFFECTS and shuffled:
        face_source = work / "faces.mp4"
        engine.apply_face_effect_chain(str(base), str(face_source))
    native = work / "native_final.mp4"
    if shuffled and native_effects:
        engine.final_render(str(face_source), str(native), render_duration=duration, mask_source=mask_source)
    else:
        native = base

    if shuffled and (dedup["enabled"] or native_effects or prepared):
        bokeh_info = _build_bokeh_loop(engine, work, plan, dedup) if dedup["enabled"] else None
        clips = _shuffle_effects(
            engine, plan, clips, native, prepared, None, work,
            dedup=dedup, bokeh_info=bokeh_info,
        )
        candidate = work / "protected.mp4"
        _join_pictures(engine, work, "protected", clips, audio, candidate)
    else:
        candidate = native

    engine.ensure_final_output_contract(str(candidate))
    video_duration, audio_duration = engine.require_av_duration_parity(str(candidate), duration, "栋哥组成片")
    if audio_duration + .002 < video_duration:
        raise RuntimeError("成片核心原声音轨提前结束")
    if engine._get_video_frame_count(str(candidate)) != sum(s["frame_count"] for s in plan["shots"]):
        raise RuntimeError("成片总帧数与已验收分镜时间轴不一致")
    quality = engine.require_final_visual_quality(str(candidate))

    tier1_summary = {
        "enabled": dedup["enabled"],
        "variant_seed": dedup["variant_seed"],
        "gop_frames": dedup["gop_frames"],
        "video_crf": dedup["video_crf"],
        "audio_bitrate": dedup["audio_bitrate"],
        "room_tone_seed": dedup["room_tone_seed"],
        "room_tone_amp": dedup["room_tone_amp"],
        "eq_low_hz": dedup["eq_low_hz"],
        "eq_low_gain": dedup["eq_low_gain"],
        "eq_high_hz": dedup["eq_high_hz"],
        "eq_high_gain": dedup["eq_high_gain"],
        "screen_opacity": dedup["screen_opacity"],
        "grain_strength": dedup["grain_strength"],
        "overlay_scale_jitter": dedup["overlay_scale_jitter"],
        "overlay_x_jitter_px": dedup["overlay_x_jitter_px"],
        "overlay_y_jitter_px": dedup["overlay_y_jitter_px"],
        "broll_shots_processed": shot_stats.get("broll_shots_processed", 0),
        "broll_cadence_converted_shots": shot_stats.get("broll_cadence_converted_shots", 0),
        "bokeh_screen_blend_applied": bool(shuffled and dedup["enabled"]),
        "metadata_stripped": True,
        "untouched_core_preserved": True,
    }
    manifest = output.with_suffix(".manifest.json")
    plan.update(
        native_render_engine="run_v16_enhanced.py",
        visual_quality_gate=quality,
        tier1_dedup_applied=tier1_summary,
        output_path=str(output),
        manifest_path=str(manifest),
        audio_path=None,
        audio_exported=False,
        audio_reencoded_once=True,
        shuffle_audio_used=False,
        audio_covers_video_verified=True,
        verified_duration_seconds=engine.get_duration(str(candidate)),
        verified_audio_duration_seconds=audio_duration,
        verified_video_duration_seconds=video_duration,
        core_picture_frames=sum(s["frame_count"] for s in plan["shots"] if s["role"] == "core"),
        shuffle_picture_frames=sum(s["frame_count"] for s in shuffled),
    )
    manifest.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    try:
        os.replace(candidate, output)
    except OSError:
        staging = output.with_suffix(".staging.mp4")
        shutil.copy2(candidate, staging)
        os.replace(staging, output)
        Path(candidate).unlink(missing_ok=True)
    print(f"8038 定稿完成 · {duration:.3f} 秒 · {fps} fps", flush=True)
