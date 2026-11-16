"""8020 renderer checkpoint. Only instantiated by the existing queue worker.

No network, billing, worker spawning or cleanup. Public metadata lives on the
original queue row; a write-ahead file journal reconciles publication crashes.
"""
import hashlib
import copy
import json
import os
import uuid
from pathlib import Path
from pathlib import PureWindowsPath
import re

# Queue cold-start recovery keys off this persisted schema version.
VERSION = '8020-autofill-20260927.1'
RETRY_POLICY = 'first-pass-3-or-4-of-5-new-batch-20260928.2'


class FillCheckpointError(RuntimeError):
    pass


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1048576), b''):
            result.update(chunk)
    return result.hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def material_signature_identity(value):
    """Keep the pre-migration material identity after the operator D->E move.

    Only the exact 8020 data tree and a valid tenant ID are mapped. This does
    not authorize file access: ordinary tenant manifest/ownership validation
    still runs first. Never generalize this to arbitrary E: directories.
    """
    raw = str(value)
    path = PureWindowsPath(raw)
    old = PureWindowsPath('D:/快手自动化GPU服务器8020数据/多用户/users')
    new = PureWindowsPath('E:/快手自动化GPU服务器8020数据/多用户/users')
    try:
        relative = path.relative_to(new)
    except ValueError:
        return raw
    if not relative.parts or not re.fullmatch('[0-9a-f]{32}', relative.parts[0]) or '..' in relative.parts:
        return raw
    return str(old / relative)


