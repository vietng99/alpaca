"""The forwarder run inside an agent jail: it listens on the jail's own loopback, pipes each
connection to the egress socket and runs the runtime as its child. Standard library only: it is
copied into each launch home and run there, so it never imports alpaca.

Design: design/research-engine/architecture.md sections 3.6 and 5.2 (FR-019). The jail has its
own network namespace, so 127.0.0.1 here is the jail's loopback, never the host's; the only way
out is the Unix socket the runner's egress proxy serves, bound read-only at /run/alpaca-egress.

Usage: python3 jail_forward.py <port> <socket> -- <command...>
"""
from __future__ import annotations

import socket
import subprocess
import sys
import threading

_CHUNK = 65536
_BACKLOG = 64


def _usage(message: str) -> int:
    sys.stderr.write("jail_forward: %s\n" % message)
    sys.stderr.write("usage: jail_forward.py <port> <socket> -- <command...>\n")
    return 64


def _pump(src: socket.socket, dst: socket.socket) -> None:
    """Copy bytes from src to dst until src ends, then half-close dst."""
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


def _serve_one(client: socket.socket, sock_path: str) -> None:
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        upstream.connect(sock_path)
    except OSError:
        upstream.close()
        client.close()
        return
    back = threading.Thread(target=_pump, args=(upstream, client), daemon=True)
    back.start()
    _pump(client, upstream)
    back.join()
    for sock in (client, upstream):
        try:
            sock.close()
        except OSError:
            pass


def _accept_loop(listener: socket.socket, sock_path: str) -> None:
    while True:
        try:
            client, _addr = listener.accept()
        except OSError:
            return
        threading.Thread(target=_serve_one, args=(client, sock_path), daemon=True).start()


def main(argv: list) -> int:
    """jail_forward.py <port> <socket> -- <command...>: listen on port <port> of the loopback address in the jail's
    own network namespace, pipe each connection to <socket>, run <command> as a child and exit
    with its code. The listener is bound before the child starts, so the runtime never finds the
    port closed. A child killed by a signal exits 128 plus the signal number, as a shell reports
    it."""
    args = list(argv)
    if "--" not in args:
        return _usage("missing -- before the command")
    cut = args.index("--")
    head, command = args[:cut], args[cut + 1:]
    if len(head) != 2 or not command:
        return _usage("need a port, a socket path and a command")
    try:
        port = int(head[0])
    except ValueError:
        return _usage("not a port: %r" % head[0])
    if not 0 < port < 65536:
        return _usage("port out of range: %d" % port)
    sock_path = head[1]
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", port))
        listener.listen(_BACKLOG)
    except OSError as exc:
        listener.close()
        sys.stderr.write("jail_forward: cannot listen on 127.0.0.1:%d: %s\n" % (port, exc))
        return 69
    threading.Thread(target=_accept_loop, args=(listener, sock_path), daemon=True).start()
    try:
        rc = subprocess.call(command)
    except OSError as exc:
        sys.stderr.write("jail_forward: cannot start %r: %s\n" % (command[0], exc))
        return 127
    finally:
        try:
            listener.close()
        except OSError:
            pass
    if rc < 0:
        return 128 - rc
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
