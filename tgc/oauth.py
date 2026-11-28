"""OAuth for the server mode: one owner, a password page, state in ~/.tgc/oauth.json.

The protocol (metadata, client registration, /authorize, /token, PKCE, bearer checks) is the SDK's;
this module stores clients and tokens and asks for the password.
"""

import base64
import fcntl  # the server mode runs on Linux or macOS: no Windows locking here
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
import tomllib
from contextlib import contextmanager
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, RefreshToken, TokenError, construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

import tgc.client as client  # CONF_DIR read at call time: tests and the CLI point it elsewhere

ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CODE_TTL = 300
LOGIN_TTL = 300
MAX_FAILS = 5
LOCKOUT_S = 900
MAX_CLIENTS = 50  # registration is open to anyone: past this, clients that hold no token are dropped, oldest first


# --- config and password -------------------------------------------------

def config_path():
    return client.CONF_DIR / "config.toml"


def server_config() -> tuple[str, str]:
    """(public_url, password_hash) for the server mode, or SystemExit with what to fix."""
    path = config_path()
    conf = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    url = str(conf.get("public_url", "")).rstrip("/")
    p = urlparse(url)
    if not (p.scheme == "https" or (p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1"))) or not p.hostname or p.path:
        raise SystemExit(f'В {path} нужен public_url = "https://tg.example.com" — адрес сервера без пути')
    if not conf.get("password_hash"):
        raise SystemExit("Пароль не задан: python -m tgc.server --set-password")
    return url, conf["password_hash"]


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$16384$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(key).decode()


def check_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, key = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p))
    except ValueError:  # a malformed hash refuses every password
        return False
    return hmac.compare_digest(got, base64.b64decode(key))


