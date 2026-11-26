"""Problems cut out of PDF papers (CMS's COMC / CLMC, HMMT).

These sources publish typeset PDFs only. Their maths can't be recovered from the text layer
(fractions, roots and exponents come out as loose characters), so a problem is shown as a
picture of its own region of the page, and the text layer is kept only as plain text for
search and for the bot's text fallback.

Ingestion (:func:`read_document`, :func:`segment`) finds where each problem is: lines are
rebuilt from the characters, a source-specific :class:`Layout` says what a problem label
looks like (``3.``, ``B2``, ``Question A1``), and labels are accepted only in sequence, at
the left edge of the text, so a stray "2." inside a statement can't start a problem. A
problem runs to the next label, a stop line ("Your solution:") or a part heading, across
pages if need be.

The statement HTML holds one ``<img class="pdf-crop">`` per page the problem covers. Its
``src`` is the PDF's URL with a ``#mathbot-crop=...`` fragment naming the page and box;
:func:`render_crop` turns that into a PNG when the problem is rendered (see
``render/assets.py``), so the database stores coordinates, not pictures, and a crop can
always be made again from the source PDF.
"""

from __future__ import annotations

import hashlib
import io
import re
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from html import escape
from urllib.parse import urldefrag

CROP_MARKER = "mathbot-crop="
# Part of every crop URL: bump when render_crop draws differently, so cached crops are made again.
# 2: italic (sheared) type is no longer taken for a watermark
CROP_VERSION = 2
CROP_SCALE = 4.0  # pixels per PDF point in a crop: sharp at RENDER_SCALE 2 for 1.8 CSS px per point
CSS_PX_PER_POINT = 1.8  # 10-11 pt type in a paper comes out near the 20px text of other statements
CROP_PADDING = 6.0  # points kept around the text column and above / below a problem
STOP_GAP = 6.0  # points left out above a stop line
FIGURE_GAP = 36.0  # a drawing that starts within this many points below a problem's ink belongs to it
INK = 160  # a pixel darker than this (0-255) is ink, not paper or light shading
EDGE_QUARTER = 0.25  # page heads and feet are looked for within this share of the page from each edge
EDGE_BAND = 0.12  # drawn page furniture (logos, rules) lies within this share of the page's height
FURNITURE_GAP = 6  # blank points that separate a page's head or foot (with its rule or logo) from the text
COLUMN_GAP = 1.5  # a horizontal gap this many times the type size splits a line into two columns
SECOND_COLUMN = 0.4  # problem labels this far across a page start a second column (a translation)
_WHITE = 250  # a pixel row whose darkest pixel is lighter than this is blank paper

# Header / footer bands: lines entirely inside them are page furniture (running heads,
# page numbers), never part of a problem.
HEADER_BAND = 0.075
FOOTER_BAND = 0.925


# PDFium is not thread-safe, and ingestion reads papers on a thread pool: every use goes through this.
PDFIUM_LOCK = threading.RLock()


class PdfLayoutError(ValueError):
    """The document does not have the problem structure its layout describes."""


@dataclass(frozen=True)
class Line:
    """One line of text, in points from the top-left corner of its page."""

    page: int
    x0: float
    top: float
    x1: float
    bottom: float
    text: str


@dataclass(frozen=True)
class Document:
    sizes: tuple[tuple[float, float], ...]  # (width, height) of each page, in points
    lines: tuple[Line, ...]  # in reading order: page, then top to bottom
    # Each page rendered in greyscale at 1 pixel per point (watermarks removed): the ink below a
    # problem's last line of text (a formula, a figure, a box) decides where the problem ends.
    images: tuple = ()


@dataclass(frozen=True)
class Piece:
    """The part of a problem on one page."""

    page: int
    x0: float
    top: float
    x1: float
    bottom: float


@dataclass
class Problem:
    section: str | None
    position: int  # number within its section ("B2" -> 2); the absolute number is its index + 1
    pieces: list[Piece] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    tail: list[Line] = field(default_factory=list)  # after a stop line, up to the next problem (a solution)

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines).strip()


