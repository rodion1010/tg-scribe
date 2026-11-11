import os
import subprocess

from tgc.login import show_qr


def test_show_qr_terminal(capsys):
    show_qr("tg://login?token=abc", in_terminal=True)
    out = capsys.readouterr().out
    assert "█" in out  # QR напечатан в терминал


def test_show_qr_png(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr("tgc.login.QR_PNG", tmp_path / "qr.png")
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: opened.append(cmd))
    if hasattr(os, "startfile"):
        monkeypatch.setattr(os, "startfile", lambda p: opened.append([p]))
    show_qr("tg://login?token=abc", in_terminal=False)
    png = tmp_path / "qr.png"
    assert png.read_bytes()[:4] == b"\x89PNG"  # PNG создан
    assert opened and str(png) in opened[0]  # и открыт программой по умолчанию
