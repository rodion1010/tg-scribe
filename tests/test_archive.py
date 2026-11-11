import asyncio
import io
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tgc.archive import (
    HEADER, Unreadable, append, chat_dir, describe, download_large, extract_text, format_line, id_range, month_files, ocr_photos,
    pending_ids, read_files, recognize, set_media, sync_chat, topic_dir, update_block,
)
from telethon.tl.types import (
    Document, DocumentAttributeAudio, DocumentAttributeFilename, Message, MessageFwdHeader, MessageMediaDocument,
    MessageMediaPhoto, MessageReplyHeader, PeerUser, Photo, User,
)

D = datetime(2026, 10, 31, 14, 2)


def test_format_line():
    assert format_line(D, 5, "Иван", "привет") == "[2026-10-31 14:02] #5 Иван: привет\n"
    assert (
        format_line(D, 6, "Иван\nПетров", "a\nb", reply_to=5, tags=["[фото]"])
        == "[2026-10-31 14:02] #6 Иван Петров ↩5 [фото]: a\n  b\n"
    )
    assert format_line(D, 7, "Я", None, tags=["[фото]"]) == "[2026-10-31 14:02] #7 Я [фото]: \n"


def test_last_id_ignores_cut_line_and_hash_in_name(tmp_path):
    append(tmp_path, "2026-09", [format_line(D, 10, "Команда #5", "x")])
    append(tmp_path, "2026-10", [format_line(D, 11, "Иван", "#99 текст\n[2026-10-31 14:02] #98 не заголовок")])
    assert id_range(tmp_path)[1] == 11
    with open(tmp_path / "2026-10.md", "a") as f:
        f.write("[2026-10-31 14:03] #4")  # crash mid-write
    assert id_range(tmp_path)[1] == 11
    append(tmp_path, "2026-10", [format_line(D, 12, "Иван", "y")])
    assert (tmp_path / "2026-10.md").read_text().count("#4") == 0
    assert id_range(tmp_path)[1] == 12


def test_last_id_falls_back_to_previous_month(tmp_path):
    append(tmp_path, "2026-09", [format_line(D, 10, "Иван", "x")])
    (tmp_path / "2026-10.md").write_text("[2026-10-01 00:01] #11 Ив")
    assert id_range(tmp_path)[1] == 10
    (tmp_path / "empty").mkdir()
    assert id_range(tmp_path / "empty")[1] == 0


def test_chat_dir_survives_rename(tmp_path):
    first = chat_dir(tmp_path, -100123, "Старое имя")
    assert chat_dir(tmp_path, -100123, "Новое имя") == first
    assert chat_dir(tmp_path, 123, "Другой") != first


def test_topic_dir_inside_chat_and_survives_rename(tmp_path):
    chat = chat_dir(tmp_path, -100123, "Чат")
    t = topic_dir(tmp_path, -100123, "Чат", 5, "GPT Model Hub")
    assert t == chat / "GPT-Model-Hub__5"  # пробел — в слаг, папка внутри папки чата
    assert topic_dir(tmp_path, -100123, "Чат", 5, "Переименованный") == t  # находится по id
    assert topic_dir(tmp_path, -100123, "Чат", 6, "Другой") != t


def test_update_block_in_previous_month(tmp_path):
    append(tmp_path, "2026-09", [format_line(D, 10, "Иван", "[голосовое 0:05, не расшифровано]\nхвост"),
                                 format_line(D, 100, "Иван", "после")])
    append(tmp_path, "2026-10", [format_line(D, 11, "Иван", "z")])
    assert update_block(tmp_path, 10, lambda _: format_line(D, 10, "Иван", "[голосовое 0:05] «текст»"))
    text = (tmp_path / "2026-09.md").read_text()
    assert "«текст»" in text and "хвост" not in text and "#100 Иван: после" in text
    assert not update_block(tmp_path, 999, lambda b: b)


