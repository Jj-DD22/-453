from delivery_handoff_guard import cleanup_allowed as _handoff_cleanup_allowed
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path


WAITING_STATUS = "waiting"
ACTIVE_STATUSES = {"running", "skip_requested", "finalizing"}
TERMINAL_STATUSES = {
    "succeeded",
    "failed",
    "timed_out",
    "skipped",
    "canceled",
    "interrupted",
    "review_required",
}
# A successful task has already delivered its requested videos.  Its private
# frozen material copy can be reclaimed, so it must not advertise a retry that
# depends on that disposable copy still existing.
RETRYABLE_STATUSES = TERMINAL_STATUSES - {"review_required", "succeeded"}
CLEARABLE_STATUSES = TERMINAL_STATUSES - {"review_required"}
# Bulk cleanup remains conservative, but an authenticated owner may explicitly
# hide one selected terminal record after billing is settled.  Hiding never
# deletes source material or delivery artifacts.
HIDEABLE_STATUSES = TERMINAL_STATUSES
BILLING_STATES = {
    "not_started",
    "started",
    "confirmed",
    "not_required",
    "uncertain",
}
DEFAULT_BILLING_STATE = "not_started"
CONTROL_STATES = {
    "active",
    "pause_requested",
    "paused",
    "resume_requested",
}
DEFAULT_CONTROL_STATE = "active"
CONTROL_DB_TIMEOUT_SECONDS = 0.25
MAX_IDENTITY_LENGTH = 256
COMPLETED_DELIVERY_RETENTION_SECONDS = 48 * 60 * 60
DOWNLOADED_DELIVERY_RETENTION_SECONDS = COMPLETED_DELIVERY_RETENTION_SECONDS
DAILY_COMPLETED_OUTPUT_CLEANUP_MAINTENANCE_KEY = (
    "daily_completed_output_cleanup_v1"
)
MAX_DELIVERY_ARTIFACT_KEY_LENGTH = 255
ERP_REPORT_OUTBOX_STATES = {
    "deferred_auth",
    "sending",
    "confirmed",
    "uncertain",
}
ERP_REPORT_PAYLOAD_KEYS = {
    "AppType",
    "ModuleName",
    "PageName",
    "PageTab",
    "ActionName",
    "ReqPath",
}
MAX_ERP_REPORT_PAYLOAD_BYTES = 64 * 1024
MAX_ERP_REPORT_ERROR_LENGTH = 1000
_ERP_OUTBOX_PROCESS_INSTANCE = uuid.uuid4().hex
_DATABASE_SETUP_LOCKS = {}
_DATABASE_SETUP_LOCKS_GUARD = threading.Lock()


def _timestamp_text(epoch_seconds=None):
    if epoch_seconds is None:
        epoch_seconds = time.time()
    epoch_seconds = float(epoch_seconds)
    if not math.isfinite(epoch_seconds):
        raise ValueError("timestamp must be finite")
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch_seconds))


def _timestamp_epoch(value):
    if isinstance(value, (int, float)):
        epoch_seconds = float(value)
        if not math.isfinite(epoch_seconds):
            raise ValueError("timestamp must be finite")
        return epoch_seconds
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("timestamp is required")
    try:
        parsed = time.strptime(normalized, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("timestamp has an invalid format") from exc
    return float(time.mktime(parsed))


def _coerce_timestamp_text(value=None):
    return _timestamp_text(
        time.time() if value is None else _timestamp_epoch(value)
    )


def _now_text():
    return _timestamp_text()


def _queue_created_at_text():
    """Wall-clock enqueue time with microseconds for cross-database FIFO ties.

    Retention and settlement timestamps intentionally keep their existing
    second-level contract.  Only a newly enqueued row needs the finer text;
    the global broker already accepts both legacy seconds and this fractional
    form, so old databases remain compatible.
    """

    epoch_nanoseconds = time.time_ns()
    whole_seconds, nanoseconds = divmod(epoch_nanoseconds, 1_000_000_000)
    return (
        time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(whole_seconds),
        )
        + f".{nanoseconds // 1_000:06d}"
    )


def _normalized_hex_identifier(value, field_name):
    normalized = str(value or "").strip()
    if (
        len(normalized) != 32
        or normalized.lower() != normalized
        or any(character not in "0123456789abcdef" for character in normalized)
    ):
        raise ValueError(f"{field_name} must be 32 lowercase hexadecimal characters")
    return normalized


def _normalized_erp_report_text(value, field_name, maximum_length):
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > int(maximum_length)
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        raise ValueError(f"{field_name} is invalid")
    return normalized


def _canonical_erp_report_payload(payload):
    """Return the fixed PageReqLog JSON without permitting credential fields."""

    if not isinstance(payload, dict) or not payload:
        raise ValueError("ERP report payload must be a non-empty object")
    unknown_keys = set(payload) - ERP_REPORT_PAYLOAD_KEYS
    if unknown_keys:
        raise ValueError("ERP report payload contains unsupported fields")
    normalized = {}
    for key, value in payload.items():
        if not isinstance(value, str):
            raise ValueError("ERP report payload values must be strings")
        maximum_length = 2048 if key == "ReqPath" else 256
        if len(value) > maximum_length or any(
            ord(character) < 32 and character not in "\t\r\n"
            for character in value
        ):
            raise ValueError("ERP report payload value is invalid")
        if value.strip().lower().startswith("bearer "):
            raise ValueError("ERP report payload must not contain credentials")
        normalized[str(key)] = value
    serialized = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    if len(serialized.encode("utf-8")) > MAX_ERP_REPORT_PAYLOAD_BYTES:
        raise ValueError("ERP report payload is too large")
    return serialized


def _clean_erp_report_error(error):
    text = re.sub(r"\s+", " ", str(error or "")).strip()
    text = re.sub(
        r"(?i)Bearer\s+[^\s,;]+",
        "Bearer [redacted]",
        text,
    )
    text = re.sub(
        r"(?i)((?:authorization|api[_-]?key|token|secret|password)\s*[=:]\s*)"
        r"[^\s,;&]+",
        r"\1[redacted]",
        text,
    )
    return text[:MAX_ERP_REPORT_ERROR_LENGTH]


def _normalized_delivery_artifact_keys(values, *, required=False):
    if isinstance(values, str) or not isinstance(values, (list, tuple, set)):
        if required:
            raise ValueError("delivery artifact keys must be a non-empty sequence")
        return ()
    normalized = []
    for value in values:
        key = str(value or "").strip()
        if (
            not key
            or len(key) > MAX_DELIVERY_ARTIFACT_KEY_LENGTH
            or key in {".", ".."}
            or "/" in key
            or "\\" in key
            or any(ord(character) < 32 or ord(character) == 127 for character in key)
        ):
            if required:
                raise ValueError("delivery artifact key is invalid")
            continue
        if key not in normalized:
            normalized.append(key)
    if required and not normalized:
        raise ValueError("delivery artifact keys must not be empty")
    return tuple(normalized)


def _normalized_download_artifact_receipts(value, fallback_timestamp=None):
    fallback = None
    if fallback_timestamp:
        try:
            fallback = _coerce_timestamp_text(fallback_timestamp)
        except ValueError:
            fallback = None
    if isinstance(value, dict):
        source_items = value.items()
    elif isinstance(value, list) and fallback:
        # A short-lived pre-release staging build used a list.  If encountered,
        # its task-level completion time is the only safe per-file timestamp.
        source_items = ((key, fallback) for key in value)
    else:
        return {}
    receipts = {}
    for raw_key, raw_timestamp in source_items:
        keys = _normalized_delivery_artifact_keys([raw_key])
        if len(keys) != 1:
            continue
        try:
            timestamp = _coerce_timestamp_text(raw_timestamp)
        except ValueError:
            continue
        previous = receipts.get(keys[0])
        if previous is None or timestamp < previous:
            receipts[keys[0]] = timestamp
    return receipts


def _required_delivery_video_keys(result):
    """Return the immutable internal MP4 names represented by a result row."""
    if not isinstance(result, dict):
        return ()
    kind = str(result.get("kind") or "")
    if kind == "single":
        return ("V17排队成片.mp4",)
    if kind != "fission":
        return ()
    outputs = result.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        return ()
    names = []
    for output in outputs:
        if not isinstance(output, dict):
            return ()
        candidate = _normalized_delivery_artifact_keys(
            [output.get("name")],
            required=False,
        )
        if len(candidate) != 1 or not candidate[0].lower().endswith(".mp4"):
            return ()
        if candidate[0] not in names:
            names.append(candidate[0])
    return tuple(names)


def _database_setup_lock(database_path):
    """Share schema initialization locks across queue objects in one process."""
    normalized_path = str(Path(database_path).resolve()).casefold()
    with _DATABASE_SETUP_LOCKS_GUARD:
        lock = _DATABASE_SETUP_LOCKS.get(normalized_path)
        if lock is None:
            lock = threading.Lock()
            _DATABASE_SETUP_LOCKS[normalized_path] = lock
        return lock


