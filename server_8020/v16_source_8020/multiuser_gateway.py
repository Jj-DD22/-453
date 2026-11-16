"""Authenticated per-user gateway for the Kuaishou GPU server.

The public process owns authentication and never imports the mutable editing
application.  Every authenticated DingTalk identity is routed to a loopback-
only backend with its own data directory, SQLite queue, materials, temporary
files, output tree and preferences. A separate host-wide lock file limits all
tenant backends to three simultaneous production tasks.
"""

from __future__ import annotations

import atexit
import base64
import ctypes
from ctypes import wintypes
import hashlib
import hmac
import http.client
import json
import os
import queue
import re
import secrets
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

try:
    import fcntl as _fcntl
except ImportError:  # Windows does not provide fcntl.
    _fcntl = None

try:
    import msvcrt as _msvcrt
except ImportError:  # POSIX systems do not provide msvcrt.
    _msvcrt = None

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template_string,
    request,
    session,
    stream_with_context,
    url_for,
)

SOURCE_DIR = Path(__file__).resolve().parent
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from render_slot_broker import GlobalFifoRenderCoordinator, GlobalRenderSlotPool
from local_reserved_entry import is_local_entry, EntrySessionInterface, LOCAL_URL
from delivery_cleanup_policy import bundle_ready_for_cleanup


RELEASE_ID = (
    "kuaishou-automation-3.2.4-v1.2-multiuser-global3-fifo-cleanup-"
    "stable-fission-download2h-win19041"
)
GLOBAL_CONCURRENT_TASKS = 3
PUBLIC_CONCURRENT_TASKS = 3
LOCAL_CONCURRENT_TASKS = 0
# All three slots are shared by the public FIFO queue.
PER_USER_CONCURRENT_TASKS = 3
MULTIUSER_ROOT = Path(
    os.environ.get("V17_MULTIUSER_ROOT", str(SOURCE_DIR / ".multiuser-dev"))
).expanduser().resolve()
USERS_ROOT = MULTIUSER_ROOT / "users"
_default_multiuser_export_root = (
    Path("D:/快手自动化GPU服务器8020成片")
    if os.name == "nt" and Path("D:/").is_dir()
    else MULTIUSER_ROOT / "成片"
)
MULTIUSER_EXPORT_ROOT = Path(
    os.environ.get(
        "V17_MULTIUSER_EXPORT_ROOT",
        str(_default_multiuser_export_root),
    )
).expanduser()
STATE_DATABASE = MULTIUSER_ROOT / "gateway.sqlite3"
SESSION_SECRET_FILE = MULTIUSER_ROOT / ".gateway_session_secret"
ERP_SESSION_STORE_FILE = MULTIUSER_ROOT / ".erp_sessions.dpapi"
RENDER_SLOT_FILE = MULTIUSER_ROOT / ".render_slots.lock"
GLOBAL_FIFO_DATABASE = MULTIUSER_ROOT / ".render_fifo.sqlite3"
GATEWAY_RENDER_SLOT_POOL = GlobalRenderSlotPool(
    RENDER_SLOT_FILE,
    slots=GLOBAL_CONCURRENT_TASKS,
    allowed_slots=(0, 1, 2),
)
GLOBAL_FIFO_COORDINATOR = GlobalFifoRenderCoordinator(
    GLOBAL_FIFO_DATABASE,
    GATEWAY_RENDER_SLOT_POOL,
)
LOCAL_FIFO_DATABASE = MULTIUSER_ROOT / ".render_fifo.local.sqlite3"
LOCAL_RENDER_SLOT_POOL = GATEWAY_RENDER_SLOT_POOL
LOCAL_FIFO_COORDINATOR = GLOBAL_FIFO_COORDINATOR
_local_owner_user_id = ""


def _is_local_user(user_id: str) -> bool:
    return bool(_local_owner_user_id and user_id == _local_owner_user_id)


def _fifo_for_user(user_id: str):
    return GLOBAL_FIFO_COORDINATOR


def _render_environment(user_id: str) -> dict:
    local = _is_local_user(user_id)
    return {
        "V17_RENDER_SLOT_FILE": str(RENDER_SLOT_FILE),
        "V17_RENDER_SLOT_COUNT": "3",
        "V17_RENDER_ALLOWED_SLOTS": "0,1,2",
        "V17_GLOBAL_FIFO_DATABASE": str(GLOBAL_FIFO_DATABASE),
        "V17_QUEUE_WORKERS": "3",
        "V17_DISPLAY_NAME": "快手自动化 GPU 服务器 · 公共 3 并发",
    }
SERVER_RUNTIME = Path(os.environ.get("V17_SERVER_RUNTIME_PATH", "")).expanduser()
PYTHON_EXE = Path(os.environ.get("V16_PYTHON_EXE", os.sys.executable)).expanduser()
def _strip_retired_external_environment(environment):
    """Drop only this process's obsolete relay configuration, never HKCU."""
    retired_auth = {
        "V17_AUTH_START_URL", "V17_AUTH_EXCHANGE_URL", "V17_AUTH_ISSUER",
        "V17_AUTH_AUDIENCE", "V17_AUTH_SHARED_SECRET_FILE", "V17_STRICT_ERP_RELAY",
    }
    for name in tuple(environment):
        upper = name.upper()
        if upper in retired_auth or upper.startswith(("V16_BILLING_", "V16_ERP_RELAY_", "V16_35_")):
            environment.pop(name, None)


_strip_retired_external_environment(os.environ)
AUTH_MODE = os.environ.get("V17_AUTH_MODE", "local_erp").strip().lower()
if AUTH_MODE not in {"local_erp", "development"}:
    raise RuntimeError("该服务器仅支持本机 ERP 扫码认证，不支持外部认证中转")
# This process never accepts a machine-global ERP credential.  Per-user
# credentials arrive only in a verified login assertion and are held in the
# locked in-memory map below.
os.environ.pop("V16_ERP_BEARER_TOKEN", None)
PUBLIC_BASE_URL = os.environ.get("V17_PUBLIC_BASE_URL", "").strip().rstrip("/")
PUBLIC_HTTPS = os.environ.get("V17_PUBLIC_HTTPS", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
AUTH_BROKER_RUNTIME = Path(
    os.environ.get(
        "V17_AUTH_BROKER_RUNTIME",
        str(SOURCE_DIR.parent / "runtime" / "electron-auth" / "electron.exe"),
    )
).expanduser()
AUTH_BROKER_APP = Path(
    os.environ.get(
        "V17_AUTH_BROKER_APP",
        str(SOURCE_DIR / "electron_auth_broker"),
    )
).expanduser()
AUTH_FLOW_SECONDS = max(60, min(300, int(os.environ.get("V17_AUTH_FLOW_SECONDS", "300"))))
MAX_PENDING_AUTH_FLOWS = max(
    6,
    min(12, int(os.environ.get("V17_MAX_PENDING_AUTH_FLOWS", "6"))),
)
MAX_AUTH_CALLBACK_BYTES = 64 * 1024
MAX_AUTH_BROKER_JSON_BYTES = 64 * 1024
MAX_AUTH_QR_BYTES = 2 * 1024 * 1024
TENANT_PORT_MIN = int(os.environ.get("V17_TENANT_PORT_MIN", "18820"))
TENANT_PORT_MAX = int(os.environ.get("V17_TENANT_PORT_MAX", "18919"))
BACKEND_START_TIMEOUT = float(os.environ.get("V17_TENANT_START_TIMEOUT", "45"))
BACKEND_IDLE_SECONDS = float(os.environ.get("V17_TENANT_IDLE_SECONDS", "1800"))
QUEUE_DATABASE_NAME = ".v17_task_queue.sqlite3"
UNFINISHED_QUEUE_STATUSES = (
    "waiting",
    "running",
    "skip_requested",
    "finalizing",
)
QUEUE_READ_TIMEOUT_SECONDS = 0.25
DAILY_COMPLETED_OUTPUT_CLEANUP_MAINTENANCE_KEY = (
    "daily_completed_output_cleanup_v1"
)
BUNDLE_CACHE_DIRECTORY_NAME = ".v17_download_cache"
# Retry only receipt-confirmed cache deletion after the tenant backend idles.
# Unknown transfers belong to the existing fixed-18:00 owned-bundle collector.
BUNDLE_CACHE_PER_TENANT_SWEEP_LIMIT = 1
BUNDLE_CACHE_GLOBAL_SWEEP_LIMIT = 4
BUNDLE_CACHE_FILE_RE = re.compile(r"bundle-[0-9a-f]{64}\.zip\Z")
DAY_LOGIN_USER_LIMIT = 50
NIGHT_LOGIN_USER_LIMIT = 50
# Physical process safety limit. Login admission has no day/night reduction;
# render work remains independently capped at three host slots and queues beyond it.
MAX_TENANT_BACKENDS = NIGHT_LOGIN_USER_LIMIT
MAX_PROXY_RESPONSE_HEADER_BYTES = 128 * 1024
USER_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
SAFE_IDENTITY_KEYS = (
    "erp_department",
    "erp_user",
    "erp_nickname",
    "erp_phone",
    "erp_user_id",
)
ERP_DIRECT_REQUESTED = (
    AUTH_MODE != "development"
    and os.environ.get("V16_ERP_DIRECT_ENABLED", "0").strip().lower()
    in {"1", "true", "yes", "on"}
)
ERP_TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/\-=]{20,10000}\Z")
_erp_sessions_lock = threading.RLock()
_erp_sessions: dict[str, tuple[str, int]] = {}


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


if os.name == "nt":
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_wchar_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(ctypes.c_wchar_p),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
else:
    _crypt32 = None
    _kernel32 = None


def _data_blob(value: bytes) -> tuple[_DataBlob, object]:
    buffer = ctypes.create_string_buffer(value or b"\0", max(1, len(value)))
    blob = _DataBlob(
        len(value),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)),
    )
    return blob, buffer


def _dpapi_transform(value: bytes, *, protect: bool) -> bytes:
    if _crypt32 is None or _kernel32 is None:
        raise RuntimeError("Windows DPAPI is unavailable")
    source, source_buffer = _data_blob(value)
    output = _DataBlob()
    if protect:
        result = _crypt32.CryptProtectData(
            ctypes.byref(source),
            "Kuaishou GPU 8020 ERP sessions",
            None,
            None,
            None,
            0x01,
            ctypes.byref(output),
        )
    else:
        result = _crypt32.CryptUnprotectData(
            ctypes.byref(source),
            None,
            None,
            None,
            None,
            0x01,
            ctypes.byref(output),
        )
    # Explicit post-call references prevent optimized runtimes from releasing
    # the ctypes buffers while the native DPAPI call still reads their pointers.
    ctypes.addressof(source_buffer)
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        _kernel32.LocalFree(output.pbData)


def _persist_erp_sessions_locked() -> None:
    if not ERP_DIRECT_REQUESTED or os.name != "nt":
        return
    now = int(time.time())
    payload = {
        "version": 1,
        "sessions": {
            user_id: [token, expires_at]
            for user_id, (token, expires_at) in _erp_sessions.items()
            if expires_at > now + 5
        },
    }
    protected = _dpapi_transform(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"),
        protect=True,
    )
    ERP_SESSION_STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = ERP_SESSION_STORE_FILE.with_name(
        f".{ERP_SESSION_STORE_FILE.name}.{uuid.uuid4().hex}.tmp"
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        remaining = memoryview(protected)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("ERP session store write made no progress")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, ERP_SESSION_STORE_FILE)
    finally:
        try:
            if os.path.lexists(temporary):
                temporary.unlink()
        except OSError:
            pass


def _load_persisted_erp_sessions() -> None:
    if not ERP_DIRECT_REQUESTED or os.name != "nt":
        return
    try:
        if ERP_SESSION_STORE_FILE.is_symlink() or not ERP_SESSION_STORE_FILE.is_file():
            return
        raw = ERP_SESSION_STORE_FILE.read_bytes()
        if not raw or len(raw) > 4 * 1024 * 1024:
            return
        payload = json.loads(_dpapi_transform(raw, protect=False).decode("utf-8"))
        values = payload.get("sessions") if isinstance(payload, dict) else None
        if payload.get("version") != 1 or not isinstance(values, dict):
            return
    except (OSError, RuntimeError, UnicodeError, ValueError, json.JSONDecodeError):
        return
    now = int(time.time())
    loaded: dict[str, tuple[str, int]] = {}
    for user_id, material in values.items():
        if not USER_ID_RE.fullmatch(str(user_id or "")):
            continue
        if not isinstance(material, list) or len(material) != 2:
            continue
        token = material[0]
        expires_at = _integer_expiry(material[1])
        if (
            isinstance(token, str)
            and ERP_TOKEN_RE.fullmatch(token)
            and expires_at is not None
            and expires_at > now + 5
        ):
            loaded[user_id] = (token, expires_at)
    with _erp_sessions_lock:
        _erp_sessions.update(loaded)


def _login_user_policy(
    local_time: time.struct_time | None = None,
) -> tuple[int, str]:
    """Return the all-day admission limit.

    ``local_time`` remains accepted for compatibility with diagnostics and
    callers that previously exercised the day/night policy.
    """
    del local_time
    return MAX_TENANT_BACKENDS, "all_day"


def _capacity_error(login_user_limit: int | None = None) -> str:
    if login_user_limit is None:
        login_user_limit, _mode = _login_user_policy()
    return (
        f"当前 {login_user_limit} 个使用名额已满，"
        "请等待其他用户退出登录后再试"
    )


def _capacity_limit_from_error(value: str) -> int | None:
    for login_user_limit in (DAY_LOGIN_USER_LIMIT, NIGHT_LOGIN_USER_LIMIT):
        if value == _capacity_error(login_user_limit):
            return login_user_limit
    return None


# Compatibility for diagnostics/tests that imported the former daytime text.
CAPACITY_ERROR = _capacity_error(DAY_LOGIN_USER_LIMIT)


def _integer_expiry(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value.strip()):
        return int(value.strip())
    return None


