import asyncio
import os
import sys
import tomllib
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from mcp.server.mcpserver.exceptions import ToolError
from telethon import TelegramClient
from telethon.errors import RPCError
from telethon.network.connection import ConnectionTcpMTProxyAbridged
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.functions.messages import GetForumTopicsByIDRequest, GetForumTopicsRequest
from telethon.utils import get_display_name, get_peer_id

CONF_DIR = Path.home() / ".tgc"
SESSION = CONF_DIR / "tgc"  # Telethon adds .session

if os.name == "nt":  # Windows: блокировка байта через msvcrt, на POSIX — flock
    import msvcrt

    def _lock_file(f):
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
else:
    import fcntl

    def _lock_file(f):
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)

# Agents call read-only tools in parallel: inside one process they queue here
# instead of failing on the file lock, which is only for other processes.
_busy = asyncio.Lock()


def load_config():
    path = CONF_DIR / "config.toml"
    if not path.exists():
        raise ToolError(
            f"Нет {path}. Создай его с api_id и api_hash из https://my.telegram.org/apps:\n"
            'api_id = 12345\napi_hash = "abc..."'
        )
    conf = tomllib.loads(path.read_text(encoding="utf-8"))
    return int(conf["api_id"]), conf["api_hash"], conf.get("proxy")


def proxy_spec(url):
    """URL прокси → (proxy, connection) для TelegramClient; (None, None) — напрямую.

    Схема необязательна: '127.0.0.1:8080' — то же, что 'https://127.0.0.1:8080'.
    Поддерживаются socks5://, socks4://, http://, https:// (HTTP CONNECT; TLS до
    самого прокси python-socks не умеет), с логином и паролем — спецсимволы в
    %-кодировке: p%40ss = пароль p@ss; и mtproto://хост:порт/секрет (или ?секрет=)."""
    if not url:
        return None, None
    url = url.strip()
    if "://" not in url:
        url = "https://" + url  # схема не указана — по умолчанию https (HTTP CONNECT)
    p = urlparse(url)
    scheme = p.scheme.lower()
    if scheme == "socks":  # версия SOCKS не указана — считаем SOCKS5
        scheme = "socks5"
    if scheme == "mtproto":
        secret = unquote(p.path.lstrip("/")) or unquote(parse_qs(p.query).get("secret", [""])[0])
        if not p.hostname or not p.port or not secret:
            raise ToolError(f"proxy = {url!r}: ожидается mtproto://хост:порт/секрет")
        return (p.hostname, p.port, secret), ConnectionTcpMTProxyAbridged
    if scheme == "https":  # python-socks: HTTP CONNECT, шифрование до прокси не поддерживается
        scheme = "http"
    if scheme not in ("socks5", "socks4", "http") or not p.hostname or not p.port:
        raise ToolError(
            f"proxy = {url!r}: ожидается socks5://хост:порт, socks4://хост:порт, http(s)://хост:порт, "
            "mtproto://хост:порт/секрет или просто хост:порт"
        )
    spec = {"proxy_type": scheme, "addr": p.hostname, "port": p.port, "rdns": True}
    if p.username:
        spec["username"] = unquote(p.username)  # p%40ss in a URL is the password p@ss
    if p.password:
        spec["password"] = unquote(p.password)
    return spec, None


def mask_credentials(url):
    """URL прокси с замаскированным паролем — для вывода в лог и ошибки."""
    p = urlparse(url if "://" in url else "https://" + url)
    if not p.hostname:
        return url
    auth = f"{unquote(p.username)}:***@" if p.username else ""
    port = f":{p.port}" if p.port else ""
    tail = f"/{unquote(p.path.lstrip('/'))}" if p.path.strip("/") else ""
    return f"{p.scheme}://{auth}{p.hostname}{port}{tail}"


