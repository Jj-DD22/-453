#!/usr/bin/env python3
"""Verify the immutable files in an installed full-bundle server tree."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import NoReturn


EXPECTED_SCHEMA = 1
EXPECTED_RELEASE = 'kuaishou-v25-rtx5070-multiuser-download-navigation-2.3.11-public3-render-diagnostics-20260907-diversity-20260908-auth-recovery-20260908-auth-download-hotfix-20260908-timeline-help-20260908-retention48h-spill-e-20260910-remove35-batch100-fetch-retry-20260911-submission-confirm-20260911-optional-opening-20260916-autofill-20260927.1-offline-opening-storage100-20260928.1-retry-batch-20260928.2-login-continuity-20260930-r1-guard-contract-20261015'
EXPECTED_DELIVERY_RELEASE = "kuaishou-v25-rtx5070-multiuser-portable-1.2.0"
EXPECTED_PAYLOAD_ROOT_SHA256 = 'caf5b861482e63c3c5341b023b8586a8cc74915030992caa951b203134ecf93d'
EXPECTED_PAYLOAD_ROOT_ALGORITHM = "app-records-path-size-sha256-v1"
EXPECTED_DELIVERY = "installer-free-portable-zip"
EXPECTED_ARCHITECTURE = "windows-x86_64"
EXPECTED_PORT = 8020
EXPECTED_STARTUP_CHAIN = "windows-vbs-cmd-bundled-python"
EXPECTED_MANIFEST_FILE_COUNT = 8342
EXPECTED_PAYLOAD_FILE_COUNT = 8336
EXPECTED_PAYLOAD_TOTAL_BYTES = 1380144788
UNMANAGED_PREFIXES = ("resources/app/v16_source/",)


def ignored_release_path(relative: str) -> bool:
    normalized = relative.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    return (
        any(
            normalized == prefix.rstrip("/") or normalized.startswith(prefix)
            for prefix in UNMANAGED_PREFIXES
        )
        or "__pycache__" in {part.casefold() for part in parts}
        or normalized.casefold().endswith((".pyc", ".pyo"))
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fail(message: str) -> NoReturn:
    print(f"FULL_BUNDLE_VERIFY_FAILED {message}", flush=True)
    raise SystemExit(1)


def safe_relative_path(raw: object) -> PurePosixPath:
    if (
        not isinstance(raw, str)
        or not raw
        or "\\" in raw
        or ":" in raw
        or "\0" in raw
    ):
        fail("manifest_path_invalid")
    candidate = PurePosixPath(raw)
    if (
        candidate.is_absolute()
        or str(candidate) != raw
        or ".." in candidate.parts
        or "." in candidate.parts
    ):
        fail("manifest_path_unsafe")
    return candidate


def is_reparse_point(path: Path) -> bool:
    details = path.lstat()
    attributes = getattr(details, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse_flag)


def main() -> int:
    if len(sys.argv) != 2:
        fail("usage")
    root = Path(sys.argv[1]).absolute()
    manifest_path = root / "runtime-manifest.json"
    if (
        not root.is_dir()
        or is_reparse_point(root)
        or not manifest_path.is_file()
        or is_reparse_point(manifest_path)
    ):
        fail("root_or_manifest_missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        fail("manifest_unreadable")
    if manifest.get("schema") != EXPECTED_SCHEMA:
        fail("manifest_schema")
    if manifest.get("release") != EXPECTED_RELEASE:
        fail("manifest_release")
    if manifest.get("delivery_release") != EXPECTED_DELIVERY_RELEASE:
        fail("manifest_delivery_release")
    if manifest.get("payload_root_sha256") != EXPECTED_PAYLOAD_ROOT_SHA256:
        fail("manifest_payload_root")
    if manifest.get("payload_root_algorithm") != EXPECTED_PAYLOAD_ROOT_ALGORITHM:
        fail("manifest_payload_root_algorithm")
    if manifest.get("delivery") != EXPECTED_DELIVERY:
        fail("manifest_delivery")
    if (
        manifest.get("architecture") != EXPECTED_ARCHITECTURE
        or manifest.get("runtime_architecture") != EXPECTED_ARCHITECTURE
    ):
        fail("manifest_architecture")
    if manifest.get("port") != EXPECTED_PORT:
        fail("manifest_port")
    if manifest.get("startup_chain") != EXPECTED_STARTUP_CHAIN:
        fail("manifest_startup_chain")
    if manifest.get("custom_native_startup_executable") is not False:
        fail("manifest_custom_native_startup_executable")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        fail("manifest_files")

    seen: set[str] = set()
    total_bytes = 0
    payload_records: list[tuple[str, int, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            fail("manifest_entry")
        relative = safe_relative_path(entry.get("path"))
        if ignored_release_path(str(relative)):
            fail("manifest_contains_unmanaged_path")
        casefolded = str(relative).casefold()
        if casefolded in seen:
            fail("manifest_casefold_collision")
        seen.add(casefolded)
        expected_size = entry.get("size")
        expected_hash = entry.get("sha256")
        if not isinstance(expected_size, int) or expected_size < 0:
            fail("manifest_size")
        if (
            not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or any(character not in "0123456789abcdef" for character in expected_hash)
        ):
            fail("manifest_sha256")
        target = root.joinpath(*relative.parts)
        try:
            parent = target.parent
            while parent != root:
                if not parent.is_dir() or is_reparse_point(parent):
                    fail(f"parent_missing_or_reparse:{relative}")
                parent = parent.parent
            if is_reparse_point(target) or not target.is_file():
                fail(f"file_missing_or_reparse:{relative}")
            actual_size = target.stat().st_size
            if actual_size != expected_size:
                fail(f"file_size:{relative}")
            if sha256_file(target) != expected_hash:
                fail(f"file_sha256:{relative}")
        except OSError:
            fail(f"file_unreadable:{relative}")
        total_bytes += expected_size
        prefix = "resources/app/"
        relative_text = str(relative)
        if relative_text.startswith(prefix):
            payload_records.append(
                (relative_text[len(prefix) :], expected_size, expected_hash)
            )

    if (
        len(entries) != EXPECTED_MANIFEST_FILE_COUNT
        or manifest.get("file_count") != EXPECTED_MANIFEST_FILE_COUNT
    ):
        fail("manifest_file_count")
    if total_bytes != manifest.get("total_bytes"):
        fail("manifest_total_bytes")
    if (
        len(payload_records) != EXPECTED_PAYLOAD_FILE_COUNT
        or sum(item[1] for item in payload_records) != EXPECTED_PAYLOAD_TOTAL_BYTES
    ):
        fail("manifest_payload_totals")
    payload_digest = hashlib.sha256()
    for relative_text, expected_size, expected_hash in sorted(payload_records):
        payload_digest.update(relative_text.encode("utf-8"))
        payload_digest.update(b"\0")
        payload_digest.update(str(expected_size).encode("ascii"))
        payload_digest.update(b"\0")
        payload_digest.update(expected_hash.encode("ascii"))
        payload_digest.update(b"\n")
    if payload_digest.hexdigest() != EXPECTED_PAYLOAD_ROOT_SHA256:
        fail("manifest_payload_root_recompute")

    actual: set[str] = set()
    try:
        for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            directory_path = Path(directory)
            if is_reparse_point(directory_path):
                fail("tree_directory_reparse")
            kept_dirnames: list[str] = []
            for dirname in dirnames:
                child = directory_path / dirname
                child_relative = child.relative_to(root).as_posix()
                if ignored_release_path(child_relative):
                    continue
                if is_reparse_point(child):
                    fail("tree_directory_reparse")
                kept_dirnames.append(dirname)
            dirnames[:] = kept_dirnames
            for filename in filenames:
                child = directory_path / filename
                relative = child.relative_to(root).as_posix()
                if ignored_release_path(relative):
                    continue
                if is_reparse_point(child):
                    fail("tree_file_reparse")
                if relative != "runtime-manifest.json":
                    actual.add(relative.casefold())
    except OSError:
        fail("tree_unreadable")
    if actual != seen:
        fail("tree_file_set_mismatch")
    print(
        f"FULL_BUNDLE_VERIFY_OK files={len(entries)} bytes={total_bytes}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
