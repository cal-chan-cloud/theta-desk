"""HTTP layer: throttled, retried, disk-cached.

Uses urllib rather than requests so the pipeline runs on any interpreter on
this machine (see the two-Python note in config).  Every response body is
cached to disk; a cached body inside its TTL means a re-run of the pipeline
costs nothing, which matters when iterating on the model.
"""

import gzip
import hashlib
import io
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

import config


def _ssl_context():
    """Prefer certifi's CA bundle over the Windows store.

    cdn.cboe.com's chain includes a cross-signed root that has expired in the
    Windows ROOT store, so the system default rejects it with
    CERTIFICATE_VERIFY_FAILED while every browser accepts it.  certifi ships a
    current bundle and is present alongside pip on both interpreters here.
    Verification stays fully on either way -- we never fall back to unverified.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:                     # noqa: BLE001 - certifi is optional
        return ssl.create_default_context()


SSL_CTX = _ssl_context()

_host_lock = threading.Lock()
_last_hit = {}
_stats = {"requests": 0, "cache_hits": 0, "errors": 0, "bytes": 0, "retries": 0}


class FetchError(Exception):
    pass


def stats():
    return dict(_stats)


def reset_stats():
    for k in _stats:
        _stats[k] = 0


def _cache_path(url, tag):
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
    sub = os.path.join(config.CACHE_DIR, tag or "misc")
    os.makedirs(sub, exist_ok=True)
    return os.path.join(sub, h + ".bin")


def _cache_read(path, ttl):
    if ttl <= 0 or not os.path.exists(path):
        return None
    try:
        age = time.time() - os.path.getmtime(path)
        if age > ttl:
            return None
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _cache_write(path, body):
    try:
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
    except OSError:
        pass


def _throttle(host):
    gap = config.THROTTLE_SECONDS.get(host, 0.25)
    with _host_lock:
        last = _last_hit.get(host, 0.0)
        wait = gap - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.time()


def _decode(resp):
    raw = resp.read()
    enc = (resp.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        try:
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        except OSError:
            pass
    elif enc == "deflate":
        try:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        except zlib.error:
            pass
    return raw


def fetch(url, tag=None, ttl=None, headers=None, allow_stale=True):
    """Return response body as bytes.  Raises FetchError after all retries.

    `allow_stale` lets an expired cache entry rescue us when the network or the
    upstream is down -- a stale chain beats no chain when the site is loading.
    """
    ttl = config.CACHE_TTL.get(tag, 600) if ttl is None else ttl
    path = _cache_path(url, tag)
    cached = _cache_read(path, ttl)
    if cached is not None:
        _stats["cache_hits"] += 1
        return cached

    host = urllib.parse.urlparse(url).netloc
    hdrs = {
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json, text/plain, text/xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    if headers:
        hdrs.update(headers)

    last_err = None
    for attempt in range(config.HTTP_RETRIES):
        try:
            _throttle(host)
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT,
                                        context=SSL_CTX) as resp:
                body = _decode(resp)
            _stats["requests"] += 1
            _stats["bytes"] += len(body)
            _cache_write(path, body)
            return body
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s %s" % (e.code, e.reason)
            # 404 means the symbol genuinely has no data -- do not burn retries
            if e.code in (404, 400):
                break
        except Exception as e:                       # noqa: BLE001 - network is messy
            last_err = "%s: %s" % (type(e).__name__, e)
        _stats["retries"] += 1
        if attempt < config.HTTP_RETRIES - 1:
            time.sleep(config.HTTP_BACKOFF ** attempt)

    _stats["errors"] += 1
    if allow_stale:
        stale = _cache_read(path, ttl=10 ** 9)
        if stale is not None:
            return stale
    raise FetchError("%s -> %s" % (url, last_err))


def get_json(url, tag=None, ttl=None, headers=None, default=None):
    try:
        body = fetch(url, tag=tag, ttl=ttl, headers=headers)
        return json.loads(body.decode("utf-8", "replace"))
    except (FetchError, ValueError) as e:
        if default is not None:
            return default
        raise FetchError(str(e))


def get_text(url, tag=None, ttl=None, headers=None, default=None):
    try:
        return fetch(url, tag=tag, ttl=ttl, headers=headers).decode("utf-8", "replace")
    except FetchError:
        if default is not None:
            return default
        raise


def clear_cache(tag=None):
    """Remove cached bodies.  Used by the 'force refresh' pipeline flag."""
    n = 0
    base = os.path.join(config.CACHE_DIR, tag) if tag else config.CACHE_DIR
    for dirpath, _dirs, files in os.walk(base):
        for fn in files:
            try:
                os.remove(os.path.join(dirpath, fn))
                n += 1
            except OSError:
                pass
    return n


def cache_size():
    total, files = 0, 0
    for dirpath, _dirs, fs in os.walk(config.CACHE_DIR):
        for fn in fs:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
                files += 1
            except OSError:
                pass
    return {"bytes": total, "files": files}