class RenderCheckpoint:
    def __init__(self, path, run_id, owner, target, signature, output_root, notify):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.path.with_suffix('.lock').open('a+b')
        try:
            self.lock.seek(0, os.SEEK_END)
            if self.lock.tell() == 0:
                self.lock.write(b'0'); self.lock.flush()
            self.lock.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise FillCheckpointError('同一任务已有制作流程，禁止重复启动')
        self.notify = notify
        self.root = Path(output_root).resolve()
        identity = dict(run_id=run_id, owner=owner, target=int(target), signature=signature)
        try:
            if self.path.exists():
                self.data = json.loads(self.path.read_text(encoding='utf-8'))
                if any(self.data.get(k) != v for k, v in identity.items()):
                    raise FillCheckpointError('原任务素材或设置与检查点不一致，禁止重做')
            else:
                if self.root.exists() and any(self.root.glob('V17裂变成片_第*条.mp4')):
                    raise FillCheckpointError('存在历史成片但缺少恢复凭证，禁止覆盖或重新制作')
                self.data = dict(identity, version=VERSION, round=0, error='', state='rendering',
                                 snapshot={}, files=[], pending=[], similarity_batch_start=0)
                self._write()
            # Old checkpoints keep their stricter current-round boundary until
            # the next actual retry. Never infer a boundary from a file count.
            self.similarity_start
            self._reconcile()
        except BaseException:
            self.close()
            raise

    @property
    def round(self):
        return self.data['round']

    @property
    def similarity_start(self):
        start = self.data.get('similarity_batch_start', 0)
        if (type(start) is not int or not 0 <= start <= len(self.data['files'])
                or (self.round == 0 and start != 0)):
            raise FillCheckpointError('补做批次边界无效，停止等待核对')
        return start

    def _write(self):
        temp = self.path.with_name(self.path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('x', encoding='utf-8') as f:
                f.write(canonical(self.data)); f.flush(); os.fsync(f.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def _owned(self, path):
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.root):
            raise FillCheckpointError('检查点包含原任务目录之外的成片')
        return resolved

    def _reconcile(self):
        for transfer in self.data['pending']:
            source, target = self._owned(transfer['source']), self._owned(transfer['target'])
            if target.exists():
                if digest(target) != transfer['sha256']:
                    raise FillCheckpointError('已有成片与发布凭证不符，禁止覆盖')
            else:
                if not source.is_file() or digest(source) != transfer['sha256']:
                    raise FillCheckpointError('已验收成片发布文件丢失，保留记录等待核对')
                # Link creates a new name atomically and refuses replacement.
                os.link(source, target)
            source.unlink(missing_ok=True)
        self.data['pending'] = []
        for item in self.data['files']:
            path = self._owned(item['path'])
            if not path.is_file() or digest(path) != item['sha256']:
                raise FillCheckpointError('已成功成片丢失或改变，禁止从头重做')
        self._write()
        self.notify(self.public())

    def restore(self):
        value = dict(self.data['snapshot'])
        if value:
            value['accepted_paths'] = [self._owned(p) for p in value['accepted_paths']]
        return value

    def verified_snapshot(self):
        """Exception publication must never count an unpublished candidate."""
        snapshot = self.restore()
        if not snapshot:
            return {}
        count = 0
        for item in self.data['files']:
            try:
                if digest(self._owned(item['path'])) != item['sha256']:
                    break
            except OSError:
                break
            count += 1
        for key in ('accepted_paths', 'accepted_durations', 'accepted_hashes', 'variant_plans'):
            snapshot[key] = snapshot[key][:count]
        snapshot['pair_results'] = [pair for pair in snapshot['pair_results']
                                    if pair['first'] <= count and pair['second'] <= count]
        return snapshot

    def publish(self, candidate, final, report, final_report, snapshot):
        candidate, final = self._owned(candidate), self._owned(final)
        if final.exists():
            raise FillCheckpointError('禁止覆盖已经生成的成片')
        sha = digest(candidate)
        if any(item['sha256'] == sha for item in self.data['files']):
            raise FillCheckpointError('禁止复制已有成片凑数')
        transfers = [dict(source=str(candidate), target=str(final), sha256=sha)]
        if Path(report).is_file():
            report, final_report = self._owned(report), self._owned(final_report)
            if final_report.exists():
                raise FillCheckpointError('禁止覆盖已有验收报告')
            transfers.append(dict(source=str(report), target=str(final_report), sha256=digest(report)))
        stored = dict(snapshot)
        stored['accepted_paths'] = [str(p) for p in stored['accepted_paths']]
        if len(stored['accepted_paths']) != len(self.data['files']) + 1:
            raise FillCheckpointError('成片检查点数量不连续')
        previous = copy.deepcopy(self.data)
        self.data['snapshot'] = stored
        self.data['files'].append(dict(path=str(final), sha256=sha))
        self.data['pending'] = transfers
        try:
            self._write()  # intent durable BEFORE publishing final names
        except Exception as error:
            self.data = previous
            raise FillCheckpointError('验收发布凭证写入失败，禁止继续制作') from error
        self._reconcile()

    def retry(self, error, snapshot, validate_materials):
        if isinstance(error, FillCheckpointError):
            raise error
        self.data['error'] = str(error)
        if self.round >= 3:
            self.data['state'] = 'fill_failed'
            self.data['error'] = '自动补做已达 3 轮上限：' + str(error)
            self._write(); self.notify(self.public())
            return False
        try:
            validate_materials()
        except Exception as missing:
            self.data['state'] = 'fill_failed'
            self.data['error'] = '原素材缺失或不可读取：' + str(missing)
            self._write(); self.notify(self.public())
            raise FillCheckpointError(self.data['error']) from missing
        # Publish errors cannot become render retries; ensure accepted disk
        # files and ledger agree before taking another round.
        self._reconcile()
        stored = dict(snapshot)
        stored['accepted_paths'] = [str(p) for p in stored['accepted_paths']]
        if len(stored['accepted_paths']) != len(self.data['files']):
            raise FillCheckpointError('成片与检查点不一致，停止补做等待核对')
        first_done = (len(self.data['files']) if self.round == 0
                      else self.data.get('initial_pass_completed'))
        if self.round == 0:
            self.data['initial_pass_completed'] = first_done
        if self.data['target'] != 5 or type(first_done) is not int or first_done not in (3, 4):
            self.data['state'] = 'fill_failed'
            reason = ('历史检查点缺少首轮完成数量，不能确认补做资格'
                      if first_done is None else
                      f"首轮完成 {first_done}/{self.data['target']}，仅首轮完成 3/5 或 4/5 才自动补做")
            self.data['error'] = reason + '；已保留合格成片，不再自动补做：' + str(error)
            self._write(); self.notify(self.public())
            return False
        self.data['snapshot'] = stored
        self.data['round'] += 1
        # A new attempt group compares only outputs accepted in that group.
        # Keep the cumulative ledger/hashes: IDs, preservation and exact-byte
        # anti-copy checks still apply across the entire original task.
        self.data['similarity_batch_start'] = len(self.data['files'])
        self.data['state'] = 'rendering'
        self._write(); self.notify(self.public())
        return True

    def rendered(self):
        if len(self.data['files']) != self.data['target']:
            raise FillCheckpointError('数量不足，不能返回整批完成')
        self.data['state'] = 'completed'
        self.data['error'] = ''
        self._write(); self.notify(self.public())

    def public(self):
        total, done = self.data['target'], len(self.data['files'])
        state = self.data['state']
        return dict(version=VERSION, task_id=self.data['run_id'], target_count=total,
                    completed_count=done, remaining_count=total-done, retry_round=self.round,
                    max_retry_rounds=3, retry_authority='local_server', state=state,
                    retry_policy=RETRY_POLICY,
                    initial_pass_completed=self.data.get('initial_pass_completed'),
                    similarity_scope='within_retry_batch',
                    similarity_batch_start=self.similarity_start,
                    failure_reason=self.data['error'],
                    outputs=[dict(output_id=str(uuid.uuid5(uuid.NAMESPACE_URL,
                                  f"8020:{self.data['owner']}:{self.data['run_id']}:{i}")),
                                  index=i, filename=Path(item['path']).name,
                                  download_url=f"/download-fission/{self.data['run_id']}/{i}")
                             for i, item in enumerate(self.data['files'], 1)],
                    message=(f'已完成 {done}/{total}，正在使用原素材自动补做剩余 {total-done} 条；本轮按新批次制作，仅本轮内部查重。'
                             if self.round and state == 'rendering' else
                             f'已完成 {done}/{total}' + ('，自动补做失败：' + self.data['error'] if state == 'fill_failed' else '')))

    def close(self):
        if not self.lock.closed:
            self.lock.close()
