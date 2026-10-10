"""The runner-side egress proxy of an agent jail: an HTTP CONNECT proxy on a Unix socket that
tunnels to the provider's hosts only, and refuses and logs every other request.

Design: design/research-engine/architecture.md sections 3.6 and 5.2 (FR-019, FR-006, FR-045).
Every agent jail has its own network namespace, so the Internet and the host's loopback services
are out of its reach. The one way out is this proxy: jail_forward.py inside the jail pipes each
connection of the runtime (HTTPS_PROXY=http://127.0.0.1:8119) to the socket served here. A
CONNECT to an allowed host:port is tunnelled; any other request gets 403. Every request, allowed
or refused, is one JSON line in the launch's egress log, which the jail never mounts.

It serves with plain sockets and threads from the standard library.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from urllib.parse import urlsplit

PROVIDER_HOSTS = {"claude": ("api.anthropic.com",), "codex": ("api.openai.com", "chatgpt.com")}

_BASE_URL_VARS = {"claude": "ANTHROPIC_BASE_URL", "codex": "OPENAI_BASE_URL"}
_SOCKET_NAME = "egress.sock"
_HEAD_MAX = 16384           # the most bytes of a request head read before it is refused
_HEAD_TIMEOUT_S = 30.0
_CONNECT_TIMEOUT_S = 30.0
_CHUNK = 65536
_LOG_MAX = 64 * 1024 * 1024  # the most bytes of egress log one proxy writes
_HOST_KEEP = 255            # the most characters of a host name kept in a log entry


def _host_port(value: str, default_port: int = 443) -> str:
    """host:port of a host name or host:port, lower case."""
    text = str(value).strip().lower()
    if not text:
        raise ValueError("an empty host")
    if text.startswith("["):
        raise ValueError("an address literal is not a host name: %r" % value)
    host, sep, port = text.rpartition(":")
    if sep and port.isdigit():
        return "%s:%d" % (host, int(port))
    return "%s:%d" % (text, default_port)


def allowed_hosts(runtime: str, environ: dict, contract) -> tuple:
    """PROVIDER_HOSTS[runtime], plus the host of ANTHROPIC_BASE_URL or OPENAI_BASE_URL when set,
    plus contract.egress_extra (research.egress.allow, class subset: owner-only to widen, t-088
    section 4). Each as host:443.

    A base URL that names a port keeps that port, since the runtime connects there; with no port
    it is 443, the one port a CONNECT tunnel serves (an http URL gets no port 80: the proxy
    refuses everything but CONNECT). A runtime outside PROVIDER_HOSTS (the control agent) gets
    only the contract's hosts. A contract of None adds nothing. Order is kept and duplicates
    dropped."""
    env = environ or {}
    out: list = []
    for host in PROVIDER_HOSTS.get(runtime, ()):
        out.append(_host_port(host))
    var = _BASE_URL_VARS.get(runtime)
    base = env.get(var) if var else None
    if base:
        parts = urlsplit(str(base))
        if parts.hostname:
            port = parts.port or 443
            out.append("%s:%d" % (parts.hostname.lower(), port))
    for host in tuple(getattr(contract, "egress_extra", ()) or ()) if contract is not None else ():
        out.append(_host_port(host))
    seen, kept = set(), []
    for item in out:
        if item not in seen:
            seen.add(item)
            kept.append(item)
    return tuple(kept)


def _parse_upstream(upstream: str | None):
    """(host, port) of the operator's HTTPS_PROXY, or None. Only an http:// proxy is supported:
    the tunnel to it is plain, as the runtime's own CONNECT would be."""
    if not upstream:
        return None
    parts = urlsplit(str(upstream))
    if parts.scheme not in ("http", "") or not parts.hostname:
        raise ValueError("unsupported upstream proxy (only http://host:port): %r" % upstream)
    return parts.hostname, parts.port or 80


