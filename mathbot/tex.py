"""TeX in statement markup: repairing what AoPS left unrendered, and finding what is left.

AoPS normally turns TeX into images. Some of its pages instead show the source as
plain text, and the statement is stored that way. :func:`repair_aops_tex` turns that
text into something the renderer typesets:

    \\begin{align*}...\\end{align*}             -> <span class="aops-tex">...</span>
    $...$ (Error compiling LaTeX. ...)        -> \\( ... \\), without the red error span
    [katex]...[/katex], [mathjax]...          -> \\( ... \\)
    \\$5                                       -> $5 (prose only)

A plain ``$`` in prose is never a math delimiter: it is money there ("$5 and $6").
:func:`unrendered_tex` reports TeX source that would still be drawn as text.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, NavigableString, Tag

TEX_CLASS = "aops-tex"
_TEX_CLASSES = {TEX_CLASS, "aops-tex-display"}
_VERBATIM_TAGS = {"script", "style", "pre", "code", "textarea"}  # MathJax skips these too

# AoPS TeX is real LaTeX; MathJax lacks text-mode tabular. array has the same syntax.
_TABULAR = re.compile(r"\\(begin|end)\{tabular\}")
# Operators of AoPS's old forum software, which MathJax (and today's AoPS) does not define.
_OLD_OPERATORS = re.compile(r"\\(plus|minus|equal)(?![a-zA-Z])(?:\s*\{\})?")
_OPERATOR = {"plus": "+", "minus": "-", "equal": "="}

_DELIMITED = r"(?<!\\)\\\(.*?(?<!\\)\\\)|(?<!\\)\\\[.*?(?<!\\)\\\]"
_DELIMITER = re.compile(r"(?<!\\)\\[()\[\]]")
_ENVIRONMENT = r"\\begin\{(?P<name>[a-zA-Z]+\*?)\}.*?\\end\{(?P=name)\}"
_BBCODE_NAMES = r"katex|mathjax|latex|tex|math"
_ESCAPED_DOLLAR = r"(?<!\\)\\\$"

# Read left to right, so whatever an earlier alternative covers is protected from the later
# ones: a "\$" inside math or Asymptote code is never touched.
_TOKEN = re.compile(
    rf"(?P<asy>\[asy\].*?\[/asy\])|(?P<math>{_DELIMITED})|(?P<env>{_ENVIRONMENT})"
    rf"|\[(?P<bb>{_BBCODE_NAMES})\](?P<bbtex>.*?)\[/(?P=bb)\]|(?P<dollar>{_ESCAPED_DOLLAR})",
    re.DOTALL | re.IGNORECASE,
)
_COMPILE_ERROR = re.compile(r"^\s*(?P<tex>.+?)\s*\(Error compiling LaTeX\b.*\)\s*$", re.DOTALL)

# A "\\" line break, and everything else that decides how deeply nested it is.
_LINE_TOKEN = re.compile(r"\\begin\b|\\end\b|(?P<newline>\\\\\*?(?:\[-?[\d.]+\s*[a-z]{2}\])?)|\\.|[{}]", re.DOTALL)

_TAG = re.compile(r"<[^>]*>")
_ENVIRONMENT_START = re.compile(r"\\begin\{[a-zA-Z]+\*?\}")
_UNRENDERED = (
    ("bare TeX environment", re.compile(r"\\(?:begin|end)\{")),
    ("LaTeX compile error", re.compile(r"Error compiling LaTeX", re.IGNORECASE)),
    ("BBCode math tag", re.compile(rf"\[/?(?:{_BBCODE_NAMES})\]", re.IGNORECASE)),
    ("escaped dollar sign", re.compile(_ESCAPED_DOLLAR)),
    ("bare TeX command", re.compile(r"\\[a-zA-Z]+")),
)


def mathjax_tex(source: str, display: bool = False) -> str | None:
    """MathJax input for TeX as AoPS writes it (``$x$``, ``\\[x\\]``, an environment), or None if it is not TeX."""
    source = source.strip()
    if not source or source.startswith("[asy]"):
        return None
    source = _TABULAR.sub(r"\\\1{array}", source)
    if source.startswith("$$") and source.endswith("$$") and len(source) > 4:
        return f"\\[{source[2:-2]}\\]"
    if source.startswith("$") and source.endswith("$") and len(source) > 2:
        inner = source[1:-1]
        return f"\\[{inner}\\]" if display else f"\\({inner}\\)"
    if source.startswith("\\[") and source.endswith("\\]"):
        return source
    if source.startswith("\\(") and source.endswith("\\)"):
        return source
    if re.match(r"\\begin\{[a-zA-Z*]+\}", source) and source.rstrip().endswith("}"):
        return source
    return None


# --------------------------------------------------------------------------- repair


def _split_lines(tex: str) -> list[str]:
    """``tex`` cut at its top-level ``\\\\`` line breaks, which MathJax ignores in inline math."""
    lines, start, depth = [], 0, 0
    for match in _LINE_TOKEN.finditer(tex):
        token = match.group()
        if token in ("{", "\\begin"):
            depth += 1
        elif token in ("}", "\\end"):
            depth -= 1
        elif match.group("newline") and depth == 0:
            lines.append(tex[start : match.start()])
            start = match.end()
    lines.append(tex[start:])
    return [line for line in lines if line.strip()]


def _repaired(source: str) -> list[str] | None:
    """MathJax input for one formula, one string per line of it, or None if it can't be converted safely."""
    tex = mathjax_tex(_OLD_OPERATORS.sub(lambda m: _OPERATOR[m.group(1)], source))
    if tex is None:
        return None
    if not tex.startswith("\\("):
        return [tex]
    inner = tex[2:-2]
    if _DELIMITER.search(inner):
        return None
    return [f"\\({line.strip()}\\)" for line in _split_lines(inner)] or None


