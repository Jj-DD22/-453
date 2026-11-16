"""Narrow, one-use LAN upload capabilities for the existing tenant gateway.

Install with install(globals()) in multiuser_gateway.py before app.run().
No new login mechanism: only an existing authenticated session may issue a
capability, after its worker validates CSRF and draft ownership. The capability
authorizes exactly one named file in one existing draft/pool, for 90 seconds.
"""
import hashlib
import http.client
import json
import re
import secrets
import threading
import time
from urllib.parse import urlencode

BI_ORIGIN = os.environ.get('V17_BI_ORIGIN', '').strip()
MAX_BYTES = 500 * 1024 * 1024
POOLS = ('开头视频', '核心素材', '打乱素材池', '贴纸素材池', '配音音频', '参考音色', '背景音乐')
AUDIO_POOLS = ('配音音频', '参考音色', '背景音乐')


class Tickets:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.entries = {}
        self.lock = threading.Lock()

    def issue(self, value):
        with self.lock:
            now = self.clock()
            self.entries = {k: v for k, v in self.entries.items() if v[0] > now}
            if len(self.entries) >= 1000 or sum(v[1]['user_id'] == value['user_id'] for v in self.entries.values()) >= 20:
                raise ValueError('too many pending uploads')
            token = secrets.token_urlsafe(32)
            self.entries[hashlib.sha256(token.encode()).hexdigest()] = (now + 90, value)
            return token

    def take(self, token):
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            return None
        with self.lock:
            item = self.entries.pop(hashlib.sha256(token.encode()).hexdigest(), None)
            return item[1] if item and item[0] > self.clock() else None


def validate_file(fields):
    draft, pool, name = (str(fields.get(k, '')) for k in ('draft_id', 'pool', 'name'))
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', draft) or pool not in POOLS:
        raise ValueError('invalid draft or pool')
    if not name or len(name) > 255 or any(c in name for c in '/\\\r\n\x00"'):
        raise ValueError('invalid filename')
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if pool in AUDIO_POOLS:
        allowed_exts = ('mp3', 'wav', 'm4a', 'aac', 'flac', 'ogg')
    elif pool == '贴纸素材池':
        allowed_exts = ('mp4', 'mov', 'avi', 'mkv', 'png', 'gif')
    elif pool == '开头视频':
        allowed_exts = ('mp4', 'mov')
    else:
        allowed_exts = ('mp4', 'mov', 'avi', 'mkv')
    if ext not in allowed_exts:
        raise ValueError('invalid extension')
    size = int(fields.get('size', 0))
    if not 0 < size <= MAX_BYTES:
        raise ValueError('invalid size')
    csrf = str(fields.get('csrf_token', ''))
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,256}', csrf):
        raise ValueError('invalid csrf')
    return dict(draft_id=draft, pool=pool, name=name, size=size, csrf_token=csrf)


