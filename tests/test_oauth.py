import asyncio
import base64
import hashlib
import json
import os
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
import uvicorn

import tgc.client as client
import tgc.oauth as oauth
import tgc.server as server

PASSWORD = "correct horse battery"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def conf(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "CONF_DIR", tmp_path / ".tgc")
    return tmp_path / ".tgc"


# --- config and password -------------------------------------------------

def test_password_hash(conf):
    stored = oauth.hash_password(PASSWORD)
    assert PASSWORD not in stored and oauth.check_password(PASSWORD, stored)
    assert not oauth.check_password("wrong", stored)
    assert not oauth.check_password(PASSWORD, "garbage")


def test_set_password_keeps_config(conf):
    conf.mkdir()
    (conf / "config.toml").write_text('api_id = 1\napi_hash = "abc"\npassword_hash = "old"', encoding="utf-8")
    oauth.set_password(PASSWORD)
    text = (conf / "config.toml").read_text(encoding="utf-8")
    assert 'api_id = 1\napi_hash = "abc"\n' in text and "old" not in text and text.count("password_hash") == 1
    assert stat.S_IMODE((conf / "config.toml").stat().st_mode) == 0o600
    (conf / "config.toml").write_text(text + 'public_url = "https://tg.example.com/"\n', encoding="utf-8")
    url, stored = oauth.server_config()
    assert url == "https://tg.example.com" and oauth.check_password(PASSWORD, stored)


@pytest.mark.parametrize("url", ["", "http://tg.example.com", "https://tg.example.com/mcp", "tg.example.com"])
def test_server_config_refuses_bad_url(conf, url):
    conf.mkdir()
    (conf / "config.toml").write_text(f'public_url = "{url}"\npassword_hash = "x"\n', encoding="utf-8")
    with pytest.raises(SystemExit, match="public_url"):
        oauth.server_config()


def test_registration_is_capped(conf, monkeypatch):
    from mcp.shared.auth import OAuthClientInformationFull

    monkeypatch.setattr(oauth, "MAX_CLIENTS", 2)
    p = oauth.Provider("https://tg.example.com", oauth.hash_password(PASSWORD))
    for i in range(4):
        asyncio.run(p.register_client(OAuthClientInformationFull(client_id=f"c{i}", redirect_uris=["https://a.example/cb"])))
        if i == 0:
            with oauth.state(write=True) as s:
                p.issue(s, "c0")  # a client that holds a token is kept
    with oauth.state() as s:
        assert sorted(s["clients"]) == ["c0", "c3"]
    assert asyncio.run(p.get_client("c3")).client_id == "c3" and asyncio.run(p.get_client("c1")) is None


# --- through a running server --------------------------------------------

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def call(method, url, data=None, headers=None, as_json=False):
    body = json.dumps(data).encode() if as_json else urlencode(data).encode() if data is not None else None
    headers = {"Content-Type": "application/json" if as_json else "application/x-www-form-urlencoded", **(headers or {})}
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with OPENER.open(req, timeout=10) as r:
            return r.status, r.headers, r.read().decode()
    except HTTPError as e:
        return e.code, e.headers, e.read().decode()


@pytest.fixture
def live(conf):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    provider = oauth.Provider(url, oauth.hash_password(PASSWORD))
    app = server.build_http(provider).streamable_http_app(streamable_http_path="/mcp", host="127.0.0.1",
                                                          transport_security=provider.transport_security())
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield url, provider
    srv.should_exit = True
    thread.join(10)
    server.build()  # back to local mode for the other tests


