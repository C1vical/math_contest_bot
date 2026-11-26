"""Turn raw source fragments into safe, deterministic, renderable HTML.

Raw source content is stored untouched elsewhere; this module produces the
*normalized* form that is hashed, versioned and rendered:

* scripts, styles, forms, buttons, comments and edit links are removed;
* event-handler attributes and ``javascript:`` URLs are stripped;
* protocol-relative and relative URLs are made absolute;
* links are unwrapped (a PNG cannot be clicked);
* LaTeX survives: MathJax ``\\(...\\)`` text is left alone and AoPS LaTeX
  images keep their ``alt`` attribute, which holds the TeX source;
* TeX that an AoPS page shows as plain text is made typesettable (``tex.py``), an ``<img>``
  tag it shows as text becomes the image, collapsed forum remarks are removed, and the
  size and baseline of its formula pictures are corrected (``formulas.py``), spaces between
  a formula and the punctuation after it and empty paragraphs at the end are dropped,
  and answer choices get a paragraph of their own.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from mathbot.corrections import correct
from mathbot.formulas import fix_formula_images
from mathbot.tex import repair_aops_tex

_DROP_TAGS = ("script", "style", "noscript", "iframe", "object", "embed", "form", "input", "button", "link", "meta")
_DROP_SELECTORS = (".mw-editsection", "#toc", ".toc", ".printfooter", ".catlinks")
# A collapsed remark in an AoPS forum post: a link that reveals text hidden with CSS.
_AOPS_DROP_SELECTORS = (".cmty-hide-heading", ".cmty-hide-content")
# An <img> tag an AoPS page shows as text, because wikitext does not allow it.
_ESCAPED_IMG = re.compile(
    r'<img\s+src\s*=\s*"(https://(?:[\w-]+\.)*artofproblemsolving\.com/[^"\s]+)"'
    r'(?:\s+alt\s*=\s*"([^"]*)")?\s*/?>',
    re.IGNORECASE,
)
_SPACED_PUNCTUATION = re.compile(r"\s+(?=[,.;?!](?:\s|$))(?!\.\s*\.)")
# TeX whose first words are the label of answer choice (A).
_FIRST_CHOICE = re.compile(r"\$\s*(?:\\\w+\s*)?[{(\s~]*\(?A\)")
_URL_ATTRS = ("src", "href", "xlink:href")
# AoPS renders TeX to images; the alt text is the TeX source.
_LATEX_IMG_CLASSES = {"latex", "latexcenter", "asy-image"}
MIN_TEXT_CHARS = 5


@dataclass(frozen=True)
class NormalizedContent:
    html: str
    text: str
    content_hash: str


class InvalidContentError(ValueError):
    """The fragment has no usable statement (empty, or only markup)."""


def sha256_text(text: str) -> str:
    """Hex SHA-256 of ``text`` as UTF-8: content hashes and source revisions."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _soup(fragment: str) -> BeautifulSoup:
    return BeautifulSoup(fragment, "html.parser")


def is_remote_url(src: object) -> bool:
    """True for an absolute http(s) URL (not a ``data:`` URI or a relative path)."""
    return isinstance(src, str) and src.startswith(("http://", "https://"))


def remote_images(html: str) -> list[str]:
    """Distinct http(s) ``<img>`` URLs in ``html``, in document order."""
    return list(dict.fromkeys(img["src"] for img in _soup(html).find_all("img") if is_remote_url(img.get("src"))))


# Browsers remove ASCII tabs, newlines and control characters from a URL before reading its
# scheme, so "java\tscript:" is still a script URL.
_URL_IGNORED_CHARS = re.compile(r"[\x00-\x20]")


def _is_script_url(value: str) -> bool:
    return _URL_IGNORED_CHARS.sub("", value).lower().startswith(("javascript:", "vbscript:"))


def _restore_escaped_images(soup: BeautifulSoup) -> None:
    soup.smooth()  # the URL inside the tag was a link: one text node again now that links are unwrapped
    for node in soup.find_all(string=_ESCAPED_IMG):
        source, position = str(node), 0
        pieces: list[Tag | NavigableString] = []
        for match in _ESCAPED_IMG.finditer(source):
            image = soup.new_tag("img", attrs={"src": match[1], "alt": match[2] or ""})
            pieces += [NavigableString(source[position : match.start()]), image]
            position = match.end()
        node.replace_with(*pieces, NavigableString(source[position:]))


