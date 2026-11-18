"""Durable, content-keyed evidence of delivered B-roll. Only the two delivery roots are scanned."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import zlib

import numpy as np


HISTORY_VIEWPORT = .37


@dataclass(frozen=True)
class HistoryEntry:
    key: str
    label: str
    run_id: str
    source_hashes: tuple
    rendered_hashes: tuple
    scenes: tuple
    windows: tuple
    state: str
    reason: str
    source_candidates: tuple | np.ndarray = ()
    rendered_candidates: tuple | np.ndarray = ()
    scene_sequences: tuple = ()


def _stamp(path, *, with_file_id=False):
    st = path.stat()
    stamp = str(path.resolve()), st.st_size, st.st_mtime_ns, st.st_ctime_ns
    return (*stamp, st.st_dev, st.st_ino) if with_file_id else stamp


@lru_cache(maxsize=2048)
def _digest(stamp):
    with open(stamp[0], "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def content_id(path):
    # NTFS can preserve the directory entry timestamps during a same-name replacement.
    return _digest(_stamp(Path(path), with_file_id=True))


def _hashes(values):
    if values is None:
        return ()
    try:
        values = tuple(int(x, 16) if isinstance(x, str) else int(x) for x in values)
    except (TypeError, ValueError):
        return ()
    return values if len(values) == 48 and all(0 <= x < 2**64 for x in values) else ()


def _groups(plan):
    fps = plan["fps"]
    if not math.isfinite(fps) or fps <= 0 or not isinstance(plan["shots"], list) or not plan["shots"]:
        raise ValueError("历史成片的镜头清单无效")
    groups, last = [], None
    for shot in plan["shots"]:
        if shot.get("role") not in {"shuffle", "core", "opening", "solid"}:
            raise ValueError("历史镜头类型缺失或无效")
        if shot.get("role") != "shuffle":
            last = None
            continue
        start = round(shot["timeline_start_seconds"] * fps)
        length = shot["frame_count"]
        if start < 0 or type(length) is not int or length < 1 or (groups and start < groups[-1][1]):
            raise ValueError("历史打乱镜头的时间重叠或帧数无效")
        # A single cut may have been partitioned at a core/audio boundary.
        # Non-adjacent fragments have a visible edit between them and are separate.
        group = shot.get("fixed_cut_group")
        offset = shot.get("fixed_cut_part_offset_frames", 0)
        if last is not None and group == last and offset > 0 and groups[-1][1] == start:
            groups[-1] = (groups[-1][0], start + length)
        else:
            groups.append((start, start + length))
        last = group
    return groups


def _visible_scene(cut, fps):
    from shuffle_scene import scene_frames, scene_windows
    from visual_shuffle import SAMPLE_RATE, _scene_thumbnails
    d = len(cut) / fps
    positions = np.arange(max(1, math.ceil(d * SAMPLE_RATE)))
    indices = np.minimum((positions * fps / SAMPLE_RATE).astype(int), len(cut) - 1)
    height = max(1, int(cut.shape[1] * HISTORY_VIEWPORT))
    detail = scene_frames(_scene_thumbnails(cut[indices, :height]))
    return scene_windows(detail, np.array([0.]), d)[0]


def history_scenes(path, plan, ffmpeg, run):
    from visual_shuffle import _rendered_cuts
    fps = plan["fps"]
    spans = _groups(plan)
    result = []
    for cut in _rendered_cuts(ffmpeg, run, path, spans, fps) if spans else ():
        result.append(_visible_scene(cut, fps))
    return tuple(result)


def _pack(scenes):
    lengths = []
    chunks = []
    for scene in scenes:
        scene = np.asarray(scene, dtype="<u8")
        if scene.ndim != 3 or scene.shape[1:] != (2, 3) or not len(scene):
            raise ValueError("历史整刀指纹形状无效")
        lengths.append(len(scene))
        chunks.append(scene.tobytes())
    return json.dumps(lengths), zlib.compress(b"".join(chunks))


def _unpack(lengths, packed):
    raw = np.frombuffer(zlib.decompress(packed), dtype="<u8").reshape(-1, 2, 3)
    cuts, cursor = [], 0
    for length in json.loads(lengths):
        cuts.append(raw[cursor:cursor + length])
        cursor += length
    if cursor != len(raw):
        raise ValueError("历史整刀指纹长度不符")
    return tuple(cuts)


def _slot(path):
    match = re.search(r"第(\d+)条$", path.stem)
    return int(match[1]) if match else 1 if path.stem == "V17排队成片" else path.stem


def _source_windows(shots, resolve):
    """Merge contiguous source parts belonging to the very same delivered cut."""
    windows, last_group = [], None
    for shot in shots:
        if shot["role"] != "shuffle":
            last_group = None
            continue
        digest = resolve(shot)
        if not digest:
            last_group = None
            continue
        start = float(shot["source_start_seconds"])
        end = shot.get("source_end_seconds")
        end = float(end) if end is not None else start + shot["duration_seconds"]
        group = shot.get("fixed_cut_group")
        if (group is not None and group == last_group and shot.get("fixed_cut_part_offset_frames", 0) > 0
                and windows and windows[-1][0] == digest and abs(windows[-1][2] - start) < .000001):
            windows[-1] = digest, windows[-1][1], end
        else:
            windows.append((digest, start, end))
        last_group = group
    return tuple(windows)


def _read_json(path):
    with path.open(encoding="utf-8-sig") as stream:
        return json.load(stream)


def _report_plans(folder):
    result = {}
    for path in sorted(folder.glob("*.json")):
        if path.name.endswith(".manifest.json") or path.name.startswith("."):
            continue
        try:
            report = _read_json(path)
            if not isinstance(report, dict):
                continue
            variants = report.get("variants") or []
            for output, variant in zip(report.get("outputs") or [], variants):
                if isinstance(output, str) and isinstance(variant, dict) and variant.get("shots"):
                    result[Path(output).name] = variant
        except (OSError, ValueError):
            continue
    return result


class _Sources:
    """Resolve old material by its saved stream metadata, including the local recycle bin."""
    def __init__(self, data_root, ffmpeg, run):
        self.root, self.ffmpeg, self.run = data_root, ffmpeg, run
        self.by_name = {}
        for root in (data_root / "打乱素材池", data_root / ".素材回收站" / "打乱素材池", data_root / ".v17_task_assets"):
            if root.is_dir():
                for p in root.rglob("*"):
                    if p.is_file() and p.suffix.lower() in {".mp4", ".mov", ".m4v", ".mkv", ".webm"}:
                        self.by_name.setdefault(p.name.casefold(), []).append(p)
        self.resolved = {}
        self.probes = {}
        self.gray = {}
        self.references = {}

    def resolve(self, path, info, *, renamed=False):
        from audio_preserving_edit import _probe
        original = Path(path)
        key = (str(original), info.get("video_duration_seconds"), info.get("container_frame_count"),
               info.get("audio_duration_seconds"), info.get("video_stream_index"), info.get("source_sha256"),
               info.get("container_duration_seconds"), info.get("audio_channels"), renamed)
        if key in self.resolved:
            return self.resolved[key]
        candidates = [original, *self.by_name.get(original.name.casefold(), [])]
        wanted = info.get("source_sha256")
        if wanted or renamed:
            candidates += [p for paths in self.by_name.values() for p in paths]
        matches = {}
        ffprobe = Path(self.ffmpeg).with_name("ffprobe" + Path(self.ffmpeg).suffix)
        for p in dict.fromkeys(candidates):
            if not p.is_file():
                continue
            digest = content_id(p)
            if wanted:
                if digest == wanted:
                    matches[digest] = p
                continue
            if key[1] is None or (key[2] is None and (key[3] is None or key[6] is None)):
                continue
            if digest not in self.probes:
                try:
                    self.probes[digest] = _probe(p, ffprobe)
                except (OSError, ValueError, subprocess.SubprocessError):
                    self.probes[digest] = {}
            actual = self.probes[digest]
            if ((key[2] is None or actual.get("container_frame_count") == key[2]) and actual.get("video_stream_index") == key[4]
                    and abs(actual.get("video_duration_seconds", -1) - key[1]) <= .00001
                    and (key[3] is None or abs(actual.get("audio_duration_seconds", -1) - key[3]) <= .00001)
                    and (key[6] is None or abs(actual.get("container_duration_seconds", -1) - key[6]) <= .00001)
                    and (key[7] is None or actual.get("audio_channels") == key[7])):
                matches[digest] = p
        # Multiple distinguishable copies with exactly the same historical metadata
        # cannot establish which original this old job consumed.
        if not matches and not renamed and not wanted:
            result = self.resolve(path, info, renamed=True)
        else:
            result = next(iter(matches.items())) if len(matches) == 1 else None
        self.resolved[key] = result
        return self.resolved[key]

    def raw_cut(self, path, digest, shot, plan, *, witness=False):
        source = plan["sources"][shot["source_path"]]
        fps, width, height = plan["fps"], plan["width"], plan["height"]
        start, duration, frames = shot["source_start_seconds"], shot["duration_seconds"], shot["frame_count"]
        end, contain = shot.get("source_end_seconds"), shot.get("picture_fit") == "contain"
        stream = source["video_stream_index"]
        loop = plan.get("visual_policy") == "interleave" and start + duration > source["duration_seconds"]
        key = digest, start, end, duration, frames, width, height, fps, stream, contain, loop
        cache = self.references if witness else self.gray
        if key not in cache:
            fit = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
                   if contain else f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}")
            # Mirror the pre-overlay source tap in render_edit. No video is created.
            filt = (fit + f",setsar=1,fps={fps},tpad=stop_mode=clone:stop_duration={duration:.9f},"
                    f"trim=end_frame={frames},setpts=N/({fps}*TB),format=yuv420p")
            filt += (",scale=144:256:force_original_aspect_ratio=increase,crop=144:256,setsar=1"
                     if witness else ",scale=9:8:flags=area,format=gray")
            with tempfile.TemporaryDirectory(prefix="8018-history-source-") as temp:
                out = Path(temp) / "cut.gray"
                cmd = [str(self.ffmpeg), "-v", "error", "-nostdin", "-y", "-threads", "2"]
                if loop:
                    cmd += ["-stream_loop", "-1"]
                cmd += ["-ss", f"{start:.9f}"]
                if end is not None:
                    cmd += ["-t", f"{end - start:.9f}"]
                self.run(cmd + ["-i", str(path), "-map", f"0:{stream}", "-an", "-vf", filt,
                                "-filter_threads", "1", "-frames:v", str(frames), "-fps_mode", "passthrough",
                                "-threads", "1", "-pix_fmt", "rgb24" if witness else "gray", "-f", "rawvideo", str(out)], out)
                data = out.read_bytes()
            if len(data) != frames * (144 * 256 * 3 if witness else 72):
                raise ValueError("历史打乱原片无法还原完整帧数")
            cache[key] = _visible_scene(np.frombuffer(data, np.uint8).reshape(-1,256,144,3), fps) if witness else data
        return cache[key]

    def _matches_delivery(self, resolved, shots, plan, actual_by_start):
        """Old manifests lacking physical frame counts also need a visible-picture witness."""
        from whole_shot_shuffle import repeated_whole_shots
        digest, path = resolved
        by_start = {}
        for shot in shots:
            at = round(shot["timeline_start_seconds"] * plan["fps"])
            if at in actual_by_start and shot.get("fixed_cut_part_offset_frames", 0) == 0:
                by_start.setdefault(shot["source_start_seconds"], (shot, actual_by_start[at]))
        # Check up to three physically distinct windows from the candidate
        # source. A single available cut is verifiable across its entire length.
        checks = sorted(by_start)
        checks = [checks[i] for i in sorted(set(np.linspace(0, len(checks)-1, min(3, len(checks))).astype(int)))]
        for start in checks:
            shot, reference = by_start[start]
            frames = shot.get("fixed_cut_group_frame_count", shot["frame_count"])
            duration = frames / plan["fps"]
            full = dict(shot, frame_count=frames, duration_seconds=duration)
            if shot.get("source_end_seconds") is not None:
                full["source_end_seconds"] = start + duration
            bank = self.raw_cut(path, digest, full, plan, witness=True)[None, ...]
            if not repeated_whole_shots(bank, reference).any():
                return False
        return bool(checks)

    def evidence(self, plan, previous, actual=()):
        from shuffle_similarity import source_shuffle_hashes
        identities, raw = {}, []
        complete = True
        groups = _groups(plan)
        actual_by_start = {span[0]:cut for span,cut in zip(groups, actual)}
        by_source = {}
        for shot in plan["shots"]:
            if shot["role"] == "shuffle":
                by_source.setdefault(shot["source_path"], []).append(shot)
        resolved_sources = {}
        for path, shots in by_source.items():
            info = plan["sources"][path]
            candidate = self.resolve(path, info)
            if (candidate and (info.get("container_frame_count") is None or candidate[1].name.casefold() != Path(path).name.casefold())
                    and not info.get("source_sha256")
                    and not self._matches_delivery(candidate, shots, plan, actual_by_start)):
                candidate = None
            resolved_sources[path] = candidate
        for shot in plan["shots"]:
            if shot["role"] != "shuffle":
                continue
            resolved = resolved_sources[shot["source_path"]]
            if not resolved:
                complete = False
                continue
            digest, path = resolved
            identities[shot["source_path"]] = digest
            if not previous and complete:
                raw.append(self.raw_cut(path, digest, shot, plan))
        windows = _source_windows(plan["shots"], lambda s: identities.get(s["source_path"]))
        if previous or not complete:
            return previous, windows
        with tempfile.TemporaryDirectory(prefix="8018-history-track-") as temp:
            track = Path(temp) / "track.gray"
            track.write_bytes(b"".join(raw))
            hashes = source_shuffle_hashes(track, track.stat().st_size // 72, plan["fps"], self.ffmpeg, self.run)
        return tuple(hashes), windows


def _pack_bounds(bounds):
    out = io.BytesIO()
    arrays = {k:np.asarray(bounds.get(k) if bounds.get(k) is not None else [], dtype="<u8")
              for k in ("source_candidates", "rendered_candidates")}
    arrays.update({f"scene{i}":np.asarray(s, dtype="<u8") for i,s in enumerate(bounds.get("scene_sequences", ()))})
    np.savez_compressed(out, **arrays)
    return out.getvalue()


def _unpack_bounds(blob):
    if not blob:
        return (), (), ()
    with np.load(io.BytesIO(blob), allow_pickle=False) as data:
        return data["source_candidates"], data["rendered_candidates"], tuple(data[k] for k in data if k.startswith("scene"))


def _history_entry_rows(conn, excluded=None):
    # If two tasks delivered byte-identical copies, keep one comparison even
    # when just one of their run IDs has been excluded.
    query = """SELECT e.*, a.run_id, a.label, b.archive FROM evidence e JOIN (
               SELECT digest, min(run_id) run_id, min(label) label FROM alias_versions
               WHERE (? IS NULL OR run_id <> ?) GROUP BY digest
               ) a ON a.digest=e.digest LEFT JOIN bounded b ON b.digest=e.digest ORDER BY a.run_id, a.label, e.digest"""
    result = []
    for digest, source, rendered, lengths, packed, windows, state, reason, job, label, archive in conn.execute(query, (excluded, excluded)):
        result.append(HistoryEntry(digest, label, job, tuple(json.loads(source)), tuple(json.loads(rendered)),
                                   _unpack(lengths, packed), tuple(map(tuple, json.loads(windows))), state, reason,
                                   *_unpack_bounds(archive)))
    return result


def _put(conn, path, key, run_id, slot, source, rendered, scenes, windows, state, reason):
    lengths, packed = _pack(scenes)
    data = key, json.dumps(source), json.dumps(rendered), lengths, packed, json.dumps(windows), state, reason
    conn.execute("""INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(digest) DO UPDATE SET
                    source=CASE WHEN excluded.source<>'[]' THEN excluded.source ELSE evidence.source END,
                    rendered=CASE WHEN excluded.rendered<>'[]' THEN excluded.rendered ELSE evidence.rendered END,
                    lengths=CASE WHEN excluded.lengths<>'[]' THEN excluded.lengths ELSE evidence.lengths END,
                    scenes=CASE WHEN excluded.lengths<>'[]' THEN excluded.scenes ELSE evidence.scenes END,
                    windows=CASE WHEN excluded.windows<>'[]' THEN excluded.windows ELSE evidence.windows END,
                    state=CASE WHEN evidence.state IN ('complete','no_shuffle') OR (evidence.state='bounded' AND excluded.state<>'complete') THEN evidence.state ELSE excluded.state END,
                    reason=CASE WHEN evidence.state IN ('complete','no_shuffle') OR (evidence.state='bounded' AND excluded.state<>'complete') THEN evidence.reason ELSE excluded.reason END""", data)
    conn.execute("""INSERT INTO aliases VALUES (?,?,?,?) ON CONFLICT(run_id,slot)
                    DO UPDATE SET digest=excluded.digest,label=excluded.label""",
                 (run_id, str(slot), key, Path(path).name))


def _load_queue_configs(data_root):
    path = data_root / ".v17_task_queue.sqlite3"
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_queue'").fetchone():
            return {}
        configs = {}
        for run_id, raw in db.execute("SELECT task_id,config_json FROM task_queue"):
            try:
                config = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(config, dict):
                configs[run_id] = config
        return configs


class ShuffleHistory:
    def __init__(self, db_path):
        self.path = Path(db_path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._open()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS evidence(
                    digest TEXT PRIMARY KEY, source TEXT NOT NULL, rendered TEXT NOT NULL,
                    lengths TEXT NOT NULL, scenes BLOB NOT NULL, windows TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS aliases(
                    run_id TEXT NOT NULL, slot TEXT NOT NULL, digest TEXT NOT NULL REFERENCES evidence(digest),
                    label TEXT NOT NULL, PRIMARY KEY(run_id,slot));
                CREATE INDEX IF NOT EXISTS alias_digest ON aliases(digest);
                CREATE TABLE IF NOT EXISTS alias_versions(
                    run_id TEXT NOT NULL, slot TEXT NOT NULL, digest TEXT NOT NULL REFERENCES evidence(digest),
                    label TEXT NOT NULL, PRIMARY KEY(run_id,slot,digest));
                CREATE INDEX IF NOT EXISTS alias_version_digest ON alias_versions(digest);
                INSERT OR IGNORE INTO alias_versions SELECT * FROM aliases;
                DROP TRIGGER IF EXISTS preserve_delivered_alias;
                CREATE TRIGGER IF NOT EXISTS preserve_delivered_alias_v2 AFTER INSERT ON aliases BEGIN
                    INSERT INTO alias_versions SELECT NEW.run_id,NEW.slot,NEW.digest,NEW.label WHERE NOT EXISTS(
                        SELECT 1 FROM alias_versions WHERE run_id=NEW.run_id AND slot=NEW.slot AND digest=NEW.digest);
                END;
                DROP TRIGGER IF EXISTS preserve_replaced_alias;
                CREATE TRIGGER IF NOT EXISTS preserve_replaced_alias_v2 AFTER UPDATE ON aliases BEGIN
                    INSERT INTO alias_versions SELECT OLD.run_id,OLD.slot,OLD.digest,OLD.label WHERE NOT EXISTS(
                        SELECT 1 FROM alias_versions WHERE run_id=OLD.run_id AND slot=OLD.slot AND digest=OLD.digest);
                    INSERT INTO alias_versions SELECT NEW.run_id,NEW.slot,NEW.digest,NEW.label WHERE NOT EXISTS(
                        SELECT 1 FROM alias_versions WHERE run_id=NEW.run_id AND slot=NEW.slot AND digest=NEW.digest);
                END;
                CREATE TRIGGER IF NOT EXISTS preserve_history_bounds BEFORE UPDATE ON evidence
                WHEN OLD.state='bounded' AND NEW.state IN ('partial','unavailable') BEGIN
                    SELECT RAISE(IGNORE);
                END;
                CREATE TABLE IF NOT EXISTS scanned(
                    path TEXT PRIMARY KEY, stamp TEXT NOT NULL, digest TEXT NOT NULL REFERENCES evidence(digest));
                CREATE TABLE IF NOT EXISTS bounded(
                    digest TEXT PRIMARY KEY REFERENCES evidence(digest), archive BLOB NOT NULL);
                COMMIT;
            """)

    def _open(self):
        db = sqlite3.connect(self.path, timeout=90)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def entries(self, exclude_run_id=None):
        with closing(self._open()) as db:
            return _history_entry_rows(db, exclude_run_id)

    def record(self, path, plan, *, run_id, source_hashes, rendered_hashes, scenes,
               windows=None, validate=None, index=1, destination=None):
        key = content_id(path)
        source, rendered = _hashes(source_hashes), _hashes(rendered_hashes)
        scenes = tuple(scenes)
        expected = len(_groups(plan))
        if len(source) != 48 or len(rendered) != 48 or not expected or len(scenes) != expected:
            raise ValueError("待交付成片的历史比对证据不完整")
        if windows is None:
            windows = _source_windows(plan["shots"], lambda s: content_id(s["source_path"]))
        target = Path(destination or path).resolve()
        with closing(self._open()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if validate is not None:
                validate(_history_entry_rows(db, run_id))
            _put(db, target, key, run_id, index, source, rendered, scenes, windows, "complete", "")
        return key

    def sync(self, roots, ffmpeg, run, *, exclude_run_id=None, log=None, only_run_ids=None, only_dongge=False):
        from shuffle_similarity import rendered_shuffle_hashes
        import shuffle_history_legacy
        roots = tuple(dict.fromkeys(Path(root).resolve() for root in roots))
        scanned = 0
        with closing(self._open()) as db:
            cached = {p:(json.loads(stamp), state, bool(json.loads(windows)), digest)
                      for p,stamp,state,windows,digest in db.execute(
                "SELECT s.path,s.stamp,e.state,e.windows,s.digest FROM scanned s JOIN evidence e ON e.digest=s.digest")}
        for root in roots:
            if not root.is_dir():
                continue
            data_root = root.parent
            sources = _Sources(data_root, ffmpeg, run)
            candidate_stats = sorted(_stamp(p) for paths in sources.by_name.values() for p in paths if p.is_file())
            source_stamp = hashlib.sha256(repr(candidate_stats).encode()).hexdigest()
            configs = _load_queue_configs(data_root)
            for folder in sorted(p for p in root.iterdir() if p.is_dir() and p.name != exclude_run_id
                                 and (only_run_ids is None or p.name in only_run_ids)):
                if only_dongge and configs.get(folder.name, {}).get("edition_mode") != "dongge" and not (folder / ".dongge").is_file():
                    continue
                reports = None
                for video in sorted(folder.glob("*.mp4")):
                    # Queue drafts, partial muxes, and internal pipeline inputs are not deliveries.
                    if video.name.startswith(".") or ".partial" in video.name.lower():
                        continue
                    sidecar = video.with_suffix(".manifest.json")
                    try:
                        media_stamp = _stamp(video)
                        side_stamp = _stamp(sidecar)[1:] if sidecar.is_file() else ()
                    except FileNotFoundError:
                        continue
                    bounded_reader = getattr(shuffle_history_legacy, "legacy_bounds", None)
                    signature = [list(media_stamp[1:]), list(side_stamp), source_stamp,
                                 3, bool(bounded_reader) if not side_stamp else True]
                    stamp = json.dumps(signature)
                    old = cached.get(str(video))
                    same_media = old and old[0][0] == signature[0]
                    same_files = same_media and old[0][1] == signature[1]
                    finished = old and (old[1] == "no_shuffle" or old[1] == "bounded" or
                                        (old[1] == "complete" and old[2]))
                    if old and (old[0] == signature or (same_files and finished)):
                        continue
                    digest = old[3] if same_media else _digest(media_stamp)
                    stored = None
                    # A duplicate or a freshly published file already indexed in
                    # record() needs only a durable alias, never another decode.
                    with closing(self._open()) as db, db:
                        known = db.execute("SELECT state,windows FROM evidence WHERE digest=?", (digest,)).fetchone()
                        upgrade_windows = known and known[0] == "complete" and known[1] == "[]" and sidecar.is_file()
                        if upgrade_windows or (known and known[0] == "partial" and same_files and sidecar.is_file()):
                            stored = db.execute("SELECT source,rendered,lengths,scenes FROM evidence WHERE digest=?",
                                                (digest,)).fetchone()
                        if known and known[0] in {"complete", "no_shuffle", "bounded"} and not upgrade_windows:
                            db.execute("INSERT OR REPLACE INTO aliases VALUES (?,?,?,?)",
                                       (folder.name, str(_slot(video)), digest, video.name))
                            db.execute("INSERT OR REPLACE INTO scanned VALUES (?,?,?)", (str(video), stamp, digest))
                            continue
                    if log:
                        log(f"核对历史成片 {folder.name[:8]} / {video.name}")
                    source, rendered, scenes, windows, issues = (), (), (), (), []
                    plan = None
                    bounds = None
                    try:
                        if sidecar.is_file():
                            plan = _read_json(sidecar)
                    except (ValueError, OSError) as exc:
                        issues.append(str(exc))
                    if not isinstance(plan, dict) or not plan.get("shots"):
                        if reports is None:
                            reports = _report_plans(folder)
                        plan = reports.get(video.name)
                    state = "unavailable"
                    if plan is None:
                        source = _hashes(shuffle_history_legacy.legacy_saved_source_hashes(data_root, video))
                        state = "partial" if source else "unavailable"
                        issues.append("缺少可验证的打乱镜头时间轴")
                        if bounded_reader is not None:
                            bounds = bounded_reader(data_root, video, ffmpeg, run)
                            if bounds:
                                source_bound = bounds.get("source_candidates")
                                render_bound = bounds.get("rendered_candidates")
                                sequences = bounds.get("scene_sequences", ())
                                if ((source or (source_bound is not None and len(source_bound)))
                                        and render_bound is not None and len(render_bound) and len(sequences)):
                                    state = "bounded"
                                issues.extend(str(bounds[k]) for k in ("source_reason","rendered_reason") if bounds.get(k))
                    else:
                        try:
                            groups = _groups(plan)
                            state = "partial" if groups else "no_shuffle"
                        except (ValueError, KeyError, TypeError) as exc:
                            groups = []
                            issues.append(str(exc))
                        if groups:
                            source = _hashes((plan.get("shuffle_visual_verification") or {}).get("pre_overlay_dhashes")
                                             or plan.get("source_shuffle_dhashes"))
                            if stored:
                                source = source or tuple(json.loads(stored[0]))
                                rendered = tuple(json.loads(stored[1]))
                                scenes = _unpack(stored[2], stored[3])
                            try:
                                if len(rendered) != 48:
                                    rendered = tuple(rendered_shuffle_hashes(video, plan, ffmpeg, run))
                                if len(scenes) != len(groups):
                                    scenes = history_scenes(video, plan, ffmpeg, run)
                            except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
                                issues.append(str(exc))
                            config = configs.get(folder.name)
                            # With a confirmed effect-free shuffle track the renderer uses
                            # the encoded fingerprint for both independent evidence slots.
                            if not source and config is not None and config.get("edit_mode") in {"original_audio_random","original_audio_broll"}:
                                if not config.get("bottom_overlays") and not config.get("shuffle_masks"):
                                    source = rendered
                            try:
                                source, windows = sources.evidence(plan, source, scenes)
                            except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
                                issues.append(str(exc))
                            if not source:
                                issues.append("未能确认打乱原画48帧指纹")
                            if source and rendered and len(scenes) == len(groups):
                                state = "complete"
                    packed_bounds = _pack_bounds(bounds) if bounds else None
                    with closing(self._open()) as db, db:
                        db.execute("BEGIN IMMEDIATE")
                        _put(db, video, digest, folder.name, _slot(video), source, rendered, scenes, windows,
                             state, "；".join(issues))
                        if packed_bounds is not None:
                            if state == "bounded":
                                db.execute("INSERT OR REPLACE INTO bounded VALUES (?,?)", (digest, packed_bounds))
                            else:
                                db.execute("INSERT OR IGNORE INTO bounded VALUES (?,?)", (digest, packed_bounds))
                        db.execute("INSERT OR REPLACE INTO scanned VALUES (?,?,?)", (str(video), stamp, digest))
                    scanned += 1
        counts = dict.fromkeys(("complete","bounded","partial","unavailable","no_shuffle"), 0)
        with closing(self._open()) as db:
            counts.update(db.execute("""SELECT e.state,count(*) FROM evidence e WHERE EXISTS (
                SELECT 1 FROM alias_versions a WHERE a.digest=e.digest AND (? IS NULL OR a.run_id<>?)) GROUP BY e.state""",
                                     (exclude_run_id, exclude_run_id)))
        return dict(scanned=scanned, **counts)
