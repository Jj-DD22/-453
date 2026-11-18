"""Plan fixed-cut timelines, run the native 8038 renderer, and verify the batch."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

from audio_preserving_edit import plan_edit
from auto_fill_runtime import FillCheckpointError, RenderCheckpoint, canonical as fill_canonical, material_signature_identity
from shuffle_similarity import rendered_shuffle_hashes, source_shuffle_hashes


MODE = "original_audio_broll"
RANDOM_MODE = "original_audio_random"


def _json_safe(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _material_file_signature(path):
    target = Path(path)
    st = target.stat()
    return [target.name, int(st.st_size), int(st.st_mtime_ns)]


def _write_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".partial")
    try:
        pending.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def run_dongge_core_batch(
    app, run_id, *, selected_materials, fission_count,
    renderer_env, renderer_python, face_effects, sticker_settings,
    single_output_path, single_compliance_path,
    material_base_dir=None, stall_timeout_seconds=1800,
    auto_save_directory="", cut_seconds=3.0,
    edit_mode=MODE, shuffle_ranges=None, core_ranges=None,
    shuffle_budget_seconds=None,
    core_audio_policy="legacy_selected_then_silence",
    core_display_modes=None,
    primary_shuffle_budget_seconds=None,
    core_timeline_seconds=None,
    primary_selection_first=False,
    fixed_shuffle_cuts=False,
    unique_shuffle=False,
    bottom_layers=None,
    shuffle_masks=None,
    batch_picture_policy="per_core",
    duration_multiplier=None,
):
    """Render and durably publish a locally authorized queue task.

    `app` is the loaded v16_web_app module. The worker has already bound its
    thread-local state and validated the immutable selected material set.
    """
    started = time.monotonic()
    output_path = Path(single_output_path)
    compliance_path = Path(single_compliance_path)
    root = Path(material_base_dir or app.BASE_DIR).resolve()
    count = int(fission_count)
    if isinstance(core_display_modes, tuple):
        core_display_modes = list(core_display_modes)
    random_mode = edit_mode == RANDOM_MODE
    rotate_cores_mode = random_mode and batch_picture_policy == "rotate_cores"
    loop_core_audio = random_mode and core_audio_policy == "selected_core_loop"
    full_core_audio_once = random_mode and core_audio_policy == "full_core_once"
    explicit_display_modes = full_core_audio_once and core_display_modes is not None
    first_core_picture_trim = (explicit_display_modes and isinstance(core_display_modes, list)
                               and bool(core_display_modes) and core_display_modes[0] == "trimmed_video_audio")
    has_picture_core_modes = (explicit_display_modes and isinstance(core_display_modes, list)
                              and any(m in {"video_audio", "video_only", "trimmed_video_audio"} for m in core_display_modes))
    bounded_core_timeline = (core_timeline_seconds is not None) or (full_core_audio_once and duration_multiplier is not None)
    selected_primary_first = primary_selection_first is True
    core_audio_covers_body = loop_core_audio or full_core_audio_once
    mode_label = "核心保留＋随机打乱" if random_mode else "原声穿插"
    duration_policy = (
        "opening-plus-rotated-core-span-multiplier-body-v7" if rotate_cores_mode
        else "opening-plus-selected-primary-span-multiplier-body-v6" if selected_primary_first
        else "opening-plus-first-core-multiplier-body-v5" if bounded_core_timeline
        else "opening-plus-complete-core-fixed-audio-once-v4" if full_core_audio_once
        else "opening-plus-selected-core-times-multiplier-v3" if random_mode and shuffle_budget_seconds is not None
        else "opening-selected-core-then-silent-shuffle-v2" if random_mode
        else "opening-plus-complete-core-original-audio-v1"
    )

    def _display_mode_label(mode):
        if mode == "trimmed_video_audio":
            return "画面裁剪、顺序原声" if bounded_core_timeline else "画面裁剪、完整原声"
        if mode == "audio_only":
            return "只保留音频"
        if mode == "video_only":
            return "仅裁剪画面（静音）"
        return "保留画面和音频"
    multi = count > 1
    # Saved jobs can predate both shuffle flags. Their original core picture
    # and audio plan stays in force; every new multi-output random render must
    # still validate its B-roll against the other complete B-roll tracks.
    check_batch = random_mode and multi
    check_history = check_batch
    unique_shuffle = unique_shuffle or check_batch
    fixed_output_cuts = fixed_shuffle_cuts or check_batch
    run_dir = Path(app.FISSION_OUTPUT_ROOT) / run_id if multi else output_path.parent
    accepted_paths, accepted_durations, manifests = [], [], []
    visual_batch = None
    fill_session = None
    fill_enabled = False
    reserved = False
    finalizing = False

    def log(message):
        app.append_log(str(message), run_id)

    def update(**changes):
        with app.state_lock:
            if app.state["run_id"] == run_id:
                app.state.update(changes)

    def checkpoint():
        app.wait_at_queue_pause_checkpoint(run_id)
        if app.queue_skip_requested(run_id):
            raise app.QueueTaskSkipped("用户已跳过本条任务，队列将继续下一条")
        if app.task_queue_shutdown_event.is_set():
            raise app.QueueControlError("服务正在关闭，已停止原声剪辑")

    def owned_run(command, destination, *, env=None, activity_paths=()):
        checkpoint()
        destination = Path(destination)
        with app.task_queue_control_lock:
            if app.task_queue_shutdown_event.is_set():
                raise app.QueueControlError("服务正在关闭，不启动新的剪辑进程")
            proc = app._start_queue_renderer_process(
                run_id, [str(part) for part in command], cwd=str(app.BASE_DIR), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        diagnostics = {}
        code = app.stream_renderer_output(
            proc, run_id, stall_timeout_seconds=stall_timeout_seconds,
            activity_paths=(destination, *activity_paths), diagnostics=diagnostics,
        )
        if code:
            if destination.suffix == ".json":
                reasons = [str(line).split("V16_SEMANTIC_ERROR ", 1)[1].strip()
                           for line in diagnostics.get("output_tail", [])
                           if "V16_SEMANTIC_ERROR " in str(line)]
                reason = app.redact_local_paths(reasons[-1]) if reasons else "请查看本任务的语义选镜日志"
                raise RuntimeError(f"原声语义选镜失败（退出码 {code}）：{reason}")
            reason = app.opening_failure_hint(diagnostics.get("output_tail", []))
            raise RuntimeError(f"原声剪辑编码失败（退出码 {code}）：{reason}")
        return subprocess.CompletedProcess(command, code)

    def report(complete, error=""):
        visual_checks = [m["shuffle_visual_verification"] for m in manifests if m.get("shuffle_visual_verification")]
        pairs = [pair for check in visual_checks for pair in check.get("pair_results", [])]
        historical = [pair for check in visual_checks for pair in check.get("history_pairs", [])]
        if complete and (check_batch or check_history):
            expected = {(first, second) for second in range(2, count + 1) for first in range(1, second)}
            actual = {(p["first"], p["second"]) for p in pairs}
            if len(visual_checks) != count or len(pairs) != len(expected) or actual != expected:
                raise ValueError("打乱画面两两验收记录不完整，无法确认整批通过。")
            for p in pairs:
                scores = [p.get(key) for key in ("source_similarity_percent", "rendered_similarity_percent")]
                if (any(type(value) not in (int, float) or not 0 <= value <= app.FISSION_SIMILARITY_LIMIT for value in scores)
                        or p["similarity_percent"] != max(scores)):
                    raise ValueError("打乱原画或最终打乱画面的相似度验收未通过。")
            for check in visual_checks:
                history_pairs = check.get("history_pairs", [])
                if check.get("history_videos_checked") != len(history_pairs):
                    raise ValueError("历史查重记录不完整，无法确认整批通过。")
                if any(not 0 <= p[k] <= app.FISSION_SIMILARITY_LIMIT for p in history_pairs
                       for k in ("source_similarity_percent", "rendered_similarity_percent")):
                    raise ValueError("历史成片相似度超过上限。")
        return {
            "report_type": "original-audio-edit-validation-v1",
            "edition_mode": "dongge",
            "dongge_timeline_mode": "core_audio",
            "native_render_engine": "run_v16_enhanced.py",
            "edit_mode": edit_mode,
            "run_id": run_id,
            "video_count": count,
            "requested_count": count,
            "completed_count": len(accepted_paths),
            "batch_complete": bool(complete),
            "delivery_state": "complete" if complete else "partial",
            "passed": bool(complete),
            "accepted_outputs_passed": bool(accepted_paths),
            "acceptance_scope": "媒体可解码、原声连续、选中素材和镜头时间轴验证",
            "failed_variant": None if complete else len(accepted_paths) + 1,
            "failure_summary": str(error),
            "cut_seconds": float(cut_seconds),
            "duration_policy": duration_policy,
            "duration_multiplier": float(duration_multiplier) if duration_multiplier is not None else None,
            "core_audio_policy": core_audio_policy if random_mode else "legacy_semantic_interleave",
            "batch_picture_policy": batch_picture_policy,
            "core_audio_covers_body": core_audio_covers_body,
            "core_timeline_seconds": core_timeline_seconds,
            "primary_selection_first": primary_selection_first,
            "fixed_shuffle_cuts": fixed_output_cuts,
            "unique_shuffle": unique_shuffle,
            "shuffle_visual_verification": visual_checks,
            "shuffle_cut_policy": "fixed-duration-hard-cuts-no-natural-boundaries" if fixed_output_cuts else "legacy-fixed-pieces-with-tails",
            "core_timeline_basis": ("rotated_selected_picture_duration" if rotate_cores_mode
                                    else "first_selected_picture_duration" if selected_primary_first
                                    else "first_source_full_duration" if bounded_core_timeline else None),
            "core_timeline_scope": "body_excluding_opening" if bounded_core_timeline else None,
            "core_audio_end_policy": "stop_at_video_end_no_loop_or_silence" if bounded_core_timeline else None,
            "core_display_modes": core_display_modes if full_core_audio_once else None,
            "primary_shuffle_budget_seconds": primary_shuffle_budget_seconds if first_core_picture_trim else None,
            "primary_shuffle_used_duration_seconds": manifests[0].get("primary_shuffle_used_duration_seconds") if first_core_picture_trim and manifests else None,
            "primary_shuffle_effective_budget_seconds": manifests[0].get("primary_shuffle_effective_budget_seconds") if first_core_picture_trim and manifests else None,
            "primary_shuffle_budget_policy": (
                "以第一条核心原片全长 C 为基准，请求打乱画面预算 C×(K−1)；实际用量受第一条未保留画面时段和可用打乱素材限制，不控制后续核心选项，不改变音轨或总时长"
                if first_core_picture_trim and primary_shuffle_budget_seconds is not None else None
            ),
            "core_display_options": [
                {"index": index, "name": name, "mode": mode, "label": _display_mode_label(mode)}
                for index, (name, mode) in enumerate(zip(selected_materials.get("核心素材", []), core_display_modes), 1)
            ] if explicit_display_modes else None,
            "audio_policy": (
                "开头原声播放一次；开头保留核心原片与同步原声不动，终点后剩余原声分配给打乱画面，播完自动拉取后续核心原声接续至片尾（仅画面核心不供音），末端可截断，不循环单条、不变速、不拉伸；音频来源不足覆盖目标时长时拒绝制作；打乱素材自身声音全部静音；AAC 格式编码"
                if rotate_cores_mode else
                "开头原声播放一次；开头保留核心原片与同步原声不动，终点后剩余原声分配给打乱画面，播完自动拉取后续核心原声接续至主体目标末端；视频末端可截断最后一段原声，不循环、不变速、不拉伸，音频来源不足覆盖目标时长时拒绝制作；打乱素材自身声音全部静音；AAC 格式编码"
                if selected_primary_first else
                "开头原声播放一次；核心原片音轨按所选核心顺序连续使用，播放至主体目标时长结束，视频末端可截断最后一段原声；不循环、不变速、不拉伸、不补静音，音频来源不足覆盖目标时长时拒绝制作；打乱素材自身声音全部静音；AAC 格式编码"
                if bounded_core_timeline else
                "开头原声播放一次；全部核心按所选顺序完整连续播放原声一次，画面裁剪或显示选项均不影响完整音轨，不循环、不拉伸、不变速、不延长；打乱素材自身声音全部静音；AAC 格式编码"
                if explicit_display_modes else
                "开头原声播放一次；完整核心原声按核心顺序连续播放一次，完全不受画面截取区间影响，不循环、不拉伸、不变速、不延长；打乱素材自身声音全部静音；AAC 格式编码"
                if full_core_audio_once else
                "开头原声播放一次；所选核心区间原声按核心顺序循环补足整个主体，覆盖核心及打乱画面，保持原速度与音量；打乱素材自身声音全部静音；AAC 格式编码"
                if loop_core_audio else
                "保留开头原声与核心所选区间的同步原声；默认核心完整保留；末尾打乱片段全部静音；AAC 格式编码"
                if random_mode else "开头原音接完整核心原音；打乱素材全部静音；AAC 格式编码"
            ),
            "picture_policy": (
                "各条成片按顺序轮换带画面的核心素材裁剪区间作为主体开场，剩余主体画面全部为静音打乱素材（跳过仅音频核心画面）；开头保留核心原片与同步原声不动，终点后剩余原声分配给打乱画面，播完自动拉取后续核心原声接续至片尾。"
                if rotate_cores_mode else
                "首条核心选区放在主体开头，其余主体画面全部为所选静音打乱素材；开头保留核心原片与同步原声不动，终点后剩余原声分配给打乱画面，播完自动拉取后续核心原声接续至片尾。"
                if batch_picture_policy == "first_core_then_shuffle" else
                "主体目标时长由第一条核心所选画面区间长度×倍率决定，开头另加；开头保留核心原片与同步原声不动，终点后剩余原声分配给打乱画面，播完自动拉取后续核心原声接续至主体目标末端。"
                if selected_primary_first else
                "主体目标时长由第一条核心原片全长×倍率决定，开头另加；第一条保留所选画面区间，区间外使用打乱画面一次，素材不足回退同一时刻第一条原画面；后续核心按所选顺序仅在主体目标范围内出现，保留画面和音频则使用原画面，只保留音频则使用打乱画面（不足循环画面、无素材黑底）；所有画面在主体目标末端结束，原声音轨连续使用至同一末端"
                if bounded_core_timeline else
                "第一条核心只按用户区间裁剪画面，原声始终完整保留一次；区间外画面使用所选打乱素材一次，素材不足时回退第一条核心同一时刻原画面。后续核心按两种选项处理：保留画面和音频则完整保留原画面；只保留音频则不显示该核心原画面，使用打乱素材画面，不足循环画面，无素材使用黑底。所有核心原声完整连续播放一次，主体时长不变"
                if first_core_picture_trim else
                "每条核心按用户选择处理：保留画面和音频时完整保留该核心原画面；只保留音频时完全不显示该核心原画面，使用所选打乱素材画面，素材不足则循环画面，没有打乱素材则使用黑底；完整核心原声始终连续播放一次，主体时长不变"
                if explicit_display_modes else
                "完整开头之后保留完整核心时间轴；用户所选核心区间仅决定优先保留哪些原画面，其余画面时段按所选打乱素材可用量静音填充；素材用完或未选择时回退同一时刻的核心原画面；核心原画面不重排、不变速，打乱片段不追加到末尾"
                if full_core_audio_once else
                ("完整开头→用户选择的核心区间→打乱画面（素材自身声音静音）；核心默认完整，仅按用户区间截取并同步保留原画原声，不自动截短、不覆盖、不重排、不变速；"
                 + ("打乱素材仅在用户所选区间按每刀秒数随机取够预算，不循环"
                    if shuffle_budget_seconds is not None else "打乱素材仅在用户所选区间按每刀秒数切分并随机排列一次"))
                if random_mode else
                "核心原时间轴不重排；核心两刀、语义匹配打乱一刀；无匹配保留核心；居中缩放裁剪"
            ),
            "semantic_matching_performed": not random_mode,
            "shuffle_ranges": shuffle_ranges if random_mode else None,
            "core_ranges": core_ranges if random_mode and (not explicit_display_modes or has_picture_core_modes) else None,
            "core_range_scope": (
                "per_picture_core" if rotate_cores_mode
                else "first_picture_only" if first_core_picture_trim
                else "per_picture_core" if has_picture_core_modes and core_ranges
                else "not_used" if explicit_display_modes
                else "picture_only" if full_core_audio_once
                else "picture_and_audio"
            ),
            "shuffle_budget_seconds": (
                manifests[0].get("shuffle_duration_seconds") if full_core_audio_once and manifests
                else None if full_core_audio_once
                else shuffle_budget_seconds if random_mode else None
            ),
            "shuffle_budget_source": (
                "rotated_core_multiplier_remainder" if rotate_cores_mode
                else "first_core_unkept_and_later_audio_only_periods" if first_core_picture_trim
                else "audio_only_core_periods" if explicit_display_modes
                else "unkept_core_picture_periods" if full_core_audio_once
                else "task_budget"
            ),
            "full_core_picture_hole_duration_seconds": manifests[0].get("full_core_picture_hole_duration_seconds") if full_core_audio_once and manifests else None,
            "shuffle_duration_seconds": manifests[0].get("shuffle_duration_seconds") if full_core_audio_once and manifests else None,
            "core_fallback_duration_seconds": manifests[0].get("core_fallback_duration_seconds") if full_core_audio_once and manifests else None,
            "shuffle_repeated": manifests[0].get("shuffle_repeated") if explicit_display_modes and manifests else None,
            "shuffle_repeat_cycles": manifests[0].get("shuffle_repeat_cycles") if explicit_display_modes and manifests else None,
            "solid_background_duration_seconds": manifests[0].get("solid_background_duration_seconds") if explicit_display_modes and manifests else None,
            "black_picture_duration_seconds": manifests[0].get("black_picture_duration_seconds") if explicit_display_modes and manifests else None,
            "shuffle_selection_policy": (
                (f"按各条轮换核心的目标时长独立抽取固定刀长打乱片段（各条刀数随主核心裁剪时长变化），可共用镜头；整段打乱原画和导出打乱画面的 48 帧两两相似度均须 ≤{app.FISSION_SIMILARITY_LIMIT:g}%。"
                 if check_batch else
                 f"按本条主核心目标时长独立随机拼接打乱镜头，整段打乱原画和导出打乱画面与历史成片的相似度均须 ≤{app.FISSION_SIMILARITY_LIMIT:g}%。"
                 if check_history else
                 "按本条轮换主核心目标时长固定刀长随机拼接打乱素材。")
                if rotate_cores_mode else
                f"每条独立随机拼接，可共用镜头。整段打乱原画和导出打乱画面的两两相似度均须 ≤{app.FISSION_SIMILARITY_LIMIT:g}%。"
                if check_batch else
                f"独立随机拼接打乱镜头，整段打乱原画和导出打乱画面与历史成片的相似度均须 ≤{app.FISSION_SIMILARITY_LIMIT:g}%。"
                if check_history else
                "固定刀长随机拼接打乱素材。"
                if unique_shuffle else
                "第一条核心未保留的画面时段只使用打乱素材一次，耗尽回退同一时刻原画面；后续只保留音频的核心时段使用打乱画面，不足循环画面、无素材黑底且不回退其原画面；打乱素材源音始终静音"
                if first_core_picture_trim else
                "仅为选择只保留音频的核心时段提供画面；按每刀秒数使用所选打乱片段，素材不足时循环画面，未选择素材时使用黑底；不回退该核心原画面，所有打乱素材源音静音"
                if explicit_display_modes else
                "所选打乱素材按每刀秒数随机填充核心未保留的画面时段，选多少用多少，不循环；素材耗尽或未选择时回退同一时刻核心原画面，不改变主体时长，不在末尾追加片段"
                if full_core_audio_once else
                "所选区间内随机取够预算，不循环，库存不足报错"
                if random_mode and shuffle_budget_seconds is not None
                else "所选打乱区间全部片段随机排列一次" if random_mode
                else "按原声语义匹配"
            ),
            "audio_exported": not random_mode,
            "price_filter": False,
            "subtitles_added": False,
            "similarity_checked": any(check["earlier_variants_checked"] > 0 for check in visual_checks),
            "similarity_scope": "shuffle_shots_only" if visual_checks else None,
            "similarity_tracks": ["before_overlays", "rendered"] if visual_checks else [],
            "threshold_percent": app.FISSION_SIMILARITY_LIMIT if visual_checks else None,
            "pairs": pairs,
            "history_comparison_policy": "track_scores_only" if (check_batch or check_history) else None,
            "history_videos_checked": len({p["history_id"] for p in historical}),
            "max_history_source_similarity_percent": max((p["source_similarity_percent"] for p in historical), default=None),
            "max_history_rendered_similarity_percent": max((p["rendered_similarity_percent"] for p in historical), default=None),
            "max_source_similarity_percent": max((pair["source_similarity_percent"] for pair in pairs), default=None),
            "max_rendered_similarity_percent": max((pair["rendered_similarity_percent"] for pair in pairs), default=None),
            "max_similarity_percent": max((pair["similarity_percent"] for pair in pairs), default=None),
            "compliance_scan_performed": False,
            "outputs": [path.name for path in accepted_paths],
            "output_durations_seconds": list(accepted_durations),
            **(
                {
                    "dongge_tier1_dedup": {
                        "enabled": True,
                        "version": "tier1-9items-v1",
                        "variants": [
                            {
                                "variant_index": idx,
                                "base_body_duration_seconds": (m.get("dongge_tier1_dedup") or {}).get("base_body_duration_seconds"),
                                "staggered_body_duration_seconds": (m.get("dongge_tier1_dedup") or {}).get("staggered_body_duration_seconds"),
                                "duration_scale": (m.get("dongge_tier1_dedup") or {}).get("duration_scale"),
                                "tail_audio_tempo": (m.get("dongge_tier1_dedup") or {}).get("tail_audio_tempo"),
                                "cut_count": (m.get("dongge_tier1_dedup") or {}).get("cut_count", m.get("shuffle_cuts_used")),
                                "cut_min_seconds": (m.get("dongge_tier1_dedup") or {}).get("cut_min_seconds"),
                                "cut_max_seconds": (m.get("dongge_tier1_dedup") or {}).get("cut_max_seconds"),
                                "adjacent_same_source_cuts": (m.get("dongge_tier1_dedup") or {}).get("adjacent_same_source_cuts"),
                                "cross_variant_reused_cuts": (m.get("dongge_tier1_dedup") or {}).get("cross_variant_reused_cuts"),
                                "gop_frames": (m.get("dongge_tier1_dedup") or {}).get("gop_frames"),
                                "video_crf": (m.get("dongge_tier1_dedup") or {}).get("video_crf"),
                                "audio_bitrate": (m.get("dongge_tier1_dedup") or {}).get("audio_bitrate"),
                            }
                            for idx, m in enumerate(manifests, 1)
                        ],
                    },
                }
                if any((m.get("dongge_tier1_dedup") or {}).get("enabled") for m in manifests)
                else {}
            ),
            **(
                {
                    "rotated_core_summary": [
                        {
                            "variant_index": idx,
                            "output": accepted_paths[idx - 1].name if idx - 1 < len(accepted_paths) else None,
                            "lead_core_index": m.get("lead_core_index"),
                            "lead_core_name": m.get("lead_core_name"),
                            "lead_core_start_seconds": m.get("primary_selection_source_start_seconds"),
                            "core_kept_picture_duration_seconds": m.get("core_kept_picture_duration_seconds"),
                            "shuffle_duration_seconds": m.get("shuffle_duration_seconds"),
                            "output_duration_seconds": m.get("output_duration_seconds"),
                            "shuffle_cuts_used": m.get("shuffle_cuts_used"),
                            "audio_core_order": m.get("audio_core_order", []),
                        }
                        for idx, m in enumerate(manifests, 1)
                    ],
                }
                if rotate_cores_mode else {}
            ),
            "variants": list(manifests),
        }

    def publish_delivery_state(complete, error=""):
        delivery_report = report(complete, error)
        if multi:
            app.publish_fission_report(run_dir, delivery_report)
        else:
            _write_report(compliance_path, delivery_report)
        update(
            output_ready=bool(accepted_paths) and not multi,
            output_path=str(output_path), compliance_path=str(compliance_path),
            fission_ready=bool(accepted_paths) and multi,
            fission_completed=len(accepted_paths),
            fission_output_paths=[str(path) for path in accepted_paths] if multi else [],
            fission_output_durations=list(accepted_durations) if multi else [],
            fission_zip_path="", fission_max_similarity=delivery_report["max_similarity_percent"],
            output_duration_seconds=accepted_durations[0] if accepted_durations and not multi else None,
        )

    try:
        if edit_mode not in {MODE, RANDOM_MODE}:
            raise ValueError("原声剪辑模式无效")
        if core_audio_policy not in {"legacy_selected_then_silence", "selected_core_loop", "full_core_once"}:
            raise ValueError("核心原声音频策略无效")
        if batch_picture_policy not in ("rotate_cores", "per_core", "first_core_then_shuffle"):
            raise ValueError("批量画面编排选项无效。")
        if batch_picture_policy == "rotate_cores" and not full_core_audio_once:
            raise ValueError("轮换核心模式仅适用于完整核心原声模式。")
        if batch_picture_policy == "first_core_then_shuffle" and not (full_core_audio_once and multi):
            raise ValueError("首段后全打乱须使用首条选区倍率的多条制作。")
        if duration_multiplier is not None:
            try:
                dm_val = float(duration_multiplier)
            except (TypeError, ValueError) as exc:
                raise ValueError("时长倍率必须是正的有限数值") from exc
            if isinstance(duration_multiplier, bool) or not math.isfinite(dm_val) or dm_val <= 0:
                raise ValueError("时长倍率必须是正的有限数值")
            duration_multiplier = dm_val
        if type(primary_selection_first) is not bool:
            raise ValueError("第一条选区前移参数必须是布尔值")
        if selected_primary_first and (not bounded_core_timeline or not has_picture_core_modes
                                       or primary_shuffle_budget_seconds is not None):
            raise ValueError("选区前移仅适用于已设置主体目标时长或倍率的带画面核心模式，且不能设置旧首条打乱预算")
        if core_timeline_seconds is not None:
            if not has_picture_core_modes:
                raise ValueError("主体目标时长仅适用于完整原声来源且至少包含一条带画面核心的模式")
            if primary_shuffle_budget_seconds is not None:
                raise ValueError("主体目标时长不能与首条打乱画面预算同时设置")
            try:
                body_target = float(core_timeline_seconds)
            except (TypeError, ValueError) as exc:
                raise ValueError("主体目标时长必须是正的有限数值") from exc
            if isinstance(core_timeline_seconds, bool) or not math.isfinite(body_target) or body_target <= 0:
                raise ValueError("主体目标时长必须是正的有限数值")
            core_timeline_seconds = body_target
        if rotate_cores_mode:
            if duration_multiplier is None and core_timeline_seconds is None:
                raise ValueError("轮换核心模式需要指定时长倍率或目标时长")
            if primary_shuffle_budget_seconds is not None:
                raise ValueError("轮换核心模式不能同时设置旧首条打乱画面预算")
        if primary_shuffle_budget_seconds is not None:
            if not first_core_picture_trim:
                raise ValueError("首条打乱画面预算仅适用于完整原声一次且第一条核心画面裁剪的模式")
            try:
                primary_budget = float(primary_shuffle_budget_seconds)
            except (TypeError, ValueError) as exc:
                raise ValueError("首条打乱画面预算必须是非负有限数值") from exc
            if isinstance(primary_shuffle_budget_seconds, bool) or not math.isfinite(primary_budget) or primary_budget < 0:
                raise ValueError("首条打乱画面预算必须是非负有限数值")
            primary_shuffle_budget_seconds = primary_budget
        if random_mode and not full_core_audio_once and shuffle_budget_seconds is not None:
            if (isinstance(shuffle_budget_seconds, bool)
                    or not math.isfinite(float(shuffle_budget_seconds))
                    or float(shuffle_budget_seconds) < 0):
                raise ValueError("打乱素材时长预算必须是非负有限数值")
            shuffle_budget_seconds = float(shuffle_budget_seconds)
        if not app.LOCAL_NO_VALIDATION or app.CUSTOMER_BILLING_REQUIRED or app.STRICT_ERP_RELAY:
            raise RuntimeError("原声穿插剪辑目前仅用于本机模式；未执行商业计费或 ERP 上报")
        if count not in {1, 3, 4, 5}:
            raise ValueError("成片数量只能选择 1、3、4 或 5 条")
        if type(fixed_shuffle_cuts) is not bool:
            raise ValueError("固定刀长选项必须是布尔值")
        if fixed_shuffle_cuts and (not (selected_primary_first or rotate_cores_mode) or not 1 <= float(cut_seconds) <= 10):
            raise ValueError("固定刀长仅用于选区倍率或轮换核心模式，每刀须为 1–10 秒")
        if not math.isfinite(float(cut_seconds)) or not 0.5 <= float(cut_seconds) <= 30:
            raise ValueError("每刀时长必须在 0.5–30 秒之间")
        app.validate_queued_materials(selected_materials, root)
        core_paths = [root / "核心素材" / name for name in selected_materials.get("核心素材", [])]
        shuffle_paths = [root / "打乱素材池" / name for name in selected_materials.get("打乱素材池", [])]
        openings = list(selected_materials.get("开头视频", []))
        app.choose_opening(openings, 1)
        if not core_paths:
            raise ValueError("请至少选择 1 条核心素材")
        allowed_modes = {"audio_only", "video_audio", "video_only", "trimmed_video_audio"}
        if rotate_cores_mode and not explicit_display_modes:
            raise ValueError("轮换核心模式需要提供每条核心的音画显示选项")
        if explicit_display_modes:
            if (not isinstance(core_display_modes, list) or len(core_display_modes) != len(core_paths)
                    or any(not isinstance(mode, str) or mode not in allowed_modes for mode in core_display_modes)):
                raise ValueError("核心显示选项必须与所选核心一一对应，且每条须选择有效的音画模式")
            if not any(mode in {"audio_only", "video_audio", "trimmed_video_audio"} for mode in core_display_modes):
                raise ValueError("请至少保留 1 条带音频的核心素材提供原声。")
            if rotate_cores_mode and not any(mode in {"video_audio", "video_only", "trimmed_video_audio"} for mode in core_display_modes):
                raise ValueError("请至少将 1 条核心素材设为「画面＋完整音频」或「仅裁剪画面」。")
        if not shuffle_paths and not full_core_audio_once:
            raise ValueError("请至少选择 1 条核心素材和 1 条打乱素材")
        if check_batch and not shuffle_paths:
            raise ValueError("批量制作需要打乱素材来验收 40% 相似度；只制作核心时请选择单条。")
        checkpoint()

        def build_render_options(variant_idx, *, use_unique_batch=None, vbatch=None):
            if use_unique_batch is None:
                use_unique_batch = unique_shuffle and fixed_output_cuts
            opts = {
                "duration_multiplier": duration_multiplier,
                "variant_index": variant_idx,
            }
            if batch_picture_policy != "per_core":
                opts["batch_picture_policy"] = batch_picture_policy
            if bottom_layers:
                opts["bottom_layers"] = bottom_layers
            if use_unique_batch and shuffle_paths:
                opts["unique_batch"] = (run_id, variant_idx - 1, count)
                opts["visual_batch"] = vbatch
            if random_mode:
                opts.update(
                    visual_policy="full_core_timeline" if full_core_audio_once else "append_shuffle",
                    shuffle_ranges=shuffle_ranges,
                    core_ranges=core_ranges if not explicit_display_modes or has_picture_core_modes else None,
                    core_audio_policy=core_audio_policy,
                )
                if full_core_audio_once:
                    opts["core_display_modes"] = core_display_modes
                if first_core_picture_trim and primary_shuffle_budget_seconds is not None:
                    opts["primary_shuffle_budget_seconds"] = primary_shuffle_budget_seconds
                if full_core_audio_once and (has_picture_core_modes or rotate_cores_mode):
                    if core_timeline_seconds is not None:
                        opts["core_timeline_seconds"] = core_timeline_seconds
                    opts["primary_selection_first"] = primary_selection_first
                    opts["fixed_shuffle_cuts"] = bool(fixed_shuffle_cuts or rotate_cores_mode)
                if not full_core_audio_once:
                    opts["shuffle_budget_seconds"] = shuffle_budget_seconds
            return opts

        def fill_snapshot():
            all_pairs = [
                pair
                for m in manifests
                for pair in (m.get("shuffle_visual_verification") or {}).get("pair_results", [])
            ]
            return dict(
                accepted_paths=list(accepted_paths),
                accepted_durations=list(accepted_durations),
                accepted_hashes=[
                    (m.get("shuffle_visual_verification") or {}).get("pre_overlay_dhashes")
                    or m.get("source_shuffle_dhashes")
                    or []
                    for m in manifests
                ],
                pair_results=all_pairs,
                variant_plans=[],
                selected_indices=(
                    [list(row) if row is not None else [] for row in visual_batch.selected_indices[:len(accepted_paths)]]
                    if visual_batch is not None and getattr(visual_batch, "selected_indices", None)
                    else []
                ),
                attempts=int(visual_batch.attempts) if visual_batch is not None else 0,
            )

        fill_store = app.get_task_queue_store(create=False) if hasattr(app, "get_task_queue_store") else None
        fill_enabled = bool(multi and fill_store is not None and fill_store.get(run_id))
        restored = None
        if fill_enabled:
            overlay_sig = [
                dict(row, path=material_signature_identity(row.get("path", "")))
                if isinstance(row, dict) and "path" in row else row
                for row in (bottom_layers or [])
            ]
            signature = hashlib.sha256(fill_canonical(_json_safe(dict(
                edition_mode="dongge",
                dongge_timeline_mode="core_audio",
                material_base_dir=material_signature_identity(root),
                selected_materials=selected_materials,
                core_material_signatures=[_material_file_signature(p) for p in core_paths],
                shuffle_material_signatures=[_material_file_signature(p) for p in shuffle_paths],
                opening_material_signatures=[_material_file_signature(root / "开头视频" / name) for name in openings],
                fission_count=count,
                edit_mode=edit_mode,
                cut_seconds=float(cut_seconds),
                core_audio_policy=core_audio_policy,
                core_timeline_seconds=core_timeline_seconds,
                duration_multiplier=float(duration_multiplier) if duration_multiplier is not None else None,
                primary_shuffle_budget_seconds=primary_shuffle_budget_seconds,
                shuffle_budget_seconds=shuffle_budget_seconds,
                primary_selection_first=bool(primary_selection_first),
                fixed_shuffle_cuts=bool(fixed_shuffle_cuts),
                unique_shuffle=bool(unique_shuffle),
                core_ranges=core_ranges,
                shuffle_ranges=shuffle_ranges,
                core_display_modes=core_display_modes,
                batch_picture_policy=batch_picture_policy,
                bottom_layers=overlay_sig,
                shuffle_masks=shuffle_masks,
                face_effects=face_effects,
                sticker_settings=sticker_settings,
            ))).encode("utf-8")).hexdigest()

            def fill_notify(metadata):
                metadata = dict(metadata)
                metadata["failure_reason"] = app.redact_local_paths(metadata.get("failure_reason") or "")
                metadata["message"] = app.redact_local_paths(metadata.get("message") or "")
                if not fill_store.update_auto_fill(run_id, metadata):
                    raise FillCheckpointError("自动补做状态无法持久化，停止以免重复制作")
                update(
                    auto_fill=metadata,
                    **({"stage": metadata["message"]} if metadata.get("retry_round") else {}),
                )

            checkpoint_root = Path(getattr(app, "QUEUE_OUTPUT_ROOT", output_path.parent.parent))
            checkpoint_owner = (
                getattr(app, "TENANT_USER_ID", None)
                or getattr(app, "INSTANCE_ID", None)
                or "local"
            )
            fill_session = RenderCheckpoint(
                checkpoint_root / run_id / ".auto-fill.json",
                run_id,
                checkpoint_owner,
                count,
                signature,
                run_dir,
                fill_notify,
            )
            restored = fill_session.restore()
            if fill_session.data["state"] == "fill_failed":
                raise FillCheckpointError(fill_session.data["error"])

        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / ".dongge").touch()
        if fill_session is not None:
            for stale_tmp in run_dir.glob(".pending_第*"):
                stale_tmp.unlink(missing_ok=True)
        app.acquire_renderer_disk_reservation(run_id)
        reserved = True
        selected_bytes = sum(path.stat().st_size for path in core_paths + shuffle_paths)
        selected_bytes += sum((root / "开头视频" / name).stat().st_size for name in openings)
        app.publish_renderer_disk_reservation(run_id, max(512 * 1024**2, selected_bytes * 3))
        ffmpeg = os.environ.get("V16_FFMPEG_EXE") or shutil.which("ffmpeg") or "ffmpeg"
        ffprobe = os.environ.get("V16_FFPROBE_EXE") or shutil.which("ffprobe") or "ffprobe"
        if random_mode and not multi and shuffle_paths:
            opening_1 = app.choose_opening(openings, 1)
            opening_path_1 = root / "开头视频" / opening_1 if opening_1 else None
            seed_1 = int.from_bytes(hashlib.sha256(f"{run_id}:1".encode("utf-8")).digest()[:8], "big")
            probe_plan = plan_edit(
                core_paths, shuffle_paths,
                opening_path=opening_path_1, cut_seconds=float(cut_seconds),
                width=720, height=1280, fps=30, seed=seed_1, ffprobe=ffprobe,
                **build_render_options(1, use_unique_batch=(unique_shuffle and fixed_output_cuts), vbatch=None),
            )
            probe_shuffle_frames = sum(s["frame_count"] for s in probe_plan["shots"] if s["role"] == "shuffle")
            if probe_shuffle_frames >= probe_plan["fps"] * 0.5:
                if unique_shuffle and fixed_output_cuts:
                    check_history = True
                else:
                    try:
                        batch_probe = plan_edit(
                            core_paths, shuffle_paths,
                            opening_path=opening_path_1, cut_seconds=float(cut_seconds),
                            width=720, height=1280, fps=30, seed=seed_1, ffprobe=ffprobe,
                            **build_render_options(1, use_unique_batch=True, vbatch=None),
                        )
                        if sum(s["frame_count"] for s in batch_probe["shots"] if s["role"] == "shuffle") >= batch_probe["fps"] * 0.5:
                            check_history = True
                            unique_shuffle = True
                            fixed_output_cuts = True
                    except ValueError:
                        pass
        if fixed_output_cuts:
            log(f"-> 打乱画面基准刀长 {float(cut_seconds):g} 秒（自动防重浮动切点约 2–3 秒），不混入素材零头；已启用 Tier-1 自动防重（成片总时长 ±15%–20% 错位、多母片禁止同源相邻＋跨条零复用优先、开场核心原画与 1.000x 原声对口型不动＋后续打乱段语速保音高微调）。")
        if explicit_display_modes:
            labels = [f"第 {index} 条：{_display_mode_label(mode)}"
                      for index, mode in enumerate(core_display_modes, 1)]
            log("-> 核心显示设置：" + "；".join(labels) + "。")
            if rotate_cores_mode:
                mult_desc = (f"本条主核心裁剪时长×{duration_multiplier:g}" if duration_multiplier is not None
                             else f"固定目标 {core_timeline_seconds:g} 秒")
                log(f"-> 轮换核心开场模式：各条成片依次轮换带画面的核心裁剪片段作为开场（跳过仅音频核心），主体目标为 {mult_desc}；开头保留核心原片与同步原声不动，终点后剩余原声分配给打乱画面，播完自动拉取后续核心原声接续（仅画面核心不供音）。")
            elif batch_picture_policy == "first_core_then_shuffle":
                log("-> 首条核心选区从主体开头播放，随后至片尾全部使用静音打乱素材；开头保留核心原片与同步原声不动，终点后剩余原声分配给打乱画面，播完自动拉取后续核心原声接续。")
            elif selected_primary_first:
                log("-> 开头保留核心原片与同步原声不动，终点后剩余原声分配给打乱画面，播完自动拉取后续核心原声接续；视频末端允许截断，不循环。")
            elif first_core_picture_trim:
                end_note = "核心原声连续使用至主体目标末端，视频末端允许截断，不循环。" if bounded_core_timeline else "第一条完整原声不截短、不循环。"
                log("-> 第一条核心画面裁剪：所选区间保留原画面，区间外使用所选打乱素材一次；素材用完则回退第一条同一时刻原画面。" + end_note)
                if primary_shuffle_budget_seconds is not None:
                    log(f"-> 首条打乱画面请求预算 {primary_shuffle_budget_seconds:g} 秒，以第一条原片全长×(倍率−1)计算；实际最多使用首条未保留画面时长和现有素材可供时长，后续核心选项不受该预算控制，原声音轨与成片总时长不变。")
            if bounded_core_timeline and not rotate_cores_mode:
                target_basis = "第一条所选画面区间长度×倍率" if selected_primary_first else "第一条原片全长×倍率"
                target_label = f"{core_timeline_seconds:g} 秒" if core_timeline_seconds is not None else f"倍率 {duration_multiplier:g}×"
                picture_rule = "打乱片段可在各条成片中共用，按整段打乱画面验收" if check_batch else "不足时循环打乱画面"
                if batch_picture_policy == "first_core_then_shuffle":
                    log(f"-> 主体目标 {target_label}（{target_basis}），开头另加。打乱镜头每刀 {float(cut_seconds):g} 秒，逐条验收。")
                else:
                    log(f"-> 主体目标 {target_label}（{target_basis}），开头另加。核心原声按顺序连续播放至片尾。后续核心按原设置保留画面或仅保留音频；{picture_rule}，未选打乱时仅音频段使用黑底。打乱素材静音，每刀 {float(cut_seconds):g} 秒。")
            elif not bounded_core_timeline:
                log(f"-> 开头原声播放一次，全部核心原声按所选顺序完整连续播放一次，不循环、不拉伸、不延长。选择只保留音频的核心不显示原画面，其画面由所选打乱素材按每刀 {float(cut_seconds):g} 秒提供；素材不足则循环打乱画面，没有素材则使用黑底。选择保留画面和音频的核心完整保留原画面。打乱素材源音静音，不单独导出音频。")
        elif full_core_audio_once:
            log(f"-> 完整开头之后保留完整核心时间轴：核心原声按原顺序连续播放一次，不受画面截取区间影响，不循环、不拉伸、不延长；核心区间只控制优先保留哪些原画面，其余时段用所选打乱素材按每刀 {float(cut_seconds):g} 秒随机填充，选多少用多少；素材用完或未选择时回退同一时刻核心原画面。打乱素材源音静音，不在末尾追加片段；不调用语义选镜，不单独导出音频。")
        elif random_mode:
            shuffle_instruction = (
                f"所选打乱区间按每刀 {float(cut_seconds):g} 秒切分后随机取够 {shuffle_budget_seconds:g} 秒，不循环，库存不足报错"
                if shuffle_budget_seconds is not None else
                f"所选打乱区间按每刀 {float(cut_seconds):g} 秒切分后随机排列一次"
            )
            audio_instruction = (
                "开头原声播放一次；所选核心原声按核心顺序循环补足整个主体，覆盖核心及打乱画面，保持原速度与音量；打乱素材自身声音全部静音"
                if loop_core_audio else "开头与所选核心原声各保留一次，末尾打乱片段保持静音（旧任务音频策略）"
            )
            log(f"-> 完整开头→所选核心区间→打乱画面：核心默认完整，若用户设置起止秒数则同步保留该段原画原声；不自动截短或重排核心。{shuffle_instruction}。{audio_instruction}；不调用语义选镜，不单独导出音频。")
        else:
            log(f"-> 原声穿插模式：每刀 {float(cut_seconds):g} 秒，核心画面为主，按原声内容匹配静音打乱画面；完整开头原音连接完整核心原音。")
        update(edit_mode=edit_mode, cut_seconds=float(cut_seconds), duration_policy=duration_policy,
               core_audio_policy=core_audio_policy if random_mode else "legacy_semantic_interleave",
               core_audio_covers_body=core_audio_covers_body,
               core_display_modes=core_display_modes if full_core_audio_once else None,
               primary_shuffle_budget_seconds=primary_shuffle_budget_seconds if first_core_picture_trim else None,
               core_timeline_seconds=core_timeline_seconds,
               primary_selection_first=primary_selection_first,
               fixed_shuffle_cuts=fixed_output_cuts,
               natural_tail_limit_seconds=0)
        history = None
        if (check_batch or check_history) and shuffle_paths:
            from shuffle_history import ShuffleHistory
            from shuffle_history_gate import BatchHistory
            from visual_shuffle import VisualShuffleBatch
            visual_batch = VisualShuffleBatch(ffmpeg, owned_run, log, app.FISSION_SIMILARITY_LIMIT)
            history_root = Path(app.FISSION_OUTPUT_ROOT).parent
            history = ShuffleHistory(history_root / ".v17_shuffle_history.sqlite3")
            log("正在核对本机已有成片的打乱镜头记录…")
            history.sync([Path(app.FISSION_OUTPUT_ROOT), history_root / "排队任务成片"],
                         ffmpeg, owned_run, exclude_run_id=run_id, log=log, only_dongge=True)
            visual_batch.history = BatchHistory(history, run_id, app.FISSION_SIMILARITY_LIMIT)
            if multi:
                log(f"打乱镜头可共用。整批两两查重，每条另与 {len(visual_batch.history.entries)} 条历史成片比较，"
                    f"整段打乱原画和导出打乱画面的相似度均须 ≤{app.FISSION_SIMILARITY_LIMIT:g}%。")
            else:
                log(f"单条制作启用历史查重：与 {len(visual_batch.history.entries)} 条历史成片比较，"
                    f"整段打乱原画和导出打乱画面的相似度均须 ≤{app.FISSION_SIMILARITY_LIMIT:g}%。")

        if fill_session is not None and restored:
            restored_paths = list(restored.get("accepted_paths") or [])
            restored_paths_ok, restored_durations_ok, restored_manifests_ok = [], [], []
            restored_fingerprints = []
            for idx, mp4_path in enumerate(restored_paths, 1):
                expected_dest = (run_dir / f"V17裂变成片_第{idx:02d}条.mp4").resolve()
                sidecar_path = mp4_path.with_suffix(".manifest.json")
                if mp4_path != expected_dest or not mp4_path.is_file() or mp4_path.stat().st_size <= 0 or not sidecar_path.is_file():
                    break
                try:
                    item_manifest = json.loads(sidecar_path.read_text(encoding="utf-8"))
                    if not isinstance(item_manifest, dict) or item_manifest.get("native_render_engine") != "run_v16_enhanced.py":
                        break
                    item_dur = float(item_manifest.get("output_duration_seconds", item_manifest.get("duration_seconds", 0)))
                    if not math.isfinite(item_dur) or item_dur <= 0:
                        break
                    if visual_batch is not None:
                        verif = item_manifest.get("shuffle_visual_verification") or {}
                        pre_hex = verif.get("pre_overlay_dhashes")
                        item_pairs = verif.get("pair_results", [])
                        item_hist = verif.get("history_pairs", [])
                        if (not isinstance(pre_hex, list) or len(pre_hex) != 48
                                or verif.get("earlier_variants_checked") != idx - 1
                                or verif.get("sample_count") != 48
                                or [(p["first"], p["second"]) for p in item_pairs] != [(j, idx) for j in range(1, idx)]
                                or any(not 0 <= p["similarity_percent"] <= app.FISSION_SIMILARITY_LIMIT for p in item_pairs)
                                or verif.get("history_videos_checked") != len(item_hist)
                                or any(not 0 <= p[k] <= app.FISSION_SIMILARITY_LIMIT for p in item_hist
                                       for k in ("source_similarity_percent", "rendered_similarity_percent"))):
                            break
                        ren_hashes = rendered_shuffle_hashes(mp4_path, item_manifest, ffmpeg, owned_run)
                        if len(ren_hashes) != 48:
                            break
                        if verif.get("source_sampling_method") == "same_encoded_track_without_overlay_or_mask":
                            src_hashes = ren_hashes
                        else:
                            src_hashes = [int(h, 16) for h in pre_hex]
                        restored_fingerprints.append((src_hashes, ren_hashes))
                except (OSError, ValueError, KeyError, TypeError):
                    break
                restored_paths_ok.append(mp4_path)
                restored_durations_ok.append(round(item_dur, 3))
                restored_manifests_ok.append(item_manifest)

            valid_k = len(restored_manifests_ok)
            accepted_paths = restored_paths_ok
            accepted_durations = restored_durations_ok
            manifests = restored_manifests_ok
            if visual_batch is not None:
                visual_batch.fingerprints = restored_fingerprints
            if valid_k < len(restored_paths):
                log(f"-> 断点检查：第 {valid_k + 1} 条缺少完整镜头清单，安全回退重跑第 {valid_k + 1}–{count} 条。")
                for stale_path in restored_paths[valid_k:]:
                    stale_path.unlink(missing_ok=True)
                    stale_path.with_suffix(".manifest.json").unlink(missing_ok=True)
                fill_session.data["files"] = fill_session.data["files"][:valid_k]
                fill_session.data["similarity_batch_start"] = min(
                    fill_session.data.get("similarity_batch_start", 0), valid_k
                )
                fill_session.data["state"] = "rendering"
                fill_session.data["error"] = ""
                if valid_k:
                    rolled = fill_snapshot()
                    rolled["accepted_paths"] = [str(p) for p in rolled["accepted_paths"]]
                    fill_session.data["snapshot"] = rolled
                else:
                    fill_session.data["snapshot"] = {}
                fill_session._write()
                fill_session.notify(fill_session.public())
            if valid_k > 0:
                if visual_batch is not None and history is not None:
                    history.sync([Path(app.FISSION_OUTPUT_ROOT)], ffmpeg, owned_run,
                                 only_run_ids={run_id}, only_dongge=True)
                    if valid_k < count and unique_shuffle and fixed_output_cuts and shuffle_paths:
                        for warm_v in range(1, valid_k + 1):
                            warm_opening = app.choose_opening(openings, warm_v)
                            warm_opening_path = root / "开头视频" / warm_opening if warm_opening else None
                            warm_seed = int.from_bytes(hashlib.sha256(f"{run_id}:{warm_v}".encode("utf-8")).digest()[:8], "big")
                            plan_edit(
                                core_paths, shuffle_paths,
                                opening_path=warm_opening_path, cut_seconds=float(cut_seconds),
                                width=720, height=1280, fps=30, seed=warm_seed, ffprobe=ffprobe,
                                **build_render_options(warm_v, vbatch=visual_batch),
                            )
                        saved_orders = restored.get("selected_indices") or []
                        if (len(saved_orders) >= valid_k
                                and getattr(visual_batch, "durations_by_variant", None)
                                and all(isinstance(row, (list, tuple))
                                        and visual_batch.durations_by_variant[v_idx] is not None
                                        and len(row) == len(visual_batch.durations_by_variant[v_idx])
                                        and all(isinstance(item, int) and not isinstance(item, bool)
                                                and 0 <= item < visual_batch.distinct_window_count for item in row)
                                        for v_idx, row in enumerate(saved_orders[:valid_k]))):
                            for v_idx, row in enumerate(saved_orders[:valid_k]):
                                tup = tuple(row)
                                prior_restored = [
                                    r for r in visual_batch.selected_indices[:v_idx] if r is not None
                                ]
                                visual_batch.selected_indices[v_idx] = tup
                                visual_batch.schedule[v_idx] = [visual_batch.windows[i] for i in tup]
                                if getattr(visual_batch, "reuse_stats_by_variant", None) is not None:
                                    visual_batch.reuse_stats_by_variant[v_idx] = visual_batch._row_stats(
                                        tup, prior_restored
                                    )
                            visual_batch.replace_rejected(valid_k)
                        saved_attempts = restored.get("attempts", valid_k)
                        if isinstance(saved_attempts, int) and saved_attempts >= valid_k:
                            visual_batch.attempts = min(saved_attempts, app.fission_candidate_limit(count, valid_k))
                        else:
                            visual_batch.attempts = valid_k
                if valid_k < count:
                    log(f"-> 断点续跑：已核验恢复前 {valid_k}/{count} 条成片及打乱指纹，继续制作第 {valid_k + 1}–{count} 条。")
                    publish_delivery_state(False)
                    if hasattr(app, "checkpoint_running_fission_result"):
                        app.checkpoint_running_fission_result(run_id, count, accepted_paths, accepted_durations)
                    update(fission_completed=valid_k, progress=round(valid_k * 95 / count))
                else:
                    log(f"-> 断点续跑：已核验恢复全部 {count}/{count} 条成片，进入收尾确认。")

        for index in range(len(accepted_paths) + 1, count + 1):
            checkpoint()
            destination = run_dir / f"V17裂变成片_第{index:02d}条.mp4" if multi else output_path
            staged_video = run_dir / f".pending_第{index:02d}条.mp4" if fill_session is not None else None
            staged_manifest = run_dir / f".pending_第{index:02d}条.manifest.json" if fill_session is not None else None
            opening_name = app.choose_opening(openings, index)
            opening_path = root / "开头视频" / opening_name if opening_name else None
            update(current_variant=index, progress=max(1, round((index - 1) * 95 / count)),
                   stage=f"正在制作{mode_label}成片 {index}/{count}")
            seed = int.from_bytes(hashlib.sha256(f"{run_id}:{index}".encode("utf-8")).digest()[:8], "big")
            render_options = build_render_options(index, vbatch=visual_batch)
            attempt_index = 0

            def render_candidate():
                nonlocal attempt_index
                checkpoint()
                attempt = attempt_index
                attempt_index += 1
                plan = plan_edit(
                    core_paths, shuffle_paths,
                    opening_path=opening_path, cut_seconds=float(cut_seconds),
                    width=720, height=1280, fps=30, seed=seed, ffprobe=ffprobe, **render_options)
                if batch_picture_policy == "rotate_cores" and attempt == 0:
                    lead_start_s = float(plan.get("primary_selection_source_start_seconds") or 0.0)
                    lead_end_s = lead_start_s + float(plan.get("core_kept_picture_duration_seconds") or 0.0)
                    log(f"-> 第 {index}/{count} 条主核心画面：{plan.get('lead_core_name')}（保留区间 {lead_start_s:.2f}s–{lead_end_s:.2f}s，共 {plan['core_kept_picture_duration_seconds']:.2f} 秒，同步原声不动），后续原声接力顺序：{' → '.join(plan.get('audio_core_order', []))}。")
                t1_info = plan.get("dongge_tier1_dedup")
                if isinstance(t1_info, dict) and t1_info.get("enabled") and attempt == 0:
                    c_min = t1_info.get("cut_min_seconds")
                    c_max = t1_info.get("cut_max_seconds")
                    cut_span_desc = (
                        f"单刀 {c_min:.2f}s–{c_max:.2f}s（共 {t1_info.get('cut_count', 0)} 刀）"
                        if c_min is not None and c_max is not None
                        else f"基准刀长 {float(cut_seconds):g}s"
                    )
                    log(
                        f"-> 第 {index}/{count} 条 Tier-1 自动防重：主体时长 {t1_info['base_body_duration_seconds']:.2f}s → "
                        f"{t1_info['staggered_body_duration_seconds']:.2f}s（缩放 {t1_info['duration_scale']:.3f}x），"
                        f"{cut_span_desc}，同源相邻 {t1_info.get('adjacent_same_source_cuts', 0)} 处，"
                        f"跨条复用 {t1_info.get('cross_variant_reused_cuts', 0)} 刀，"
                        f"开场核心 1.000x 不动＋后续语速 {t1_info['tail_audio_tempo']:.3f}x。"
                    )
                with tempfile.TemporaryDirectory(prefix=".native-core-", dir=run_dir) as directory:
                    work = Path(directory)
                    candidate = work / "render.mp4"
                    raw = work / "before-effects.gray"
                    request_path = work / "timeline.json"
                    frames = sum(s["frame_count"] for s in plan["shots"] if s["role"] == "shuffle")
                    capture = frames >= plan["fps"] * .5
                    _write_report(request_path, {
                        "plan": plan, "output": str(candidate), "shuffle_masks": shuffle_masks,
                        "bottom_layers": bottom_layers or [], "pre_effects_gray": str(raw) if capture else None})
                    stickers = app.build_fission_stickers(sticker_settings, index, attempt) if multi else sticker_settings
                    effects = app.build_fission_effects(face_effects, index, attempt) if multi else face_effects
                    mask_name = app.choose_shuffle_mask_name(selected_materials.get("打乱素材池", []), run_id, index, attempt)
                    job_tmp = Path(app.BASE_DIR) / "tmp_process_v16_enhanced" / f"{run_id}-{index:02d}-{uuid.uuid4().hex[:12]}"
                    env = dict(renderer_env, V16_DONGGE_RENDER_REQUEST=str(request_path),
                               KUAISHOU_TASK_ID=run_id, KUAISHOU_JOB_TMP=str(job_tmp), OUT_FILE=str(candidate),
                               V16_OUTPUT_FPS="30", V16_FISSION_VARIANT_SEED=str(seed), V16_MASK_SOURCE_NAME=mask_name,
                               FACE_EFFECTS=",".join(effects) or "none", FACE_EFFECT=effects[0] if effects else "none",
                               V16_SELECTED_MATERIALS_JSON=json.dumps(selected_materials, ensure_ascii=False),
                               V16_STICKER_CORNER_CONFIG=json.dumps(stickers.get("corners", {}), ensure_ascii=False),
                               V16_STICKER_SIZE_PERCENT=str(stickers.get("size_percent", 100)),
                               V16_FULLSCREEN_STICKER_CONFIG=json.dumps(stickers.get("fullscreen", {}), ensure_ascii=False))
                    if rotate_cores_mode:
                        log(f"-> 第 {index} 条启动 8038 原版渲染器，按轮换主核心裁剪画面与连续原声接力时间轴制作。")
                    else:
                        log(f"-> 第 {index} 条启动 8038 原版渲染器，按首条核心选区与原声时间轴制作。")
                    owned_run([renderer_python, app.RUNNER], candidate, env=env, activity_paths=(work, job_tmp))
                    manifest_path = candidate.with_suffix(".manifest.json")
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if manifest.get("native_render_engine") != "run_v16_enhanced.py":
                        raise RuntimeError("原版渲染器未确认本条制作完成。")
                    source_hashes = source_shuffle_hashes(raw, frames, plan["fps"], ffmpeg, owned_run) if capture else None
                    if visual_batch is not None:
                        manifest["shuffle_visual_verification"] = visual_batch.verify(
                            candidate, manifest, source_hashes=source_hashes, destination=destination)
                    elif source_hashes:
                        manifest["source_shuffle_dhashes"] = [f"{h:016x}" for h in source_hashes]
                    published_manifest = destination.with_suffix(".manifest.json")
                    manifest.update(output_path=str(destination), manifest_path=str(published_manifest))
                    _write_report(manifest_path, manifest)
                    if fill_session is not None:
                        staged_manifest.unlink(missing_ok=True)
                        staged_video.unlink(missing_ok=True)
                        os.replace(manifest_path, staged_manifest)
                        os.replace(candidate, staged_video)
                    else:
                        os.replace(manifest_path, published_manifest)
                        os.replace(candidate, destination)
                    return manifest

            if visual_batch is None:
                manifest = render_candidate()
            else:
                candidate_limit = (
                    app.fission_candidate_limit(count, index)
                    if multi
                    else getattr(app, "FISSION_ATTEMPTS_PER_OUTPUT", 4) + getattr(app, "FISSION_BATCH_EXTRA_ATTEMPTS", 8)
                )
                manifest = visual_batch.render_variant(index - 1, render_candidate, candidate_limit)
            rendered_video = staged_video if fill_session is not None else destination
            duration = float(manifest.get("output_duration_seconds", manifest.get("duration_seconds", 0)))
            if not rendered_video.is_file() or rendered_video.stat().st_size <= 0 or not math.isfinite(duration) or duration <= 0:
                raise RuntimeError("原声剪辑未返回有效成片，未登记成功")
            if visual_batch is not None:
                check = manifest.get("shuffle_visual_verification") or {}
                pairs = check.get("pair_results", [])
                history_pairs = check.get("history_pairs", [])
                if (check.get("earlier_variants_checked") != index - 1 or check.get("sample_count") != 48
                        or [(pair["first"], pair["second"]) for pair in pairs] != [(i, index) for i in range(1, index)]
                        or any(not 0 <= pair["similarity_percent"] <= app.FISSION_SIMILARITY_LIMIT for pair in pairs)
                        or check.get("history_videos_checked") != len(history_pairs)
                        or any(not 0 <= p[k] <= app.FISSION_SIMILARITY_LIMIT for p in history_pairs
                               for k in ("source_similarity_percent", "rendered_similarity_percent"))):
                    raise RuntimeError(f"第 {index} 条打乱镜头验收记录不完整，未登记交付。")
            accepted_paths.append(destination)
            accepted_durations.append(round(duration, 3))
            manifests.append(manifest)
            if fill_session is not None:
                try:
                    fill_session.publish(
                        staged_video,
                        destination,
                        staged_manifest,
                        destination.with_suffix(".manifest.json"),
                        fill_snapshot(),
                    )
                except Exception as publish_exc:
                    raise FillCheckpointError("成片发布检查点失败：" + str(publish_exc)) from publish_exc
            if "shuffle_cuts_used" in manifest:
                t1_m = manifest.get("dongge_tier1_dedup") or {}
                if t1_m.get("enabled") and t1_m.get("cut_min_seconds") is not None:
                    log(
                        f"-> 第 {index} 条实际使用 {manifest['shuffle_duration_seconds']:.2f} 秒打乱画面，"
                        f"共 {manifest['shuffle_cuts_used']} 刀（单刀 {t1_m['cut_min_seconds']:.2f}s–{t1_m['cut_max_seconds']:.2f}s）。"
                    )
                else:
                    log(f"-> 第 {index} 条实际使用 {manifest['shuffle_duration_seconds']:.2f} 秒打乱画面，"
                        f"共 {manifest['shuffle_cuts_used']} 刀。")
            if multi and index < count:
                publish_delivery_state(False)
                if fill_enabled and hasattr(app, "checkpoint_running_fission_result"):
                    app.checkpoint_running_fission_result(run_id, count, accepted_paths, accepted_durations)
            update(fission_completed=index, progress=round(index * 95 / count))
            audio_summary = (
                "开头保留核心原片与同步原声不动，终点后剩余原声分配给打乱画面，播完自动拉取后续核心原声接续（仅画面核心不供音，打乱素材源音静音）" if rotate_cores_mode else
                "开头保留核心原片与同步原声不动，终点后剩余原声分配给打乱画面，播完自动拉取后续核心原声接续；打乱素材源音静音" if selected_primary_first else
                "核心原声按所选顺序连续播放至主体目标末端，不循环、不补静音；打乱素材源音静音" if bounded_core_timeline else
                "完整核心原声连续播放一次覆盖主体；打乱素材仅填充画面，源音静音" if full_core_audio_once else
                "核心原声已覆盖全部主体；打乱素材源音静音" if loop_core_audio else "打乱素材源音静音"
            )
            if full_core_audio_once and not explicit_display_modes:
                used = manifest.get("shuffle_duration_seconds")
                fallback = manifest.get("core_fallback_duration_seconds")
                if used is not None and fallback is not None:
                    audio_summary += f"；实际打乱画面 {float(used):.3f} 秒，回退核心画面 {float(fallback):.3f} 秒"
            elif explicit_display_modes:
                used = manifest.get("shuffle_duration_seconds")
                black = manifest.get("solid_background_duration_seconds")
                if used is not None and black is not None:
                    audio_summary += f"；实际打乱画面 {float(used):.3f} 秒，黑底画面 {float(black):.3f} 秒"
                if manifest.get("shuffle_repeated"):
                    audio_summary += "；打乱画面已循环补足，仅循环画面，核心原声不循环"
            if first_core_picture_trim and not bounded_core_timeline and manifest.get("primary_shuffle_used_duration_seconds") is not None:
                requested = f"{primary_shuffle_budget_seconds:g} 秒" if primary_shuffle_budget_seconds is not None else "未设（兼容旧任务）"
                audio_summary += f"；首条打乱画面请求 {requested}，实际使用 {float(manifest['primary_shuffle_used_duration_seconds']):.3f} 秒"
            log(f"-> 第 {index}/{count} 条已验证，时长 {duration:.3f} 秒；{audio_summary}。")
        if fill_session is not None:
            fill_session.rendered()
        checkpoint()
        publish_delivery_state(True)
        if not multi:
            from shuffle_history import ShuffleHistory
            history_root = Path(app.FISSION_OUTPUT_ROOT).parent
            history = ShuffleHistory(history_root / ".v17_shuffle_history.sqlite3")
            history.sync([run_dir.parent], ffmpeg, owned_run, only_run_ids={run_id},
                         only_dongge=True, log=log)
        result = app.queue_result_for_outputs(run_id, count, output_path, accepted_paths, accepted_durations)
        result["edition_mode"] = "dongge"
        result["dongge_timeline_mode"] = "core_audio"
        result["native_render_engine"] = "run_v16_enhanced.py"
        result["auto_save"] = app.pending_auto_save_metadata(auto_save_directory)
        result["edit_mode"] = edit_mode
        result["cut_seconds"] = float(cut_seconds)
        result["core_timeline_seconds"] = core_timeline_seconds
        if duration_multiplier is not None:
            result["duration_multiplier"] = float(duration_multiplier)
        result["batch_picture_policy"] = batch_picture_policy
        result["primary_selection_first"] = primary_selection_first
        result["fixed_shuffle_cuts"] = fixed_output_cuts
        final_report = report(True)
        if visual_batch is not None:
            result.update(max_similarity_percent=final_report["max_similarity_percent"],
                          max_source_similarity_percent=final_report["max_source_similarity_percent"],
                          max_rendered_similarity_percent=final_report["max_rendered_similarity_percent"],
                          threshold_percent=final_report["threshold_percent"])
            for key in ("history_videos_checked", "max_history_source_similarity_percent", "max_history_rendered_similarity_percent"):
                result[key] = final_report[key]
        result["shuffle_cut_policy"] = "fixed-duration-hard-cuts-no-natural-boundaries" if fixed_output_cuts else "legacy-fixed-pieces-with-tails"
        if final_report.get("dongge_tier1_dedup"):
            result["dongge_tier1_dedup"] = final_report["dongge_tier1_dedup"]
        if bounded_core_timeline:
            result["duration_policy"] = duration_policy
            result["core_timeline_scope"] = "body_excluding_opening"
            result["core_audio_end_policy"] = "stop_at_video_end_no_loop_or_silence"
            result["core_timeline_basis"] = (
                "rotated_selected_picture_duration" if rotate_cores_mode
                else "first_selected_picture_duration" if selected_primary_first
                else "first_source_full_duration"
            )
        if random_mode:
            result["shuffle_budget_seconds"] = manifests[0].get("shuffle_duration_seconds") if full_core_audio_once else shuffle_budget_seconds
            result["core_audio_policy"] = core_audio_policy
            result["core_audio_covers_body"] = core_audio_covers_body
            result["core_range_scope"] = (
                "per_picture_core" if rotate_cores_mode
                else "first_picture_only" if first_core_picture_trim
                else "per_picture_core" if has_picture_core_modes and core_ranges
                else "not_used" if explicit_display_modes
                else "picture_only" if full_core_audio_once
                else "picture_and_audio"
            )
            result["core_display_modes"] = core_display_modes if full_core_audio_once else None
            if has_picture_core_modes and core_ranges:
                result["core_ranges"] = core_ranges
            if first_core_picture_trim:
                result["core_ranges"] = core_ranges
                result["primary_shuffle_budget_seconds"] = primary_shuffle_budget_seconds
                result["primary_shuffle_used_duration_seconds"] = manifests[0].get("primary_shuffle_used_duration_seconds")
                result["primary_shuffle_effective_budget_seconds"] = manifests[0].get("primary_shuffle_effective_budget_seconds")
            if full_core_audio_once:
                for field in ("full_core_picture_hole_duration_seconds", "shuffle_duration_seconds", "core_fallback_duration_seconds"):
                    result[field] = manifests[0].get(field)
                if explicit_display_modes:
                    for field in ("core_display_mode_details", "shuffle_repeated", "shuffle_repeat_cycles", "solid_background_duration_seconds", "black_picture_duration_seconds"):
                        result[field] = manifests[0].get(field)
        result["audio_exported"] = not random_mode
        if not random_mode:
            result["audio_outputs"] = [
                {"index": index, "name": Path(manifest["audio_path"]).name,
                 "url": f"/queue/audio/{run_id}/{index}",
                 "duration_seconds": accepted_durations[index - 1]}
                for index, manifest in enumerate(manifests, 1)
                if manifest.get("audio_path") and Path(manifest["audio_path"]).is_file()
            ]
        store = app.get_task_queue_store(create=False)
        record = app.retry_queue_store_operation(lambda: store.get(run_id), run_id, "读取原声剪辑队列状态") if store is not None else None
        if record is not None:
            if not app.mark_queue_task_finalizing(run_id, result, "not_required"):
                checkpoint()
                raise RuntimeError("无法保存原声成品的队列状态")
            finalizing = True
        update(returncode=0, status="succeeded", progress=100, stage=f"{mode_label}制作完成")
        if visual_batch is not None:
            log(f"-> {mode_label}制作完成；已复核 {len(manifests)} 条 MP4 的打乱画面。")
        else:
            log(f"-> {mode_label}制作完成；未执行价格消音、字幕改写或相似度筛选。")
        return result
    except Exception as exc:
        if fill_session is not None and not finalizing:
            committed = fill_session.verified_snapshot()
            accepted_paths = committed.get("accepted_paths", [])
            accepted_durations = committed.get("accepted_durations", [])
            manifests = manifests[:len(accepted_paths)]
        skipped = isinstance(exc, app.QueueTaskSkipped) or (
            isinstance(exc, FillCheckpointError) and app.queue_skip_requested(run_id)
        )
        message = app.redact_local_paths(exc)
        if finalizing and not skipped:
            update(returncode=0, status="review_required", progress=100, stage="成片已保留，队列收尾状态待核对")
        else:
            if multi and accepted_paths and not skipped:
                try:
                    publish_delivery_state(False, message)
                except Exception as report_exc:
                    log(f"-> 已完成 MP4 保留在任务目录；报告写入失败：{app.redact_local_paths(report_exc)}")
            update(returncode=-2, status="skipped" if skipped else "failed",
                   stage=("本条已停止，队列继续下一条" if skipped else f"制作失败：{message}"))
        log(f"-> {'本条已停止' if skipped else '原声剪辑失败'}：{message}")
        return None
    finally:
        if fill_session is not None:
            fill_session.close()
        if reserved:
            app.release_renderer_disk_reservation(run_id)
        update(running=False, finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        log(f"V16_ORIGINAL_AUDIO_BENCHMARK {json.dumps({'wall_seconds': round(time.monotonic() - started, 3), 'completed': len(accepted_paths), 'requested': count}, ensure_ascii=False)}")
