"""Size and baseline of AoPS's inline formula pictures.

AoPS draws each formula at twice its HTML size, with a few blank pixels around it. Pages
saved before it added those blank pixels give sizes that no longer fit the picture (the same
"Z" is 13x12 on one page and 16x14 on another), so short formulas came out up to a third too
small, and give no baseline, or the old one, so they sat below or above the line.

``formula_sizes.json`` holds ``[width, height, vertical-align]`` of the pictures this matters
for, measured from the pictures themselves; its keys are their file names.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

from bs4 import BeautifulSoup

_SIZES = Path(__file__).with_name("formula_sizes.json")
_HOST = "//latex.artofproblemsolving.com/"
DEFAULT_BASELINE = "vertical-align: -1px"  # the usual blank pixel under a formula without descenders


@cache
def _sizes() -> dict[str, list[int]]:
    return json.loads(_SIZES.read_text(encoding="utf-8"))


def fix_formula_images(soup: BeautifulSoup) -> None:
    """Correct the size and baseline of inline AoPS formula pictures, in place."""
    for image in soup.find_all("img", class_="latex"):
        src, style = str(image.get("src") or ""), str(image.get("style") or "")
        if _HOST not in src or str(image.get("alt") or "").startswith("[asy]"):
            continue
        entry = _sizes().get(src.rsplit("/", 1)[-1].removesuffix(".png"))
        width = str(image.get("width") or "")
        if entry and width.isdigit() and int(width) <= entry[0]:
            image["width"], image["height"] = str(entry[0]), str(entry[1])
            image["style"] = f"vertical-align: {entry[2]}px"
        elif "vertical-align" not in style:
            image["style"] = f"vertical-align: {entry[2]}px" if entry else DEFAULT_BASELINE
