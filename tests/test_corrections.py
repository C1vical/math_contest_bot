r"""Hand-checked corrections, formula picture sizes, wide answer choices and CEMC page quirks."""

from __future__ import annotations

import json

import pytest
from bs4 import BeautifulSoup

from mathbot import corrections, formulas
from mathbot.config import RenderSettings
from mathbot.contests import CEMC
from mathbot.corrections import CORRECTIONS, FIGURES, correct
from mathbot.htmlnorm import normalize_fragment
from mathbot.ingest import Ingestor
from mathbot.models import EditionRef, EditionResult, ParsedProblem
from mathbot.render.pipeline import RenderPipeline
from mathbot.sources.cemc import parse_contest_page
from mathbot.tex import unrendered_tex
from mathbot.validate import validate
from tests.conftest import add_problem, render_in_chromium

BASE = "https://artofproblemsolving.com/wiki/index.php/Some_Page"
L = "https://latex.artofproblemsolving.com/a/b/c/x.png"
CHOICES = (
    r"$\textbf{(A)}\ x<0 \qquad \textbf{(B)}\ 10\% \qquad \textbf{(C)}\ \frac{1}{2}\\ "
    r"\textbf{(D)}\ 4\qquad\textbf{(E)}\ \text{none}$"
)


def latex(alt: str, extra: str = 'width="1052"') -> str:
    return f'<img class="latex" src="{L}" alt="{alt.replace("<", "&lt;")}" {extra}>'


def aops(html: str, pid: str | None = None) -> str:
    return normalize_fragment(html, BASE, aops=True, problem_id=pid).html


# --------------------------------------------------------------------------- corrections


def test_every_figure_file_is_used_and_exists():
    source = (FIGURES.parent / "corrections.py").read_text(encoding="utf-8")
    assert all(path.name in source for path in FIGURES.iterdir())
    assert all(isinstance(steps, tuple) and steps for steps in CORRECTIONS.values())


@pytest.mark.parametrize(
    "pid, before, expected",
    [
        ("amc8-2013-p16", "<p>It is 5, and the the ratio is 8.</p>", "<p>It is 5, and the ratio is 8.</p>"),
        ("amc10a-2014-p18", "<p>Find the area.</p><p>~Diagram by someone\n</p>", "<p>Find the area.</p>"),
        (
            "imo-2000-p06",
            "<p>Prove the images form a triangle whose vertices line on it.</p>",
            "<p>Prove the images form a triangle whose vertices lie on it.</p>",
        ),
    ],
)
def test_corrections_apply_once_and_only_to_their_problem(pid, before, expected):
    assert aops(before, pid) == expected == aops(expected, pid)
    assert aops(before, "imo-1999-p05") == aops(before) == before.strip()


def test_missing_choices_are_added_once():
    html = aops("<p>What was the resulting sum?</p>", "amc8-2003-p01")
    assert r"\textbf{(E)}\ 26" in html and html.count("(A)") == 1
    assert aops(html, "amc8-2003-p01") == html
    complete = r"<p>What was the resulting sum?</p><p>\(\textbf{(A)}\ 12\)</p>"
    assert aops(complete, "amc8-2003-p01") == complete  # the page was fixed at its source


def test_vandalised_statement_is_replaced_but_a_repaired_one_is_not():
    html = aops("<p>Isaac is using increasing angles. What happens?</p>", "ahsme-1950-p12")
    assert "sum of the exterior angles" in html and "Isaac" not in html and unrendered_tex(html) == []
    repaired = "<p>As the number of sides of a polygon increases, what happens?</p>"
    assert aops(repaired, "ahsme-1950-p12") == repaired


def test_missing_figures_are_embedded():
    post = 'See diagram.<br/><span style="color:red">Invalid URL</span><br/>Find it.'
    html = aops(post, "cmo-1974-p07")
    image = BeautifulSoup(html, "html.parser").img
    assert image["src"].startswith("data:image/png;base64,") and "Invalid URL" not in html
    assert aops(html, "cmo-1974-p07") == html
    svg = aops("<p>Twelve congruent disks are placed.</p>", "aime-1991-p11")
    assert "data:image/svg+xml;base64," in svg and aops(svg, "aime-1991-p11") == svg


def test_choice_labels_and_lists_are_corrected():
    alt = r"$\textbf{(a) } \frac16 \qquad \textbf{(B) } 2 \qquad \textbf{(C) } 3 \qquad "
    alt += r"\textbf{(D) } 4 \qquad \textbf{(E) } 5$"
    html = aops(f"<p>Find it.</p><p>{latex(alt, '')}</p>", "amc10b-2020-p18")
    assert r"\(\displaystyle \textbf{(A) } \frac16\)" in html and "<img" not in html
    listed = '<p>How many?</p><ol type="a">' + "<li>x</li>" * 5 + "</ol>"
    fixed = normalize_fragment(listed, BASE, problem_id="pascal-2020-p03").html
    assert '<ol class="mc">' in fixed and normalize_fragment(fixed, BASE, problem_id="pascal-2020-p03").html == fixed