def test_photo_marks(tmp_path):
    append(tmp_path, "2026-09", [format_line(D, 1, "Иван", "старое", tags=["[фото]"])])
    append(tmp_path, "2026-10", [
        format_line(D, 2, "Иван", "подпись\nвторая", tags=["[фото]"]),
        format_line(D, 3, "Иван", "текст про [фото] в сообщении"),
        format_line(D, 4, "Иван", "", tags=["[фото]"]),
    ])
    assert pending_ids(tmp_path, "фото") == [4, 2, 1]
    assert update_block(tmp_path, 2, lambda b: set_media(b, ", с текстом", ["Deploy failed", "exit 137"]))
    assert update_block(tmp_path, 4, lambda b: set_media(b, ", без текста"))
    assert pending_ids(tmp_path, "фото") == [1]
    text = (tmp_path / "2026-10.md").read_text()
    assert ("#2 Иван [фото, с текстом]: подпись\n  вторая\n  [на фото] Deploy failed\n  [на фото] exit 137\n#"
            in text.replace("[2026-10-31 14:02] ", ""))
    assert update_block(tmp_path, 2, lambda b: set_media(b, ": схема деплоя"))
    text = (tmp_path / "2026-10.md").read_text()
    assert "#2 Иван [фото, с текстом: схема деплоя]: подпись" in text and "[на фото] exit 137" in text
    assert update_block(tmp_path, 2, lambda b: set_media(b, ": новая схема"))  # a description has ": " inside
    assert "#2 Иван [фото, с текстом: новая схема]: подпись" in (tmp_path / "2026-10.md").read_text()
    assert id_range(tmp_path)[1] == 4
    with pytest.raises(ValueError):
        set_media(format_line(D, 5, "Иван", "просто текст"), ": x")
    assert "Ivan: work [фото, без текста]" in set_media(format_line(D, 6, "Ivan: work", "", tags=["[фото]"]), ", без текста")
    append(tmp_path, "2026-10", [format_line(D, 7, "Иван", "", tags=["[фото: схема]"])])  # described before states were kept
    assert pending_ids(tmp_path, "фото") == [1]  # OCR again would duplicate its [на фото] lines


def test_ocr_photos_skips_processed(tmp_path):
    class Client:
        requested = []

        async def get_messages(self, entity, ids):
            self.requested += ids
            return [None] * len(ids)

    user = User(id=1, first_name="Иван")
    path = chat_dir(tmp_path, 1, "Иван")
    append(path, "2026-10", [format_line(D, 1, "Иван", "", tags=["[фото, с текстом]"]),
                             format_line(D, 2, "Иван", "", tags=["[фото]"])])
    client = Client()
    asyncio.run(ocr_photos(client, user, [1, 2], root=tmp_path))
    assert client.requested == [2]


class FakeChat:
    """Photo messages sent the given number of days ago; iter_messages pages like Telethon."""

    def __init__(self, *days_ago):
        now = datetime.now(timezone.utc)
        self.msgs, self.downloads = [], 0
        for days in days_ago:
            self.add(now - timedelta(days=days))

    def add(self, date):
        i = len(self.msgs) + 1
        self.msgs.append(Message(i, PeerUser(1), date, "", media=MessageMediaPhoto(photo=Photo(i, 0, b"", date, [], 2))))

    async def iter_messages(self, entity, min_id=0, max_id=0, reverse=False, wait_time=None, reply_to=None):
        msgs = [m for m in self.msgs if m.id > min_id and (not max_id or m.id < max_id)]
        for m in msgs if reverse else msgs[::-1]:
            yield m

    async def download_media(self, msg, file):
        self.downloads += 1
        return b""


def archived(path):
    return [int(i) for f in month_files(path) for i in HEADER.findall(f.read_text())]


