"""Web sign-in for `alpaca serve`: a signed session cookie in front of the configured credential.

The credential is the same `user:pass` string `serve.files_auth` reads (ALPACA_FILES_AUTH or
`.alpaca/files-auth`). A browser signs in once through the `/login` page and receives an HttpOnly
cookie `alpaca_session_<id>=<expiry>.<mac>`, where `<id>` is the first 8 hex of the instance id
(sha256 of the real root). Cookies ignore the port, so two instances on 127.0.0.1 share one cookie
jar; the per-instance name keeps each instance in its own slot. A cookie under the old shared name
`alpaca_session` is not read at all, so it reads as signed out. The MAC covers the expiry and a digest of the credential,
keyed by a random per-project secret in `.alpaca/web-session-key`, so:

- nothing secret is stored in the browser, and the cookie cannot be forged without the key;
- changing the credential invalidates every issued cookie at once;
- deleting the key file signs every browser out.

HTTP Basic credentials stay accepted beside the cookie, so curl, the tunnel watchdog and older
scripts keep working. Failed attempts on either path feed one throttle: a client that fails
CLIENT_MAX times in WINDOW_S is refused until the window passes, and GLOBAL_MAX failures from
all clients together close sign-in for everyone for the same window. The global cap is what
bounds a distributed guess against a short pin; the cost is that a flood can lock the owner
out for one window.

Authenticator codes (TOTP, RFC 6238): when `.alpaca/totp-secret` holds a base32 secret
(written by `alpaca serve --totp-enroll`), the `/login` page accepts the current 6-digit code
from an authenticator app instead of the fixed `files-auth` code. One 30 s step of clock drift
either way is accepted, and a code that already signed in is refused for the rest of its
window. The cookie MAC then also covers a digest of the secret, so re-enrolling signs every
browser out. HTTP Basic keeps using `files-auth`: a script cannot read a rotating code.

This module never writes the record. The key file is runtime state beside `files-auth`.
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
import threading
import time
from urllib.parse import quote

from alpaca import paths

COOKIE = "alpaca_session"         # the name prefix; the full name carries the instance id (cookie_name)
TTL_S = 12 * 3600            # one sign-in lasts a working day
WINDOW_S = 15 * 60           # throttle window
CLIENT_MAX = 8               # failures one client may make per window
GLOBAL_MAX = 60              # failures all clients together may make per window
KEY_FILE = "web-session-key"
TOTP_FILE = "totp-secret"
TOTP_STEP = 30               # seconds per code
TOTP_DIGITS = 6
TOTP_DRIFT = 1               # steps of clock drift accepted either way


# A sign-in redirect target (?next=): "/" then printable ASCII with no backslash, never "//" or
# "/\\" at the start. A control character could split the Location header; a backslash, tab or
# newline lets the browser read the target as another host. vault.js applies the same rule.
SAFE_NEXT_RE = re.compile(r"/(?![/\\])[!-\[\]-~]*")


def safe_next(target):
    """`target` when it is a same-origin absolute path with no control character, space or
    backslash, else "/"."""
    if isinstance(target, str) and SAFE_NEXT_RE.fullmatch(target):
        return target
    return "/"


def _key(root):
    """The per-project signing key, created (0600) on first use."""
    path = os.path.join(paths.runtime_dir(root), KEY_FILE)
    try:
        with open(path, "r", encoding="ascii") as fh:
            value = fh.read().strip()
        if len(value) >= 32:
            return value.encode("ascii")
    except OSError:
        pass
    value = secrets.token_hex(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(value + "\n")
    os.replace(path + ".tmp", path)
    return value.encode("ascii")


def _mac(root, credential, expiry):
    digest = hashlib.sha256(credential.encode("utf-8")).hexdigest()
    message = ("%d|%s" % (expiry, digest)).encode("ascii")
    return hmac.new(_key(root), message, hashlib.sha256).hexdigest()


def issue(root, credential, now=None):
    """A fresh cookie value for a correct sign-in."""
    expiry = int((time.time() if now is None else now) + TTL_S)
    return "%d.%s" % (expiry, _mac(root, credential, expiry))


def valid(root, credential, value, now=None):
    """True when `value` is an unexpired cookie issued for this credential."""
    if not credential or not value or "." not in value:
        return False
    head, _, mac = value.partition(".")
    try:
        expiry = int(head)
    except ValueError:
        return False
    if expiry < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(mac, _mac(root, credential, expiry))


def credential_ok(credential, user, password):
    """Constant-time comparison of a submitted user and password against `user:pass`."""
    if not credential:
        return False
    return hmac.compare_digest(("%s:%s" % (user or "", password or "")).encode("utf-8"),
                               credential.encode("utf-8"))


def user_ok(credential, user):
    """Constant-time check of a submitted user against the user part of `user:pass`."""
    want = (credential or "").partition(":")[0]
    return hmac.compare_digest((user or "").encode("utf-8"), want.encode("utf-8"))


def totp_secret(root):
    """The enrolled base32 authenticator secret, or None when codes are not enabled."""
    try:
        with open(os.path.join(paths.runtime_dir(root), TOTP_FILE), "r", encoding="ascii") as fh:
            value = fh.read().strip().upper()
        base64.b32decode(value + "=" * (-len(value) % 8))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return value or None


def totp_enroll(root):
    """Write a fresh 160-bit secret (0600) and return it. Refuses to replace an existing one."""
    path = os.path.join(paths.runtime_dir(root), TOTP_FILE)
    if os.path.exists(path):
        raise FileExistsError(path)
    value = base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(value + "\n")
    os.replace(path + ".tmp", path)
    return value


def totp_uri(secret, account):
    """The otpauth:// URI an authenticator app imports (issuer Alpaca)."""
    return "otpauth://totp/%s?secret=%s&issuer=Alpaca&digits=%d&period=%d" % (
        quote("Alpaca:" + account), secret, TOTP_DIGITS, TOTP_STEP)