def multipart_parts(value):
    boundary = 'bi-lan-' + secrets.token_hex(16)
    prefix = b''
    for name in ('csrf_token', 'draft_id', 'pool'):
        prefix += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value[name]}\r\n').encode()
    prefix += (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{value["name"]}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
    suffix = f'\r\n--{boundary}--\r\n'.encode()
    return boundary, prefix, suffix


def install(g):
    from flask import Response, request, jsonify
    app = g['app']
    tickets = Tickets()

    @app.route('/bi-lan-upload/status', methods=['GET'], provide_automatic_options=False)
    def bi_lan_status():
        return jsonify(ok=True, protocol=2, direct=True)

    def worker_json(backend, fields):
        connection = http.client.HTTPConnection('127.0.0.1', backend.port, timeout=15)
        try:
            connection.request('POST', '/task-drafts/snapshot', urlencode(fields).encode(), {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Accept': 'application/json', 'X-Requested-With': 'fetch',
                'X-Kuaishou-Gateway-Token': backend.gateway_token,
            })
            response = connection.getresponse()
            body = response.read(1024 * 1024 + 1)
            return response.status == 200 and len(body) <= 1024 * 1024 and json.loads(body).get('ok') is True
        finally:
            connection.close()

    # Only the existing HTTPS BI origin can use these two browser-facing routes.
    # Cookies are never shared cross-origin; uploads require a one-file ticket.
    @app.route('/bi-lan-upload/status', methods=['OPTIONS'])
    @app.route('/bi-lan-upload/file', methods=['OPTIONS'])
    def bi_lan_options():
        if request.headers.get('Origin') != BI_ORIGIN:
            return jsonify(ok=False), 403
        method=request.headers.get('Access-Control-Request-Method','')
        allowed='GET' if request.path.endswith('/status') else 'POST'
        headers={x.strip().lower() for x in request.headers.get('Access-Control-Request-Headers','').split(',') if x.strip()}
        if method != allowed or not headers <= {'content-type','x-bi-upload-ticket'}:
            return jsonify(ok=False), 403
        return Response(status=204)

    @app.after_request
    def bi_lan_cors(response):
        if request.path in ('/bi-lan-upload/status','/bi-lan-upload/file') and request.headers.get('Origin') == BI_ORIGIN:
            response.headers['Access-Control-Allow-Origin']=BI_ORIGIN
            response.headers.add('Vary','Origin')
            response.headers['Access-Control-Allow-Methods']='GET, POST, OPTIONS'
            response.headers['Access-Control-Allow-Headers']='Content-Type, X-BI-Upload-Ticket'
            response.headers['Access-Control-Max-Age']='300'
            response.headers['Cache-Control']='no-store'
        return response

    @app.post('/bi-lan-upload/ticket')
    def bi_lan_ticket():
        from flask import session
        if request.content_length is None or request.content_length > 4096:
            return jsonify(ok=False), 413
        if request.headers.get('Origin') not in (None, BI_ORIGIN):
            return jsonify(ok=False), 403
        user = g['_current_user']()
        if user is None:
            return jsonify(ok=False), 401
        try:
            value = validate_file(request.form)
            backend = g['backend_manager'].get(user)
            g['backend_manager'].begin_proxy_request(backend)
            try:
                if not worker_json(backend, {k: value[k] for k in ('draft_id', 'csrf_token')}):
                    return jsonify(ok=False), 409
            finally:
                g['backend_manager'].finish_proxy_request(backend)
            value.update(user_id=user['user_id'], session_hash=g['_secret_hash'](str(session.get('browser_session', ''))))
            token = tickets.issue(value)
            return jsonify(ok=True, protocol=2, token=token, expiresIn=90)
        except (ValueError, TypeError, RuntimeError, OSError, http.client.HTTPException):
            return jsonify(ok=False), 409

    @app.route('/bi-lan-upload/file', methods=['POST'], provide_automatic_options=False)
    def bi_lan_file():
        # Current BI page sends only the file and the narrowly scoped ticket.
        if request.headers.get('Origin') != BI_ORIGIN:
            return jsonify(ok=False), 403
        value = tickets.take(str(request.headers.get('X-BI-Upload-Ticket', ''))
        )
        if not value:
            return jsonify(ok=False), 401
        if request.content_length != value['size'] or request.mimetype != 'application/octet-stream':
            return jsonify(ok=False), 413
        with g['_browser_sessions_lock']:
            record = g['_browser_sessions'].get(value['session_hash'])
            if not record or record[0] != value['user_id'] or record[2] <= time.time():
                return jsonify(ok=False), 401
        user = g['_user_record_by_id'](value['user_id'])
        if user is None:
            return jsonify(ok=False), 401
        connection = None
        reserved = False
        try:
            backend = g['backend_manager'].get(user)
            g['backend_manager'].begin_proxy_request(backend)
            reserved = True
            boundary, prefix, suffix = multipart_parts(value)
            connection = http.client.HTTPConnection('127.0.0.1', backend.port, timeout=600)
            connection.putrequest('POST', '/task-drafts/upload')
            for name, data in {
                'Content-Type': 'multipart/form-data; boundary=' + boundary,
                'Content-Length': str(len(prefix) + value['size'] + len(suffix)),
                'Accept': 'application/json', 'X-Requested-With': 'fetch',
                'X-Kuaishou-Gateway-Token': backend.gateway_token,
            }.items():
                connection.putheader(name, data)
            connection.endheaders()
            connection.send(prefix)
            remaining = value['size']
            while remaining:
                chunk = request.stream.read(min(256 * 1024, remaining))
                if not chunk:
                    raise ValueError('incomplete file')
                connection.send(chunk)
                remaining -= len(chunk)
            connection.send(suffix)
            response = connection.getresponse()
            raw = response.read(1024 * 1024 + 1)
            if response.status not in (200, 201) or len(raw) > 1024 * 1024 or json.loads(raw).get('ok') is not True:
                return jsonify(ok=False), 409
            return jsonify(ok=True)
        except (ValueError, RuntimeError, OSError, http.client.HTTPException):
            return jsonify(ok=False), 502
        finally:
            if connection:
                connection.close()
            if reserved:
                g['backend_manager'].finish_proxy_request(backend)
