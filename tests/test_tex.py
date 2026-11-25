r"""TeX that AoPS pages show as plain text: repaired on normalization, reported when it is not.

The statements are the stored ones, shortened.
"""

from __future__ import annotations

import sqlite3

import pytest

from mathbot.config import RenderSettings
from mathbot.contests import AOPS_COMMUNITY, AOPS_WIKI, CEMC
from mathbot.htmlnorm import normalize_fragment
from mathbot.ingest import Ingestor
from mathbot.legacy import import_legacy
from mathbot.models import EditionRef, EditionResult, ParsedProblem
from mathbot.render.aops_tex import latex_images_to_tex
from mathbot.render.pipeline import RenderPipeline
from mathbot.render.template import build_document, needs_mathjax
from mathbot.tex import has_environment, unrendered_tex
from tests.conftest import add_problem, render_in_chromium

BASE = "https://artofproblemsolving.com/wiki/index.php/Some_Page"
L = "https://latex.artofproblemsolving.com/a/b/c/x.png"

ENVIRONMENT = (  # 2025 AIME I 8
    f'<p>Let <img alt="$k$" class="latex" src="{L}"/> be a real number such that the system\n'
    "\\begin{align*}\n&amp;|25 + 20i - z| = 5 \\\\\n&amp;|z - 4 - k| = |z - 3i - k|\n\\end{align*}\n"
    "has exactly one complex solution.</p>"
)
COMPILE_ERROR = (  # 1958 AHSME 30
    '<p>If <span style="color:red">$xy \\equal{} b$ (Error compiling LaTeX. Unknown error_msg)</span> and '
    '<span style="color:red">$\\frac{1}{x^2} \\plus{} \\frac{1}{y^2} \\equal{} a$ '
    "(Error compiling LaTeX. Unknown error_msg)</span>, then:</p>"
)
COMPILE_ERROR_LINES = (  # 1958 AHSME 29
    '<p><span style="color:red">$\\textbf{(A)}\\ x \\plus{} z \\equal{} a \\plus{} b\\qquad \\\\\n'
    "\\textbf{(B)}\\ x \\plus{} y \\equal{} m$ (Error compiling LaTeX. Unknown error_msg)</span></p>"
)
BBCODE = (  # 2008 AMC 10B 14, 2001 AMC 8 23
    "<p>Triangle [katex]OAB[/katex] has [katex]\\angle ABO=90^\\circ[/katex].</p>"
    "<p>[mathjax]\\text{(A)}\\ 1 \\qquad \\text{(B)}\\ 2[/mathjax]</p>"
)
DOLLARS = (  # 2010 AMC 10B 8 and 1950 AHSME 23
    f'<p>Tickets cost \\$<img alt="$48$" class="latex" src="{L}"/> in total; he pays \\$325 a year.</p>'
    f'<p><img alt="$\\textbf{{(A)}} \\ \\$64.82$" class="latex" src="{L}"/></p>'
)
GROUPS = {
    "environment": ENVIRONMENT,
    "compile-error": COMPILE_ERROR,
    "compile-error-lines": COMPILE_ERROR_LINES,
    "bbcode": BBCODE,
    "dollars": DOLLARS,
}


def aops(html: str) -> str:
    return normalize_fragment(html, BASE, aops=True).html


# --------------------------------------------------------------------------- repair


def test_bare_environments_are_wrapped_without_invalid_nesting():
    html = aops(ENVIRONMENT)
    assert '<span class="aops-tex">\\begin{align*}\n&amp;|25 + 20i' in html and "\\end{align*}</span>\nhas" in html
    assert "\\[" not in html and "\\(" not in html  # align inside display math is a TeX error
    assert html.count("latex.artofproblemsolving.com") == 1  # the formula images are untouched


def test_compile_errors_become_math_without_the_red_message():
    html = aops(COMPILE_ERROR)
    assert html == "<p>If \\(xy = b\\) and \\(\\frac{1}{x^2} + \\frac{1}{y^2} = a\\), then:</p>"


def test_line_breaks_between_answer_choices_become_real_ones():
    """MathJax ignores a top-level ``\\\\`` in inline math."""
    assert aops(COMPILE_ERROR_LINES) == (
        "<p>\\(\\textbf{(A)}\\ x + z = a + b\\qquad\\)<br/>\\(\\textbf{(B)}\\ x + y = m\\)</p>"
    )


def test_line_breaks_inside_a_matrix_stay_in_the_formula():
    error = " (Error compiling LaTeX. ! Missing $ inserted (line 2).)"
    matrix = "\\begin{pmatrix} a \\\\ b \\end{pmatrix}"
    html = aops(f'<p>Let <span style="color:red">$M \\equal{{}} {matrix}${error}</span> be given.</p>')
    assert html == f"<p>Let \\(M = {matrix}\\) be given.</p>"


