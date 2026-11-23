"""Fallback for AoPS formula images: typeset them from their TeX source.

AoPS renders every formula (and every Asymptote diagram) to a PNG on
``latex.artofproblemsolving.com``. The renderer shows those native PNGs, as the
pre-2.0 bot did, downloading each one once through the asset cache. Each formula
image also carries its TeX source in its ``alt`` text, so when a formula's PNG
can't be downloaded, this module turns that image back into MathJax input and
the renderer typesets it locally instead:

    $...$          -> \\( ... \\)        inline
    $$...$$, \\[...\\] -> \\[ ... \\]     display
    \\begin{env}...   -> left as-is        (MathJax processes bare environments)

``$`` is never enabled as a MathJax delimiter globally, because CEMC text uses
it for money ("$5"). Images whose ``alt`` is Asymptote source (``[asy]...``)
or not TeX have no fallback: if they can't be downloaded, the render fails
rather than shipping a picture with a hole.
"""

from __future__ import annotations

from collections.abc import Callable

from bs4 import BeautifulSoup, NavigableString, Tag

from mathbot.tex import TEX_CLASS, mathjax_tex

AOPS_LATEX_HOSTS = ("latex.artofproblemsolving.com",)
_DISPLAY_CLASSES = {"latexcenter"}


def is_aops_latex_image(src: str) -> bool:
    return any(f"//{host}/" in src for host in AOPS_LATEX_HOSTS)


def _is_display(img: Tag) -> bool:
    return bool(set(img.get("class") or []) & _DISPLAY_CLASSES)


def _img_tex(img: Tag) -> str | None:
    """TeX for an AoPS formula ``<img>``, or ``None`` when it has no TeX fallback."""
    src = img.get("src")
    if not isinstance(src, str) or not is_aops_latex_image(src):
        return None
    return mathjax_tex(str(img.get("alt") or ""), _is_display(img))


def formula_images(html: str) -> set[str]:
    """URLs of the AoPS formula images in ``html`` that can fall back to their alt TeX."""
    if not any(host in html for host in AOPS_LATEX_HOSTS):
        return set()
    soup = BeautifulSoup(html, "html.parser")
    return {img["src"] for img in soup.find_all("img") if _img_tex(img) is not None}


def latex_images_to_tex(html: str, keep: Callable[[str], bool] | None = None) -> tuple[str, int]:
    """Return ``(html, converted)`` with AoPS formula images replaced by MathJax TeX.

    ``keep(src)`` returning True leaves that image in place (its PNG is available);
    every other convertible image becomes TeX.
    """
    if not any(host in html for host in AOPS_LATEX_HOSTS):
        return html, 0
    soup = BeautifulSoup(html, "html.parser")
    converted = 0
    for img in soup.find_all("img"):
        tex = _img_tex(img)
        if tex is None or (keep is not None and keep(img["src"])):
            continue
        if _is_display(img) or tex.startswith(("\\[", "\\begin")):
            block = soup.new_tag("div", attrs={"class": "aops-tex-display"})
            block.append(NavigableString(tex))
            img.replace_with(block)
        else:
            span = soup.new_tag("span", attrs={"class": TEX_CLASS})
            span.append(NavigableString(tex))
            img.replace_with(span)
        converted += 1
    return (str(soup), converted) if converted else (html, 0)
