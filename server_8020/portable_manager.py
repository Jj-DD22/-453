#!/usr/bin/env python3
"""Native-EXE-free Windows manager for the portable v25 GPU LAN server.

This module is intentionally standard-library only.  The root ASCII CMD starts
it with the bundled x64 ``v16-runtime.exe`` that has been proven to load on the
target Windows machine.  It never starts or delegates to the MinGW launcher.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import http.client
import ipaddress
import json
import os
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
import winreg
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn


SERVER_PORT = 8020
# Keep the legacy desktop range reserved on loopback so the old single-user
# desktop backend cannot start alongside this server.  These are not public
# listener ports; the multi-user tenant pool remains 18820-18919.
LEGACY_DESKTOP_PORT_MIN = 8818
LEGACY_DESKTOP_PORT_MAX = 8838
ALREADY_RUNNING_EXIT_CODE = 10
MANAGER_VERSION = "1.2.11-rtx5070-download-navigation"
EXPECTED_INSTANCE_ID = "kuaishou-automation-v17-8020"
EXPECTED_RELEASE_ID = (
    "kuaishou-automation-3.2.4-v1.2-multiuser-global3-fifo-cleanup-"
    "stable-fission-download2h-win19041"
)
EXPECTED_BUNDLE_RELEASE = 'kuaishou-v25-rtx5070-multiuser-download-navigation-2.3.11-public3-render-diagnostics-20260907-diversity-20260908-auth-recovery-20260908-auth-download-hotfix-20260908-timeline-help-20260908-retention48h-spill-e-20260910-remove35-batch100-fetch-retry-20260911-submission-confirm-20260911-optional-opening-20260916-autofill-20260927.1-offline-opening-storage100-20260928.1-retry-batch-20260928.2-login-continuity-20260930-r1-guard-contract-20261015'
EXPECTED_DELIVERY_RELEASE = "kuaishou-v25-rtx5070-multiuser-portable-1.2.0"
EXPECTED_PAYLOAD_ROOT_SHA256 = 'caf5b861482e63c3c5341b023b8586a8cc74915030992caa951b203134ecf93d'
EXPECTED_SERVER_RUNTIME_SHA256 = 'fbac7d0a5305a7444ba8f36e80965a6949f4ad5a09c3f6b6b8681f317836bf01'

MANAGER_MUTEX = r"Local\KuaishouGpuServerPythonManager_v104"
INSTALL_MUTEX = r"Local\KuaishouAutomationInstallMutex"
FIREWALL_RULE = "Kuaishou GPU Server Full 8020 Private LAN"
FIREWALL_REMOTE_ALIASES = frozenset(
    {"localsubnet", "localsubnet4", "localsubnet6"}
)
DOCUMENTS_FOLDER_ID = "FDD39AD0-238F-46AF-ADB4-6C85480369C7"
LOCAL_APP_DATA_FOLDER_ID = "F1B32785-6FBA-4FCF-9D55-7B8E7F157091"
DATA_DIRECTORY_NAME = (
    "\u5feb\u624b\u81ea\u52a8\u5316GPU\u670d\u52a1\u56688020"
)
EXPORT_DIRECTORY_NAME = (
    "\u5feb\u624b\u81ea\u52a8\u5316GPU\u670d\u52a1\u56688020\u6210\u7247"
)
DESKTOP_PROCESS_NAME = "\u5feb\u624b\u81ea\u52a8\u5316.exe"
DISPLAY_NAME = "\u5feb\u624b\u81ea\u52a8\u5316 GPU \u670d\u52a1\u5668"

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102
INFINITE = 0xFFFFFFFF
ERROR_ALREADY_EXISTS = 183
ERROR_CANCELLED = 1223
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
FILE_ATTRIBUTE_REPARSE_POINT = getattr(
    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
)

SCRUBBED_ENVIRONMENT = {
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONINSPECT",
    "PYTHONUSERBASE",
    "PYTHONBREAKPOINT",
    "PYTHONWARNINGS",
    "PYTHONDEBUG",
    "PYTHONMALLOC",
    "PYTHONHASHSEED",
    "V16_LOCAL_NO_VALIDATION",
    "V16_INSTALLER_SELF_TEST",
    "FLASK_DEBUG",
    "FLASK_ENV",
    "WERKZEUG_DEBUG_PIN",
    "WERKZEUG_RUN_MAIN",
    "V16_VISION_MOCK_RESPONSE",
    "V16_TTS_MOCK_AUDIO",
    "V16_ALIGNMENT_MOCK_RESPONSE",
    "V16_SHOT_ANALYSIS_MOCK_RESPONSE",
    "V16_VISION_REUSE_COPY_FILE",
    "V16_TTS_REUSE_AUDIO",
    "V16_ERP_BEARER_TOKEN",
    "V16_ERP_IDENTITY_JSON",
}

SERVER_USER_ENVIRONMENT = (
    "V16_ERP_DIRECT_ENABLED",
)

RETIRED_EXTERNAL_AUTH_ENVIRONMENT = {
    "V17_AUTH_START_URL", "V17_AUTH_EXCHANGE_URL", "V17_AUTH_ISSUER",
    "V17_AUTH_AUDIENCE", "V17_AUTH_SHARED_SECRET_FILE", "V17_STRICT_ERP_RELAY",
}


def _retired_external_environment(name: str) -> bool:
    key = str(name).upper()
    return key in RETIRED_EXTERNAL_AUTH_ENVIRONMENT or key.startswith(
        ("V16_BILLING_", "V16_ERP_RELAY_", "V16_35_")
    )


def read_persistent_user_environment(name: str) -> str:
    """Read an HKCU environment value without logging credential contents."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, value_type = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    if value_type not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ}:
        return ""
    return str(value).strip()

