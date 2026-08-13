"""Password gate for Theta Desk.

Design notes
------------
* The password is never stored, only a **PBKDF2-HMAC-SHA256** hash with a
  per-install random salt at 600k iterations (the OWASP figure for SHA-256).
  Verification uses `hmac.compare_digest`, so it does not leak position
  information through timing.

* Credentials live in `data/auth.json`, which is **gitignored**.  Nothing
  secret ever enters the repository.  `THETA_DESK_PASSWORD` in the environment
  overrides the file, which is what you would use if this were ever deployed
  behind a real host.

* The session is a Flask signed cookie over a persisted random secret, so a
  server restart does not log you out, and a forged cookie fails the HMAC.

* Failed logins back off exponentially per client address.  This is a
  single-user tool, so a simple in-memory counter is the right amount of
  machinery -- it stops an automated guesser without pulling in a dependency.

Note that binding to 127.0.0.1 (the default) already makes the site
unreachable from outside this machine.  The gate matters the moment you change
`HOST`, put it behind a tunnel, or share a screen.
"""

import base64
import functools
import hashlib
import hmac
import json
import os
import secrets
import time

from flask import (jsonify, redirect, render_template_string, request,
                   session, url_for)

import config

AUTH_FILE = os.path.join(config.DATA_DIR, "auth.json")
ITERATIONS = 600_000
SESSION_KEY = "td_auth"
SESSION_DAYS = 30

# Paths that must stay reachable without a session, or you could never log in.
OPEN_PATHS = {"/login", "/logout", "/healthz", "/favicon.ico"}
OPEN_PREFIXES = ("/static/",)

_failures = {}          # remote addr -> [count, first_failure_ts]


# --------------------------------------------------------------- credentials
def _read():
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write(data):
    tmp = AUTH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, AUTH_FILE)
    try:                       # best effort on Windows; no-op if unsupported
        os.chmod(AUTH_FILE, 0o600)
    except OSError:
        pass


def hash_password(password, salt=None, iterations=ITERATIONS):
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {
        "algo": "pbkdf2_sha256",
        "iterations": iterations,
        "salt": base64.b64encode(salt).decode(),
        "hash": base64.b64encode(dk).decode(),
    }


def set_password(password):
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    data = _read()
    data["password"] = hash_password(password)
    data.setdefault("secret_key", secrets.token_urlsafe(48))
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _write(data)
    return True


def verify_password(password):
    """True if `password` matches the configured secret."""
    if not password:
        return False
    env = os.environ.get("THETA_DESK_PASSWORD")
    if env:
        return hmac.compare_digest(password, env)
    rec = (_read() or {}).get("password")
    if not rec:
        return False
    try:
        salt = base64.b64decode(rec["salt"])
        expect = base64.b64decode(rec["hash"])
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                 int(rec.get("iterations", ITERATIONS)))
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expect)


def is_enabled():
    """Is a password configured at all?"""
    return bool(os.environ.get("THETA_DESK_PASSWORD") or (_read() or {}).get("password"))


def secret_key():
    """Persisted signing key, generated on first use."""
    env = os.environ.get("THETA_DESK_SECRET_KEY")
    if env:
        return env
    data = _read()
    if not data.get("secret_key"):
        data["secret_key"] = secrets.token_urlsafe(48)
        _write(data)
    return data["secret_key"]


# ------------------------------------------------------------- rate limiting
def _throttle_seconds(addr):
    rec = _failures.get(addr)
    if not rec:
        return 0
    count, last = rec
    if count < 5:
        return 0
    wait = min(2 ** (count - 4), 300)          # 2s, 4s, 8s ... capped at 5 min
    remaining = wait - (time.time() - last)
    return max(int(remaining), 0)


def _record_failure(addr):
    count, _ = _failures.get(addr, (0, 0))
    _failures[addr] = (count + 1, time.time())


def _clear_failures(addr):
    _failures.pop(addr, None)


# --------------------------------------------------------------- Flask glue
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Theta Desk</title>
<link rel="stylesheet" href="/static/styles.css">
<style>
  body { display: grid; place-items: center; min-height: 100vh; }
  .login {
    width: min(380px, 92vw); background: var(--surface);
    border: 1px solid var(--border); border-radius: var(--r-lg);
    padding: 30px 28px; box-shadow: var(--shadow-3);
  }
  .login .brand-mark { width: 40px; height: 40px; border-radius: 11px; font-size: 20px; }
  .login h2 { font-size: 17px; margin: 14px 0 4px; }
  .login p.sub { color: var(--text-tertiary); font-size: 12.5px; margin: 0 0 20px; }
  .login input { width: 100%; padding: 10px 12px; font-size: 14px; }
  .login button { width: 100%; margin-top: 12px; justify-content: center; padding: 10px; }
  .err {
    background: rgba(208,59,59,0.12); color: var(--down); border: 1px solid var(--down);
    border-radius: var(--r-sm); padding: 9px 12px; font-size: 12.5px; margin-bottom: 14px;
  }
</style>
</head>
<body>
  <form class="login" method="post" action="/login">
    <div class="brand-mark">&theta;</div>
    <h2>Theta Desk</h2>
    <p class="sub">Options research &mdash; enter the desk password to continue.</p>
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <input type="password" name="password" placeholder="Password" autofocus
           autocomplete="current-password" aria-label="Password" required>
    <input type="hidden" name="next" value="{{ next_url }}">
    <button class="btn primary" type="submit">Unlock</button>
  </form>
</body>
</html>"""


def install(app):
    """Wire the gate into a Flask app."""
    app.secret_key = secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Only set Secure when actually served over TLS -- a Secure cookie on
        # plain http is simply never sent back, which looks like a broken login.
        SESSION_COOKIE_SECURE=bool(os.environ.get("THETA_DESK_HTTPS")),
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * SESSION_DAYS,
    )

    @app.before_request
    def _gate():
        if not is_enabled():
            return None                       # no password set: stay open
        p = request.path
        if p in OPEN_PATHS or p.startswith(OPEN_PREFIXES):
            return None
        if session.get(SESSION_KEY):
            return None
        if p.startswith("/api/"):
            return jsonify({"error": "authentication required", "login": "/login"}), 401
        return redirect(url_for("login", next=request.full_path if request.query_string else p))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not is_enabled():
            return redirect("/")
        addr = request.remote_addr or "?"
        nxt = request.values.get("next") or "/"
        if not nxt.startswith("/"):           # never bounce to another origin
            nxt = "/"
        if request.method == "GET":
            if session.get(SESSION_KEY):
                return redirect(nxt)
            return render_template_string(LOGIN_PAGE, error=None, next_url=nxt)

        wait = _throttle_seconds(addr)
        if wait:
            return render_template_string(
                LOGIN_PAGE, next_url=nxt,
                error="Too many attempts. Try again in %d seconds." % wait), 429

        if verify_password(request.form.get("password", "")):
            _clear_failures(addr)
            session.permanent = True
            session[SESSION_KEY] = True
            return redirect(nxt)

        _record_failure(addr)
        return render_template_string(
            LOGIN_PAGE, error="Incorrect password.", next_url=nxt), 401

    @app.route("/logout")
    def logout():
        session.pop(SESSION_KEY, None)
        return redirect("/login")

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "auth": is_enabled()})

    return app


def require(fn):
    """Decorator for anything registered outside the before_request hook."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if is_enabled() and not session.get(SESSION_KEY):
            return jsonify({"error": "authentication required"}), 401
        return fn(*a, **kw)
    return wrapper
