"""Compare each new B-roll track with earlier exports, before and during registration."""
from shuffle_history import content_id as content_digest
from shuffle_history_legacy import upper_similarity
from shuffle_similarity import frame_hash_similarity_percent


def history_entries(entries):
    checked = []
    for entry in entries:
        if entry.state == "no_shuffle":
            continue
        exact_source = len(entry.source_hashes) == 48
        exact_rendered = len(entry.rendered_hashes) == 48
        bounded = entry.state == "bounded"
        has_source = exact_source or (bounded and len(entry.source_candidates) > 0)
        has_rendered = exact_rendered or (bounded and len(entry.rendered_candidates) > 0)
        if not (has_source and has_rendered):
            raise ValueError(f"历史成片「{entry.label}」缺少可核对的打乱画面记录：{entry.reason}。"
                             "请恢复该任务的成片、镜头记录或原素材后重试。")
        checked.append(entry)
    return checked


class BatchHistory:
    def __init__(self, store, run_id, limit):
        self.store = store
        self.run_id = run_id
        self.limit = limit
        self.entries = history_entries(store.entries(exclude_run_id=run_id))

    def accept(self, path, plan, index, source, rendered, scenes, destination, reject):
        # The SQLite commit re-reads history under its cross-worker write lock.
        # Reuse measurements of immutable videos when nothing has changed.
        windows = []
        for shot in plan["shots"]:
            if shot["role"] != "shuffle":
                continue
            start = shot["source_start_seconds"]
            end = start + shot["frame_count"] / plan["fps"]
            key = content_digest(shot["source_path"])
            if shot.get("fixed_cut_part_offset_frames", 0) and windows and windows[-1][0] == key and abs(windows[-1][2] - start) < .001:
                windows[-1] = (key, windows[-1][1], end)
            else:
                windows.append((key, start, end))
        cached = {}
        results = []
        def validate(entries):
            results.clear()
            for entry in history_entries(entries):
                if entry.key not in cached:
                    bounded = entry.state == "bounded"
                    src = (frame_hash_similarity_percent(entry.source_hashes, source) if len(entry.source_hashes) == 48
                           else upper_similarity(source, entry.source_candidates))
                    out = (frame_hash_similarity_percent(entry.rendered_hashes, rendered) if len(entry.rendered_hashes) == 48
                           else upper_similarity(rendered, entry.rendered_candidates))
                    if max(src, out) > self.limit:
                        bound_note = "（含旧记录的保守估计上界）" if bounded else ""
                        raise reject(f"与历史成片「{entry.label}」打乱原画 {src:.2f}%、成片打乱画面 {out:.2f}%{bound_note}，上限 {self.limit:g}%。")
                    cached[entry.key] = dict(history_id=entry.key, source_similarity_percent=src,
                                             rendered_similarity_percent=out)
                    if bounded:
                        cached[entry.key]["bounded"] = True
                results.append(cached[entry.key])
        validate(self.entries)
        self.store.record(path, plan, run_id=self.run_id, index=index, destination=destination,
                          source_hashes=source, rendered_hashes=rendered, scenes=scenes,
                          windows=windows, validate=validate)
        return dict(history_comparison_policy="track_scores_only", history_videos_checked=len(results), history_pairs=results,
                    max_history_source_similarity_percent=max((p["source_similarity_percent"] for p in results), default=None),
                    max_history_rendered_similarity_percent=max((p["rendered_similarity_percent"] for p in results), default=None))