@dataclass(frozen=True)
class Layout:
    """How a family of papers labels and bounds its problems.

    ``start`` matches the line that begins a problem; its ``number`` group is the problem's
    number within its part and an optional ``section`` group the part letter (``B2``).
    ``heading`` matches a part heading (``PART B``, ``Section B``) and its ``section``
    group; numbering restarts at 1 after one unless ``continuous``. ``stop`` lines end a
    problem without starting one. ``furniture`` lines are running heads or footers outside
    the header / footer bands; they bound the content of a page like the bands do.
    ``skip_label_line`` starts a problem below its label line (a label on a line of its own,
    like "Question A1 (4 points)") instead of at it. A statement containing a ``forbid``
    line (an "Answer:" from a solutions PDF) means a label was found in the wrong place,
    and the document is rejected.
    """

    start: re.Pattern[str]
    heading: re.Pattern[str] | None = None
    stop: tuple[re.Pattern[str], ...] = ()
    furniture: tuple[re.Pattern[str], ...] = ()
    forbid: tuple[re.Pattern[str], ...] = ()
    # After a stop line, the problem's next part ("(b)" after "(a)") continues its statement:
    # solutions PDFs solve each part right after stating it.
    resume: bool = False
    # Every problem must end at a stop line (a solutions PDF: each statement is followed by its
    # solution). One that runs into the next label means a stop line was not recognised.
    require_stop: bool = False
    # Lines repeated at the same height near the top or bottom of several pages (a guts round's
    # "Team ID#" box) are page furniture even outside the header and footer bands.
    repeated_heads: bool = False
    skip_label_line: bool = False
    continuous: bool = False  # numbers run on across parts (1-6 in A, 7-9 in B)
    max_indent: float = 24.0  # a label must start this close to the left edge of the text
    first_page: int = 0  # pages before this one (cover, instructions) are ignored
    pad_x: float = CROP_PADDING  # points added left and right of the text (more for boxed statements)
    pad_top: float = CROP_PADDING / 2  # points added above a problem's first line


# --------------------------------------------------------------------------- reading


def _char_rotated(textpage, index: int) -> bool:
    """Whether a character is turned (a diagonal watermark). Italic type that is slanted by a
    shear (only the matrix's ``c`` is non-zero) is ordinary text and is kept."""
    import pypdfium2.raw as raw

    matrix = raw.FS_MATRIX()
    if not raw.FPDFText_GetMatrix(textpage, index, matrix):
        return False
    return abs(matrix.b) > 1e-6


def read_document(data: bytes) -> Document:
    """Lines of every page of a PDF, rebuilt from its characters.

    Rotated characters (diagonal watermarks such as "Final") are left out, and so are
    characters without a box (the spaces and line breaks the PDF library generates):
    words are separated by measuring the gaps between characters instead.
    """
    with PDFIUM_LOCK:
        return _read_document(data)


def _read_document(data: bytes) -> Document:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise PdfLayoutError(f"not a readable PDF: {exc}") from exc
    sizes: list[tuple[float, float]] = []
    lines: list[Line] = []
    images = []
    try:
        for index in range(len(pdf)):
            page = pdf[index]
            left, bottom, right, top = page.get_cropbox()
            width, height = right - left, top - bottom
            sizes.append((width, height))
            textpage = page.get_textpage()
            chars = []
            space_before = False
            for i in range(textpage.count_chars()):
                text = textpage.get_text_range(i, 1)
                if not text or not text.strip():
                    space_before = True  # a space or line break the PDF library put between words
                    continue
                if _char_rotated(textpage, i):
                    continue
                # The loose box spans the font's full height, so every character of a line has
                # about the same box whatever its shape.
                c_left, c_bottom, c_right, c_top = textpage.get_charbox(i, loose=True)
                if c_right <= c_left or c_top <= c_bottom:
                    continue
                chars.append((c_left - left, top - c_top, c_right - left, top - c_bottom, text, space_before))
                space_before = False
            lines.extend(_lines(index, chars))
            textpage.close()
            _drop_rotated_text(page)
            images.append(page.render(scale=1.0, grayscale=True).to_pil().convert("L"))
            page.close()
    finally:
        pdf.close()
    return Document(tuple(sizes), tuple(lines), tuple(images))


Char = tuple[float, float, float, float, str, bool]  # left, top, right, bottom, text, space before


