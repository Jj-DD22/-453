"""Compare and distribute complete B-roll shots for first-core-then-shuffle edits."""
from functools import lru_cache
import math

import numpy as np

from shuffle_scene import _near_frames


def _ordered_matches(pairs, required):
    """Length of the best order-preserving match; no picture is counted twice."""
    row = np.zeros(pairs.shape[1] + 1, dtype=np.int32)
    for index, matches in enumerate(pairs):
        np.maximum.accumulate(np.maximum(row[1:], row[:-1] + matches), out=row[1:])
        if row[-1] >= required:
            return True
        if row[-1] + len(pairs) - index - 1 < required:
            return False
    return False


def repeated_whole_shots(candidates, reference):
    """Return one match flag per cut. A shortened tail is compared as an entire tail.

    Keep one orientation and chronological order through at least 80% of the
    shorter shot. Bound the matching interval to allow modest retiming without
    accepting scattered common pictures from a much longer shot.
    """
    count, length = candidates.shape[:2]
    other = len(reference)
    if not min(length, other):
        raise ValueError("打乱镜头缺少可比较的画面。")
    short = min(length, other)
    required = math.ceil(4 * short / 5)
    window = min(max(length, other), math.ceil(1.25 * short))
    found = np.zeros(count, dtype=bool)
    chunk = max(1, 100_000 // (length * other))
    for start in range(0, count, chunk):
        batch = candidates[start:start+chunk]
        near = _near_frames(batch, reference)
        # Hashing mirrored images can round differently. Use both equivalent
        # comparisons, so swapping the two input cuts gives the same verdict.
        near |= _near_frames(batch[:, :, ::-1], reference[:, ::-1])
        possible = (near.any(axis=1).sum(axis=1) >= required) & (near.any(axis=2).sum(axis=1) >= required)
        for output, direction in np.argwhere(possible):
            if found[start + output]:
                continue
            grid = near[output, :, :, direction]
            if length > other:
                grid = grid.T
            for offset in range(grid.shape[1] - window + 1):
                if _ordered_matches(grid[:, offset:offset+window], required):
                    found[start + output] = True
                    break
    return found


class WholeShotAllocator:
    """Allocate one batch without resampling the same source time or complete shot."""

    def __init__(self, ids, related, lengths, layouts, signatures, visible=None, *, starts=None, visible_by_fraction=None):
        self.ids = ids
        self.related = related
        self.lengths = lengths
        self.signatures = signatures
        self.visible = visible
        self.visible_by_fraction = visible_by_fraction or {}
        self.rendered_conflicts = {}
        self.layout = {n: values[:, :, 0].mean(axis=1) for n, values in layouts.items()}
        self.time_ranks = None
        if starts is not None:
            starts = np.asarray(starts)
            self.time_ranks = np.empty(len(ids), dtype=int)
            for source in np.unique(ids):
                pieces = np.flatnonzero(ids == source)
                self.time_ranks[pieces[np.argsort(starts[pieces], kind="stable")]] = np.arange(len(pieces))

        @lru_cache(maxsize=512)
        def conflict_mask(candidate_length, reference_length, reference_index):
            reference = signatures[reference_length][reference_index]
            flags = repeated_whole_shots(signatures[candidate_length], reference)
            if self.visible:
                flags |= repeated_whole_shots(self.visible[candidate_length], self.visible[reference_length][reference_index])
            if self.visible_by_fraction:
                top = min(candidate_length[1], reference_length[1])
                if top < 1.:
                    views = self.visible_by_fraction[top]
                    flags |= repeated_whole_shots(views[candidate_length[0]], views[reference_length[0]][reference_index])
            return flags

        self.conflict_mask = conflict_mask

    def remember_conflicts(self, rows, positions):
        """Keep only the conflicting pair of actual source windows and cut lengths."""
        for (v, slot), (other_v, other_slot) in positions:
            first = (self.lengths[slot], rows[v][slot])
            second = (self.lengths[other_slot], rows[other_v][other_slot])
            self.rendered_conflicts.setdefault(first, set()).add(second)
            self.rendered_conflicts.setdefault(second, set()).add(first)

    def allocate(self, count, rng, fixed=(), tried=(), *, allowed=None):
        ids, related = self.ids, self.related
        size = len(ids)
        rows = [list(row) for row in fixed] + [[] for _ in range(count-len(fixed))]
        available = np.ones(size, dtype=bool)
        conflicts = {n: np.zeros(size, dtype=bool) for n in self.signatures}
        use = np.zeros((count, len(related)), dtype=int)
        closest = {n: np.full((count, size), 5., dtype=np.float32) for n in self.signatures}
        slot_sources = np.zeros((len(self.lengths), len(related)), dtype=int)
        discouraged = {piece for row in tried for piece in row}
        transition_owners = {}

        def reserve(v, slot, piece):
            n = self.lengths[slot]
            available[piece] = False
            use[v, ids[piece]] += 1
            slot_sources[slot, ids[piece]] += 1
            if slot:
                edge = (ids[rows[v][slot - 1]], ids[piece])
                transition_owners[edge] = transition_owners.get(edge, 0) | (1 << v)
            for length, other in self.rendered_conflicts.get((n, piece), ()):
                conflicts[length][other] = True
            sample = self.layout[n][piece]
            for length, layouts in self.layout.items():
                conflicts[length] |= self.conflict_mask(length, n, piece)
                delta = layouts - sample
                distance = np.einsum('ij,ij->i', delta, delta)
                np.minimum(closest[length][v], distance, out=closest[length][v])

        for v, row in enumerate(fixed):
            for slot, piece in enumerate(row):
                reserve(v, slot, piece)
        variants = list(range(len(fixed), count))
        if not variants:
            return rows
        # Give each output a different first choice of source files. Balance
        # by remaining usable windows: one long source can feed at most one
        # whole output before another variant needs first choice of its own.
        longest = max(self.lengths, default=0)
        if not longest:
            return rows
        stock = available & ~conflicts[longest]
        if allowed is not None:
            stock &= allowed[longest]
        capacity = np.bincount(ids[stock], minlength=len(related))
        source_order = np.flatnonzero(capacity).tolist()
        rng.shuffle(source_order)
        rng.shuffle(variants)
        owners = np.full(len(related), -1, dtype=int)
        loads = np.zeros(count, dtype=int)
        for source in sorted(source_order, key=lambda src: -capacity[src]):
            owner = min(variants, key=lambda v: loads[v])
            owners[source] = owner
            loads[owner] += min(capacity[source], len(self.lengths))
        for slot, n in enumerate(self.lengths):
            rng.shuffle(variants)
            for v in variants:
                viable = available & ~conflicts[n]
                if allowed is not None:
                    viable &= allowed[n]
                if tried and v == len(fixed) and slot + 1 == len(self.lengths):
                    prefix = tuple(rows[v])
                    for row in tried:
                        if row[:-1] == prefix:
                            viable[row[-1]] = False
                if not viable.any():
                    return None
                score = np.minimum(closest[n][v], 3.) + np.fromiter(
                    (rng.random() for _ in ids), dtype=float, count=size)
                score -= (.6 * np.log1p(use[v]) + .26 * np.log1p(use[v] @ related) +
                          1.8 * (slot_sources[slot] @ related))[ids]
                preferred = viable & (owners[ids] == v)
                source_choices = np.unique(ids[viable])
                if len(source_choices) <= 2:
                    # With two files, always insisting on a switch forces every
                    # video into ABAB or BABA. Draw a fresh preference per slot;
                    # both switching and jumping within one source are valid.
                    source_draw = np.zeros(len(related))
                    for source in source_choices:
                        source_draw[source] = rng.uniform(-3., 3.)
                    score += source_draw[ids]
                    preferred.fill(False)
                if rows[v]:
                    last_piece = rows[v][-1]
                    previous = ids[last_piece]
                    other_variants = ~(1 << v)
                    repeated_edges = np.fromiter(
                        ((transition_owners.get((previous, src), 0) & other_variants).bit_count()
                         for src in range(len(related))), dtype=int)
                    score -= 2.5 * np.minimum(repeated_edges[ids], 1)
                    different = ids != previous
                    if self.time_ranks is not None:
                        neighbours = (~different) & (np.abs(self.time_ranks - self.time_ranks[last_piece]) == 1)
                        score[neighbours] -= 3.
                    if len(source_choices) > 2 and np.unique(ids[preferred]).size != 2:
                        viable &= different
                        preferred &= different
                    else:
                        score[~different] -= .6
                if discouraged and v == len(fixed):
                    score[list(discouraged)] -= 2.
                if preferred.any():
                    viable = preferred
                piece = int(np.argmax(np.where(viable, score, -np.inf)))
                rows[v].append(piece)
                reserve(v, slot, piece)
        return rows
