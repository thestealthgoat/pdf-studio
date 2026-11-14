"""Polite HTTP for public data services: User-Agent, timeouts, retries with backoff, on-disk cache.

Only the services named in :mod:`pdfcore.ops.plans.site.sources` are contacted. Google / Bing / Apple
imagery and tiles are never fetched (their terms forbid it); user screenshots go through site-from-image.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from pdfcore import __version__

USER_AGENT = f"pdf-studio/{__version__} (+https://github.com/thestealthgoat/pdf-studio)"
_BLOCKED = ("google.", "googleapis.", "gstatic.", "bing.", "virtualearth.", "apple.com", "apple-mapkit")


class NetError(RuntimeError):
    pass


def cache_dir() -> Path:
    d = Path(os.environ.get("PDFCORE_SITE_CACHE") or (Path.home() / ".cache" / "pdf-studio" / "site"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(url: str, data: bytes | None) -> str:
    h = hashlib.sha256(url.encode() + b"\0" + (data or b"")).hexdigest()[:32]
    return h


def fetch(url: str, *, data: bytes | None = None, timeout: float = 60, retries: int = 3, backoff: float = 4.0,
          cache: bool = True, accept: str = "*/*", quiet: bool = True) -> bytes:
    """GET (or POST *data*) with retries; cached by URL + body under :func:`cache_dir`."""
    host = urllib.parse.urlparse(url).netloc.lower()
    if any(b in host for b in _BLOCKED):
        raise NetError(f"refusing to fetch from {host}: its terms forbid programmatic imagery/tile access")
    path = cache_dir() / _key(url, data)
    if cache and path.exists() and path.stat().st_size > 0:
        return path.read_bytes()
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, "Accept": accept})
            if data is not None:
                req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
            if cache:
                tmp = path.with_suffix(".part")
                tmp.write_bytes(body)
                tmp.replace(path)
            return body
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (400, 401, 403, 404):
                break
        except Exception as exc:  # timeouts, resets
            last = exc
        if attempt < retries - 1:
            wait = backoff * (2 ** attempt)
            if not quiet:
                print(f"  {host}: {last} — retrying in {wait:.0f} s")
            time.sleep(wait)
    raise NetError(f"{host}: {last}")


def fetch_json(url: str, **kw) -> dict:
    body = fetch(url, accept="application/json", **kw)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        # do not keep a bad body in the cache
        p = cache_dir() / _key(url, kw.get("data"))
        p.unlink(missing_ok=True)
        raise NetError(f"not JSON from {urllib.parse.urlparse(url).netloc}: {body[:120]!r}") from exc


def online(host: str = "basemap.nationalmap.gov", timeout: float = 4.0) -> bool:
    import socket

    try:
        socket.create_connection((host, 443), timeout=timeout).close()
        return True
    except OSError:
        return False
