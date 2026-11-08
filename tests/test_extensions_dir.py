"""Extension provisioning must never fail a launch, and must not write into the package.

Measured 2026-11-08: under a sandbox where only /tmp is writable, provisioning
raised PermissionError from os.makedirs (outside its try), so start_browser
failed outright although the code meant to carry on without extensions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zendriver_mcp import session as S

SPEC = {"dir": "probe", "repo": "nobody/nothing", "asset_suffix": ".zip", "wrapper": None}


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise OSError("network disabled in this test")

    monkeypatch.setattr("urllib.request.urlopen", refuse)


def test_unwritable_extensions_dir_does_not_raise(tmp_path: Path, monkeypatch, no_network) -> None:
    # A path under a regular file cannot be created, even by root (who
    # ignores directory permission bits): the same failure as a read-only
    # sandbox, reproducible anywhere.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("ZENDRIVER_MCP_EXTENSIONS_DIR", str(blocker / "ext"))
    assert S.BrowserSession._ensure_extension(SPEC) is None


def test_extensions_dir_honours_the_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ZENDRIVER_MCP_EXTENSIONS_DIR", str(tmp_path / "ext"))
    assert S.extensions_dir() == tmp_path / "ext"


def test_default_is_a_cache_dir_not_the_package(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ZENDRIVER_MCP_EXTENSIONS_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    d = S.extensions_dir()
    assert d == tmp_path / "cache" / "zendriver-mcp" / "extensions"
    pkg = Path(S.__file__).resolve().parent.parent
    assert pkg not in d.parents


def test_already_installed_extension_is_used_read_only(
    tmp_path: Path, monkeypatch, no_network
) -> None:
    ext = tmp_path / "ext" / "probe"
    ext.mkdir(parents=True)
    (ext / "manifest.json").write_text("{}")
    monkeypatch.setenv("ZENDRIVER_MCP_EXTENSIONS_DIR", str(tmp_path / "ext"))
    assert S.BrowserSession._ensure_extension(SPEC) == str(ext)


def test_unwritable_dir_falls_back_to_temp(tmp_path: Path, monkeypatch, no_network) -> None:
    """When the cache dir cannot be written, provisioning goes to the temp dir."""
    monkeypatch.delenv("ZENDRIVER_MCP_EXTENSIONS_DIR", raising=False)
    monkeypatch.setattr(S, "_writable", lambda p: str(p).startswith(str(tmp_path / "tmp")))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(S.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    assert S.extensions_dir() == tmp_path / "tmp" / "zendriver-mcp-extensions"


def test_permission_error_creating_the_dir_does_not_fail_the_launch(
    tmp_path: Path, monkeypatch, no_network
) -> None:
    """The sandbox case exactly: creating the directory is refused."""
    monkeypatch.setenv("ZENDRIVER_MCP_EXTENSIONS_DIR", str(tmp_path / "ext"))

    def refuse(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("os.makedirs", refuse)
    monkeypatch.setattr("tempfile.mkdtemp", refuse)
    assert S.BrowserSession._ensure_extension(SPEC) is None


def test_reserved_metadata_dir_is_stripped_at_provisioning(tmp_path: Path, monkeypatch) -> None:
    """Chrome refuses an unpacked extension holding `_metadata` unless it can delete it,
    which a read-only (sandboxed) copy cannot. Provisioning removes it up front."""
    import io
    import json as _json
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", "{}")
        z.writestr("_metadata/verified_contents.json", "[]")
    data = buf.getvalue()

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    meta = {"assets": [{"name": "x.zip", "browser_download_url": "https://example.invalid/x.zip"}]}
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp(_json.dumps(meta).encode()))
    monkeypatch.setattr(
        "urllib.request.urlretrieve", lambda url, dest: Path(dest).write_bytes(data)
    )
    monkeypatch.setenv("ZENDRIVER_MCP_EXTENSIONS_DIR", str(tmp_path / "ext"))
    got = S.BrowserSession._ensure_extension(SPEC)
    assert got is not None
    assert (Path(got) / "manifest.json").is_file()
    assert not (Path(got) / "_metadata").exists()


def test_read_only_install_is_copied_somewhere_writable(
    tmp_path: Path, monkeypatch, no_network
) -> None:
    """Chrome writes indexed rulesets INTO an unpacked extension's directory, so a
    read-only copy (a sandbox's view of the cache) fails to load ("Internal error
    while parsing rules"). It is copied to the writable candidate and loaded from there."""
    cache = tmp_path / "cache" / "zendriver-mcp" / "extensions"
    (cache / "probe").mkdir(parents=True)
    (cache / "probe" / "manifest.json").write_text("{}")
    (cache / "probe" / "rules.json").write_text("[]")
    monkeypatch.delenv("ZENDRIVER_MCP_EXTENSIONS_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(S.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    real = S._writable
    monkeypatch.setattr(S, "_writable", lambda p: not str(p).startswith(str(cache)) and real(p))

    got = Path(S.BrowserSession._ensure_extension(SPEC) or "")
    assert got == tmp_path / "tmp" / "zendriver-mcp-extensions" / "probe"
    assert (got / "rules.json").read_text() == "[]"