def test_sync_loads_newest_first_down_to_depth(tmp_path, monkeypatch):
    monkeypatch.setattr("tgc.archive.recognize", lambda data: [])
    monkeypatch.setattr("tgc.archive.BUDGET_S", -1)  # every call stops after one message, like a big chat
    user, chat = User(id=1, first_name="Иван"), FakeChat(60, 30, 3, 1)
    path = chat_dir(tmp_path, 1, "Иван")
    assert asyncio.run(sync_chat(chat, user, days=7, root=tmp_path)).startswith("не всё")
    assert archived(path) == [4]
    assert asyncio.run(sync_chat(chat, user, root=tmp_path)).startswith("не всё")  # the depth is remembered
    monkeypatch.setattr("tgc.archive.BUDGET_S", 200)
    assert asyncio.run(sync_chat(chat, user, root=tmp_path)).startswith("готово")
    assert archived(path) == [3, 4] and chat.downloads == 0  # old history is not OCR'd
    chat.add(datetime.now(timezone.utc))
    asyncio.run(sync_chat(chat, user, root=tmp_path))
    assert archived(path) == [3, 4, 5] and chat.downloads == 1  # a new photo is
    chat.msgs.append(doc_msg(6, "план.txt", date=datetime.now(timezone.utc)))
    asyncio.run(sync_chat(chat, user, root=tmp_path))
    assert chat.downloads == 2  # and a new file
    assert "[файл план.txt, 1 КБ, без текста]" in (path / f"{datetime.now():%Y-%m}.md").read_text()
    asyncio.run(sync_chat(chat, user, days=45, root=tmp_path))  # deeper later
    assert archived(path) == [2, 3, 4, 5, 6]
    asyncio.run(sync_chat(FakeChat(60, 30, 3, 1), User(id=2, first_name="Пётр"), root=tmp_path))
    assert archived(chat_dir(tmp_path, 2, "Пётр")) == [1, 2, 3, 4]  # no days: the whole history


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision")
def test_recognize():
    lines = recognize((Path(__file__).parent / "fixtures" / "ocr.png").read_bytes())
    assert lines == ["Встреча перенесена на 14:30 в четверг", "Deploy failed: exit code 137 (OOMKilled)"]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS textutil and PDFKit")
def test_extract_text_macos():
    assert extract_text("a.rtf", rb"{\rtf1\ansi Hello\par World}") == (["Hello", "World"], "")
    with pytest.raises(Unreadable):
        extract_text("a.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 broken word file")
    with pytest.raises(Unreadable):
        extract_text("a.rtf", b"not an rtf")
    assert extract_text("a.doc", "просто текст".encode())[0] == ["просто текст"]
    with pytest.raises(Unreadable):
        extract_text("a.pdf", b"not a pdf")


@pytest.mark.skipif(sys.platform != "darwin", reason="PDFKit and Apple Vision")
def test_scanned_pdf_is_ocrd():
    from AppKit import NSImage
    from Foundation import NSData
    from Quartz import PDFDocument, PDFPage

    png = (Path(__file__).parent / "fixtures" / "ocr.png").read_bytes()
    doc = PDFDocument.alloc().init()
    doc.insertPage_atIndex_(PDFPage.alloc().initWithImage_(NSImage.alloc().initWithData_(NSData.dataWithBytes_length_(png, len(png)))), 0)
    lines, info = extract_text("scan.pdf", bytes(doc.dataRepresentation()))
    assert info == ", 1 стр., скан" and "Deploy failed: exit code 137 (OOMKilled)" in lines


def test_line_break_in_tag_cannot_forge_header(tmp_path):
    append(tmp_path, "2026-10", [format_line(D, 10, "Иван", "x", tags=["[файл a\r[2030-01-01 00:00] #999 b.pdf]"])])
    assert id_range(tmp_path)[1] == 10


def test_last_id_is_max_over_months(tmp_path):
    append(tmp_path, "2026-11", [format_line(D, 10, "Иван", "x")])
    append(tmp_path, "2026-10", [format_line(D, 11, "Иван", "после смены часового пояса")])
    assert id_range(tmp_path)[1] == 11


def test_pending_photo_with_colon_in_name(tmp_path):
    append(tmp_path, "2026-10", [
        format_line(D, 1, "Dev: Ivan", "", tags=["[фото]", "[переслано из «A: B»]"]),
        format_line(D, 2, "Dev: Ivan", "текст про [фото] в сообщении"),
    ])
    assert pending_ids(tmp_path, "фото") == [1]


