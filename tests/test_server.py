import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from telethon.tl.types import (
    Chat, DialogFilter, DialogFilterDefault, Document, DocumentAttributeFilename, InputPeerChat, Message, MessageMediaDocument,
    PeerUser, TextWithEntities, User,
)
from telethon.utils import get_peer_id

import tgc.server as server

USER = User(id=1, first_name="Иван")


def doc(i, size):
    media = MessageMediaDocument(document=Document(i, 0, b"", None, "application/pdf", size, 2,
                                                   [DocumentAttributeFilename(f"{i}.pdf")]))
    return Message(i, PeerUser(1), None, "", media=media)


def test_fetch_media_refuses_over_50_mb(tmp_path, monkeypatch):
    """fetch_media needs no confirmation, so it must never download a big file: that is fetch_large_file, behind ask."""
    downloads = []

    class Client:
        async def get_messages(self, entity, ids):
            return [doc(1, 60_000_000), doc(2, 1000)]

        async def download_media(self, msg, file):
            downloads.append(msg.id)
            return file

    @asynccontextmanager
    async def telegram():
        yield Client()

    async def resolve(client, chat):
        return USER

    monkeypatch.setattr(server, "telegram", telegram)
    monkeypatch.setattr(server, "resolve", resolve)
    monkeypatch.setattr(server, "ARCHIVE", tmp_path)
    out = asyncio.run(server.fetch_media("Иван", [1, 2]))
    assert downloads == [2]
    assert "#1: 60.0 МБ, больше 50 МБ, не скачан" in out
    assert f"#2: {tmp_path / 'Иван__1' / 'media' / '2_2.pdf'}" in out  # the original name is kept


def fake_telegram(monkeypatch, client):
    @asynccontextmanager
    async def telegram():
        yield client

    async def resolve(client, chat):
        return USER

    monkeypatch.setattr(server, "telegram", telegram)
    monkeypatch.setattr(server, "resolve", resolve)


def test_forward_reports_skipped_instead_of_failing(monkeypatch):
    """Telethon returns None for a message it could not forward; the others are already sent, so no error and no retry."""
    class Client:
        async def forward_messages(self, dst, ids, from_peer):
            return [Message(10, PeerUser(1), None, ""), None]

    fake_telegram(monkeypatch, Client())
    out = asyncio.run(server.forward_messages("Иван", [1, 2], "me"))
    assert "#10" in out and "Не пересланы" in out and "#2" in out


def test_folder_by_rules_without_explicit_chats(monkeypatch):
    """A folder like «all groups» lists no chats explicitly; membership comes from its flags minus exclude_peers."""
    group, other, bot = Chat(5, "Группа", None, 0, 0, 0), Chat(6, "Другая", None, 0, 0, 0), User(id=7, first_name="Бот", bot=True)
    filt = DialogFilter(1, TextWithEntities("Groups", []), pinned_peers=[], include_peers=[],
                        exclude_peers=[InputPeerChat(6)], groups=True)

    class Client:
        async def __call__(self, request):
            return SimpleNamespace(filters=[DialogFilterDefault(), filt])

        async def get_peer_id(self, peer):
            return get_peer_id(peer)

        async def get_dialogs(self, limit):
            assert limit is None  # a folder looks through every chat, not the 50 most recent
            return [SimpleNamespace(entity=e, unread_count=0) for e in (group, other, bot, USER)]

    fake_telegram(monkeypatch, Client())
    out = asyncio.run(server.list_dialogs(folder="grou"))
    assert "Группа" in out and "Другая" not in out and "Бот" not in out and "Иван" not in out
    assert "не найдена" in asyncio.run(server.list_dialogs(folder="нет такой"))