def _lines(page: int, chars: list[Char]) -> list[Line]:
    """Group characters whose vertical centres fall inside one another's height into lines.

    Superscripts and subscripts overlap their line's band and join it; the numerator and
    denominator of a displayed fraction do not, and form lines of their own.
    """
    groups: list[list[Char]] = []
    bands: list[list[float]] = []
    for char in sorted(chars, key=lambda c: (c[1] + c[3]) / 2):
        centre = (char[1] + char[3]) / 2
        if bands and bands[-1][0] <= centre <= bands[-1][1]:
            groups[-1].append(char)
        else:
            groups.append([char])
            bands.append([char[1], char[3]])
    out = []
    for band in groups:
        band.sort(key=lambda c: c[0])
        # A wide gap separates columns (a bilingual paper prints the French beside the English).
        runs: list[list[Char]] = [[]]
        for char in band:
            if runs[-1]:
                previous = runs[-1][-1]
                size = min(previous[3] - previous[1], char[3] - char[1])
                if char[0] - previous[2] > COLUMN_GAP * size:
                    runs.append([])
            runs[-1].append(char)
        out.extend(_line(page, run) for run in runs)
    return sorted(out, key=lambda line: (line.top, line.x0))


def _line(page: int, group: list[Char]) -> Line:
    text = ""
    previous = None
    for char in group:
        if previous is not None:
            gap = char[0] - previous[2]
            size = min(previous[3] - previous[1], char[3] - char[1])
            if char[5] or gap > 0.3 * size:
                text += " "
        text += char[4]
        previous = char
    return Line(
        page,
        min(c[0] for c in group),
        min(c[1] for c in group),
        max(c[2] for c in group),
        max(c[3] for c in group),
        re.sub(r"\s+", " ", text).strip(),
    )


# --------------------------------------------------------------------------- segmenting


_PAGE_NUMBER = re.compile(r"^(?:page\s*|p\.\s*)?[-–]?\s*\d{1,3}(?:\s*(?:/|of)\s*\d{1,3})?\s*[-–]?$", re.IGNORECASE)


def _beside_text(document: Document, line: Line) -> bool:
    """Whether other text sits right next to ``line`` (an exponent beside its formula, not a
    page number standing on its own)."""
    return any(
        other is not line
        and other.page == line.page
        and other.top < line.bottom
        and line.top < other.bottom
        and other.x0 - 20 < line.x1
        and line.x0 < other.x1 + 20
        for other in document.lines
    )


def _furniture_lines(document: Document, layout: Layout) -> set[int]:
    """``id()`` of every line that is page furniture: running heads and footers, page numbers.

    A line near the top or bottom edge counts only if it looks like furniture (it recurs on
    other pages, is a page number, or matches the layout's patterns): old papers print
    questions down to the very foot of the page.
    """

    def shape(text: str) -> str:
        return re.sub(r"\d+", "#", text)

    in_band = [
        line
        for line in document.lines
        if line.bottom <= document.sizes[line.page][1] * HEADER_BAND
        or line.top >= document.sizes[line.page][1] * FOOTER_BAND
    ]
    pages_by_shape: dict[str, set[int]] = {}
    for line in in_band:
        pages_by_shape.setdefault(shape(line.text), set()).add(line.page)
    out = set()
    for line in document.lines:
        if layout.start.match(line.text):
            continue  # a label that shares its line with a running head is still a label
        if any(pattern.search(line.text) for pattern in layout.furniture):
            out.add(id(line))
        height = document.sizes[line.page][1]
        near_edge = line.top >= height * 0.85 or line.bottom <= height * 0.12
        if near_edge and _PAGE_NUMBER.match(line.text) and not _beside_text(document, line):
            out.add(id(line))  # a page number set a little further in than usual
    for line in in_band:
        if layout.start.match(line.text) and id(line) not in out:
            continue
        recurring = len(pages_by_shape[shape(line.text)]) > 1 or len(document.sizes) == 1
        if recurring or (_PAGE_NUMBER.match(line.text) and not _beside_text(document, line)):
            out.add(id(line))
    # Running heads set further in (a contest's name and a "Team ID#" box above each page of a
    # guts round): the same line at the same height near the top or bottom of several pages.
    if layout.repeated_heads and len(document.sizes) > 1:
        places: dict[tuple[str, int], set[int]] = {}
        for line in document.lines:
            places.setdefault((shape(line.text), round(line.top / 3)), set()).add(line.page)
        for line in document.lines:
            height = document.sizes[line.page][1]
            if not (line.bottom <= height * 0.25 or line.top >= height * 0.8) or layout.start.match(line.text):
                continue
            if any(p.search(line.text) for p in layout.stop) or (layout.heading and layout.heading.match(line.text)):
                continue  # "Your solution:" recurs too, and must still end its question
            key = shape(line.text)
            pages = set().union(*(places.get((key, round(line.top / 3) + d), set()) for d in (-1, 0, 1)))
            if len(pages) > 1:
                out.add(id(line))
    return out