class _Proxy:
    """The handle start() returns: the socket path, a refused count, and close()."""

    def __init__(self, sock_dir: str, allow: tuple, log: str, upstream):
        self.path = os.path.join(sock_dir, _SOCKET_NAME)
        self.log = log
        self.log_max = _LOG_MAX
        self.dropped = 0
        self.allow = frozenset(allow)
        self.upstream = upstream
        self.requests = 0
        self.refused = 0
        self._lock = threading.Lock()
        self._conns: set = set()
        self._closed = False
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if os.path.lexists(self.path):
            os.unlink(self.path)
        old = os.umask(0o077)
        try:
            self._listener.bind(self.path)
        finally:
            os.umask(old)
        self._listener.listen(64)
        os.makedirs(os.path.dirname(os.path.abspath(log)), exist_ok=True)
        with open(log, "a", encoding="utf-8"):
            pass
        self.log_bytes = os.path.getsize(log)
        self._thread = threading.Thread(target=self._accept, name="alpaca-egress", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ bookkeeping
    def _track(self, sock) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._conns.add(sock)
            return True

    def _untrack(self, sock) -> None:
        with self._lock:
            self._conns.discard(sock)
        try:
            sock.close()
        except OSError:
            pass

    def _record(self, entry: dict) -> None:
        """Count the request and append its line to the log, the host cut to _HOST_KEEP
        characters. Once the log would pass log_max bytes (_LOG_MAX when the proxy started) the
        line is left out and counted in `dropped` instead, so an agent that floods the proxy
        cannot grow the log on the host past the cap (check round 4 of t-098, adversarial
        m1)."""
        if isinstance(entry.get("host"), str):
            entry["host"] = entry["host"][:_HOST_KEEP]
        line = json.dumps(entry, sort_keys=True, ensure_ascii=True) + "\n"
        with self._lock:
            self.requests += 1
            if not entry.get("allowed"):
                self.refused += 1
            if self.log_bytes + len(line) > self.log_max:
                self.dropped += 1
                return
            self.log_bytes += len(line)
            with open(self.log, "a", encoding="utf-8") as fh:
                fh.write(line)

    # ------------------------------------------------------------------ serving
    def _accept(self) -> None:
        while True:
            try:
                client, _addr = self._listener.accept()
            except OSError:
                return
            if not self._track(client):
                client.close()
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _read_head(self, client) -> bytes | None:
        client.settimeout(_HEAD_TIMEOUT_S)
        data = b""
        while b"\r\n\r\n" not in data:
            try:
                chunk = client.recv(4096)
            except OSError:
                return None
            if not chunk:
                return None
            data += chunk
            if len(data) > _HEAD_MAX:
                return None
        return data

    def _refuse(self, client, status: bytes) -> None:
        try:
            client.sendall(b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\n"
                           b"Connection: close\r\n\r\n")
        except OSError:
            pass

    def _open_upstream(self, host: str, port: int):
        if self.upstream is None:
            return socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT_S)
        sock = socket.create_connection(self.upstream, timeout=_CONNECT_TIMEOUT_S)
        target = ("%s:%d" % (host, port)).encode("ascii")
        sock.sendall(b"CONNECT " + target + b" HTTP/1.1\r\nHost: " + target + b"\r\n\r\n")
        reply = b""
        while b"\r\n\r\n" not in reply:
            chunk = sock.recv(4096)
            if not chunk or len(reply) > _HEAD_MAX:
                sock.close()
                raise OSError("upstream proxy closed before its reply")
            reply += chunk
        status = reply.split(b"\r\n", 1)[0].split()
        if len(status) < 2 or status[1] != b"200":
            sock.close()
            raise OSError("upstream proxy refused: %s" % reply.split(b"\r\n", 1)[0][:80])
        return sock

    def _handle(self, client) -> None:
        upstream_sock = None
        try:
            head = self._read_head(client)
            if head is None:
                self._record({"ts": time.time(), "request": None, "method": None, "host": None,
                              "port": None, "allowed": False, "reason": "no request head"})
                self._refuse(client, b"400 Bad Request")
                return
            first = head.split(b"\r\n", 1)[0].decode("ascii", "replace")
            parts = first.split()
            method = parts[0].upper() if parts else ""
            target = parts[1] if len(parts) >= 2 else ""
            host, port = None, None
            if method == "CONNECT" and len(parts) == 3:
                name, sep, digits = target.rpartition(":")
                if sep and digits.isdigit():
                    host, port = name.lower(), int(digits)
            else:
                try:
                    split = urlsplit(target)
                    host, port = split.hostname, split.port
                except ValueError:
                    host, port = None, None
            key = "%s:%d" % (host, port) if host and port else None
            allowed = method == "CONNECT" and key is not None and key in self.allow
            entry = {"ts": time.time(), "request": first[:512], "method": method, "host": host,
                     "port": port, "allowed": allowed}
            if not allowed:
                entry["reason"] = "not CONNECT" if method != "CONNECT" else "host not allowed"
                self._record(entry)
                self._refuse(client, b"403 Forbidden")
                return
            try:
                upstream_sock = self._open_upstream(host, port)
            except OSError as exc:
                entry["error"] = str(exc)[:200]
                self._record(entry)
                self._refuse(client, b"502 Bad Gateway")
                return
            self._record(entry)
            if not self._track(upstream_sock):
                return
            client.settimeout(None)
            upstream_sock.settimeout(None)
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            rest = head.split(b"\r\n\r\n", 1)[1]
            if rest:
                upstream_sock.sendall(rest)
            back = threading.Thread(target=_pump, args=(upstream_sock, client), daemon=True)
            back.start()
            _pump(client, upstream_sock)
            back.join()
        except OSError:
            pass
        finally:
            if upstream_sock is not None:
                self._untrack(upstream_sock)
            self._untrack(client)

    def close(self) -> None:
        """Stop accepting, close every open tunnel and remove the socket file."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            conns = list(self._conns)
            self._conns.clear()
        try:
            self._listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._listener.close()
        except OSError:
            pass
        for sock in conns:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self._thread.join(timeout=5.0)
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _pump(src, dst) -> None:
    try:
        while True:
            data = src.recv(_CHUNK)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def start(sock_dir: str, *, allow: tuple, log: str, upstream: str | None = None):
    """Serve an HTTP CONNECT proxy on sock_dir/egress.sock in a thread of the runner: CONNECT to
    an allowed host:port is tunnelled (through `upstream`, the operator's HTTPS_PROXY, when set);
    any other request gets 403. Every request is one JSON line in `log` with the host, port and
    whether it was allowed. Returns a handle with close().

    `allow` holds host:port strings (allowed_hosts gives them). The socket is made with mode
    0600 inside the 0700 folder paths.egress_dir makes. The handle also has `path` (the socket),
    `requests` and `refused` (counts). A tunnel whose upstream cannot be reached answers 502 and
    is logged with its error. The log holds at most _LOG_MAX bytes: past it a request is still
    answered but its line is left out and counted in the handle's `dropped`. Each entry keeps at
    most _HOST_KEEP characters of the host and 512 of the request line. Raises ValueError for
    an upstream that is not an http:// proxy."""
    upstream_addr = _parse_upstream(upstream)
    return _Proxy(sock_dir, tuple(_host_port(a) for a in allow), log, upstream_addr)
