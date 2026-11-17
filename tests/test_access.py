"""labkit.access: the second lock behind Cloudflare Access."""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from labkit.access import AccessGuard, is_private

TEAM = "example.cloudflareaccess.com"
AUD = "a" * 64
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key=KEY, aud=AUD, iss=f"https://{TEAM}", exp_in=600, email="someone@example.com"):
    now = int(time.time())
    return jwt.encode({"aud": [aud], "iss": iss, "iat": now, "exp": now + exp_in, "email": email},
                      key, algorithm="RS256")


def whoami(request: Request):
    return PlainTextResponse(f"hello {request.state.access_email}")


def client(ip="203.0.113.7", team=TEAM, aud=AUD):
    app = Starlette(routes=[Route("/", whoami), Route("/healthz", whoami)])
    app.add_middleware(AccessGuard, team_domain=team, aud=aud, key_for=lambda _t: KEY.public_key(),
                       exempt_paths=("/healthz",))
    return TestClient(app, client=(ip, 50000))


def test_a_valid_token_through_the_tunnel_passes_with_the_email():
    r = client().get("/", headers={"Cf-Access-Jwt-Assertion": token(), "Cf-Ray": "x"})
    assert r.status_code == 200 and r.text == "hello someone@example.com"


@pytest.mark.parametrize("bad", [
    token(key=OTHER_KEY), token(aud="b" * 64), token(iss="https://evil.cloudflareaccess.com"),
    token(exp_in=-60), "not-a-jwt"])
def test_a_wrong_token_is_refused(bad):
    assert client().get("/", headers={"Cf-Access-Jwt-Assertion": bad}).status_code == 403


def test_the_tunnel_without_a_token_is_refused_even_from_a_private_address():
    """cloudflared sits on a docker network: its address is private, its headers give it away."""
    r = client(ip="172.20.0.5").get("/", headers={"Cf-Connecting-Ip": "198.51.100.1", "Cf-Ray": "x"})
    assert r.status_code == 403


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Real-Ip"])
def test_a_claimed_forwarded_lan_address_is_not_trusted(header):
    """uvicorn --proxy-headers would turn "X-Forwarded-For: 192.168.1.5" into the client address."""
    r = client(ip="192.168.1.5").get("/", headers={header: "192.168.1.5"})
    assert r.status_code == 403


def test_a_public_address_without_a_token_is_refused():
    assert client(ip="2a12:1234::5").get("/").status_code == 403


@pytest.mark.parametrize("ip", ["192.168.1.20", "100.101.102.103", "127.0.0.1", "172.18.0.1", "fe80::1"])
def test_the_lan_and_tailnet_pass_without_a_login(ip):
    r = client(ip=ip).get("/")
    assert r.status_code == 200 and r.text == "hello "


def test_without_settings_it_fails_closed_but_the_lan_still_works(monkeypatch):
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    app = Starlette(routes=[Route("/", whoami)])
    app.add_middleware(AccessGuard)
    assert TestClient(app, client=("8.8.8.8", 1)).get(
        "/", headers={"Cf-Access-Jwt-Assertion": token()}).status_code == 403
    assert TestClient(app, client=("192.168.1.20", 1)).get("/").status_code == 200


def test_exempt_paths_skip_the_check():
    assert client().get("/healthz").status_code == 200


def test_is_private():
    assert is_private("::ffff:192.168.1.9") and is_private("10.1.2.3")
    assert not is_private("8.8.8.8") and not is_private("testclient") and not is_private(None)
