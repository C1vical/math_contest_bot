from __future__ import annotations

import hashlib

import pytest

from mathbot.http import HttpStatusError, Response
from mathbot.render import vendor
from mathbot.render.vendor import (
    VENDORED,
    RenderAssetsMissing,
    install_assets,
    missing_assets,
    read_asset,
    require_assets,
)


class FakeClient:
    def __init__(self, bodies: dict[str, bytes | Exception]):
        self.bodies = bodies
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        body = self.bodies[url]
        if isinstance(body, Exception):
            raise body
        return Response(url, 200, {}, body)


@pytest.fixture
def pinned(monkeypatch):
    """Swap the real pins for tiny ones whose hashes we control."""
    files = {
        "mathjax/tex-svg-full.js": b"window.MathJax = window.MathJax || {};",
        "fonts/cmunrm.woff": b"wOFF-fake-font",
    }
    items = tuple(
        vendor.VendoredFile(path, f"https://cdn.example.org/{path}", hashlib.sha256(data).hexdigest(), "x/y")
        for path, data in files.items()
    )
    monkeypatch.setattr(vendor, "VENDORED", items)
    monkeypatch.setattr(vendor, "BY_PATH", {i.path: i for i in items})
    return {f"https://cdn.example.org/{path}": data for path, data in files.items()}


def test_real_pins_are_exact_versions_with_hashes():
    for item in VENDORED:
        assert "@master" not in item.url and "@latest" not in item.url
        assert len(item.sha256) == 64
    assert any(i.path.startswith("mathjax/") and "mathjax@3.2.2" in i.url for i in VENDORED)


def test_install_verifies_and_is_idempotent(tmp_path, pinned):
    client = FakeClient(dict(pinned))
    assert missing_assets(tmp_path) == ["mathjax/tex-svg-full.js", "fonts/cmunrm.woff"]
    assert set(install_assets(tmp_path, client).values()) == {"installed"}
    require_assets(tmp_path)
    assert set(install_assets(tmp_path, client).values()) == {"present"}
    assert len(client.calls) == 2  # second run downloaded nothing


def test_install_refuses_a_file_with_the_wrong_hash(tmp_path, pinned):
    tampered = {url: b"tampered" for url in pinned}
    with pytest.raises(RenderAssetsMissing, match="refusing to install"):
        install_assets(tmp_path, FakeClient(tampered))
    assert not any(tmp_path.rglob("*.js"))


def test_install_reports_download_failures(tmp_path, pinned):
    url = next(iter(pinned))
    with pytest.raises(RenderAssetsMissing, match="could not download"):
        install_assets(tmp_path, FakeClient({url: HttpStatusError("HTTP 503", url=url, status=503)}))


def test_read_asset_serves_only_verified_pinned_files(tmp_path, pinned):
    install_assets(tmp_path, FakeClient(dict(pinned)))
    data, content_type = read_asset(tmp_path, "mathjax/tex-svg-full.js")
    assert data.startswith(b"window.MathJax") and content_type == "x/y"
    assert read_asset(tmp_path, "../secrets.txt") is None
    assert read_asset(tmp_path, "fonts/unknown.woff") is None
    (tmp_path / "fonts" / "cmunrm.woff").write_bytes(b"corrupted")
    assert read_asset(tmp_path, "fonts/cmunrm.woff") is None
    with pytest.raises(RenderAssetsMissing, match="fonts/cmunrm.woff"):
        require_assets(tmp_path)