def test_describe():
    voice = MessageMediaDocument(document=Document(
        1, 0, b"", D, "audio/ogg", 1, 2, [DocumentAttributeAudio(65, voice=True)]))
    assert describe(Message(1, PeerUser(1), D, "подпись", out=True, media=voice)) == (
        "[2026-10-31 14:02] #1 Я: [голосовое 1:05, не расшифровано] подпись\n")
    assert describe(Message(1, PeerUser(1), D, "", out=True, media=voice), "текст") == (
        "[2026-10-31 14:02] #1 Я: [голосовое 1:05] «текст»\n")
    photo = MessageMediaPhoto(photo=Photo(2, 0, b"", D, [], 2))
    fwd = MessageFwdHeader(D, from_name="Канал")
    assert describe(Message(2, PeerUser(1), D, "", media=photo, fwd_from=fwd, reply_to=MessageReplyHeader(reply_to_msg_id=1))) == (
        "[2026-10-31 14:02] #2 ? ↩1 [фото] [переслано из «Канал»]: \n")
    doc = MessageMediaDocument(document=Document(
        3, 0, b"", D, "application/pdf", 1, 2, [DocumentAttributeFilename("отчёт\n.pdf")]))
    assert describe(Message(3, PeerUser(1), D, "", media=doc)) == "[2026-10-31 14:02] #3 ? [файл отчёт .pdf, 1 КБ]: \n"
    odd = doc_msg(4, "Итоги: план].txt", size=2_345_678)
    assert describe(odd) == "[2026-10-31 14:02] #4 ? [файл Итоги - план).txt, 2.3 МБ]: \n"


def doc_msg(i, name, size=10, date=D):
    media = MessageMediaDocument(document=Document(i, 0, b"", date, "application/octet-stream", size, 2,
                                                   [DocumentAttributeFilename(name)]))
    return Message(i, PeerUser(1), date, "", media=media)


def test_file_marks(tmp_path):
    photo = MessageMediaPhoto(photo=Photo(5, 0, b"", D, [], 2))
    append(tmp_path, "2026-10", [
        describe(doc_msg(1, "смета.PDF")),
        describe(doc_msg(2, "архив.zip")),  # not readable locally: never pending
        format_line(D, 3, "Иван", "", tags=["[файл старый.docx]"]),  # archived before sizes were added
        describe(doc_msg(4, "план.md")),
        describe(Message(5, PeerUser(1), D, "", media=photo)),
    ])
    assert pending_ids(tmp_path, "файл") == [4, 3, 1] and pending_ids(tmp_path, "фото") == [5]
    assert update_block(tmp_path, 1, lambda b: set_media(b, ", с текстом"))
    assert update_block(tmp_path, 4, lambda b: set_media(b, ", слишком большой"))
    assert update_block(tmp_path, 2, lambda b: set_media(b, ": фото с объекта"))
    assert pending_ids(tmp_path, "файл") == [3]
    text = (tmp_path / "2026-10.md").read_text().replace("[2026-10-31 14:02] ", "")
    assert "#1 ? [файл смета.PDF, 1 КБ, с текстом]: \n" in text
    assert "#2 ? [файл архив.zip, 1 КБ: фото с объекта]: " in text
    assert "#4 ? [файл план.md, 1 КБ, слишком большой]: " in text
    assert update_block(tmp_path, 1, lambda b: set_media(b, ": смета на кухню"))
    assert "[файл смета.PDF, 1 КБ, с текстом: смета на кухню]: \n" in (tmp_path / "2026-10.md").read_text()


def test_description_keeps_file_pending(tmp_path):
    append(tmp_path, "2026-10", [describe(doc_msg(1, "смета.pdf")), describe(doc_msg(2, "план.md"))])
    assert update_block(tmp_path, 1, lambda b: set_media(b, ": смета на кухню"))
    assert update_block(tmp_path, 2, lambda b: set_media(b, ", с текстом"))
    assert update_block(tmp_path, 2, lambda b: set_media(b, ": копия из итоги.pdf"))  # must not look like a file name
    assert pending_ids(tmp_path, "файл") == [1]
    assert update_block(tmp_path, 1, lambda b: set_media(b, ", 3 стр., с текстом"))
    assert "[файл смета.pdf, 1 КБ, 3 стр., с текстом: смета на кухню]" in (tmp_path / "2026-10.md").read_text()
    assert pending_ids(tmp_path, "файл") == []