def _close_up_punctuation(soup: BeautifulSoup) -> None:
    """Remove the space a page leaves between a formula picture and the punctuation after it,
    where the line could break. (Not before ":", which is also a ratio.)"""
    for image in soup.find_all("img", class_="latex"):
        after = image.next_sibling
        if isinstance(after, NavigableString) and (space := _SPACED_PUNCTUATION.match(after)):
            after.replace_with(after[space.end() :])


def _separate_choices(soup: BeautifulSoup) -> None:
    """Start a new paragraph at answer choices that follow the question in its own paragraph."""
    for image in soup.find_all("img", class_="latex"):
        paragraph, alt = image.parent, str(image.get("alt") or "")
        if paragraph.name != "p" or not (_FIRST_CHOICE.match(alt) and "(B)" in alt):
            continue
        rest = list(image.next_siblings)
        if "(E)" not in alt and not any(isinstance(n, Tag) and "(E)" in str(n) for n in rest):
            continue
        before = [n for n in image.previous_siblings if isinstance(n, Tag) or n.strip()]
        if not before or (isinstance(before[0], Tag) and before[0].name in ("br", "p", "div", "center")):
            continue
        if isinstance(before[0], Tag) and "latexcenter" in (before[0].get("class") or []):
            continue
        choices = soup.new_tag("p")
        paragraph.insert_after(choices)
        choices.extend([image, *rest])


def _drop_trailing_spacers(soup: BeautifulSoup) -> None:
    """Remove the empty paragraphs (``<p><br/></p>``) that leave a blank band under many wiki statements,
    and the rules between the sections of a page."""
    for node in reversed(list(soup.contents)):
        if node.get_text(strip=True) or (isinstance(node, Tag) and (node.name == "img" or node.find("img"))):
            return
        if not isinstance(node, Tag) or node.name in ("p", "br", "div", "hr"):
            node.extract()


def normalize_fragment(
    fragment: str, base_url: str, *, aops: bool = False, problem_id: str | None = None
) -> NormalizedContent:
    """Sanitize ``fragment`` and return its normalized HTML, text and hash.

    ``aops`` repairs markup that AoPS left unrendered; other sources' text is never touched.
    Both the live ingestion and the legacy import set it for every AoPS provider. They also
    pass the ``problem_id`` of a statement, which applies its entry in ``corrections.py``.

    Raises :class:`InvalidContentError` when nothing meaningful is left, so an
    empty parser result can never be stored as a problem.
    """
    soup = _soup(fragment)

    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    for selector in _DROP_SELECTORS + (_AOPS_DROP_SELECTORS if aops else ()):
        for tag in soup.select(selector):
            tag.decompose()

    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag.attrs[attr]
        for attr in _URL_ATTRS:
            value = tag.attrs.get(attr)
            if not isinstance(value, str):
                continue
            value = value.strip()
            if _is_script_url(value):
                del tag.attrs[attr]
            elif value.startswith(("data:", "#")):
                tag.attrs[attr] = value
            else:
                tag.attrs[attr] = urljoin(base_url, value)
        tag.attrs.pop("srcset", None)
        tag.attrs.pop("loading", None)

    for anchor in soup.find_all("a"):
        anchor.unwrap()

    if aops:
        _restore_escaped_images(soup)
        repair_aops_tex(soup)
        fix_formula_images(soup)
        _close_up_punctuation(soup)
        _separate_choices(soup)
        _drop_trailing_spacers(soup)

    html = str(soup).strip()
    if problem_id is not None and (corrected := correct(problem_id, html)) != html:
        soup = _soup(corrected)
        html = str(soup).strip()
    text = extract_text(soup)

    has_image = soup.find("img") is not None
    if len(re.sub(r"\s", "", text)) < MIN_TEXT_CHARS and not has_image:
        raise InvalidContentError("statement is empty after normalization")

    return NormalizedContent(html=html, text=text, content_hash=sha256_text(html))


def extract_text(node: BeautifulSoup | Tag | str) -> str:
    """Plain text with TeX source inline (``$...$`` from AoPS alts, ``\\(...\\)`` from CEMC)."""
    soup = _soup(str(node))
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        classes = set(img.get("class") or [])
        if "pdf-crop" in classes:  # a problem cut out of a PDF: its alt is the problem's text
            img.replace_with(f"\n{alt}\n" if alt else " ")
        elif alt and (classes & _LATEX_IMG_CLASSES or alt.startswith(("$", "\\[", "[asy]"))):
            if alt.startswith("[asy]"):
                img.replace_with(" [diagram] ")
            else:
                img.replace_with(f" {alt} ")
        elif alt:
            img.replace_with(f" [image: {alt}] ")
        else:
            img.replace_with(" [image] ")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    text = soup.get_text(" ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{2,}", "\n", text).strip()