def _os_proxies():
    """Системные настройки прокси ОС: реестр Windows или System Configuration macOS.
    {} — Linux, настройки недоступны или не заданы."""
    if sys.platform == "darwin":
        reader = getattr(urllib.request, "getproxies_macosx_sysconf", None)
    elif os.name == "nt":
        reader = getattr(urllib.request, "getproxies_registry", None)
    else:
        reader = None
    if reader is None:
        return {}
    try:
        return reader() or {}
    except Exception:  # настройки ОС не читаются — подключаемся напрямую
        return {}


def system_proxies():
    """[(url, источник), ...] — прокси из переменных окружения и настроек ОС, по убыванию приоритета.

    urllib.request.getproxies() не подходит: если задана только no_proxy, он возвращает
    {'no': ...} — словарь истинен, и системные настройки не проверяются вовсе."""
    found, seen = [], set()

    def add(url, source):
        if url and url not in seen:
            seen.add(url)
            found.append((url, source))

    env = urllib.request.getproxies_environment()
    for scheme in ("https", "http"):
        add(env.get(scheme), f"переменная окружения {scheme}_proxy")
    os_proxies = _os_proxies()
    for scheme in ("https", "http"):
        add(os_proxies.get(scheme), "настройки прокси ОС")
    socks = os_proxies.get("socks")  # macOS: включён только SOCKS-прокси, без HTTP
    if socks:
        # SystemConfiguration не указывает версию SOCKS и отдаёт значение как http://host:port
        p = urlparse(socks)
        if p.hostname and p.port:
            add(f"socks5://{p.hostname}:{p.port}", "настройки прокси ОС (SOCKS)")
    return found


def resolve_proxy(configured):
    """proxy из config.toml → (proxy, connection, описание) для TelegramClient.

    Не задан или 'direct'/'none'/'off' — напрямую; 'auto' — автопоиск (переменные
    окружения, затем настройки ОС), без находки — напрямую; иначе URL прокси
    (см. proxy_spec). Описание — для вывода используемого метода подключения."""
    if configured:
        configured = configured.strip()
    if not configured:
        return None, None, "напрямую (proxy в config.toml не задан)"
    if configured.lower() in ("direct", "none", "off"):
        return None, None, "напрямую (proxy = direct)"
    if configured.lower() == "auto":
        for url, source in system_proxies():
            try:
                proxy, connection = proxy_spec(url)
            except ToolError:
                continue  # значение из окружения или ОС не разобрано — пробуем следующее
            return proxy, connection, f"через прокси {mask_credentials(url)} (автопоиск: {source})"
        return None, None, "напрямую (автопоиск: прокси не найден)"
    proxy, connection = proxy_spec(configured)
    kind = "MTProto-прокси" if connection else "прокси"
    return proxy, connection, f"через {kind} {mask_credentials(configured)} (из config.toml)"


_reported = False


def report_connection(note):
    """Один раз на процесс сообщить в stderr, как сервер подключается к Telegram."""
    global _reported
    if not _reported:
        print(f"Telegram: подключаюсь {note}", file=sys.stderr)
        _reported = True


@asynccontextmanager
async def telegram(login=False):
    """One Telegram connection per call: every agent session runs its own server process,
    and two processes on one SQLite session file fail with `database is locked`.
    login=True — подключение без проверки входа: так tgc.login сам ведёт QR-поток авторизации."""
    async with _busy:
        CONF_DIR.mkdir(mode=0o700, exist_ok=True)
        with open(CONF_DIR / "tgc.lock", "w") as lock:
            try:
                _lock_file(lock)
            except OSError:  # BlockingIOError (POSIX) / PermissionError (Windows): занято
                raise ToolError("Telegram занят другой сессией, повтори позже") from None
            api_id, api_hash, configured_proxy = load_config()
            proxy, connection, note = resolve_proxy(configured_proxy)
            report_connection(note)
            # Longer FloodWaits are raised, so a call never sleeps past its time budget.
            kwargs = {"proxy": proxy}
            if connection:  # MTProto-прокси: класс соединения и кортеж (хост, порт, секрет)
                kwargs["connection"] = connection
            client = TelegramClient(str(SESSION), api_id, api_hash, flood_sleep_threshold=30, **kwargs)
            try:
                await client.connect()
                if not login and not await client.is_user_authorized():
                    raise ToolError("Нет входа в Telegram: выполни `uv run python -m tgc.login`")
                yield client
            except ConnectionError as e:
                if proxy:  # прокси не сработал — прямое подключение могло работать
                    raise ToolError(
                        f"Telegram {note}: {e}. Если прокси не нужен — proxy = \"direct\" в config.toml"
                    ) from e
                raise ToolError(f"Telegram: {e}") from e
            except RPCError as e:  # MCP hides the text of other exceptions from Claude
                raise ToolError(f"Telegram: {e}") from e
            finally:
                await client.disconnect()