def test_extract_text():
    assert extract_text("a.txt", "\ufeffпервая\n\n  вторая   строка ".encode()) == (["первая", "вторая строка"], "")
    assert extract_text("a.TXT", "привет".encode("cp1251"))[0] == ["привет"]
    assert extract_text("a.csv", "дата;сумма\n".encode("utf-16"))[0] == ["дата;сумма"]
    assert extract_text("boot.txt", b"\x1b[2J\x1b[01;01HBdsDxe: loading\n\xef\xbf\xbc\n")[0] == ["BdsDxe: loading"]
    page = ("<!doctype html><html><head><title>Отчёт</title><style>p{color:red}</style><script>var x = '<p>';</script></head>"
            "<body><h1>Итоги</h1><p>Выручка &amp; расходы</p><table><tr><td>Q1</td><td>10</td></tr></table></body></html>")
    assert extract_text("a.html", page.encode())[0] == ["Отчёт", "Итоги", "Выручка & расходы", "| Q1 | 10"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", (
            "<w:body><w:p><w:r><w:t>Смета</w:t></w:r><w:r><w:tab/><w:t xml:space=\"preserve\"> на &amp; </w:t></w:r>"
            "<w:r><w:t>кухню</w:t></w:r></w:p><w:p></w:p><w:p><w:r><w:t>Итого</w:t></w:r></w:p></w:body>"))
    assert extract_text("a.docx", buf.getvalue())[0] == ["Смета на & кухню", "Итого"]
    assert extract_text("a.doc", buf.getvalue())[0] == ["Смета на & кухню", "Итого"]  # a docx saved as .doc
    assert extract_text("a.doc", "\ufeff<html><body><p>Привет</p></body></html>".encode())[0] == ["Привет"]
    with pytest.raises(Unreadable):  # binary junk is not read as text
        extract_text("a.doc", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    with pytest.raises(Unreadable):
        extract_text("a.docx", b"not a zip")


def office_zip(parts):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, xml in parts.items():
            z.writestr(name, xml)
    return buf.getvalue()


R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
RELS = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{}</Relationships>'
X = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def test_extract_xlsx():
    data = office_zip({
        "xl/workbook.xml": f'<workbook {X} {R}><sheets><sheet name="Смета" sheetId="1" r:id="rId2"/>'
                           f'<sheet name="Итог" sheetId="2" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": RELS.format('<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                                                  '<Relationship Id="rId2" Target="/xl/worksheets/sheet2.xml"/>'),
        "xl/sharedStrings.xml": f'<sst {X}><si><t>Плитка</t></si><si><r><t>м</t></r><r><t>²</t></r></si></sst>',
        "xl/worksheets/sheet2.xml": f'<worksheet {X}><sheetData><row><c t="s"><v>0</v></c><c><v>12.5</v></c>'
                                    f'<c t="s"><v>1</v></c><c/></row></sheetData></worksheet>',
        "xl/worksheets/sheet1.xml": f'<worksheet {X}><sheetData><row><c t="inlineStr"><is><t>Всего</t></is></c>'
                                    f'<c><f>B1*2</f><v>25</v></c></row></sheetData></worksheet>',
    })
    assert extract_text("a.xlsx", data)[0] == ["## Лист «Смета»", "Плитка | 12.5 | м²", "## Лист «Итог»", "Всего | 25"]
    with pytest.raises(Unreadable):
        extract_text("a.xlsx", office_zip({"xl/workbook.xml": "<workbook"}))


def test_extract_pptx():
    A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    slide = '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" {} {}>{}</p:sld>'
    data = office_zip({
        "ppt/presentation.xml": f'<p:presentation xmlns:p="x" {R}><p:sldIdLst><p:sldId id="256" r:id="rId3"/>'
                                f'<p:sldId id="257" r:id="rId2"/></p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": RELS.format('<Relationship Id="rId2" Target="slides/slide1.xml"/>'
                                                       '<Relationship Id="rId3" Target="slides/slide2.xml"/>'),
        "ppt/slides/slide2.xml": slide.format(A, R, "<a:p><a:r><a:t>План</a:t></a:r><a:br/><a:r><a:t>на год</a:t></a:r></a:p>"),
        "ppt/slides/slide1.xml": slide.format(A, R, "<a:p><a:r><a:t>Итоги</a:t></a:r></a:p><a:p/>"),
    })
    assert extract_text("a.pptx", data)[0] == ["## Слайд 1", "План на год", "## Слайд 2", "Итоги"]


