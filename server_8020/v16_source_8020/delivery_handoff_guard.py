"""Durable handoff receipts. EOF, ERP reporting and elapsed age are not receipts."""
import hashlib
import json
import re
import time

KEY = 'cloud_delivery_handoff_v1'

def result_of(row):
    value = row.get('result', row.get('result_json'))
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
        return decoded if isinstance(decoded, dict) else {}
    except (ValueError, TypeError):
        return {}

def manifest(row):
    result = result_of(row)
    if not isinstance(result, dict):
        return None
    if result.get('kind') == 'single':
        names = ['V17排队成片.mp4']
    elif result.get('kind') == 'fission':
        outputs = result.get('outputs')
        if not isinstance(outputs, list) or not outputs:
            return None
        names = [x.get('name') if isinstance(x, dict) else None for x in outputs]
    else:
        return None
    if any(not isinstance(n, str) or not n.lower().endswith('.mp4') or '/' in n or '\\' in n for n in names):
        return None
    if len(set(names)) != len(names):
        return None
    identity = {k: row.get(k) for k in ('task_id','owner_user_id','tenant_id','submission_key','finished_at')}
    if any(not identity[k] for k in identity):
        return None
    identity['names'] = sorted(names)
    # Include immutable output descriptors. Later refill/replace invalidates receipt.
    identity['outputs'] = result.get('outputs', [])
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return digest, sorted(names)

def cleanup_allowed(row):
    try:
        row = dict(row)
        if row.get('status') != 'succeeded' or row.get('control_state','active') != 'active':
            return False
        current = manifest(row)
        receipt = result_of(row).get(KEY)
        return bool(current and isinstance(receipt,dict) and receipt.get('version') == 1
                    and receipt.get('kind') in ('published','client_saved')
                    and receipt.get('manifest') == current[0]
                    and isinstance(receipt.get('confirmed_at'),(int,float))
                    and 0 < receipt['confirmed_at'] <= time.time()
                    and receipt.get('receipt_id'))
    except (TypeError, ValueError, KeyError, OverflowError):
        return False

def record_handoff(connection, task_id, owner, tenant, submission_key, names, receipt_id, kind='published'):
    """Caller provides a current authenticated owner; transaction pins exact row."""
    if kind not in ('published','client_saved') or not re.fullmatch(r'[A-Za-z0-9:_-]{16,160}',str(receipt_id)):
        raise ValueError('invalid handoff receipt')
    connection.execute('BEGIN IMMEDIATE')
    try:
        row = connection.execute('SELECT * FROM task_queue WHERE task_id=? AND owner_user_id=? AND tenant_id=? AND submission_key=?',
                                 (task_id,owner,tenant,submission_key)).fetchone()
        if row is None:
            raise ValueError('task identity does not match')
        row = dict(row)
        current = manifest(row)
        if row['status'] != 'succeeded' or row.get('control_state','active') != 'active' or row.get('delivery_expired_at') or not current:
            raise ValueError('task is not a complete retained delivery')
        if not isinstance(names,list) or len(names)!=len(set(names)) or sorted(names)!=current[1]:
            raise ValueError('incomplete delivery manifest')
        result = result_of(row)
        if cleanup_allowed(row):
            connection.commit()
            return True
        result[KEY] = {'version':1,'kind':kind,'manifest':current[0],'receipt_id':str(receipt_id),'confirmed_at':time.time()}
        connection.execute('UPDATE task_queue SET result_json=? WHERE task_id=?',
                           (json.dumps(result,ensure_ascii=False,separators=(',',':')),task_id))
        connection.commit()
        return True
    except BaseException:
        connection.rollback()
        raise

def mount(g):
    """Use existing authenticated tenant routing and native CSRF, never identity headers."""
    from flask import request, jsonify
    import sqlite3
    app = g['app']
    @app.post('/queue/confirm-cloud-delivery')
    def confirm_cloud_delivery():
        error = g['_task_draft_csrf_error']()
        if error is not None:
            return error
        if not g.get('TENANT_BACKEND_MODE') or not g.get('TENANT_USER_ID') or not g.get('TENANT_ID'):
            return jsonify(ok=False, message='需要已登录的原任务账号'),403
        try:
            names = json.loads(request.form.get('names','[]'))
            with sqlite3.connect(g['_task_queue_database_path'](),timeout=2) as c:
                c.row_factory = sqlite3.Row
                record_handoff(c, request.form.get('task_id'),g['TENANT_USER_ID'],g['TENANT_ID'],
                               request.form.get('submission_key'),names,request.form.get('receipt_id'))
        except (ValueError, TypeError):
            return jsonify(ok=False,message='原任务或完整成片清单不匹配，继续保留文件'),409
        except (OSError,sqlite3.Error):
            return jsonify(ok=False,message='交接确认暂未保存，继续保留文件'),503
        return jsonify(ok=True,confirmed=True)