def totp_code(secret, counter):
    """The RFC 4226 code for one counter value."""
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return "%0*d" % (TOTP_DIGITS, value % (10 ** TOTP_DIGITS))


class TotpGuard:
    """Accepts a current code once. The newest accepted step is remembered in memory, so a code
    (or an older one) cannot sign in twice; a restart forgets it, which reopens at most one
    30 s window."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last = -1

    def check(self, secret, code, now=None):
        code = (code or "").replace(" ", "")
        if not secret or len(code) != TOTP_DIGITS or not code.isdigit():
            return False
        step = int((time.time() if now is None else now) // TOTP_STEP)
        with self.lock:
            for counter in range(step - TOTP_DRIFT, step + TOTP_DRIFT + 1):
                if counter > self.last and hmac.compare_digest(totp_code(secret, counter), code):
                    self.last = counter
                    return True
        return False


def session_credential(credential, secret):
    """What the cookie MAC covers: the credential, plus a digest of the authenticator secret
    when one is enrolled, so a re-enroll invalidates every issued cookie."""
    if not credential or not secret:
        return credential
    return "%s|totp:%s" % (credential, hashlib.sha256(secret.encode("ascii")).hexdigest())


def needs_user(credential):
    """True when the configured credential has a non-empty user name."""
    return bool(credential) and bool(credential.partition(":")[0])


def cookie_name(root):
    """This instance's session cookie name: `alpaca_session_` plus 8 hex of sha256(real root)."""
    return "%s_%s" % (COOKIE, hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:8])


def read_cookie(root, header):
    """This instance's session cookie value from a Cookie header, or ''. Another instance's
    cookie and the old shared `alpaca_session` are ignored (signed out, never an error)."""
    want = cookie_name(root)
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == want:
            return value.strip()
    return ""


def set_cookie(root, value, secure):
    """The Set-Cookie header value that stores a session."""
    flags = "Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % TTL_S
    return "%s=%s; %s%s" % (cookie_name(root), value, flags, "; Secure" if secure else "")


def clear_cookie(root, secure):
    """The Set-Cookie header value that removes the session."""
    return "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0%s" % (cookie_name(root), "; Secure" if secure else "")


class Throttle:
    """Failure counts per client and in total over a sliding window."""

    def __init__(self, window=WINDOW_S, client_max=CLIENT_MAX, global_max=GLOBAL_MAX):
        self.window = window
        self.client_max = client_max
        self.global_max = global_max
        self.lock = threading.Lock()
        self.fails = {}          # client -> [timestamps]
        self.all = []            # every failure timestamp

    def _prune(self, now):
        edge = now - self.window
        self.all = [t for t in self.all if t > edge]
        for client in list(self.fails):
            kept = [t for t in self.fails[client] if t > edge]
            if kept:
                self.fails[client] = kept
            else:
                del self.fails[client]

    def blocked(self, client, now=None):
        """Seconds until `client` may try again, or 0 when it may try now."""
        now = time.time() if now is None else now
        with self.lock:
            self._prune(now)
            mine = self.fails.get(client) or []
            waits = []
            if len(mine) >= self.client_max:
                waits.append(mine[-self.client_max] + self.window - now)
            if len(self.all) >= self.global_max:
                waits.append(self.all[-self.global_max] + self.window - now)
            return max(0, int(max(waits) + 1)) if waits else 0

    def fail(self, client, now=None):
        now = time.time() if now is None else now
        with self.lock:
            self._prune(now)
            self.fails.setdefault(client, []).append(now)
            self.all.append(now)

    def succeed(self, client):
        with self.lock:
            self.fails.pop(client, None)