def test_read_files(tmp_path, monkeypatch):
    monkeypatch.setattr("tgc.archive.pdf_text", lambda data: ("", ", 3 стр."))  # no text even after OCR
    class Client:
        def __init__(self, msgs):
            self.msgs, self.downloads = msgs, 0

        async def get_messages(self, entity, ids):
            return [self.msgs.get(i) for i in ids]

        async def download_media(self, msg, file):
            self.downloads += 1
            return "строка 1\nстрока 2".encode()

    user = User(id=1, first_name="Иван")
    path = chat_dir(tmp_path, 1, "Иван")
    big = doc_msg(3, "big.txt", size=60_000_000)
    pdf = doc_msg(4, "scan.pdf")
    append(path, "2026-10", [describe(doc_msg(1, "a.txt")), describe(doc_msg(2, "gone.txt")), describe(big), describe(pdf)])
    client = Client({1: doc_msg(1, "a.txt"), 3: big, 4: pdf})
    assert "осталось необработанных файлов 0" in asyncio.run(read_files(client, user, root=tmp_path))
    assert client.downloads == 2  # the big file is not downloaded
    text = (path / "2026-10.md").read_text()
    assert "[файл a.txt, 1 КБ, с текстом]: \n[" in text  # the text is not in the archive
    assert (path / "text" / "1.txt").read_text() == "строка 1\nстрока 2\n" and not (path / "text" / "4.txt").exists()
    assert "[файл gone.txt, 1 КБ, недоступно]" in text and "[файл big.txt, 60.0 МБ, слишком большой]" in text
    assert "[файл scan.pdf, 1 КБ, 3 стр., без текста]" in text


def test_download_large_resumes(tmp_path, monkeypatch):
    data = bytes(range(256)) * 10

    class Client:
        offsets = []

        async def iter_download(self, media, offset=0):
            self.offsets.append(offset)
            for i in range(offset, len(data), 1000):
                yield data[i : i + 1000]

    msg = doc_msg(7, "big.zip", size=len(data))
    monkeypatch.setattr("tgc.archive.BUDGET_S", -1)  # every call stops after one chunk
    client = Client()
    assert "вызови fetch_large_file ещё раз" in asyncio.run(download_large(client, msg, tmp_path))
    assert "ещё раз" in asyncio.run(download_large(client, msg, tmp_path))
    out = asyncio.run(download_large(client, msg, tmp_path))
    assert client.offsets == [0, 1000, 2000] and out.endswith(".part") is False
    assert [p.name for p in tmp_path.iterdir()] == [Path(out.split(": ")[1]).name]
    assert Path(out.split(": ")[1]).read_bytes() == data


def test_textutil_timeout_is_unreadable(monkeypatch):
    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired("textutil", 60)

    monkeypatch.setattr("tgc.archive.subprocess.run", hang)
    with pytest.raises(Unreadable):  # marked "не читается" instead of retried on every call
        extract_text("a.doc", b"...")


def test_sync_does_not_count_big_file_as_read(tmp_path):
    user, chat = User(id=1, first_name="Иван"), FakeChat(1)
    asyncio.run(sync_chat(chat, user, root=tmp_path))
    now = datetime.now(timezone.utc)
    chat.msgs += [doc_msg(2, "big.pdf", size=60_000_000, date=now), doc_msg(3, "a.txt", date=now),
                  doc_msg(4, "mid.pdf", size=20_000_000, date=now)]
    out = asyncio.run(sync_chat(chat, user, root=tmp_path))
    assert "файлов прочитано 1" in out and chat.downloads == 1  # a.txt only
    assert pending_ids(chat_dir(tmp_path, 1, "Иван"), "файл") == [4]  # over 10 MB: left for read_files
