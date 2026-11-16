"""Crash-safe host-wide renderer concurrency slots.

One byte-range lock represents each configured physical render slot on the host.
Operating-system locks are released automatically if a backend crashes, while
an in-process ownership set prevents two worker threads from re-entering the
same OS byte lock on platforms whose process-scoped lock semantics allow it.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows path
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX path
    msvcrt = None


class RenderSlotLease:
    """One held byte-range lock.  Closing it releases the slot."""

    def __init__(self, slot: int, release_callback) -> None:
        self.slot = int(slot)
        self._release_callback = release_callback
        self._close_lock = threading.Lock()
        self._active = True

    @property
    def active(self) -> bool:
        with self._close_lock:
            return self._active

    def close(self) -> None:
        with self._close_lock:
            if not self._active:
                return
            release_callback = self._release_callback
            release_callback(self.slot)
            self._active = False
            self._release_callback = None

    def __enter__(self) -> "RenderSlotLease":
        return self

    def __exit__(self, _kind, _value, _traceback) -> None:
        self.close()


class GlobalRenderSlotPool:
    """Non-blocking inter-process slot pool backed by a validated plain file."""

    def __init__(self, path: str | os.PathLike[str], slots: int = 2, *, allowed_slots=None) -> None:
        self.path = Path(path).expanduser().resolve()
        self.slots = max(1, min(8, int(slots)))
        self.allowed_slots = tuple(range(self.slots)) if allowed_slots is None else tuple(allowed_slots)
        if (not self.allowed_slots or len(set(self.allowed_slots)) != len(self.allowed_slots)
                or any(type(slot) is not int or not 0 <= slot < self.slots for slot in self.allowed_slots)):
            raise ValueError("allowed_slots must be a nonempty unique subset of physical slots")
        self._local_lock = threading.Lock()
        self._held_slots: set[int] = set()
        self._descriptor = -1

    def _release_slot(self, slot: int) -> None:
        with self._local_lock:
            slot = int(slot)
            if slot not in self._held_slots:
                return
            descriptor = self._descriptor
            if descriptor < 0:
                raise RuntimeError("渲染并发锁文件已经关闭")
            os.lseek(descriptor, slot, os.SEEK_SET)
            if fcntl is not None:
                fcntl.lockf(descriptor, fcntl.LOCK_UN, 1)
            elif msvcrt is not None:
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - unsupported interpreter
                raise RuntimeError("当前系统不支持跨进程渲染并发锁")
            self._held_slots.remove(slot)

    def _open_slot_file_locked(self) -> int:
        if self._descriptor >= 0:
            return self._descriptor
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise RuntimeError("渲染并发锁文件不能是符号链接")
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.fstat(descriptor).st_size < self.slots:
                os.ftruncate(descriptor, self.slots)
                os.fsync(descriptor)
            self._descriptor = descriptor
            return self._descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def close(self) -> None:
        """Close the shared descriptor after every outstanding lease is closed."""

        with self._local_lock:
            if self._held_slots:
                raise RuntimeError("仍有渲染槽正在使用，不能关闭并发锁池")
            descriptor = self._descriptor
            if descriptor < 0:
                return
            self._descriptor = -1
            os.close(descriptor)

    def __enter__(self) -> "GlobalRenderSlotPool":
        return self

    def __exit__(self, _kind, _value, _traceback) -> None:
        self.close()

    @staticmethod
    def _try_lock(descriptor: int, slot: int) -> bool:
        os.lseek(descriptor, slot, os.SEEK_SET)
        try:
            if fcntl is not None:
                fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB, 1)
            elif msvcrt is not None:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - unsupported interpreter
                raise RuntimeError("当前系统不支持跨进程渲染并发锁")
            return True
        except (BlockingIOError, OSError):
            return False

    def try_acquire(self, fairness_key: str = "") -> RenderSlotLease | None:
        """Try once and return immediately when all configured slots are busy."""

        width = len(self.allowed_slots)
        start = sum(str(fairness_key).encode("utf-8")) % width
        with self._local_lock:
            descriptor = self._open_slot_file_locked()
            for offset in range(width):
                slot = self.allowed_slots[(start + offset) % width]
                if slot in self._held_slots:
                    continue
                if self._try_lock(descriptor, slot):
                    self._held_slots.add(slot)
                    return RenderSlotLease(slot, self._release_slot)
        return None

    def acquire(
        self,
        fairness_key: str = "",
        *,
        timeout: float | None = None,
        poll_interval: float = 0.25,
    ) -> RenderSlotLease | None:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            lease = self.try_acquire(fairness_key)
            if lease is not None:
                return lease
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(max(0.05, min(2.0, poll_interval)))


_FIFO_SETUP_LOCKS: dict[str, threading.Lock] = {}
_FIFO_SETUP_LOCKS_GUARD = threading.Lock()
_FIFO_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?$"
)


def _fifo_setup_lock(database_path: Path) -> threading.Lock:
    normalized = str(database_path.resolve()).casefold()
    with _FIFO_SETUP_LOCKS_GUARD:
        lock = _FIFO_SETUP_LOCKS.get(normalized)
        if lock is None:
            lock = threading.Lock()
            _FIFO_SETUP_LOCKS[normalized] = lock
        return lock


def _fifo_text(value, field_name: str, *, limit: int = 256) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if len(normalized) > int(limit):
        raise ValueError(f"{field_name} is too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{field_name} contains control characters")
    return normalized


def _fifo_created_at(value) -> str:
    """Normalize queue timestamps so SQLite text ordering is chronological."""

    normalized = _fifo_text(value, "created_at", limit=32)
    match = _FIFO_TIMESTAMP_RE.fullmatch(normalized)
    if match is None:
        raise ValueError("created_at has an invalid format")
    try:
        time.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("created_at has an invalid value") from exc
    fraction = (match.group(2) or "").ljust(6, "0")
    return f"{match.group(1)}.{fraction}"


class GlobalFifoRenderCoordinator:
    """Serialize cross-tenant FIFO admission around the existing slot pool.

    Only scheduling metadata is shared.  Task configuration, materials,
    results, customer identity, and billing state remain in each tenant's own
    SQLite database.  One row per tenant advertises that tenant's current
    local FIFO head.  The gateway separately marks a backend ready after it is
    healthy.  ERP token lifetime is deliberately not part of render admission:
    an expired token may defer ERP reporting, but it must never stop an already
    authenticated tenant's queued video work.

    ``try_admit_and_claim`` keeps one short ``BEGIN IMMEDIATE`` transaction
    open while it takes a non-blocking physical slot and invokes the caller's
    exact-task claim callback.  This prevents two backend processes from
    observing the same global head and admitting a later task around it.  The
    callback must perform only the tenant-local ``waiting -> running`` SQLite
    transition and must return ``None`` when that exact row is no longer
    waiting; rendering and every external side effect happen after return.
    """

    def __init__(self, database_path, slot_pool: GlobalRenderSlotPool) -> None:
        raw_path = Path(database_path).expanduser()
        if raw_path.exists() and raw_path.is_symlink():
            raise RuntimeError("全局 FIFO 数据库不能是符号链接")
        self.database_path = raw_path.resolve()
        if not isinstance(slot_pool, GlobalRenderSlotPool):
            raise TypeError("slot_pool must be a GlobalRenderSlotPool")
        self.slot_pool = slot_pool
        self._setup_lock = _fifo_setup_lock(self.database_path)
        self._ready = False

    def _connect(self, timeout_seconds: float = 10.0) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        if self.database_path.exists() and self.database_path.is_symlink():
            raise RuntimeError("全局 FIFO 数据库不能是符号链接")
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
    def _enable_wal(connection: sqlite3.Connection, timeout_seconds=10.0) -> None:
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while True:
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def ensure_ready(self) -> None:
        if self._ready:
            return
        with self._setup_lock:
            if self._ready:
                return
            with self._connect() as connection:
                self._enable_wal(connection)
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS tenant_readiness (
                        owner_user_id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        backend_instance_id TEXT NOT NULL DEFAULT '',
                        ready INTEGER NOT NULL DEFAULT 0
                            CHECK (ready IN (0, 1)),
                        token_expires_at INTEGER NOT NULL DEFAULT 0,
                        changed_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS tenant_queue_heads (
                        owner_user_id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        stable_key TEXT NOT NULL,
                        local_sequence INTEGER NOT NULL,
                        changed_at REAL NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        idx_tenant_queue_heads_task
                        ON tenant_queue_heads(owner_user_id, tenant_id, task_id);
                    CREATE INDEX IF NOT EXISTS idx_tenant_queue_heads_fifo
                        ON tenant_queue_heads(created_at, stable_key,
                                              owner_user_id, task_id);
                    COMMIT;
                    """
                )
            self._ready = True

    def mark_all_offline(self) -> int:
        """Invalidate readiness left by a previous gateway process."""

        self.ensure_ready()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tenant_readiness "
                "SET ready=0, backend_instance_id='', changed_at=? "
                "WHERE ready!=0 OR backend_instance_id!=''",
                (time.time(),),
            )
            return int(cursor.rowcount or 0)

    def mark_ready(
        self,
        owner_user_id,
        tenant_id,
        backend_instance_id,
        *,
        token_expires_at=0,
    ) -> None:
        """Publish one healthy backend generation as globally runnable.

        ``token_expires_at`` remains persisted for compatibility and ERP relay
        diagnostics only.  It is intentionally scheduling-neutral: rendering
        continues after token expiry and ERP reporting can be retried after a
        user refreshes the token.
        """

        owner_user_id = _fifo_text(owner_user_id, "owner_user_id")
        tenant_id = _fifo_text(tenant_id, "tenant_id")
        backend_instance_id = _fifo_text(
            backend_instance_id, "backend_instance_id"
        )
        try:
            token_expires_at = int(token_expires_at or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("token_expires_at is invalid") from exc
        if token_expires_at < 0:
            raise ValueError("token_expires_at is invalid")
        self.ensure_ready()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tenant_readiness (
                    owner_user_id, tenant_id, backend_instance_id,
                    ready, token_expires_at, changed_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(owner_user_id) DO UPDATE SET
                    tenant_id=excluded.tenant_id,
                    backend_instance_id=excluded.backend_instance_id,
                    ready=1,
                    token_expires_at=excluded.token_expires_at,
                    changed_at=excluded.changed_at
                """,
                (
                    owner_user_id,
                    tenant_id,
                    backend_instance_id,
                    token_expires_at,
                    time.time(),
                ),
            )

    def mark_offline(self, owner_user_id, *, backend_instance_id=None) -> bool:
        """Mark an owner offline without letting an old backend fence a new one."""

        owner_user_id = _fifo_text(owner_user_id, "owner_user_id")
        parameters: list[object] = [time.time(), owner_user_id]
        instance_clause = ""
        if backend_instance_id is not None:
            backend_instance_id = _fifo_text(
                backend_instance_id, "backend_instance_id"
            )
            instance_clause = " AND backend_instance_id=?"
            parameters.append(backend_instance_id)
        self.ensure_ready()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tenant_readiness SET ready=0, changed_at=? "
                f"WHERE owner_user_id=?{instance_clause}",
                tuple(parameters),
            )
            return int(cursor.rowcount or 0) == 1

    def publish_head(
        self,
        owner_user_id,
        tenant_id,
        task_id,
        created_at,
        *,
        local_sequence=0,
        stable_key=None,
    ) -> dict:
        """Publish one tenant's read-only local waiting head idempotently."""

        owner_user_id = _fifo_text(owner_user_id, "owner_user_id")
        tenant_id = _fifo_text(tenant_id, "tenant_id")
        task_id = _fifo_text(task_id, "task_id")
        created_at = _fifo_created_at(created_at)
        try:
            local_sequence = int(local_sequence)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("local_sequence is invalid") from exc
        if local_sequence < 0:
            raise ValueError("local_sequence is invalid")
        if stable_key is None:
            stable_key = (
                f"{local_sequence:020d}:{owner_user_id}:{tenant_id}:{task_id}"
            )
        stable_key = _fifo_text(stable_key, "stable_key", limit=1024)
        changed_at = time.time()
        self.ensure_ready()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tenant_queue_heads (
                    owner_user_id, tenant_id, task_id, created_at,
                    stable_key, local_sequence, changed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_user_id) DO UPDATE SET
                    tenant_id=excluded.tenant_id,
                    task_id=excluded.task_id,
                    created_at=excluded.created_at,
                    stable_key=excluded.stable_key,
                    local_sequence=excluded.local_sequence,
                    changed_at=excluded.changed_at
                """,
                (
                    owner_user_id,
                    tenant_id,
                    task_id,
                    created_at,
                    stable_key,
                    local_sequence,
                    changed_at,
                ),
            )
        return {
            "owner_user_id": owner_user_id,
            "tenant_id": tenant_id,
            "task_id": task_id,
            "created_at": created_at,
            "stable_key": stable_key,
            "local_sequence": local_sequence,
        }

    # ``set_head`` is a concise integration alias for gateway/web call sites.
    set_head = publish_head

    def clear_head(
        self,
        owner_user_id,
        *,
        tenant_id=None,
        expected_task_id=None,
    ) -> bool:
        """Clear only the caller's still-current advertised head."""

        owner_user_id = _fifo_text(owner_user_id, "owner_user_id")
        clauses = ["owner_user_id=?"]
        parameters: list[object] = [owner_user_id]
        if tenant_id is not None:
            clauses.append("tenant_id=?")
            parameters.append(_fifo_text(tenant_id, "tenant_id"))
        if expected_task_id is not None:
            clauses.append("task_id=?")
            parameters.append(_fifo_text(expected_task_id, "expected_task_id"))
        self.ensure_ready()
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM tenant_queue_heads WHERE " + " AND ".join(clauses),
                tuple(parameters),
            )
            return int(cursor.rowcount or 0) == 1

    def current_head(self, owner_user_id=None, *, now_epoch=None) -> dict | None:
        """Return one advertised head for diagnostics/tests without task data."""

        self.ensure_ready()
        if owner_user_id is not None:
            owner_user_id = _fifo_text(owner_user_id, "owner_user_id")
            query = "SELECT * FROM tenant_queue_heads WHERE owner_user_id=?"
            parameters = (owner_user_id,)
        else:
            # ``now_epoch`` is retained as a backwards-compatible diagnostic
            # argument.  ERP token expiry no longer participates in selecting
            # a runnable render head.
            query = (
                "SELECT h.* FROM tenant_queue_heads AS h "
                "JOIN tenant_readiness AS r "
                "ON r.owner_user_id=h.owner_user_id "
                "AND r.tenant_id=h.tenant_id "
                "WHERE r.ready=1 "
                "ORDER BY h.created_at ASC, h.stable_key ASC, "
                "h.owner_user_id ASC, h.task_id ASC LIMIT 1"
            )
            parameters = ()
        with self._connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        return dict(row) if row is not None else None

    def waiting_accounts_ahead(
        self,
        owner_user_id,
        tenant_id,
        task_id,
    ) -> int | None:
        """Return only the number of ready tenant heads ahead of this head.

        The caller receives no other tenant identity or task metadata.  ``None``
        means its advertised head is not currently present/ready.
        """

        owner_user_id = _fifo_text(owner_user_id, "owner_user_id")
        tenant_id = _fifo_text(tenant_id, "tenant_id")
        task_id = _fifo_text(task_id, "task_id")
        self.ensure_ready()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT h.owner_user_id, h.tenant_id, h.task_id
                FROM tenant_queue_heads AS h
                JOIN tenant_readiness AS r
                  ON r.owner_user_id=h.owner_user_id
                 AND r.tenant_id=h.tenant_id
                WHERE r.ready=1
                ORDER BY h.created_at ASC, h.stable_key ASC,
                         h.owner_user_id ASC, h.task_id ASC
                """
            ).fetchall()
        for position, row in enumerate(rows):
            if (
                row["owner_user_id"] == owner_user_id
                and row["tenant_id"] == tenant_id
                and row["task_id"] == task_id
            ):
                return position
        return None

    @staticmethod
    def _refresh_head_in_transaction(
        connection: sqlite3.Connection,
        owner_user_id: str,
        tenant_id: str,
        current_task_id: str,
        next_head_callback,
    ) -> None:
        """Replace a claimed/stale head before another tenant can be admitted."""

        next_head = next_head_callback()
        if next_head is None:
            connection.execute(
                "DELETE FROM tenant_queue_heads "
                "WHERE owner_user_id=? AND tenant_id=? AND task_id=?",
                (owner_user_id, tenant_id, current_task_id),
            )
            return
        if not isinstance(next_head, dict):
            raise TypeError("next_head_callback must return a mapping or None")
        returned_owner = str(next_head.get("owner_user_id") or "").strip()
        returned_tenant = str(next_head.get("tenant_id") or "").strip()
        if returned_owner and returned_owner != owner_user_id:
            raise ValueError("next waiting head owner does not match")
        if returned_tenant and returned_tenant != tenant_id:
            raise ValueError("next waiting head tenant does not match")
        task_id = _fifo_text(next_head.get("task_id"), "next task_id")
        created_at = _fifo_created_at(next_head.get("created_at"))
        try:
            local_sequence = int(
                next_head.get("local_sequence", next_head.get("sequence", 0))
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("next local_sequence is invalid") from exc
        if local_sequence < 0:
            raise ValueError("next local_sequence is invalid")
        stable_key = next_head.get(
            "stable_key", next_head.get("fifo_stable_key")
        )
        if stable_key is None:
            stable_key = (
                f"{local_sequence:020d}:"
                f"{owner_user_id}:{tenant_id}:{task_id}"
            )
        stable_key = _fifo_text(stable_key, "next stable_key", limit=1024)
        connection.execute(
            """
            INSERT INTO tenant_queue_heads (
                owner_user_id, tenant_id, task_id, created_at,
                stable_key, local_sequence, changed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner_user_id) DO UPDATE SET
                tenant_id=excluded.tenant_id,
                task_id=excluded.task_id,
                created_at=excluded.created_at,
                stable_key=excluded.stable_key,
                local_sequence=excluded.local_sequence,
                changed_at=excluded.changed_at
            """,
            (
                owner_user_id,
                tenant_id,
                task_id,
                created_at,
                stable_key,
                local_sequence,
                time.time(),
            ),
        )

    def try_admit_and_claim(
        self,
        owner_user_id,
        tenant_id,
        task_id,
        claim_callback,
        *,
        next_head_callback=None,
        fairness_key="",
        now_epoch=None,
    ) -> tuple[RenderSlotLease | None, object | None]:
        """Admit and exactly claim the earliest eligible cross-tenant head.

        Returns ``(lease, claimed_item)`` only after the tenant-local exact
        claim committed.  ``(None, None)`` means it was not this task's turn,
        no physical slot was free, or the advertised local row had already
        changed.  Every non-success path releases a newly acquired slot.
        """

        owner_user_id = _fifo_text(owner_user_id, "owner_user_id")
        tenant_id = _fifo_text(tenant_id, "tenant_id")
        task_id = _fifo_text(task_id, "task_id")
        if not callable(claim_callback):
            raise TypeError("claim_callback must be callable")
        if next_head_callback is not None and not callable(next_head_callback):
            raise TypeError("next_head_callback must be callable")
        # Kept in the public signature so staged/older gateway callers do not
        # break during a rolling update.  ERP token expiry only controls ERP
        # reporting; backend health (``ready``) controls render admission.
        self.ensure_ready()
        lease = None
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            head = connection.execute(
                """
                SELECT h.* FROM tenant_queue_heads AS h
                JOIN tenant_readiness AS r
                  ON r.owner_user_id=h.owner_user_id
                 AND r.tenant_id=h.tenant_id
                WHERE r.ready=1
                ORDER BY h.created_at ASC, h.stable_key ASC,
                         h.owner_user_id ASC, h.task_id ASC
                LIMIT 1
                """
            ).fetchone()
            if (
                head is None
                or head["owner_user_id"] != owner_user_id
                or head["tenant_id"] != tenant_id
                or head["task_id"] != task_id
            ):
                connection.execute("COMMIT")
                return None, None
            lease = self.slot_pool.try_acquire(fairness_key or owner_user_id)
            if lease is None:
                connection.execute("COMMIT")
                return None, None
            claimed = claim_callback(task_id)
            if claimed is None:
                if next_head_callback is None:
                    connection.execute(
                        "DELETE FROM tenant_queue_heads "
                        "WHERE owner_user_id=? AND tenant_id=? AND task_id=?",
                        (owner_user_id, tenant_id, task_id),
                    )
                else:
                    try:
                        self._refresh_head_in_transaction(
                            connection,
                            owner_user_id,
                            tenant_id,
                            task_id,
                            next_head_callback,
                        )
                    except Exception:
                        # Fail closed on a damaged/unreadable tenant queue.  A
                        # later successful publish refreshes this stale head;
                        # deleting it here could let a newer tenant jump ahead.
                        pass
                connection.execute("COMMIT")
                lease.close()
                return None, None
            if next_head_callback is None:
                connection.execute(
                    "DELETE FROM tenant_queue_heads "
                    "WHERE owner_user_id=? AND tenant_id=? AND task_id=?",
                    (owner_user_id, tenant_id, task_id),
                )
            else:
                try:
                    self._refresh_head_in_transaction(
                        connection,
                        owner_user_id,
                        tenant_id,
                        task_id,
                        next_head_callback,
                    )
                except Exception:
                    # The tenant-local exact claim already committed.  Keep
                    # the old head as a conservative barrier and let the next
                    # worker publish/refresh it; never expose a scheduling gap.
                    pass
            connection.execute("COMMIT")
            return lease, claimed
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if lease is not None:
                lease.close()
            raise
        finally:
            connection.close()
