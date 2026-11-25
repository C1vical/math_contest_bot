r"""Corrections to statements whose source page is wrong or incomplete.

Each entry was checked by hand against the official paper (or, for AMC and AIME papers,
against the licensed copies at live.poshenloh.com). They are applied when a statement is
normalized, for live ingestion and the legacy import alike; the raw payload keeps what the
source said. Every step looks for the mistake it corrects, so a page that has been fixed at
the source, or a statement corrected already, is left alone.

Figures are files in ``mathbot/figures``: crops of the official paper, or redrawn. They are
embedded as ``data:`` URIs, like the pictures of CEMC statements.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from html import escape
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

from mathbot.tex import typeset_choices

FIGURES = Path(__file__).parent / "figures"
_TYPES = {".png": "image/png", ".svg": "image/svg+xml"}
_BLOCKS = ("p", "div", "figcaption", "ul", "li")

_OPENING = re.compile(r'[("[]+$')
_CLOSING = re.compile(r"(?:'s\b|th\b|[-,.;:?!)\]])+")

Step = Callable[[str], str]


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def figure(name: str, width: int, alt: str) -> str:
    """The figure file ``name`` as an ``<img>``, centred and ``width`` CSS pixels wide."""
    path = FIGURES / name
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return (
        f'<img alt="{escape(alt)}" class="asy-image" src="data:{_TYPES[path.suffix]};base64,{data}" width="{width}"/>'
    )


# --------------------------------------------------------------------------- steps


def replace(find: str, new: str) -> Step:
    return lambda html: html.replace(find, new)


def prepend(new: str, *, unless: str) -> Step:
    return lambda html: html if unless in html else new + html


def append(new: str, *, unless: str) -> Step:
    return lambda html: html if unless in html else html + new


def whole(new: str, *, when: str) -> Step:
    """Replace the whole statement if it contains ``when``."""
    return lambda html: new if when in html else html


def drop(prefix: str) -> Step:
    """Remove each innermost block whose text starts with ``prefix``."""

    def step(html: str) -> str:
        soup = _soup(html)
        found = [
            tag
            for tag in soup.find_all(_BLOCKS)
            if " ".join(tag.get_text().split()).startswith(prefix) and not tag.find(_BLOCKS)
        ]
        for tag in found:
            tag.decompose()
        return str(soup).strip() if found else html

    return step


def drop_selector(selector: str) -> Step:
    def step(html: str) -> str:
        soup = _soup(html)
        found = soup.select(selector)
        for tag in found:
            tag.decompose()
        return str(soup).strip() if found else html

    return step


def choices(alt_part: str, find: str = "", new: str = "") -> Step:
    """Typeset the answer choices of the formula image whose alt text holds ``alt_part``,
    with ``find`` replaced by ``new`` in its TeX."""

    def step(html: str) -> str:
        soup = _soup(html)
        for image in soup.find_all("img"):
            alt = str(image.get("alt") or "")
            if alt_part in alt and typeset_choices(soup, image, alt.replace(find, new) if find else alt):
                return str(soup).strip()
        return html

    return step


def image(alt_part: str, new: str) -> Step:
    """Replace the image whose alt text or address holds ``alt_part`` with ``new``."""

    def step(html: str) -> str:
        soup = _soup(html)
        for img in soup.find_all("img"):
            if alt_part in str(img.get("alt") or "") or alt_part in str(img.get("src") or ""):
                img.replace_with(_soup(new))
                return str(soup).strip()
        return html

    return step


def before_choices(new: str, *, unless: str) -> Step:
    """Insert ``new`` in front of the block that holds the answer choices."""

    def step(html: str) -> str:
        if unless in html:
            return html
        soup = _soup(html)
        for img in soup.find_all("img"):
            block = img.find_parent("p")
            if "(A)" in str(img.get("alt") or "") and isinstance(block, Tag):
                block.insert_before(_soup(new))
                return str(soup).strip()
        return html

    return step


def lettered_choices(html: str) -> str:
    """Label a plain a.-e. list of five choices (A)-(E)."""
    soup = _soup(html)
    for ol in soup.find_all("ol", attrs={"type": "a"}):
        if len(ol.find_all("li", recursive=False)) == 5:
            del ol["type"]
            ol["class"] = "mc"
            for li in ol.find_all("li", recursive=False):
                li["style"] = "margin: 10px 0"  # room for fractions
    return str(soup).strip()


def keep_together(html: str) -> str:
    """Keep each formula picture on one line with the punctuation that touches it, and drop
    punctuation that would stand alone under a displayed formula."""
    soup = _soup(html)
    changed = False
    for img in soup.find_all("img", class_="latex"):
        before, after = img.previous_sibling, img.next_sibling
        opening = _OPENING.search(before) if isinstance(before, NavigableString) else None
        closing = _CLOSING.match(after) if isinstance(after, NavigableString) else None
        if not (opening or closing) or img.find_parent("span", style="white-space: nowrap"):
            continue
        span = soup.new_tag("span", attrs={"style": "white-space: nowrap"})
        img.wrap(span)
        if opening and isinstance(before, NavigableString):
            span.insert(0, opening.group())
            before.replace_with(before[: opening.start()])
        if closing and isinstance(after, NavigableString):
            span.append(closing.group())
            after.replace_with(after[closing.end() :])
        changed = True
    for img in soup.find_all("img", class_="latexcenter"):
        after = img.next_sibling
        if isinstance(after, NavigableString) and (stray := _CLOSING.match(after)):
            after.replace_with(after[stray.end() :])
            changed = True
    return str(soup).strip() if changed else html


def _choice_line(*values: str) -> str:
    line = r"\qquad ".join(rf"\textbf{{({letter})}}\ {value}" for letter, value in zip("ABCDE", values, strict=True))
    return rf"<p>\({line}\)</p>"


_INVALID_URL = '<span style="color:red">Invalid URL</span>'
_NISOS = (
    "<p>In the very center of the Irenic Sea lie the beautiful Nisos Isles. In 1998 the number of people on these "
    "islands is only 200, but the population triples every 25 years. Queen Irene has decreed that there must be at "
    "least 1.5 square miles for every person living in the Isles. The total area of the Nisos Isles is 24,900 square "
    "miles.</p>"
)

_ARCS_2019 = (
    image(r"$\overarc{AEB},\overarc{BFC},$", r"\(\overparen{AEB}\), \(\overparen{BFC}\),"),
    image(r"$\overarc{CGD},$", r"\(\overparen{CGD}\),"),
)

# --------------------------------------------------------------------------- the corrections

CORRECTIONS: dict[str, tuple[Step, ...]] = {
    # --- question or choices missing
    "ahsme-1950-p12": (  # vandalised
        whole(
            "<p>As the number of sides of a polygon increases from \\(3\\) to \\(n\\), the sum of the exterior angles "
            "formed by extending each side in succession:</p><p>"
            + "<br/>".join(
                rf"\(\textbf{{({letter})}}\ {text}\)"
                for letter, text in zip(
                    "ABCDE",
                    (
                        r"\text{increases}",
                        r"\text{decreases}",
                        r"\text{remains constant}",
                        r"\text{cannot be predicted}",
                        r"\text{becomes }(n-3)\text{ straight angles}",
                    ),
                    strict=True,
                )
            )
            + "</p>",
            when="Isaac is using increasing angles",
        ),
    ),
    "ahsme-1971-p24": (
        prepend(
            "<p>Pascal's triangle is an array of positive integers, shown below, in which the first row is "
            "\\(1\\), the second row is two \\(1\\)'s, each row begins and ends with \\(1\\), and each other entry "
            "is the sum of the two entries above it.</p>",
            unless="Pascal's triangle",
        ),
        append(
            "<p>The quotient of the number of entries in the first \\(n\\) rows which are not \\(1\\)'s and the number "
            "of \\(1\\)'s is:</p>"
            + _choice_line(
                r"\dfrac{n^2-n}{2n-1}",
                r"\dfrac{n^2-n}{4n-2}",
                r"\dfrac{n^2-2n}{2n-1}",
                r"\dfrac{n^2-3n+2}{4n-2}",
                r"\text{None of these}",
            ),
            unless="The quotient of the number",
        ),
    ),
    "ahsme-1982-p14": (
        replace("are diameters of circle  <img", "are diameters of circles <img"),
        append(_choice_line("20", r"15\sqrt{2}", "24", "25", r"\text{none of these}"), unless=r"\textbf{(A)}"),
    ),
    "ajhsme-1998-p15": (prepend(_NISOS, unless="Irenic Sea"),),
    "ajhsme-1998-p16": (prepend(_NISOS, unless="Irenic Sea"),),
    "ajhsme-1998-p17": (prepend(_NISOS, unless="Irenic Sea"),),
    "ajhsme-1998-p20": (append(_choice_line("9", "16", "18", "24", "36"), unless=r"\textbf{(A)}"),),
    "amc8-2003-p01": (append(_choice_line("12", "16", "20", "22", "26"), unless=r"\textbf{(A)}"),),
    "amc8-2026-p11": (
        append(
            f"<p>{figure('amc8-2026-p11.png', 400, 'Squares of side 1, 1, 2, 3, 5 with quarter circles')}</p>"
            + _choice_line(r"4\pi", r"6\pi", r"\dfrac{13}{2}\pi", r"8\pi", r"13\pi"),
            unless=r"\textbf{(A)}",
        ),
    ),
    "ahsme-1973-p11": (
        replace(
            "centered at the origin  of a rectangular coordinate system with positive  and  axes  and  is shown in "
            "each figure  to  below.",
            "centered at the origin \\(O\\) of a rectangular coordinate system with positive \\(x\\) and \\(y\\) axes "
            "\\(OX\\) and \\(OY\\) is shown in each figure \\(I\\) to \\(IV\\) below.",
        ),
    ),
    "ahsme-1997-p13": (
        replace("the order of the digits of is a perfect", "the order of the digits of \\(N\\) is a perfect"),
    ),
    # --- figure missing (CMO: official paper; AIME: licensed copy of the paper, 1991 redrawn)
    "cmo-1973-p04": (
        replace(_INVALID_URL, figure("cmo-1973-p04.png", 290, "Polygon P0 to P8 with six diagonals drawn")),
    ),
    "cmo-1974-p05": (
        replace(_INVALID_URL, figure("cmo-1974-p05.png", 270, "Circle on diameter AB with the tangents at A, B and X")),
    ),
    "cmo-1974-p07": (
        replace(_INVALID_URL, figure("cmo-1974-p07.png", 220, "Circular road joined to the terminus at Q")),
    ),
    "cmo-2005-p01": (
        append(
            "<br/>"
            + figure("cmo-2005-p01.png", 270, "A path from the top row to the middle triangle of the bottom row"),
            unless='<img alt="A path from',
        ),
    ),
    "cmo-2007-p01": (
        append(
            "<br/>" + figure("cmo-2007-p01.png", 290, "An 8 by 9 board with six horizontal dominoes placed"),
            unless='<img alt="An 8 by 9 board',
        ),
    ),
    "aime-1991-p11": (
        replace("can be written in the from ", "can be written in the form "),
        append(
            f"<p>{figure('aime-1991-p11.svg', 300, 'A circle covered by a ring of twelve tangent disks')}</p>",
            unless='<img alt="A circle covered',
        ),
    ),
    "aime2-2023-p14": (
        append(
            f"<p>{figure('aime2-2023-p14.png', 520, 'A tilted cube partly filled with water')}</p>",
            unless='<img alt="A tilted cube',
        ),
    ),
    "aime2-2025-p03": (
        append(
            f"<p>{figure('aime2-2025-p03.png', 220, 'A 2 by 2 grid with solid red and dashed blue sides')}</p>",
            unless='<img alt="A 2 by 2 grid',
        ),
    ),
    "amc10b-2007-p04": (  # the same question as 2007 AMC 12B 3, which has its figure
        before_choices(
            '<p><img alt="" class="mw-file-element" '
            'src="https://artofproblemsolving.com/wiki/images/8/8a/2007_12B_AMC-3.png"/></p>',
            unless="2007_12B_AMC-3.png",
        ),
    ),
    # --- drawn too small: these pictures were made wider after the page that gives their size
    "ahsme-1971-p32": (choices(r"\textbf{(A) }\textstyle"),),
    "ahsme-1952-p35": (choices(r"\textbf{(A)}\ \frac {3"),),
    "amc8-2008-p18": (choices(r"\textbf{(A)}\ 10\pi"),),
    "amc12a-2016-p05": (choices(r"\textbf{(A)}\ \text{an odd integer"),),
    "amc10a-2004-p05": (image("AMC10_2004A_4.gif", figure("amc10a-2004-p05.svg", 120, "A 3 by 3 grid of points")),),
    "imo-1966-p05": (
        image(
            r"|a_1 - a_2| x_2 + |a_1 - a_3| x_3",
            r"\[\begin{array}{rrrrl}"
            r" & |a_1-a_2|x_2 & +\,|a_1-a_3|x_3 & +\,|a_1-a_4|x_4 & = 1\\"
            r"|a_2-a_1|x_1 & & +\,|a_2-a_3|x_3 & +\,|a_2-a_4|x_4 & = 1\\"
            r"|a_3-a_1|x_1 & +\,|a_3-a_2|x_2 & & +\,|a_3-a_4|x_4 & = 1\\"
            r"|a_4-a_1|x_1 & +\,|a_4-a_2|x_2 & +\,|a_4-a_3|x_3 & & = 1"
            r"\end{array}\]".replace("&", "&amp;"),
        ),
    ),
    # --- text that is not part of the problem
    "ahsme-1998-p16": (drop("###px"),),
    "aime1-2005-p15": (  # the wiki's own paraphrase, then the official wording
        drop_selector("ul"),
        drop("Triangle has The incircle of the triangle evenly trisects"),
        replace("Original wording of the problem: In ", "In "),
    ),
    "aime1-2015-p15": (drop("--Credit to"),),
    "aime2-2026-p03": (drop("~Diagram by"),),
    "amc10a-2014-p18": (drop("~Diagram by"),),
    "amc10a-2025-p23": (drop("~ Diagram provided by"),),
    "amc12a-2025-p16": (drop("~ Diagram provided by"),),
    "amc12b-2012-p21": (drop("(diagram by"),),
    "imo-1962-p02": (drop("(Note that the problem as written in the official PDF"),),
    "cmo-1980-p05": (
        replace("<br/>\n<br/>\nTypesetter's Note: I believe that proof of existence or non-existence suffices.", ""),
    ),
    "aime-1994-p02": (drop("Note: The diagram was not given"),),
    "amc10b-2024-p11": (drop("Note: On certain tests that took place in China"),),
    "amc12b-2024-p07": (drop("Note: On certain tests that took place in China"),),
    # --- typos and choice labels
    "ahsme-1953-p26": (choices(r"\textbf{(4)}", r"\textbf{(4)}", r"\textbf{(E)}"),),
    "amc12a-2009-p14": (choices(r"\textbf{A} -", r"\textbf{A} -", r"\textbf{(A)} -"),),
    "amc10b-2020-p18": (choices(r"\textbf{(a) }", r"\textbf{(a) }", r"\textbf{(A) }"),),
    "amc12b-2020-p16": (choices(r"\textbf{(a) }", r"\textbf{(a) }", r"\textbf{(A) }"),),
    "ahsme-1970-p02": (replace("to the are of the square", "to the area of the square"),),
    "ahsme-1981-p04": (replace("the the larger of two numbers", "then the larger of two numbers"),),
    "aime-1985-p04": (replace("if the the area", "if the area"),),
    "amc8-2013-p16": (replace("and the the ratio", "and the ratio"),),
    "amc12a-2021-p21": (replace("is the is the distance", "is the distance"),),
    "ahsme-1955-p48": (image("$BH and HE$", r"\(BH\) and \(HE\)"),),  # words in math mode
    "ahsme-1995-p01": (image("$87,83, and 88$", r"\(87\), \(83\), and \(88\)"),),
    "ahsme-1969-p17": (choices(r"\text{(A) } log(3)", "log(", r"\log("),),
    "imo-1997-p01": (image("max\\left", r"\(f(m,n) \le \dfrac{1}{2} \max\left\{ m,n \right\}\)"),),
    "ahsme-1950-p39": (replace("Of these statments", "Of these statements"),),
    "ahsme-1967-p15": (replace("triange", "triangle"),),
    "ahsme-1971-p10": (replace("blue eyed of brown eyed", "blue eyed or brown eyed"),),
    "ahsme-1980-p09": (replace("finishes a a point", "finishes at a point"),),
    "ahsme-1998-p10": (replace("perimter", "perimeter"),),
    "aime-1992-p12": (replace("removed form the original", "removed from the original"),),
    "aime-1995-p11": (replace("parallelpiped", "parallelepiped"),),
    "amc10b-2008-p04": (replace("possiblle", "possible"),),
    "amc12b-2008-p03": (replace("possiblle", "possible"),),
    "amc10b-2008-p11": (replace("satifying", "satisfying"),),
    "amc10b-2023-p20": (replace("creating a close curve", "creating a closed curve"),),
    "amc8-2025-p05": (replace("(labled ", "(labeled "),),
    "cmo-2019-p01": (replace("from an previously", "from a previously"),),
    "cmo-2024-p04": (replace("gaurantee", "guarantee"),),
    "imo-1960-p06": (replace("diamter", "diameter"),),
    "imo-1964-p03": (replace("contructed", "constructed"),),
    "imo-1973-p04": (replace("shouid", "should"),),
    "imo-1977-p03": (replace("there exist a number", "there exists a number"),),
    "imo-1988-p05": (replace("dnote", "denote"),),
    "imo-2016-p06": (replace("claps,each", "claps, each"),),
    "imo-2017-p06": (replace("there exist a positive integer", "there exists a positive integer"),),
    "usamo-1988-p01": (replace("divisble", "divisible"),),
    "imo-2000-p06": (replace("whose vertices line on", "whose vertices lie on"),),
    "aime-1989-p14": (replace("/>,\n</p>", "/>.\n</p>"),),
    # --- arcs missing from the formula picture, footnotes and stray punctuation
    "amc10b-2019-p20": _ARCS_2019,
    "amc12b-2019-p15": _ARCS_2019,
    "amc8-2017-p25": (
        image(r"$\overarc{TR}$", r"\(\overparen{TR}\)"),
        image(r"$\overarc{SR}$", r"\(\overparen{SR}\)"),
    ),
    "ahsme-1978-p12": (
        replace("are distinct.*", "are distinct."),
        drop("The specification that points"),
        drop("If , then and"),
        drop_selector("ul:not(:has(li))"),
    ),
    "ahsme-1958-p45": (image("&#036;17.82", "$17.82"),),  # the picture lacks the dollar sign
    "imo-2007-p01": (
        replace("/>.\n</p><p>(a)", "/>\n</p><p>(a)"),
        image(r"$x_1\le x_2\le x_n$", r"\(x_1\le x_2\le \cdots\le x_n\)"),
    ),
    "aime-1994-p05": (drop("."),),
    "ahsme-1950-p38": (choices("satisified", "satisified", "satisfied"),),
    "ahsme-1951-p43": (
        image(
            "ther product is largest",
            r"\(\textbf{(C)}\ \text{If the sum of two positive quantities is given, their product is largest when they "
            r"are equal.}\)",
        ),
    ),
    "ahsme-1999-p02": (image("equianguilar", r"\(\mathrm{(C)\ All\ equilateral\ triangles\ are\ equiangular.}\)"),),
    # --- spaces missing between words and formulas
    "ahsme-1959-p16": (replace("The expression<img", "The expression <img"),),
    "ahsme-1969-p32": (replace("/>If <img", "/> If <img"),),
    "ahsme-1992-p22": (replace("-axis,<img", "-axis, <img"),),
    "ahsme-1993-p09": (
        replace("of the worlds wealth", "of the world's wealth"),
        replace("equally,and", "equally, and"),
    ),
    "aime2-2004-p12": (replace("/>and <img", "/> and <img"),),
    "amc10a-2004-p12": (replace("two,or three", "two, or three"),),
    "amc10a-2024-p14": (
        replace("lies on line<img", "lies on line <img"),
        replace("and lin <img", "and line <img"),
        image(r"$\ l$", r"\(\ell\)"),
    ),
    "amc10a-2025-p02": (replace("almonds.They", "almonds. They"),),
    "amc12a-2025-p02": (replace("almonds.They", "almonds. They"),),
    "amc10a-2025-p09": (replace("/>more than <img", "/> more than <img"),),
    "amc12a-2023-p24": (replace("/>.What is", "/>. What is"),),
    "amc12b-2003-p07": (replace("nickels,dimes", "nickels, dimes"),),
    "amc8-2009-p24": (replace("/>and <img", "/> and <img"), replace("/>,what digit", "/>, what digit")),
    "cmo-2015-p03": (replace("side.The turtle", "side. The turtle"),),
    "cmo-2020-p04": (
        replace(
            "i</span>s the set of perfect integer power. ( <span",
            "</span> is the set of perfect integer powers. (<span",
        ),
        replace(".</span> )We arrange", "</span>). We arrange"),
        replace("infinite many", "infinitely many"),
    ),
    "amc8-2012-p05": (replace("What is , <img", "What is <img"), replace("/> in centimeters?", "/>, in centimeters?")),
    "amc10b-2014-p24": (replace("/>.\n</p>", "/>\n</p>"),),
    "imo-2017-p02": (replace("real numbers , determine", "real numbers, determine"),),
    "imo-1989-p06": (replace("of the set<img", "of the set <img"),),
    "imo-2018-p05": (replace("there is an integer<img", "there is an integer <img"),),
    "usamo-1995-p03": (replace("/>and ", "/> and "),),
    "usamo-2015-p06": (replace("/>numbers. Show", "/> numbers. Show"),),
    **{f"pascal-2020-p{number:02d}": (lettered_choices,) for number in range(1, 26)},
}

# The line would break between a formula picture and the punctuation next to it ("P(16)" / ".").
_SPLIT_PUNCTUATION = """
    ahsme-1961-p36 ahsme-1967-p32 ahsme-1970-p09 ahsme-1975-p14 ahsme-1988-p02 ahsme-1991-p30 ahsme-1995-p29
    ahsme-1997-p30 aime-1987-p01 aime-1991-p11 aime1-2001-p07 aime1-2009-p05 aime1-2010-p06 aime1-2011-p08
    aime1-2016-p05 aime1-2018-p14 aime1-2018-p15 aime2-2007-p13 aime2-2014-p02 aime2-2015-p08 aime2-2018-p02
    aime2-2018-p15 aime2-2026-p03 ajhsme-1995-p24 amc10-2001-p06 amc10a-2008-p13 amc10a-2010-p19 amc10a-2011-p05
    amc10a-2011-p11 amc10a-2014-p24 amc10a-2017-p02 amc10a-2020-p02 amc10b-2006-p18 amc10b-2010-p09 amc10b-2012-p21
    amc10b-2020-p19 amc10b-2024-p14 amc12-2001-p02 amc12-2001-p20 amc12a-2008-p10 amc12a-2009-p19 amc12a-2009-p24
    amc12a-2010-p17 amc12a-2011-p04 amc12a-2017-p09 amc12a-2019-p15 amc12b-2006-p04 amc12b-2010-p05 amc12b-2012-p19
    amc12b-2014-p15 amc12b-2017-p09 amc12b-2024-p09 amc8-2007-p11 cmo-2017-p01 imo-1990-p05 imo-1994-p01
    imo-2000-p05 imo-2005-p02 imo-2015-p01 imo-2019-p02 imo-2022-p02 usajmo-2010-p03 usajmo-2023-p06 usamo-1978-p01
    usamo-1997-p05 usamo-2010-p01 aime1-2016-p08 amc10b-2003-p06 amc10b-2003-p11 amc10b-2020-p15 amc12b-2003-p05
    ahsme-1979-p25 amc12a-2016-p09
"""
for _pid in _SPLIT_PUNCTUATION.split():
    CORRECTIONS[_pid] = (*CORRECTIONS.get(_pid, ()), keep_together)


def correct(problem_id: str, html: str) -> str:
    """``html`` (a normalized statement) with the corrections for ``problem_id`` applied."""
    for step in CORRECTIONS.get(problem_id, ()):
        html = step(html)
    return html
