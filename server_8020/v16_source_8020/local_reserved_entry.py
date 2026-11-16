"""Trusted loopback entry boundary; HTTP headers never grant a reserved lane."""
from __future__ import annotations

from flask import has_request_context, request
from flask.sessions import SecureCookieSessionInterface

LOCAL_PORT = 8021
LOCAL_URL = f"http://127.0.0.1:{LOCAL_PORT}"
ENTRY_ENVIRON_KEY = "kuaishou.trusted_local_entry"


def is_local_entry() -> bool:
    return has_request_context() and request.environ.get(ENTRY_ENVIRON_KEY) is True


class EntryBoundary:
    """Install this on separate listeners; never infer the lane from Host/XFF."""
    def __init__(self, app, *, local=False):
        self.app = app
        self.local = bool(local)

    def __call__(self, environ, start_response):
        # Overwrite, do not setdefault: even upstream middleware cannot promote public traffic.
        environ[ENTRY_ENVIRON_KEY] = self.local
        if self.local:
            if (environ.get("REMOTE_ADDR") != "127.0.0.1"
                    or environ.get("HTTP_HOST", "").lower() != f"127.0.0.1:{LOCAL_PORT}"
                    or (environ.get("HTTP_ORIGIN") and environ["HTTP_ORIGIN"] != LOCAL_URL)):
                start_response("403 Forbidden", [("Content-Type", "text/plain"), ("Content-Length", "9")])
                return [b"Forbidden"]
        return self.app(environ, start_response)


class EntrySessionInterface(SecureCookieSessionInterface):
    def get_cookie_name(self, app):
        name = super().get_cookie_name(app)
        return name + "_local" if is_local_entry() else name

    def get_cookie_secure(self, app):
        # The isolated loopback entry is HTTP even if the public entry later uses TLS.
        return False if is_local_entry() else super().get_cookie_secure(app)
