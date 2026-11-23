"""Fast reachability probe for the recall hook's remote backends (#106).

The recall hook blocks prompt submission. On localhost a dead Qdrant or Ollama
refuses the connection at once, so it fails open for free. A REMOTE backend is
different: when the tailnet or VPN is down, or the host is asleep, packets are
dropped with no reply, and every request waits out its full timeout. Name
resolution can stall as well (a MagicDNS-style name with no resolver), and a
`requests` timeout does not bound name resolution at all.

So before the hook does any work it opens one TCP connection to each non-loopback
backend, in parallel, under one short deadline (`proactive_recall.connect_timeout_s`,
default 1 s). Each probe runs in a daemon thread, so a resolver that hangs cannot
hold the hook (or interpreter shutdown) past that deadline. Loopback endpoints are
skipped: they cost nothing to fail.

Read-only: a TCP connect and close, nothing sent.
"""
from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from carta.config import is_loopback_url

DEFAULT_CONNECT_TIMEOUT_S = 1.0


@dataclass
class Unreachable:
    name: str       # "Qdrant", "Ollama (query)"
    url: str
    reason: str     # short, one line

    def __str__(self) -> str:
        return f"{self.name} at {self.url} unreachable ({self.reason})"


def host_port(url: str) -> tuple[str, int] | None:
    """(host, port) for an http(s) URL, defaulting the port by scheme."""
    try:
        u = urlparse(url if "//" in url else f"//{url}")
        host = u.hostname
        port = u.port or (443 if u.scheme == "https" else 80)
    except ValueError:
        return None
    return (host, port) if host else None


def _connect(host: str, port: int, timeout_s: float, out: dict) -> None:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            pass
        out["ok"] = True
    except socket.timeout:
        out["reason"] = f"connect timed out after {timeout_s:g}s"
    except socket.gaierror as e:
        out["reason"] = f"cannot resolve host: {e.strerror or e}"
    except OSError as e:
        out["reason"] = e.strerror or type(e).__name__
    except Exception as e:  # pragma: no cover - defensive
        out["reason"] = type(e).__name__


def probe(endpoints: dict[str, str], timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
          *, include_loopback: bool = False) -> list[Unreachable]:
    """Connect to every endpoint at once; return the ones that did not answer.

    Bounded by `timeout_s` wall-clock in total (plus thread start-up), whatever the
    resolver or the network does. `endpoints` maps a display name to a URL.
    """
    results: list[tuple[str, str, dict, threading.Thread]] = []
    for name, url in endpoints.items():
        if not url or (not include_loopback and is_loopback_url(url)):
            continue
        hp = host_port(url)
        if hp is None:
            results.append((name, url, {"reason": "not a valid URL"}, None))
            continue
        out: dict = {}
        t = threading.Thread(target=_connect, args=(hp[0], hp[1], timeout_s, out),
                             name=f"carta-reach-{hp[0]}", daemon=True)
        t.start()
        results.append((name, url, out, t))

    deadline = time.monotonic() + timeout_s
    down: list[Unreachable] = []
    for name, url, out, t in results:
        if t is not None:
            t.join(max(0.0, deadline - time.monotonic()) + 0.05)
        if out.get("ok"):
            continue
        reason = out.get("reason") or f"no answer within {timeout_s:g}s"
        down.append(Unreachable(name, url, reason))
    return down