def test_validate_reports_corrections_that_are_not_applied_yet(repo, tmp_path):
    add_problem(repo, 16, "<p>It is 5, and the the ratio is 8.</p>", key="AMC8", year=2013)
    found = [f.message for f in validate(repo, tmp_path, "x").findings if f.check == "pending-correction"]
    assert len(found) == 1 and found[0].startswith("amc8-2013-p16")


class OneProblem:
    provider = CEMC

    def discover(self, series):
        return [EditionRef(series.key, 2020, "2020 Pascal", "https://example.org/2020", "2020", None)]

    def fetch_edition(self, series, edition, *, known=None, only_numbers=None):
        html = '<p>How many squares?</p><ol type="a">' + "<li>x</li>" * 5 + "</ol>"
        problem = ParsedProblem(3, html, "https://example.org/2020/3", raw_payload=html)
        return EditionResult(edition=edition, problems=[problem], source_hash="h")


def test_live_ingestion_applies_corrections_and_keeps_the_raw_payload(repo, registry):
    Ingestor(repo, {CEMC: OneProblem()}).run([registry.get("PASCAL")])
    assert '<ol class="mc">' in repo.get_problem("PASCAL", 2020, 3).statement_html
    raw = repo.conn.execute("SELECT raw_payload FROM source_records").fetchone()[0]
    assert 'type="a"' in raw


def test_arcs_missing_from_a_formula_picture_are_typeset():
    first, second = latex(r"$\overarc{TR}$", ""), latex(r"$\overarc{SR}$", "")
    html = aops(f"<p>Arcs {first} and {second} are shown.</p>", "amc8-2017-p25")
    assert html == r"<p>Arcs \(\overparen{TR}\) and \(\overparen{SR}\) are shown.</p>"
    footnote = (
        "<p>Points are distinct.* Find it.</p><ul><li>The specification that points were.</li></ul><p>Choices</p>"
    )
    assert aops(footnote, "ahsme-1978-p12") == "<p>Points are distinct. Find it.</p><p>Choices</p>"


def test_punctuation_stays_on_the_line_of_its_formula():
    x = latex("$x$", 'style="vertical-align: -1px"').replace(">", "/>")
    html = aops(f"<p>Take ({x}) and {x}, then {x} alone.</p>", "aime1-2010-p06")
    assert html.count('<span style="white-space: nowrap">') == 2
    assert html.count('nowrap">(<img') == 1 and "/>)</span> and" in html and "/>,</span> then" in html
    assert aops(html, "aime1-2010-p06") == html
    display = latex(r"\[x\]", "").replace('class="latex"', 'class="latexcenter"')
    assert (
        "." not in BeautifulSoup(aops(f"<p>Show that</p><p>{display}.</p>", "usamo-1997-p05"), "html.parser").get_text()
    )


# --------------------------------------------------------------------------- formula pictures


def test_stale_formula_sizes_and_missing_baselines_are_corrected(monkeypatch):
    monkeypatch.setattr(formulas, "_sizes", lambda: {"x": [16, 14, -1]})
    small = aops(f'<p>Point <img alt="$Z$" class="latex" height="12" src="{L}" width="13"/> lies here.</p>')
    assert 'height="14"' in small and 'width="16"' in small and "vertical-align: -1px" in small
    assert aops(small) == small
    raised = f'<p>Point <img alt="$Z$" class="latex" src="{L}" style="vertical-align: 0px" width="16"/> lies here.</p>'
    assert "vertical-align: -1px" in aops(raised)
    other = L.replace("x.png", "y.png")
    bare = aops(f'<p>Point <img alt="$n$" class="latex" src="{other}"/> lies here.</p>')
    assert 'style="vertical-align: -1px"' in bare
    own = f'<p>Point <img alt="$q$" class="latex" src="{other}" style="vertical-align: -4px"/> lies here.</p>'
    assert aops(own) == own
    cemc = f'<p>Point <img alt="$Z$" class="latex" height="12" src="{L}" width="13"/> lies here.</p>'
    assert normalize_fragment(cemc, BASE).html == cemc  # AoPS only


def test_the_size_table_is_well_formed():
    table = json.loads((FIGURES.parent / "formula_sizes.json").read_text(encoding="utf-8"))
    assert len(table) > 400
    assert all(len(v) == 3 and v[0] > 0 and v[1] > 0 and -60 < v[2] < 10 for v in table.values())


def test_choices_wider_than_the_page_are_typeset_one_by_one():
    html = aops(f"<p>Find it.</p><p>{latex(CHOICES)}</p>")
    assert "<img" not in html
    assert r"\(\displaystyle \textbf{(A)}\ x&lt;0\)" in html and r"\(\displaystyle \textbf{(E)}\ \text{none}\)" in html
    assert html.count("\\(") == 5 and "<br/>" not in html and unrendered_tex(html) == []
    assert aops(html) == html
    narrow = aops(f"<p>Find it.</p><p>{latex(CHOICES, 'width=700')}</p>")
    assert "<img" in narrow  # fits the page: AoPS's own picture is kept