def _nodes(soup: BeautifulSoup, lines: list[str]) -> list[Tag | NavigableString]:
    nodes: list[Tag | NavigableString] = []
    for index, line in enumerate(lines):
        if index:
            nodes.append(soup.new_tag("br"))
        if _ENVIRONMENT_START.match(line):
            span = soup.new_tag("span", attrs={"class": TEX_CLASS})
            span.append(NavigableString(line))
            nodes.append(span)
        else:
            nodes.append(NavigableString(line))
    return nodes


def _text_nodes(soup: BeautifulSoup) -> list[NavigableString]:
    """Visible text, except verbatim elements and TeX this module already marked up."""

    def excluded(node: NavigableString) -> bool:
        return any(p.name in _VERBATIM_TAGS or _TEX_CLASSES & set(p.get("class") or []) for p in node.parents)

    # Comments and the like are subclasses of NavigableString, hence the exact type.
    return [n for n in soup.find_all(string=True) if type(n) is NavigableString and not excluded(n)]


def _replace_compile_errors(soup: BeautifulSoup) -> None:
    for span in soup.find_all("span"):
        if span.find(lambda tag: tag.name != "br"):
            continue  # an enclosing element, or markup this module does not understand
        match = _COMPILE_ERROR.match(span.get_text())
        lines = _repaired(match["tex"]) if match else None
        if lines:
            span.replace_with(*_nodes(soup, lines))


def _replacement(soup: BeautifulSoup, match: re.Match[str]) -> list[Tag | NavigableString] | None:
    if match["dollar"]:
        return [NavigableString("$")]
    if match["env"]:
        lines = _repaired(match["env"])
    elif match["bb"]:
        tex = match["bbtex"].strip()
        lines = _repaired(tex) or _repaired(f"${tex}$")
    else:
        return None  # already typeset, or Asymptote source
    return _nodes(soup, lines) if lines else None


def repair_aops_tex(soup: BeautifulSoup) -> None:
    """Make the TeX an AoPS page shows as plain text typesettable, in place (see the module docstring).

    Repairing an already repaired statement changes nothing.
    """
    _replace_compile_errors(soup)
    soup.smooth()
    nodes = _text_nodes(soup)
    edits: dict[int, list[tuple[int, int, list[Tag | NavigableString]]]] = {}
    # Matching the text as a whole protects math and Asymptote code that other markup (a <br/>)
    # cuts into several text nodes. Only a match inside a single text node is ever replaced.
    starts, text = [], ""
    for node in nodes:
        starts.append(len(text))
        text += node
    index = 0
    for match in _TOKEN.finditer(text):
        while index + 1 < len(nodes) and starts[index + 1] <= match.start():
            index += 1
        offset = starts[index]
        if match.end() > offset + len(nodes[index]):
            continue
        replacement = _replacement(soup, match)
        if replacement is not None:
            edits.setdefault(index, []).append((match.start() - offset, match.end() - offset, replacement))

    for index, node_edits in edits.items():
        source, position = str(nodes[index]), 0
        pieces: list[Tag | NavigableString] = []
        for start, end, replacement in node_edits:
            pieces += [NavigableString(source[position:start]), *replacement]
            position = end
        pieces.append(NavigableString(source[position:]))
        nodes[index].replace_with(*pieces)
    if edits:
        soup.smooth()


# --------------------------------------------------------------------------- detection


def has_environment(html: str) -> bool:
    """Whether the visible text (not an ``alt``) of ``html`` holds a TeX environment, which MathJax typesets."""
    if not _ENVIRONMENT_START.search(html):
        return False
    return bool(_ENVIRONMENT_START.search(BeautifulSoup(html, "html.parser").get_text()))


def unrendered_tex(html: str) -> list[str]:
    """What TeX source the visible text of ``html`` would show to the reader; empty when there is none."""
    # Parsing is only worth it when something can be found at all, math or not.
    stripped = _TAG.sub("", html)
    if not any(pattern.search(stripped) for _, pattern in _UNRENDERED):
        return []
    visible = "".join(_text_nodes(BeautifulSoup(html, "html.parser")))
    prose = re.sub(_DELIMITED, " ", visible, flags=re.DOTALL)
    return [name for name, pattern in _UNRENDERED if pattern.search(prose)]
