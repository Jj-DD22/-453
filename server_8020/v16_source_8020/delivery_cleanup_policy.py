"""Shared, fail-closed ZIP receipts for web and offline gateway maintenance.

Completed-task ZIPs survive until the task's 48-hour retention deadline.
Transfer EOF is accounting only, never proof of a successful client disk save.
Legacy receipts without a task deadline conservatively retain the ZIP for
48 hours from its creation timestamp. Unfinished packaging is never selected.
"""
import json
import math
import os
import re
import stat
import time
import uuid
from pathlib import Path

COMPLETED_OUTPUT_RETENTION_SECONDS = 48 * 60 * 60


def _identity(path):
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise OSError("cache file is not a plain file")
    return {
        "size": int(metadata.st_size),
        "mtime_ns": int(metadata.st_mtime_ns),
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
    }


def read_bundle_receipt(path, owner_user_id):
    path = Path(path)
    if not re.fullmatch(r"bundle-[0-9a-f]{64}\.zip", path.name):
        return None
    try:
        identity = _identity(path)
        sidecar = path.with_suffix(".json")
        if _identity(sidecar)["size"] > 65536:
            return None
        receipt = json.loads(sidecar.read_text(encoding="utf-8"))
        task_ids = receipt.get("task_ids") if isinstance(receipt, dict) else None
        if (
            not isinstance(receipt, dict)
            or receipt.get("version") != 1
            or receipt.get("kind") != "v17_bundle_ownership"
            or receipt.get("owner_user_id") != owner_user_id
            or receipt.get("bundle_name") != path.name
            or receipt.get("size_bytes") != identity["size"]
            or receipt.get("file_identity") != identity
            or not isinstance(task_ids, list) or not task_ids
            or any(not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key) for key in task_ids)
            or len(set(task_ids)) != len(task_ids)
            or _identity(path) != identity
        ):
            return None
        return receipt
    except (OSError, ValueError, TypeError):
        return None


def bundle_was_delivered(path, owner_user_id):
    receipt = read_bundle_receipt(path, owner_user_id)
    return bool(receipt and receipt.get("transfer_completed") is True)


def bundle_retention_deadline(path, owner_user_id):
    receipt = read_bundle_receipt(path, owner_user_id)
    if receipt is None:
        return None
    value = receipt.get("retain_until_epoch")
    if value is not None:
        try:
            deadline = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return deadline if math.isfinite(deadline) and deadline > 0 else None
    try:
        return _identity(Path(path))["mtime_ns"] / 1e9 + COMPLETED_OUTPUT_RETENTION_SECONDS
    except (OSError, ValueError, TypeError):
        return None


def bundle_ready_for_cleanup(path, owner_user_id, *, now=None):
    deadline = bundle_retention_deadline(path, owner_user_id)
    current = time.time() if now is None else float(now)
    return bool(deadline is not None and math.isfinite(current) and current >= deadline)


def write_bundle_receipt(path, owner_user_id, task_ids, *, completed=False, retain_until_epoch=None):
    """Caller holds the exact bundle lock; completion is monotonic per file."""
    path = Path(path)
    task_ids = sorted(set(str(key) for key in task_ids))
    if (
        not re.fullmatch(r"bundle-[0-9a-f]{64}\.zip", path.name)
        or not re.fullmatch(r"[0-9a-f]{32}", str(owner_user_id))
        or not task_ids
        or any(not re.fullmatch(r"[0-9a-f]{32}", key) for key in task_ids)
    ):
        raise ValueError("invalid bundle ownership")
    previous = read_bundle_receipt(path, owner_user_id)
    identity = _identity(path)
    if retain_until_epoch is None:
        retain_until_epoch = (previous or {}).get("retain_until_epoch")
    if retain_until_epoch is None:
        retain_until_epoch = identity["mtime_ns"] / 1e9 + COMPLETED_OUTPUT_RETENTION_SECONDS
    retain_until_epoch = float(retain_until_epoch)
    if not math.isfinite(retain_until_epoch) or retain_until_epoch <= 0:
        raise ValueError("invalid retention deadline")
    receipt = {
        "version": 1, "kind": "v17_bundle_ownership",
        "owner_user_id": owner_user_id, "task_ids": task_ids,
        "bundle_name": path.name, "size_bytes": identity["size"],
        "file_identity": identity,
        "transfer_completed": bool(completed or (previous and previous.get("transfer_completed") is True)),
        "retain_until_epoch": retain_until_epoch,
        "retention_basis": "completed_task_48h",
    }
    destination = path.with_suffix(".json")
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(receipt, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        if _identity(path) != identity:
            raise OSError("bundle changed while recording ownership")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