def _next_section(section: str | None) -> str:
    return "A" if section is None else chr(ord(section) + 1)


def segment(document: Document, layout: Layout) -> list[Problem]:
    """Cut a document into problems. Raises :class:`PdfLayoutError` when none are found."""
    furniture = _furniture_lines(document, layout)
    content = [line for line in document.lines if line.page >= layout.first_page and id(line) not in furniture]
    if not content:
        raise PdfLayoutError("the PDF has no text layer (a scan?)")
    # A bilingual paper sets the translation in a second column, with labels of its own: on
    # such a page only the first column is read and pictured.
    for page in _pages(content):
        width = document.sizes[page][0]
        second = [
            line.x0
            for line in content
            if line.page == page and line.x0 > width * SECOND_COLUMN and layout.start.match(line.text)
        ]
        if second:
            split = min(second) - 4
            content = [line for line in content if line.page != page or line.x0 < split]
    # Per page: books alternate their margins between left and right pages.
    left_edge = {page: _left_edge([line for line in content if line.page == page], layout) for page in _pages(content)}
    columns = {
        page: (
            max(0.0, min(line.x0 for line in content if line.page == page) - layout.pad_x),
            min(document.sizes[page][0], max(line.x1 for line in content if line.page == page) + layout.pad_x),
        )
        for page in _pages(content)
    }

    problems: list[Problem] = []
    section: str | None = None
    position = 0
    current: Problem | None = None
    start_at: tuple[int, float] | None = None  # (page, top) where the current problem's region begins

    resumable: Problem | None = None  # stopped by a stop line; its next part ("(b)") may follow

    def close(end_page: int, end_top: float) -> None:
        nonlocal current, start_at, resumable
        if current is not None:
            if start_at is not None:
                current.pieces.extend(
                    _pieces(document, layout, furniture, content, start_at, (end_page, end_top), columns)
                )
            if not problems or problems[-1] is not current:
                problems.append(current)  # kept even without a region, so check_problems reports it
        current, start_at, resumable = None, None, None

    for line in content:
        if layout.heading is not None and (heading := layout.heading.match(line.text)):
            close(line.page, line.top)
            section = heading.group("section").upper()
            if not layout.continuous:
                position = 0
            continue
        if any(pattern.search(line.text) for pattern in layout.stop):
            # A stop line often sits beside a box whose border rises above the text.
            stopped = current
            close(line.page, line.top - STOP_GAP)
            resumable = stopped
            if problems:
                problems[-1].tail.append(line)
            continue
        if layout.resume and resumable is not None and _next_part(resumable, line):
            # A solutions PDF answers part (a) before stating part (b): the statement goes on.
            current, resumable, start_at = resumable, None, (line.page, line.top)
            current.lines.append(line)
            continue
        label = layout.start.match(line.text) if line.x0 <= left_edge[line.page] + layout.max_indent else None
        if label is not None:
            accepted = _accept(label, section, position)
            if accepted is not None:
                # A tall formula on the label's line (a fraction's numerator) rises above it and was
                # read as a line of its own: it belongs to this problem, not the end of the last one.
                before = current.lines if current is not None else problems[-1].tail if problems else []
                risers = [] if layout.skip_label_line else _risers(content, line)
                top = min([line.top, *(r.top for r in risers)])
                before[:] = [x for x in before if x not in risers]
                close(line.page, top)
                section, position = accepted
                current = Problem(section, position)
                if not layout.skip_label_line:
                    start_at = (line.page, top)
                    current.lines.extend([*risers, line])
                # else the region starts at the statement's first line, below any rule under the label
                continue
        if current is not None:
            if start_at is None:
                start_at = (line.page, line.top)
            current.lines.append(line)
        elif problems:
            problems[-1].tail.append(line)
    last_page = len(document.sizes) - 1
    close(last_page, _content_bottom(document, furniture, last_page))

    if not problems:
        raise PdfLayoutError("no problem labels found")
    for problem in problems:
        if layout.require_stop and not any(p.search(x.text) for x in problem.tail[:1] for p in layout.stop):
            raise PdfLayoutError(
                f"problem {problem.section or ''}{problem.position} is not followed by a stop line "
                "(its end could not be found)"
            )
        for line in problem.lines:
            if any(pattern.search(line.text) for pattern in layout.forbid):
                raise PdfLayoutError(
                    f"problem {problem.section or ''}{problem.position} contains {line.text[:60]!r}: "
                    "a label was found in the wrong place"
                )
    return problems


