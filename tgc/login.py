"""Вход в Telegram по QR-коду: `uv run python -m tgc.login [--terminal]`.

QR открывается как PNG в программе по умолчанию (macOS / Windows / Linux);
с --terminal печатается прямо в терминал — без внешних программ.
Отсканируйте код: Telegram → Настройки → Устройства → Подключить устройство.
"""

import asyncio
import os
import subprocess
import sys
from getpass import getpass

import segno
from mcp.server.mcpserver.exceptions import ToolError
from telethon import errors

from tgc.client import CONF_DIR, SESSION, telegram

QR_PNG = CONF_DIR / "login-qr.png"


def open_png(path) -> bool:
    """Открывает файл программой по умолчанию: open на macOS, os.startfile на Windows, xdg-open на Linux."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        elif os.name == "nt":
            os.startfile(str(path))  # noqa: PTH124 — штатный способ открыть файл на Windows
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as e:
        print(f"Не удалось открыть {path} программой по умолчанию: {e}")
        return False
    return True


def show_qr(url: str, in_terminal: bool) -> None:
    """Показывает QR-код: в терминале (in_terminal=True) или PNG-файлом через программу по умолчанию."""
    qr = segno.make(url)
    if in_terminal:
        qr.terminal(compact=True)  # печатает код прямо в терминал
        return
    qr.save(str(QR_PNG), scale=10, border=2)
    print(f"QR-код сохранён: {QR_PNG}")
    if not open_png(QR_PNG):  # программа не открылась — запасной путь, код в терминале
        qr.terminal(compact=True)


async def login(in_terminal: bool):
    print("Подключаюсь к Telegram...")
    async with telegram(login=True) as client:
        if await client.is_user_authorized():
            return await client.get_me()
        try:
            qr = await client.qr_login()
        except errors.SessionPasswordNeededError:
            # QR уже отсканирован в прошлой попытке — Telegram сразу просит пароль
            return await client.sign_in(password=getpass("Облачный пароль (двухэтапная проверка): "))
        while True:
            show_qr(qr.url, in_terminal)
            print("Отсканируйте код в Telegram: Настройки → Устройства → Подключить устройство.")
            try:
                return await qr.wait()  # ждёт, пока код не отсканируют или не истечёт (~26 секунд)
            except asyncio.TimeoutError:
                await qr.recreate()  # обновляет код на месте и возвращает None
                print("Срок кода истёк, показываю свежий.")
            except errors.SessionPasswordNeededError:
                return await client.sign_in(password=getpass("Облачный пароль (двухэтапная проверка): "))


async def main():
    me = await login("--terminal" in sys.argv[1:])
    SESSION.with_suffix(".session").chmod(0o600)
    QR_PNG.unlink(missing_ok=True)  # токен погашен, картинка больше не нужна
    print(f"Вход выполнен: {me.first_name} (@{me.username})")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ToolError as e:
        raise SystemExit(str(e)) from None