def _finite_number(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _normalized_asset_set_id(value):
    normalized = str(value or "").strip().lower()
    if len(normalized) != 32 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        return None
    return normalized


def _normalized_identity(value, field_name, required=True):
    """Normalize a persisted identity without ever inventing one.

    Legacy rows deliberately keep ``owner_user_id``/``tenant_id`` as NULL.
    New public writes must supply an authenticated owner; silently falling
    back to a machine-wide identity would re-introduce cross-user access.
    """
    normalized = str(value or "").strip()
    if not normalized:
        if required:
            raise ValueError(f"{field_name} is required")
        return None
    if len(normalized) > MAX_IDENTITY_LENGTH:
        raise ValueError(f"{field_name} is too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{field_name} contains control characters")
    return normalized


def _public_material_name(value):
    """Return a bounded basename-like label without exposing a local path."""
    raw_name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    return " ".join(
        "".join(character if ord(character) >= 32 else " " for character in raw_name)
        .split()
    )[:160]


def _public_task_summary(config):
    """Return only the small, non-sensitive fields needed by queue cards."""
    if not isinstance(config, dict):
        return {}
    raw_name = str(config.get("task_name") or "视频制作任务")
    task_name = " ".join(
        "".join(character if ord(character) >= 32 else " " for character in raw_name)
        .split()
    )[:80] or "视频制作任务"
    try:
        fission_count = max(1, min(5, int(config.get("fission_count") or 1)))
    except (TypeError, ValueError):
        fission_count = 1

    configured_material = config.get("material_summary")
    if isinstance(configured_material, dict):
        material = {
            "core_count": max(0, int(_finite_number(configured_material.get("core_count")))),
            "shuffle_count": max(0, int(_finite_number(configured_material.get("shuffle_count")))),
            "sticker_count": max(0, int(_finite_number(configured_material.get("sticker_count")))),
            "dynamic_sticker_count": max(
                0,
                int(_finite_number(configured_material.get("dynamic_sticker_count"))),
            ),
            "dynamic_sticker_name": _public_material_name(
                configured_material.get("dynamic_sticker_name")
            ),
            "core_seconds": round(max(0.0, _finite_number(configured_material.get("core_seconds"))), 1),
            "shuffle_seconds": round(max(0.0, _finite_number(configured_material.get("shuffle_seconds"))), 1),
        }
    else:
        selected = config.get("selected_materials")
        selected = selected if isinstance(selected, dict) else {}
        selected_stickers = selected.get("贴纸素材池", [])
        selected_stickers = selected_stickers if isinstance(selected_stickers, list) else []
        dynamic_stickers = [
            name
            for name in selected_stickers
            if Path(str(name)).suffix.lower() in {".gif", ".mp4"}
        ]
        corner_stickers = [
            name
            for name in selected_stickers
            if Path(str(name)).suffix.lower() == ".png"
        ]
        material = {
            "core_count": len(selected.get("核心素材", []))
            if isinstance(selected.get("核心素材", []), list)
            else 0,
            "shuffle_count": len(selected.get("打乱素材池", []))
            if isinstance(selected.get("打乱素材池", []), list)
            else 0,
            "sticker_count": len(corner_stickers),
            "dynamic_sticker_count": len(dynamic_stickers),
            "dynamic_sticker_name": _public_material_name(
                dynamic_stickers[0] if dynamic_stickers else ""
            ),
            "core_seconds": 0.0,
            "shuffle_seconds": 0.0,
        }

    timepin = config.get("timepin_zoom")
    timepin = timepin if isinstance(timepin, dict) else {}
    keyframes = timepin.get("keyframes")
    keyframes = keyframes if isinstance(keyframes, list) else []
    scales = [
        _finite_number(item.get("scale_percent"), 100.0)
        for item in keyframes
        if isinstance(item, dict)
    ]
    verticals = [
        abs(_finite_number(item.get("vertical_percent"), 0.0))
        for item in keyframes
        if isinstance(item, dict)
    ]
    scale_span = (max(scales) - min(scales)) if scales else 0.0
    timepin_summary = {
        "enabled": bool(timepin.get("enabled")),
        "scales": [int(round(value)) for value in scales[:8]],
        "max_vertical": int(round(max(verticals))) if verticals else 0,
        "low_scale_span": bool(timepin.get("enabled")) and scale_span < 5.0,
    }
    corner = config.get("corner_mask_reveal")
    corner = corner if isinstance(corner, dict) else {}
    return {
        "task_name": task_name,
        "fission_count": fission_count,
        "target_duration": round(
            max(0.0, _finite_number(config.get("target_duration"))), 1
        ),
        "material": material,
        "timepin": timepin_summary,
        "corner_radius": max(
            0, min(300, int(_finite_number(corner.get("radius_px"))))
        ),
        "auto_save_enabled": bool(
            str(config.get("auto_save_directory") or "").strip()
        ),
    }


class PersistentTaskQueue:
    """Small SQLite FIFO with no application-level pending-task limit."""

    def __init__(self, database_path):
        self.database_path = Path(database_path).expanduser().resolve()
        self._setup_lock = _database_setup_lock(self.database_path)
        self._ready = False

    def _connect(self, timeout_seconds=30):
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        timeout_seconds = max(0.05, float(timeout_seconds))
        connection = sqlite3.connect(
            str(self.database_path),
            timeout=timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(
            f"PRAGMA busy_timeout={max(50, int(timeout_seconds * 1000))}"
        )
        return connection

    @staticmethod
    def _enable_wal(connection, timeout_seconds=30):
        """Enable WAL even when two fresh worker processes initialize together."""
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while True:
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def ensure_ready(self):
        if self._ready:
            return
        with self._setup_lock:
            if self._ready:
                return
            with self._connect() as connection:
                self._enable_wal(connection)
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS task_queue (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL UNIQUE,
                        owner_user_id TEXT,
                        tenant_id TEXT,
                        submission_key TEXT,
                        status TEXT NOT NULL,
                        title TEXT NOT NULL,
                        config_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        finished_at TEXT,
                        stage TEXT NOT NULL DEFAULT '',
                        error TEXT NOT NULL DEFAULT '',
                        result_json TEXT NOT NULL DEFAULT '{}',
                        history_hidden INTEGER NOT NULL DEFAULT 0
                            CHECK (history_hidden IN (0, 1)),
                        billing_state TEXT NOT NULL DEFAULT 'not_started'
                            CHECK (billing_state IN (
                                'not_started', 'started', 'confirmed',
                                'not_required', 'uncertain'
                            )),
                        control_state TEXT NOT NULL DEFAULT 'active'
                            CHECK (control_state IN (
                                'active', 'pause_requested', 'paused',
                                'resume_requested'
                            )),
                        control_error TEXT NOT NULL DEFAULT '',
                        control_changed_at TEXT,
                        downloaded_artifacts_json TEXT NOT NULL DEFAULT '{}',
                        downloaded_at TEXT,
                        delivery_retention_started_at TEXT,
                        delivery_expired_at TEXT,
                        delivery_expired_reason TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS queue_maintenance_state (
                        maintenance_key TEXT PRIMARY KEY,
                        next_due_at TEXT NOT NULL,
                        last_completed_at TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS erp_report_outbox (
                        event_id TEXT PRIMARY KEY,
                        owner_user_id TEXT NOT NULL,
                        tenant_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        event_key TEXT NOT NULL,
                        output_name TEXT NOT NULL DEFAULT '',
                        payload_json TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'deferred_auth'
                            CHECK (state IN (
                                'deferred_auth', 'sending',
                                'confirmed', 'uncertain'
                            )),
                        attempt_count INTEGER NOT NULL DEFAULT 0
                            CHECK (attempt_count >= 0),
                        last_error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_attempt_at TEXT,
                        confirmed_at TEXT,
                        sender_instance TEXT NOT NULL DEFAULT ''
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_erp_report_outbox_state_created "
                    "ON erp_report_outbox(state, created_at, event_id)"
                )
                # A direct ERP POST has no remote idempotency field.  If the
                # previous tenant process died after changing a row to
                # ``sending``, its outcome is unknowable; fence it as
                # ``uncertain`` instead of ever submitting it again.
                recovered_at = _now_text()
                connection.execute(
                    """
                    UPDATE erp_report_outbox
                    SET state='uncertain', sender_instance='', updated_at=?,
                        last_error=CASE
                            WHEN TRIM(last_error)='' THEN ?
                            ELSE last_error
                        END
                    WHERE state='sending' AND sender_instance!=?
                    """,
                    (
                        recovered_at,
                        "服务在 ERP 补传请求期间退出；远端结果不明，禁止自动重试。",
                        _ERP_OUTBOX_PROCESS_INSTANCE,
                    ),
                )
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(task_queue)"
                    ).fetchall()
                }
                if "auto_fill_json" not in columns:
                    connection.execute("ALTER TABLE task_queue ADD COLUMN auto_fill_json TEXT NOT NULL DEFAULT '{}'")
                if "submission_key" not in columns:
                    connection.execute(
                        "ALTER TABLE task_queue ADD COLUMN submission_key TEXT"
                    )
                if "owner_user_id" not in columns:
                    connection.execute(
                        "ALTER TABLE task_queue ADD COLUMN owner_user_id TEXT"
                    )
                if "tenant_id" not in columns:
                    connection.execute(
                        "ALTER TABLE task_queue ADD COLUMN tenant_id TEXT"
                    )
                if "billing_state" not in columns:
                    connection.execute(
                        """
                        ALTER TABLE task_queue
                        ADD COLUMN billing_state TEXT NOT NULL DEFAULT 'not_started'
                            CHECK (billing_state IN (
                                'not_started', 'started', 'confirmed',
                                'not_required', 'uncertain'
                            ))
                        """
                    )
                if "history_hidden" not in columns:
                    connection.execute(
                        """
                        ALTER TABLE task_queue
                        ADD COLUMN history_hidden INTEGER NOT NULL DEFAULT 0
                            CHECK (history_hidden IN (0, 1))
                        """
                    )
                if "control_state" not in columns:
                    connection.execute(
                        """
                        ALTER TABLE task_queue
                        ADD COLUMN control_state TEXT NOT NULL DEFAULT 'active'
                            CHECK (control_state IN (
                                'active', 'pause_requested', 'paused',
                                'resume_requested'
                            ))
                        """
                    )
                if "control_error" not in columns:
                    connection.execute(
                        "ALTER TABLE task_queue "
                        "ADD COLUMN control_error TEXT NOT NULL DEFAULT ''"
                    )
                if "control_changed_at" not in columns:
                    connection.execute(
                        "ALTER TABLE task_queue ADD COLUMN control_changed_at TEXT"
                    )
                if "downloaded_artifacts_json" not in columns:
                    connection.execute(
                        "ALTER TABLE task_queue ADD COLUMN "
                        "downloaded_artifacts_json TEXT NOT NULL DEFAULT '{}'"
                    )
                if "downloaded_at" not in columns:
                    connection.execute(
                        "ALTER TABLE task_queue ADD COLUMN downloaded_at TEXT"
                    )
                retention_column_added = "delivery_retention_started_at" not in columns
                if retention_column_added:
                    connection.execute(
                        "ALTER TABLE task_queue ADD COLUMN "
                        "delivery_retention_started_at TEXT"
                    )
                if "delivery_expired_at" not in columns:
                    connection.execute(
                        "ALTER TABLE task_queue ADD COLUMN delivery_expired_at TEXT"
                    )
                if "delivery_expired_reason" not in columns:
                    connection.execute(
                        "ALTER TABLE task_queue ADD COLUMN "
                        "delivery_expired_reason TEXT NOT NULL DEFAULT ''"
                    )
                connection.execute(
                    "UPDATE task_queue SET downloaded_artifacts_json='{}' "
                    "WHERE downloaded_artifacts_json IS NULL "
                    "OR TRIM(downloaded_artifacts_json)=''"
                )
                if retention_column_added:
                    # Pre-upgrade downloads were never recorded.  Giving every
                    # existing terminal row a fresh 24-hour anchor avoids an
                    # unsafe first-start purge of old but still-needed videos.
                    migration_now = _now_text()
                    terminal_placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
                    connection.execute(
                        f"""
                        UPDATE task_queue
                        SET delivery_retention_started_at=CASE
                            WHEN finished_at>? THEN finished_at ELSE ? END
                        WHERE delivery_retention_started_at IS NULL
                          AND finished_at IS NOT NULL
                          AND status IN ({terminal_placeholders})
                        """,
                        (
                            migration_now,
                            migration_now,
                            *tuple(sorted(TERMINAL_STATUSES)),
                        ),
                    )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_queue_status_sequence "
                    "ON task_queue(status, sequence)"
                )
                # Releases before multi-user support used a globally unique
                # submission key.  Drop that index before replacing it with
                # owner-scoped idempotency.  Existing rows remain NULL-owned
                # and are never assigned to the first person who signs in.
                connection.execute(
                    "DROP INDEX IF EXISTS idx_task_queue_submission_key"
                )
                connection.execute(
                    "UPDATE task_queue SET owner_user_id=NULL "
                    "WHERE owner_user_id IS NOT NULL AND TRIM(owner_user_id)=''"
                )
                connection.execute(
                    "UPDATE task_queue SET tenant_id=NULL "
                    "WHERE tenant_id IS NOT NULL AND TRIM(tenant_id)=''"
                )
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "idx_task_queue_owner_submission_key "
                    "ON task_queue(owner_user_id, submission_key) "
                    "WHERE owner_user_id IS NOT NULL "
                    "AND submission_key IS NOT NULL"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS "
                    "idx_task_queue_owner_status_sequence "
                    "ON task_queue(owner_user_id, status, sequence)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_queue_owner_task "
                    "ON task_queue(owner_user_id, task_id)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS "
                    "idx_task_queue_tenant_owner_status_sequence "
                    "ON task_queue(tenant_id, owner_user_id, status, sequence)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS "
                    "idx_task_queue_delivery_retention "
                    "ON task_queue(delivery_expired_at, downloaded_at, "
                    "delivery_retention_started_at, sequence)"
                )
                connection.execute("COMMIT")
            self._ready = True

    @staticmethod
    def _decode_row(row, include_config=False):
        if row is None:
            return None
        item = dict(row)
        try:
            item['auto_fill'] = json.loads(item.pop('auto_fill_json', '{}') or '{}')
        except (ValueError, TypeError):
            item['auto_fill'] = {}
        item["legacy_unassigned"] = not bool(
            str(item.get("owner_user_id") or "").strip()
        )
        raw_downloaded_artifacts = item.pop("downloaded_artifacts_json", "{}")
        try:
            downloaded_artifacts = json.loads(raw_downloaded_artifacts or "{}")
        except (TypeError, ValueError):
            downloaded_artifacts = {}
        item["downloaded_artifacts"] = _normalized_download_artifact_receipts(
            downloaded_artifacts,
            fallback_timestamp=item.get("downloaded_at"),
        )
        item["retention_anchor_at"] = item.get(
            "delivery_retention_started_at"
        )
        item["output_retention_state"] = (
            "expired" if item.get("delivery_expired_at") else "active"
        )
        item["output_retention_reason"] = str(
            item.get("delivery_expired_reason") or ""
        )
        try:
            item["result"] = json.loads(item.pop("result_json") or "{}")
        except (TypeError, ValueError):
            item["result"] = {}
        raw_config = item.pop("config_json", "{}")
        try:
            decoded_config = json.loads(raw_config or "{}")
        except (TypeError, ValueError):
            decoded_config = None
        try:
            target = int((decoded_config or {}).get('fission_count') or 1)
        except (TypeError, ValueError, AttributeError):
            target = 1
        if target > 1 and not item['auto_fill'] and item.get('status') == 'waiting':
            item['auto_fill'] = dict(version='8020-autofill-20260927.1', task_id=item['task_id'],
                target_count=target, completed_count=0, remaining_count=target, retry_round=0,
                max_retry_rounds=3, retry_authority='local_server', state='queued',
                outputs=[], failure_reason='', message=f'已完成 0/{target}，等待制作')
        if item.get('status') == 'waiting' and item['auto_fill']:
            fill = item['auto_fill']
            fill['state'] = 'auto_fill_queued' if fill.get('retry_round') else 'queued'
            fill['message'] = f"已完成 {fill['completed_count']}/{fill['target_count']}，等待制作或恢复缺少部分"
        item["summary"] = _public_task_summary(decoded_config)
        if include_config:
            item["config"] = decoded_config
        else:
            item.pop("submission_key", None)
        return item

    def update_auto_fill(self, task_id, metadata):
        if metadata.get('task_id') != task_id:
            raise ValueError('补做状态必须归属原任务')
        encoded = json.dumps(metadata, ensure_ascii=False, separators=(',', ':'))
        self.ensure_ready()
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE task_queue SET auto_fill_json=? WHERE task_id=? AND status='running'",
                (encoded, str(task_id)))
            return bool(changed.rowcount)

    def recover_interrupted(self):
        """Resolve tasks abandoned by a previous service process conservatively."""
        self.ensure_ready()
        finished_at = _now_text()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Called only during existing tenant cold-start recovery under its
            # instance lock. Renderer checkpoint also holds an OS task lock.
            fill_recovered = 0
            for row in connection.execute(
                "SELECT task_id,auto_fill_json FROM task_queue WHERE status='running' AND control_state='active'").fetchall():
                try:
                    fill = json.loads(row['auto_fill_json'] or '{}')
                except (ValueError, TypeError):
                    continue
                if (fill.get('version') == '8020-autofill-20260927.1'
                        and fill.get('state') in ('rendering', 'completed')):
                    connection.execute(
                        "UPDATE task_queue SET status='waiting',started_at=NULL,finished_at=NULL,"
                        "stage='重启后正在核对成片，只恢复缺少部分' WHERE task_id=? AND status='running'",
                        (row['task_id'],))
                    fill_recovered += 1
            interrupted = connection.execute(
                """
                UPDATE task_queue
                SET status='interrupted', finished_at=?,
                    delivery_retention_started_at=COALESCE(
                        delivery_retention_started_at, ?
                    ),
                    stage='服务重启，本条已跳过',
                    error='服务在任务运行期间重启；为避免重复制作或重复扣费，本条未自动重跑。',
                    control_state='active', control_error='',
                    control_changed_at=?
                WHERE status IN ('running', 'skip_requested')
                """,
                (finished_at, finished_at, finished_at),
            )
            recovered_finalizing = 0
            finalizing_rows = connection.execute(
                "SELECT task_id, billing_state, result_json FROM task_queue "
                "WHERE status='finalizing'"
            ).fetchall()
            for row in finalizing_rows:
                try:
                    result = json.loads(row["result_json"] or "{}")
                except (TypeError, ValueError):
                    result = {}
                auto_save = result.get("auto_save", {}) if isinstance(result, dict) else {}
                if (
                    isinstance(auto_save, dict)
                    and auto_save.get("enabled") is True
                    and auto_save.get("status") == "pending"
                ):
                    # The web worker resumes this idempotent file copy before
                    # settling the preserved output into a terminal state.
                    continue
                if row["billing_state"] in {"confirmed", "not_required"}:
                    cursor = connection.execute(
                        """
                        UPDATE task_queue
                        SET status='succeeded', finished_at=?,
                            delivery_retention_started_at=COALESCE(
                                delivery_retention_started_at, ?
                            ),
                            stage='服务重启，已确认成品和计费状态', error='',
                            control_state='active', control_error='',
                            control_changed_at=?
                        WHERE task_id=? AND status='finalizing'
                        """,
                        (finished_at, finished_at, finished_at, row["task_id"]),
                    )
                else:
                    cursor = connection.execute(
                        """
                        UPDATE task_queue
                        SET status='review_required', finished_at=?,
                            delivery_retention_started_at=COALESCE(
                                delivery_retention_started_at, ?
                            ),
                            stage='服务重启，本条需要人工核对',
                            error='成品已经生成，但服务在收尾或计费确认期间重启；为避免重复扣费，本条不可自动重试。',
                            control_state='active', control_error='',
                            control_changed_at=?
                        WHERE task_id=? AND status='finalizing'
                        """,
                        (finished_at, finished_at, finished_at, row["task_id"]),
                    )
                recovered_finalizing += int(cursor.rowcount or 0)
            connection.execute("COMMIT")
            return int(interrupted.rowcount or 0) + recovered_finalizing + fill_recovered

    @staticmethod
    def _normalize_submission_key(submission_key):
        if submission_key is None:
            return None
        normalized = str(submission_key).strip()
        return normalized or None

    def _enqueue(
        self,
        title,
        config,
        task_id=None,
        submission_key=None,
        *,
        owner_user_id,
        tenant_id=None,
        allow_unassigned=False,
    ):
        self.ensure_ready()
        task_id = str(task_id or uuid.uuid4().hex)
        submission_key = self._normalize_submission_key(submission_key)
        owner_user_id = _normalized_identity(
            owner_user_id,
            "owner_user_id",
            required=not allow_unassigned,
        )
        tenant_id = _normalized_identity(
            tenant_id,
            "tenant_id",
            required=False,
        )
        if owner_user_id is None and tenant_id is not None:
            raise ValueError("an unassigned legacy task cannot have a tenant_id")
        created_at = _queue_created_at_text()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if submission_key is not None:
                if owner_user_id is None:
                    existing = connection.execute(
                        "SELECT * FROM task_queue "
                        "WHERE owner_user_id IS NULL AND submission_key=?",
                        (submission_key,),
                    ).fetchone()
                else:
                    existing = connection.execute(
                        "SELECT * FROM task_queue "
                        "WHERE owner_user_id=? AND submission_key=?",
                        (owner_user_id, submission_key),
                    ).fetchone()
                if existing is not None:
                    connection.execute("COMMIT")
                    return self._decode_row(existing, include_config=True)
            # Placement belongs to a single running task. Never inherit a
            # source task's pin (or a client-supplied one) into a fresh queue
            # row; its first publication chooses and persists its own root.
            # Keep caller dictionaries and deduplicated existing rows intact.
            new_config = dict(config) if isinstance(config, dict) else config
            if isinstance(new_config, dict):
                new_config.pop("server_output_placement", None)
            config_json = json.dumps(
                new_config,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            connection.execute(
                """
                INSERT INTO task_queue (
                    task_id, owner_user_id, tenant_id, submission_key,
                    status, title, config_json, created_at, stage, billing_state
                ) VALUES (
                    ?, ?, ?, ?, 'waiting', ?, ?, ?, '等待制作', 'not_started'
                )
                """,
                (
                    task_id,
                    owner_user_id,
                    tenant_id,
                    submission_key,
                    str(title),
                    config_json,
                    created_at,
                ),
            )
            inserted = connection.execute(
                "SELECT * FROM task_queue WHERE task_id=?",
                (task_id,),
            ).fetchone()
            connection.execute("COMMIT")
        return self._decode_row(inserted, include_config=True)

    def enqueue(
        self,
        title,
        config,
        task_id=None,
        submission_key=None,
        *,
        owner_user_id,
        tenant_id=None,
    ):
        """Enqueue an authenticated user's task.

        ``owner_user_id`` is keyword-only and mandatory so a public call site
        cannot accidentally create another machine-global task.  ``tenant_id``
        is optional metadata; callers that have it should always provide it.
        """
        return self._enqueue(
            title,
            config,
            task_id=task_id,
            submission_key=submission_key,
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            allow_unassigned=False,
        )

    def enqueue_legacy_internal(
        self,
        title,
        config,
        task_id=None,
        submission_key=None,
    ):
        """Explicit compatibility path for trusted migration code only."""
        return self._enqueue(
            title,
            config,
            task_id=task_id,
            submission_key=submission_key,
            owner_user_id=None,
            tenant_id=None,
            allow_unassigned=True,
        )

    @staticmethod
    def _prepare_auto_fill_claim(connection, row):
        try:
            config = json.loads(row['config_json'] or '{}')
            target = int(config.get('fission_count') or 1)
        except (ValueError, TypeError, AttributeError):
            return
        if target <= 1:
            return
        metadata = json.loads(row['auto_fill_json'] or '{}')
        if not metadata:
            metadata = dict(version='8020-autofill-20260927.1', task_id=row['task_id'],
                            target_count=target, completed_count=0, remaining_count=target,
                            retry_round=0, max_retry_rounds=3, retry_authority='local_server',
                            state='rendering', outputs=[], failure_reason='', message=f'已完成 0/{target}')
            connection.execute('UPDATE task_queue SET auto_fill_json=? WHERE task_id=?',
                               (json.dumps(metadata, ensure_ascii=False), row['task_id']))

    def _claim_next(self, *, legacy_unassigned=False):
        """Atomically claim one task for an internal renderer worker.

        ``BEGIN IMMEDIATE`` serializes the short select/update transaction, so
        two worker processes/threads cannot claim the same row.  Rendering is
        performed after this method commits and therefore remains concurrent.
        """
        self.ensure_ready()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner_clause = (
                "owner_user_id IS NULL"
                if legacy_unassigned
                else "owner_user_id IS NOT NULL"
            )
            row = connection.execute(
                "SELECT * FROM task_queue WHERE status='waiting' AND "
                f"{owner_clause} ORDER BY sequence ASC LIMIT 1"
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            started_at = _now_text()
            changed = connection.execute(
                """
                UPDATE task_queue
                SET status='running', started_at=?, finished_at=NULL,
                    stage='正在启动', error='', control_state='active',
                    control_error='', control_changed_at=?
                WHERE task_id=? AND status='waiting'
                """,
                (started_at, started_at, row["task_id"]),
            ).rowcount
            if changed != 1:
                connection.execute("ROLLBACK")
                return None
            self._prepare_auto_fill_claim(connection, row)
            claimed = connection.execute(
                "SELECT * FROM task_queue WHERE task_id=?",
                (row["task_id"],),
            ).fetchone()
            connection.execute("COMMIT")
        return self._decode_row(claimed, include_config=True)

    def claim_next(self):
        """Claim the next authenticated task; legacy-unassigned rows stay parked."""
        return self._claim_next(legacy_unassigned=False)

    def claim_next_legacy_internal(self):
        """Explicit migration-only claim path for a NULL-owned legacy row."""
        return self._claim_next(legacy_unassigned=True)

    def waiting_head_owned(self, owner_user_id, *, tenant_id):
        """Return only one authenticated tenant's oldest waiting identity.

        The global FIFO coordinator needs scheduling metadata, never the saved
        configuration or result.  Requiring both owner and tenant prevents a
        caller from using a valid owner id to inspect a row in another tenant.
        """

        self.ensure_ready()
        owner_user_id = _normalized_identity(owner_user_id, "owner_user_id")
        tenant_id = _normalized_identity(tenant_id, "tenant_id")
        with self._connect(CONTROL_DB_TIMEOUT_SECONDS) as connection:
            row = connection.execute(
                """
                SELECT sequence, task_id, owner_user_id, tenant_id,
                       created_at, status
                FROM task_queue
                WHERE status='waiting'
                  AND owner_user_id=?
                  AND tenant_id=?
                ORDER BY sequence ASC
                LIMIT 1
                """,
                (owner_user_id, tenant_id),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["fifo_stable_key"] = (
            f"{int(item['sequence']):020d}:"
            f"{owner_user_id}:{tenant_id}:{item['task_id']}"
        )
        return item

    def claim_waiting_exact_owned(
        self,
        owner_user_id,
        task_id,
        *,
        tenant_id,
    ):
        """Atomically claim one exact waiting row inside its owner boundary.

        Unlike ``claim_next``, this method cannot silently advance to another
        row when the advertised global FIFO head was canceled or claimed by a
        competing worker.  A mismatch returns ``None`` and has no side effect.
        """

        self.ensure_ready()
        owner_user_id = _normalized_identity(owner_user_id, "owner_user_id")
        tenant_id = _normalized_identity(tenant_id, "tenant_id")
        task_id = str(task_id or "").strip()
        if not task_id:
            raise ValueError("task_id is required")
        if len(task_id) > MAX_IDENTITY_LENGTH or any(
            ord(character) < 32 or ord(character) == 127 for character in task_id
        ):
            raise ValueError("task_id is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM task_queue
                WHERE task_id=? AND status='waiting'
                  AND owner_user_id=? AND tenant_id=?
                """,
                (task_id, owner_user_id, tenant_id),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            started_at = _now_text()
            changed = connection.execute(
                """
                UPDATE task_queue
                SET status='running', started_at=?, finished_at=NULL,
                    stage='正在启动', error='', control_state='active',
                    control_error='', control_changed_at=?
                WHERE task_id=? AND status='waiting'
                  AND owner_user_id=? AND tenant_id=?
                """,
                (
                    started_at,
                    started_at,
                    task_id,
                    owner_user_id,
                    tenant_id,
                ),
            ).rowcount
            if changed != 1:
                connection.execute("ROLLBACK")
                return None
            self._prepare_auto_fill_claim(connection, row)
            claimed = connection.execute(
                """
                SELECT * FROM task_queue
                WHERE task_id=? AND owner_user_id=? AND tenant_id=?
                """,
                (task_id, owner_user_id, tenant_id),
            ).fetchone()
            connection.execute("COMMIT")
        return self._decode_row(claimed, include_config=True)

    def get_internal(self, task_id, include_config=False):
        """Unscoped lookup for trusted renderer/recovery code only."""
        self.ensure_ready()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_queue WHERE task_id=?",
                (str(task_id),),
            ).fetchone()
        return self._decode_row(row, include_config=include_config)

    def get(self, task_id, include_config=False):
        """Backward-compatible internal alias; public routes must use get_owned."""
        return self.get_internal(task_id, include_config=include_config)

    def get_owned(
        self,
        owner_user_id,
        task_id,
        include_config=False,
        *,
        tenant_id=None,
    ):
        """Return an owned row, hiding foreign and nonexistent ids identically."""
        self.ensure_ready()
        owner_user_id = _normalized_identity(owner_user_id, "owner_user_id")
        tenant_id = _normalized_identity(
            tenant_id,
            "tenant_id",
            required=False,
        )
        query = (
            "SELECT * FROM task_queue "
            "WHERE task_id=? AND owner_user_id=?"
        )
        parameters = [str(task_id), owner_user_id]
        if tenant_id is not None:
            query += " AND tenant_id=?"
            parameters.append(tenant_id)
        with self._connect() as connection:
            row = connection.execute(query, tuple(parameters)).fetchone()
        return self._decode_row(row, include_config=include_config)

    def local_save_status_owned(self, owner_user_id, task_ids, *, tenant_id):
        """Bounded metadata-only lookup; never read video bytes or expose config."""
        self.ensure_ready()
        owner = _normalized_identity(owner_user_id, "owner_user_id")
        tenant = _normalized_identity(tenant_id, "tenant_id")
        if (not isinstance(task_ids, list) or not 1 <= len(task_ids) <= 200
                or any(not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key) for key in task_ids)
                or len(set(task_ids)) != len(task_ids)):
            raise ValueError("自动保存查询必须包含1至200个不同任务编号。")
        placeholders = ','.join('?' for _ in task_ids)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT task_id, status, delivery_expired_at FROM task_queue "
                f"WHERE owner_user_id=? AND tenant_id=? AND task_id IN ({placeholders})",
                (owner, tenant, *task_ids),
            ).fetchall()
        found = {row['task_id']: row for row in rows}
        return [dict(task_id=key, status=found[key]['status'], expired=bool(found[key]['delivery_expired_at']))
                if key in found else dict(task_id=key, status='unavailable', expired=False)
                for key in task_ids]

    def get_control(self, task_id):
        """Read a live control row without blocking a renderer watchdog."""
        self.ensure_ready()
        with self._connect(CONTROL_DB_TIMEOUT_SECONDS) as connection:
            row = connection.execute(
                "SELECT * FROM task_queue WHERE task_id=?",
                (str(task_id),),
            ).fetchone()
        return self._decode_row(row, include_config=False)

    def get_by_submission_key_owned(
        self,
        owner_user_id,
        submission_key,
        include_config=False,
        *,
        tenant_id=None,
    ):
        """Resolve an idempotent submission inside one authenticated owner."""
        self.ensure_ready()
        owner_user_id = _normalized_identity(owner_user_id, "owner_user_id")
        tenant_id = _normalized_identity(
            tenant_id,
            "tenant_id",
            required=False,
        )
        submission_key = self._normalize_submission_key(submission_key)
        if submission_key is None:
            return None
        query = (
            "SELECT * FROM task_queue "
            "WHERE owner_user_id=? AND submission_key=?"
        )
        parameters = [owner_user_id, submission_key]
        if tenant_id is not None:
            query += " AND tenant_id=?"
            parameters.append(tenant_id)
        with self._connect() as connection:
            row = connection.execute(query, tuple(parameters)).fetchone()
        return self._decode_row(row, include_config=include_config)

    def get_by_submission_key(self, submission_key, include_config=False):
        """Legacy-unassigned lookup for trusted migration compatibility only."""
        self.ensure_ready()
        submission_key = self._normalize_submission_key(submission_key)
        if submission_key is None:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_queue "
                "WHERE owner_user_id IS NULL AND submission_key=?",
                (submission_key,),
            ).fetchone()
        return self._decode_row(row, include_config=include_config)

    def asset_set_references(
        self,
        asset_set_id=None,
        *,
        owner_user_id=None,
        tenant_id=None,
    ):
        """Return queue rows that reference task-private frozen materials.

        Only the opaque asset-set id and lifecycle status leave this method;
        the saved task configuration (which can contain customer details and
        material filenames) remains private.  Authenticated maintenance must
        provide ``owner_user_id`` (and ``tenant_id`` when available).  The
        explicit unscoped form remains available only for trusted legacy
        maintenance and deliberately includes old NULL-owner rows.
        """
        self.ensure_ready()
        requested_id = None
        if asset_set_id is not None:
            requested_id = _normalized_asset_set_id(asset_set_id)
            if requested_id is None:
                return []
        scope_sql = ""
        scope_parameters = ()
        if owner_user_id is not None:
            owner_user_id = _normalized_identity(
                owner_user_id,
                "owner_user_id",
            )
            tenant_id = _normalized_identity(
                tenant_id,
                "tenant_id",
                required=False,
            )
            scope_sql = " WHERE owner_user_id=?"
            scope_parameters = (owner_user_id,)
            if tenant_id is not None:
                scope_sql += " AND tenant_id=?"
                scope_parameters += (tenant_id,)
        elif tenant_id is not None:
            raise ValueError(
                "tenant_id cannot scope asset references without owner_user_id"
            )
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * "
                "FROM task_queue "
                f"{scope_sql} ORDER BY sequence ASC",
                scope_parameters,
            ).fetchall()
        references = []
        for row in rows:
            raw_config = str(row["config_json"] or "")
            try:
                config = json.loads(raw_config or "{}")
            except (TypeError, ValueError):
                config = None
            candidate_id = None
            if isinstance(config, dict):
                try:
                    config_version = int(config.get("config_version", 1) or 1)
                except (TypeError, ValueError, OverflowError):
                    config_version = 0
                if config_version >= 2:
                    candidate_id = _normalized_asset_set_id(
                        config.get("asset_set_id")
                    )
            if candidate_id is not None and (
                requested_id is None or candidate_id == requested_id
            ):
                references.append(
                    {
                        "asset_set_id": candidate_id,
                        "task_id": str(row["task_id"]),
                        "status": str(row["status"]),
                        "finished_at": row["finished_at"],
                        "conservative": not _handoff_cleanup_allowed(dict(row)),
                    }
                )
                continue
            if (
                requested_id is not None
                and requested_id in raw_config.lower()
            ):
                # Fail closed for a damaged row: if its raw persisted text still
                # mentions the exact opaque id, the collector must treat it as
                # an unresolved reference instead of deleting underneath it.
                references.append(
                    {
                        "asset_set_id": requested_id,
                        "task_id": str(row["task_id"]),
                        "status": str(row["status"]),
                        "finished_at": row["finished_at"],
                        "conservative": True,
                    }
                )
        return references

    def update_stage(self, task_id, stage):
        self.ensure_ready()
        with self._connect() as connection:
            connection.execute(
                "UPDATE task_queue SET stage=? "
                "WHERE task_id=? AND status IN ('running', 'skip_requested', 'finalizing')",
                (str(stage or ""), str(task_id)),
            )

    def update_running_result(self, task_id, result, stage=None):
        """Checkpoint accepted artifacts without changing live task control state."""
        self.ensure_ready()
        result_json = json.dumps(
            {} if result is None else result,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._connect() as connection:
            if stage is None:
                cursor = connection.execute(
                    """
                    UPDATE task_queue
                    SET result_json=?
                    WHERE task_id=? AND status IN ('running', 'skip_requested')
                    """,
                    (result_json, str(task_id)),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE task_queue
                    SET result_json=?, stage=?
                    WHERE task_id=? AND status IN ('running', 'skip_requested')
                    """,
                    (result_json, str(stage or ""), str(task_id)),
                )
            return int(cursor.rowcount or 0) == 1

    def finish(self, task_id, status, stage, error="", result=None):
        if status not in TERMINAL_STATUSES:
            raise ValueError("invalid terminal queue status")
        self.ensure_ready()
        result_json = None
        if result is not None:
            result_json = json.dumps(
                result,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        finished_at = _now_text()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            billing_guard = (
                " AND billing_state IN ('confirmed', 'not_required')"
                if status == "succeeded"
                else ""
            )
            if result_json is None:
                cursor = connection.execute(
                    f"""
                    UPDATE task_queue
                    SET status=?, finished_at=?, stage=?, error=?,
                        delivery_retention_started_at=COALESCE(
                            delivery_retention_started_at, ?
                        ),
                        control_state='active', control_error='',
                        control_changed_at=?
                    WHERE task_id=?
                      AND status IN ('running', 'skip_requested', 'finalizing')
                      {billing_guard}
                    """,
                    (
                        status,
                        finished_at,
                        str(stage or ""),
                        str(error or ""),
                        finished_at,
                        finished_at,
                        str(task_id),
                    ),
                )
            else:
                cursor = connection.execute(
                    f"""
                    UPDATE task_queue
                    SET status=?, finished_at=?, stage=?, error=?, result_json=?,
                        delivery_retention_started_at=COALESCE(
                            delivery_retention_started_at, ?
                        ),
                        control_state='active', control_error='',
                        control_changed_at=?
                    WHERE task_id=?
                      AND status IN ('running', 'skip_requested', 'finalizing')
                      {billing_guard}
                    """,
                    (
                        status,
                        finished_at,
                        str(stage or ""),
                        str(error or ""),
                        result_json,
                        finished_at,
                        finished_at,
                        str(task_id),
                    ),
                )
            if int(cursor.rowcount or 0) != 1:
                connection.execute("ROLLBACK")
                return None
            fill_row = connection.execute('SELECT auto_fill_json FROM task_queue WHERE task_id=?', (str(task_id),)).fetchone()
            fill = json.loads(fill_row['auto_fill_json'] or '{}')
            if fill and status in {'failed', 'timed_out', 'skipped', 'canceled', 'interrupted'}:
                fill['state'] = 'fill_failed' if status in {'failed', 'timed_out', 'interrupted'} else 'canceled'
                fill['failure_reason'] = str(error or stage or '')
                fill['message'] = f"已完成 {fill['completed_count']}/{fill['target_count']}，还差 {fill['remaining_count']} 条；" + fill['failure_reason']
                connection.execute('UPDATE task_queue SET auto_fill_json=? WHERE task_id=?',
                                   (json.dumps(fill, ensure_ascii=False), str(task_id)))
            finished = connection.execute(
                "SELECT * FROM task_queue WHERE task_id=?",
                (str(task_id),),
            ).fetchone()
            connection.execute("COMMIT")
        return self._decode_row(finished)

    @staticmethod
    def _delivery_scope_clause(owner_user_id=None, tenant_id=None):
        scope_sql = ""
        scope_parameters = ()
        if owner_user_id is not None:
            owner_user_id = _normalized_identity(
                owner_user_id,
                "owner_user_id",
            )
            tenant_id = _normalized_identity(
                tenant_id,
                "tenant_id",
                required=False,
            )
            scope_sql = " AND owner_user_id=?"
            scope_parameters = (owner_user_id,)
            if tenant_id is not None:
                scope_sql += " AND tenant_id=?"
                scope_parameters += (tenant_id,)
        elif tenant_id is not None:
            raise ValueError(
                "tenant_id cannot scope delivery retention without owner_user_id"
            )
        return scope_sql, scope_parameters

    def _record_downloaded_artifacts(
        self,
        task_id,
        artifact_keys,
        *,
        owner_user_id=None,
        tenant_id=None,
        completed_at=None,
    ):
        """Atomically merge fully-sent MP4s and settle the first full download.

        Callers must invoke this only after a response iterator reaches EOF.
        A multi-video task gets ``downloaded_at`` only when every MP4 named by
        its durable result has been recorded.  Repeated downloads are
        idempotent and never extend the original delivery-retention window.
        """
        self.ensure_ready()
        submitted_keys = set(
            _normalized_delivery_artifact_keys(artifact_keys, required=True)
        )
        scope_sql, scope_parameters = self._delivery_scope_clause(
            owner_user_id,
            tenant_id,
        )
        completed_at_text = _coerce_timestamp_text(completed_at)
        terminal_placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT * FROM task_queue WHERE task_id=?{scope_sql}",
                (str(task_id), *scope_parameters),
            ).fetchone()
            if (
                row is None
                or str(row["status"]) not in TERMINAL_STATUSES
                or row["finished_at"] is None
                or row["delivery_expired_at"] is not None
            ):
                connection.execute("ROLLBACK")
                return None
            try:
                result = json.loads(row["result_json"] or "{}")
            except (TypeError, ValueError):
                result = {}
            required_keys = _required_delivery_video_keys(result)
            if not required_keys:
                connection.execute("ROLLBACK")
                return None
            required_set = set(required_keys)
            unknown_keys = submitted_keys - required_set
            if unknown_keys:
                connection.execute("ROLLBACK")
                raise ValueError("downloaded artifact is not part of this task")
            try:
                existing_values = json.loads(
                    row["downloaded_artifacts_json"] or "{}"
                )
            except (TypeError, ValueError):
                existing_values = {}
            receipts = _normalized_download_artifact_receipts(
                existing_values,
                fallback_timestamp=row["downloaded_at"],
            )
            receipts = {
                key: timestamp
                for key, timestamp in receipts.items()
                if key in required_set
            }
            for key in submitted_keys:
                # Keep the first successful full response so repeated clicks
                # cannot indefinitely extend server retention.
                receipts.setdefault(key, completed_at_text)
            ordered_receipts = {
                key: receipts[key] for key in required_keys if key in receipts
            }
            all_required_downloaded = required_set.issubset(ordered_receipts)
            full_downloaded_at = (
                max(ordered_receipts[key] for key in required_keys)
                if all_required_downloaded
                else completed_at_text
            )
            merged_json = json.dumps(
                ordered_receipts,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            connection.execute(
                f"""
                UPDATE task_queue
                SET downloaded_artifacts_json=?,
                    delivery_retention_started_at=COALESCE(
                        delivery_retention_started_at, finished_at, ?
                    ),
                    downloaded_at=CASE
                        WHEN ? THEN COALESCE(downloaded_at, ?)
                        ELSE downloaded_at
                    END
                WHERE task_id=?
                  AND status IN ({terminal_placeholders})
                  AND delivery_expired_at IS NULL
                  {scope_sql}
                """,
                (
                    merged_json,
                    completed_at_text,
                    1 if all_required_downloaded else 0,
                    full_downloaded_at,
                    str(task_id),
                    *tuple(sorted(TERMINAL_STATUSES)),
                    *scope_parameters,
                ),
            )
            updated = connection.execute(
                f"SELECT * FROM task_queue WHERE task_id=?{scope_sql}",
                (str(task_id), *scope_parameters),
            ).fetchone()
            connection.execute("COMMIT")
        return self._decode_row(updated)

    def pin_server_output_placement(
        self, task_id, owner_user_id, tenant_id, root, placement,
    ):
        """Pin one server-selected D/E destination without changing queue state.

        This is a server-internal CAS, not a client-supplied configuration API.
        Only the already-running task or a terminal task with deliverable
        results can be pinned. A successful prior placement is immutable.
        """
        task_id = str(task_id or "")
        owner_user_id = str(owner_user_id or "")
        if not re.fullmatch(r"[0-9a-f]{32}", task_id) or not re.fullmatch(r"[0-9a-f]{32}", owner_user_id):
            return False
        allowed = {
            os.path.normcase(os.path.abspath(str(Path(drive + ":/快手自动化GPU服务器8020成片") / owner_user_id)))
            for drive in ("D", "E")
        }
        normalized_root = os.path.normcase(os.path.abspath(str(root or "")))
        expected = {
            "version": 1, "task_id": task_id, "owner_user_id": owner_user_id,
            "root": str(root),
        }
        if normalized_root not in allowed or not isinstance(placement, dict) or placement != expected:
            return False
        self.ensure_ready()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status,config_json,result_json FROM task_queue "
                "WHERE task_id=? AND owner_user_id=? AND tenant_id=?",
                (task_id, owner_user_id, str(tenant_id)),
            ).fetchone()
            if row is None:
                return False
            try:
                config = json.loads(row["config_json"] or "{}")
                result = json.loads(row["result_json"] or "{}")
            except (TypeError, ValueError):
                return False
            if not isinstance(config, dict) or not isinstance(result, dict):
                return False
            status = str(row["status"] or "")
            if status not in {"running", "finalizing"} and not (
                status in TERMINAL_STATUSES and _required_delivery_video_keys(result)
            ):
                return False
            previous = config.get("server_output_placement")
            configured_root = os.path.normcase(os.path.abspath(str(config.get("auto_save_directory") or "")))
            if previous is not None:
                return previous == placement and configured_root == normalized_root
            saved = result.get("auto_save")
            if isinstance(saved, dict) and saved.get("status") == "saved" and configured_root != normalized_root:
                return False
            config["auto_save_directory"] = str(root)
            config["server_output_placement"] = dict(placement)
            cursor = connection.execute(
                "UPDATE task_queue SET config_json=? WHERE task_id=? "
                "AND owner_user_id=? AND tenant_id=? AND status=? AND config_json=?",
                (json.dumps(config, ensure_ascii=False), task_id, owner_user_id,
                 str(tenant_id), status, row["config_json"]),
            )
            return int(cursor.rowcount or 0) == 1

    def record_completed_download(
        self,
        task_id,
        artifact_keys,
        *,
        completed_at,
        owner_user_id=None,
        tenant_id=None,
    ):
        """Persist a natural-EOF receipt using the web delivery contract."""
        return bool(
            self._record_downloaded_artifacts(
                task_id,
                artifact_keys,
                owner_user_id=owner_user_id,
                tenant_id=tenant_id,
                completed_at=completed_at,
            )
        )

    def _list_output_retention_candidates(
        self,
        *,
        owner_user_id=None,
        tenant_id=None,
        limit=200,
    ):
        """Return completed deliveries and recovery work, independent of EOF receipts."""
        self.ensure_ready()
        limit = max(1, min(1000, int(limit)))
        scope_sql, scope_parameters = self._delivery_scope_clause(
            owner_user_id,
            tenant_id,
        )
        terminal_placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        # Expired rows are also maintenance work: the web layer may need to
        # finish deleting a tombstone left by a process interruption.  Query
        # active and expired rows independently so a large active backlog can
        # never starve that recovery path.
        recovery_limit = min(limit, 32)
        active_query_limit = min(10000, max(limit, limit * 10))
        expired_query_limit = min(
            10000,
            max(recovery_limit, recovery_limit * 10),
        )
        with self._connect() as connection:
            active_rows = connection.execute(
                f"""
                SELECT * FROM task_queue
                WHERE status IN ({terminal_placeholders})
                  AND delivery_retention_started_at IS NOT NULL
                  AND delivery_expired_at IS NULL
                  AND datetime(finished_at) IS NOT NULL
                  AND result_json LIKE '%"kind"%'
                  {scope_sql}
                ORDER BY
                  COALESCE(
                    delivery_retention_started_at, finished_at, created_at
                  ) ASC,
                  sequence ASC
                LIMIT ?
                """,
                (
                    *tuple(sorted(TERMINAL_STATUSES)),
                    *scope_parameters,
                    active_query_limit,
                ),
            ).fetchall()
            expired_rows = connection.execute(
                f"""
                SELECT * FROM task_queue
                WHERE status IN ({terminal_placeholders})
                  AND delivery_retention_started_at IS NOT NULL
                  AND delivery_expired_at IS NOT NULL
                  AND result_json LIKE '%"kind"%'
                  {scope_sql}
                ORDER BY
                  COALESCE(
                    delivery_expired_at,
                    delivery_retention_started_at,
                    finished_at,
                    created_at
                  ) ASC,
                  sequence ASC
                LIMIT ?
                """,
                (
                    *tuple(sorted(TERMINAL_STATUSES)),
                    *scope_parameters,
                    expired_query_limit,
                ),
            ).fetchall()

        def decode_candidates(rows, result_limit):
            candidates = []
            for row in rows:
                item = self._decode_row(row, include_config=True)
                required_keys = _required_delivery_video_keys(item.get("result"))
                if not required_keys:
                    continue
                item["required_download_artifacts"] = list(required_keys)
                candidates.append(item)
                if len(candidates) >= result_limit:
                    break
            return candidates

        return (
            decode_candidates(active_rows, limit)
            + decode_candidates(expired_rows, recovery_limit)
        )

    def list_output_retention_candidates(
        self,
        *,
        limit=200,
        owner_user_id=None,
        tenant_id=None,
    ):
        """List full task records using the web maintenance contract."""
        return self._list_output_retention_candidates(
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            limit=limit,
        )

    def list_daily_succeeded_output_cleanup_candidates(
        self,
        *,
        limit=200,
        owner_user_id=None,
        tenant_id=None,
        finished_cutoff_at=None,
    ):
        """Return only unexpired, exact-succeeded deliveries for the daily sweep.

        This query intentionally does not share the broad terminal-retention
        result window.  A large retry/review backlog must never hide later
        succeeded rows and make the fixed-time sweep appear complete.  Missing
        retention timestamps are also eligible: the wall-clock sweep is based
        on current status and verified artifacts, not download age.
        """
        self.ensure_ready()
        limit = max(1, min(1000, int(limit)))
        finished_cutoff_text = (
            _coerce_timestamp_text(finished_cutoff_at)
            if finished_cutoff_at is not None
            else "9999-12-31 23:59:59"
        )
        scope_sql, scope_parameters = self._delivery_scope_clause(
            owner_user_id,
            tenant_id,
        )
        candidates = []
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                SELECT * FROM task_queue
                WHERE status='succeeded'
                  AND delivery_expired_at IS NULL
                  AND finished_at IS NOT NULL
                  AND finished_at<=?
                  AND result_json LIKE '%"kind"%'
                  {scope_sql}
                ORDER BY
                  COALESCE(finished_at, created_at) ASC,
                  sequence ASC
                """,
                (finished_cutoff_text, *tuple(scope_parameters)),
            )
            # Invalid/malformed result rows are protected, but they cannot
            # starve valid succeeded rows merely by occupying a fixed SQL LIMIT.
            while len(candidates) < limit:
                rows = cursor.fetchmany(min(200, limit))
                if not rows:
                    break
                for row in rows:
                    item = self._decode_row(row, include_config=True)
                    required_keys = _required_delivery_video_keys(item.get("result"))
                    if not required_keys:
                        continue
                    item["required_download_artifacts"] = list(required_keys)
                    candidates.append(item)
                    if len(candidates) >= limit:
                        break
        return candidates

    def daily_completed_output_cleanup_state(
        self,
        *,
        now,
        initial_due_at,
    ):
        """Return the durable fixed-time sweep state, initializing fail-safe.

        The caller supplies the release's fixed first local-18:00 deadline.
        The timestamp lives beside the queue so restarts neither repeat nor
        postpone a missed sweep; an overdue first initialization is deliberately
        persisted as due so a service that was offline can catch up once.
        """
        self.ensure_ready()
        now_epoch = _timestamp_epoch(now)
        initialized_due_at = _coerce_timestamp_text(initial_due_at)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO queue_maintenance_state (
                    maintenance_key, next_due_at, last_completed_at
                ) VALUES (?, ?, NULL)
                """,
                (
                    DAILY_COMPLETED_OUTPUT_CLEANUP_MAINTENANCE_KEY,
                    initialized_due_at,
                ),
            )
            row = connection.execute(
                """
                SELECT next_due_at, last_completed_at
                FROM queue_maintenance_state
                WHERE maintenance_key=?
                """,
                (DAILY_COMPLETED_OUTPUT_CLEANUP_MAINTENANCE_KEY,),
            ).fetchone()
            connection.execute("COMMIT")
        if row is None:
            raise RuntimeError("daily cleanup state was not persisted")
        next_due_at = _coerce_timestamp_text(row["next_due_at"])
        return {
            "due": now_epoch >= _timestamp_epoch(next_due_at),
            "next_due_at": next_due_at,
            "last_completed_at": row["last_completed_at"],
        }

    def complete_daily_completed_output_cleanup(
        self,
        *,
        completed_at,
        next_due_at,
    ):
        """Advance the durable cycle only after every safe candidate drains."""
        self.ensure_ready()
        completed_epoch = _timestamp_epoch(completed_at)
        completed_at_text = _timestamp_text(completed_epoch)
        next_due_at = _coerce_timestamp_text(next_due_at)
        if _timestamp_epoch(next_due_at) <= completed_epoch:
            raise ValueError("next cleanup deadline must follow completion")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO queue_maintenance_state (
                    maintenance_key, next_due_at, last_completed_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(maintenance_key) DO UPDATE SET
                    next_due_at=excluded.next_due_at,
                    last_completed_at=excluded.last_completed_at
                """,
                (
                    DAILY_COMPLETED_OUTPUT_CLEANUP_MAINTENANCE_KEY,
                    next_due_at,
                    completed_at_text,
                ),
            )
            connection.execute("COMMIT")
        return {
            "next_due_at": next_due_at,
            "last_completed_at": completed_at_text,
        }

    def _mark_delivery_expired(
        self,
        task_id,
        *,
        owner_user_id=None,
        tenant_id=None,
        expired_at=None,
        reason="",
    ):
        """Atomically mark a record only if its current deadline is still due."""
        self.ensure_ready()
        expired_epoch = time.time() if expired_at is None else _timestamp_epoch(expired_at)
        expired_at_text = _timestamp_text(expired_epoch)
        reason_text = " ".join(
            "".join(
                character if ord(character) >= 32 and ord(character) != 127 else " "
                for character in str(reason or "")
            ).split()
        )[:512]
        if not reason_text:
            raise ValueError("output expiry reason is required")
        completed_cutoff = _timestamp_text(
            expired_epoch - COMPLETED_DELIVERY_RETENTION_SECONDS
        )
        scope_sql, scope_parameters = self._delivery_scope_clause(
            owner_user_id,
            tenant_id,
        )
        terminal_placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            handoff_row = connection.execute("SELECT * FROM task_queue WHERE task_id=?", (str(task_id),)).fetchone()
            if handoff_row is None or not _handoff_cleanup_allowed(dict(handoff_row)):
                return False
            cursor = connection.execute(
                f"""
                UPDATE task_queue
                SET delivery_expired_at=?, delivery_expired_reason=?
                WHERE task_id=?
                  AND status IN ({terminal_placeholders})
                  AND delivery_expired_at IS NULL
                  AND delivery_retention_started_at IS NOT NULL
                  AND datetime(finished_at) IS NOT NULL AND datetime(finished_at)<=datetime(?)
                  {scope_sql}
                """,
                (
                    expired_at_text,
                    reason_text,
                    str(task_id),
                    *tuple(sorted(TERMINAL_STATUSES)),
                    completed_cutoff,
                    *scope_parameters,
                ),
            )
            return int(cursor.rowcount or 0) == 1

    def mark_output_expired(
        self,
        task_id,
        *,
        expired_at,
        reason,
        owner_user_id=None,
        tenant_id=None,
    ):
        """Mark a still-due scoped task expired using the web contract."""
        return self._mark_delivery_expired(
            task_id,
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            expired_at=expired_at,
            reason=reason,
        )

    def force_mark_output_expired(
        self,
        task_id,
        *,
        expired_at,
        reason,
        owner_user_id=None,
        tenant_id=None,
        finished_cutoff_at=None,
    ):
        """Mark only an exact succeeded delivery during the daily big sweep.

        Download state never shortens the 48-hour completion-time window.
        ``succeeded`` is the sole deletion whitelist: failed,
        timed-out, interrupted, skipped, canceled and review-required outputs
        can still be needed for retry or settlement and remain protected.  The
        task record itself is never removed.
        """
        self.ensure_ready()
        expired_at_text = _coerce_timestamp_text(expired_at)
        finished_cutoff_text = _coerce_timestamp_text(
            expired_at if finished_cutoff_at is None else finished_cutoff_at
        )
        finished_cutoff_text = min(
            finished_cutoff_text,
            _timestamp_text(_timestamp_epoch(expired_at) - COMPLETED_DELIVERY_RETENTION_SECONDS),
        )
        reason_text = " ".join(
            "".join(
                character if ord(character) >= 32 and ord(character) != 127 else " "
                for character in str(reason or "")
            ).split()
        )[:512]
        if not reason_text:
            raise ValueError("output expiry reason is required")
        scope_sql, scope_parameters = self._delivery_scope_clause(
            owner_user_id,
            tenant_id,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            handoff_row = connection.execute("SELECT * FROM task_queue WHERE task_id=?", (str(task_id),)).fetchone()
            if handoff_row is None or not _handoff_cleanup_allowed(dict(handoff_row)):
                return False
            cursor = connection.execute(
                """
                UPDATE task_queue
                SET delivery_expired_at=?, delivery_expired_reason=?
                WHERE task_id=?
                  AND status='succeeded'
                  AND delivery_expired_at IS NULL
                  AND datetime(finished_at) IS NOT NULL
                  AND datetime(finished_at)<=datetime(?)
                  {scope_sql}
                """.format(scope_sql=scope_sql),
                (
                    expired_at_text,
                    reason_text,
                    str(task_id),
                    finished_cutoff_text,
                    *scope_parameters,
                ),
            )
        return int(cursor.rowcount or 0) == 1

    def request_skip(self, task_id):
        self.ensure_ready()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE task_queue
                SET status='skip_requested', stage='正在停止本条，随后继续下一条'
                WHERE task_id=? AND status='running'
                """,
                (str(task_id),),
            )
            return int(cursor.rowcount or 0) == 1

    def _transition_control(
        self,
        task_id,
        expected_state,
        target_state,
        stage,
        allowed_statuses=("running",),
    ):
        if expected_state not in CONTROL_STATES or target_state not in CONTROL_STATES:
            raise ValueError("invalid queue control state")
        self.ensure_ready()
        statuses = tuple(str(value) for value in allowed_statuses)
        placeholders = ",".join("?" for _ in statuses)
        changed_at = _now_text()
        with self._connect(CONTROL_DB_TIMEOUT_SECONDS) as connection:
            cursor = connection.execute(
                f"""
                UPDATE task_queue
                SET control_state=?, control_error='', control_changed_at=?,
                    stage=?
                WHERE task_id=? AND status IN ({placeholders})
                  AND control_state=?
                """,
                (
                    target_state,
                    changed_at,
                    str(stage or ""),
                    str(task_id),
                    *statuses,
                    expected_state,
                ),
            )
            return int(cursor.rowcount or 0) == 1

    def request_pause(self, task_id):
        return self._transition_control(
            task_id,
            "active",
            "pause_requested",
            "正在暂停制作",
        )

    def confirm_paused(self, task_id):
        return self._transition_control(
            task_id,
            "pause_requested",
            "paused",
            "制作已暂停",
        )

    def request_resume(self, task_id):
        return self._transition_control(
            task_id,
            "paused",
            "resume_requested",
            "正在继续制作",
        )

    def confirm_resumed(self, task_id):
        return self._transition_control(
            task_id,
            "resume_requested",
            "active",
            "正在制作",
        )

    def record_control_failure(self, task_id, message):
        """Return a live task to active after a failed control operation."""
        self.ensure_ready()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE task_queue
                SET control_state='active', control_error=?,
                    control_changed_at=?, stage='暂停控制失败，正在安全停止'
                WHERE task_id=? AND status IN ('running', 'skip_requested')
                """,
                (str(message or "")[:500], _now_text(), str(task_id)),
            )
            return int(cursor.rowcount or 0) == 1

    def normalize_control(self, task_id, stage=None):
        """Clear a transient control state without changing lifecycle status."""
        self.ensure_ready()
        with self._connect() as connection:
            if stage is None:
                cursor = connection.execute(
                    """
                    UPDATE task_queue
                    SET control_state='active', control_error='',
                        control_changed_at=?
                    WHERE task_id=? AND status IN ('running', 'skip_requested')
                    """,
                    (_now_text(), str(task_id)),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE task_queue
                    SET control_state='active', control_error='',
                        control_changed_at=?, stage=?
                    WHERE task_id=? AND status IN ('running', 'skip_requested')
                    """,
                    (_now_text(), str(stage), str(task_id)),
                )
            return int(cursor.rowcount or 0) == 1

    @staticmethod
    def _validate_billing_state(billing_state):
        normalized = str(billing_state or "")
        if normalized not in BILLING_STATES:
            raise ValueError("invalid queue billing state")
        return normalized

    def mark_finalizing(
        self,
        task_id,
        result=None,
        billing_state=DEFAULT_BILLING_STATE,
    ):
        billing_state = self._validate_billing_state(billing_state)
        self.ensure_ready()
        result_json = json.dumps(
            {} if result is None else result,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE task_queue
                SET status='finalizing', stage='成品已生成，正在验收和保存记录',
                    result_json=?, billing_state=?, control_state='active',
                    control_error='', control_changed_at=?
                WHERE task_id=? AND status='running'
                  AND control_state IN ('active', 'pause_requested', 'resume_requested')
                """,
                (result_json, billing_state, _now_text(), str(task_id)),
            )
            return int(cursor.rowcount or 0) == 1

    def update_preserved_result(self, task_id, result, stage=None):
        """Update metadata for an already-preserved output without changing status."""
        self.ensure_ready()
        result_json = json.dumps(
            {} if result is None else result,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._connect() as connection:
            if stage is None:
                cursor = connection.execute(
                    """
                    UPDATE task_queue
                    SET result_json=?
                    WHERE task_id=?
                      AND status IN ('finalizing', 'succeeded', 'review_required')
                    """,
                    (result_json, str(task_id)),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE task_queue
                    SET result_json=?, stage=?
                    WHERE task_id=?
                      AND status IN ('finalizing', 'succeeded', 'review_required')
                    """,
                    (result_json, str(stage or ""), str(task_id)),
                )
            return int(cursor.rowcount or 0) == 1

    @staticmethod
    def _decode_erp_report_outbox_row(row):
        if row is None:
            return None
        item = dict(row)
        raw_payload = item.pop("payload_json", "{}")
        try:
            payload = json.loads(raw_payload or "{}")
        except (TypeError, ValueError):
            payload = {}
        item["payload"] = payload if isinstance(payload, dict) else {}
        # This process marker is only a crash fence.  It is not part of the
        # reporting contract and must not escape through application JSON.
        item.pop("sender_instance", None)
        return item

    def enqueue_erp_report(
        self,
        event_id,
        owner_user_id,
        tenant_id,
        task_id,
        event_key,
        payload,
        *,
        output_name="",
    ):
        """Durably record one never-sent direct ERP event.

        The deterministic event ID is immutable.  Re-enqueueing the same
        event is idempotent, including after confirmation; a collision with
        different content fails closed instead of rewriting audit history.
        """

        event_id = _normalized_hex_identifier(event_id, "event_id")
        owner_user_id = _normalized_hex_identifier(
            owner_user_id, "owner_user_id"
        )
        tenant_id = _normalized_hex_identifier(tenant_id, "tenant_id")
        task_id = _normalized_erp_report_text(task_id, "task_id", 256)
        event_key = _normalized_erp_report_text(event_key, "event_key", 1024)
        output_name = str(output_name or "").strip()
        if len(output_name) > 512 or any(
            ord(character) < 32 or ord(character) == 127
            for character in output_name
        ):
            raise ValueError("output_name is invalid")
        payload_json = _canonical_erp_report_payload(payload)
        self.ensure_ready()
        created_at = _queue_created_at_text()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO erp_report_outbox(
                        event_id, owner_user_id, tenant_id, task_id,
                        event_key, output_name, payload_json, state,
                        attempt_count, last_error, created_at, updated_at,
                        sender_instance
                    ) VALUES(?,?,?,?,?,?,?,'deferred_auth',0,'',?,?, '')
                    ON CONFLICT(event_id) DO NOTHING
                    """,
                    (
                        event_id,
                        owner_user_id,
                        tenant_id,
                        task_id,
                        event_key,
                        output_name,
                        payload_json,
                        created_at,
                        created_at,
                    ),
                )
                inserted = int(cursor.rowcount or 0) == 1
                row = connection.execute(
                    "SELECT * FROM erp_report_outbox WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                if row is None:
                    raise RuntimeError("ERP report outbox insert was not preserved")
                expected = {
                    "owner_user_id": owner_user_id,
                    "tenant_id": tenant_id,
                    "task_id": task_id,
                    "event_key": event_key,
                    "output_name": output_name,
                    "payload_json": payload_json,
                }
                if any(row[key] != value for key, value in expected.items()):
                    raise ValueError("ERP report event_id collision")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        decoded = self._decode_erp_report_outbox_row(row)
        decoded["inserted"] = inserted
        return decoded

    def claim_deferred_erp_report(self, event_id=None):
        """Atomically claim one never-sent event for the current process."""

        if event_id is not None:
            event_id = _normalized_hex_identifier(event_id, "event_id")
        self.ensure_ready()
        attempted_at = _now_text()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if event_id is None:
                    row = connection.execute(
                        """
                        SELECT event_id FROM erp_report_outbox
                        WHERE state='deferred_auth'
                        ORDER BY created_at ASC, event_id ASC
                        LIMIT 1
                        """
                    ).fetchone()
                else:
                    row = connection.execute(
                        """
                        SELECT event_id FROM erp_report_outbox
                        WHERE event_id=? AND state='deferred_auth'
                        """,
                        (event_id,),
                    ).fetchone()
                if row is None:
                    connection.execute("COMMIT")
                    return None
                claimed_event_id = row["event_id"]
                cursor = connection.execute(
                    """
                    UPDATE erp_report_outbox
                    SET state='sending', attempt_count=attempt_count+1,
                        last_error='', updated_at=?, last_attempt_at=?,
                        sender_instance=?
                    WHERE event_id=? AND state='deferred_auth'
                    """,
                    (
                        attempted_at,
                        attempted_at,
                        _ERP_OUTBOX_PROCESS_INSTANCE,
                        claimed_event_id,
                    ),
                )
                if int(cursor.rowcount or 0) != 1:
                    connection.execute("COMMIT")
                    return None
                claimed = connection.execute(
                    "SELECT * FROM erp_report_outbox WHERE event_id=?",
                    (claimed_event_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return self._decode_erp_report_outbox_row(claimed)

    def mark_erp_report_confirmed(self, event_id):
        event_id = _normalized_hex_identifier(event_id, "event_id")
        self.ensure_ready()
        confirmed_at = _now_text()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE erp_report_outbox
                    SET state='confirmed', last_error='', updated_at=?,
                        confirmed_at=?, sender_instance=''
                    WHERE event_id=? AND state='sending'
                    """,
                    (confirmed_at, confirmed_at, event_id),
                )
                row = connection.execute(
                    "SELECT * FROM erp_report_outbox WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        if row is None or row["state"] != "confirmed":
            return None
        return self._decode_erp_report_outbox_row(row)

    def mark_erp_report_deferred_auth(self, event_id, error=""):
        event_id = _normalized_hex_identifier(event_id, "event_id")
        self.ensure_ready()
        updated_at = _now_text()
        last_error = _clean_erp_report_error(error)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE erp_report_outbox
                    SET state='deferred_auth', last_error=?, updated_at=?,
                        sender_instance=''
                    WHERE event_id=?
                      AND state IN ('sending', 'deferred_auth')
                    """,
                    (last_error, updated_at, event_id),
                )
                row = connection.execute(
                    "SELECT * FROM erp_report_outbox WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        if row is None or row["state"] != "deferred_auth":
            return None
        return self._decode_erp_report_outbox_row(row)

    def mark_erp_report_uncertain(self, event_id, error):
        event_id = _normalized_hex_identifier(event_id, "event_id")
        self.ensure_ready()
        updated_at = _now_text()
        last_error = _clean_erp_report_error(error) or (
            "ERP 补传结果不明，禁止自动重试。"
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE erp_report_outbox
                    SET state='uncertain', last_error=?, updated_at=?,
                        sender_instance=''
                    WHERE event_id=? AND state IN ('sending', 'uncertain')
                    """,
                    (last_error, updated_at, event_id),
                )
                row = connection.execute(
                    "SELECT * FROM erp_report_outbox WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        if row is None or row["state"] != "uncertain":
            return None
        return self._decode_erp_report_outbox_row(row)

    def erp_report_outbox_counts(self):
        self.ensure_ready()
        counts = {state: 0 for state in sorted(ERP_REPORT_OUTBOX_STATES)}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM erp_report_outbox "
                "GROUP BY state"
            ).fetchall()
        for row in rows:
            state = str(row["state"] or "")
            if state in counts:
                counts[state] = int(row["count"] or 0)
        counts["total"] = sum(counts.values())
        return counts

    def update_terminal_delivery_config(self, task_id, config):
        """Persist only a terminal task's server-side delivery preferences."""
        config_json = json.dumps(
            {} if config is None else config,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE task_queue
                SET config_json=?
                WHERE task_id=?
                  AND status IN (
                    'succeeded', 'review_required', 'failed', 'timed_out',
                    'skipped', 'canceled', 'interrupted'
                  )
                """,
                (config_json, str(task_id)),
            )
            return int(cursor.rowcount or 0) == 1

    def update_terminal_delivery(self, task_id, config, result):
        """Atomically publish a completed task's new export location/result."""
        config_json = json.dumps(
            {} if config is None else config,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        result_json = json.dumps(
            {} if result is None else result,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE task_queue
                SET config_json=?, result_json=?
                WHERE task_id=?
                  AND status IN ('succeeded', 'review_required')
                """,
                (config_json, result_json, str(task_id)),
            )
            return int(cursor.rowcount or 0) == 1

    def pending_auto_save_tasks(self):
        """Return preserved tasks whose optional folder export was interrupted."""
        self.ensure_ready()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM task_queue
                WHERE status='finalizing'
                ORDER BY sequence ASC
                """
            ).fetchall()
        pending = []
        for row in rows:
            item = self._decode_row(row, include_config=True)
            auto_save = (
                item.get("result", {}).get("auto_save", {})
                if isinstance(item, dict)
                else {}
            )
            if (
                isinstance(auto_save, dict)
                and auto_save.get("enabled") is True
                and auto_save.get("status") == "pending"
            ):
                pending.append(item)
        return pending

    def _transition_billing_state(self, task_id, target_state, allowed_states):
        target_state = self._validate_billing_state(target_state)
        self.ensure_ready()
        allowed_states = tuple(sorted(set(allowed_states)))
        placeholders = ",".join("?" for _ in allowed_states)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"""
                UPDATE task_queue
                SET billing_state=?
                WHERE task_id=? AND status='finalizing'
                  AND billing_state IN ({placeholders})
                """,
                (target_state, str(task_id), *allowed_states),
            )
            if int(cursor.rowcount or 0) == 1:
                connection.execute("COMMIT")
                return True
            row = connection.execute(
                "SELECT status, billing_state FROM task_queue WHERE task_id=?",
                (str(task_id),),
            ).fetchone()
            connection.execute("COMMIT")
        return bool(
            row
            and row["status"] == "finalizing"
            and row["billing_state"] == target_state
        )

    def mark_billing_started(self, task_id):
        return self._transition_billing_state(
            task_id,
            "started",
            {"not_started"},
        )

    def mark_billing_confirmed(self, task_id):
        return self._transition_billing_state(
            task_id,
            "confirmed",
            {"started", "uncertain"},
        )

    def mark_billing_uncertain(self, task_id):
        return self._transition_billing_state(
            task_id,
            "uncertain",
            {"started"},
        )

    def cancel_waiting(self, task_id):
        self.ensure_ready()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE task_queue
                SET status='canceled', finished_at=?, stage='已取消', error=''
                WHERE task_id=? AND status='waiting'
                """,
                (_now_text(), str(task_id)),
            )
            return int(cursor.rowcount or 0) == 1

    def redo_completed_owned(self, owner_user_id, task_id, *, tenant_id,
                             submission_key, validate_config):
        """Append a fresh job; source and shared-asset protection stay atomic.

        The daily asset collector also uses BEGIN IMMEDIATE. It cannot remove
        the last successful asset reference between validation and enqueue.
        """
        self.ensure_ready()
        owner_user_id = _normalized_identity(owner_user_id, 'owner_user_id', required=True)
        tenant_id = _normalized_identity(tenant_id, 'tenant_id', required=True)
        submission_key = self._normalize_submission_key(submission_key)
        if not submission_key:
            raise ValueError('重做操作缺少防重复标识')
        with closing(self._connect()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            original = connection.execute(
                'SELECT * FROM task_queue WHERE task_id=? AND owner_user_id=? AND tenant_id=?',
                (str(task_id), owner_user_id, tenant_id),
            ).fetchone()
            if original is None or original['status'] not in {'succeeded', 'review_required'}:
                connection.execute('ROLLBACK')
                return None
            existing = connection.execute(
                'SELECT * FROM task_queue WHERE owner_user_id=? AND tenant_id=? AND submission_key=?',
                (owner_user_id, tenant_id, submission_key),
            ).fetchone()
            if existing is not None:
                connection.execute('COMMIT')
                return self._decode_row(existing, include_config=True)
            decoded = self._decode_row(original, include_config=True)
            config = dict(decoded['config'])
            config.pop('server_output_placement', None)
            validate_config(config)
            config['redo_of_task_id'] = str(task_id)
            new_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO task_queue (task_id,owner_user_id,tenant_id,submission_key,status,title,"
                "config_json,created_at,stage,billing_state) VALUES (?,?,?,?,'waiting',?,?,?,'等待制作','not_started')",
                (new_id, owner_user_id, tenant_id, submission_key, original['title'],
                 json.dumps(config, ensure_ascii=False, allow_nan=False), _queue_created_at_text()),
            )
            child = connection.execute('SELECT * FROM task_queue WHERE task_id=?', (new_id,)).fetchone()
            connection.execute('COMMIT')
        return self._decode_row(child, include_config=True)

    def retry_owned(
        self,
        owner_user_id,
        task_id,
        new_task_id=None,
        submission_key=None,
        *,
        tenant_id=None,
    ):
        """Retry only when the source row belongs to the authenticated owner."""
        original = self.get_owned(
            owner_user_id,
            task_id,
            include_config=True,
            tenant_id=tenant_id,
        )
        if not original or original["status"] not in RETRYABLE_STATUSES:
            return None
        if not isinstance(original.get("config"), dict):
            return None
        return self.enqueue(
            original["title"],
            original["config"],
            task_id=new_task_id,
            submission_key=submission_key,
            owner_user_id=owner_user_id,
            tenant_id=original.get("tenant_id"),
        )

    def retry(self, task_id, new_task_id=None, submission_key=None):
        """Unscoped compatibility path for trusted internal callers only."""
        original = self.get_internal(task_id, include_config=True)
        if not original or original["status"] not in RETRYABLE_STATUSES:
            return None
        if not isinstance(original.get("config"), dict):
            return None
        if original.get("legacy_unassigned"):
            return self.enqueue_legacy_internal(
                original["title"],
                original["config"],
                task_id=new_task_id,
                submission_key=submission_key,
            )
        return self.enqueue(
            original["title"],
            original["config"],
            task_id=new_task_id,
            submission_key=submission_key,
            owner_user_id=original["owner_user_id"],
            tenant_id=original.get("tenant_id"),
        )

    def reserved_units_for_customer(self, customer_id):
        self.ensure_ready()
        if customer_id is None:
            return 0
        normalized_customer_id = str(customer_id).strip()
        if not normalized_customer_id:
            return 0
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, config_json, billing_state FROM task_queue
                WHERE status IN ('waiting', 'running', 'skip_requested')
                   OR (
                       status='finalizing'
                       AND billing_state IN (
                           'not_started', 'started', 'uncertain'
                       )
                   )
                """
            ).fetchall()
        reserved_units = 0
        for row in rows:
            try:
                config = json.loads(row["config_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(config, dict):
                continue
            saved_customer_id = config.get("customer_id")
            if (
                saved_customer_id is None
                or str(saved_customer_id).strip() != normalized_customer_id
            ):
                continue
            try:
                units = int(config.get("fission_count", 1) or 1)
            except (TypeError, ValueError, OverflowError):
                units = 1
            reserved_units += max(1, units)
        return reserved_units

    def _clear_finished_scoped(self, *, owner_user_id=None, tenant_id=None):
        self.ensure_ready()
        scope_sql = ""
        scope_parameters = ()
        if owner_user_id is not None:
            owner_user_id = _normalized_identity(owner_user_id, "owner_user_id")
            tenant_id = _normalized_identity(
                tenant_id,
                "tenant_id",
                required=False,
            )
            scope_sql = " AND owner_user_id=?"
            scope_parameters = (owner_user_id,)
            if tenant_id is not None:
                scope_sql += " AND tenant_id=?"
                scope_parameters += (tenant_id,)
        elif tenant_id is not None:
            raise ValueError("tenant_id cannot scope a clear without owner_user_id")
        placeholders = ",".join("?" for _ in CLEARABLE_STATUSES)
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE task_queue
                SET history_hidden=1
                WHERE status IN ({placeholders})
                  AND billing_state != 'uncertain'
                  AND history_hidden=0
                  {scope_sql}
                """,
                (*tuple(sorted(CLEARABLE_STATUSES)), *scope_parameters),
            )
            return int(cursor.rowcount or 0)

    def clear_finished_owned(self, owner_user_id, *, tenant_id=None):
        """Hide only one authenticated owner's clearable history."""
        return self._clear_finished_scoped(
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
        )

    def clear_finished(self):
        """Backward-compatible machine-wide clear for trusted admin code only."""
        return self._clear_finished_scoped()

    def _hide_finished_scoped(
        self,
        task_ids,
        *,
        owner_user_id=None,
        tenant_id=None,
    ):
        """Hide selected settled terminal records without deleting artifacts."""
        self.ensure_ready()
        scope_sql = ""
        scope_parameters = ()
        if owner_user_id is not None:
            owner_user_id = _normalized_identity(owner_user_id, "owner_user_id")
            tenant_id = _normalized_identity(
                tenant_id,
                "tenant_id",
                required=False,
            )
            scope_sql = " AND owner_user_id=?"
            scope_parameters = (owner_user_id,)
            if tenant_id is not None:
                scope_sql += " AND tenant_id=?"
                scope_parameters += (tenant_id,)
        elif tenant_id is not None:
            raise ValueError("tenant_id cannot scope a hide without owner_user_id")
        if not isinstance(task_ids, (list, tuple, set, frozenset)):
            return 0
        normalized_ids = list(
            dict.fromkeys(
                str(task_id or "").strip()
                for task_id in task_ids
                if str(task_id or "").strip()
            )
        )
        if not normalized_ids:
            return 0
        status_placeholders = ",".join("?" for _ in HIDEABLE_STATUSES)
        hidden = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for offset in range(0, len(normalized_ids), 500):
                    chunk = normalized_ids[offset : offset + 500]
                    id_placeholders = ",".join("?" for _ in chunk)
                    cursor = connection.execute(
                        f"""
                        UPDATE task_queue
                        SET history_hidden=1
                        WHERE task_id IN ({id_placeholders})
                          AND status IN ({status_placeholders})
                          AND billing_state != 'uncertain'
                          AND history_hidden=0
                          {scope_sql}
                        """,
                        (
                            *chunk,
                            *tuple(sorted(HIDEABLE_STATUSES)),
                            *scope_parameters,
                        ),
                    )
                    hidden += int(cursor.rowcount or 0)
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return hidden

    def hide_finished_owned(
        self,
        owner_user_id,
        task_ids,
        *,
        tenant_id=None,
    ):
        """Hide selected ids only when they belong to the authenticated owner."""
        return self._hide_finished_scoped(
            task_ids,
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
        )

    def hide_finished(self, task_ids):
        """Backward-compatible machine-wide hide for trusted admin code only."""
        return self._hide_finished_scoped(task_ids)

    def _snapshot_scoped(
        self,
        history_limit=80,
        waiting_limit=200,
        *,
        owner_user_id=None,
        tenant_id=None,
    ):
        self.ensure_ready()
        history_limit = max(1, min(500, int(history_limit)))
        waiting_limit = max(1, min(1000, int(waiting_limit)))
        scope_sql = ""
        scope_parameters = ()
        if owner_user_id is not None:
            owner_user_id = _normalized_identity(
                owner_user_id,
                "owner_user_id",
            )
            tenant_id = _normalized_identity(
                tenant_id,
                "tenant_id",
                required=False,
            )
            scope_sql = " AND owner_user_id=?"
            scope_parameters = (owner_user_id,)
            if tenant_id is not None:
                scope_sql += " AND tenant_id=?"
                scope_parameters += (tenant_id,)
        elif tenant_id is not None:
            raise ValueError("tenant_id cannot scope a snapshot without owner_user_id")
        with self._connect() as connection:
            count_rows = connection.execute(
                f"""
                SELECT status, COUNT(*) AS total FROM task_queue
                WHERE history_hidden=0{scope_sql}
                GROUP BY status
                """,
                scope_parameters,
            ).fetchall()
            running_rows = connection.execute(
                f"""
                SELECT * FROM task_queue
                WHERE status IN ('running', 'skip_requested', 'finalizing')
                  {scope_sql}
                ORDER BY sequence ASC
                """,
                scope_parameters,
            ).fetchall()
            waiting_rows = connection.execute(
                f"""
                SELECT * FROM task_queue
                WHERE status='waiting'
                  {scope_sql}
                ORDER BY sequence ASC LIMIT ?
                """,
                (*scope_parameters, waiting_limit),
            ).fetchall()
            clearable_placeholders = ",".join(
                "?" for _ in CLEARABLE_STATUSES
            )
            clearable = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) AS total FROM task_queue
                    WHERE status IN ({clearable_placeholders})
                      AND billing_state != 'uncertain'
                      AND history_hidden=0
                      {scope_sql}
                    """,
                    (*tuple(sorted(CLEARABLE_STATUSES)), *scope_parameters),
                ).fetchone()["total"]
            )
            terminal_placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
            history_rows = connection.execute(
                f"""
                SELECT * FROM task_queue
                WHERE status IN ({terminal_placeholders})
                  AND history_hidden=0
                  {scope_sql}
                ORDER BY sequence DESC LIMIT ?
                """,
                (
                    *tuple(sorted(TERMINAL_STATUSES)),
                    *scope_parameters,
                    history_limit,
                ),
            ).fetchall()
        counts = {row["status"]: int(row["total"]) for row in count_rows}
        active = [self._decode_row(row) for row in (*running_rows, *waiting_rows)]
        history = [self._decode_row(row) for row in history_rows]
        paused = sum(
            1
            for item in active
            if item.get("status") == "running"
            and item.get("control_state") == "paused"
        )
        return {
            "active": active,
            "history": history,
            "counts": counts,
            "waiting": counts.get("waiting", 0),
            "running": counts.get("running", 0)
            + counts.get("skip_requested", 0),
            "paused": paused,
            "finalizing": counts.get("finalizing", 0),
            "finished": sum(counts.get(status, 0) for status in TERMINAL_STATUSES),
            "clearable": clearable,
            "total": sum(counts.values()),
            "history_limit": history_limit,
            "waiting_limit": waiting_limit,
            "visible_waiting": len(waiting_rows),
        }

    def snapshot_owned(
        self,
        owner_user_id,
        history_limit=80,
        waiting_limit=200,
        *,
        tenant_id=None,
    ):
        """Return only one authenticated owner's queue and history.

        NULL-owned legacy rows and every foreign row are excluded from every
        list and aggregate, including counts and clearable totals.
        """
        owner_user_id = _normalized_identity(owner_user_id, "owner_user_id")
        return self._snapshot_scoped(
            history_limit=history_limit,
            waiting_limit=waiting_limit,
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
        )

    def snapshot_internal(self, history_limit=80, waiting_limit=200):
        """Machine-wide snapshot for trusted scheduling/diagnostics only."""
        return self._snapshot_scoped(
            history_limit=history_limit,
            waiting_limit=waiting_limit,
        )

    def snapshot(self, history_limit=80, waiting_limit=200):
        """Backward-compatible internal alias; public routes use snapshot_owned."""
        return self.snapshot_internal(
            history_limit=history_limit,
            waiting_limit=waiting_limit,
        )