CRITICAL_HASHES = {'resources/app/runtime/ffmpeg/bin/ffmpeg.exe': '1326dde4c84ff1f96fe6b8916c5bed29e163e9b5dccf995f6f3db069d143ec5e', 'resources/app/runtime/ffmpeg/bin/ffprobe.exe': 'b49ccc7c6547b141ad5a2f6ec69cc04323d7133d7704d70b331b904c63eecb07', 'resources/app/runtime/python/v16-runtime.exe': '5f7b89a612c9b8af1d6456cdfcd1dbe5ca630849e79aebced9bee9a6694952ec', 'resources/app/runtime/tesseract/tesseract.exe': 'babb405f4366b480d02cd8ff2bac8d497170f6c1711ce6f3d5d8bf0fb7fa6ed9', 'resources/app/v16_source_8020/client_save_browser.js': '2a040dfa1fe7f95840be063aac4b29bfade566539cba03dc9b8503a315dd1d1a', 'resources/app/v16_source_8020/client_save_sessions.py': 'a683f9df2fff602238b770a794ec06da38f02142c97721250429494118235ec9', 'resources/app/v16_source_8020/delivery_cleanup_policy.py': 'fe2f3a412b3f4a031a63b0896137c5b4f15840a263021d9169e96031c3996ab1', 'resources/app/v16_source_8020/electron_auth_broker/broker_core.js': 'f4d1cefce0d40a70fc92419323314414d7588b8a3a05bd391bbb0114a54f85e1', 'resources/app/v16_source_8020/electron_auth_broker/main.js': '4cfebc39b93868c0d3ed2ed8d1313324ac1fdf7eeb6b96e944b67ea73fdf9750', 'resources/app/v16_source_8020/local_reserved_entry.py': '828fcd1249c6b19c9bbb6141e46bc0bc4fc0469a125d017e5591832ecd4877ca', 'resources/app/v16_source_8020/multiuser_gateway.py': 'df7d675ea90edf864f748257c6727585247256fc479d7827d2285718ec0feced', 'resources/app/v16_source_8020/render_slot_broker.py': '17ec0b17e476958dc43ae24e86dce831b0423a031b03fed1ddfd6073eeb90cf4', 'resources/app/v16_source_8020/run_v16_enhanced.py': 'c9361142a4f0d3e4e7b0017cec0f4c45af3a4a1ade7b3e2d937e753ae2dc0d3f', 'resources/app/v16_source_8020/storage_roots.py': 'be84de49b90d56347328782e2a8dfc75b2c5e708a12770a7524acb8c1f0107c4', 'resources/app/v16_source_8020/task_queue_store.py': 'f761852010cb1189bd9eadb309fc3ba0365c5a3a62c3c087c8f43a7f888392fa', 'resources/app/v16_source_8020/v16_web_app.py': '723cebda285909552f5c3a4ca04dc330eddfd9bf8b7609af9a3c363672a07ce7', 'server_runtime.pyz': 'fbac7d0a5305a7444ba8f36e80965a6949f4ad5a09c3f6b6b8681f317836bf01', 'resources/app/v16_source_8020/opening_video.py': 'e44bc9a99a6d47c2e0b345fced6f183a6da02b15bda967ba7a7ea2ea588afc13', 'resources/app/v16_source_8020/auto_fill_runtime.py': 'f31390fe84f4ac3e4e99f35054c2fe81497f60e2e448b870f2cd561cbc780f3c', 'resources/app/v16_source_8020/bi_lan_upload.py': '6b363fe50c11be43c1af87dc9dbc006a7c2e2b083684330365f7c13246975083', 'resources/app/v16_source_8020/delivery_handoff_guard.py': '6541ffa5ae5ba3a65149ed3e1cd340eee2d320dd3e0cbcd754f324db9e86e1af', 'resources/app/v16_source_8020/cosyvoice_synthesize_fast.py': '645bc2969d86ce46c2ed2d173c9eeb5c3643b2ba46117fccd2953abd102dfc94', 'resources/app/v16_source_8020/hardcut_audio.py': '16a0061be8a3e1c7b36304b53a03921daf25ae5eb2946e51e904420f0b89102a', 'resources/app/v16_source_8020/mac_performance.py': 'fea3ee374565ae23d75287c5866af7963dc8d733623329d8a0c73127b25dbfc6'}

PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)

PRIVATE_LAN_DISCOVERY_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$privateProfiles = @(Get-NetConnectionProfile | Where-Object {
  $_.NetworkCategory -eq 'Private' -and $_.IPv4Connectivity -ne 'Disconnected'
})
$privateIndexes = @($privateProfiles | ForEach-Object { [int]$_.InterfaceIndex })
$blocked = '(?i)vpn|tap|wireguard|tailscale|zerotier|zero tier|virtual|hyper-v|vethernet|vmware|virtualbox|vbox|wsl|docker|container|hamachi|openvpn|fortinet|npcap|loopback|pseudo'
$physical = @(Get-NetAdapter -Physical | Where-Object {
  $_.Status -eq 'Up' -and
  (($_.Name + ' ' + $_.InterfaceDescription) -notmatch $blocked)
})
$physicalIndexes = @($physical | ForEach-Object { [int]$_.ifIndex })
$ipInterfaces = @(Get-NetIPInterface -AddressFamily IPv4)
$candidates = @(Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred |
  Where-Object {
    $privateIndexes -contains [int]$_.InterfaceIndex -and
    $physicalIndexes -contains [int]$_.InterfaceIndex -and
    $_.IPAddress -notlike '169.254.*'
  } |
  ForEach-Object {
    $ip = $_
    $adapter = $physical | Where-Object { [int]$_.ifIndex -eq [int]$ip.InterfaceIndex } | Select-Object -First 1
    $ipInterface = $ipInterfaces | Where-Object { [int]$_.InterfaceIndex -eq [int]$ip.InterfaceIndex } | Select-Object -First 1
    $rank = if ($adapter.NdisPhysicalMedium -eq 9 -or $adapter.Name -match '(?i)wi-?fi|wireless|wlan') { 1 } else { 0 }
    [PSCustomObject]@{
      Address = [string]$ip.IPAddress
      InterfaceIndex = [int]$ip.InterfaceIndex
      Metric = [int]$ipInterface.InterfaceMetric
      Rank = [int]$rank
    }
  } | Sort-Object Rank, Metric, InterfaceIndex, Address)