def _jwt_expiry(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        return None
    try:
        payload = json.loads(_base64url_decode(parts[1]))
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _integer_expiry(payload.get("exp"))


def _extract_erp_session_from_claims(claims: dict) -> tuple[str, int] | None:
    """Return a verified per-user ERP session only when direct ERP is enabled.

    Kept only as a legacy data parser; remote assertions cannot log in here.
    Token material is deliberately accepted only from the two explicit
    top-level claim names, never from identity data or recursively searched
    fields that might later be persisted.
    """
    if not ERP_DIRECT_REQUESTED or AUTH_MODE == "development":
        return None
    candidates: list[str] = []
    for key in ("erp_bearer_token", "erp_access_token"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            token = value.strip()
            if token[:7].lower() == "bearer ":
                token = token[7:].strip()
            candidates.append(token)
    if not candidates:
        raise ValueError("钉钉登录未返回当前用户的 ERP 授权，请重新扫码")
    if any(not hmac.compare_digest(candidates[0], value) for value in candidates[1:]):
        raise ValueError("钉钉登录返回了冲突的 ERP 授权")
    token = candidates[0]
    if not ERP_TOKEN_RE.fullmatch(token):
        raise ValueError("钉钉登录返回的 ERP 授权格式无效")
    if "erp_token_expires_at" in claims:
        expires_at = _integer_expiry(claims.get("erp_token_expires_at"))
        if expires_at is None:
            raise ValueError("钉钉登录返回的 ERP 授权有效期无效")
    else:
        expires_at = _jwt_expiry(token)
        if expires_at is None:
            raise ValueError("钉钉登录未返回可信的 ERP 授权有效期")
    if expires_at <= int(time.time()) + 5:
        raise ValueError("当前用户的 ERP 授权已过期，请重新扫码")
    return token, expires_at


def _store_erp_session(user_id: str, token: str, expires_at: int) -> None:
    if not USER_ID_RE.fullmatch(user_id) or not ERP_TOKEN_RE.fullmatch(token):
        raise ValueError("ERP 用户授权绑定无效")
    if expires_at <= int(time.time()) + 5:
        raise ValueError("当前用户的 ERP 授权已过期，请重新扫码")
    with _erp_sessions_lock:
        previous = _erp_sessions.get(user_id)
        _erp_sessions[user_id] = (token, expires_at)
        try:
            _persist_erp_sessions_locked()
        except (OSError, RuntimeError) as exc:
            if previous is None:
                _erp_sessions.pop(user_id, None)
            else:
                _erp_sessions[user_id] = previous
            raise RuntimeError("当前用户的 ERP 授权无法安全保存，请重新扫码") from exc


def _erp_session_for_user(user_id: str) -> tuple[str, int] | None:
    if not USER_ID_RE.fullmatch(user_id):
        return None
    now = int(time.time())
    with _erp_sessions_lock:
        material = _erp_sessions.get(user_id)
        if material is None:
            return None
        if material[1] <= now + 5:
            _erp_sessions.pop(user_id, None)
            return None
        return material


def _clear_erp_session(user_id: str) -> None:
    if not USER_ID_RE.fullmatch(user_id):
        return
    with _erp_sessions_lock:
        previous = _erp_sessions.pop(user_id, None)
        try:
            _persist_erp_sessions_locked()
        except (OSError, RuntimeError):
            if previous is not None:
                _erp_sessions[user_id] = previous
            raise


def _erp_token_fingerprint(token: str, expires_at: int) -> str:
    digest = hashlib.sha256()
    digest.update(token.encode("ascii"))
    digest.update(b"\0")
    digest.update(str(expires_at).encode("ascii"))
    return digest.hexdigest()


@dataclass
class PendingAuth:
    flow_id: str
    state: str
    nonce: str
    browser_hash: bytes
    client_hash: bytes
    result_ticket_hash: bytes
    broker_secret: bytes
    control_ticket: str
    process: subprocess.Popen
    port: int
    expires_at: int
    remote_addr: str
    login_hash: bytes = b""
    browser_bind_value: str = ""
    csrf_value: str = ""
    phase: str = "waiting"
    result: dict | None = None
    error: str = ""
    completed_user_id: str = ""
    browser_session_value: str = ""
    browser_session_expires_at: int = 0
    terminal_expires_at: int = 0
    termination_started: bool = False
    broker_lock: threading.Lock = field(default_factory=threading.Lock)
    completion_lock: threading.Lock = field(default_factory=threading.Lock)


_pending_auth_lock = threading.RLock()
_pending_auth: dict[str, PendingAuth] = {}
_pending_auth_starting: dict[str, int] = {}
_pending_auth_login_starts: dict[bytes, threading.Event] = {}
_browser_sessions_lock = threading.RLock()
_browser_sessions: dict[bytes, tuple[str, bytes, int]] = {}
_browser_sessions_closing: set[str] = set()


def _user_is_closing(user_id: str) -> bool:
    with _browser_sessions_lock:
        return user_id in _browser_sessions_closing


def _active_browser_user_ids_locked(now: int | None = None) -> set[str]:
    """Return live browser users while ``_browser_sessions_lock`` is held."""
    if now is None:
        now = int(time.time())
    return {
        user_id
        for user_id, _binding, expires_at in _browser_sessions.values()
        if expires_at > now
    }


def _capability() -> str:
    return secrets.token_urlsafe(48)


def _capability_ok(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{43,128}", value))


def _secret_hash(value: str) -> bytes:
    return hashlib.sha256(value.encode("ascii")).digest()


def _browser_binding_hash() -> bytes:
    remote_addr = str(request.remote_addr or "")[:80]
    user_agent = str(request.headers.get("User-Agent") or "")[:512]
    entry = "local" if is_local_entry() else "public"
    return hashlib.sha256(f"{remote_addr}\0{user_agent}\0{entry}".encode("utf-8")).digest()


# Sliding browser inactivity window. Native ERP authorization is still checked
# separately and explicit logout still revokes the server-side capability.
BROWSER_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
BROWSER_SESSION_RENEW_INTERVAL_SECONDS = 5 * 60


def _establish_browser_session(
    user_id: str,
    *,
    session_capability: str | None = None,
    expires_at: int | None = None,
) -> None:
    if not USER_ID_RE.fullmatch(user_id):
        raise ValueError("登录用户编号无效")
    if session_capability is None:
        session_capability = _capability()
    if not _capability_ok(session_capability):
        raise ValueError("浏览器登录凭据无效")
    if expires_at is None:
        expires_at = int(time.time()) + BROWSER_SESSION_TTL_SECONDS
    if not isinstance(expires_at, int) or expires_at <= int(time.time()) + 5:
        raise ValueError("浏览器登录有效期无效")
    # Any operation needing both locks must take the backend-manager lock first.
    # This keeps admission atomic with idle/stop transitions and avoids the
    # browser-lock -> manager-lock inversion that would deadlock logout.
    manager = globals().get("backend_manager")
    manager_lock = getattr(manager, "_lock", None)

    def register_locked(reserved_users: set[str]) -> None:
        with _browser_sessions_lock:
            manager_closing = manager is not None and user_id in getattr(
                manager, "_closing_backends", {}
            )
            if user_id in _browser_sessions_closing or manager_closing:
                raise RuntimeError("上一次登录正在安全退出，请稍后重新扫码")
            now = int(time.time())
            occupied_users = (
                _active_browser_user_ids_locked(now)
                | _browser_sessions_closing
                | reserved_users
            )
            login_user_limit, _login_limit_mode = _login_user_policy()
            public_occupied = {uid for uid in occupied_users if not _is_local_user(uid)}
            if (not _is_local_user(user_id) and user_id not in occupied_users
                    and len(public_occupied) >= login_user_limit):
                raise RuntimeError(_capacity_error(login_user_limit))
            key = _secret_hash(session_capability)
            existing = _browser_sessions.get(key)
            client_hash = _browser_binding_hash()
            if existing is not None and (
                not hmac.compare_digest(existing[0], user_id)
                or not hmac.compare_digest(existing[1], client_hash)
            ):
                raise RuntimeError("浏览器登录凭据发生冲突")
            _browser_sessions[key] = (
                user_id,
                client_hash,
                expires_at,
            )

    if manager_lock is None:
        register_locked(set())
    else:
        with manager_lock:
            reserved_users = manager._reserved_user_ids_locked()
            register_locked(reserved_users)
    session.clear()
    session["user_id"] = user_id
    session["browser_session"] = session_capability
    session.permanent = True


def _revoke_browser_session(*, require_binding: bool = True) -> bool:
    capability = str(session.get("browser_session") or "")
    user_id = str(session.get("user_id") or "")
    if not _capability_ok(capability) or not USER_ID_RE.fullmatch(user_id):
        return False
    with _browser_sessions_lock:
        key = _secret_hash(capability)
        record = _browser_sessions.get(key)
        if record is None:
            return False
        if not hmac.compare_digest(record[0], user_id):
            return False
        if require_binding and not hmac.compare_digest(
            record[1], _browser_binding_hash()
        ):
            return False
        _browser_sessions.pop(key, None)
        return True


def _auth_broker_assets_ready() -> bool:
    required = (
        AUTH_BROKER_RUNTIME,
        AUTH_BROKER_APP / "package.json",
        AUTH_BROKER_APP / "main.js",
        AUTH_BROKER_APP / "broker_core.js",
    )
    return all(path.is_file() and not path.is_symlink() for path in required)


def _close_broker_stdout(process: subprocess.Popen) -> None:
    stdout = getattr(process, "stdout", None)
    if stdout is not None and not getattr(stdout, "closed", True):
        try:
            stdout.close()
        except OSError:
            pass


def _terminate_broker(flow: PendingAuth) -> None:
    process = flow.process
    if process.poll() is not None:
        _close_broker_stdout(process)
        return
    try:
        _broker_request(flow, "POST", "/v1/auth/cancel", response_limit=4096)
    except RuntimeError:
        pass
    try:
        process.wait(timeout=2)
        _close_broker_stdout(process)
        return
    except subprocess.TimeoutExpired:
        pass
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    _close_broker_stdout(process)


def _schedule_terminate_broker(flow: PendingAuth) -> None:
    with _pending_auth_lock:
        if flow.termination_started:
            return
        flow.termination_started = True
    threading.Thread(target=_terminate_broker, args=(flow,), daemon=True).start()


def _cleanup_auth_flows() -> None:
    now = int(time.time())
    expired: list[PendingAuth] = []
    with _pending_auth_lock:
        for flow_id, flow in list(_pending_auth.items()):
            terminal_done = (
                flow.phase == "authenticated"
                and flow.terminal_expires_at > 0
                and flow.terminal_expires_at < now
            )
            ordinary_expired = flow.phase != "authenticated" and flow.expires_at < now
            if terminal_done or ordinary_expired or (
                flow.process.poll() is not None and flow.phase == "waiting"
            ):
                _pending_auth.pop(flow_id, None)
                flow.result = None
                flow.login_hash = b""
                flow.browser_bind_value = ""
                flow.csrf_value = ""
                flow.browser_session_value = ""
                expired.append(flow)
    for flow in expired:
        try:
            _terminate_broker(flow)
        except Exception:
            continue


def _stop_all_auth_flows() -> None:
    with _pending_auth_lock:
        flows = list(_pending_auth.values())
        _pending_auth.clear()
        starting_events = list(_pending_auth_login_starts.values())
        _pending_auth_login_starts.clear()
    for event in starting_events:
        event.set()
    for flow in flows:
        try:
            _terminate_broker(flow)
        except Exception:
            continue


def _read_ready_line(process: subprocess.Popen, timeout: float = 15.0) -> dict:
    if process.stdout is None:
        raise RuntimeError("本机扫码组件没有启动输出")
    replies: queue.Queue[bytes] = queue.Queue(maxsize=1)

    def read_line() -> None:
        try:
            total = 0
            while True:
                raw = process.stdout.readline()
                total += len(raw)
                if total > 16 * 1024:
                    replies.put(b"", block=False)
                    return
                if not raw or raw.strip():
                    replies.put(raw, block=False)
                    return
        except (OSError, queue.Full):
            pass

    threading.Thread(target=read_line, daemon=True).start()
    try:
        raw = replies.get(timeout=timeout)
    except queue.Empty as exc:
        raise RuntimeError("本机扫码组件启动超时") from exc
    if len(raw) > 16 * 1024:
        raise RuntimeError("本机扫码组件启动响应异常")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("本机扫码组件启动响应无效") from exc
    if not isinstance(payload, dict) or payload.get("event") != "ready":
        raise RuntimeError("本机扫码组件未就绪")
    return payload


def _spawn_auth_flow(
    flow_id: str,
    state: str,
    nonce: str,
    browser_hash: bytes,
    client_hash: bytes,
    remote_addr: str,
    login_hash: bytes = b"",
    browser_bind_value: str = "",
    csrf_value: str = "",
) -> PendingAuth:
    if not _auth_broker_assets_ready():
        raise RuntimeError("本机钉钉扫码组件不完整，请重新校验安装包")
    result_ticket = _capability()
    control_ticket = _capability()
    broker_secret_text = _capability()
    now = int(time.time())
    # The QR helper needs only ordinary Windows runtime paths; never inherit
    # the full environment or any per-user ERP credential into Electron.
    safe_environment_names = {
        "appdata",
        "comspec",
        "homedrive",
        "homepath",
        "localappdata",
        "number_of_processors",
        "path",
        "pathext",
        "processor_architecture",
        "programdata",
        "systemdrive",
        "systemroot",
        "temp",
        "tmp",
        "userprofile",
        "windir",
    }
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.casefold() in safe_environment_names
    }
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(
            [str(AUTH_BROKER_RUNTIME), str(AUTH_BROKER_APP)],
            cwd=str(AUTH_BROKER_APP),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise RuntimeError("无法启动本机钉钉扫码组件") from exc
    try:
        ready = _read_ready_line(process)
        _close_broker_stdout(process)
        try:
            port = int(ready.get("listen_port") or 0)
            launch_expires_at = int(ready.get("launch_expires_at") or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("本机扫码组件监听地址异常") from exc
        launch_ticket = str(ready.get("launch_ticket") or "")
        if (
            ready.get("version") != 1
            or ready.get("listen_host") != "127.0.0.1"
            or not 1024 <= port <= 65535
            or not _capability_ok(launch_ticket)
            or launch_expires_at < int(time.time()) + 2
            or launch_expires_at > int(time.time()) + 60
            or process.poll() is not None
        ):
            raise RuntimeError("本机扫码组件监听地址异常")
        flow = PendingAuth(
            flow_id=flow_id,
            state=state,
            nonce=nonce,
            browser_hash=browser_hash,
            client_hash=client_hash,
            result_ticket_hash=_secret_hash(result_ticket),
            broker_secret=broker_secret_text.encode("ascii"),
            control_ticket=control_ticket,
            process=process,
            port=port,
            expires_at=now + AUTH_FLOW_SECONDS,
            remote_addr=remote_addr,
            login_hash=login_hash,
            browser_bind_value=browser_bind_value,
            csrf_value=csrf_value,
        )
        with _pending_auth_lock:
            _pending_auth[flow_id] = flow
        callback_port = int(os.environ.get("PORT", "8020"))
        body = {
            "version": 1,
            "flow_id": flow_id,
            "state": state,
            "nonce": nonce,
            "result_url": (
                f"http://127.0.0.1:{callback_port}/internal/auth-result"
            ),
            "result_ticket": result_ticket,
            "control_ticket": control_ticket,
            "broker_secret": broker_secret_text,
            "expires_at": flow.expires_at,
        }
        status, _headers, _payload = _broker_request(
            flow,
            "POST",
            "/v1/auth/start",
            json_body=body,
            bearer=launch_ticket,
            rotate_ticket=False,
            response_limit=MAX_AUTH_BROKER_JSON_BYTES,
        )
        if status != 202:
            raise RuntimeError("本机扫码组件拒绝了登录任务")
        return flow
    except (OSError, RuntimeError, ValueError):
        with _pending_auth_lock:
            _pending_auth.pop(flow_id, None)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        _close_broker_stdout(process)
        raise


def _broker_request(
    flow: PendingAuth,
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    bearer: str | None = None,
    rotate_ticket: bool = True,
    response_limit: int,
) -> tuple[int, dict[str, str], bytes]:
    body = (
        json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        if json_body is not None
        else None
    )
    with flow.broker_lock:
        ticket = bearer if bearer is not None else flow.control_ticket
        if not _capability_ok(ticket):
            raise RuntimeError("本机扫码控制凭据已失效")
        headers = {
            "Authorization": f"Bearer {ticket}",
            "Accept": "application/json, image/png",
            "Connection": "close",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection("127.0.0.1", flow.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(response_limit + 1)
            response_headers = {name.lower(): value for name, value in response.getheaders()}
            status = int(response.status)
        except (OSError, http.client.HTTPException) as exc:
            raise RuntimeError("本机扫码组件暂时不可用") from exc
        finally:
            connection.close()
        if len(payload) > response_limit:
            raise RuntimeError("本机扫码组件响应过大")
        if rotate_ticket:
            next_ticket = response_headers.get("x-next-control-ticket", "")
            if not _capability_ok(next_ticket):
                raise RuntimeError("本机扫码控制凭据轮换失败")
            flow.control_ticket = next_ticket
        return status, response_headers, payload


def _plain_directory(path: Path) -> Path:
    candidate = Path(os.path.abspath(str(path)))
    if not candidate.is_absolute() or not candidate.anchor:
        raise RuntimeError(f"不安全的多用户目录：{path}")
    current = Path(candidate.anchor)
    anchor_attributes = int(
        getattr(os.lstat(current), "st_file_attributes", 0)
    )
    if (
        current.is_symlink()
        or anchor_attributes & 0x400
        or not current.is_dir()
    ):
        raise RuntimeError(f"不安全的多用户目录：{current}")
    for part in candidate.parts[1:]:
        current = current / part
        if os.path.lexists(current):
            attributes = int(
                getattr(os.lstat(current), "st_file_attributes", 0)
            )
            if current.is_symlink() or attributes & 0x400 or not current.is_dir():
                raise RuntimeError(f"不安全的多用户目录：{current}")
            continue
        current.mkdir()
        attributes = int(getattr(os.lstat(current), "st_file_attributes", 0))
        if current.is_symlink() or attributes & 0x400 or not current.is_dir():
            raise RuntimeError(f"不安全的多用户目录：{current}")
    return candidate.resolve(strict=True)


def _load_or_create_secret(path: Path, size: int = 48) -> bytes:
    _plain_directory(path.parent)
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("网关密钥文件不安全")
        value = path.read_bytes()
        if len(value) < 32:
            raise RuntimeError("网关密钥文件长度不足")
        return value
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    value = secrets.token_bytes(size)
    try:
        os.write(descriptor, value)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return value


def _state_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(STATE_DATABASE, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def _tenant_unfinished_state(user_id: str) -> bool | None:
    """Return True/False for unfinished work, or None when it is uncertain.

    Shutdown decisions treat ``None`` as unfinished.  A transient SQLite lock,
    damaged database, or unsafe tenant path must never cause a live worker to
    be killed while its durable queue may still contain work.
    """
    if not USER_ID_RE.fullmatch(str(user_id or "")):
        return None
    try:
        users_root = USERS_ROOT.resolve()
        data_dir = users_root / user_id
        if data_dir.is_symlink():
            return None
        if not data_dir.exists():
            return False
        if not data_dir.is_dir() or data_dir.resolve() != data_dir:
            return None
        database = data_dir / QUEUE_DATABASE_NAME
        if database.is_symlink():
            return None
        if not database.exists():
            return False
        if not database.is_file() or database.resolve().parent != data_dir:
            return None
        connection = sqlite3.connect(
            database.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=QUEUE_READ_TIMEOUT_SECONDS,
        )
        try:
            connection.execute(
                f"PRAGMA busy_timeout={int(QUEUE_READ_TIMEOUT_SECONDS * 1000)}"
            )
            connection.execute("PRAGMA query_only=ON")
            placeholders = ",".join("?" for _ in UNFINISHED_QUEUE_STATUSES)
            row = connection.execute(
                f"SELECT 1 FROM task_queue WHERE status IN ({placeholders}) LIMIT 1",
                UNFINISHED_QUEUE_STATUSES,
            ).fetchone()
            return row is not None
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError):
        return None


def _tenant_download_retention_worker_state(user_id: str) -> bool | None:
    """Return whether immediate retention or fixed local-18:00 cleanup is due.

    Only an exact ``succeeded`` unexpired delivery creates daily cleanup work.
    Before its durable deadline the backend may sleep; at/after the deadline
    the reviver starts it even without a browser or ERP token.  A missing state
    row requests one bootstrap start so the tenant process can persist the
    release's fixed first deadline.  ``None`` is uncertainty and fails closed
    for lifecycle decisions that could terminate an already-running backend.
    """
    if not USER_ID_RE.fullmatch(str(user_id or "")):
        return None
    try:
        users_root = USERS_ROOT.resolve()
        data_dir = users_root / user_id
        if data_dir.is_symlink():
            return None
        if not data_dir.exists():
            return False
        if not data_dir.is_dir() or data_dir.resolve() != data_dir:
            return None
        database = data_dir / QUEUE_DATABASE_NAME
        if database.is_symlink():
            return None
        if not database.exists():
            return False
        if not database.is_file() or database.resolve().parent != data_dir:
            return None
        connection = sqlite3.connect(
            database.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=QUEUE_READ_TIMEOUT_SECONDS,
        )
        try:
            connection.execute(
                f"PRAGMA busy_timeout={int(QUEUE_READ_TIMEOUT_SECONDS * 1000)}"
            )
            connection.execute("PRAGMA query_only=ON")
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(task_queue)")
            }
            if {"downloaded_at", "delivery_expired_at"}.issubset(columns):
                receipt_due = connection.execute(
                    "SELECT 1 FROM task_queue "
                    "WHERE downloaded_at IS NOT NULL "
                    "AND TRIM(downloaded_at)<>'' "
                    "AND delivery_expired_at IS NULL LIMIT 1"
                ).fetchone()
                if receipt_due is not None:
                    # Preserve the ordinary exact-EOF immediate collector.
                    # The fixed daily sweep is an additional fallback only.
                    return True
            if not {"status", "delivery_expired_at", "result_json"}.issubset(
                columns
            ):
                return False
            candidate = connection.execute(
                "SELECT 1 FROM task_queue "
                "WHERE status='succeeded' "
                "AND delivery_expired_at IS NULL "
                "AND result_json LIKE '%\"kind\"%' LIMIT 1"
            ).fetchone()
            if candidate is None:
                return False
            maintenance_table = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='queue_maintenance_state' LIMIT 1"
            ).fetchone()
            if maintenance_table is None:
                return True
            row = connection.execute(
                "SELECT next_due_at FROM queue_maintenance_state "
                "WHERE maintenance_key=?",
                (DAILY_COMPLETED_OUTPUT_CLEANUP_MAINTENANCE_KEY,),
            ).fetchone()
            if row is None:
                return True
            next_due_at = str(row[0] or "")
            if not re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2} "
                r"[0-9]{2}:[0-9]{2}:[0-9]{2}",
                next_due_at,
            ):
                return None
            try:
                due_epoch = time.mktime(
                    time.strptime(next_due_at, "%Y-%m-%d %H:%M:%S")
                )
            except (OSError, OverflowError, ValueError):
                return None
            return time.time() >= float(due_epoch)
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError):
        return None


def _user_has_unfinished_tasks(user_id: str) -> bool:
    """Fail closed for every lifecycle decision that could stop a tenant."""
    return _tenant_unfinished_state(user_id) is not False


def _user_requires_live_backend(user_id: str) -> bool:
    """Keep queue work and in-process output-retention duties alive."""
    return (
        _tenant_unfinished_state(user_id) is not False
        or _tenant_download_retention_worker_state(user_id) is not False
    )


def _initialize_state() -> None:
    global _local_owner_user_id
    _plain_directory(MULTIUSER_ROOT)
    _plain_directory(USERS_ROOT)
    _plain_directory(MULTIUSER_EXPORT_ROOT)
    with _state_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_users (
                user_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                external_subject TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                identity_json TEXT NOT NULL DEFAULT '{}',
                disabled INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                last_login_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS app_users_tenant
                ON app_users(tenant_id, user_id);
            CREATE TABLE IF NOT EXISTS assertion_replays (
                issuer TEXT NOT NULL,
                jti TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                PRIMARY KEY (issuer, jti)
            );
            CREATE TABLE IF NOT EXISTS local_entry_owner (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                external_subject TEXT NOT NULL UNIQUE,
                user_id TEXT NOT NULL UNIQUE
            );
            """
        )
        row = connection.execute("SELECT user_id FROM local_entry_owner WHERE singleton=1").fetchone()
        _local_owner_user_id = str(row["user_id"]) if row else ""


def _base64url_decode(value: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value or ""):
        raise ValueError("身份断言编码无效")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _load_auth_secret() -> bytes:
    raise RuntimeError("外部认证中转已永久停用，请使用本机 ERP 扫码登录")


def _verify_compact_assertion(compact: str, expected_state: str, expected_nonce: str) -> dict:
    raise RuntimeError("外部认证中转已永久停用，请使用本机 ERP 扫码登录")


def _exchange_authorization_code(code: str, state: str, nonce: str) -> str:
    raise RuntimeError("外部认证中转已永久停用，请使用本机 ERP 扫码登录")


def _identity_from_claims(claims: dict) -> dict:
    identity = {}
    source = claims.get("identity") if isinstance(claims.get("identity"), dict) else claims
    for key in SAFE_IDENTITY_KEYS:
        value = source.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            identity[key] = str(value)[:256]
    identity.setdefault("erp_user_id", str(claims.get("sub") or "")[:256])
    return identity


def _upsert_user(claims: dict) -> dict:
    global _local_owner_user_id
    corp_id = str(claims["corp_id"]).strip()
    subject = str(claims["sub"]).strip()
    # The verifier rejects ':' in both components, so this legacy encoding is
    # unambiguous while preserving existing user IDs, disabled-account state,
    # and private data directories across the multi-user upgrade.
    external_subject = f"{corp_id}:{subject}"
    original_subject = external_subject
    local_entry = is_local_entry()
    if local_entry:
        external_subject = "local-entry:" + external_subject
    user_id = uuid.uuid5(uuid.NAMESPACE_URL, f"ding:{external_subject}").hex
    tenant_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"ding-corp:{corp_id}").hex
    display_name = str(
        claims.get("display_name")
        or claims.get("name")
        or _identity_from_claims(claims).get("erp_nickname")
        or "已登录用户"
    ).strip()[:128]
    identity = _identity_from_claims(claims)
    now = int(time.time())
    with _state_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if local_entry:
            disabled = connection.execute(
                "SELECT disabled FROM app_users WHERE external_subject=?", (original_subject,)
            ).fetchone()
            if disabled and int(disabled["disabled"]):
                raise ValueError("此账号已被停用")
            owner = connection.execute(
                "SELECT external_subject FROM local_entry_owner WHERE singleton=1"
            ).fetchone()
            if owner and owner["external_subject"] != original_subject:
                raise ValueError("本机专用入口已绑定其他钉钉账号，请使用首次绑定的账号扫码")
            connection.execute(
                "INSERT OR IGNORE INTO local_entry_owner(singleton,external_subject,user_id) VALUES(1,?,?)",
                (original_subject, user_id),
            )
        existing = connection.execute(
            "SELECT user_id, disabled FROM app_users WHERE external_subject=?",
            (external_subject,),
        ).fetchone()
        if existing and int(existing["disabled"]):
            connection.execute("ROLLBACK")
            raise ValueError("此账号已被停用")
        if existing and existing["user_id"] != user_id:
            connection.execute("ROLLBACK")
            raise ValueError("账号映射发生冲突")
        connection.execute(
            """
            INSERT INTO app_users(
                user_id,tenant_id,external_subject,display_name,identity_json,
                disabled,created_at,last_login_at
            ) VALUES(?,?,?,?,?,0,?,?)
            ON CONFLICT(external_subject) DO UPDATE SET
                display_name=excluded.display_name,
                identity_json=excluded.identity_json,
                last_login_at=excluded.last_login_at
            """,
            (
                user_id,
                tenant_id,
                external_subject,
                display_name,
                json.dumps(identity, ensure_ascii=False, separators=(",", ":")),
                now,
                now,
            ),
        )
        connection.execute("COMMIT")
    if local_entry:
        _local_owner_user_id = user_id
    return {
        "user_id": user_id,
        "tenant_id": tenant_id,
        "display_name": display_name,
        "identity": identity,
    }


def _user_record_by_id(
    user_id: str,
    *,
    require_erp_session: bool = True,
) -> dict | None:
    """Load one enabled tenant and optionally require its live ERP session.

    Browser requests and runnable queue work keep the strict default.  The
    background output-retention worker may load the durable tenant identity
    without ERP authority because it never submits an ERP request.
    """
    if not USER_ID_RE.fullmatch(str(user_id or "")):
        return None
    with _state_connection() as connection:
        row = connection.execute(
            "SELECT user_id,tenant_id,display_name,identity_json,disabled FROM app_users WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if row is None or int(row["disabled"]):
        return None
    try:
        identity = json.loads(row["identity_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        identity = {}
    user = {
        "user_id": row["user_id"],
        "tenant_id": row["tenant_id"],
        "display_name": row["display_name"],
        "identity": identity if isinstance(identity, dict) else {},
    }
    if ERP_DIRECT_REQUESTED and require_erp_session:
        erp_session = _erp_session_for_user(user_id)
        if erp_session is None:
            return None
        user["_erp_bearer_token"] = erp_session[0]
        user["_erp_token_expires_at"] = erp_session[1]
    return user


def _current_user() -> dict | None:
    user_id = str(session.get("user_id") or "")
    browser_capability = str(session.get("browser_session") or "")
    if not USER_ID_RE.fullmatch(user_id) or not _capability_ok(browser_capability):
        return None
    if _is_local_user(user_id) != is_local_entry():
        return None
    session_hash = _secret_hash(browser_capability)
    now = int(time.time())
    with _browser_sessions_lock:
        record = _browser_sessions.get(session_hash)
        if record is None:
            return None
        bound_user_id, bound_client, expires_at = record
        if expires_at <= now:
            return None
        if (
            not hmac.compare_digest(bound_user_id, user_id)
            or not hmac.compare_digest(bound_client, _browser_binding_hash())
        ):
            return None
    user = _user_record_by_id(user_id)
    if user is None:
        return None
    # Renew only an existing, unexpired, correctly bound capability. Recheck
    # after the user lookup so a concurrent logout can never resurrect it.
    with _browser_sessions_lock:
        current = _browser_sessions.get(session_hash)
        if current != record or user_id in _browser_sessions_closing:
            return None
        if expires_at - now <= BROWSER_SESSION_TTL_SECONDS - BROWSER_SESSION_RENEW_INTERVAL_SECONDS:
            _browser_sessions[session_hash] = (bound_user_id, bound_client, now + BROWSER_SESSION_TTL_SECONDS)
            session.permanent = True
            session.modified = True
    return user


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _initialize_tenant_data(user: dict) -> tuple[Path, str]:
    user_id = user["user_id"]
    if not USER_ID_RE.fullmatch(user_id):
        raise RuntimeError("租户用户编号无效")
    data_dir = (USERS_ROOT / user_id).resolve()
    if data_dir.parent != USERS_ROOT.resolve():
        raise RuntimeError("租户目录越界")
    _plain_directory(data_dir)
    for name in ("核心素材", "打乱素材池", "贴纸素材池"):
        _plain_directory(data_dir / name)
    instance_id = f"kuaishou-tenant-{user_id}"
    marker = data_dir / ".v17_instance_id"
    if marker.exists():
        if marker.is_symlink() or marker.read_text(encoding="utf-8").strip() != instance_id:
            raise RuntimeError("租户实例标记不匹配")
    else:
        marker.write_text(instance_id + "\n", encoding="utf-8")
    order_file = data_dir / ".v16_upload_order.json"
    if not order_file.exists():
        _atomic_json(order_file, {})
    legacy_token_file = data_dir / ".v16_erp_bearer_token"
    if legacy_token_file.exists() or legacy_token_file.is_symlink():
        if legacy_token_file.is_dir() and not legacy_token_file.is_symlink():
            raise RuntimeError("租户 ERP 旧授权路径不是普通文件")
        legacy_token_file.unlink()
    _atomic_json(data_dir / ".v16_erp_identity.json", dict(user.get("identity") or {}))
    return data_dir, instance_id


@dataclass
class TenantBackend:
    user_id: str
    port: int
    gateway_token: str
    control_token: str
    cookie_name: str
    erp_token_fingerprint: str
    process: subprocess.Popen
    last_used: float
    log_handle: object
    inflight_requests: int = 0


@dataclass
class _BackendLane:
    """Serialize one tenant without blocking unrelated tenant backends."""

    lock: threading.Lock = field(default_factory=threading.Lock)


class BackendManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._backends: dict[str, TenantBackend] = {}
        # A backend remains a capacity reservation until its process has
        # actually exited. Moving it here before termination prevents a
        # concurrent login from briefly launching a sixth tenant process.
        self._closing_backends: dict[str, TenantBackend] = {}
        self._starting_backends: set[str] = set()
        self._lanes: dict[str, _BackendLane] = {}
        self._stopping = False

    @staticmethod
    def _backend_exited(backend: TenantBackend) -> bool:
        process = getattr(backend, "process", None)
        return process is not None and process.poll() is not None

    @staticmethod
    def _close_backend_log(backend: TenantBackend) -> None:
        log_handle = getattr(backend, "log_handle", None)
        if log_handle is None:
            return
        try:
            log_handle.close()
        except Exception:
            pass

    def _prune_dead_locked(self) -> None:
        """Release already-dead processes while ``self._lock`` is held."""
        dead: list[TenantBackend] = []
        for collection in (self._backends, self._closing_backends):
            for user_id, backend in list(collection.items()):
                if self._backend_exited(backend):
                    if collection.get(user_id) is backend:
                        collection.pop(user_id, None)
                        dead.append(backend)
        for backend in dead:
            try:
                _fifo_for_user(backend.user_id).mark_offline(
                    backend.user_id,
                    backend_instance_id=backend.gateway_token,
                )
            except (OSError, sqlite3.Error, RuntimeError, TypeError, ValueError):
                # A dead process cannot claim work.  The next successful
                # reviver pass refreshes/fences this readiness generation.
                pass
            self._close_backend_log(backend)

    def _reserved_user_ids_locked(self) -> set[str]:
        """Return capacity owners while ``self._lock`` is held."""
        self._prune_dead_locked()
        return (
            set(self._backends)
            | set(self._closing_backends)
            | set(self._starting_backends)
        )

    def _lane_for(self, user_id: str) -> _BackendLane:
        with self._lock:
            return self._lanes.setdefault(user_id, _BackendLane())

    def has_backend_reservation(self, user_id: str) -> bool:
        if not USER_ID_RE.fullmatch(str(user_id or "")):
            return False
        with self._lock:
            return user_id in self._reserved_user_ids_locked()

    def begin_offline_maintenance(self, user_id: str) -> _BackendLane | None:
        """Reserve one inactive tenant lane without starting its backend."""
        if not USER_ID_RE.fullmatch(str(user_id or "")):
            return None
        lane = self._lane_for(user_id)
        if not lane.lock.acquire(blocking=False):
            return None
        try:
            with self._lock:
                if (
                    self._stopping
                    or user_id in self._reserved_user_ids_locked()
                    or _user_is_closing(user_id)
                ):
                    lane.lock.release()
                    return None
            # Every backend start/stop for this user first takes this same lane;
            # holding it closes the offline-cleanup versus login/start race.
            return lane
        except BaseException:
            lane.lock.release()
            raise

    @staticmethod
    def finish_offline_maintenance(lane: _BackendLane | None) -> None:
        if lane is not None:
            lane.lock.release()

    def begin_proxy_request(self, backend: TenantBackend) -> None:
        with self._lock:
            if self._backends.get(backend.user_id) is not backend:
                raise RuntimeError("当前个人空间连接状态已更新")
            backend.inflight_requests += 1
            backend.last_used = time.monotonic()

    def finish_proxy_request(self, backend: TenantBackend) -> None:
        with self._lock:
            backend.inflight_requests = max(0, backend.inflight_requests - 1)
            backend.last_used = time.monotonic()

    def _finish_closing(self, user_id: str, backend: TenantBackend) -> None:
        with self._lock:
            if (
                self._closing_backends.get(user_id) is backend
                and self._backend_exited(backend)
            ):
                self._closing_backends.pop(user_id, None)

    @staticmethod
    def _reserve_port() -> tuple[socket.socket, int]:
        for port in range(TENANT_PORT_MIN, TENANT_PORT_MAX + 1):
            guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                guard.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                guard.bind(("127.0.0.1", port))
                return guard, port
            except OSError:
                guard.close()
        raise RuntimeError("没有可用的租户后端端口")

    @staticmethod
    def _backend_headers(backend: TenantBackend) -> dict[str, str]:
        return {
            "Host": f"127.0.0.1:{backend.port}",
            "X-Kuaishou-Gateway-Token": backend.gateway_token,
            "Connection": "close",
        }

    def _healthy(self, backend: TenantBackend) -> bool:
        if backend.process.poll() is not None:
            return False
        connection = http.client.HTTPConnection("127.0.0.1", backend.port, timeout=0.7)
        try:
            connection.request("GET", "/healthz", headers=self._backend_headers(backend))
            response = connection.getresponse()
            body = response.read(64 * 1024)
            if response.status != 200:
                return False
            payload = json.loads(body)
            expected_instance_id = f"kuaishou-tenant-{backend.user_id}"
            return bool(
                isinstance(payload, dict)
                and payload.get("ok")
                and payload.get("tenant_backend")
                and hmac.compare_digest(
                    str(payload.get("release_id") or ""), RELEASE_ID
                )
                and hmac.compare_digest(
                    str(payload.get("instance_id") or ""), expected_instance_id
                )
            )
        except (OSError, ValueError, http.client.HTTPException):
            return False
        finally:
            connection.close()

    @staticmethod
    def _erp_session_material(user: dict) -> tuple[str, int, str] | None:
        if not ERP_DIRECT_REQUESTED:
            return None
        allow_deferred = bool(user.get("_allow_deferred_erp"))
        user_id = str(user.get("user_id") or "")
        token = user.get("_erp_bearer_token")
        expires_at = _integer_expiry(user.get("_erp_token_expires_at"))
        if (
            not USER_ID_RE.fullmatch(user_id)
            or not isinstance(token, str)
            or not ERP_TOKEN_RE.fullmatch(token)
            or expires_at is None
            or expires_at <= int(time.time()) + 5
        ):
            # A durable queue worker may keep rendering for an already
            # authenticated owner after the short-lived ERP token expires.
            # Direct ERP delivery is then written to that tenant's persistent
            # outbox and retried only after the same owner scans again.
            if allow_deferred and USER_ID_RE.fullmatch(user_id):
                return None
            raise RuntimeError("当前用户的 ERP 授权缺失或已过期，请重新扫码")
        current = _erp_session_for_user(user_id)
        if (
            current is None
            or current[1] != expires_at
            or not hmac.compare_digest(current[0], token)
        ):
            raise RuntimeError("当前用户的 ERP 授权已更新，请重新扫码")
        return token, expires_at, _erp_token_fingerprint(token, expires_at)

    def _install_erp_session(self, backend: TenantBackend, user: dict) -> int:
        material = self._erp_session_material(user)
        if material is None:
            return 0
        token, expires_at, fingerprint = material
        if hmac.compare_digest(backend.erp_token_fingerprint, fingerprint):
            return expires_at
        connection = http.client.HTTPConnection(
            "127.0.0.1", backend.port, timeout=3.0
        )
        headers = self._backend_headers(backend)
        headers.update(
            {
                "X-Kuaishou-Erp-Bearer": token,
                "X-Kuaishou-Erp-Expires-At": str(expires_at),
                "X-Kuaishou-Erp-User-Id": backend.user_id,
            }
        )
        try:
            connection.request(
                "POST", "/internal/erp-session", body=b"", headers=headers
            )
            response = connection.getresponse()
            raw = response.read(64 * 1024 + 1)
            if response.status != 200 or len(raw) > 64 * 1024:
                raise RuntimeError("个人制作空间拒绝接收当前 ERP 授权")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict) or not payload.get("ok") or not payload.get(
                "token_present"
            ):
                raise RuntimeError("个人制作空间未确认当前 ERP 授权")
        except (OSError, UnicodeError, ValueError, http.client.HTTPException) as exc:
            raise RuntimeError("当前用户的 ERP 授权无法安全注入，请重新扫码") from exc
        finally:
            connection.close()
        backend.erp_token_fingerprint = fingerprint
        return expires_at

    def _launch(self, user: dict) -> TenantBackend:
        if not SERVER_RUNTIME.is_file() or not PYTHON_EXE.is_file():
            raise RuntimeError("多用户运行时路径未配置完整")
        maintenance_only = bool(user.get("_retention_only"))
        data_dir, instance_id = _initialize_tenant_data(user)
        export_base = _plain_directory(MULTIUSER_EXPORT_ROOT)
        export_dir = Path(
            os.path.abspath(str(export_base / user["user_id"]))
        )
        if export_dir.parent != export_base:
            raise RuntimeError("租户成片目录越界")
        _plain_directory(export_dir)
        guard, port = self._reserve_port()
        gateway_token = secrets.token_hex(32)
        control_token = secrets.token_hex(32)
        cookie_name = f"kuaishou_tenant_{user['user_id'][:12]}"
        environment = os.environ.copy()
        _strip_retired_external_environment(environment)
        environment.update(
            {
                "V17_TENANT_BACKEND": "1",
                "V17_TENANT_GATEWAY_TOKEN": gateway_token,
                "V17_TENANT_USER_ID": user["user_id"],
                "V17_TENANT_ID": user["tenant_id"],
                "V17_INSTANCE_ID": instance_id,
                "V16_DATA_DIR": str(data_dir),
                "V17_MULTIUSER_EXPORT_ROOT": str(export_base),
                "V17_TENANT_EXPORT_ROOT": str(export_dir),
                "V16_HOST": "127.0.0.1",
                "PORT": str(port),
                "V17_SESSION_COOKIE_NAME": cookie_name,
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
                "V25_SERVER_CONTROL_TOKEN": control_token,
                "V25_SERVER_CONTROL_LOCAL_IP": "127.0.0.1",
                "V17_RENDER_SLOT_FILE": str(RENDER_SLOT_FILE),
                "V17_RENDER_SLOT_COUNT": str(GLOBAL_CONCURRENT_TASKS),
                "V17_GLOBAL_FIFO_DATABASE": str(GLOBAL_FIFO_DATABASE),
                # _render_environment supplies all three shared public slots.
                # Both partitions lock bytes in the same host-wide lock file.
                "V17_QUEUE_WORKERS": str(PER_USER_CONCURRENT_TASKS),
                # Three CPU-heavy pipelines at eight threads each use 24 of the
                # host's 28 logical CPUs and leave headroom for the gateway/OS.
                "V16_FFMPEG_THREADS": "8",
                "V16_RENDER_WORKERS": "1",
                "V16_LOCAL_NO_VALIDATION": (
                    "1" if AUTH_MODE == "development" else "0"
                ),
                "V16_ERP_DIRECT_ENABLED": "1" if ERP_DIRECT_REQUESTED else "0",
                "V16_ERP_RELAY_ENABLED": "0",
                # Rendering is the durable primary workload. Direct ERP delivery
                # is audited and retried from the per-tenant outbox, but it is
                # never allowed to stop the all-day/night production queue.
                "V17_STRICT_ERP_RELAY": "0",
            }
        )
        environment.pop("V17_AUTH_SHARED_SECRET_FILE", None)
        environment.update(_render_environment(user["user_id"]))
        environment.pop("V16_ERP_BEARER_TOKEN", None)
        log_dir = _plain_directory(MULTIUSER_ROOT / "logs")
        log_path = log_dir / f"tenant-{user['user_id']}.log"
        log_handle = log_path.open("ab", buffering=0)
        command = [
            str(PYTHON_EXE),
            "-I",
            "-B",
            "-X",
            "utf8",
            "-u",
            str(SERVER_RUNTIME),
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = 0x08000000 | 0x00000200
        guard.close()
        try:
            process = subprocess.Popen(
                command,
                cwd=SERVER_RUNTIME.parent,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                close_fds=True,
                creationflags=creationflags,
            )
        except BaseException:
            log_handle.close()
            raise
        backend = TenantBackend(
            user_id=user["user_id"],
            port=port,
            gateway_token=gateway_token,
            control_token=control_token,
            cookie_name=cookie_name,
            erp_token_fingerprint="",
            process=process,
            last_used=time.monotonic(),
            log_handle=log_handle,
        )
        deadline = time.monotonic() + BACKEND_START_TIMEOUT
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            if self._healthy(backend):
                try:
                    if not maintenance_only:
                        self._install_erp_session(backend, user)
                        _fifo_for_user(user["user_id"]).mark_ready(
                            user["user_id"],
                            user["tenant_id"],
                            backend.gateway_token,
                            # FIFO readiness describes a live renderer, not a
                            # short-lived ERP transport credential.
                            token_expires_at=0,
                        )
                except (
                    OSError,
                    sqlite3.Error,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as exc:
                    self._terminate(backend)
                    if isinstance(exc, RuntimeError):
                        raise
                    raise RuntimeError(
                        "全局队列调度状态无法安全更新，请稍后重试"
                    ) from exc
                return backend
            time.sleep(0.25)
        self._terminate(backend)
        raise RuntimeError("个人制作空间启动失败，请联系管理员查看租户日志")

    def get(self, user: dict) -> TenantBackend:
        user_id = str(user["user_id"])
        lane = self._lane_for(user_id)
        with lane.lock:
            while True:
                with self._lock:
                    if self._stopping:
                        raise RuntimeError("服务器正在关闭")
                    self._prune_dead_locked()
                    if user_id in self._closing_backends or _user_is_closing(user_id):
                        raise RuntimeError("当前个人空间正在安全退出")
                    backend = self._backends.get(user_id)
                    if backend is None:
                        reserved = self._reserved_user_ids_locked()
                        if (
                            not _is_local_user(user_id) and user_id not in reserved
                            and len({uid for uid in reserved if not _is_local_user(uid)}) >= MAX_TENANT_BACKENDS
                        ):
                            raise RuntimeError(_capacity_error(MAX_TENANT_BACKENDS))
                        self._starting_backends.add(user_id)

                if backend is not None:
                    install_error = None
                    healthy = self._healthy(backend)
                    if healthy and not bool(user.get("_retention_only")):
                        try:
                            self._install_erp_session(backend, user)
                            _fifo_for_user(user["user_id"]).mark_ready(
                                user["user_id"],
                                user["tenant_id"],
                                backend.gateway_token,
                                token_expires_at=0,
                            )
                        except (
                            OSError,
                            sqlite3.Error,
                            RuntimeError,
                            TypeError,
                            ValueError,
                        ) as exc:
                            install_error = (
                                exc
                                if isinstance(exc, RuntimeError)
                                else RuntimeError(
                                    "全局队列调度状态无法安全更新，请稍后重试"
                                )
                            )
                            healthy = False
                    if healthy:
                        with self._lock:
                            if (
                                self._backends.get(user_id) is backend
                                and not self._stopping
                                and user_id not in self._closing_backends
                                and not _user_is_closing(user_id)
                            ):
                                backend.last_used = time.monotonic()
                                return backend
                        continue

                    owns_close = False
                    with self._lock:
                        if self._backends.get(user_id) is backend:
                            self._backends.pop(user_id, None)
                            self._closing_backends[user_id] = backend
                            owns_close = True
                    if owns_close:
                        try:
                            self._terminate(backend)
                        finally:
                            self._finish_closing(user_id, backend)
                    if install_error is not None:
                        raise install_error
                    continue

                try:
                    created = self._launch(user)
                except BaseException:
                    with self._lock:
                        self._starting_backends.discard(user_id)
                    raise

                reject_reason = ""
                with self._lock:
                    self._starting_backends.discard(user_id)
                    if self._stopping:
                        reject_reason = "服务器正在关闭"
                    elif user_id in self._closing_backends or _user_is_closing(user_id):
                        reject_reason = "当前个人空间正在安全退出"
                    elif user_id in self._backends:
                        reject_reason = "当前个人空间启动状态已更新"
                    if not reject_reason:
                        created.last_used = time.monotonic()
                        self._backends[user_id] = created
                        return created
                    self._closing_backends.setdefault(user_id, created)
                try:
                    self._terminate(created)
                finally:
                    self._finish_closing(user_id, created)
                raise RuntimeError(reject_reason)

    def stop_user(self, user_id: str, *, preserve_unfinished: bool = True) -> bool:
        if not USER_ID_RE.fullmatch(user_id):
            return False
        if preserve_unfinished and _user_requires_live_backend(user_id):
            return False
        lane = self._lane_for(user_id)
        with lane.lock:
            # Check again after serializing this tenant.  A queue row may have
            # committed between the first check and acquisition of the lane.
            if preserve_unfinished and _user_requires_live_backend(user_id):
                return False
            backend = None
            with self._lock:
                self._prune_dead_locked()
                if user_id in self._closing_backends:
                    return False
                backend = self._backends.get(user_id)
                if backend is not None and backend.inflight_requests > 0:
                    return False
                backend = self._backends.pop(user_id, None)
                if backend is not None:
                    self._closing_backends[user_id] = backend
            if backend is not None:
                try:
                    self._terminate(backend)
                finally:
                    self._finish_closing(user_id, backend)
                return True
        return False

    def reap_idle(self) -> None:
        cutoff = time.monotonic() - BACKEND_IDLE_SECONDS
        with self._lock:
            if self._stopping:
                return
            self._prune_dead_locked()
            with _browser_sessions_lock:
                active_users = _active_browser_user_ids_locked()
                candidates = [
                    (user_id, backend)
                    for user_id, backend in self._backends.items()
                    if backend.last_used < cutoff
                    and backend.inflight_requests == 0
                    and user_id not in active_users
                ]
        # SQLite reads stay outside manager/browser locks.  Only a definite
        # empty queue with no pending downloaded-output collector duty may be
        # reclaimed; lock/schema/I/O uncertainty is kept.
        drained = [
            (user_id, backend)
            for user_id, backend in candidates
            if _tenant_unfinished_state(user_id) is False
            and _tenant_download_retention_worker_state(user_id) is False
        ]
        idle: list[tuple[str, TenantBackend]] = []
        with self._lock:
            if self._stopping:
                return
            self._prune_dead_locked()
            with _browser_sessions_lock:
                active_users = _active_browser_user_ids_locked()
                for user_id, backend in drained:
                    if (
                        self._backends.get(user_id) is backend
                        and backend.last_used < cutoff
                        and backend.inflight_requests == 0
                        and user_id not in active_users
                    ):
                        self._backends.pop(user_id, None)
                        self._closing_backends[user_id] = backend
                        idle.append((user_id, backend))
        for user_id, backend in idle:
            try:
                self._terminate(backend)
            except Exception:
                pass
            finally:
                self._finish_closing(user_id, backend)

    def _terminate(self, backend: TenantBackend) -> None:
        try:
            _fifo_for_user(backend.user_id).mark_offline(
                backend.user_id,
                backend_instance_id=backend.gateway_token,
            )
        except (OSError, sqlite3.Error, RuntimeError, TypeError, ValueError):
            pass
        if backend.process.poll() is None:
            connection = http.client.HTTPConnection("127.0.0.1", backend.port, timeout=2)
            try:
                headers = self._backend_headers(backend)
                headers["X-Kuaishou-Server-Control"] = backend.control_token
                connection.request("POST", "/server-control/shutdown", body=b"", headers=headers)
                response = connection.getresponse()
                response.read()
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            try:
                backend.process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                backend.process.terminate()
                try:
                    backend.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    backend.process.kill()
                    try:
                        backend.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
        self._close_backend_log(backend)

    def stop_all(self) -> None:
        with self._lock:
            self._stopping = True
            lanes = list(self._lanes.values())
        # Drain per-user startup/health work without holding the manager lock.
        # Each lane will observe ``_stopping`` before it can publish a backend.
        for lane in lanes:
            with lane.lock:
                pass
        with self._lock:
            backends = list(self._backends.items())
            for user_id, backend in backends:
                self._closing_backends.setdefault(user_id, backend)
            self._backends.clear()
        for user_id, backend in backends:
            try:
                self._terminate(backend)
            finally:
                self._finish_closing(user_id, backend)


_initialize_state()
_load_persisted_erp_sessions()
GLOBAL_FIFO_COORDINATOR.mark_all_offline()
LOCAL_FIFO_COORDINATOR.mark_all_offline()
app = Flask(__name__)
app.session_interface = EntrySessionInterface()
app.secret_key = _load_or_create_secret(SESSION_SECRET_FILE)
app.config.update(
    MAX_CONTENT_LENGTH=32 * 1024 * 1024 * 1024,
    SESSION_COOKIE_NAME="kuaishou_multiuser_gateway",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=PUBLIC_HTTPS,
    PERMANENT_SESSION_LIFETIME=BROWSER_SESSION_TTL_SECONDS,
)
backend_manager = BackendManager()
atexit.register(backend_manager.stop_all)
atexit.register(_stop_all_auth_flows)


def _teardown_current_browser_session() -> str:
    user_id = str(session.get("user_id") or "")
    # The Flask cookie is signed and carries the unguessable server-side
    # capability. Explicit logout must still work after DHCP/VPN or a browser
    # update changes the IP/User-Agent binding.
    _revoke_browser_session(require_binding=False)
    # Logging out revokes browser access immediately, but it does not cancel
    # durable production work.  The idle reaper closes the tenant only after
    # the queue is definitely empty and the last proxied request is long over.
    return user_id


def _reap_expired_browser_sessions() -> None:
    now = int(time.time())
    with _browser_sessions_lock:
        for key, (_user_id, _binding, expiry) in list(_browser_sessions.items()):
            if expiry <= now:
                _browser_sessions.pop(key, None)


def _tenant_reviver_queue_state(user_id: str, tenant_id: str) -> dict | None:
    """Read one tenant's global FIFO head and lifecycle need fail-closed."""
    if not USER_ID_RE.fullmatch(user_id) or not USER_ID_RE.fullmatch(tenant_id):
        return None
    try:
        users_root = USERS_ROOT.resolve()
        data_dir = users_root / user_id
        if data_dir.is_symlink():
            return None
        if not data_dir.exists():
            return {"unfinished": False, "head": None}
        if not data_dir.is_dir() or data_dir.resolve() != data_dir:
            return None
        database = data_dir / QUEUE_DATABASE_NAME
        if database.is_symlink():
            return None
        if not database.exists():
            return {"unfinished": False, "head": None}
        if not database.is_file() or database.resolve().parent != data_dir:
            return None
        connection = sqlite3.connect(
            database.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=QUEUE_READ_TIMEOUT_SECONDS,
        )
        try:
            connection.execute(
                f"PRAGMA busy_timeout={int(QUEUE_READ_TIMEOUT_SECONDS * 1000)}"
            )
            connection.execute("PRAGMA query_only=ON")
            placeholders = ",".join("?" for _ in UNFINISHED_QUEUE_STATUSES)
            unfinished = connection.execute(
                f"SELECT 1 FROM task_queue WHERE status IN ({placeholders}) LIMIT 1",
                UNFINISHED_QUEUE_STATUSES,
            ).fetchone() is not None
            row = connection.execute(
                "SELECT task_id,created_at,sequence FROM task_queue "
                "WHERE status='waiting' AND owner_user_id=? AND tenant_id=? "
                "ORDER BY created_at ASC,sequence ASC,task_id ASC LIMIT 1",
                (user_id, tenant_id),
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None
    head = None
    if row is not None:
        head = {
            "task_id": str(row[0] or ""),
            "created_at": str(row[1] or ""),
            "sequence": int(row[2] or 0),
        }
    return {"unfinished": bool(unfinished), "head": head}


def _queued_user_candidates() -> list[dict]:
    """Return runnable queue/retention users in global task-time order.

    Durable queue tenants remain runnable after their short-lived ERP token
    expires.  Their accepted output events are retained in a per-user outbox
    until the same owner scans again.  A retention-only tenant likewise needs
    no ERP token because it never submits an ERP request.
    """
    try:
        with _state_connection() as connection:
            rows = connection.execute(
                "SELECT user_id FROM app_users WHERE disabled=0"
            ).fetchall()
    except sqlite3.Error:
        return []
    candidates: list[dict] = []
    for row in rows:
        user_id = str(row["user_id"] or "")
        try:
            user = _user_record_by_id(user_id, require_erp_session=False)
        except sqlite3.Error:
            continue
        if user is None:
            continue
        queue_state = _tenant_reviver_queue_state(
            user_id,
            str(user.get("tenant_id") or ""),
        )
        if queue_state is None:
            continue
        retention_state = _tenant_download_retention_worker_state(user_id)
        if not queue_state["unfinished"] and retention_state is not True:
            continue
        retention_only = bool(
            not queue_state["unfinished"] and retention_state is True
        )
        candidate = dict(user)
        if ERP_DIRECT_REQUESTED and not retention_only:
            erp_session = _erp_session_for_user(user_id)
            if erp_session is not None:
                candidate["_erp_bearer_token"] = erp_session[0]
                candidate["_erp_token_expires_at"] = erp_session[1]
            candidate["_allow_deferred_erp"] = True
        candidate["_fifo_head"] = queue_state.get("head")
        candidate["_retention_only"] = retention_only
        head = candidate["_fifo_head"]
        if isinstance(head, dict):
            candidate["_fifo_sort_key"] = (
                str(head.get("created_at") or ""),
                user_id,
                int(head.get("sequence") or 0),
                str(head.get("task_id") or ""),
            )
        else:
            # Recovery/retention-only tenants must stay live but cannot pass a
            # real waiting task merely because they have no claimable head.
            candidate["_fifo_sort_key"] = (
                "9999-12-31 23:59:59",
                user_id,
                0,
                "",
            )
        candidates.append(candidate)
    candidates.sort(key=lambda item: item["_fifo_sort_key"])
    return candidates


_offline_bundle_cleanup_lock = threading.Lock()


def _existing_plain_directory_tree(path: Path) -> Path:
    """Resolve an existing directory only when no component is a reparse point."""
    candidate = Path(os.path.abspath(str(path)))
    if not candidate.is_absolute() or not candidate.anchor:
        raise OSError("缓存目录不是绝对路径")
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current = current / part
        if not os.path.lexists(current):
            raise OSError("缓存目录不存在")
        metadata = os.lstat(current)
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if current.is_symlink() or attributes & 0x400 or not current.is_dir():
            raise OSError("缓存目录包含链接或重解析点")
    resolved = candidate.resolve(strict=True)
    if os.path.normcase(str(resolved)) != os.path.normcase(str(candidate)):
        raise OSError("缓存目录解析后越界")
    # Close the resolve-versus-last-lstat gap with one final leaf check.
    leaf = os.lstat(candidate)
    leaf_attributes = int(getattr(leaf, "st_file_attributes", 0))
    if candidate.is_symlink() or leaf_attributes & 0x400 or not stat.S_ISDIR(
        leaf.st_mode
    ):
        raise OSError("缓存目录在检查期间发生变化")
    return resolved


def _plain_file_metadata(path: Path):
    """Return lstat metadata for one exact regular file, otherwise ``None``."""
    try:
        metadata = os.lstat(path)
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if path.is_symlink() or attributes & 0x400 or not stat.S_ISREG(metadata.st_mode):
            return None
        return metadata
    except OSError:
        return None


def _same_file_identity(left, right) -> bool:
    return (
        int(getattr(left, "st_dev", 0)) == int(getattr(right, "st_dev", 0))
        and int(getattr(left, "st_ino", 0)) == int(getattr(right, "st_ino", 0))
    )


def _try_lock_existing_bundle_cache(lock_path: Path):
    """Take the tenant-compatible byte lock without creating any path."""
    before = _plain_file_metadata(lock_path)
    if before is None or int(before.st_size) < 1:
        return None
    descriptor = None
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | int(getattr(os, "O_BINARY", 0)),
        )
        opened = os.fstat(descriptor)
        after = _plain_file_metadata(lock_path)
        if after is None or not _same_file_identity(before, opened) or not _same_file_identity(
            opened, after
        ):
            raise OSError("缓存锁文件在检查期间发生变化")
        os.lseek(descriptor, 0, os.SEEK_SET)
        if _fcntl is not None:
            _fcntl.flock(descriptor, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        elif _msvcrt is not None:
            _msvcrt.locking(descriptor, _msvcrt.LK_NBLCK, 1)
        else:
            raise OSError("当前系统不支持缓存文件锁")
        return descriptor
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        return None


def _close_bundle_cache_lock(descriptor) -> None:
    if descriptor is None:
        return
    try:
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if _fcntl is not None:
                _fcntl.flock(descriptor, _fcntl.LOCK_UN)
            elif _msvcrt is not None:
                _msvcrt.locking(descriptor, _msvcrt.LK_UNLCK, 1)
        except OSError:
            # The descriptor is still closed below.  A late unlock error must
            # not abort the bounded sweep or leave later tenants unexamined.
            pass
    finally:
        os.close(descriptor)


def _inactive_bundle_cache_tenants() -> list[tuple[str, Path]]:
    """Enumerate only fixed D/E tenant export roots; missing E never hides D."""
    roots = (MULTIUSER_EXPORT_ROOT, Path("E:/快手自动化GPU服务器8020成片"))
    tenants = []
    seen = set()
    for configured in roots:
        try:
            export_root = _existing_plain_directory_tree(configured)
        except (OSError, RuntimeError, ValueError):
            continue
        for candidate in sorted(export_root.iterdir(), key=lambda item: item.name):
            user_id = candidate.name
            if not USER_ID_RE.fullmatch(user_id):
                continue
            try:
                resolved = _existing_plain_directory_tree(candidate)
            except (OSError, RuntimeError, ValueError):
                continue
            if resolved.parent == export_root and resolved.name == user_id and resolved not in seen:
                seen.add(resolved)
                tenants.append((user_id, resolved))
    return tenants


def cleanup_inactive_bundle_caches(
    *,
    now: float | None = None,
    per_tenant_limit: int = BUNDLE_CACHE_PER_TENANT_SWEEP_LIMIT,
    global_limit: int = BUNDLE_CACHE_GLOBAL_SWEEP_LIMIT,
) -> dict[str, int]:
    """Retry confirmed-transfer ZIP cleanup for tenants with no backend.

    Actual outputs, queue databases, task assets, partial files and lock files
    are outside this exact path/name contract and can never be selected here.
    """
    result = {
        "removed_count": 0,
        "released_bytes": 0,
        "examined_count": 0,
        "failure_count": 0,
    }
    if not _offline_bundle_cleanup_lock.acquire(blocking=False):
        return result
    try:
        try:
            tenant_limit = max(0, min(4, int(per_tenant_limit)))
            total_limit = max(0, min(16, int(global_limit)))
            tenants = _inactive_bundle_cache_tenants()
        except (OSError, TypeError, ValueError, OverflowError):
            result["failure_count"] += 1
            return result
        if tenant_limit <= 0 or total_limit <= 0:
            return result

        for user_id, tenant_root in tenants:
            if result["removed_count"] >= total_limit:
                break
            lane = backend_manager.begin_offline_maintenance(user_id)
            if lane is None:
                continue
            removed_for_tenant = 0
            try:
                cache_path = tenant_root / BUNDLE_CACHE_DIRECTORY_NAME
                try:
                    cache_root = _existing_plain_directory_tree(cache_path)
                except OSError:
                    continue
                if (
                    cache_root.parent != tenant_root
                    or cache_root.name != BUNDLE_CACHE_DIRECTORY_NAME
                ):
                    continue
                for candidate in sorted(cache_root.iterdir(), key=lambda item: item.name):
                    if (
                        removed_for_tenant >= tenant_limit
                        or result["removed_count"] >= total_limit
                    ):
                        break
                    if not BUNDLE_CACHE_FILE_RE.fullmatch(candidate.name):
                        continue
                    before = _plain_file_metadata(candidate)
                    if before is None or not bundle_ready_for_cleanup(candidate, user_id):
                        continue
                    result["examined_count"] += 1
                    lock_path = candidate.with_suffix(".lock")
                    if (
                        lock_path.parent != cache_root
                        or lock_path.name != candidate.stem + ".lock"
                    ):
                        result["failure_count"] += 1
                        continue
                    descriptor = _try_lock_existing_bundle_cache(lock_path)
                    if descriptor is None:
                        continue
                    try:
                        # The tenant lane blocks every gateway-driven start.
                        # Re-check both path identity and time after obtaining
                        # the cross-process cache lock, immediately before unlink.
                        current = _plain_file_metadata(candidate)
                        if (
                            current is None
                            or not _same_file_identity(before, current)
                            or int(current.st_size) != int(before.st_size)
                            or int(current.st_mtime_ns) != int(before.st_mtime_ns)
                            or not bundle_ready_for_cleanup(candidate, user_id)
                        ):
                            continue
                        candidate.unlink()
                        candidate.with_suffix(".json").unlink(missing_ok=True)
                        result["removed_count"] += 1
                        result["released_bytes"] += int(current.st_size)
                        removed_for_tenant += 1
                    except OSError:
                        result["failure_count"] += 1
                    finally:
                        _close_bundle_cache_lock(descriptor)
            finally:
                backend_manager.finish_offline_maintenance(lane)
        return result
    finally:
        _offline_bundle_cleanup_lock.release()


_queue_reviver_lock = threading.Lock()
_queue_reviver_retry_after: dict[str, float] = {}


def _prime_missing_fifo_candidates(
    missing: list[dict],
    batch_generation: str,
) -> list[dict]:
    """Publish a complete runnable snapshot before any backend can claim."""
    prepared: list[dict] = []
    for user in missing:
        user_id = str(user["user_id"])
        tenant_id = str(user["tenant_id"])
        head = user.get("_fifo_head")
        try:
            if isinstance(head, dict):
                sequence = int(head.get("sequence") or 0)
                task_id = str(head.get("task_id") or "")
                _fifo_for_user(user_id).publish_head(
                    user_id,
                    tenant_id,
                    task_id,
                    str(head.get("created_at") or ""),
                    local_sequence=sequence,
                    stable_key=(
                        f"{sequence:020d}:{user_id}:{tenant_id}:{task_id}"
                    ),
                )
                _fifo_for_user(user_id).mark_ready(
                    user_id,
                    tenant_id,
                    batch_generation,
                    token_expires_at=0,
                )
            else:
                _fifo_for_user(user_id).clear_head(
                    user_id,
                    tenant_id=tenant_id,
                )
            prepared.append(user)
        except (OSError, sqlite3.Error, RuntimeError, TypeError, ValueError):
            _queue_reviver_retry_after[user_id] = time.monotonic() + 15.0
    return prepared


def _run_queue_reviver() -> None:
    if not _queue_reviver_lock.acquire(blocking=False):
        return
    try:
        now = time.monotonic()
        missing: list[dict] = []
        for user in _queued_user_candidates():
            user_id = str(user["user_id"])
            if backend_manager.has_backend_reservation(user_id):
                _queue_reviver_retry_after.pop(user_id, None)
                continue
            if _queue_reviver_retry_after.get(user_id, 0.0) > now:
                continue
            missing.append(user)

        # Publish every valid missing tenant before launching the first one.
        # The oldest unstarted tenant may briefly block a slot, but a backend
        # that starts faster can never let its later task jump the queue.
        batch_generation = f"reviver-{uuid.uuid4().hex}"
        prepared = _prime_missing_fifo_candidates(missing, batch_generation)

        for user in prepared:
            user_id = str(user["user_id"])
            try:
                backend_manager.get(user)
            except RuntimeError:
                try:
                    _fifo_for_user(user_id).mark_offline(
                        user_id,
                        backend_instance_id=batch_generation,
                    )
                except (OSError, sqlite3.Error, RuntimeError, TypeError, ValueError):
                    pass
                _queue_reviver_retry_after[user_id] = time.monotonic() + 15.0
            else:
                _queue_reviver_retry_after.pop(user_id, None)
    finally:
        _queue_reviver_lock.release()


def _schedule_queue_reviver() -> None:
    threading.Thread(
        target=_run_queue_reviver,
        name="kuaishou-queued-tenant-reviver",
        daemon=True,
    ).start()


def _prime_startup_global_fifo() -> None:
    """Publish all valid persisted heads before the gateway accepts traffic."""
    try:
        candidates = [
            user
            for user in _queued_user_candidates()
            if not backend_manager.has_backend_reservation(str(user["user_id"]))
        ]
        _prime_missing_fifo_candidates(
            candidates,
            f"bootstrap-{uuid.uuid4().hex}",
        )
    except (OSError, sqlite3.Error, RuntimeError, TypeError, ValueError):
        # Workers remain fail-closed until the asynchronous reviver succeeds.
        return


_housekeeping_stop = threading.Event()


def _housekeeping_loop() -> None:
    while not _housekeeping_stop.wait(15):
        try:
            _cleanup_auth_flows()
            _reap_expired_browser_sessions()
            backend_manager.reap_idle()
            cleanup_inactive_bundle_caches()
            _schedule_queue_reviver()
            now = int(time.time())
            with _erp_sessions_lock:
                expired_changed = False
                for user_id, (_token, expiry) in list(_erp_sessions.items()):
                    if expiry <= now + 5:
                        _erp_sessions.pop(user_id, None)
                        expired_changed = True
                if expired_changed:
                    _persist_erp_sessions_locked()
        except Exception:
            # Housekeeping must never take down the authenticated gateway.
            continue


def _stop_housekeeping() -> None:
    _housekeeping_stop.set()


threading.Thread(
    target=_housekeeping_loop,
    name="kuaishou-gateway-housekeeping",
    daemon=True,
).start()
_prime_startup_global_fifo()
_schedule_queue_reviver()
atexit.register(_stop_housekeeping)


def _pending_flow_for_session() -> PendingAuth | None:
    flow_id = str(session.get("auth_flow_id") or "")
    browser_bind = str(session.get("auth_browser_bind") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", flow_id):
        return None
    if not _capability_ok(browser_bind):
        return None
    with _pending_auth_lock:
        flow = _pending_auth.get(flow_id)
        if flow is None:
            return None
        if (
            not hmac.compare_digest(flow.browser_hash, _secret_hash(browser_bind))
            or not hmac.compare_digest(flow.client_hash, _browser_binding_hash())
        ):
            return None
        return flow


def _reusable_pending_flow(
    login_hash: bytes,
    client_hash: bytes,
    remote_addr: str,
) -> PendingAuth | None:
    now = int(time.time())
    with _pending_auth_lock:
        for flow in _pending_auth.values():
            reusable_phase = (
                (
                    flow.phase in {"waiting", "claimed"}
                    and flow.expires_at >= now
                    and flow.process.poll() is None
                )
                or (flow.phase == "complete" and flow.expires_at >= now)
                or (
                    flow.phase == "authenticated"
                    and flow.terminal_expires_at >= now
                    and bool(flow.completed_user_id)
                    and _capability_ok(flow.browser_session_value)
                )
            )
            if (
                flow.login_hash
                and hmac.compare_digest(flow.login_hash, login_hash)
                and hmac.compare_digest(flow.client_hash, client_hash)
                and hmac.compare_digest(flow.remote_addr, remote_addr)
                and reusable_phase
                and _capability_ok(flow.browser_bind_value)
                and _capability_ok(flow.csrf_value)
            ):
                return flow
    return None


def _bind_session_to_pending_flow(flow: PendingAuth) -> None:
    if (
        not _capability_ok(flow.flow_id)
        or not _capability_ok(flow.browser_bind_value)
        or not _capability_ok(flow.csrf_value)
    ):
        raise RuntimeError("扫码登录会话绑定无效")
    session.clear()
    session["auth_flow_id"] = flow.flow_id
    session["auth_browser_bind"] = flow.browser_bind_value
    session["auth_csrf"] = flow.csrf_value
    session["auth_started"] = int(time.time())


def _establish_completed_flow_session(flow: PendingAuth) -> bool:
    with flow.completion_lock:
        with _pending_auth_lock:
            if (
                _pending_auth.get(flow.flow_id) is not flow
                or flow.phase != "authenticated"
                or not USER_ID_RE.fullmatch(flow.completed_user_id)
                or not _capability_ok(flow.browser_session_value)
            ):
                return False
            user_id = flow.completed_user_id
            session_capability = flow.browser_session_value
            expires_at = flow.browser_session_expires_at
        _establish_browser_session(
            user_id,
            session_capability=session_capability,
            expires_at=expires_at,
        )
        return True


def _clean_broker_identity(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("钉钉登录没有返回有效 ERP 身份")
    identity: dict[str, str] = {}
    for key in SAFE_IDENTITY_KEYS:
        item = value.get(key)
        if isinstance(item, (str, int, float)) and not isinstance(item, bool):
            text = re.sub(r"\s+", " ", str(item)).strip()
            if text and text.lower() not in {"null", "undefined", "none", "-"}:
                identity[key] = text[:256]
    if not identity.get("erp_user_id"):
        raise ValueError("钉钉登录没有返回 ERP 用户编号")
    if not (identity.get("erp_user") or identity.get("erp_nickname")):
        raise ValueError("钉钉登录没有返回 ERP 用户名称")
    return identity


def _finish_pending_flow(
    flow: PendingAuth,
    *,
    expected_phase: str,
    result: dict | None,
    error: str,
) -> bool:
    with _pending_auth_lock:
        current = _pending_auth.get(flow.flow_id)
        if current is not flow or flow.phase != expected_phase:
            return False
        flow.result = result
        flow.error = error[:240]
        flow.phase = "complete" if result is not None else "failed"
        flow.result_ticket_hash = b""
        flow.broker_secret = b""
        flow.control_ticket = ""
        return True


@app.post("/internal/auth-result")
def internal_auth_result():
    if request.remote_addr not in {"127.0.0.1", "::1"}:
        abort(404)
    content_type = str(request.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return jsonify(ok=False), 415
    content_length = request.content_length
    if content_length is not None and content_length > MAX_AUTH_CALLBACK_BYTES:
        return jsonify(ok=False), 413
    raw = request.stream.read(MAX_AUTH_CALLBACK_BYTES + 1)
    if not raw or len(raw) > MAX_AUTH_CALLBACK_BYTES:
        return jsonify(ok=False), 400
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return jsonify(ok=False), 400
    if not isinstance(payload, dict):
        return jsonify(ok=False), 400
    flow_id = str(payload.get("flow_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", flow_id):
        return jsonify(ok=False), 400
    with _pending_auth_lock:
        flow = _pending_auth.get(flow_id)
        if flow is None or flow.phase != "waiting":
            return jsonify(ok=False), 409
        broker_secret = flow.broker_secret
        ticket_hash = flow.result_ticket_hash
        state = flow.state
        nonce = flow.nonce
        expires_at = flow.expires_at
    timestamp = str(request.headers.get("X-Kuaishou-Broker-Timestamp") or "")
    proof = str(request.headers.get("X-Kuaishou-Broker-Proof") or "")
    authorization = str(request.headers.get("Authorization") or "")
    ticket_match = re.fullmatch(r"Bearer ([A-Za-z0-9_-]{43,128})", authorization)
    if not timestamp.isdigit() or len(timestamp) > 12 or not re.fullmatch(r"[0-9a-f]{64}", proof):
        return jsonify(ok=False), 403
    now = int(time.time())
    if abs(now - int(timestamp)) > 30 or now > expires_at + 5:
        return jsonify(ok=False), 403
    body_hash = hashlib.sha256(raw).hexdigest()
    expected_proof = hmac.new(
        broker_secret,
        f"{timestamp}\n{body_hash}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if (
        ticket_match is None
        or not hmac.compare_digest(_secret_hash(ticket_match.group(1)), ticket_hash)
        or not hmac.compare_digest(proof, expected_proof)
        or not hmac.compare_digest(str(payload.get("state") or ""), state)
        or not hmac.compare_digest(str(payload.get("nonce") or ""), nonce)
    ):
        return jsonify(ok=False), 403
    try:
        issued_at = int(payload.get("issued_at"))
    except (TypeError, ValueError):
        return jsonify(ok=False), 400
    if abs(now - issued_at) > 60:
        return jsonify(ok=False), 400
    with _pending_auth_lock:
        if (
            _pending_auth.get(flow_id) is not flow
            or flow.phase != "waiting"
            or not flow.result_ticket_hash
            or flow.expires_at < now
        ):
            return jsonify(ok=False), 409
        flow.phase = "claimed"
        flow.result_ticket_hash = b""
    outcome = str(payload.get("outcome") or "")
    if outcome != "success":
        error_codes = {
            "flow_expired": "扫码登录已过期，请重新扫码",
            "user_cancelled": "扫码登录已取消",
            "browser_start_failed": "本机扫码组件启动失败，请点击重新扫码",
            "erp_bootstrap_failed": "登录服务初始化失败，已自动重试；请点击重新扫码",
            "browser_create_failed": "扫码窗口创建失败，请点击重新扫码",
            "browser_capture_failed": "扫码窗口初始化失败，请点击重新扫码",
            "browser_closed": "本机扫码窗口意外关闭",
        }
        code = str(payload.get("error_code") or "authentication_failed")
        if not _finish_pending_flow(
            flow,
            expected_phase="claimed",
            result=None,
            error=error_codes.get(code, "钉钉扫码登录失败，请重试"),
        ):
            return jsonify(ok=False), 409
        return "", 204
    try:
        identity = _clean_broker_identity(payload.get("identity"))
        token = str(payload.get("erp_bearer_token") or "").strip()
        if token[:7].lower() == "bearer ":
            token = token[7:].strip()
        if not ERP_TOKEN_RE.fullmatch(token):
            raise ValueError("钉钉登录返回的 ERP 授权格式无效")
        raw_subject = identity["erp_user_id"]
        subject = hashlib.sha256(
            ("erp-intranet\0" + raw_subject).encode("utf-8")
        ).hexdigest()
        claims = {
            "sub": subject,
            "corp_id": "erp-intranet",
            "display_name": (
                identity.get("erp_user")
                or identity.get("erp_nickname")
                or raw_subject
            ),
            "identity": identity,
        }
        result: dict = {"claims": claims}
        if ERP_DIRECT_REQUESTED:
            token_expiry = _integer_expiry(payload.get("erp_token_expires_at"))
            if token_expiry is None:
                token_expiry = _jwt_expiry(token)
            if token_expiry is None or token_expiry <= now + 5:
                raise ValueError("当前用户的 ERP 授权缺少有效期，请重新扫码")
            result["erp_session"] = (token, token_expiry)
        if not _finish_pending_flow(
            flow, expected_phase="claimed", result=result, error=""
        ):
            return jsonify(ok=False), 409
    except ValueError as exc:
        if not _finish_pending_flow(
            flow, expected_phase="claimed", result=None, error=str(exc)
        ):
            return jsonify(ok=False), 409
    return "", 204


LOGIN_PAGE = """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,">
<title>快手自动化 · 独立空间登录</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#07111d;color:#eef7ff;font-family:"Segoe UI",sans-serif}
main{width:min(460px,calc(100vw - 40px));padding:34px;border:1px solid #24425e;border-radius:14px;background:#0d1c2c;box-shadow:0 28px 80px #0008}
h1{margin:0 0 14px;font-size:25px}p{color:#a9bdd0;line-height:1.7}.button{display:flex;justify-content:center;margin-top:24px;padding:13px;border-radius:9px;background:linear-gradient(135deg,#14b8d4,#7047eb);color:white;text-decoration:none;font-weight:700}
.error{padding:10px 12px;border:1px solid #d65b6b;border-radius:8px;background:#611c2b66;color:#ffd1d7}
.launch{display:flex;align-items:center;justify-content:center;gap:12px;margin-top:24px;padding:14px;border-radius:9px;background:#132a3e;color:#bfefff}
.spinner{width:18px;height:18px;border:3px solid #3b5970;border-top-color:#21c1db;border-radius:50%;animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
</style></head><body><main><h1>{{ '本机专用入口 · 预留 1 个制作名额' if local_entry else '进入你的独立制作空间' }}</h1>
{% if local_entry %}<p>仅限这台服务器本机使用；首次扫码绑定专用账号。素材和任务与公共入口分开。</p>{% endif %}
<p>登录后只会显示你的素材、任务、日志和下载；其他人的任务不会出现在你的页面或接口中。</p>
{% if error %}<div class="error">{{ error }}</div>{% endif %}
    {% if ready and not error %}<div id="launch" class="launch"><span class="spinner"></span><span>正在打开 ERP 钉钉扫码…</span></div>
    <form id="login-form" method="post" action="{{ url_for('auth_login') }}">
      <input type="hidden" name="csrf" value="{{ login_csrf }}">
    </form>
    <script>document.getElementById("login-form").submit();</script>
    {% elif ready %}
    <p>本次扫码未能启动，点击下方按钮重新生成二维码。已有制作任务会继续运行。</p>
    <form id="login-form" method="post" action="{{ url_for('auth_login') }}">
      <input type="hidden" name="csrf" value="{{ login_csrf }}">
      <button class="button" type="submit" style="border:0;width:100%;cursor:pointer;font:inherit">重新扫码</button>
    </form>
    {% else %}<div class="error">本机钉钉扫码组件不完整，服务器保持关闭式登录，未匿名开放任务。</div>{% endif %}
    </main></body></html>
"""


SCAN_PAGE = """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,">
<title>钉钉扫码登录 · 快手自动化</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#07111d;color:#eef7ff;font-family:"Segoe UI",sans-serif}
main{width:min(520px,calc(100vw - 32px));padding:28px;border:1px solid #24425e;border-radius:14px;background:#0d1c2c;box-shadow:0 28px 80px #0008;text-align:center}
h1{margin:0 0 10px;font-size:24px}p{color:#a9bdd0;line-height:1.6}.frame{min-height:360px;display:grid;place-items:center;margin:18px auto;padding:10px;border-radius:12px;background:white;overflow:hidden}
#qr{display:none;max-width:100%;width:365px;height:auto}.status{min-height:28px;color:#8fdcf0}.error{color:#ffb9c2}.button{display:inline-block;margin-top:12px;padding:10px 18px;border:0;border-radius:8px;background:#334b62;color:white;text-decoration:none;cursor:pointer}
</style></head><body><main><h1>请使用钉钉扫码</h1>
<p>二维码由这台服务器上的独立临时登录窗口生成；本次扫码只绑定当前浏览器。</p>
<div class="frame"><img id="qr" alt="钉钉登录二维码"><div id="loading">正在加载二维码…</div></div>
<div id="status" class="status">二维码显示后请扫码，页面会自动进入你的空间</div>
<form id="status-form" method="post" action="{{ status_page_url }}">
  <input type="hidden" name="csrf" value="{{ csrf }}">
</form>
<form id="cancel-form" method="post" action="{{ cancel_page_url }}">
  <input type="hidden" name="csrf" value="{{ csrf }}">
  <button class="button" type="submit">取消并返回</button>
</form>
<script>
const qr=document.getElementById("qr");const loading=document.getElementById("loading");const qrUrl={{ qr_url|tojson }};let qrRetry=0;let statusTimer=0;
function loadQr(){qr.src=qrUrl+"?v="+Date.now();}
qr.addEventListener("load",()=>{qr.style.display="block";loading.style.display="none";});
qr.addEventListener("error",()=>{qr.style.display="none";loading.style.display="block";qrRetry=setTimeout(loadQr,900);});
function stopTimers(){clearTimeout(qrRetry);clearTimeout(statusTimer);}
document.getElementById("cancel-form").addEventListener("submit",stopTimers);
window.addEventListener("pagehide",stopTimers);
loadQr();
statusTimer=setTimeout(()=>document.getElementById("status-form").submit(),4000);
</script></main></body></html>
"""


AUTH_STATUS_ERROR_PAGE = """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="0;url=/">
<link rel="icon" href="data:,">
<title>正在进入制作空间 · 快手自动化</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#07111d;color:#eef7ff;font-family:"Segoe UI",sans-serif}
main{width:min(480px,calc(100vw - 40px));padding:34px;border:1px solid #24425e;border-radius:14px;background:#0d1c2c;box-shadow:0 28px 80px #0008;text-align:center}
h1{margin:0 0 14px;font-size:24px}p{color:#a9bdd0;line-height:1.7}.actions{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin-top:24px}.button{padding:11px 17px;border-radius:8px;background:#334b62;color:white;text-decoration:none}.primary{background:linear-gradient(135deg,#14b8d4,#7047eb)}
</style></head><body><main><h1>正在进入制作空间</h1>
<p>登录状态已更新，页面正在自动继续。</p>
<div class="actions"><a class="button primary" href="/">立即继续</a></div>
</main></body></html>
"""


CAPACITY_PAGE = """
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,">
<title>使用名额已满 · 快手自动化</title><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#07111d;color:#eef7ff;font-family:"Segoe UI",sans-serif}
main{width:min(500px,calc(100vw - 40px));padding:34px;border:1px solid #24425e;border-radius:14px;background:#0d1c2c;box-shadow:0 28px 80px #0008;text-align:center}
h1{margin:0 0 14px;font-size:24px}p{color:#a9bdd0;line-height:1.7}.actions{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin-top:24px}
.button{padding:11px 17px;border:0;border-radius:8px;background:#334b62;color:white;text-decoration:none;cursor:pointer;font:inherit}.primary{background:linear-gradient(135deg,#14b8d4,#7047eb)}form{margin:0}
</style></head><body><main><h1>当前 {{ login_user_limit }} 个使用名额已满</h1>
<p>请等待其他用户退出登录后再试；有人退出后，刷新本页即可进入。</p>
<p>全天允许全部已启用账号登录（物理安全上限 16 位），每人一个独立制作空间。整台服务器共享 3 个制作槽，全局按最初加入时间依次启动，不按账号预留；槽满后继续排队。</p>
<div class="actions"><a class="button primary" href="/">刷新重试</a>
<form method="post" action="/logout"><button class="button" type="submit">退出当前登录</button></form></div>
</main></body></html>
"""


def _capacity_response(error: str | None = None) -> tuple[str, int]:
    login_user_limit = _capacity_limit_from_error(str(error or ""))
    if login_user_limit is None:
        login_user_limit, login_limit_mode = _login_user_policy()
    else:
        login_limit_mode = "all_day"
    return (
        render_template_string(
            CAPACITY_PAGE,
            login_user_limit=login_user_limit,
            login_limit_mode=login_limit_mode,
        ),
        503,
    )


@app.after_request
def _security_headers(response):
    if is_local_entry():
        response.headers["X-Kuaishou-Entry"] = "local-reserved-1"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "media-src 'self' blob:; connect-src 'self'; font-src 'self' data:; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'",
    )
    return response


@app.get("/healthz")
def healthz():
    login_user_limit, login_limit_mode = _login_user_policy()
    return jsonify(
        ok=True,
        mode="authenticated-multiuser-gateway",
        record_delivery_mode="erp_direct_only",
        usage_backup_enabled=False,
        concurrent_tasks=GLOBAL_CONCURRENT_TASKS,
        global_concurrent_tasks=GLOBAL_CONCURRENT_TASKS,
        per_user_concurrent_tasks=3,
        public_concurrent_tasks=3,
        local_reserved_concurrent_tasks=0,
        local_entry_url=None,
        entry="local-reserved" if is_local_entry() else "public",
        scheduler_mode="public-fifo3",
        scheduler_schema=1,
        downloaded_output_retention_seconds=48 * 60 * 60,
        output_retention_basis="task_finished_at",
        max_active_users=login_user_limit,
        max_tenant_backends=MAX_TENANT_BACKENDS,
        login_user_limit=login_user_limit,
        day_login_user_limit=DAY_LOGIN_USER_LIMIT,
        night_login_user_limit=NIGHT_LOGIN_USER_LIMIT,
        login_user_limit_mode=login_limit_mode,
        login_mode=login_limit_mode,
        login_user_limits={"day": DAY_LOGIN_USER_LIMIT, "night": NIGHT_LOGIN_USER_LIMIT},
    )


@app.get("/favicon.ico")
def favicon():
    # Browsers request this automatically while the anonymous ERP login flow
    # is active.  It must remain session-neutral or it erases the QR binding.
    return "", 204


@app.get("/server-info")
def server_info():
    login_user_limit, login_limit_mode = _login_user_policy()
    return jsonify(
        ok=True,
        mode="authenticated-multiuser-gateway",
        record_delivery_mode="erp_direct_only",
        usage_backup_enabled=False,
        profile="v1.2-multiuser-process-isolation",
        browser_session_ttl_seconds=BROWSER_SESSION_TTL_SECONDS,
        browser_session_sliding=True,
        login_continuity_version="20260930.1",
        release_id=RELEASE_ID,
        instance_id=os.environ.get("V17_INSTANCE_ID", ""),
        service_pid=os.getpid(),
        port=8021 if is_local_entry() else int(os.environ.get("PORT", "8020")),
        bind="127.0.0.1" if is_local_entry() else os.environ.get("V16_HOST", ""),
        shared_console=False,
        concurrent_tasks=GLOBAL_CONCURRENT_TASKS,
        global_concurrent_tasks=GLOBAL_CONCURRENT_TASKS,
        per_user_concurrent_tasks=3,
        public_concurrent_tasks=3,
        local_reserved_concurrent_tasks=0,
        local_entry_url=None,
        entry="local-reserved" if is_local_entry() else "public",
        scheduler_mode="public-fifo3",
        scheduler_schema=1,
        downloaded_output_retention_seconds=48 * 60 * 60,
        output_retention_basis="task_finished_at",
        max_active_users=login_user_limit,
        max_tenant_backends=MAX_TENANT_BACKENDS,
        login_user_limit=login_user_limit,
        day_login_user_limit=DAY_LOGIN_USER_LIMIT,
        night_login_user_limit=NIGHT_LOGIN_USER_LIMIT,
        login_user_limit_mode=login_limit_mode,
        login_mode=login_limit_mode,
        login_user_limits={"day": DAY_LOGIN_USER_LIMIT, "night": NIGHT_LOGIN_USER_LIMIT},
        tenant_isolation="process+database+filesystem",
    )


@app.get("/auth")
def auth_page():
    _cleanup_auth_flows()
    if _current_user() is not None:
        return redirect("/", code=303)
    if AUTH_MODE == "local_erp":
        active_flow = _pending_flow_for_session()
        active_csrf = str(session.get("auth_csrf") or "")
        if active_flow is not None and _capability_ok(active_csrf):
            return redirect(url_for("auth_scan"), code=303)
    # Keep one login token for the lifetime of this anonymous browser session.
    # Rotating it on every GET makes two tabs (or a refresh/back navigation)
    # invalidate each other's still-visible form and produces a false 403.
    login_csrf = str(session.get("auth_login_csrf") or "")
    if not _capability_ok(login_csrf):
        login_csrf = _capability()
        session["auth_login_csrf"] = login_csrf
    page_error = request.args.get("error", "")[:240]
    # An expired/stale browser flow is recoverable and must not strand the
    # user on an error-only page.  Returning to /auth should immediately
    # create a fresh ERP DingTalk QR flow without requiring another click.
    if page_error in {
        "扫码登录已失效，请重新开始",
        "扫码登录已过期，请重新扫码",
    }:
        page_error = ""
    return render_template_string(
        LOGIN_PAGE,
        local_entry=is_local_entry(),
        ready=bool(
            AUTH_MODE == "development"
            or (AUTH_MODE == "local_erp" and _auth_broker_assets_ready())

        ),
        error=page_error,
        login_csrf=login_csrf,
    )


@app.route("/auth/login", methods=["GET", "POST"])
def auth_login():
    _cleanup_auth_flows()
    if _current_user() is not None:
        return redirect("/", code=303)
    if AUTH_MODE == "local_erp":
        # A stale tab or browser refresh turns the previous form POST URL into
        # a GET.  GET must never start authentication, but it can safely return
        # the user to the entry page instead of exposing a raw Flask 403 page.
        if request.method != "POST":
            return redirect(url_for("auth_page"), code=303)
        expected_csrf = str(session.get("auth_login_csrf") or "")
        provided_csrf = str(
            request.headers.get("X-Kuaishou-Login-CSRF")
            or request.form.get("csrf")
            or ""
        )
        if (
            not _capability_ok(expected_csrf)
            or not _capability_ok(provided_csrf)
            or not hmac.compare_digest(expected_csrf, provided_csrf)
        ):
            abort(403)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    _teardown_current_browser_session()
    if AUTH_MODE != "local_erp":
        session.clear()
        session["auth_state"] = state
        session["auth_nonce"] = nonce
        session["auth_started"] = int(time.time())
    if AUTH_MODE == "development":
        if (
            os.environ.get("V16_INSTALLER_SELF_TEST") != "1"
            or request.remote_addr not in {"127.0.0.1", "::1"}
        ):
            abort(403)
        requested_user = str(request.args.get("user") or "acceptance-user").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", requested_user):
            abort(400)
        claims = {
            "sub": os.environ.get(
                "V17_DEV_USER_SUB", f"local-{requested_user}"
            ),
            "corp_id": os.environ.get("V17_DEV_CORP_ID", "local-acceptance-corp"),
            "display_name": f"本机验收账号 {requested_user}",
            "identity": {"erp_user_id": f"local-{requested_user}"},
        }
        user = _upsert_user(claims)
        _establish_browser_session(user["user_id"])
        _clear_erp_session(user["user_id"])
        return redirect("/", code=303)
    if AUTH_MODE == "local_erp":
        if not _auth_broker_assets_ready():
            return redirect(
                url_for("auth_page", error="本机钉钉扫码组件不完整，请重新校验安装包"),
                code=303,
            )
        remote_addr = str(request.remote_addr or "")[:80]
        client_hash = _browser_binding_hash()
        login_hash = _secret_hash(provided_csrf)
        start_event: threading.Event | None = None
        leader = False
        for _attempt in range(2):
            reusable = _reusable_pending_flow(login_hash, client_hash, remote_addr)
            if reusable is not None:
                if _establish_completed_flow_session(reusable):
                    return redirect("/", code=303)
                _bind_session_to_pending_flow(reusable)
                return redirect(url_for("auth_scan"), code=303)
            with _pending_auth_lock:
                start_event = _pending_auth_login_starts.get(login_hash)
                if start_event is None:
                    now = int(time.time())
                    active = [
                        flow
                        for flow in _pending_auth.values()
                        if flow.expires_at >= now
                        and flow.phase not in {"failed", "consumed", "cancelled"}
                        and flow.process.poll() is None
                    ]
                    same_address = [
                        flow for flow in active if flow.remote_addr == remote_addr
                    ]
                    starting_total = sum(_pending_auth_starting.values())
                    starting_for_address = _pending_auth_starting.get(remote_addr, 0)
                    if (
                        len(active) + starting_total >= MAX_PENDING_AUTH_FLOWS + (1 if is_local_entry() else 0)
                        or len(same_address) + starting_for_address
                        >= MAX_PENDING_AUTH_FLOWS + (1 if is_local_entry() else 0)
                    ):
                        return redirect(
                            url_for("auth_page", error="当前扫码人数较多，请稍后再试"),
                            code=303,
                        )
                    start_event = threading.Event()
                    _pending_auth_login_starts[login_hash] = start_event
                    _pending_auth_starting[remote_addr] = starting_for_address + 1
                    leader = True
                    break
            if start_event.wait(timeout=20):
                continue
            return redirect(
                url_for("auth_page", error="本机扫码组件启动超时，请重试"),
                code=303,
            )
        if not leader:
            return redirect(
                url_for("auth_page", error="扫码窗口尚未就绪，请重试"),
                code=303,
            )
        flow_id = _capability()
        browser_bind = _capability()
        flow_csrf = _capability()
        flow: PendingAuth | None = None
        try:
            flow = _spawn_auth_flow(
                flow_id,
                state,
                nonce,
                _secret_hash(browser_bind),
                client_hash,
                remote_addr,
                login_hash=login_hash,
                browser_bind_value=browser_bind,
                csrf_value=flow_csrf,
            )
        except RuntimeError as exc:
            return redirect(url_for("auth_page", error=str(exc)), code=303)
        finally:
            with _pending_auth_lock:
                remaining = _pending_auth_starting.get(remote_addr, 1) - 1
                if remaining > 0:
                    _pending_auth_starting[remote_addr] = remaining
                else:
                    _pending_auth_starting.pop(remote_addr, None)
                current_event = _pending_auth_login_starts.get(login_hash)
                if current_event is start_event:
                    _pending_auth_login_starts.pop(login_hash, None)
            start_event.set()
        if flow is None:
            return redirect(
                url_for("auth_page", error="本机扫码组件未返回登录任务"),
                code=303,
            )
        _bind_session_to_pending_flow(flow)
        return redirect(url_for("auth_scan"), code=303)
    abort(404)


@app.get("/auth/scan")
def auth_scan():
    if _current_user() is not None:
        return redirect("/", code=303)
    flow = _pending_flow_for_session()
    if flow is not None and _establish_completed_flow_session(flow):
        return redirect("/", code=303)
    csrf = str(session.get("auth_csrf") or "")
    if flow is None or not _capability_ok(csrf):
        return redirect(url_for("auth_page"), code=303)
    return render_template_string(
        SCAN_PAGE,
        csrf=csrf,
        qr_url=url_for("auth_qr"),
        status_page_url=url_for("auth_status_page"),
        cancel_page_url=url_for("auth_cancel_page"),
    )


@app.get("/auth/status-error")
def auth_status_error():
    # Deliberately static and session-neutral.  A late response from another
    # tab must never erase an authenticated cookie established by the winner.
    return AUTH_STATUS_ERROR_PAGE


@app.get("/auth/qr")
def auth_qr():
    flow = _pending_flow_for_session()
    if flow is None:
        abort(404)
    if flow.phase != "waiting":
        return jsonify(ok=False, status=flow.phase), 409
    try:
        status, headers, payload = _broker_request(
            flow,
            "GET",
            "/v1/auth/qr",
            response_limit=MAX_AUTH_QR_BYTES,
        )
    except RuntimeError:
        return jsonify(ok=False, status="loading"), 409
    if status != 200 or not str(headers.get("content-type") or "").startswith("image/png"):
        return jsonify(ok=False, status="loading"), 409
    return Response(
        payload,
        status=200,
        headers={
            "Content-Type": "image/png",
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
        },
    )


def _auth_csrf_matches() -> bool:
    expected = str(session.get("auth_csrf") or "")
    provided_header = str(request.headers.get("X-Kuaishou-Auth-CSRF") or "")
    provided_form = ""
    if request.mimetype == "application/x-www-form-urlencoded":
        content_length = request.content_length
        if content_length is None or content_length > 2048:
            return False
        csrf_values = request.form.getlist("csrf")
        if len(csrf_values) != 1 or set(request.form.keys()) != {"csrf"}:
            return False
        provided_form = str(csrf_values[0] or "")
    if provided_header and provided_form and not hmac.compare_digest(
        provided_header, provided_form
    ):
        return False
    provided = provided_header or provided_form
    return _capability_ok(expected) and _capability_ok(provided) and hmac.compare_digest(
        expected, provided
    )


@app.post("/auth/status")
def auth_status():
    if not _auth_csrf_matches():
        abort(403)
    flow = _pending_flow_for_session()
    if flow is None:
        return jsonify(ok=False, status="expired", error="扫码登录已过期，请重新扫码")
    with _pending_auth_lock:
        phase = flow.phase
        result = flow.result
        error = flow.error
    if phase in {"complete", "authenticated"}:
        with flow.completion_lock:
            try:
                with _pending_auth_lock:
                    current = _pending_auth.get(flow.flow_id)
                    if current is not flow or flow.phase not in {"complete", "authenticated"}:
                        return jsonify(ok=False, status="expired", error="扫码结果已经使用")
                    current_phase = flow.phase
                    current_result = flow.result
                if current_phase == "complete":
                    if not isinstance(current_result, dict):
                        raise ValueError("扫码身份结果无效")
                    claims = current_result.get("claims")
                    if not isinstance(claims, dict):
                        raise ValueError("扫码身份结果无效")
                    user = _upsert_user(claims)
                    erp_session = current_result.get("erp_session")
                    if erp_session is not None and not (
                        isinstance(erp_session, tuple)
                        and len(erp_session) == 2
                        and isinstance(erp_session[0], str)
                        and isinstance(erp_session[1], int)
                    ):
                        raise ValueError("ERP 授权绑定无效")
                    browser_session_value = _capability()
                    browser_session_expires_at = int(time.time()) + BROWSER_SESSION_TTL_SECONDS
                    with _pending_auth_lock:
                        if (
                            _pending_auth.get(flow.flow_id) is not flow
                            or flow.phase != "complete"
                        ):
                            return jsonify(
                                ok=False,
                                status="expired",
                                error="扫码结果已经使用",
                            )
                        flow.completed_user_id = user["user_id"]
                        flow.browser_session_value = browser_session_value
                        flow.browser_session_expires_at = browser_session_expires_at
                        flow.terminal_expires_at = int(time.time()) + 30
                        flow.result = None
                        flow.phase = "authenticated"
                user_id = flow.completed_user_id
                browser_session_value = flow.browser_session_value
                browser_session_expires_at = flow.browser_session_expires_at
                if (
                    not USER_ID_RE.fullmatch(user_id)
                    or not _capability_ok(browser_session_value)
                ):
                    raise ValueError("扫码登录完成状态无效")
                _establish_browser_session(
                    user_id,
                    session_capability=browser_session_value,
                    expires_at=browser_session_expires_at,
                )
                if current_phase == "complete":
                    try:
                        if erp_session is None:
                            _clear_erp_session(user_id)
                        else:
                            _store_erp_session(user_id, erp_session[0], erp_session[1])
                    except (RuntimeError, ValueError):
                        _revoke_browser_session(require_binding=False)
                        raise
            except (RuntimeError, ValueError) as exc:
                with _pending_auth_lock:
                    if _pending_auth.get(flow.flow_id) is flow:
                        flow.phase = "failed"
                        flow.result = None
                        flow.error = str(exc)[:240]
                session.clear()
                _schedule_terminate_broker(flow)
                return jsonify(ok=False, status="failed", error=str(exc))
        _schedule_terminate_broker(flow)
        return jsonify(ok=True, status="complete", redirect="/")
    if phase in {"failed", "claimed"}:
        if phase == "claimed":
            return jsonify(ok=True, status="pending", message="正在确认扫码身份…")
        with _pending_auth_lock:
            if _pending_auth.get(flow.flow_id) is not flow or flow.phase != "failed":
                return jsonify(ok=True, status="pending", message="正在确认扫码身份…")
            _pending_auth.pop(flow.flow_id, None)
        threading.Thread(target=_terminate_broker, args=(flow,), daemon=True).start()
        return jsonify(ok=False, status="failed", error=error or "钉钉扫码登录失败")
    if int(time.time()) > flow.expires_at:
        with _pending_auth_lock:
            if _pending_auth.get(flow.flow_id) is not flow or flow.phase != "waiting":
                return jsonify(ok=True, status="pending", message="正在确认扫码身份…")
            _pending_auth.pop(flow.flow_id, None)
        threading.Thread(target=_terminate_broker, args=(flow,), daemon=True).start()
        return jsonify(ok=False, status="expired", error="二维码已过期，请重新扫码")
    if flow.process.poll() is not None:
        if _finish_pending_flow(
            flow,
            expected_phase="waiting",
            result=None,
            error="本机扫码组件意外停止，请重试",
        ):
            return jsonify(ok=False, status="failed", error=flow.error)
        return jsonify(ok=True, status="pending", message="正在确认扫码身份…")
    message = "等待扫码"
    try:
        broker_status, _headers, broker_payload = _broker_request(
            flow,
            "GET",
            "/v1/auth/status",
            response_limit=MAX_AUTH_BROKER_JSON_BYTES,
        )
        if broker_status == 200:
            value = json.loads(broker_payload.decode("utf-8"))
            broker_phase = str(value.get("status") or "") if isinstance(value, dict) else ""
            message = {
                "loading": "正在生成二维码…",
                "qr_ready": "等待扫码",
                "scanned": "扫码成功，正在读取你的 ERP 身份…",
                "delivering": "身份已确认，正在进入独立空间…",
            }.get(broker_phase, message)
    except (RuntimeError, UnicodeError, json.JSONDecodeError):
        message = "正在等待本机扫码组件…"
    return jsonify(ok=True, status="pending", message=message)


@app.post("/auth/status-page")
def auth_status_page():
    if _current_user() is not None:
        return redirect("/", code=303)
    response = app.make_response(auth_status())
    payload = response.get_json(silent=True)
    if not isinstance(payload, dict):
        return redirect(url_for("auth_status_error"), code=303)
    status = str(payload.get("status") or "")
    if status == "complete":
        return redirect("/", code=303)
    if status == "pending":
        return redirect(url_for("auth_scan"), code=303)
    page_error = str(payload.get("error") or "钉钉扫码登录失败")[:240]
    if _capacity_limit_from_error(page_error) is not None:
        return _capacity_response(page_error)
    # Stale tabs recover through the idempotent login entry and reuse this
    # browser's active QR flow instead of creating competing scan windows.
    return redirect(
        url_for("auth_page", error=page_error),
        code=303,
    )


@app.post("/auth/cancel")
def auth_cancel():
    if not _auth_csrf_matches():
        abort(403)
    _cancel_current_auth_flow()
    return "", 204


def _cancel_current_auth_flow() -> bool:
    flow = _pending_flow_for_session()
    cancelled = False
    if flow is not None:
        with _pending_auth_lock:
            if _pending_auth.get(flow.flow_id) is flow and flow.phase == "waiting":
                cancelled = True
                flow.phase = "cancelled"
                _pending_auth.pop(flow.flow_id, None)
    if cancelled:
        session.clear()
        threading.Thread(target=_terminate_broker, args=(flow,), daemon=True).start()
    return cancelled


@app.post("/auth/cancel-page")
def auth_cancel_page():
    if _current_user() is not None:
        return redirect("/", code=303)
    if not _auth_csrf_matches():
        abort(403)
    flow = _pending_flow_for_session()
    if flow is not None and _establish_completed_flow_session(flow):
        return redirect("/", code=303)
    if _cancel_current_auth_flow():
        return redirect(url_for("auth_page"), code=303)
    return redirect(url_for("auth_page"), code=303)


@app.get("/auth/callback")
def auth_callback():
    # Retired remote assertion endpoint: never exchange an authorization code.
    abort(404)


@app.post("/logout")
def logout():
    _teardown_current_browser_session()
    session.clear()
    return redirect(url_for("auth_page"), code=303)


_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

LARGE_DOWNLOAD_PROXY_TIMEOUT_SECONDS = 2 * 60 * 60


def _proxy_timeout_seconds(target: str, ordinary_timeout: float = 600.0) -> float:
    normalized_target = str(target or "").split("?", 1)[0].lower()
    if normalized_target == "/queue/batch-download/file" or re.fullmatch(
        r"/queue/[0-9a-f]{32}/client-save/[0-9a-f]{32}/(?:bundle|file/[0-9]+)",
        normalized_target,
    ):
        return float(LARGE_DOWNLOAD_PROXY_TIMEOUT_SECONDS)
    return max(60.0, float(ordinary_timeout))


def _proxy_request_headers(backend: TenantBackend) -> dict[str, str]:
    headers = {}
    for name, value in request.headers.items():
        lower = name.lower()
        if lower in _HOP_BY_HOP_HEADERS or lower in {
            "host",
            "cookie",
            "forwarded",
            "x-forwarded-for",
            "x-forwarded-host",
            "x-forwarded-proto",
            "x-kuaishou-gateway-token",
            "x-kuaishou-server-control",
            "x-kuaishou-erp-bearer",
            "x-kuaishou-erp-expires-at",
            "x-kuaishou-erp-user-id",
        }:
            continue
        headers[name] = value
    cookie_parts = []
    gateway_cookie = app.config["SESSION_COOKIE_NAME"]
    for part in str(request.headers.get("Cookie") or "").split(";"):
        name = part.strip().split("=", 1)[0]
        if name == backend.cookie_name and name != gateway_cookie:
            cookie_parts.append(part.strip())
    if cookie_parts:
        headers["Cookie"] = "; ".join(cookie_parts)
    headers["Host"] = f"127.0.0.1:{backend.port}"
    headers["X-Kuaishou-Gateway-Token"] = backend.gateway_token
    headers["X-Forwarded-Proto"] = "https" if PUBLIC_HTTPS else "http"
    return headers


def _proxy_response(backend: TenantBackend, target: str) -> Response:
    # Large ZIP responses are streamed and do not consume a render slot.  A
    # two-hour upstream socket window prevents slow LAN clients and download
    # managers from being cut off by the ordinary ten-minute proxy timeout.
    proxy_timeout_seconds = _proxy_timeout_seconds(
        target,
        request.environ.get("werkzeug.server.shutdown_timeout", 600),
    )
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        backend.port,
        timeout=proxy_timeout_seconds,
    )
    headers = _proxy_request_headers(backend)
    body = request.stream if request.method not in {"GET", "HEAD"} else None
    try:
        connection.request(request.method, target, body=body, headers=headers)
        upstream = connection.getresponse()
    except (OSError, http.client.HTTPException) as exc:
        connection.close()
        raise RuntimeError("个人制作空间暂时不可用，请刷新重试") from exc

    response_headers: list[tuple[str, str]] = []
    total_header_bytes = 0
    for name, value in upstream.getheaders():
        lower = name.lower()
        if lower in _HOP_BY_HOP_HEADERS or lower == "server":
            continue
        total_header_bytes += len(name) + len(value)
        if total_header_bytes > MAX_PROXY_RESPONSE_HEADER_BYTES:
            upstream.close()
            connection.close()
            raise RuntimeError("个人制作空间响应头异常")
        if lower == "location":
            parsed = urlsplit(value)
            if parsed.hostname in {"127.0.0.1", "localhost"}:
                value = parsed.path or "/"
                if parsed.query:
                    value += "?" + parsed.query
        if lower == "set-cookie":
            cookie_name = value.split("=", 1)[0].strip()
            if cookie_name != backend.cookie_name:
                continue
        response_headers.append((name, value))

    @stream_with_context
    def generate():
        try:
            while True:
                chunk = upstream.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()
            connection.close()

    return Response(
        generate(),
        status=upstream.status,
        headers=response_headers,
        direct_passthrough=True,
    )


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
def proxy(path: str):
    if path.strip("/").lower().startswith("internal/"):
        abort(404)
    user = _current_user()
    if user is None:
        if request.method == "GET":
            return redirect(url_for("auth_page"), code=303)
        abort(401)
    try:
        backend = backend_manager.get(user)
        target = "/" + quote(path, safe="/!$&'()*+,;=:@-._~")
        if request.query_string:
            target += "?" + request.query_string.decode("latin-1")
        backend_manager.begin_proxy_request(backend)
        try:
            response = _proxy_response(backend, target)
        except BaseException:
            backend_manager.finish_proxy_request(backend)
            raise
        release_lock = threading.Lock()
        released = False

        def release_proxy_reservation() -> None:
            nonlocal released
            with release_lock:
                if released:
                    return
                released = True
            backend_manager.finish_proxy_request(backend)

        response.call_on_close(release_proxy_reservation)
        return response
    except RuntimeError as exc:
        if request.method == "GET" and _capacity_limit_from_error(str(exc)) is not None:
            return _capacity_response(str(exc))
        return jsonify(ok=False, error=str(exc)), 503


def shutdown() -> None:
    _stop_housekeeping()
    _stop_all_auth_flows()
    backend_manager.stop_all()


# BI LAN upload: existing-session, single-file capabilities.
from bi_lan_upload import install as _install_bi_lan_upload
_install_bi_lan_upload(globals())
