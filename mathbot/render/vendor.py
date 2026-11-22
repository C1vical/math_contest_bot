"""Pinned third-party files the renderer needs: MathJax and the Computer Modern fonts.

Rendering must be deterministic and must not depend on a CDN being reachable,
so these files are downloaded once (``python -m mathbot install-render-assets``),
verified against the SHA-256 recorded here, and served to the page from disk.
The page refers to them under :data:`ASSET_ORIGIN`, a reserved ``.invalid``
host that Playwright intercepts (see ``PlaywrightRenderer``); nothing the page
loads ever reaches the network.

To upgrade a file, change its URL and hash together and bump
``TEMPLATE_VERSION`` so every image is re-rendered with it.

Licences: MathJax is Apache-2.0; Computer Modern Unicode fonts are SIL OFL 1.1.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from mathbot.http import HttpError, PoliteClient, atomic_write_bytes

log = logging.getLogger(__name__)

ASSET_ORIGIN = "https://render-assets.invalid"
_FONT_COMMIT = "e9977be1283466992a2dede47a342655d8426fee"  # dreampulse/computer-modern-web-font has no releases
_FONT_BASE = f"https://cdn.jsdelivr.net/gh/dreampulse/computer-modern-web-font@{_FONT_COMMIT}/font/Serif"


@dataclass(frozen=True)
class VendoredFile:
    path: str  # served at ASSET_ORIGIN/<path>, stored at <assets dir>/<path>
    url: str
    sha256: str
    content_type: str


VENDORED: tuple[VendoredFile, ...] = (
    VendoredFile(
        "mathjax/tex-svg-full.js",
        "https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5/tex-svg-full.js",
        "a4354ff94fd868aea0cc6eaaa79a57fda0588646fc46ee3700a349ee0a11cbe6",
        "application/javascript",
    ),
    VendoredFile(
        "fonts/cmunrm.woff",
        f"{_FONT_BASE}/cmunrm.woff",
        "32115abbbfb231586813333bfa5a234f4dc7f28b27df7260f42f81146ccd9097",
        "font/woff",
    ),
    VendoredFile(
        "fonts/cmunbx.woff",
        f"{_FONT_BASE}/cmunbx.woff",
        "3226e8a7068df32b96f2a80f7ad2ca306c8dfcc494ab2b337019cbb7d098db24",
        "font/woff",
    ),
    VendoredFile(
        "fonts/cmunti.woff",
        f"{_FONT_BASE}/cmunti.woff",
        "d9a02b7b67874c62f47f9c9a0914c115972ab77d43e82a163baf0888eb5d4a2d",
        "font/woff",
    ),
    VendoredFile(
        "fonts/cmunbi.woff",
        f"{_FONT_BASE}/cmunbi.woff",
        "c91f3defd696023069b9953de078bf0f27a32218e08405ebfbf50c29d10004ab",
        "font/woff",
    ),
)
BY_PATH = {f.path: f for f in VENDORED}


class RenderAssetsMissing(RuntimeError):
    """Pinned render assets are absent or corrupt."""


def asset_url(path: str) -> str:
    """The URL the render page uses for a pinned file (intercepted by the renderer)."""
    return f"{ASSET_ORIGIN}/{path}"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_installed(root: Path, item: VendoredFile) -> bool:
    path = root / item.path
    return path.is_file() and _sha256(path.read_bytes()) == item.sha256


def missing_assets(root: Path) -> list[str]:
    """Paths that are absent or whose hash does not match the pin."""
    return [item.path for item in VENDORED if not _is_installed(root, item)]


def require_assets(root: Path) -> None:
    """Raise :class:`RenderAssetsMissing` unless every pinned file is installed and intact."""
    missing = missing_assets(root)
    if missing:
        raise RenderAssetsMissing(
            f"render assets missing or corrupt in {root}: {', '.join(missing)}. "
            "Run: python -m mathbot install-render-assets"
        )


def install_assets(root: Path, client: PoliteClient) -> dict[str, str]:
    """Download every pinned file that is missing or wrong. Returns ``{path: outcome}``."""
    outcomes: dict[str, str] = {}
    for item in VENDORED:
        if _is_installed(root, item):
            outcomes[item.path] = "present"
            continue
        try:
            data = client.get(item.url, use_cache=False).content
        except HttpError as exc:
            raise RenderAssetsMissing(f"could not download {item.url}: {exc}") from exc
        digest = _sha256(data)
        if digest != item.sha256:
            raise RenderAssetsMissing(
                f"{item.url} has SHA-256 {digest}, expected {item.sha256}; refusing to install it"
            )
        atomic_write_bytes(root / item.path, data)
        outcomes[item.path] = "installed"
        log.info("render asset installed", extra={"path": item.path, "bytes": len(data)})
    return outcomes


def read_asset(root: Path, path: str) -> tuple[bytes, str] | None:
    """Bytes and content type for a request path, only if it is a verified pinned file."""
    item = BY_PATH.get(path)
    if item is None:
        return None
    file = root / item.path
    try:
        data = file.read_bytes()
    except OSError:
        return None
    return (data, item.content_type) if _sha256(data) == item.sha256 else None
