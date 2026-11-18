import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TEST_DATA_DIR = tempfile.TemporaryDirectory()
os.environ.setdefault("V16_DATA_DIR", _TEST_DATA_DIR.name)
os.environ.setdefault("V16_LOCAL_NO_VALIDATION", "1")
os.environ.setdefault("V17_TENANT_BACKEND", "0")
os.environ.setdefault("V17_QUEUE_WORKERS", "1")
os.environ.setdefault("V16_ERP_DIRECT_ENABLED", "0")
import bottom_overlay


class DonggeTimelineTests(unittest.TestCase):
    def test_new_and_preexisting_presets_keep_their_timeline(self):
        from v16_web_app import normalize_task_parameter_preset_settings as normalize

        preset = {"edition_mode": "dongge"}
        self.assertEqual(normalize(preset)["dongge_timeline_mode"], "core_audio")
        self.assertEqual(normalize(preset, stored=True)["dongge_timeline_mode"], "core_audio")
        self.assertEqual(normalize(preset)["batch_picture_policy"], "rotate_cores")
        self.assertEqual(normalize(dict(preset, fission_count=3))["batch_picture_policy"], "rotate_cores")
        self.assertEqual(
            normalize(dict(preset, fission_count=1, batch_picture_policy="first_core_then_shuffle"))["batch_picture_policy"],
            "rotate_cores",
        )
        for mode in ("native", "core_audio"):
            stored = normalize(dict(preset, dongge_timeline_mode=mode))
            self.assertEqual(stored["dongge_timeline_mode"], mode)
            self.assertEqual(normalize(stored, stored=True)["dongge_timeline_mode"], mode)
        for policy in ("rotate_cores", "per_core", "first_core_then_shuffle"):
            batch_preset = normalize(dict(preset, fission_count=3, batch_picture_policy=policy))
            self.assertEqual(batch_preset["batch_picture_policy"], policy)
            self.assertEqual(normalize(batch_preset, stored=True)["batch_picture_policy"], policy)

    def test_apply_submitted_core_order_reorders_matching_filenames(self):
        from v16_web_app import _apply_submitted_core_order

        materials = {"核心素材": ["a.mp4", "b.mp4", "c.mp4"], "打乱素材池": ["s.mp4"]}
        _apply_submitted_core_order(materials, json.dumps(["c.mp4", "a.mp4", "b.mp4"]))
        self.assertEqual(materials["核心素材"], ["c.mp4", "a.mp4", "b.mp4"])

        # Mismatched set or invalid JSON leaves existing order untouched.
        _apply_submitted_core_order(materials, json.dumps(["c.mp4", "a.mp4", "other.mp4"]))
        self.assertEqual(materials["核心素材"], ["c.mp4", "a.mp4", "b.mp4"])
        _apply_submitted_core_order(materials, "not-json")
        self.assertEqual(materials["核心素材"], ["c.mp4", "a.mp4", "b.mp4"])

    def test_dongge_ui_defaults_and_pools_quick_bar_render_cleanly(self):
        import v16_web_app as web

        with patch.object(web, "ensure_task_queue_worker", return_value=False):
            resp = web.app.test_client().get("/")
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
        self.assertIn('id="pools-edition-bar"', html)
        self.assertIn('class="dongge-settings in-pools"', html)
        self.assertIn('data-sync-for="dongge-timeline-mode"', html)
        self.assertIn('data-sync-for="dongge-cut"', html)
        self.assertIn('data-sync-for="batch-picture-policy"', html)
        self.assertIn('id="core-materials"', html)
        self.assertIn('id="duration-mode-title"', html)
        self.assertIn('id="duration-shuffle-budget-box"', html)
        self.assertIn('<option value="core_audio" selected>首条核心裁剪＋连续原声（默认）</option>', html)
        self.assertIn('<option value="rotate_cores" selected>多条画面核心自动轮换（5条成片自动换核心 · 默认）</option>', html)
        self.assertIn('<option value="per_core">单条成片内按序保留全部核心画面</option>', html)
        self.assertIn('<option value="first_core_then_shuffle">固定只用第1条核心画面，其余仅作原声</option>', html)
        self.assertGreaterEqual(html.count("data-dongge-rotation-preview"), 3)
        self.assertLess(
            html.index('id="task-dongge-timeline-mode"'),
            html.index('class="task-pool-grid"'),
        )

    def test_normalize_core_display_modes_and_all_core_ranges(self):
        import v16_web_app as web

        materials = {"核心素材": ["c1.mp4", "c2.mp4", "c3.mp4"]}
        modes = web.normalize_selected_core_display_modes(
            materials,
            json.dumps(["video_only", "audio_only", "video_audio"]),
            batch_picture_policy="rotate_cores",
        )
        self.assertEqual(modes, ["video_only", "audio_only", "video_audio"])

        with self.assertRaisesRegex(ValueError, "主画面"):
            web.normalize_selected_core_display_modes(
                materials, ["audio_only", "audio_only", "audio_only"], "rotate_cores"
            )
        with self.assertRaisesRegex(ValueError, "提供原声"):
            web.normalize_selected_core_display_modes(
                materials, ["video_only", "video_only", "video_only"], "rotate_cores"
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "核心素材").mkdir()
            for name in materials["核心素材"]:
                (root / "核心素材" / name).write_bytes(b"fake")
            with patch.object(web, "_media_duration", return_value=30.0):
                ranges = web.normalize_selected_core_ranges(
                    materials,
                    json.dumps([[1.5, 12.0], None, [3.0, 20.5]]),
                    root,
                )
            self.assertEqual(ranges, [[1.5, 12.0], [0.0, 30.0], [3.0, 20.5]])

    def test_dongge_submission_rotate_cores_audio_gate_and_options(self):
        import v16_web_app as web

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "核心素材").mkdir()
            (root / "打乱素材池").mkdir()
            core_names = [f"core_{i}.mp4" for i in range(1, 6)]
            for name in core_names:
                (root / "核心素材" / name).write_bytes(b"fake")
            (root / "打乱素材池" / "shuf.mp4").write_bytes(b"fake")

            materials = {"核心素材": core_names, "打乱素材池": ["shuf.mp4"], "开头视频": []}
            # 5 cores * 36s = 180s (3 min) total audio; 30s crop * 10x = 300s (5 min) -> MUST block!
            with patch.object(web, "_media_duration", return_value=36.0), patch.object(
                web, "_media_audio_duration", return_value=36.0
            ):
                with self.assertRaisesRegex(ValueError, "不能制作（核心原声不足）"):
                    web.dongge_submission(
                        {
                            "cut_seconds": "3",
                            "batch_picture_policy": "rotate_cores",
                            "core_ranges": json.dumps([[0, 30.0]] * 5),
                            "core_display_modes": json.dumps(["video_audio"] * 5),
                        },
                        materials,
                        root,
                        multiplier=10.0,
                        count=5,
                        run_id="run-gate-test",
                    )

                # Variant 3 has 35s crop -> 35 * 6 = 210s > 180s audio -> blocks Variant 3!
                with self.assertRaisesRegex(
                    ValueError,
                    r"第 3 条成片目标需 210\.00 秒，可用连续原声共 180\.00 秒（差 30\.00 秒）",
                ):
                    web.dongge_submission(
                        {
                            "cut_seconds": "3",
                            "batch_picture_policy": "rotate_cores",
                            "core_ranges": json.dumps([[0, 20], [0, 20], [0, 35], [0, 20], [0, 20]]),
                            "core_display_modes": json.dumps(["video_audio"] * 5),
                        },
                        materials,
                        root,
                        multiplier=6.0,
                        count=5,
                        run_id="run-var3-gate",
                    )

                # Valid 5-variant rotation with mixed modes (video_audio, audio_only, video_only)
                fake_plan = {
                    "shuffle_duration_seconds": 40.0,
                    "output_duration_seconds": 60.0,
                    "core_kept_picture_duration_seconds": 20.0,
                    "opening_duration_seconds": 0.0,
                }
                with patch("audio_preserving_edit.plan_edit", return_value=fake_plan) as mock_plan:
                    budget, opts = web.dongge_submission(
                        {
                            "cut_seconds": "3",
                            "batch_picture_policy": "rotate_cores",
                            "core_ranges": json.dumps([[1, 21], None, [2, 22], [0, 18], [5, 25]]),
                            "core_display_modes": json.dumps(
                                ["video_audio", "audio_only", "video_only", "video_audio", "video_audio"]
                            ),
                        },
                        materials,
                        root,
                        multiplier=3.0,
                        count=5,
                        run_id="run-ok-test",
                    )
                self.assertEqual(opts["batch_picture_policy"], "rotate_cores")
                self.assertEqual(opts["duration_multiplier"], 3.0)
                self.assertEqual(
                    opts["core_display_modes"],
                    ["video_audio", "audio_only", "video_only", "video_audio", "video_audio"],
                )
                self.assertEqual(
                    opts["core_ranges"],
                    [[1.0, 21.0], [0.0, 36.0], [2.0, 22.0], [0.0, 18.0], [5.0, 25.0]],
                )
                # 4 audio cores * 36s = 144s full sum; Variant 4 leads with core_5 (start=5.0s) -> min usable = 139.0s
                self.assertAlmostEqual(budget["core_audio_full_duration"], 144.0)
                self.assertAlmostEqual(budget["core_audio_available_duration"], 139.0)
                self.assertTrue(mock_plan.called)

    def test_dongge_submission_deducts_lead_core_start_offset_from_usable_audio(self):
        import v16_web_app as web

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "核心素材").mkdir()
            (root / "打乱素材池").mkdir()
            for name in ("core_1.mp4", "core_2.mp4"):
                (root / "核心素材" / name).write_bytes(b"fake")
            (root / "打乱素材池" / "shuf.mp4").write_bytes(b"fake")

            materials = {"核心素材": ["core_1.mp4", "core_2.mp4"], "打乱素材池": ["shuf.mp4"], "开头视频": []}
            fake_plan = {
                "shuffle_duration_seconds": 4.0,
                "output_duration_seconds": 6.0,
                "core_kept_picture_duration_seconds": 2.0,
                "opening_duration_seconds": 0.0,
            }
            with patch.object(web, "_media_duration", return_value=4.0), patch.object(
                web, "_media_audio_duration", return_value=4.0
            ), patch("audio_preserving_edit.plan_edit", return_value=fake_plan):
                # Core 1 (4.0s audio) cropped [1.5, 3.5] (2.0s basis) + Core 2 (4.0s audio, unset):
                # Variant 1 usable continuous audio = (4.0 - 1.5) + 4.0 = 6.5s.
                # At multiplier = 3.5 -> target_1 = 7.0s > 6.5s -> MUST block with ValueError!
                for policy in ("rotate_cores", "per_core"):
                    with self.assertRaisesRegex(
                        ValueError,
                        r"不能制作（核心原声不足）：第 1 条成片目标需 7\.00 秒，可用连续原声共 6\.50 秒（差 0\.50 秒）",
                    ):
                        web.dongge_submission(
                            {
                                "cut_seconds": "3",
                                "batch_picture_policy": policy,
                                "core_ranges": json.dumps([[1.5, 3.5], None]),
                                "core_display_modes": json.dumps(["video_audio", "video_audio"]),
                            },
                            materials,
                            root,
                            multiplier=3.5,
                            count=1,
                            run_id=f"run-start-offset-block-{policy}",
                        )

                # At multiplier = 3.0 -> target_1 = 6.0s <= 6.5s -> MUST pass!
                budget, opts = web.dongge_submission(
                    {
                        "cut_seconds": "3",
                        "batch_picture_policy": "rotate_cores",
                        "core_ranges": json.dumps([[1.5, 3.5], None]),
                        "core_display_modes": json.dumps(["video_audio", "video_audio"]),
                    },
                    materials,
                    root,
                    multiplier=3.0,
                    count=1,
                    run_id="run-start-offset-pass",
                )
                self.assertAlmostEqual(budget["core_audio_available_duration"], 6.5)
                self.assertAlmostEqual(budget["core_audio_full_duration"], 8.0)
                self.assertEqual(opts["core_ranges"], [[1.5, 3.5], [0.0, 4.0]])

            # If Core 1's video is 4.0s but its audio stream is only 1.2s, start=1.5s exceeds audio stream -> block!
            with patch.object(web, "_media_duration", return_value=4.0), patch.object(
                web, "_media_audio_duration", side_effect=lambda p: 1.2 if p.name == "core_1.mp4" else 4.0
            ):
                with self.assertRaisesRegex(ValueError, "所选起点已超出原声音轨长度"):
                    web.dongge_submission(
                        {
                            "cut_seconds": "3",
                            "batch_picture_policy": "rotate_cores",
                            "core_ranges": json.dumps([[1.5, 3.5], None]),
                            "core_display_modes": json.dumps(["video_audio", "video_audio"]),
                        },
                        materials,
                        root,
                        multiplier=1.5,
                        count=1,
                        run_id="run-start-beyond-audio",
                    )

    def test_preset_preserves_dongge_shuffle_masks_and_styles(self):
        from v16_web_app import normalize_task_parameter_preset_settings as normalize

        preset = {
            "edition_mode": "dongge",
            "dongge_timeline_mode": "native",
            "dongge_shuffle_masks": {
                "enabled": True,
                "style": "delogo",
                "strength": 64,
                "boxes": [[0.1, 0.72, 0.9, 0.84]],
            },
        }
        normalized = normalize(preset)
        masks = normalized.get("dongge_shuffle_masks")
        self.assertIsInstance(masks, dict)
        self.assertTrue(masks["enabled"])
        self.assertEqual(masks["style"], "delogo")
        self.assertEqual(masks["strength"], 64)
        self.assertEqual(len(masks["boxes"]), 1)

    def test_shuffled_clips_use_the_finished_movie_clock_and_skip_core(self):
        layer = {"start": 2.0, "end": 3.0, "path": "prepared.png"}
        paths = ["core-A", "shuffle-A", "core-B", "shuffle-B"]
        graph = []
        inputs = []
        with patch.object(bottom_overlay, "prepare_layers", return_value=[layer]):
            labels = bottom_overlay.shuffle_concat_labels(
                inputs, graph, paths, [1.0] * 4, {"shuffle-A", "shuffle-B"},
                [layer], 1.0, ".", 720, 1280,
                masks={"strength": 50, "boxes": [[0.2, 0.5, 0.8, 0.7]]},
            )
        self.assertEqual(labels[::2], ["[v0]", "[v2]"])
        self.assertEqual(inputs, ["-i", "prepared.png"])
        self.assertIn("layer", labels[1])
        self.assertIn("mask", labels[3])
        self.assertNotIn("layer", labels[3])

    def test_resolve_shuffle_timeline_accumulates_multiple_leading_cores(self):
        paths = ["core-A", "core-B", "shuffle-A", "core-C", "shuffle-B"]
        durations = [2.5, 3.5, 3.0, 2.0, 4.0]
        info = bottom_overlay.resolve_shuffle_timeline(
            paths, durations, {"shuffle-A", "shuffle-B"}, opening=1.5
        )
        self.assertAlmostEqual(info["first_core_end"], 7.5)
        self.assertEqual(info["shuffle_intervals"], [(6.0, 9.0), (11.0, 15.0)])

    def test_post_zoom_bottom_layers_only_enable_on_shuffle_intervals(self):
        with tempfile.TemporaryDirectory() as tmp:
            img_path = Path(tmp) / "src.png"
            Image.new("RGBA", (200, 120), (255, 128, 0, 255)).save(img_path)
            layers = [
                {
                    "path": str(img_path),
                    "anchor": "first_core_end",
                    "duration": 6.0,
                    "opacity": 30.0,
                    "size_percent": 100.0,
                }
            ]
            cmd = []
            filter_parts = []
            # base_concat has core [0..4], shuffle [4..7], core [7..9], shuffle [9..12]; opening = 1.0
            # first_core_end = 1.0 + 4.0 = 5.0 -> movie window [5.0, 11.0] -> local [4.0, 10.0]
            # Intersected with shuffle [(4,7), (9,12)] => [(4,7), (9,10)]
            v_out, inputs_count = bottom_overlay.append_post_zoom_bottom_layers(
                cmd, filter_parts, "[v_zoomed]", 1, layers,
                [(4.0, 7.0), (9.0, 12.0)], 1.0, tmp, 720, 1280, base_duration=12.0,
            )
            self.assertEqual(inputs_count, 2)
            self.assertEqual(v_out, "[v_dongge_btm_0]")
            self.assertEqual(len(filter_parts), 1)
            self.assertIn("gte(t,4.000000)*lt(t,7.000000)+gte(t,9.000000)*lt(t,10.000000)", filter_parts[0])

    def test_read_shuffle_masks_supports_blur_delogo_and_dark(self):
        blur = bottom_overlay.read_shuffle_masks(json.dumps({"strength": 55, "boxes": [[0.1, 0.7, 0.9, 0.85]]}))
        self.assertNotIn("style", blur)
        for style in ("delogo", "dark"):
            parsed = bottom_overlay.read_shuffle_masks(
                json.dumps({"strength": 60, "style": style, "boxes": [[0.1, 0.7, 0.9, 0.85]]})
            )
            self.assertEqual(parsed["style"], style)
            graph = []
            out_label = bottom_overlay._blur_boxes(graph, "base", parsed, 720, 1280)
            self.assertIn("mask", out_label)
            self.assertTrue(any(style if style == "delogo" else "drawbox" in item for item in graph))

    def test_choose_window_order_303_windows_5x40_zero_reuse_and_zero_adjacent_same_source(self):
        import random
        from shuffle_order import choose_window_order

        # Test both balanced 6-source pool (303 windows) and dominant-mother 5-source pool (303 windows)
        distributions = [
            [55, 52, 51, 50, 48, 47],
            [145, 42, 40, 39, 37],
        ]
        for dist_idx, counts in enumerate(distributions):
            self.assertEqual(sum(counts), 303)
            windows = []
            for src_i, cnt in enumerate(counts):
                src_path = f"/virtual/broll_{src_i:02d}.mp4"
                for w_i in range(cnt):
                    windows.append({
                        "source_path": src_path,
                        "source_identity": src_path,
                        "start": round(w_i * 3.0, 3),
                    })
            self.assertEqual(len(windows), 303)

            rng = random.Random(f"tier1-unit-seed-{dist_idx}")
            prior_rows = []
            all_used = []
            for variant in range(5):
                slot_durs = [75 + ((variant * 7 + k * 5) % 25) for k in range(40)]
                row = choose_window_order(
                    windows,
                    40,
                    rng,
                    previous=prior_rows,
                    slot_durations=slot_durs,
                )
                self.assertEqual(len(row), 40)
                self.assertEqual(len(set(row)), 40, "No within-variant duplicate cuts allowed")
                adj_same_src = sum(
                    1
                    for a, b in zip(row, row[1:])
                    if windows[a]["source_path"] == windows[b]["source_path"]
                )
                self.assertEqual(
                    adj_same_src,
                    0,
                    f"Distribution {dist_idx} variant {variant + 1} had adjacent same-source cuts",
                )
                prior_rows.append(row)
                all_used.extend(row)

            self.assertEqual(len(all_used), 200)
            self.assertEqual(
                len(set(all_used)),
                200,
                f"Expected 0 cross-variant window reuse out of 303 windows (dist {dist_idx})",
            )

    def test_plan_edit_rotate_cores_5_variants_staggered_durations_jittered_cuts_and_untouched_core(self):
        import hashlib
        import audio_preserving_edit as ape

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            core_paths = []
            shuffle_paths = []
            for i in range(1, 5):
                p = root / f"core_{i}.mp4"
                p.write_bytes(f"core-content-{i}".encode("utf-8") * (i + 1))
                core_paths.append(p)
            for i in range(1, 7):
                p = root / f"shuf_{i}.mp4"
                p.write_bytes(f"shuffle-content-{i}".encode("utf-8") * (i + 2))
                shuffle_paths.append(p)

            def fake_probe(path, _ffprobe):
                p_str = str(path)
                is_core = "core_" in Path(p_str).name
                dur = 25.0 if is_core else 36.0
                return {
                    "source_path": p_str,
                    "duration_seconds": dur,
                    "video_duration_seconds": dur,
                    "audio_duration_seconds": dur,
                    "container_duration_seconds": dur,
                    "has_audio": True,
                    "audio_channels": 2,
                    "video_stream_index": 0,
                    "audio_stream_index": 1,
                    "width": 720,
                    "height": 1280,
                }

            run_id = "8899aabbccddeeff0011223344556677"
            plans = []
            with patch.object(ape, "_probe", side_effect=fake_probe):
                for v in range(1, 6):
                    seed = int.from_bytes(hashlib.sha256(f"{run_id}:{v}".encode("utf-8")).digest()[:8], "big")
                    plan = ape.plan_edit(
                        core_paths,
                        shuffle_paths,
                        opening_path=None,
                        cut_seconds=3.0,
                        width=720,
                        height=1280,
                        fps=30,
                        seed=seed,
                        ffprobe="ffprobe",
                        visual_policy="full_core_timeline",
                        core_audio_policy="full_core_once",
                        batch_picture_policy="rotate_cores",
                        core_display_modes=["video_audio"] * 4,
                        core_ranges=[[1.0, 5.0]] * 4,
                        duration_multiplier=3.5,
                        variant_index=v,
                        fixed_shuffle_cuts=True,
                        unique_batch=(run_id, v - 1, 5),
                    )
                    plans.append(plan)

        # 1) All 5 output durations are distinct and staggered around base target (4.0 * 3.5 = 14.0s)
        durations = [p["output_duration_seconds"] for p in plans]
        self.assertEqual(len(set(durations)), 5, f"Expected 5 distinct durations, got {durations}")
        for d in durations:
            self.assertGreaterEqual(d, 14.0 * 0.80 - 0.05)
            self.assertLessEqual(d, 14.0 * 1.20 + 0.05)
        self.assertTrue(any(d < 14.0 for d in durations) and any(d > 14.0 for d in durations))

        # 2) Opening core selection_frames (120 frames = 4.0s from 1.0..5.0s) is 100% untouched across all 5 variants
        for v_idx, plan in enumerate(plans, 1):
            core_shots = [s for s in plan["shots"] if s["role"] == "core"]
            self.assertEqual(len(core_shots), 1)
            self.assertEqual(core_shots[0]["frame_count"], 120, f"Variant {v_idx} modified core selection_frames")
            self.assertAlmostEqual(core_shots[0]["source_start_seconds"], 1.0)
            self.assertAlmostEqual(core_shots[0]["source_end_seconds"], 5.0)
            self.assertEqual(core_shots[0]["picture_fit"], "contain")

            # 3) B-roll cut durations are jittered (non-constant)
            shuf_shots = [
                s for s in plan["shots"]
                if s["role"] == "shuffle" and s.get("fixed_cut_part_offset_frames", 0) == 0
            ]
            cut_frame_lengths = [s.get("fixed_cut_group_frame_count", s["frame_count"]) for s in shuf_shots]
            self.assertGreaterEqual(len(shuf_shots), 3)
            self.assertGreater(
                len(set(cut_frame_lengths)),
                1,
                f"Variant {v_idx} cut durations were constant: {cut_frame_lengths}",
            )

            # 4) dongge_tier1_dedup contract is populated
            dedup = plan.get("dongge_tier1_dedup")
            self.assertIsInstance(dedup, dict)
            self.assertTrue(dedup["enabled"])
            self.assertEqual(dedup["version"], "tier1-9items-v1")
            self.assertEqual(dedup["variant_index"], v_idx)
            self.assertEqual(dedup["adjacent_same_source_cuts"], 0)
            self.assertTrue(0.05 <= dedup["screen_opacity"] <= 0.15)
            self.assertTrue(2 <= dedup["grain_strength"] <= 7)
            self.assertNotAlmostEqual(dedup["tail_audio_tempo"], 1.0, places=3)

        # Variant 1 and Variant 5 (both wrap around to Core #1) must differ in seed, tempo, and duration
        self.assertNotEqual(plans[0]["dongge_tier1_dedup"]["variant_seed"], plans[4]["dongge_tier1_dedup"]["variant_seed"])
        self.assertNotEqual(plans[0]["dongge_tier1_dedup"]["tail_audio_tempo"], plans[4]["dongge_tier1_dedup"]["tail_audio_tempo"])

    def test_delivery_video_name_randomized_hex_suffixes_preserve_internal_names(self):
        import re
        import v16_web_app as web
        from task_queue_store import _required_delivery_video_keys

        run_a = "11223344556677889900aabbccddeeff"
        run_b = "ffeeddccbbaa00998877665544332211"

        names_a = [web.delivery_video_name("爆款测试", run_a, index=i) for i in range(1, 6)]
        suffixes_a = []
        for idx, name in enumerate(names_a, 1):
            m = re.fullmatch(rf"爆款测试__11223344_第0{idx}条_([0-9a-f]{{6}})\.mp4", name)
            self.assertIsNotNone(m, f"Unexpected delivery filename format: {name}")
            suffixes_a.append(m.group(1))
        self.assertEqual(len(set(suffixes_a)), 5, "Each variant in a batch must get a distinct 6-hex suffix")

        # Deterministic per (run_id, index), distinct across different run_ids
        self.assertEqual(names_a[0], web.delivery_video_name("爆款测试", run_a, index=1))
        self.assertNotEqual(names_a[0], web.delivery_video_name("爆款测试", run_b, index=1))
        single_name = web.delivery_video_name("单条测试", run_a, index=None)
        self.assertRegex(single_name, r"^单条测试__11223344_[0-9a-f]{6}\.mp4$")

        # Internal immutable MP4 filenames remain V17裂变成片_第XX条.mp4 for retention/checkpoint contracts
        fission_paths = [Path(f"/tmp/V17裂变成片_第0{i}条.mp4") for i in range(1, 6)]
        q_res = web.queue_result_for_outputs(run_a, 5, Path("/tmp/V17排队成片.mp4"), fission_paths, [12.0] * 5)
        internal_names = [item["name"] for item in q_res["outputs"]]
        self.assertEqual(internal_names, [f"V17裂变成片_第0{i}条.mp4" for i in range(1, 6)])
        self.assertEqual(
            _required_delivery_video_keys(q_res),
            tuple(f"V17裂变成片_第0{i}条.mp4" for i in range(1, 6)),
        )


if __name__ == "__main__":
    unittest.main()