def _risers(content: Sequence[Line], label: Line) -> list[Line]:
    """Lines that sit right of ``label``'s start and reach down into its line from above: the
    top of a formula on the label's line (a fraction's numerator), not text of their own."""
    return [
        line
        for line in content
        if line.page == label.page and line.top < label.top and line.bottom >= label.top + 1 and line.x0 > label.x0 + 10
    ]


_PART_LABEL = re.compile(r"^\(([a-h])\)")


def _next_part(problem: Problem, line: Line) -> bool:
    """Whether ``line`` starts the part after the last one ``problem`` states ("(b)" after "(a)")."""
    letters = [m.group(1) for statement in problem.lines if (m := _PART_LABEL.match(statement.text))]
    match = _PART_LABEL.match(line.text)
    return bool(letters) and match is not None and match.group(1) == chr(ord(max(letters)) + 1)


def _pages(content: Sequence[Line]) -> list[int]:
    return sorted({line.page for line in content})


def _left_edge(content: Sequence[Line], layout: Layout) -> float:
    """Where labels start on a page: its leftmost label-like line (text can be indented past it)."""
    labels = [line.x0 for line in content if layout.start.match(line.text)]
    return min(labels) if labels else min(line.x0 for line in content)


def _accept(label: re.Match[str], section: str | None, position: int) -> tuple[str | None, int] | None:
    """The (section, position) a label starts, or None when it is out of sequence."""
    number = int(label.group("number"))
    letter = label.groupdict().get("section")
    if letter is None:
        return (section, number) if number == position + 1 else None
    letter = letter.upper()
    if letter == section and number == position + 1:
        return letter, number
    if letter == _next_section(section) and number == 1:
        return letter, number
    return None


def _pieces(
    document: Document,
    layout: Layout,
    furniture: set[int],
    content: Sequence[Line],
    start: tuple[int, float],
    end: tuple[int, float],
    columns: dict[int, tuple[float, float]],
) -> list[Piece]:
    """One box per page from ``start`` down to ``end`` (exclusive), inside each page's content area."""
    pieces = []
    for page in range(start[0], end[0] + 1):
        width, height = document.sizes[page]
        on_page = [line for line in content if line.page == page]
        top = start[1] if page == start[0] else _content_top(document, furniture, page)
        bottom = end[1] if page == end[0] else _content_bottom(document, furniture, page)
        if page not in columns or not any(top - 1 <= line.top and line.bottom <= bottom + 1 for line in on_page):
            continue  # nothing of this problem on this page (it ended at the top of the next)
        x0, x1 = columns[page]
        pad = layout.pad_top if page == start[0] else 0.0
        inside = (x0 + layout.pad_x, x1 - layout.pad_x)  # the text column, without frames around it
        # Furniture below the problem's text on the same page (a guts round's cut line and the next
        # round's header in mid-page) ends it, whatever ink follows.
        last_text = max(
            (line.bottom for line in on_page if top - 1 <= line.top and line.bottom <= bottom + 1), default=top
        )
        below = [
            line.top
            for line in document.lines
            if line.page == page
            and id(line) in furniture
            and last_text <= line.top < bottom
            and line.x0 < inside[1]
            and line.x1 > inside[0]
            # Wide lines only: a lone number there may be a figure's label, not a page number.
            and line.x1 - line.x0 >= 0.3 * (inside[1] - inside[0])
        ]
        bottom = min([bottom, *(edge - 1 for edge in below)])
        bottom = _ink_bottom(document, on_page, page, inside, top, bottom)
        pieces.append(Piece(page, x0, max(0.0, top - pad), x1, min(height, bottom)))
    return pieces


