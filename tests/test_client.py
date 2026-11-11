import asyncio
import os
import sys
import urllib.request
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from telethon.tl.functions.messages import GetForumTopicsByIDRequest, GetForumTopicsRequest
from telethon.tl.types import PeerUser, User

from tgc.client import proxy_spec, resolve, resolve_proxy, resolve_topic, system_proxies, topics

ANNA = User(id=1, first_name="Анна")
ANNA_S = User(id=2, first_name="Анна", last_name="Смирнова")
PETR = User(id=3, first_name="Пётр")


class FakeClient:
    def __init__(self, found=(), topic_pages=(), topics_by_id=()):
        self.found = found
        self.topic_pages = list(topic_pages)
        self.topics_by_id = list(topics_by_id)
        self._page = 0

    async def get_me(self):
        return "self"

    async def __call__(self, request):  # contacts.SearchRequest
        if isinstance(request, GetForumTopicsRequest):
            page = self.topic_pages[self._page] if self._page < len(self.topic_pages) else []
            self._page += 1
            return SimpleNamespace(topics=page)
        if isinstance(request, GetForumTopicsByIDRequest):
            return SimpleNamespace(topics=self.topics_by_id)
        return SimpleNamespace(users=list(self.found), chats=[], my_results=[PeerUser(u.id) for u in self.found])

    async def get_dialogs(self):
        return []


def test_me_is_saved_messages():
    assert asyncio.run(resolve(FakeClient(), "Me")) == "self"


def test_resolve_by_name():
    assert asyncio.run(resolve(FakeClient([ANNA, ANNA_S]), "анна")) is ANNA  # exact name wins
    assert asyncio.run(resolve(FakeClient([ANNA_S]), "Анна")) is ANNA_S  # one partial match is taken
    with pytest.raises(ToolError, match="несколько"):
        asyncio.run(resolve(FakeClient([ANNA_S, PETR]), "р"))
    with pytest.raises(ToolError, match="не найден"):
        asyncio.run(resolve(FakeClient(), "Анна"))


FORUM = SimpleNamespace(forum=True)  # get_display_name is only needed for a non-forum error
T_GENERAL = SimpleNamespace(id=1, title="General")
T_HUB = SimpleNamespace(id=2, title="GPT Model Hub")
T_HUB2 = SimpleNamespace(id=3, title="GPT Model Hub Backup")


def test_topics_pages():
    c = FakeClient(topic_pages=[[T_GENERAL, T_HUB], [T_HUB2], []])
    assert asyncio.run(topics(c, FORUM)) == [(1, "General"), (2, "GPT Model Hub"), (3, "GPT Model Hub Backup")]


def test_resolve_topic_by_id():
    c = FakeClient(topics_by_id=[T_HUB])
    assert asyncio.run(resolve_topic(c, FORUM, "2")) == (2, "GPT Model Hub")
    with pytest.raises(ToolError, match="не найден"):
        asyncio.run(resolve_topic(c, FORUM, "9"))


def test_resolve_topic_by_title():
    pages = [[T_GENERAL, T_HUB, T_HUB2]]
    assert asyncio.run(resolve_topic(FakeClient(topic_pages=pages), FORUM, "gpt model hub")) == (2, "GPT Model Hub")  # exact match wins
    assert asyncio.run(resolve_topic(FakeClient(topic_pages=pages), FORUM, "backup")) == (3, "GPT Model Hub Backup")
    with pytest.raises(ToolError, match="несколько"):
        asyncio.run(resolve_topic(FakeClient(topic_pages=pages), FORUM, "hub"))
    with pytest.raises(ToolError, match="не найден"):
        asyncio.run(resolve_topic(FakeClient(topic_pages=pages), FORUM, "xyz"))


def test_resolve_topic_not_a_forum():
    with pytest.raises(ToolError, match="не форум"):
        asyncio.run(resolve_topic(FakeClient(), ANNA, "hub"))


def test_proxy_spec_parses_url():
    assert proxy_spec("socks5://127.0.0.1:12334") == (
        {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 12334, "rdns": True}, None,
    )


def test_proxy_spec_with_credentials():
    spec, _ = proxy_spec("http://user:pass@127.0.0.1:8080")
    assert spec["proxy_type"] == "http" and spec["username"] == "user" and spec["password"] == "pass"


def test_proxy_spec_rejects_unknown_scheme():
    with pytest.raises(ToolError, match="ожидается"):
        proxy_spec("ftp://127.0.0.1:21")


def test_proxy_spec_none_when_unset():
    assert proxy_spec(None) == (None, None)


def test_proxy_spec_socks_means_socks5():
    assert proxy_spec("socks://127.0.0.1:1080")[0]["proxy_type"] == "socks5"


def test_proxy_spec_schemeless_means_https_connect():
    spec, _ = proxy_spec("127.0.0.1:8080")
    assert spec["proxy_type"] == "http" and spec["addr"] == "127.0.0.1" and spec["port"] == 8080


def test_proxy_spec_https_is_http_connect():
    assert proxy_spec("https://proxy:3128")[0]["proxy_type"] == "http"


def test_proxy_spec_mtproto():
    from telethon.network.connection import ConnectionTcpMTProxyAbridged
    assert proxy_spec("mtproto://127.0.0.1:443/ee1603dddeadbeef") == (
        ("127.0.0.1", 443, "ee1603dddeadbeef"), ConnectionTcpMTProxyAbridged,
    )