async def resolve(client, chat: str):
    """Chat by marked id, @username, part of its title, or «me» for Saved Messages."""
    if chat.lower() == "me":
        return await client.get_me()
    if chat.lstrip("-").isdigit() or chat.startswith("@"):
        key = int(chat) if chat.lstrip("-").isdigit() else chat
        try:
            return await client.get_entity(key)
        except ValueError:
            await client.get_dialogs()  # fills the entity cache, needed for bare ids
            try:
                return await client.get_entity(key)
            except ValueError:
                raise ToolError(f"Чат «{chat}» не найден") from None
    found = await search(client, chat)
    if not found:  # search misses some non-contact chats; the full scan is slow (~1 min on 5k chats)
        found = [d.entity for d in await client.get_dialogs() if chat.lower() in d.name.lower()]
    matches = [e for e in found if get_display_name(e).lower() == chat.lower()] or found
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ToolError(f"Чат «{chat}» не найден")
    rows = "\n".join(f"{get_peer_id(e)}\t{get_display_name(e)}" for e in matches[:20])
    raise ToolError(f"Под «{chat}» подходит несколько чатов, укажи id:\n{rows}")


async def search(client, query: str):
    """Server-side name search among the user's own chats: one request instead of paging all dialogs."""
    r = await client(SearchRequest(q=query, limit=50))
    by_id = {get_peer_id(e): e for e in r.users + r.chats}
    return [by_id[get_peer_id(p)] for p in r.my_results if get_peer_id(p) in by_id]


async def topics(client, entity) -> list[tuple[int, str]]:
    """Топики форума: (id, название), постранично."""
    out, seen, offset = [], set(), 0
    while True:
        r = await client(GetForumTopicsRequest(peer=entity, offset_date=0, offset_id=0, offset_topic=offset, limit=100))
        fresh = [(t.id, t.title) for t in r.topics if hasattr(t, "title") and t.id not in seen]
        if not fresh:
            return out
        out += fresh
        seen |= {i for i, _ in fresh}
        offset = fresh[-1][0]


async def resolve_topic(client, entity, topic: str):
    """Топик форума по id или части названия -> (id, название)."""
    if not getattr(entity, "forum", False):
        raise ToolError(f"«{get_display_name(entity)}» — не форум, топиков нет")
    if topic.lstrip("-").isdigit():
        want = int(topic)
        r = await client(GetForumTopicsByIDRequest(peer=entity, topics=[want]))
        for t in r.topics:
            if getattr(t, "id", None) == want and hasattr(t, "title"):
                return want, t.title
        raise ToolError(f"Топик #{want} не найден, вызови list_topics")
    found = await topics(client, entity)
    matches = [ft for ft in found if topic.lower() in ft[1].lower()]
    exact = [ft for ft in matches if ft[1].lower() == topic.lower()] or matches
    if len(exact) == 1:
        return exact[0]
    if not exact:
        raise ToolError(f"Топик «{topic}» не найден. Топики:\n" + "\n".join(f"{i}\t{t}" for i, t in found))
    raise ToolError(f"Под «{topic}» подходит несколько топиков, укажи id:\n" + "\n".join(f"{i}\t{t}" for i, t in exact[:20]))
