"""Cloudflare Access login check for a lab web app published through the tunnel.

A private site sits behind Cloudflare Access: the
edge asks for the login (e.g. an emailed one-time code) and then sends every request through the tunnel
with a signed token (``Cf-Access-Jwt-Assertion``). Cloudflare alone already enforces that;
this middleware is the second lock, so a request that did not come through the login is
refused even if it reaches the app (a misrouted hostname, a direct hit on the host).

Rule, per HTTP request:

- it carries Cloudflare headers (it came through the tunnel), or its source address is not
  private (loopback, RFC 1918, the tailnet, link-local, ULA): a valid Access token is
  required — RS256, signed by the team's keys, this app's AUD tag, the team as issuer;
- otherwise (the LAN, the tailnet, the host itself, other containers) it passes, as before.

Without settings (``CF_ACCESS_TEAM_DOMAIN`` / ``CF_ACCESS_AUD`` unset) nothing can be
verified, so every request that would need a token is refused: fail closed.

    from labkit.access import AccessGuard
    app.add_middleware(AccessGuard)            # settings from the environment

The verified email is put in ``request.state.access_email`` ("" on the LAN).
Needs the ``access`` extra (PyJWT with crypto).
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
from typing import Any, Callable, Iterable, Optional

CF_HEADERS = (b"cf-access-jwt-assertion", b"cf-connecting-ip", b"cf-ray")
TOKEN_HEADER = b"cf-access-jwt-assertion"
PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
    "::1/128", "fe80::/10", "fc00::/7"))


def is_private(host: Optional[str]) -> bool:
    """True for loopback, RFC 1918, the tailnet (100.64/10), link-local and ULA addresses."""
    try:
        addr = ipaddress.ip_address((host or "").split("%")[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in PRIVATE_NETS)


class TokenError(Exception):
    pass


class Verifier:
    """Checks an Access token against the team's public keys (fetched once an hour)."""

    def __init__(self, team_domain: str, aud: str, key_for: Optional[Callable[[str], Any]] = None):
        import jwt

        self.team_domain = team_domain.removeprefix("https://").rstrip("/")
        self.aud = aud
        self._jwt = jwt
        if key_for is None:
            client = jwt.PyJWKClient(f"https://{self.team_domain}/cdn-cgi/access/certs",
                                     cache_keys=True, lifespan=3600, timeout=10)
            key_for = lambda token: client.get_signing_key_from_jwt(token).key  # noqa: E731
        self._key_for = key_for

    def email(self, token: str) -> str:
        """The verified identity's email; raises TokenError on anything wrong."""
        try:
            claims = self._jwt.decode(token, self._key_for(token), algorithms=["RS256"],
                                      audience=self.aud, issuer=f"https://{self.team_domain}",
                                      options={"require": ["exp", "iat", "aud", "iss"]})
        except Exception as exc:  # noqa: BLE001 - any failure is a refusal
            raise TokenError(str(exc)) from exc
        return str(claims.get("email") or claims.get("common_name") or "")


class AccessGuard:
    """ASGI middleware applying the rule above to every HTTP request."""

    def __init__(self, app, team_domain: Optional[str] = None, aud: Optional[str] = None,
                 key_for: Optional[Callable[[str], Any]] = None,
                 exempt_paths: Iterable[str] = ()):
        self.app = app
        team_domain = team_domain if team_domain is not None else os.environ.get("CF_ACCESS_TEAM_DOMAIN", "")
        aud = aud if aud is not None else os.environ.get("CF_ACCESS_AUD", "")
        self.verifier = Verifier(team_domain, aud, key_for) if team_domain and aud else None
        self.exempt_paths = tuple(exempt_paths)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scope.setdefault("state", {})
        if scope.get("path", "") in self.exempt_paths:
            scope["state"]["access_email"] = ""
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        client = (scope.get("client") or ("", 0))[0]
        if not any(h in headers for h in CF_HEADERS) and is_private(client):
            scope["state"]["access_email"] = ""
            await self.app(scope, receive, send)
            return
        token = headers.get(TOKEN_HEADER, b"").decode("latin-1")
        if not token or self.verifier is None:
            await _forbidden(send)
            return
        try:
            email = await asyncio.to_thread(self.verifier.email, token)
        except TokenError:
            await _forbidden(send)
            return
        scope["state"]["access_email"] = email
        await self.app(scope, receive, send)


async def _forbidden(send) -> None:
    body = b"Forbidden"
    await send({"type": "http.response.start", "status": 403,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