def test_proxy_spec_mtproto_secret_in_query():
    proxy, _ = proxy_spec("mtproto://127.0.0.1:443?secret=abc123")
    assert proxy == ("127.0.0.1", 443, "abc123")


def test_proxy_spec_mtproto_requires_secret():
    with pytest.raises(ToolError, match="mtproto"):
        proxy_spec("mtproto://127.0.0.1:443")


def test_resolve_proxy_unset_is_direct_even_with_system_proxy(monkeypatch):
    monkeypatch.setattr("tgc.client.system_proxies", lambda: [("http://system:1", "настройки прокси ОС")])
    assert resolve_proxy(None) == (None, None, "напрямую (proxy в config.toml не задан)")


def test_resolve_proxy_direct_disables_system_proxy(monkeypatch):
    monkeypatch.setattr("tgc.client.system_proxies", lambda: [("http://system:1", "настройки прокси ОС")])
    assert resolve_proxy("direct")[:2] == (None, None)
    assert resolve_proxy("none")[:2] == (None, None)


def test_resolve_proxy_auto_uses_system_proxy(monkeypatch):
    monkeypatch.setattr("tgc.client.system_proxies", lambda: [("http://system:1", "настройки прокси ОС")])
    proxy, connection, note = resolve_proxy("auto")
    assert proxy["addr"] == "system" and connection is None and "автопоиск" in note


def test_resolve_proxy_auto_skips_unparseable_values(monkeypatch):
    monkeypatch.setattr("tgc.client.system_proxies", lambda: [
        ("::плохое значение::", "переменная окружения https_proxy"),
        ("socks5://os:1080", "настройки прокси ОС"),
    ])
    proxy, _, note = resolve_proxy("auto")
    assert proxy["addr"] == "os" and "настройки прокси ОС" in note


def test_resolve_proxy_auto_without_findings_is_direct(monkeypatch):
    monkeypatch.setattr("tgc.client.system_proxies", lambda: [])
    assert resolve_proxy("auto") == (None, None, "напрямую (автопоиск: прокси не найден)")


def test_resolve_proxy_explicit_config():
    proxy, connection, note = resolve_proxy("socks5://config:2")
    assert proxy["addr"] == "config" and connection is None and "config.toml" in note


def test_resolve_proxy_masks_password_in_note():
    _, _, note = resolve_proxy("socks5://user:secret@127.0.0.1:1080")
    assert "secret" not in note and "user:***@" in note


def test_report_connection_once_per_process(capsys, monkeypatch):
    import tgc.client
    monkeypatch.setattr(tgc.client, "_reported", False)
    tgc.client.report_connection("напрямую")
    tgc.client.report_connection("через прокси")
    err = capsys.readouterr().err
    assert "напрямую" in err and err.count("Telegram") == 1


def test_system_proxies_env_vars_first(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies_environment",
                        lambda: {"no": "localhost", "https": "http://env:3"})
    assert system_proxies() == [("http://env:3", "переменная окружения https_proxy")]


def test_system_proxies_registry_checked_despite_no_proxy(monkeypatch):
    # только no_proxy в окружении: getproxies() на этом останавливается, system_proxies — нет
    monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: {"no": "localhost"})
    monkeypatch.setattr(urllib.request, "getproxies_registry",
                        lambda: {"http": "http://reg:4", "https": "http://reg:4"}, raising=False)
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(sys, "platform", "win32")
    assert system_proxies() == [("http://reg:4", "настройки прокси ОС")]


def test_system_proxies_macos_sysconf(monkeypatch):
    # macOS: системный прокси из System Configuration (Network → Proxies)
    monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: {})
    monkeypatch.setattr(urllib.request, "getproxies_macosx_sysconf",
                        lambda: {"https": "http://mac:6152"}, raising=False)
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert system_proxies() == [("http://mac:6152", "настройки прокси ОС")]


def test_system_proxies_macos_socks_only(monkeypatch):
    # macOS: включён только SOCKS-прокси — _scproxy отдаёт его как http://host:port
    monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: {})
    monkeypatch.setattr(urllib.request, "getproxies_macosx_sysconf",
                        lambda: {"socks": "http://127.0.0.1:1080"}, raising=False)
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert system_proxies() == [("socks5://127.0.0.1:1080", "настройки прокси ОС (SOCKS)")]


def test_system_proxies_none_when_nowhere(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies_environment", lambda: {})
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(sys, "platform", "linux")
    assert system_proxies() == []


def test_parallel_calls_queue_instead_of_busy(tmp_path, monkeypatch):
    """Agents run read-only tools in parallel; in one process they must wait, not fail on the file lock."""
    import tgc.client as client

    class Client:
        def __init__(self, *a, **kw): ...
        async def connect(self): ...
        async def is_user_authorized(self): return True
        async def disconnect(self): ...

    monkeypatch.setattr(client, "CONF_DIR", tmp_path)
    monkeypatch.setattr(client, "load_config", lambda: (1, "h", "direct"))
    monkeypatch.setattr(client, "TelegramClient", Client)

    async def call():
        async with client.telegram():
            await asyncio.sleep(0.01)

    async def both():
        await asyncio.gather(call(), call())

    asyncio.run(both())


def test_proxy_spec_decodes_percent_escapes():
    # «@» and «:» in a login or password must be written as %40 and %3A in the URL
    spec, _ = proxy_spec("socks5://us%3Aer:p%40ss@127.0.0.1:1080")
    assert spec["username"] == "us:er" and spec["password"] == "p@ss"