if ($candidates.Count -lt 1) { exit 41 }
$candidates[0] | ConvertTo-Json -Compress
""".strip()


class ManagerError(RuntimeError):
    def __init__(self, friendly: str, detail: str = "") -> None:
        super().__init__(friendly)
        self.friendly = friendly
        self.detail = detail


class ManagerAlreadyRunning(RuntimeError):
    """Normal second-launch state while the first server window owns the lock."""


@dataclass(frozen=True)
class InstallPaths:
    program_root: Path
    app_root: Path
    source_dir: Path
    runtime_dir: Path
    python_dir: Path
    ffmpeg_dir: Path
    tesseract_dir: Path
    python_exe: Path
    ffmpeg_exe: Path
    ffprobe_exe: Path
    web_script: Path
    runner_script: Path
    server_runtime: Path
    manifest: Path


@dataclass(frozen=True)
class LogPaths:
    root: Path
    launcher: Path
    server: Path


def fail(friendly: str, detail: str = "") -> NoReturn:
    raise ManagerError(friendly, detail)


def _require_windows() -> None:
    if os.name != "nt":
        fail("This portable server manager must run on Windows.", os.name)


def _win_dll(name: str) -> Any:
    _require_windows()
    return ctypes.WinDLL(name, use_last_error=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_reparse_point(path: Path) -> bool:
    details = path.lstat()
    attributes = getattr(details, "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def require_plain_file(path: Path) -> None:
    try:
        if is_reparse_point(path) or not path.is_file():
            fail("The portable package is incomplete or unsafe.", str(path))
    except OSError as exc:
        fail("The portable package cannot be read.", f"{path}: {exc}")


def require_plain_directory(path: Path) -> None:
    try:
        if is_reparse_point(path) or not path.is_dir():
            fail("The portable package directory is incomplete or unsafe.", str(path))
    except OSError as exc:
        fail("The portable package directory cannot be read.", f"{path}: {exc}")


def require_plain_ancestry(root: Path, target: Path) -> None:
    root = root.absolute()
    target = target.absolute()
    try:
        target.relative_to(root)
    except ValueError:
        fail("A portable package path escaped the program directory.", str(target))
    require_plain_directory(root)
    parent = target.parent
    while parent != root:
        require_plain_directory(parent)
        parent = parent.parent


def ensure_plain_directory(path: Path) -> None:
    try:
        if path.exists():
            if is_reparse_point(path) or not path.is_dir():
                fail("A local data directory is unsafe.", str(path))
            return
        parent = path.parent
        if parent == path:
            fail("A local data directory cannot be created.", str(path))
        ensure_plain_directory(parent)
        path.mkdir()
        if is_reparse_point(path) or not path.is_dir():
            fail("A local data directory is unsafe.", str(path))
    except ManagerError:
        raise
    except OSError as exc:
        fail("A local data directory cannot be created.", f"{path}: {exc}")


def make_install_paths(program_root: Path) -> InstallPaths:
    program_root = program_root.absolute()
    app_root = program_root / "resources" / "app"
    runtime = app_root / "runtime"
    python_dir = runtime / "python"
    ffmpeg_dir = runtime / "ffmpeg" / "bin"
    tesseract_dir = runtime / "tesseract"
    # 8020 has an independent source tree.  The standalone 8088 launcher keeps
    # using resources/app/v16_source, so updates on either service can never
    # leak into the other one through Python's live source loading.
    source = app_root / "v16_source_8020"
    return InstallPaths(
        program_root=program_root,
        app_root=app_root,
        source_dir=source,
        runtime_dir=runtime,
        python_dir=python_dir,
        ffmpeg_dir=ffmpeg_dir,
        tesseract_dir=tesseract_dir,
        python_exe=python_dir / "v16-runtime.exe",
        ffmpeg_exe=ffmpeg_dir / "ffmpeg.exe",
        ffprobe_exe=ffmpeg_dir / "ffprobe.exe",
        web_script=source / "v16_web_app.py",
        runner_script=source / "run_v16_enhanced.py",
        server_runtime=program_root / "server_runtime.pyz",
        manifest=program_root / "runtime-manifest.json",
    )


def discover_install(script_path: Path | None = None) -> InstallPaths:
    script = (script_path or Path(__file__)).absolute()
    roots = (script.parent, script.parent / "program")
    for root in roots:
        paths = make_install_paths(root)
        if paths.source_dir.is_dir() and paths.runtime_dir.is_dir():
            require_plain_directory(paths.program_root)
            require_plain_directory(paths.app_root)
            return paths
    fail(
        "The full portable package was not found. Extract the entire ZIP first.",
        str(script),
    )


def verify_manifest_contract(paths: InstallPaths) -> dict[str, Any]:
    require_plain_ancestry(paths.program_root, paths.manifest)
    require_plain_file(paths.manifest)
    try:
        payload = json.loads(paths.manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        fail("The portable runtime manifest cannot be read.", str(exc))
    if payload.get("schema") != 1:
        fail("The portable runtime manifest has the wrong schema.")
    if payload.get("release") != EXPECTED_BUNDLE_RELEASE:
        fail("The portable package contains a different server release.")
    if payload.get("delivery_release") != EXPECTED_DELIVERY_RELEASE:
        fail("The portable package contains a different delivery release.")
    if payload.get("payload_root_sha256") != EXPECTED_PAYLOAD_ROOT_SHA256:
        fail("The v25.1/RTX5070 tuned payload identity does not match.")
    if payload.get("payload_root_algorithm") != "app-records-path-size-sha256-v1":
        fail("The v25.1 payload identity algorithm does not match.")
    if payload.get("port") != SERVER_PORT:
        fail("The portable package does not use the fixed port 8020.")
    if payload.get("architecture") != "windows-x86_64":
        fail("The portable package does not contain the required x64 runtime.")
    return payload


def verify_critical_files(paths: InstallPaths) -> None:
    require_plain_directory(paths.source_dir)
    require_plain_directory(paths.runtime_dir)
    for relative, expected in CRITICAL_HASHES.items():
        target = paths.program_root.joinpath(*relative.split("/"))
        require_plain_ancestry(paths.program_root, target)
        require_plain_file(target)
        try:
            actual = sha256_file(target)
        except OSError as exc:
            fail("A required portable server file cannot be read.", f"{target}: {exc}")
        if actual.lower() != expected:
            fail(
                "A required v25/RTX5070 tuned server file is damaged or mismatched.",
                f"{relative}: {actual}",
            )


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def known_folder(folder_id: str) -> Path:
    shell32 = _win_dll("shell32")
    ole32 = _win_dll("ole32")
    guid = _Guid.from_buffer_copy(uuid.UUID(folder_id).bytes_le)
    output = ctypes.c_wchar_p()
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(_Guid),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    result = shell32.SHGetKnownFolderPath(
        ctypes.byref(guid), 0x00008000, None, ctypes.byref(output)
    )
    if result != 0 or not output.value:
        fail("Windows could not locate a required user folder.", hex(result & 0xFFFFFFFF))
    try:
        return Path(output.value)
    finally:
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree.restype = None
        ole32.CoTaskMemFree(ctypes.cast(output, ctypes.c_void_p))


def initialize_logs() -> LogPaths:
    root = known_folder(LOCAL_APP_DATA_FOLDER_ID) / "KuaishouGpuServer" / "state"
    logs = root / "logs"
    ensure_plain_directory(logs)
    return LogPaths(
        root=root,
        launcher=logs / "portable-python-manager.log",
        server=logs / "server.log",
    )


_log_lock = threading.Lock()
_launcher_log: Path | None = None


def log(message: str) -> None:
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + message
    print(line, flush=True)
    if _launcher_log is None:
        return
    with _log_lock:
        try:
            with _launcher_log.open("a", encoding="utf-8", newline="") as handle:
                handle.write(line + "\r\n")
        except OSError:
            pass


def _copy_missing(source: Path, target: Path) -> None:
    if is_reparse_point(source):
        fail("Bundled initial data contains an unsafe reparse point.", str(source))
    if source.is_dir():
        ensure_plain_directory(target)
        for child in source.iterdir():
            _copy_missing(child, target / child.name)
        return
    if not source.is_file():
        fail("Bundled initial data contains an unsupported entry.", str(source))
    if os.path.lexists(target):
        if is_reparse_point(target):
            fail("An existing business data entry is unsafe.", str(target))
        return
    ensure_plain_directory(target.parent)
    try:
        shutil.copy2(source, target)
    except OSError as exc:
        fail("Business data could not be initialized.", f"{source} -> {target}: {exc}")


def initialize_data_directory(paths: InstallPaths, documents: Path | None = None) -> Path:
    documents_root = documents or known_folder(DOCUMENTS_FOLDER_ID)
    require_plain_directory(documents_root)
    data_dir = documents_root / DATA_DIRECTORY_NAME
    ensure_plain_directory(data_dir)
    entries = (
        "\u6838\u5fc3\u7d20\u6750",
        "\u6253\u4e71\u7d20\u6750\u6c60",
        "\u8d34\u7eb8\u7d20\u6750\u6c60",
        "\u6253\u4e71\u7d20\u6750\u6c60_\u751f\u6210\u8981\u6c42.txt",
        ".v16_upload_order.json",
        ".v17_instance_id",
    )
    for name in entries:
        source = paths.source_dir / name
        target = data_dir / name
        if not os.path.lexists(target) and source.exists():
            _copy_missing(source, target)
    marker = data_dir / ".v17_instance_id"
    if not os.path.lexists(marker):
        try:
            marker.write_bytes((EXPECTED_INSTANCE_ID + "\r\n").encode("ascii"))
        except OSError as exc:
            fail("The business data identity could not be created.", str(exc))
    require_plain_file(marker)
    try:
        marker_value = marker.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        fail("The business data identity could not be read.", str(exc))
    if marker_value != EXPECTED_INSTANCE_ID:
        fail(
            "The Documents business directory belongs to another application version.",
            str(marker),
        )
    if os.name == "nt":
        try:
            kernel32 = _win_dll("kernel32")
            kernel32.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
            kernel32.GetFileAttributesW.restype = ctypes.c_uint32
            kernel32.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
            kernel32.SetFileAttributesW.restype = ctypes.c_int
            current = kernel32.GetFileAttributesW(str(marker))
            if current != 0xFFFFFFFF:
                kernel32.SetFileAttributesW(str(marker), current | 0x2)
        except Exception:
            pass
    return data_dir


def initialize_export_directory(data_dir: Path) -> Path:
    """Keep mutable work data on C while placing per-user deliverables on D."""
    preferred_drive = Path("D:/")
    if preferred_drive.is_dir():
        export_root = preferred_drive / EXPORT_DIRECTORY_NAME
    else:
        # A machine without D remains usable, but still keeps exports outside
        # each tenant's private queue/material tree.
        export_root = data_dir / "\u591a\u7528\u6237\u6210\u7247"
    ensure_plain_directory(export_root)
    return export_root.resolve(strict=True)


def _powershell_path() -> Path:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    path = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not path.is_file():
        fail("Windows PowerShell is unavailable.", str(path))
    return path


def encoded_powershell(script: str) -> str:
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")


def run_powershell(script: str, timeout: float = 20.0) -> subprocess.CompletedProcess[bytes]:
    command = [
        str(_powershell_path()),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded_powershell(script),
    ]
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail("A Windows network check could not run.", str(exc))


def select_private_lan_ipv4() -> str:
    completed = run_powershell(PRIVATE_LAN_DISCOVERY_SCRIPT, timeout=20.0)
    if completed.returncode != 0:
        fail(
            "No usable physical adapter on a Windows Private network was found.",
            f"PowerShell exit code {completed.returncode}",
        )
    try:
        payload = json.loads(completed.stdout.decode("utf-8-sig"))
        raw = payload["Address"]
        address = ipaddress.ip_address(raw)
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        fail("Windows returned an invalid private LAN address.", str(exc))
    if address.version != 4 or not any(address in network for network in PRIVATE_NETWORKS):
        fail("The selected address is not a supported private IPv4 address.", str(address))
    return str(address)


def _path_b64(path: Path) -> str:
    return base64.b64encode(str(path).encode("utf-16le")).decode("ascii")


def firewall_probe_script() -> str:
    return rf"""
