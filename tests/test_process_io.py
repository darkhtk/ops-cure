from __future__ import annotations

import os
import sys
from pathlib import Path


def test_build_utf8_subprocess_env_adds_expected_defaults(monkeypatch):
    repo_root = Path(r"C:\Users\darkh\Projects\ops-cure")
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from pc_launcher.process_io import build_utf8_subprocess_env

    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)

    env = build_utf8_subprocess_env(extra={"OPS_CURE_SAMPLE": "한글"})

    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUTF8"] == "1"
    assert env["LANG"] == "C.UTF-8"
    assert env["LC_ALL"] == "C.UTF-8"
    assert env["NO_COLOR"] == "1"
    assert env["OPS_CURE_SAMPLE"] == "한글"


def test_wrap_powershell_utf8_includes_utf8_prologue():
    repo_root = Path(r"C:\Users\darkh\Projects\ops-cure")
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from pc_launcher.process_io import wrap_powershell_utf8

    wrapped = wrap_powershell_utf8("Write-Output '한글 테스트'")

    assert "[Console]::InputEncoding" in wrapped
    assert "[Console]::OutputEncoding" in wrapped
    assert "$OutputEncoding" in wrapped
    assert "chcp 65001" in wrapped
    assert wrapped.endswith("Write-Output '한글 테스트'")


def test_configure_utf8_stdio_reconfigures_all_text_streams():
    repo_root = Path(r"C:\Users\darkh\Projects\ops-cure")
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import pc_launcher.process_io as process_io

    class FakeStream:
        def __init__(self) -> None:
            self.calls = []

        def reconfigure(self, **kwargs):
            self.calls.append(kwargs)

    fake_stdin = FakeStream()
    fake_stdout = FakeStream()
    fake_stderr = FakeStream()

    original_stdin = process_io.sys.stdin
    original_stdout = process_io.sys.stdout
    original_stderr = process_io.sys.stderr
    process_io.sys.stdin = fake_stdin
    process_io.sys.stdout = fake_stdout
    process_io.sys.stderr = fake_stderr
    try:
        process_io.configure_utf8_stdio()
    finally:
        process_io.sys.stdin = original_stdin
        process_io.sys.stdout = original_stdout
        process_io.sys.stderr = original_stderr

    assert fake_stdin.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert fake_stdout.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert fake_stderr.calls == [{"encoding": "utf-8", "errors": "replace"}]
