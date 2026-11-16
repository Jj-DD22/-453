"""Offline, tenant-scoped D/E output placement policy.

No directory creation, database writes, file movement, cleanup or process control
occurs here.  Callers must persist ``prepare_delivery_config``'s returned config
before publishing any bytes, then use that persisted config on every retry.
Only pass server-owned base paths; never take a base from an HTTP request.
"""
from __future__ import annotations

import copy
import os
import re
import shutil
import stat
from pathlib import Path


PRIMARY_OUTPUT_BASE = Path("D:/快手自动化GPU服务器8020成片")
SECONDARY_OUTPUT_BASE = Path("E:/快手自动化GPU服务器8020成片")
PRIMARY_FREE_THRESHOLD_BYTES = 100 * 1024**3
PLACEMENT_KEY = "server_output_placement"
PLACEMENT_VERSION = 1


class StorageRootsError(ValueError):
    pass


def _identifier(value, label):
    value = str(value or "")
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise StorageRootsError(f"{label}无效")
    return value


def _lexical_local_path(value):
    raw = os.fspath(value)
    if not raw or "\x00" in raw or raw.startswith(("\\\\", "//")):
        raise StorageRootsError("成片目录必须是本机完整路径")
    candidate = Path(raw)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise StorageRootsError("成片目录不允许相对路径或上级跳转")
    if os.name == "nt" and any(":" in part for part in candidate.parts[1:]):
        raise StorageRootsError("成片目录不能使用替代数据流")
    return Path(os.path.abspath(raw))


def _plain_local_path(value, *, require_exists):
    """Validate the entire existing ancestry, including Windows junctions."""
    candidate = _lexical_local_path(value)
    anchor = Path(candidate.anchor)
    current = anchor
    missing = False
    for part in (None, *candidate.parts[1:]):
        if part is not None:
            current /= part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if current == anchor or require_exists:
                raise StorageRootsError("成片所在磁盘或目录不存在")
            missing = True
            continue
        except OSError as exc:
            raise StorageRootsError("成片目录无法安全读取") from exc
        if missing or stat.S_ISLNK(info.st_mode) or int(getattr(info, "st_file_attributes", 0)) & 0x400:
            raise StorageRootsError("成片目录不能经过链接或重解析点")
        if not stat.S_ISDIR(info.st_mode):
            raise StorageRootsError("成片目录不是普通文件夹")
    if candidate.resolve(strict=require_exists) != candidate:
        raise StorageRootsError("成片目录解析后越界")
    return candidate


def tenant_output_roots(owner_user_id, *, primary_base=PRIMARY_OUTPUT_BASE,
                        secondary_base=SECONDARY_OUTPUT_BASE):
    """Return exactly this owner's two permitted roots; never create them."""
    owner = _identifier(owner_user_id, "账号编号")
    # Merely listing the allowlist must not require E to be online: existing
    # D downloads remain readable even if E is temporarily disconnected.
    bases = tuple(_lexical_local_path(base)
                  for base in (primary_base, secondary_base))
    if bases[0] == bases[1] or any(base == Path(base.anchor) for base in bases):
        raise StorageRootsError("服务器成片根目录配置无效")
    return tuple(base / owner for base in bases)


def validate_tenant_output_root(value, owner_user_id, *, require_exists=True,
                               primary_base=PRIMARY_OUTPUT_BASE,
                               secondary_base=SECONDARY_OUTPUT_BASE):
    roots = tenant_output_roots(owner_user_id, primary_base=primary_base,
                                secondary_base=secondary_base)
    candidate = _plain_local_path(value, require_exists=require_exists)
    if candidate not in roots:
        raise StorageRootsError("只能访问当前账号在服务器 D/E 盘的成片目录")
    return candidate


def select_new_output_root(owner_user_id, *, primary_base=PRIMARY_OUTPUT_BASE,
                           secondary_base=SECONDARY_OUTPUT_BASE,
                           disk_usage=shutil.disk_usage,
                           threshold_bytes=PRIMARY_FREE_THRESHOLD_BYTES,
                           required_bytes=0):
    """Choose E at/below 100 GiB; never silently fall back to a full D disk."""
    roots = tenant_output_roots(owner_user_id, primary_base=primary_base,
                                secondary_base=secondary_base)
    if int(threshold_bytes) < 0 or int(required_bytes) < 0:
        raise StorageRootsError("磁盘容量阈值无效")
    try:
        primary_free = int(disk_usage(Path(roots[0].anchor)).free)
    except (OSError, ValueError, AttributeError) as exc:
        raise StorageRootsError("暂时无法确认 D 盘容量，成片暂存等待交付") from exc
    selected = roots[1] if primary_free <= int(threshold_bytes) else roots[0]
    selected = _plain_local_path(selected, require_exists=False)
    if selected == roots[1]:
        try:
            secondary_free = int(disk_usage(Path(selected.anchor)).free)
        except (OSError, ValueError, AttributeError) as exc:
            raise StorageRootsError("E 盘暂不可用，成片暂存等待交付") from exc
        if secondary_free <= int(required_bytes):
            raise StorageRootsError("E 盘空间不足，成片暂存等待交付")
    elif primary_free <= int(required_bytes):
        raise StorageRootsError("D 盘空间不足，成片暂存等待交付")
    return selected


def prepare_delivery_config(config, task_id, owner_user_id, *, result=None,
                            primary_base=PRIMARY_OUTPUT_BASE,
                            secondary_base=SECONDARY_OUTPUT_BASE,
                            disk_usage=shutil.disk_usage,
                            threshold_bytes=PRIMARY_FREE_THRESHOLD_BYTES,
                            required_bytes=0):
    """Pure config proposal: persist atomically BEFORE first output publication.

    A saved result or an existing placement is immutable here. In particular,
    retries cannot change roots when free space fluctuates. Existing legacy C
    exports remain the responsibility of the caller's exact legacy-path branch.
    """
    run_id = _identifier(task_id, "任务编号")
    owner = _identifier(owner_user_id, "账号编号")
    if not isinstance(config, dict):
        raise StorageRootsError("任务配置无效")
    proposed = copy.deepcopy(config)
    placement = config.get(PLACEMENT_KEY)
    options = dict(primary_base=primary_base, secondary_base=secondary_base)
    if placement is not None:
        if (not isinstance(placement, dict) or placement.get("version") != PLACEMENT_VERSION
                or placement.get("task_id") != run_id or placement.get("owner_user_id") != owner
                or placement.get("root") != config.get("auto_save_directory")):
            raise StorageRootsError("任务持久化成片位置不匹配")
        validate_tenant_output_root(placement["root"], owner, require_exists=False, **options)
        return proposed, False
    metadata = result.get("auto_save") if isinstance(result, dict) else None
    if isinstance(metadata, dict) and metadata.get("status") == "saved":
        validate_tenant_output_root(config.get("auto_save_directory", ""), owner,
                                    require_exists=True, **options)
        return proposed, False
    root = select_new_output_root(owner, disk_usage=disk_usage,
                                  threshold_bytes=threshold_bytes,
                                  required_bytes=required_bytes, **options)
    proposed["auto_save_directory"] = str(root)
    proposed[PLACEMENT_KEY] = {"version": PLACEMENT_VERSION, "task_id": run_id,
                               "owner_user_id": owner, "root": str(root)}
    return proposed, proposed != config