def _ink_bottom(
    document: Document,
    on_page: Sequence[Line],
    page: int,
    inside: tuple[float, float],
    top: float,
    bottom: float,
) -> float:
    """Where a problem's region really ends: its last line of text, or further down through
    whatever is printed below it (a formula, a figure, the edge of a box) as long as no blank
    gap of more than :data:`FIGURE_GAP` interrupts it. Answer space below a statement (blank
    paper, then ruled lines) is left out. Only ink inside the text column counts, so the sides
    of a frame around both question and answer space do not join them up."""
    ink = max((line.bottom for line in on_page if top - 1 <= line.top and line.bottom <= bottom + 1), default=top)
    if page >= len(document.images):
        return min(bottom - 0.5, ink + CROP_PADDING)
    image = document.images[page]
    left, right = max(0, int(inside[0])), min(image.width, int(inside[1]) + 1)
    y, end = int(ink), min(image.height, int(bottom))
    blank = 0
    while y < end and right > left:
        if image.crop((left, y, right, y + 1)).getextrema()[0] < INK:
            ink, blank = y + 1, 0
        else:
            blank += 1
            if blank > FIGURE_GAP:
                break
        y += 1
    return min(bottom - 0.5, ink + CROP_PADDING)


def _content_top(document: Document, furniture: set[int], page: int) -> float:
    height = document.sizes[page][1]
    heads = [
        line.bottom
        for line in document.lines
        if line.page == page and line.bottom <= height * EDGE_QUARTER and id(line) in furniture
    ]
    edge = max(heads, default=0.0) + 1
    if heads:
        # The rule or logo that goes with a running head, but never past the first line of text.
        below = [x.top for x in document.lines if x.page == page and id(x) not in furniture and x.top >= edge]
        edge = min(_block_edge(document, page, edge, +1), min(below, default=edge) - 1)
    edge = max(edge, _edge_blocks(document, furniture, page, top=True))
    # Never through a line of the content (a page number can share a line's height).
    cut = [x.top for x in document.lines if x.page == page and id(x) not in furniture and x.top < edge < x.bottom]
    return min([edge, *cut]) - (1 if cut else 0)


def _edge_blocks(document: Document, furniture: set[int], page: int, *, top: bool) -> float:
    """Inner side of drawn page furniture without text (a logo, a rule): the blocks of ink that
    lie wholly within :data:`EDGE_BAND` of the top (or bottom) edge, before the first (after the
    last) line of text. 0 (the page height) when there are none."""
    height = document.sizes[page][1]
    text = [x for x in document.lines if x.page == page and id(x) not in furniture]
    if page >= len(document.images) or not text:
        return 0.0 if top else height
    image = document.images[page]
    rows = [image.crop((0, y, image.width, y + 1)).getextrema()[0] < INK for y in range(image.height)]
    blocks: list[tuple[int, int]] = []
    y = 0
    while y < len(rows):
        if rows[y]:
            start = y
            gap = 0
            while y < len(rows) and gap < FURNITURE_GAP:
                gap = 0 if rows[y] else gap + 1
                y += 1
            blocks.append((start, y - gap))
        else:
            y += 1
    if top:
        limit, edge = min(x.top for x in text), 0.0
        for _start, end in blocks:
            if end > limit or end > height * EDGE_BAND:
                break
            edge = end + 1.0
        return edge
    limit, edge = max(x.bottom for x in text), height
    for start, _end in reversed(blocks):
        if start < limit or start < height * (1 - EDGE_BAND):
            break
        edge = start - 1.0
    return edge