def register(url, name="Claude"):
    status, _, body = call("POST", f"{url}/register", {
        "redirect_uris": ["http://localhost:9/cb"], "client_name": name, "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}, as_json=True)
    assert status == 201, body
    return json.loads(body)["client_id"]


def pkce():
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def authorize(url, client_id, challenge, resource=None):
    q = {"response_type": "code", "client_id": client_id, "redirect_uri": "http://localhost:9/cb", "state": "xyz",
         "code_challenge": challenge, "code_challenge_method": "S256", "resource": resource or f"{url}/mcp"}
    status, headers, _ = call("GET", f"{url}/authorize?{urlencode(q)}")
    assert status == 302
    return headers["Location"]


def login(url, client_id, challenge):
    """The password page for a fresh request, then a correct password: the code."""
    req = parse_qs(urlparse(authorize(url, client_id, challenge)).query)["req"][0]
    status, headers, _ = call("POST", f"{url}/login", {"req": req, "password": PASSWORD})
    assert status == 302
    return parse_qs(urlparse(headers["Location"]).query)["code"][0]


def token(url, **form):
    status, _, body = call("POST", f"{url}/token", form)
    return status, json.loads(body)


def mcp_status(url, access):
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    headers = {"Accept": "application/json, text/event-stream", **({"Authorization": f"Bearer {access}"} if access else {})}
    return call("POST", f"{url}/mcp", init, headers, as_json=True)[0]


def test_login_page_and_code_exchange(live):
    url, _ = live
    cid = register(url, "<b>Evil</b>")
    verifier, challenge = pkce()
    location = authorize(url, cid, challenge)
    assert location.startswith(f"{url}/login?req=")
    req = parse_qs(urlparse(location).query)["req"][0]

    status, headers, body = call("GET", location)
    assert status == 200 and headers["X-Frame-Options"] == "DENY"
    assert "&lt;b&gt;Evil&lt;/b&gt;" in body and "<b>Evil</b>" not in body and "http://localhost:9/cb" in body

    assert call("POST", f"{url}/login", {"req": req, "password": "wrong"})[0] == 401
    status, headers, _ = call("POST", f"{url}/login", {"req": req, "password": PASSWORD})
    assert status == 302
    back = urlparse(headers["Location"])
    assert back.netloc == "localhost:9" and parse_qs(back.query)["state"] == ["xyz"]
    code = parse_qs(back.query)["code"][0]
    assert call("POST", f"{url}/login", {"req": req, "password": PASSWORD})[0] == 400  # a request is used once

    base = {"grant_type": "authorization_code", "code": code, "redirect_uri": "http://localhost:9/cb", "client_id": cid}
    other = register(url, "Other")
    assert token(url, **base, code_verifier="wrong-verifier-" + "x" * 40)[0] == 400
    assert token(url, **{**base, "redirect_uri": "http://localhost:9/other"}, code_verifier=verifier)[0] == 400
    assert token(url, **{**base, "client_id": other}, code_verifier=verifier)[0] == 400
    status, tokens = token(url, **base, code_verifier=verifier)
    assert status == 200 and tokens["access_token"] and tokens["refresh_token"]
    assert token(url, **base, code_verifier=verifier)[0] == 400  # a code is used once

    assert mcp_status(url, None) == 401
    assert mcp_status(url, "made-up") == 401
    assert mcp_status(url, tokens["access_token"]) == 200


def test_other_resource_is_refused(live):
    url, _ = live
    cid = register(url)
    location = authorize(url, cid, pkce()[1], resource="http://other.example/mcp")
    assert "error=invalid_target" in location


def test_refresh_rotates(live):
    url, _ = live
    cid = register(url)
    verifier, challenge = pkce()
    code = login(url, cid, challenge)
    _, first = token(url, grant_type="authorization_code", code=code, redirect_uri="http://localhost:9/cb", client_id=cid,
                     code_verifier=verifier)
    status, second = token(url, grant_type="refresh_token", refresh_token=first["refresh_token"], client_id=cid)
    assert status == 200 and second["refresh_token"] != first["refresh_token"]
    assert token(url, grant_type="refresh_token", refresh_token=first["refresh_token"], client_id=cid)[0] == 400
    assert mcp_status(url, first["access_token"]) == 401 and mcp_status(url, second["access_token"]) == 200


def test_revoke_all_from_another_process(live, conf):
    url, provider = live
    cid = register(url)
    verifier, challenge = pkce()
    code = login(url, cid, challenge)
    _, tokens = token(url, grant_type="authorization_code", code=code, redirect_uri="http://localhost:9/cb", client_id=cid,
                      code_verifier=verifier)
    assert mcp_status(url, tokens["access_token"]) == 200

    env = {**os.environ, "HOME": str(conf.parent)}  # the CLI finds ~/.tgc there, as the test server does
    r = subprocess.run([sys.executable, "-m", "tgc.server", "--revoke-all"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    assert mcp_status(url, tokens["access_token"]) == 401
    assert token(url, grant_type="refresh_token", refresh_token=tokens["refresh_token"], client_id=cid)[0] in (400, 401)
    restarted = oauth.Provider(url, provider.password_hash)  # nothing in memory brings them back
    assert asyncio.run(restarted.load_access_token(tokens["access_token"])) is None
    assert stat.S_IMODE((conf / "oauth.json").stat().st_mode) == 0o600


def test_lockout_after_five_wrong_passwords(live):
    url, provider = live
    cid = register(url)
    req = parse_qs(urlparse(authorize(url, cid, pkce()[1])).query)["req"][0]
    for _ in range(oauth.MAX_FAILS):
        assert call("POST", f"{url}/login", {"req": req, "password": "wrong"})[0] == 401
    status, _, body = call("POST", f"{url}/login", {"req": req, "password": PASSWORD})
    assert status == 429 and "Вход закрыт до" in body
    provider.locked_until = 0  # what a restart or 15 minutes does
    assert call("POST", f"{url}/login", {"req": req, "password": PASSWORD})[0] == 302


def test_foreign_host_is_refused(live):
    """With a valid token, so the request gets past the bearer check to the host check."""
    url, _ = live
    cid = register(url)
    verifier, challenge = pkce()
    code = login(url, cid, challenge)
    _, tokens = token(url, grant_type="authorization_code", code=code, redirect_uri="http://localhost:9/cb", client_id=cid,
                      code_verifier=verifier)
    headers = {"Host": "evil.example", "Accept": "application/json, text/event-stream", "Authorization": f"Bearer {tokens['access_token']}"}
    status = call("POST", f"{url}/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers, as_json=True)[0]
    assert status in (403, 421)


def test_parallel_refresh_wins_once(live):
    url, _ = live
    cid = register(url)
    verifier, challenge = pkce()
    code = login(url, cid, challenge)
    _, tokens = token(url, grant_type="authorization_code", code=code, redirect_uri="http://localhost:9/cb", client_id=cid,
                      code_verifier=verifier)
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        token(url, grant_type="refresh_token", refresh_token=tokens["refresh_token"], client_id=cid)[0])) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(results)[0] == 200 and results.count(200) == 1
    with oauth.state() as s:
        assert len(s["refresh"]) == 1  # only the winner's refresh token is left
