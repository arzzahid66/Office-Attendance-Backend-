import ipaddress

from fastapi import Request

from app.config import get_settings

settings = get_settings()


def normalize_ip(ip: str) -> str | None:
    """Canonical string form of a valid IPv4/IPv6 address, or None if invalid."""
    try:
        return str(ipaddress.ip_address(ip.strip()))
    except ValueError:
        return None


def get_client_ip(request: Request) -> str:
    """Resolve the real client IP behind exactly ONE trusted reverse proxy.

    SECURITY: never trust the LEFTMOST X-Forwarded-For entry. With nginx's
    `$proxy_add_x_forwarded_for`, whatever the client sends is preserved and the real IP is
    APPENDED, so the leftmost value is attacker-controlled. A client could send
    `X-Forwarded-For: <office-ip>` and check in via the WiFi path from anywhere.

    Order of trust:
      1. `X-Real-IP` — nginx sets it from `$remote_addr` (the TCP source address). The client
         cannot forge it, because nginx overwrites any value they send.
      2. The RIGHTMOST `X-Forwarded-For` entry — the one appended by the closest (trusted)
         proxy. Still safe if the proxy also overwrites XFF (recommended).
      3. `request.client.host` — the direct peer, when not behind a proxy.
    """
    if settings.trust_proxy:
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            canonical = normalize_ip(real_ip)
            if canonical:
                return canonical

        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            if parts:
                canonical = normalize_ip(parts[-1])  # rightmost == appended by OUR proxy
                if canonical:
                    return canonical

    client = request.client
    return client.host if client else "unknown"


def is_non_public_ip(ip: str) -> bool:
    """True for any address that must never be registered as an office IP in production.

    Uses `is_global` rather than `not is_private`, which also excludes ranges that are
    neither private nor globally routable:
      * CGNAT / shared address space  100.64.0.0/10   (is_private=False, is_global=False)
      * documentation ranges          192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24
      * reserved / benchmarking / link-local, loopback, private
    An unparseable value is reported as non-public (fail closed).
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return not addr.is_global
