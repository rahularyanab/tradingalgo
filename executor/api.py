"""Client for the Theta Desk executor API: HTTPS, trusting ONLY the pinned certificate fingerprint."""
from __future__ import annotations

import hashlib
import json
import socket
import ssl
from urllib.parse import urlparse


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Api:
    def __init__(self, url: str, pin: str, key: str, timeout: float = 10.0):
        u = urlparse(url)
        self.host, self.port = u.hostname, u.port or 443
        self.pin, self.key, self.timeout = pin.lower(), key, timeout

    def _get(self, path: str) -> dict:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE                  # trust is the pinned fingerprint, checked below
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as raw, \
                    ctx.wrap_socket(raw, server_hostname=self.host) as s:
                got = hashlib.sha256(s.getpeercert(binary_form=True)).hexdigest()
                if got != self.pin:
                    raise ApiError(f"server certificate does not match the pinned fingerprint (got {got[:12]}…) — refusing")
                s.sendall((f"GET {path} HTTP/1.1\r\nHost: {self.host}\r\nConnection: close\r\n"
                           f"Authorization: Bearer {self.key}\r\nUser-Agent: thetadesk-executor/1\r\n\r\n").encode())
                data = b""
                while chunk := s.recv(65536):
                    data += chunk
        except (OSError, ssl.SSLError) as e:
            raise ApiError(f"cannot reach the executor API: {e}") from e
        head, _, body = data.partition(b"\r\n\r\n")
        try:
            status = int(head.split()[1])
            payload = json.loads(body or b"{}")
        except (IndexError, ValueError) as e:
            raise ApiError(f"unreadable reply from the executor API: {head[:80]!r}") from e
        if status != 200:
            raise ApiError(payload.get("error") or f"HTTP {status}", status)
        return payload

    def status(self) -> dict:
        return self._get("/v1/status")

    def instructions(self, since: int) -> dict:
        return self._get(f"/v1/instructions?since={int(since)}")
