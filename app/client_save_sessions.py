"""Bounded local-save leases; EOF alone never confirms a disk save."""
import threading
import time
import uuid


class ClientSaveSessions:
    def __init__(self, acquire, release, *, ttl_seconds=7200, limit=4):
        self.acquire = acquire
        self.release = release
        self.ttl_seconds = ttl_seconds
        self.limit = limit
        self.lock = threading.RLock()
        self.sessions = {}

    def expire(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            expired = [key for key, value in self.sessions.items() if value['deadline'] <= now]
        for key in expired:
            self.finish(key, remove=True)

    def create(self, task_id, artifacts):
        self.expire()
        with self.lock:
            if sum(not value['released'] for value in self.sessions.values()) >= self.limit:
                raise ValueError('已有4个本机保存操作，请完成或取消后再试。')
            # Bound completed-session receipts too, without evicting live saves.
            completed = [key for key, value in self.sessions.items() if value['released']]
            for key in completed[:-16]:
                self.sessions.pop(key, None)
            key = uuid.uuid4().hex
            value = {
                'id': key, 'task_id': task_id, 'artifacts': artifacts,
                'deadline': time.monotonic() + self.ttl_seconds,
                'guard': self.acquire([task_id]), 'released': False,
                'confirmed': False, 'transferred': set(), 'bundles': [],
            }
            self.sessions[key] = value
            return value

    def get(self, key, task_id):
        self.expire()
        with self.lock:
            value = self.sessions.get(key)
            if value is None or value['task_id'] != task_id:
                raise ValueError('保存操作已失效，请重新保存或确认本机文件后清理。')
            return value

    def transferred(self, key, indices):
        with self.lock:
            value = self.sessions.get(key)
            if value and not value['released']:
                value['transferred'].update(indices)

    def finish(self, key, *, confirmed=False, remove=False):
        guard = None
        with self.lock:
            value = self.sessions.get(key)
            if value:
                if not value['released']:
                    value['released'] = True
                    guard = value['guard']
                value['confirmed'] = value['confirmed'] or confirmed
                if remove:
                    self.sessions.pop(key, None)
        if guard is not None:
            self.release(guard)