def _block_edge(document: Document, page: int, edge: float, step: int) -> float:
    """Move ``edge`` (the inner side of a page head or foot) on through the ink touching it - a
    rule, a logo - until blank paper of at least :data:`FURNITURE_GAP` points."""
    if page >= len(document.images):
        return edge
    image = document.images[page]
    y = int(edge)
    blank = 0
    last_ink = edge
    while 0 <= y < image.height and blank < FURNITURE_GAP:
        if image.crop((0, y, image.width, y + 1)).getextrema()[0] < INK:
            last_ink, blank = (y + 1 if step > 0 else y), 0
        else:
            blank += 1
        y += step
    return last_ink


def _content_bottom(document: Document, furniture: set[int], page: int) -> float:
    height = document.sizes[page][1]
    feet = [
        line.top
        for line in document.lines
        if line.page == page and line.top >= height * (1 - EDGE_QUARTER) and id(line) in furniture
    ]
    edge = min(feet, default=height) - 1
    if feet:
        # The rule or logo that goes with a footer, but never past the last line of text.
        above = [x.bottom for x in document.lines if x.page == page and id(x) not in furniture and x.bottom <= edge]
        edge = max(_block_edge(document, page, edge, -1), max(above, default=edge) + 1)
    edge = min(edge, _edge_blocks(document, furniture, page, top=False))
    # Never through a line of the content (a page number can share a line's height).
    cut = [x.bottom for x in document.lines if x.page == page and id(x) not in furniture and x.top < edge < x.bottom]
    return max([edge, *cut]) + (1 if cut else 0)


# --------------------------------------------------------------------------- statement HTML


def pdf_digest(data: bytes) -> str:
    """Short fingerprint of a PDF, carried in crop URLs so a changed file is never cropped."""
    return hashlib.sha256(data).hexdigest()[:16]


@dataclass(frozen=True)
class Crop:
    pdf_url: str
    digest: str  # pdf_digest() of the file the coordinates were measured on
    page: int
    box: tuple[float, float, float, float]  # x0, top, x1, bottom in points


def crop_url(pdf_url: str, digest: str, piece: Piece) -> str:
    """``https://.../problems.pdf#mathbot-crop=v2:1a2b3c4d5e6f7a8b:p0:66.0,120.5,546.0,301.2``."""
    base, _ = urldefrag(pdf_url)
    box = f"{piece.x0:.1f},{piece.top:.1f},{piece.x1:.1f},{piece.bottom:.1f}"
    return f"{base}#{CROP_MARKER}v{CROP_VERSION}:{digest}:p{piece.page}:{box}"


def statement_html(pdf_url: str, digest: str, problem: Problem) -> str:
    """One picture per page piece; the first carries the problem's text as its alt text."""
    images = []
    for index, piece in enumerate(problem.pieces):
        alt = escape(problem.text, quote=True) if index == 0 else ""
        width = round((piece.x1 - piece.x0) * CSS_PX_PER_POINT)
        images.append(
            f'<img class="pdf-crop" src="{escape(crop_url(pdf_url, digest, piece), quote=True)}" alt="{alt}" '
            f'width="{width}" style="display: block; margin: 0 0 4px 0">'
        )
    return "\n".join(images)


_CROP_SPEC = re.compile(r"^(?:v\d+:)?([0-9a-f]{16}):p(\d+):(-?[\d.]+),(-?[\d.]+),(-?[\d.]+),(-?[\d.]+)$")


def parse_crop_url(url: str) -> Crop | None:
    """The :class:`Crop` a :func:`crop_url` names, or None for any other URL."""
    base, fragment = urldefrag(url)
    if not fragment.startswith(CROP_MARKER):
        return None
    match = _CROP_SPEC.match(fragment[len(CROP_MARKER) :])
    if match is None:
        return None
    x0, top, x1, bottom = (float(match.group(i)) for i in range(3, 7))
    if not (x1 > x0 and bottom > top):
        return None
    return Crop(base, match.group(1), int(match.group(2)), (x0, top, x1, bottom))


# --------------------------------------------------------------------------- rendering


def _drop_rotated_text(page) -> bool:
    """Remove rotated text objects (diagonal "Final" / "Draft" watermarks). True if any were."""
    import pypdfium2.raw as raw

    removed = False
    for obj in list(page.get_objects(max_depth=1)):
        if obj.type != raw.FPDF_PAGEOBJ_TEXT:
            continue
        matrix = obj.get_matrix()
        if abs(matrix.b) > 1e-6:  # turned, not just slanted like italic type
            page.remove_obj(obj)
            removed = True
    if removed:
        page.gen_content()
    return removed


