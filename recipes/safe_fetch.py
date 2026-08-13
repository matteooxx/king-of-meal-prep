"""Hardened HTTP fetcher for user-supplied recipe URLs.

Defense against SSRF, DNS rebinding, and runaway downloads:

  1. Scheme allowlist (http/https only).
  2. Resolve hostname; reject if any A/AAAA points to RFC1918, loopback,
     link-local, IPv6 ULA, Tailscale CGNAT (100.64.0.0/10), or the host's
     own interface addresses.
  3. Manual redirect handling — re-validate the destination at every hop,
     defeats DNS-rebind and 302-to-internal redirect attacks.
  4. Body size cap (2 MB default). recipe pages are typically <500 KB.
  5. Per-request timeout (12 s) + per-redirect-chain timeout (30 s total).

Returns (text, final_url) on success, raises SafeFetchError on rejection.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import time

import httpx

log = logging.getLogger("king-of-meal-prep.safe_fetch")


class SafeFetchError(Exception):
    """Anything we deliberately refused or that timed out."""


MAX_BYTES   = 2 * 1024 * 1024
MAX_REDIRECTS = 4
PER_HOP_TIMEOUT_S = 12.0
TOTAL_TIMEOUT_S   = 30.0

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17 Safari/605.1.15",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en;q=0.9, it;q=0.8",
}


def _is_blocked_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True   # unparseable = treat as hostile
    # is_global rejects loopback, private, link-local, CGNAT, multicast,
    # documentation, unspecified and reserved ranges for both IPv4 and IPv6.
    return not ip.is_global


def _resolve_and_check(host: str, port: int) -> list[str]:
    """Resolve once and return validated addresses for connection pinning."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SafeFetchError(f"DNS lookup failed for {host}: {e}")
    seen: set[str] = set()
    addresses: list[str] = []
    for fam, _, _, _, sockaddr in infos:
        addr = sockaddr[0]
        if addr in seen:
            continue
        seen.add(addr)
        if _is_blocked_ip(addr):
            raise SafeFetchError(f"refusing to fetch — {host} resolves to {addr} (private/internal)")
        addresses.append(addr)
    if not addresses:
        raise SafeFetchError(f"DNS lookup returned no addresses for {host}")
    return addresses


def _validate_url(url: str) -> tuple[httpx.URL, list[str]]:
    """Parse, resolve and validate. The caller must connect to a returned IP."""
    try:
        parsed = httpx.URL(url)
    except Exception as e:
        raise SafeFetchError(f"invalid URL: {e}")
    if parsed.scheme not in ("http", "https"):
        raise SafeFetchError(f"refusing scheme {parsed.scheme!r}; only http/https allowed")
    if parsed.userinfo:
        raise SafeFetchError("URL credentials are not allowed")
    host = parsed.host
    if not host:
        raise SafeFetchError("URL has no host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed, _resolve_and_check(host, port)


def _connected_peer(response: httpx.Response) -> str | None:
    """Return the peer IP when httpcore exposes it; transports may omit it."""
    stream = response.extensions.get("network_stream")
    if stream is None or not hasattr(stream, "get_extra_info"):
        return None
    for key in ("server_addr", "peername"):
        try:
            value = stream.get_extra_info(key)
        except (OSError, RuntimeError):
            continue
        if isinstance(value, tuple) and value:
            return str(value[0])
        if isinstance(value, str):
            return value
    return None


def _host_header(parsed: httpx.URL) -> str:
    host = parsed.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 443 if parsed.scheme == "https" else 80
    if parsed.port and parsed.port != default_port:
        host = f"{host}:{parsed.port}"
    return host


def fetch(url: str, *, max_bytes: int = MAX_BYTES,
          max_redirects: int = MAX_REDIRECTS) -> tuple[str, str]:
    """Fetch a URL with full SSRF + body-size + redirect protection.

    Returns (text, final_url). Raises SafeFetchError on any rejection.
    """
    started = time.monotonic()
    current = url

    for hop in range(max_redirects + 1):
        if time.monotonic() - started > TOTAL_TIMEOUT_S:
            raise SafeFetchError("total fetch timeout exceeded")
        parsed, addresses = _validate_url(current)
        redirect_target: str | None = None
        last_connection_error: httpx.HTTPError | None = None
        for pinned in addresses:
            remaining = TOTAL_TIMEOUT_S - (time.monotonic() - started)
            if remaining <= 0:
                raise SafeFetchError("total fetch timeout exceeded")
            target = parsed.copy_with(host=pinned)
            try:
                # A new client for each address and redirect prevents a pooled
                # TLS connection from being reused for a different hostname.
                with httpx.Client(
                    timeout=min(PER_HOP_TIMEOUT_S, remaining),
                    follow_redirects=False,
                    headers=_HEADERS,
                    trust_env=False,
                ) as client:
                    with client.stream(
                        "GET",
                        target,
                        headers={**_HEADERS, "Host": _host_header(parsed)},
                        extensions={"sni_hostname": parsed.host},
                    ) as r:
                        peer = _connected_peer(r)
                        if peer is not None and (
                            _is_blocked_ip(peer)
                            or ipaddress.ip_address(peer)
                            != ipaddress.ip_address(pinned)
                        ):
                            raise SafeFetchError(
                                f"connected peer {peer} did not match validated address"
                            )
                        if r.status_code in (301, 302, 303, 307, 308):
                            loc = r.headers.get("Location")
                            if not loc:
                                raise SafeFetchError(
                                    "redirect with no Location header "
                                    f"(HTTP {r.status_code})"
                                )
                            redirect_target = loc
                            break
                        if r.status_code != 200:
                            raise SafeFetchError(f"HTTP {r.status_code}")

                        chunks: list[bytes] = []
                        total = 0
                        for chunk in r.iter_bytes():
                            total += len(chunk)
                            if total > max_bytes:
                                raise SafeFetchError(
                                    f"response > {max_bytes} bytes"
                                )
                            chunks.append(chunk)
                        body = b"".join(chunks)
                        encoding = r.encoding or "utf-8"
                        try:
                            text = body.decode(encoding, errors="replace")
                        except (LookupError, UnicodeDecodeError):
                            text = body.decode("utf-8", errors="replace")
                        return text, str(parsed)
            except httpx.HTTPError as e:
                last_connection_error = e
                continue
            if redirect_target is not None:
                break

        if redirect_target is not None:
            try:
                current = str(parsed.join(redirect_target))
            except Exception as e:
                raise SafeFetchError(f"invalid redirect target: {e}")
            continue
        if last_connection_error is not None:
            raise SafeFetchError(f"http error: {last_connection_error}")
        raise SafeFetchError("no validated address could be reached")
    raise SafeFetchError(f"too many redirects (>{max_redirects})")