def test_choices_on_their_own_lines_stay_there():
    alt = r"$\text{(A) one} \\ \text{(B) two}\quad\\ \text{(C) three}\\ \text{(D) four}\\ \text{(E) five}$"
    html = aops(f"<p>Then we have: {latex(alt)}</p>")
    assert html.count("<br/>") == 5 and html.startswith("<p>Then we have: <br/>\\(")


@pytest.mark.parametrize(
    "alt", [r"$x^2+y^2$", r"$\textbf{(A)}\ 1 \qquad \textbf{(B)}\ 2$", r"$\begin{cases}a\\b\end{cases}$"]
)
def test_other_wide_formulas_keep_their_picture(alt):
    assert "<img" in aops(f"<p>See {latex(alt)}</p>")


def test_choices_get_their_own_paragraph():
    choices = latex(r"$\textbf{(A)}\ 1 \qquad \textbf{(B)}\ 2 \qquad \textbf{(E)}\ 3$", "")
    html = aops(f"<p>Then the sum is:\n{choices}\n</p>")
    assert html.startswith("<p>Then the sum is:\n</p><p><img") and aops(html) == html
    for other in (f"<p>Find it.</p><p>{choices}</p>", f"<p>Find it.<br/>{choices}</p>"):
        assert aops(other).count("<p>") == other.count("<p>")
    part = latex("$(A)$", "")
    assert aops(f"<p>The area {part} is 3.</p>").count("<p>") == 1


def test_space_between_a_formula_and_its_punctuation_is_removed():
    x = f'<img alt="$x$" class="latex" src="{L}" style="vertical-align: -1px"/>'
    html = aops(f"<p>With {x} , then {x}\u00a0?</p>")
    assert html == f"<p>With {x}, then {x}?</p>"
    ratio = f"<p>The ratio {x} : {x} . . . is {x} .5 here</p>"
    assert aops(ratio) == ratio


def test_empty_paragraphs_at_the_end_are_dropped():
    assert aops("<p>Find it.</p><p><br/>\n</p><p>\n</p>") == "<p>Find it.</p>"
    assert aops("<p>Find it.</p>\n<hr/>") == "<p>Find it.</p>"
    picture = f'<p>Find it.</p><p><img alt="" class="mw-file-element" src="{BASE}"/></p>'
    assert aops(picture) == picture
    assert normalize_fragment("<p>Find it.</p><p><br/></p>", BASE).html == "<p>Find it.</p><p><br/></p>"


# --------------------------------------------------------------------------- CEMC pages

PAGE = """<html><head><title>T</title><style>td {{ padding: 1em; border: 1px solid #808080; }}</style></head><body>
<h2>Part A: Each correct answer is worth 5.</h2><ol>
<li><p>The numbers are marked.</p><p><img src="data:image/png;base64,AAAA"></p></li>
What is the value of <span class="math inline">\\(S &lt; 2\\)</span>?
<ol class="horizontal"><li>1</li><li>2</li></ol>
<li><p>Second question.</p><table><tr><td>1</td><td style="width: 30px"></td></tr></table></li>
</ol></body></html>""".format()


def test_loose_text_after_a_diagram_belongs_to_its_question():
    (_, _, first), (_, _, second) = parse_contest_page(PAGE).problems
    text = " ".join(BeautifulSoup(first, "html.parser").get_text().split())
    assert "The numbers are marked. What is the value of \\(S < 2\\)? 1" in text.replace(" 12", " 1 2")
    assert "What is the value" not in second


def test_table_borders_of_the_page_stylesheet_are_kept():
    second = parse_contest_page(PAGE).problems[1][2]
    cells = BeautifulSoup(second, "html.parser").find_all("td")
    assert [c["style"] for c in cells] == ["border: 1px solid #808080", "border: 1px solid #808080; width: 30px"]
    plain = parse_contest_page(PAGE.replace("border: 1px solid #808080;", "")).problems[1][2]
    assert "border" not in plain


# --------------------------------------------------------------------------- real browser


@pytest.mark.browser
def test_real_chromium_draws_wide_choices_and_embedded_figures(repo, tmp_path, chromium_assets_dir):
    wide = add_problem(repo, 1, aops(f"<p>Find it.</p><p>{latex(CHOICES)}</p>"), key="AMC10A", year=2020)
    svg = add_problem(
        repo, 2, aops("<p>Twelve congruent disks are placed.</p>", "aime-1991-p11"), key="AMC10A", year=2020
    )
    png = add_problem(repo, 3, correct("cmo-2007-p01", "The dominoes are placed as shown."), key="AMC10A", year=2020)
    settings = RenderSettings(concurrency=1, retries=0)
    pipeline = RenderPipeline(repo, settings, tmp_path / "renders")
    report = render_in_chromium(pipeline, settings, chromium_assets_dir, [wide, svg, png])
    assert report.failed == 0, report.errors
    assert repo.current_render(svg.id)["height"] > 700 and repo.current_render(png.id)["height"] > 500
    assert corrections.figure("aime-1991-p11.svg", 300, 'a "b"').count('"') == 8  # the alt text is escaped