def render_crop(data: bytes, page_index: int, box: tuple[float, float, float, float]) -> bytes:
    with PDFIUM_LOCK:
        return _render_crop(data, page_index, box)


def _render_crop(data: bytes, page_index: int, box: tuple[float, float, float, float]) -> bytes:
    """PNG (greyscale) of one box of one page, with blank rows above and below trimmed off."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        if not 0 <= page_index < len(pdf):
            raise PdfLayoutError(f"page {page_index} is not in the PDF ({len(pdf)} pages)")
        page = pdf[page_index]
        left, bottom, right, top = page.get_cropbox()
        width, height = right - left, top - bottom
        x0, y0, x1, y1 = box
        x0, x1 = max(0.0, x0), min(width, x1)
        y0, y1 = max(0.0, y0), min(height, y1)
        if x1 - x0 < 1 or y1 - y0 < 1:
            raise PdfLayoutError(f"crop box {box} is outside page {page_index}")
        _drop_rotated_text(page)
        bitmap = page.render(scale=CROP_SCALE, crop=(x0, height - y1, width - x1, y0), grayscale=True)
        image = bitmap.to_pil().convert("L")
        page.close()
    finally:
        pdf.close()
    image = _trim_rows(image)
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _trim_rows(image):
    """Cut blank paper off the top and bottom, keeping a little margin. Width is kept, so
    the picture's displayed width (set when the statement was made) stays right."""
    from PIL import ImageOps

    inverted = ImageOps.invert(image).point(lambda v: 255 if v > 255 - _WHITE else 0)
    bbox = inverted.getbbox()
    if bbox is None:
        raise PdfLayoutError("crop is blank")
    margin = round(CROP_PADDING * CROP_SCALE / 2)
    return image.crop((0, max(0, bbox[1] - margin), image.width, min(image.height, bbox[3] + margin)))


def check_problems(problems: Iterable[Problem], expected: Sequence[tuple[str | None, int]] | None = None) -> None:
    """Raise :class:`PdfLayoutError` unless every problem has a region and text, and (when given)
    the labels are exactly ``expected`` (``[("A", 1), ("A", 2), ...]``)."""
    problems = list(problems)
    for problem in problems:
        label = f"{problem.section or ''}{problem.position}"
        if not problem.pieces:
            raise PdfLayoutError(f"problem {label} has no region on the page")
        if len(re.sub(r"\s", "", problem.text)) < 8:
            raise PdfLayoutError(f"problem {label} has almost no text ({problem.text!r})")
    if expected is not None:
        found = [(p.section, p.position) for p in problems]
        if found != list(expected):
            raise PdfLayoutError(f"found problems {_labels(found)}, expected {_labels(expected)}")


def _labels(pairs: Iterable[tuple[str | None, int]]) -> str:
    return " ".join(f"{s or ''}{n}" for s, n in pairs) or "none"


__all__ = [
    "CROP_MARKER",
    "Crop",
    "Document",
    "Layout",
    "Line",
    "PdfLayoutError",
    "Piece",
    "Problem",
    "check_problems",
    "crop_url",
    "parse_crop_url",
    "pdf_digest",
    "read_document",
    "render_crop",
    "segment",
    "statement_html",
]


def misaligned_lines(document: Document) -> float:
    """Share of text lines whose box on the rendered page is blank paper.

    Some PDFs place their text layer away from the glyphs that are drawn (2012-2016 COMC
    booklets), so boxes found from the text would cut through the wrong questions. Such a file
    has many lines whose box shows no ink at all; a sound one has (almost) none.
    """
    blank = total = 0
    for line in document.lines:
        if line.page >= len(document.images):
            continue
        image = document.images[line.page]
        box = (
            max(0, int(line.x0)),
            max(0, int(line.top)),
            min(image.width, int(line.x1) + 1),
            min(image.height, int(line.bottom) + 1),
        )
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        total += 1
        if image.crop(box).getextrema()[0] >= INK:
            blank += 1
    return blank / total if total else 1.0