def set_password(password: str):
    """Writes password_hash into config.toml, keeping the rest of the file."""
    path = config_path()
    client.CONF_DIR.mkdir(mode=0o700, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    text = re.sub(r"(?m)^password_hash\s*=.*\n?", "", text)
    text = (text if not text or text.endswith("\n") else text + "\n") + f'password_hash = "{hash_password(password)}"\n'
    write_private(path, text)


def write_private(path, text: str):
    """Atomic replace of a 0600 file: a reader sees the old file or the new one, never half of it."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# --- stored state --------------------------------------------------------

def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@contextmanager
def state(write=False):
    """oauth.json under flock, read on every use: no token is cached in memory, so --revoke-all from another
    process takes effect on the next request, and a refresh cannot write back what was revoked."""
    client.CONF_DIR.mkdir(mode=0o700, exist_ok=True)
    path = client.CONF_DIR / "oauth.json"
    with open(client.CONF_DIR / "oauth.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # held for a file read or write: short enough to block the event loop
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        for k in ("clients", "access", "refresh"):
            data.setdefault(k, {})
        yield data
        if write:
            now = time.time()
            for k in ("access", "refresh"):
                data[k] = {h: t for h, t in data[k].items() if t["expires_at"] > now}
            write_private(path, json.dumps(data, ensure_ascii=False, indent=1))


def revoke_all():
    """Forgets every client and token; the running server reads the file on each request."""
    with state(write=True) as s:
        for k in s:
            s[k] = {}


# --- the provider --------------------------------------------------------

class Provider:
    def __init__(self, public_url: str, password_hash: str):
        self.url = public_url
        self.resource = f"{public_url}/mcp"
        self.password_hash = password_hash
        self.pending: dict[str, tuple[OAuthClientInformationFull, AuthorizationParams, float]] = {}  # /login requests
        self.codes: dict[str, AuthorizationCode] = {}
        self.fails, self.locked_until = 0, 0.0  # in memory: a restart lifts a lockout

    def settings(self) -> AuthSettings:
        return AuthSettings(issuer_url=self.url, resource_server_url=self.resource, validate_token_resource=True,
                            client_registration_options=ClientRegistrationOptions(enabled=True),
                            revocation_options=RevocationOptions(enabled=True))

    def transport_security(self) -> TransportSecuritySettings:
        """The public host only: a DNS-rebound name pointing at the server is refused."""
        host = urlparse(self.url).netloc
        return TransportSecuritySettings(allowed_hosts=[host], allowed_origins=[self.url])

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with state() as s:
            c = s["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(c) if c else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with state(write=True) as s:
            s["clients"][client_info.client_id] = client_info.model_dump(mode="json", exclude_none=True) | {"registered_at": time.time()}
            if len(s["clients"]) > MAX_CLIENTS:
                used = {t["client_id"] for k in ("access", "refresh") for t in s[k].values()}
                idle = sorted((c.get("registered_at", 0), cid) for cid, c in s["clients"].items() if cid not in used)
                for _, cid in idle[: len(s["clients"]) - MAX_CLIENTS]:
                    del s["clients"][cid]

    async def authorize(self, client_info: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self.resource:
            raise AuthorizeError("invalid_target", f"этот сервер выдаёт доступ только к {self.resource}")
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if v[2] > now}
        req = secrets.token_urlsafe(32)
        self.pending[req] = (client_info, params, now + LOGIN_TTL)
        return f"{self.url}/login?req={req}"

    async def login(self, request: Request) -> Response:
        """GET shows who asks and where the code goes; POST checks the password once per request."""
        form = await request.form() if request.method == "POST" else request.query_params
        req = str(form.get("req", ""))
        found = self.pending.get(req)
        if not found or found[2] < time.time():
            return page("Запрос входа устарел или уже использован. Начните подключение в приложении заново.", 400)
        client_info, params, _ = found
        if request.method == "GET":
            return login_form(req, client_info, params)
        now = time.time()
        if now < self.locked_until:
            return page(f"Слишком много неверных паролей. Вход закрыт до {time.strftime('%H:%M', time.localtime(self.locked_until))}.", 429)
        if not check_password(str(form.get("password", "")), self.password_hash):
            self.fails += 1
            if self.fails >= MAX_FAILS:
                self.fails, self.locked_until = 0, now + LOCKOUT_S
            return login_form(req, client_info, params, "Неверный пароль.", 401)
        self.fails = 0
        del self.pending[req]  # one password, one code: the request is used up
        code = secrets.token_urlsafe(32)
        self.codes = {k: c for k, c in self.codes.items() if c.expires_at > now}
        self.codes[code] = AuthorizationCode(
            code=code, scopes=params.scopes or [], expires_at=now + CODE_TTL, client_id=client_info.client_id,
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=self.resource)
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), 302)

    async def load_authorization_code(self, client_info, authorization_code: str) -> AuthorizationCode | None:
        code = self.codes.get(authorization_code)
        return code if code and code.client_id == client_info.client_id else None

    async def exchange_authorization_code(self, client_info, authorization_code: AuthorizationCode) -> OAuthToken:
        if self.codes.pop(authorization_code.code, None) is None:  # single use, even if two exchanges race
            raise TokenError("invalid_grant", "код уже использован")
        with state(write=True) as s:
            return self.issue(s, client_info.client_id)

    def issue(self, s: dict, client_id: str) -> OAuthToken:
        access, refresh, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), time.time()
        s["access"][digest(access)] = {"client_id": client_id, "expires_at": now + ACCESS_TTL, "refresh": digest(refresh)}
        s["refresh"][digest(refresh)] = {"client_id": client_id, "expires_at": now + REFRESH_TTL, "access": digest(access)}
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL, refresh_token=refresh)

    async def load_refresh_token(self, client_info, refresh_token: str) -> RefreshToken | None:
        with state() as s:
            t = s["refresh"].get(digest(refresh_token))
        if not t or t["client_id"] != client_info.client_id or t["expires_at"] < time.time():
            return None
        return RefreshToken(token=refresh_token, client_id=t["client_id"], scopes=[], expires_at=int(t["expires_at"]),
                            resource=self.resource)

    async def exchange_refresh_token(self, client_info, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        with state(write=True) as s:  # checked again under the lock: revoked or used since load_refresh_token
            t = s["refresh"].pop(digest(refresh_token.token), None)
            if t is None:
                raise TokenError("invalid_grant", "токен отозван или уже использован")
            s["access"].pop(t["access"], None)
            return self.issue(s, client_info.client_id)

    async def load_access_token(self, token: str) -> AccessToken | None:
        with state() as s:
            t = s["access"].get(digest(token))
        if not t or t["expires_at"] < time.time():
            return None
        return AccessToken(token=token, client_id=t["client_id"], scopes=[], expires_at=int(t["expires_at"]), resource=self.resource)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with state(write=True) as s:
            for kind, pair in (("access", "refresh"), ("refresh", "access")):
                t = s[kind].pop(digest(token.token), None)
                if t:
                    s[pair].pop(t[pair], None)


# --- pages ---------------------------------------------------------------

def page(body: str, status: int = 200) -> HTMLResponse:
    html_text = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                 "<title>tg-scribe</title><body style='font:16px system-ui;max-width:28em;margin:2em auto;padding:0 1em'>"
                 f"{body}</body>")
    # no framing: a page that takes the password must not sit inside someone else's site
    return HTMLResponse(html_text, status, headers={"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'",
                                                    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def login_form(req: str, client_info: OAuthClientInformationFull, params: AuthorizationParams, error: str = "",
               status: int = 200) -> HTMLResponse:
    """The client names itself at registration, so the name is escaped and the decision rests on the exact return address."""
    name = html.escape(client_info.client_name or client_info.client_id)
    redirect = html.escape(str(params.redirect_uri))
    err = f"<p style='color:#b00'>{html.escape(error)}</p>" if error else ""
    return page(f"<h1 style='font-size:1.3em'>Доступ к вашему Telegram</h1>"
                f"<p>Клиент <b>{name}</b> просит доступ. После входа код уйдёт на адрес:</p>"
                f"<p><code style='word-break:break-all'>{redirect}</code></p>"
                "<p>Если вы не подключали этот клиент или адрес не тот — закройте страницу.</p>"
                f"{err}<form method=post><input type=hidden name=req value='{html.escape(req, quote=True)}'>"
                "<input type=password name=password autofocus autocomplete=current-password style='width:100%;font:inherit;padding:.4em'>"
                "<p><button style='font:inherit;padding:.4em 1em'>Войти</button></p></form>", status)
