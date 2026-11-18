"""Constructive anti-duplication B-roll window ordering across batch variants."""


def _estimate_nominal_step(windows, sources):
    by_src = {}
    for w, src in zip(windows, sources):
        by_src.setdefault(src, []).append(float(w.get("start", 0.0)))
    diffs = []
    for pts in by_src.values():
        pts = sorted(set(pts))
        for a, b in zip(pts, pts[1:]):
            if b - a > 1e-4:
                diffs.append(b - a)
    if not diffs:
        return 3.0
    diffs.sort()
    return diffs[len(diffs) // 2]


def choose_window_order(windows, slots, rng, previous=(), tried=(), slot_durations=None):
    """Constructively interleave mother sources and prioritize zero cross-variant window reuse."""
    size = len(windows)
    if not slots:
        return ()
    if not size:
        raise ValueError("已选打乱素材没有完整的一刀，请缩短刀长或补充打乱素材。")
    sources = [w.get("source_identity") or w["source_path"] for w in windows]
    starts = [float(w.get("start", 0.0)) for w in windows]
    by_source = {}
    for idx, src in enumerate(sources):
        by_source.setdefault(src, []).append(idx)
    source_keys = list(by_source.keys())
    distinct_sources = len(source_keys)
    nominal_step = _estimate_nominal_step(windows, sources)
    adj_gap = nominal_step * 1.35

    used_counts = [0] * size
    prior_sets = []
    for old in previous:
        old_valid = [idx for idx in old if 0 <= idx < size]
        prior_sets.append(set(old_valid))
        for idx in old_valid:
            used_counts[idx] += 1
    prior_used_set = {idx for idx, c in enumerate(used_counts) if c > 0}
    prev_win_at = [
        {old[t] for old in previous if t < len(old) and 0 <= old[t] < size}
        for t in range(slots)
    ]
    prev_src_at = [
        {sources[old[t]] for old in previous if t < len(old) and 0 <= old[t] < size}
        for t in range(slots)
    ]
    tried_set = tried if isinstance(tried, (set, frozenset)) else set(tried)

    def score_row(row):
        adj_same_src = sum(1 for a, b in zip(row, row[1:]) if sources[a] == sources[b])
        exact_adj_rep = sum(1 for a, b in zip(row, row[1:]) if a == b)
        in_row_dup = slots - len(set(row))
        exact_repeats_in_row = exact_adj_rep * (slots + 1) + in_row_dup
        reused_across_prev = sum(1 for idx in row if idx in prior_used_set)
        row_unique = set(row)
        max_pair_overlap = max(
            (sum(1 for idx in row_unique if idx in old_set) for old_set in prior_sets),
            default=0,
        )
        total_prior_uses = sum(used_counts[idx] for idx in row)
        pos_win_max = max(
            (sum(1 for a, b in zip(row, old) if a == b) for old in previous),
            default=0,
        )
        pos_win_sum = sum(sum(1 for a, b in zip(row, old) if a == b) for old in previous)
        pos_src_max = max(
            (sum(1 for a, b in zip(row, old) if sources[a] == sources[b]) for old in previous),
            default=0,
        )
        pos_src_sum = sum(
            sum(1 for a, b in zip(row, old) if sources[a] == sources[b]) for old in previous
        )
        adj_ts = (
            sum(
                1
                for a, b in zip(row, row[1:])
                if sources[a] == sources[b] and abs(starts[a] - starts[b]) <= adj_gap
            )
            if distinct_sources == 1
            else 0
        )
        return (
            adj_same_src,
            exact_repeats_in_row,
            (reused_across_prev, max_pair_overlap, total_prior_uses),
            (pos_win_max, pos_win_sum),
            (pos_src_max, pos_src_sum, adj_ts),
        )

    perfect_score = (0, 0, (0, 0, 0), (0, 0), (0, 0, 0))

    if slots == 1:
        best, best_score = None, None
        for index in rng.sample(range(size), size):
            row = (index,)
            if row in tried_set:
                continue
            sc = score_row(row)
            if best is None or sc < best_score:
                best, best_score = row, sc
                if sc == perfect_score:
                    break
        return best

    def build_constructive_trial():
        row = []
        row_counts = [0] * size
        cur_overlap = [0] * len(prior_sets)
        unused_by_src = {
            s: sum(1 for i in idxs if used_counts[i] == 0)
            for s, idxs in by_source.items()
        }
        fresh_in_row_by_src = {s: len(idxs) for s, idxs in by_source.items()}
        total_unused = sum(unused_by_src.values())
        total_fresh_in_row = size
        win_rand = [rng.random() for _ in range(size)]
        src_bias = {s: rng.uniform(0.0, 0.45) for s in source_keys}

        for t in range(slots):
            rem_slots = slots - t
            prev_win = row[t - 1] if t > 0 else -1
            prev_src = sources[prev_win] if t > 0 else None

            if distinct_sources >= 2:
                recent_2 = sources[row[t - 2]] if t >= 2 else None
                recent_3 = sources[row[t - 3]] if t >= 3 else None

                def src_key(s):
                    same_prev = 1 if (t > 0 and s == prev_src) else 0
                    u_cnt = unused_by_src[s]
                    f_cnt = fresh_in_row_by_src[s]
                    # Dominant-source safeguard: if one mother video holds >=50% of remaining
                    # unused windows (or >=50% of remaining non-repeated row capacity), pick it
                    # whenever it isn't adjacent so non-dominant sources aren't exhausted early.
                    dominant_unused = (
                        total_unused > 0
                        and u_cnt > 0
                        and u_cnt * 2 >= total_unused
                    )
                    dominant_row = (
                        u_cnt == 0
                        and f_cnt > 0
                        and f_cnt * 2 >= min(total_fresh_in_row, rem_slots + 1)
                    )
                    urgent = 0 if (dominant_unused or dominant_row) else 1
                    no_unused = 0 if u_cnt > 0 else 1
                    no_fresh_row = 0 if f_cnt > 0 else 1
                    rec_pen = 0
                    if urgent:
                        if distinct_sources >= 3 and s == recent_2:
                            rec_pen += 2
                        if distinct_sources >= 4 and s == recent_3:
                            rec_pen += 1
                    pos_src_pen = 1 if s in prev_src_at[t] else 0
                    stock_score = -(u_cnt + 0.25 * f_cnt + src_bias[s] + rng.uniform(0.0, 0.35))
                    return (
                        same_prev,
                        no_unused,
                        urgent,
                        no_fresh_row,
                        rec_pen,
                        pos_src_pen,
                        stock_score,
                    )

                chosen_src = min(source_keys, key=src_key)
                pool = by_source[chosen_src]
            else:
                pool = range(size)

            def win_key(i):
                same_exact = 1 if i == prev_win else 0
                in_row = 1 if row_counts[i] > 0 else 0
                adj_ts_pen = (
                    1
                    if (
                        t > 0
                        and sources[i] == prev_src
                        and abs(starts[i] - starts[prev_win]) <= adj_gap
                    )
                    else 0
                )
                was_used_prev = 1 if used_counts[i] > 0 else 0
                max_ov = (
                    max(
                        cur_overlap[p] + (1 if row_counts[i] == 0 and i in prior_sets[p] else 0)
                        for p in range(len(prior_sets))
                    )
                    if prior_sets and was_used_prev
                    else 0
                )
                pos_win_pen = 1 if i in prev_win_at[t] else 0
                dur_tie = 0.0
                if slot_durations is not None and t < len(slot_durations):
                    # Subtle jitter pairing cut length with window index for extra entropy
                    dur_tie = ((i * 31 + int(slot_durations[t])) % 17) * 1e-4
                return (
                    same_exact,
                    in_row,
                    row_counts[i],
                    was_used_prev,
                    adj_ts_pen,
                    max_ov,
                    used_counts[i],
                    pos_win_pen,
                    win_rand[i] + dur_tie,
                )

            picked = min(pool, key=win_key)
            s_picked = sources[picked]
            if row_counts[picked] == 0:
                fresh_in_row_by_src[s_picked] -= 1
                total_fresh_in_row -= 1
                if used_counts[picked] == 0:
                    unused_by_src[s_picked] -= 1
                    total_unused -= 1
                for p, pset in enumerate(prior_sets):
                    if picked in pset:
                        cur_overlap[p] += 1
            row_counts[picked] += 1
            row.append(picked)
        return tuple(row)

    def random_fallback_draw():
        row = []
        while len(row) < slots:
            chunk = rng.sample(range(size), min(size, slots - len(row)))
            if row and len(chunk) > 1 and chunk[0] == row[-1]:
                chunk[0], chunk[1] = chunk[1], chunk[0]
            row.extend(chunk)
        return tuple(row)

    best, best_score = None, None
    seen = set()
    for _ in range(64):
        row = build_constructive_trial()
        if row in seen or row in tried_set:
            continue
        seen.add(row)
        sc = score_row(row)
        if best is None or sc < best_score:
            best, best_score = row, sc
            if sc == perfect_score:
                return best

    if best is None:
        for _ in range(96):
            row = random_fallback_draw()
            if row in seen or row in tried_set:
                continue
            seen.add(row)
            sc = score_row(row)
            if best is None or sc < best_score:
                best, best_score = row, sc
    return best

