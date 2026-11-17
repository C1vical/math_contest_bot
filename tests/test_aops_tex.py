from mathbot.render.aops_tex import latex_images_to_tex
from mathbot.render.template import needs_mathjax

L = "https://latex.artofproblemsolving.com/a/b/c/x.png"


def img(alt, cls="latex", src=L):
    return f'<img src="{src}" class="{cls}" alt="{alt}">'


def test_inline_and_display_formulas_become_mathjax_input():
    html = f"<p>Solve {img('$x^2 = 49$')} for x.</p><p>{img('$$a+b$$', 'latexcenter')}</p>"
    out, converted = latex_images_to_tex(html)
    assert converted == 2
    assert '<span class="aops-tex">\\(x^2 = 49\\)</span>' in out
    assert '<div class="aops-tex-display">\\[a+b\\]</div>' in out
    assert "latex.artofproblemsolving.com" not in out
    assert needs_mathjax(out)


def test_centered_single_dollar_is_display_and_bracket_forms_are_kept():
    out, _ = latex_images_to_tex(img("$\\frac{1}{2}$", "latexcenter") + img("\\[ y \\]") + img("\\( z \\)"))
    assert "\\[\\frac{1}{2}\\]" in out and "\\[ y \\]" in out and "\\( z \\)" in out


def test_html_special_characters_in_tex_are_escaped_safely():
    out, _ = latex_images_to_tex(img("$a &lt; b$"))
    assert "\\(a &lt; b\\)" in out and "<b" not in out.replace("<span", "")


def test_asymptote_and_non_tex_images_are_left_for_the_asset_cache():
    asy = img("[asy] draw((0,0)--(1,1)); [/asy]", "latexcenter")
    other = img("A diagram", "", "https://artofproblemsolving.com/wiki/images/a.png")
    plain = img("$x$", "latex", "https://example.org/x.png")  # not the AoPS LaTeX host
    out, converted = latex_images_to_tex(asy + other + plain)
    assert converted == 0 and out == asy + other + plain


def test_dollar_signs_in_ordinary_text_are_not_treated_as_math():
    out, converted = latex_images_to_tex("<p>It costs $5 and $6.</p>")
    assert converted == 0 and not needs_mathjax(out)


def test_tabular_becomes_array_which_mathjax_supports():
    out, _ = latex_images_to_tex(img(r"$\begin{tabular}{cc}1&2\end{tabular}$"))
    assert r"\(\begin{array}{cc}1&amp;2\end{array}\)" in out and "tabular" not in out


def test_missing_latex_commands_are_defined_for_mathjax():
    import json

    from mathbot.render.template import _MACROS_JS

    macros = json.loads(_MACROS_JS)
    assert macros["emph"] == [r"\textit{#1}", 1]
    assert macros["hdots"] == r"\ldots" and macros["textdollar"] == r"\$"
    assert macros["vspace"] == ["", 1]  # layout-only commands are no-ops
    assert all("\x01" not in json.dumps(v) for v in macros.values())