@pytest.mark.parametrize(
    "source, expected",
    [
        ("$$a \\plus{} b$$", "\\[a + b\\]"),
        ("\\[a\\]", "\\[a\\]"),
        ("\\begin{align*}a&amp;=b\\end{align*}", '<span class="aops-tex">\\begin{align*}a&amp;=b\\end{align*}</span>'),
        ("$a\\equality b$", "\\(a\\equality b\\)"),  # not the old \equal operator
    ],
)
def test_compile_errors_of_other_shapes(source, expected):
    html = aops(f'<p>Now <span style="color:red">{source} (Error compiling LaTeX. Unknown error_msg)</span></p>')
    assert html == f"<p>Now {expected}</p>"


def test_a_compile_error_that_is_not_tex_is_left_for_validate():
    html = '<p>See <span style="color:red">[asy] draw(); [/asy] (Error compiling LaTeX. Unknown error_msg)</span></p>'
    assert aops(html) == html and unrendered_tex(html) == ["LaTeX compile error"]


def test_bbcode_math_tags_become_inline_math():
    assert aops(BBCODE) == (
        "<p>Triangle \\(OAB\\) has \\(\\angle ABO=90^\\circ\\).</p><p>\\(\\text{(A)}\\ 1 \\qquad \\text{(B)}\\ 2\\)</p>"
    )


def test_escaped_dollars_are_fixed_in_prose_only():
    html = aops(DOLLARS)
    assert 'cost $<img alt="$48$"' in html and "pays $325 a year" in html
    assert 'alt="$\\textbf{(A)} \\ \\$64.82$"' in html  # alt text is TeX source


@pytest.mark.parametrize(
    "fragment",
    [
        "<p>One costs \\(\\$5\\) and \\[\\text{\\$6}\\] buys two.</p>",
        '<p>Sum <span class="aops-tex">\\begin{align*}x &amp;= \\$5\\end{align*}</span> dollars.</p>',
        '<p>Code: [asy] label("\\$5", (0,0)); [/asy]</p>',
        '<p>Code: [asy] draw((0,0));<br/>label("\\$5", (0,0)); [/asy]</p>',  # cut into two text nodes
    ],
)
def test_escaped_dollars_in_math_and_asymptote_code_are_untouched(fragment):
    assert aops(fragment) == fragment


@pytest.mark.parametrize(
    "fragment",
    [
        "<p>It costs $5 and $6.</p>",
        "<p>It costs $5 and $x^2$ more.</p>",
        f'<p>Paid $<img alt="$x$" class="latex" src="{L}" style="vertical-align: -1px"/> and $5.</p>',
        "<p>[Note: see [x2] and [asy].]</p>",
        "<p>An unfinished \\begin{align*} and [katex] tag.</p>",
        "<p>Split [katex]a<b>b</b>[/katex] tag.</p>",  # markup inside: left for validate
        "<!-- \\$5 --><p>Find the value.</p>",
    ],
)
def test_other_text_is_unchanged(fragment):
    assert aops(fragment) == normalize_fragment(fragment, BASE).html


@pytest.mark.parametrize("html", GROUPS.values(), ids=GROUPS.keys())
def test_normalizing_twice_changes_nothing_more(html):
    once = normalize_fragment(html, BASE, aops=True)
    twice = normalize_fragment(once.html, BASE, aops=True)
    assert once == twice and once.html != normalize_fragment(html, BASE).html


@pytest.mark.parametrize("html", GROUPS.values(), ids=GROUPS.keys())
def test_other_sources_are_never_repaired(html):
    assert normalize_fragment(html, BASE) == normalize_fragment(html, BASE, aops=False)
    assert normalize_fragment(html, BASE).html == html


# --------------------------------------------------------------------------- where it applies


class OneProblemAdapter:
    def __init__(self, provider: str, html: str) -> None:
        self.provider, self.html = provider, html

    def discover(self, series):
        return [EditionRef(series.key, 2020, "2020 paper", "https://example.org/2020", "2020", None)]

    def fetch_edition(self, series, edition, *, known=None, only_numbers=None):
        result = EditionResult(edition=edition, source_hash="h")
        result.problems.append(ParsedProblem(1, self.html, "https://example.org/2020/1", raw_payload=self.html))
        return result


@pytest.mark.parametrize(
    "provider, key, repaired", [(AOPS_WIKI, "AMC10A", True), (AOPS_COMMUNITY, "CMO", True), (CEMC, "EUCLID", False)]
)
def test_live_ingestion_repairs_aops_only(repo, registry, provider, key, repaired):
    Ingestor(repo, {provider: OneProblemAdapter(provider, BBCODE)}).run([registry.get(key)])
    problem = repo.get_problem(key, 2020, 1)
    assert problem.statement_html == (aops(BBCODE) if repaired else BBCODE)
    raw = repo.conn.execute("SELECT raw_payload FROM source_records WHERE problem_id = ?", (problem.id,)).fetchone()[0]
    assert raw == BBCODE


