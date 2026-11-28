import asyncio
from pathlib import Path
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


def test_archive_grep_unsynced_chat_creates_nothing(tmp_path, monkeypatch):
    fake_telegram(monkeypatch, object())
    monkeypatch.setattr(server, "ARCHIVE", tmp_path)
    assert "ещё нет в архиве" in asyncio.run(server.archive_grep("аванс", chat="Иван"))
    assert list(tmp_path.iterdir()) == []
    (tmp_path / "Иван__1").mkdir()
    (tmp_path / "Иван__1" / "2026-10.md").write_text("[2026-10-01 10:00] #2 Иван: аванс\n", encoding="utf-8")
    assert "#2 Иван: аванс" in asyncio.run(server.archive_grep("аванс", chat="Иван"))


def named_doc(i, name, size=1000):
    media = MessageMediaDocument(document=Document(i, 0, b"", None, "application/octet-stream", size, 2,
                                                   [DocumentAttributeFilename(name)]))
    return Message(i, PeerUser(1), None, "", media=media)


def test_modes_differ_only_where_planned():
    local, remote = server.build(), server.build(http=True)
    names = lambda srv: {t.name for t in asyncio.run(srv.list_tools())}
    assert names(local) - names(remote) == {"fetch_large_file"} and names(remote) <= names(local)
    assert "archive_read" in names(remote) and "archive_grep" in names(remote)
    text = server.instructions(True)
    assert str(server.ARCHIVE) not in text and "fetch_large_file" not in text and "archive_read" in text
    server.build()  # leave the module in local mode for the other tests


def test_fetch_media_over_http_attaches_images(tmp_path, monkeypatch):
    png = (Path(__file__).parent / "fixtures" / "ocr.png").read_bytes()
    msgs = [named_doc(i, f"{i}.png") for i in range(1, 8)] + [named_doc(8, "отчёт.pdf"), named_doc(9, "IMG.HEIC"),
                                                              named_doc(10, "big.png")]

    class Client:
        async def get_messages(self, entity, ids):
            by_id = {m.id: m for m in msgs}
            return [by_id.get(i) for i in ids]  # Telegram answers in the order asked

        async def download_media(self, msg, file):
            Path(file).write_bytes(png + (b"\0" * 6_000_000 if msg.id == 10 else b""))
            return file

    fake_telegram(monkeypatch, Client())
    monkeypatch.setattr(server, "ARCHIVE", tmp_path)
    monkeypatch.setattr(server, "HTTP", True)
    text, *images = asyncio.run(server.fetch_media("Иван", [10, 8, 9, 1, 2, 3, 4, 5, 6, 7]))
    assert len(images) == 5 and all(isinstance(i, server.Image) for i in images)
    assert images[0].to_image_content().mime_type == "image/png"
    assert f"#8: {Path('Иван__1') / 'media' / '8_отчёт.pdf'}" in text  # paths from the archive root, not the container
    assert "#8: не картинка" in text and "#9: HEIC" in text and "#10: не приложена: 6.0 МБ" in text
    assert "#6: не приложена: больше 5 картинок" in text and "#7: не приложена: больше 5" in text


def test_fetch_media_over_http_refuses_huge_pixels(tmp_path, monkeypatch):
    png = (Path(__file__).parent / "fixtures" / "ocr.png").read_bytes()

    class Client:
        async def get_messages(self, entity, ids):
            return [named_doc(1, "a.png")]

        async def download_media(self, msg, file):
            Path(file).write_bytes(png)
            return file

    fake_telegram(monkeypatch, Client())
    monkeypatch.setattr(server, "ARCHIVE", tmp_path)
    monkeypatch.setattr(server, "HTTP", True)
    monkeypatch.setattr(server, "MAX_PIXELS", 100)
    text, *images = asyncio.run(server.fetch_media("Иван", [1]))
    assert images == [] and "пикселей, слишком большая" in text