$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

function Get-RawValue([object]$object, [string]$name) {{
  if ($null -eq $object) {{ return $null }}
  $property = $object.CimInstanceProperties[$name]
  if ($null -eq $property) {{ return $null }}
  return $property.Value
}}
function Get-RawInt([object]$object, [string]$name) {{
  $value = Get-RawValue $object $name
  if ($null -eq $value) {{ return $null }}
  return [int]$value
}}

$rules = @(Get-NetFirewallRule -DisplayName '{FIREWALL_RULE}' `
  -PolicyStore PersistentStore -ErrorAction SilentlyContinue)
$apps = @()
$ports = @()
$addresses = @()
if ($rules.Count -eq 1) {{
  $apps = @(Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $rules[0])
  $ports = @(Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rules[0])
  $addresses = @(Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $rules[0])
}}

$rawEnabled = $null
$rawProfiles = $null
$rawDirection = $null
$rawAction = $null
$rawEdge = $null
$displayEnabled = $null
$displayProfile = $null
$displayDirection = $null
$displayAction = $null
$displayEdge = $null
if ($rules.Count -eq 1) {{
  $rawEnabled = Get-RawInt $rules[0] 'Enabled'
  $rawProfiles = Get-RawInt $rules[0] 'Profiles'
  $rawDirection = Get-RawInt $rules[0] 'Direction'
  $rawAction = Get-RawInt $rules[0] 'Action'
  $rawEdge = Get-RawInt $rules[0] 'EdgeTraversalPolicy'
  $displayEnabled = [string]$rules[0].Enabled
  $displayProfile = [string]$rules[0].Profile
  $displayDirection = [string]$rules[0].Direction
  $displayAction = [string]$rules[0].Action
  $displayEdge = [string]$rules[0].EdgeTraversalPolicy
}}

$rawAppPath = $null
$displayProgram = $null
if ($apps.Count -eq 1) {{
  $rawAppPath = [string](Get-RawValue $apps[0] 'AppPath')
  $displayProgram = [string]$apps[0].Program
}}

$rawProtocol = $null
$rawLocalPort = @()
$displayProtocol = $null
$displayLocalPort = @()
if ($ports.Count -eq 1) {{
  $rawProtocol = Get-RawValue $ports[0] 'Protocol'
  $rawLocalPort = @(Get-RawValue $ports[0] 'LocalPort')
  $displayProtocol = [string]$ports[0].Protocol
  $displayLocalPort = @($ports[0].LocalPort)
}}

$rawRemoteAddress = @()
$displayRemoteAddress = @()
if ($addresses.Count -eq 1) {{
  $rawRemoteAddress = @(Get-RawValue $addresses[0] 'RemoteAddress')
  $displayRemoteAddress = @($addresses[0].RemoteAddress)
}}

[PSCustomObject]@{{
  RuleCount = [int]$rules.Count
  ApplicationFilterCount = [int]$apps.Count
  PortFilterCount = [int]$ports.Count
  AddressFilterCount = [int]$addresses.Count
  RawEnabled = $rawEnabled
  RawProfiles = $rawProfiles
  RawDirection = $rawDirection
  RawAction = $rawAction
  RawEdgeTraversalPolicy = $rawEdge
  RawAppPath = $rawAppPath
  RawProtocol = $rawProtocol
  RawLocalPort = @($rawLocalPort)
  RawRemoteAddress = @($rawRemoteAddress)
  DisplayEnabled = $displayEnabled
  DisplayProfile = $displayProfile
  DisplayDirection = $displayDirection
  DisplayAction = $displayAction
  DisplayEdgeTraversalPolicy = $displayEdge
  DisplayProgram = $displayProgram
  DisplayProtocol = $displayProtocol
  DisplayLocalPort = @($displayLocalPort)
  DisplayRemoteAddress = @($displayRemoteAddress)
}} | ConvertTo-Json -Compress -Depth 4
""".strip()


def _strict_int(value: Any, expected: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _json_array(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def firewall_snapshot_matches(snapshot: dict[str, Any], python_exe: Path) -> bool:
    for name in (
        "RuleCount",
        "ApplicationFilterCount",
        "PortFilterCount",
        "AddressFilterCount",
    ):
        if not _strict_int(snapshot.get(name), 1):
            return False
    for name, expected in (
        ("RawEnabled", 1),
        ("RawProfiles", 2),
        ("RawDirection", 1),
        ("RawAction", 2),
        ("RawEdgeTraversalPolicy", 0),
    ):
        if not _strict_int(snapshot.get(name), expected):
            return False

    raw_path = snapshot.get("RawAppPath")
    if not isinstance(raw_path, str) or raw_path.casefold() != str(python_exe).casefold():
        return False

    protocol = snapshot.get("RawProtocol")
    if isinstance(protocol, bool) or str(protocol).casefold() not in {"tcp", "6"}:
        return False

    local_ports = _json_array(snapshot.get("RawLocalPort"))
    if len(local_ports) != 1 or str(local_ports[0]) != str(SERVER_PORT):
        return False

    remote_values = _json_array(snapshot.get("RawRemoteAddress"))
    if not remote_values or not all(isinstance(value, str) for value in remote_values):
        return False
    remote = [value.casefold() for value in remote_values]
    if len(remote) != len(set(remote)) or not set(remote).issubset(FIREWALL_REMOTE_ALIASES):
        return False
    if "localsubnet" not in remote and "localsubnet4" not in remote:
        return False
    return True


def read_firewall_snapshot() -> dict[str, Any]:
    completed = run_powershell(firewall_probe_script(), timeout=20.0)
    stdout = completed.stdout.decode("utf-8-sig", errors="replace").strip()
    stderr = completed.stderr.decode("utf-8-sig", errors="replace").strip()
    if completed.returncode != 0:
        fail(
            "Windows could not inspect the Private-LAN firewall rule.",
            f"PowerShell exit code {completed.returncode}: {stderr or stdout}",
        )
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        fail(
            "Windows returned invalid firewall rule details.",
            f"{exc}: {stdout[:1000]}",
        )
    if not isinstance(payload, dict):
        fail("Windows returned invalid firewall rule details.", type(payload).__name__)
    return payload


def firewall_snapshot_text(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def firewall_rule_matches(python_exe: Path) -> bool:
    return firewall_snapshot_matches(read_firewall_snapshot(), python_exe)


def firewall_install_script(python_exe: Path) -> str:
    return rf"""
$ErrorActionPreference = 'Stop'
$path = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('{_path_b64(python_exe)}'))
$expected = '{CRITICAL_HASHES['resources/app/runtime/python/v16-runtime.exe']}'
function Get-RawValue([object]$object, [string]$name) {{
  if ($null -eq $object) {{ return $null }}
  $property = $object.CimInstanceProperties[$name]
  if ($null -eq $property) {{ return $null }}
  return $property.Value
}}
$private = @(Get-NetConnectionProfile | Where-Object {{ $_.NetworkCategory -eq 'Private' }})
if ($private.Count -lt 1) {{ exit 61 }}
if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {{ exit 62 }}
$actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $expected) {{ exit 63 }}
Get-NetFirewallRule -DisplayName '{FIREWALL_RULE}' `
  -PolicyStore PersistentStore -ErrorAction SilentlyContinue |
  Remove-NetFirewallRule -ErrorAction Stop
New-NetFirewallRule -DisplayName '{FIREWALL_RULE}' `
  -Description 'Kuaishou v25 GPU server; private local subnet only.' `
  -Enabled True -Direction Inbound -Action Allow -Program $path -Protocol TCP -LocalPort 8020 `
  -Profile Private -RemoteAddress LocalSubnet4 -EdgeTraversalPolicy Block `
  -PolicyStore PersistentStore |
  Out-Null

Start-Sleep -Milliseconds 250
$rules = @(Get-NetFirewallRule -DisplayName '{FIREWALL_RULE}' `
  -PolicyStore PersistentStore -ErrorAction Stop)
if ($rules.Count -ne 1) {{ exit 64 }}
$rule = $rules[0]
$apps = @(Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $rule)
$ports = @(Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule)
$addresses = @(Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $rule)
if ($apps.Count -ne 1 -or $ports.Count -ne 1 -or $addresses.Count -ne 1) {{ exit 65 }}
$rawEnabled = Get-RawValue $rule 'Enabled'
$rawProfiles = Get-RawValue $rule 'Profiles'
$rawDirection = Get-RawValue $rule 'Direction'
$rawAction = Get-RawValue $rule 'Action'
$rawEdge = Get-RawValue $rule 'EdgeTraversalPolicy'
if ($null -eq $rawEnabled -or $null -eq $rawProfiles -or
    $null -eq $rawDirection -or $null -eq $rawAction -or
    $null -eq $rawEdge) {{ exit 66 }}
if ([int]$rawEnabled -ne 1 -or [int]$rawProfiles -ne 2 -or
    [int]$rawDirection -ne 1 -or [int]$rawAction -ne 2 -or
    [int]$rawEdge -ne 0) {{ exit 66 }}
$rawAppPath = [string](Get-RawValue $apps[0] 'AppPath')
if ($rawAppPath -ine $path) {{ exit 67 }}
$rawProtocol = [string](Get-RawValue $ports[0] 'Protocol')
if ($rawProtocol -ine 'TCP' -and $rawProtocol -ne '6') {{ exit 68 }}
$rawLocalPort = @(Get-RawValue $ports[0] 'LocalPort')
if ($rawLocalPort.Count -ne 1 -or [string]$rawLocalPort[0] -ne '8020') {{ exit 69 }}
$rawRemote = @(Get-RawValue $addresses[0] 'RemoteAddress')
$allowedRemote = @('LocalSubnet', 'LocalSubnet4', 'LocalSubnet6')
if ($rawRemote.Count -lt 1 -or
    @($rawRemote | Sort-Object -Unique).Count -ne $rawRemote.Count) {{ exit 70 }}
foreach ($entry in $rawRemote) {{
  if ($allowedRemote -inotcontains [string]$entry) {{ exit 70 }}
}}
if ($rawRemote -inotcontains 'LocalSubnet' -and
    $rawRemote -inotcontains 'LocalSubnet4') {{ exit 71 }}
exit 0
""".strip()


def firewall_remove_script(python_exe: Path) -> str:
    return rf"""
$ErrorActionPreference = 'Stop'
$path = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('{_path_b64(python_exe)}'))
function Get-RawValue([object]$object, [string]$name) {{
  if ($null -eq $object) {{ return $null }}
  $property = $object.CimInstanceProperties[$name]
  if ($null -eq $property) {{ return $null }}
  return $property.Value
}}
$rules = @(Get-NetFirewallRule -DisplayName '{FIREWALL_RULE}' `
  -PolicyStore PersistentStore -ErrorAction SilentlyContinue)
foreach ($rule in $rules) {{
  $apps = @(Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $rule)
  if ($apps.Count -eq 1) {{
    $rawAppPath = [string](Get-RawValue $apps[0] 'AppPath')
    if ($rawAppPath -ieq $path) {{ $rule | Remove-NetFirewallRule -ErrorAction Stop }}
  }}
}}
$remaining = @(Get-NetFirewallRule -DisplayName '{FIREWALL_RULE}' `
  -PolicyStore PersistentStore -ErrorAction SilentlyContinue)
foreach ($rule in $remaining) {{
  $apps = @(Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $rule)
  if ($apps.Count -eq 1) {{
    $rawAppPath = [string](Get-RawValue $apps[0] 'AppPath')
    if ($rawAppPath -ieq $path) {{ exit 81 }}
  }}
}}
exit 0
""".strip()


class _ShellExecuteInfoW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("fMask", ctypes.c_uint32),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_uint32),
        ("hIconOrMonitor", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]


def run_elevated_powershell(script: str, timeout_ms: int = 60000) -> int:
    shell32 = _win_dll("shell32")
    kernel32 = _win_dll("kernel32")
    parameters = (
        "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass "
        f"-EncodedCommand {encoded_powershell(script)}"
    )
    info = _ShellExecuteInfoW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00000100 | 0x00000400
    info.lpVerb = "runas"
    info.lpFile = str(_powershell_path())
    info.lpParameters = parameters
    info.nShow = 0
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfoW)]
    shell32.ShellExecuteExW.restype = ctypes.c_int
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.TerminateProcess.restype = ctypes.c_int
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    if not shell32.ShellExecuteExW(ctypes.byref(info)) or not info.hProcess:
        error = ctypes.get_last_error()
        if error == ERROR_CANCELLED:
            fail("Administrator confirmation was cancelled; LAN access was not opened.")
        fail("Windows could not start the firewall authorization helper.", str(error))
    try:
        waited = kernel32.WaitForSingleObject(info.hProcess, timeout_ms)
        if waited != WAIT_OBJECT_0:
            if waited == WAIT_TIMEOUT:
                kernel32.TerminateProcess(info.hProcess, 124)
                kernel32.WaitForSingleObject(info.hProcess, 5000)
            fail("Windows firewall authorization timed out.", str(waited))
        exit_code = ctypes.c_uint32(1)
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            fail("Windows could not read the firewall helper result.")
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(info.hProcess)


def ensure_firewall(python_exe: Path) -> None:
    try:
        initial = read_firewall_snapshot()
    except ManagerError as exc:
        log(
            "WARNING: the ordinary-user firewall probe could not read the "
            "policy store; the elevated helper will perform the authoritative "
            "raw CIM verification."
        )
        log(f"Ordinary firewall probe: {exc.friendly} | {exc.detail}")
    else:
        if firewall_snapshot_matches(initial, python_exe):
            return
    result = run_elevated_powershell(firewall_install_script(python_exe))
    if result != 0:
        meanings = {
            61: "no connected Windows Private network",
            62: "bundled Python path is missing",
            63: "bundled Python hash mismatch",
            64: "created rule count is not exactly one",
            65: "associated filter count is not exactly one",
            66: "enabled/profile/direction/action/edge raw CIM values mismatch",
            67: "raw AppPath does not equal the bundled Python path",
            68: "raw protocol is not TCP/6",
            69: "raw local port is not exactly TCP 8020",
            70: "raw remote address is outside the LocalSubnet aliases",
            71: "raw remote address does not cover the local IPv4 subnet",
        }
        fail(
            "Windows did not create the required Private-LAN firewall rule.",
            f"helper exit code {result}: {meanings.get(result, 'PowerShell or Windows policy error')}",
        )
    # Exit code 0 is issued only after the elevated helper has read back and
    # validated every raw CIM field.  A second ordinary-user query must not
    # overrule that stronger same-process verification on Windows 10.
    return


def remove_firewall(python_exe: Path) -> None:
    result = run_elevated_powershell(firewall_remove_script(python_exe))
    if result != 0:
        fail(
            "Windows could not remove the product-owned firewall rule.",
            "the elevated removal helper still found the exact rule"
            if result == 81
            else str(result),
        )


class NamedMutex:
    def __init__(self, name: str, busy_message: str) -> None:
        self.name = name
        self.busy_message = busy_message
        self.handle: int | None = None
        self.owned = False

    def __enter__(self) -> "NamedMutex":
        kernel32 = _win_dll("kernel32")
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            fail("Windows could not create a server instance lock.", str(ctypes.get_last_error()))
        self.handle = int(handle)
        waited = kernel32.WaitForSingleObject(handle, 0)
        if waited in (WAIT_OBJECT_0, WAIT_ABANDONED):
            self.owned = True
            return self
        kernel32.CloseHandle(handle)
        self.handle = None
        if waited == WAIT_TIMEOUT:
            if self.name == MANAGER_MUTEX:
                raise ManagerAlreadyRunning(self.busy_message)
            fail(self.busy_message)
        fail("Windows could not acquire a server instance lock.", str(waited))

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        if self.handle is None:
            return
        kernel32 = _win_dll("kernel32")
        kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel32.ReleaseMutex.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        if self.owned:
            kernel32.ReleaseMutex(self.handle)
        kernel32.CloseHandle(self.handle)
        self.handle = None
        self.owned = False


def desktop_application_running() -> bool:
    _require_windows()

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_uint32),
            ("cntUsage", ctypes.c_uint32),
            ("th32ProcessID", ctypes.c_uint32),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.c_uint32),
            ("cntThreads", ctypes.c_uint32),
            ("th32ParentProcessID", ctypes.c_uint32),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_uint32),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = _win_dll("kernel32")
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32FirstW.restype = ctypes.c_int
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32NextW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot in (None, 0, INVALID_HANDLE_VALUE):
        return True
    entry = ProcessEntry32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return True
        while True:
            if entry.szExeFile.casefold() == DESKTOP_PROCESS_NAME.casefold():
                return True
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                return False
    finally:
        kernel32.CloseHandle(snapshot)


def http_json(
    connect_host: str,
    selected_host: str,
    port: int,
    path: str,
    *,
    method: str = "GET",
    token: str = "",
    timeout: float = 0.7,
) -> tuple[int, dict[str, Any]] | None:
    connection = http.client.HTTPConnection(connect_host, port, timeout=timeout)
    headers = {"Host": f"{selected_host}:{port}", "Connection": "close"}
    if token:
        headers["X-Kuaishou-Server-Control"] = token
    try:
        connection.request(method, path, body=b"" if method != "GET" else None, headers=headers)
        response = connection.getresponse()
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            return None
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        return int(response.status), payload
    except (OSError, TimeoutError, UnicodeError, json.JSONDecodeError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def expected_server_info(result: tuple[int, dict[str, Any]] | None, pid: int = 0) -> bool:
    if result is None or result[0] != 200:
        return False
    payload = result[1]
    if (
        payload.get("mode") != "authenticated-multiuser-gateway"
        or payload.get("release_id") != EXPECTED_RELEASE_ID
        or payload.get("instance_id") != EXPECTED_INSTANCE_ID
        or payload.get("port") != SERVER_PORT
        or payload.get("global_concurrent_tasks") != 3
        or payload.get("per_user_concurrent_tasks") != 3
        or payload.get("public_concurrent_tasks") != 3
        or payload.get("local_reserved_concurrent_tasks") != 0
        or payload.get("entry") != "public"
    ):
        return False
    return not pid or payload.get("service_pid") == pid


def _tcp_open(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def assert_ports_free(lan_ip: str) -> None:
    for host in ("127.0.0.1", lan_ip):
        if expected_server_info(http_json(host, lan_ip, SERVER_PORT, "/server-info", timeout=0.4)):
            fail("Another copy of this GPU server is already running.", host)
    for port in range(LEGACY_DESKTOP_PORT_MIN, LEGACY_DESKTOP_PORT_MAX + 1):
        for host in ("127.0.0.1", lan_ip):
            if not _tcp_open(host, port, timeout=0.15):
                continue
            result = http_json(host, lan_ip, port, "/status", timeout=0.25)
            if result and result[0] == 200:
                payload = result[1]
                if (
                    payload.get("release_id") == EXPECTED_RELEASE_ID
                    and payload.get("instance_id") == EXPECTED_INSTANCE_ID
                ):
                    fail(
                        "The ordinary Kuaishou Automation app is still running.",
                        f"{host}:{port}",
                    )
    if _tcp_open("127.0.0.1", SERVER_PORT) or _tcp_open(lan_ip, SERVER_PORT):
        fail("TCP port 8020 is already in use by another program.")


def acquire_loopback_guards() -> list[socket.socket]:
    guards: list[socket.socket] = []
    try:
        for port in range(LEGACY_DESKTOP_PORT_MIN, LEGACY_DESKTOP_PORT_MAX + 1):
            guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
            guard.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            guard.bind(("127.0.0.1", port))
            guards.append(guard)
        return guards
    except OSError as exc:
        for guard in guards:
            guard.close()
        fail("The ordinary app or an old server is still using protected ports.", str(exc))


def build_server_environment(
    paths: InstallPaths,
    data_dir: Path,
    export_root: Path,
    token: str,
    lan_ip: str,
) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in SCRUBBED_ENVIRONMENT and not _retired_external_environment(key)
    }
    environment.update(
        {
            "PORT": "8020",
            "V16_HOST": lan_ip,
            "V16_DATA_DIR": str(data_dir),
            "V17_MULTIUSER_ROOT": str(data_dir / "多用户"),
            "V17_MULTIUSER_EXPORT_ROOT": str(export_root),
            # Sign-in uses the bundled loopback-only broker. Usage records
            # go directly to ERP; no external billing/backup/relay is loaded.
            "V17_AUTH_MODE": "local_erp",
            "V17_TENANT_PORT_MIN": "18820",
            "V17_TENANT_PORT_MAX": "18919",
            "V17_TENANT_RENDER_WORKERS": "3",
            # No per-account compute reservation: any active tenant may fill
            # the three host-wide render slots while they are available.
            "V17_TENANT_QUEUE_WORKERS": "3",
            "V16_CLIENT_NAME": socket.gethostname(),
            "V17_DISPLAY_NAME": DISPLAY_NAME,
            "V17_INSTANCE_ID": EXPECTED_INSTANCE_ID,
            "V17_SESSION_COOKIE_NAME": "kuaishou_automation_server_session",
            "V17_DEFAULT_VISION_PROVIDER": "qwen",
            "V16_ERP_TOKEN_FILE": str(data_dir / ".v16_erp_bearer_token"),
            "V16_ERP_IDENTITY_FILE": str(data_dir / ".v16_erp_identity.json"),
            "V16_UPLOAD_ORDER_FILE": str(data_dir / ".v16_upload_order.json"),
            "V16_PRICE_CACHE_DIR": str(data_dir / ".v16_cache" / "price"),
            "V16_TEXT_CACHE_DIR": str(data_dir / ".v16_cache" / "text"),
            "V16_SUBTITLE_CACHE_DIR": str(data_dir / ".v16_cache" / "subtitles"),
            "V16_FACE_CACHE_DIR": str(data_dir / ".v16_cache" / "face"),
            "V16_TTS_CACHE_DIR": str(data_dir / ".v16_cache" / "tts"),
            "V16_VIDEOCAPTIONER_DATA_DIR": str(
                data_dir / ".v16_cache" / "videocaptioner"
            ),
            "V16_INSIGHTFACE_ROOT": str(paths.runtime_dir / "insightface"),
            "V16_FFMPEG_EXE": str(paths.ffmpeg_exe),
            "V16_FFPROBE_EXE": str(paths.ffprobe_exe),
            "V16_TESSERACT_EXE": str(paths.tesseract_dir / "tesseract.exe"),
            "V16_PYTHON_EXE": str(paths.python_exe),
            "V25_SERVER_SOURCE_DIR": str(paths.source_dir),
            "V25_SERVER_CONTROL_TOKEN": token,
            "V25_SERVER_CONTROL_LOCAL_IP": lan_ip,
            "TESSDATA_PREFIX": str(paths.tesseract_dir / "tessdata"),
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            # The desktop launcher may inherit a restricted PATHEXT (for
            # example only .CPL).  Python's shutil.which then reports bundled
            # *.exe tools as missing even though PATH is correct.
            "PATHEXT": ".COM;.EXE;.BAT;.CMD;.CPL",
        }
    )
    # Only the direct-ERP enable switch remains configurable for this server.
    # Do not read/delete shared HKCU credentials used by other applications.
    for variable_name in SERVER_USER_ENVIRONMENT:
        configured_value = read_persistent_user_environment(variable_name)
        if not configured_value:
            configured_value = os.environ.get(variable_name, "").strip()
        if configured_value:
            environment[variable_name] = configured_value
    environment["V16_ERP_RELAY_ENABLED"] = "0"
    original_path = environment.get("PATH", "")
    prefixes = [str(paths.ffmpeg_dir), str(paths.tesseract_dir), str(paths.python_dir)]
    environment["PATH"] = os.pathsep.join(prefixes + ([original_path] if original_path else []))
    return environment


class WindowsJob:
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        pass

    ExtendedLimitInformation._fields_ = [
        ("BasicLimitInformation", BasicLimitInformation),
        ("IoInfo", IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]

    def __init__(self) -> None:
        kernel32 = _win_dll("kernel32")
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            fail("Windows could not create process-tree protection.", str(ctypes.get_last_error()))
        self.handle: int | None = int(handle)
        limits = self.ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(self.handle)
            self.handle = None
            fail("Windows could not enable process-tree protection.", str(error))

    def assign(self, pid: int) -> None:
        if self.handle is None:
            fail("The process-tree protection handle is closed.")
        kernel32 = _win_dll("kernel32")
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        process = kernel32.OpenProcess(0x0001 | 0x0100 | 0x1000, False, pid)
        if not process:
            fail("Windows could not open the server process for protection.", str(pid))
        try:
            if not kernel32.AssignProcessToJobObject(self.handle, process):
                fail("Windows could not attach the server to process-tree protection.", str(ctypes.get_last_error()))
        finally:
            kernel32.CloseHandle(process)

    def terminate(self, exit_code: int = 1) -> None:
        if self.handle is not None:
            kernel32 = _win_dll("kernel32")
            kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.TerminateJobObject.restype = ctypes.c_int
            kernel32.TerminateJobObject(self.handle, exit_code)

    def close(self) -> None:
        if self.handle is not None:
            kernel32 = _win_dll("kernel32")
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> "WindowsJob":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()


def launch_server(paths: InstallPaths, environment: dict[str, str], server_log: Path) -> subprocess.Popen[bytes]:
    command = [
        str(paths.python_exe),
        "-I",
        "-B",
        "-X",
        "utf8",
        "-u",
        str(paths.server_runtime),
    ]
    try:
        output = server_log.open("ab", buffering=0)
    except OSError as exc:
        fail("The server log cannot be opened.", str(exc))
    try:
        process = subprocess.Popen(
            command,
            cwd=paths.program_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        )
    except OSError as exc:
        output.close()
        fail("The bundled x64 Python server could not be started.", str(exc))
    output.close()
    return process


def wait_until_healthy(process: subprocess.Popen[bytes], lan_ip: str, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        result = http_json(lan_ip, lan_ip, SERVER_PORT, "/server-info", timeout=0.7)
        if expected_server_info(result, process.pid):
            return
        time.sleep(0.3)
    fail(
        "The GPU LAN server did not become healthy within 45 seconds.",
        f"child exit code {process.poll()}",
    )


def graceful_stop(process: subprocess.Popen[bytes], job: WindowsJob, lan_ip: str, token: str) -> bool:
    accepted = http_json(
        lan_ip,
        lan_ip,
        SERVER_PORT,
        "/server-control/shutdown",
        method="POST",
        token=token,
        timeout=5.0,
    )
    graceful = bool(accepted and accepted[0] == 202)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        graceful = False
        job.terminate(1)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
    return graceful


def open_browser(address: str) -> None:
    try:
        os.startfile(address)  # type: ignore[attr-defined]
    except OSError as exc:
        log(f"Browser did not open automatically: {exc}")


def set_console_title() -> None:
    if os.name != "nt":
        return
    try:
        kernel32 = _win_dll("kernel32")
        kernel32.SetConsoleOutputCP.argtypes = [ctypes.c_uint32]
        kernel32.SetConsoleOutputCP.restype = ctypes.c_int
        kernel32.SetConsoleTitleW.argtypes = [ctypes.c_wchar_p]
        kernel32.SetConsoleTitleW.restype = ctypes.c_int
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleTitleW(f"{DISPLAY_NAME} - {SERVER_PORT}")
    except Exception:
        pass


def handle_existing_manager(no_browser: bool = False) -> int:
    """Treat a second double-click as a normal, non-destructive user action."""
    log("The first GPU server window is already running.")
    try:
        lan_ip = select_private_lan_ipv4()
    except ManagerError:
        print(
            "[INFO] The server is already starting in the first window. "
            "Keep that window open.",
            flush=True,
        )
        return ALREADY_RUNNING_EXIT_CODE

    for connect_host in (lan_ip, "127.0.0.1"):
        result = http_json(
            connect_host,
            lan_ip,
            SERVER_PORT,
            "/server-info",
            timeout=0.7,
        )
        if not expected_server_info(result):
            continue
        address = f"http://{lan_ip}:{SERVER_PORT}"
        log(f"SERVER ALREADY READY: {address}")
        print(f"[INFO] The server is already running: {address}", flush=True)
        if not no_browser:
            open_browser(address)
        return ALREADY_RUNNING_EXIT_CODE

    print(
        "[INFO] The server is starting in the first window. Keep that window open.",
        flush=True,
    )
    return ALREADY_RUNNING_EXIT_CODE


def run_server(no_browser: bool = False) -> int:
    global _launcher_log
    _require_windows()
    set_console_title()
    paths = discover_install()
    logs = initialize_logs()
    _launcher_log = logs.launcher
    stop_event = threading.Event()
    old_handlers: dict[int, Any] = {}

    def request_stop(signum: int, _frame: Any) -> None:
        log(f"Stop requested by signal {signum}.")
        stop_event.set()

    for signum in (signal.SIGINT, getattr(signal, "SIGBREAK", signal.SIGTERM)):
        old_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)

    guards: list[socket.socket] = []
    process: subprocess.Popen[bytes] | None = None
    try:
        with NamedMutex(
            MANAGER_MUTEX,
            "The GPU server manager is already running. Check the existing window.",
        ), NamedMutex(
            INSTALL_MUTEX,
            "v25 is currently installing or updating. Wait for it to finish.",
        ):
            log("Checking the tuned v25/RTX5070 portable package...")
            verify_manifest_contract(paths)
            verify_critical_files(paths)
            if desktop_application_running():
                fail(
                    "The ordinary Kuaishou Automation app is running. Exit it before starting the server."
                )
            data_dir = initialize_data_directory(paths)
            export_root = initialize_export_directory(data_dir)
            log("Selecting a physical adapter on a Windows Private network...")
            lan_ip = select_private_lan_ipv4()
            assert_ports_free(lan_ip)
            guards = acquire_loopback_guards()
            log("Checking the dedicated Private-LAN firewall rule...")
            ensure_firewall(paths.python_exe)
            token = secrets.token_hex(32)
            environment = build_server_environment(
                paths,
                data_dir,
                export_root,
                token,
                lan_ip,
            )
            address = f"http://{lan_ip}:{SERVER_PORT}"
            browser_opened = False
            unexpected_restarts = 0
            while not stop_event.is_set():
                log("Starting the tuned v25/RTX5070 server on the LAN...")
                launch_error = None
                exit_code = None
                ready_since_monotonic = None
                with WindowsJob() as job:
                    process = None
                    try:
                        process = launch_server(paths, environment, logs.server)
                        job.assign(process.pid)
                        wait_until_healthy(process, lan_ip)
                    except Exception as exc:
                        launch_error = exc
                        job.terminate(1)
                        if process is not None:
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                    if launch_error is None and process is not None:
                        ready_since_monotonic = time.monotonic()
                        log(f"SERVER READY: {address}")
                        print("=" * 68, flush=True)
                        print(f"  {DISPLAY_NAME}", flush=True)
                        print(f"  LAN address: {address}", flush=True)
                        print(
                            "  Queue-first supervisor is active. Press Ctrl+C to stop safely.",
                            flush=True,
                        )
                        print("=" * 68, flush=True)
                        if not no_browser and not browser_opened:
                            open_browser(address)
                            browser_opened = True
                        while process.poll() is None and not stop_event.wait(0.5):
                            pass
                        if stop_event.is_set() and process.poll() is None:
                            log("Stopping the server and active render processes safely...")
                            graceful = graceful_stop(process, job, lan_ip, token)
                            log(
                                "Server stopped gracefully."
                                if graceful
                                else "Server stopped with job fallback."
                            )
                            return 0
                        exit_code = process.poll()
                        # Remove every surviving tenant/renderer descendant before
                        # relaunch. Durable SQLite queue rows recover in FIFO order.
                        job.terminate(1)
                if stop_event.is_set():
                    return 0
                if (
                    ready_since_monotonic is not None
                    and time.monotonic() - ready_since_monotonic >= 5 * 60
                ):
                    unexpected_restarts = 0
                unexpected_restarts += 1
                retry_delay = min(30.0, float(2 ** min(unexpected_restarts, 5)))
                if launch_error is not None:
                    log(
                        "Server launch/health check failed; queue data is durable and "
                        f"the supervisor will retry in {retry_delay:.0f}s: {launch_error}"
                    )
                else:
                    log(
                        "Server stopped unexpectedly; queue data is durable and "
                        f"the supervisor will retry in {retry_delay:.0f}s "
                        f"(exit code {exit_code})."
                    )
                if stop_event.wait(retry_delay):
                    return 0
    finally:
        for guard in guards:
            guard.close()
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def self_test() -> int:
    paths = discover_install()
    verify_manifest_contract(paths)
    verify_critical_files(paths)
    print("PORTABLE_MANAGER_SELF_TEST_OK", flush=True)
    return 0


def diagnose() -> int:
    """Print read-only target evidence without starting or changing the server."""
    _require_windows()
    set_console_title()
    print(f"portable_manager_version={MANAGER_VERSION}")
    print(f"python_executable={sys.executable}")
    print(f"python_version={sys.version.split()[0]}")
    print(f"python_pointer_bits={ctypes.sizeof(ctypes.c_void_p) * 8}")
    failures = 0
    try:
        paths = discover_install()
        print(f"program_root={paths.program_root}")
    except ManagerError as exc:
        print(f"install_tree=FAIL {exc.friendly} | {exc.detail}")
        print("PORTABLE_MANAGER_DIAGNOSTIC_COMPLETE failures=1")
        return 0

    checks = (
        ("manifest_contract", lambda: verify_manifest_contract(paths)),
        ("critical_hashes", lambda: verify_critical_files(paths)),
    )
    for name, check in checks:
        try:
            check()
            print(f"{name}=OK")
        except ManagerError as exc:
            failures += 1
            print(f"{name}=FAIL {exc.friendly} | {exc.detail}")

    lan_ip = ""
    try:
        lan_ip = select_private_lan_ipv4()
        print(f"private_lan_ipv4=OK {lan_ip}")
    except ManagerError as exc:
        failures += 1
        print(f"private_lan_ipv4=FAIL {exc.friendly} | {exc.detail}")

    try:
        running = desktop_application_running()
        print(f"ordinary_desktop_running={'YES' if running else 'NO'}")
        if running:
            failures += 1
    except ManagerError as exc:
        failures += 1
        print(f"ordinary_desktop_running=UNKNOWN {exc.friendly} | {exc.detail}")

    try:
        snapshot = read_firewall_snapshot()
        print(f"private_lan_firewall_probe={firewall_snapshot_text(snapshot)}")
        matches = firewall_snapshot_matches(snapshot, paths.python_exe)
        print(f"private_lan_firewall_rule={'MATCH' if matches else 'MISSING_OR_MISMATCH'}")
        if not matches:
            failures += 1
    except ManagerError as exc:
        failures += 1
        print(f"private_lan_firewall_rule=UNKNOWN {exc.friendly} | {exc.detail}")

    print(f"tcp_127_0_0_1_8020={'OPEN' if _tcp_open('127.0.0.1', SERVER_PORT) else 'CLOSED'}")
    if lan_ip:
        print(f"tcp_{lan_ip}_8020={'OPEN' if _tcp_open(lan_ip, SERVER_PORT) else 'CLOSED'}")
        existing = http_json(lan_ip, lan_ip, SERVER_PORT, "/server-info", timeout=0.7)
        print(f"expected_server_info={'YES' if expected_server_info(existing) else 'NO'}")
    print(f"PORTABLE_MANAGER_DIAGNOSTIC_COMPLETE failures={failures}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--remove-firewall", action="store_true")
    parser.add_argument("--version", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        if arguments.version:
            print(f"Kuaishou v25 RTX5070 multi-user portable Python manager {MANAGER_VERSION}")
            return 0
        if arguments.diagnose:
            return diagnose()
        if arguments.self_test:
            return self_test()
        if arguments.remove_firewall:
            _require_windows()
            paths = discover_install()
            verify_critical_files(paths)
            remove_firewall(paths.python_exe)
            print("The product-owned firewall rule is absent.", flush=True)
            return 0
        return run_server(no_browser=arguments.no_browser)
    except ManagerAlreadyRunning:
        return handle_existing_manager(no_browser=arguments.no_browser)
    except ManagerError as exc:
        log(f"START FAILED: {exc.friendly}")
        if exc.detail:
            log(f"DETAIL: {exc.detail}")
        print("", flush=True)
        print(f"[ERROR] {exc.friendly}", flush=True)
        if exc.detail:
            print(f"Details: {exc.detail}", flush=True)
        return 1
    except KeyboardInterrupt:
        log("Interrupted by the user.")
        return 130
    except Exception as exc:
        log(f"UNEXPECTED FAILURE: {type(exc).__name__}: {exc}")
        print(f"[ERROR] Unexpected startup failure: {type(exc).__name__}: {exc}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