def test_legacy_import_repairs_aops_only_and_keeps_the_raw_payload(repo, registry, tmp_path):
    rows = [
        ("a", 2008, "AMC10B", 14, f"<h2>2008 AMC 10B Problem 14</h2>{BBCODE}", "0"),
        ("b", 2025, "CMO", 2, f"<h2>2025 CMO Problem 2</h2>{DOLLARS}", "0"),
        ("c", 2019, "Euclid", 2, f"<h2>Problem 2</h2>{DOLLARS}", "0"),
    ]
    with sqlite3.connect(tmp_path / "old.db") as conn:
        conn.execute("CREATE TABLE math_problems (id, year, contest, question_number, question_statement, answer)")
        conn.executemany("INSERT INTO math_problems VALUES (?, ?, ?, ?, ?, ?)", rows)
    import_legacy(repo, registry, tmp_path / "old.db")
    assert repo.get_problem("AMC10B", 2008, 14).statement_html == aops(BBCODE)
    assert "pays $325" in repo.get_problem("CMO", 2025, 2).statement_html
    assert repo.get_problem("EUCLID", 2019, 2).statement_html == DOLLARS
    raw = [r[0] for r in repo.conn.execute("SELECT raw_payload FROM source_records ORDER BY source_identifier")]
    assert raw == [row[4] for row in rows]
    assert import_legacy(repo, registry, tmp_path / "old.db").outcomes["unchanged"] == 3


# --------------------------------------------------------------------------- detection


def test_unrendered_tex_is_reported_until_it_is_repaired():
    found = {name: unrendered_tex(html) for name, html in GROUPS.items()}
    assert found["environment"][0] == "bare TeX environment"
    assert found["compile-error"] == found["compile-error-lines"] == ["LaTeX compile error", "bare TeX command"]
    assert found["bbcode"] == ["BBCode math tag", "bare TeX command"]
    assert found["dollars"] == ["escaped dollar sign"]
    assert unrendered_tex("<p>Compute \\frac{1}{2}.</p>") == ["bare TeX command"]
    assert all(unrendered_tex(aops(html)) == [] for html in GROUPS.values())


@pytest.mark.parametrize(
    "html",
    [
        "<p>It costs $5 and $6.</p>",
        "<p>If \\(x^2 = \\$5\\) and \\[\\begin{array}{c} a \\\\[-2mm] b \\end{array}\\] then.</p>",
        f'<p><img alt="$\\text{{[katex]}} \\$5$" class="latex" src="{L}"/> and [see figure]</p>',
        f'<p><img alt="\\begin{{align*}}a\\end{{align*}}" class="latexcenter" src="{L}"/></p>',
        "<p>A lone backslash A \\ B, a set {1, 2} and x_1.</p>",
        "<pre>\\begin{verbatim}</pre>",
    ],
)
def test_correct_statements_are_not_reported(html):
    assert unrendered_tex(html) == []


def test_mathjax_loads_for_environments_in_visible_text():
    assert needs_mathjax(aops(ENVIRONMENT)) and needs_mathjax("<p>\\begin{eqnarray*}a\\end{eqnarray*}</p>")
    in_alt = f'<p>Solve <img alt="\\begin{{align*}}a&amp;=b\\end{{align*}}" class="latexcenter" src="{L}"/>.</p>'
    assert not has_environment(in_alt) and not needs_mathjax(in_alt)  # its PNG is shown
    fallback, converted = latex_images_to_tex(in_alt)  # its PNG is not available
    assert converted == 1 and needs_mathjax(fallback)
    assert "mathjax" in build_document("T", fallback).lower()


@pytest.mark.browser
def test_real_chromium_typesets_a_bare_align(repo, tmp_path, chromium_assets_dir):
    """The align is drawn as a tall display block, not as one line of source text."""
    align = (
        "\\begin{align*}" + "\\\\".join(f"x_{{{n}}} &amp;= \\frac{{{n}}}{{2}}" for n in range(1, 9)) + "\\end{align*}"
    )
    bare = add_problem(repo, 1, f"<p>Solve {align} for x.</p>")  # not repaired: needs_mathjax alone must be enough
    repaired = add_problem(repo, 2, aops(f"<p>Solve {align} for x.</p>"))
    plain = add_problem(repo, 3, "<p>Solve it for x.</p>")
    broken = add_problem(repo, 4, "<p>Solve \\begin{align*} \\notarealmacro{1} \\end{align*} for x.</p>")
    settings = RenderSettings(concurrency=1, retries=0)
    pipeline = RenderPipeline(repo, settings, tmp_path / "renders")
    report = render_in_chromium(pipeline, settings, chromium_assets_dir, [bare, repaired, plain, broken])
    assert report.rendered == 3 and report.failed == 1, report.errors
    assert report.errors[0][0] == broken.id and "TeX error" in report.errors[0][1]
    height = {p.id: repo.current_render(p.id)["height"] for p in (bare, repaired, plain)}
    assert height[bare.id] == height[repaired.id] > height[plain.id] + 600  # eight rows of fractions
