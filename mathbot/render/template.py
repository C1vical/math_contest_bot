"""HTML document used to render one problem.

Bump :data:`TEMPLATE_VERSION` whenever the markup or CSS below changes in a
way that alters the picture: the renderer version is part of the render
manifest key, so a bump makes ``render`` regenerate every image and
``cleanup`` remove the old ones.
"""

from __future__ import annotations

import json
from html import escape

from mathbot.config import RenderSettings
from mathbot.render.vendor import asset_url

TEMPLATE_VERSION = "6"  # 6: AoPS statements show AoPS's own formula and diagram PNGs

# Fonts and MathJax come from the pinned, locally served files in render/vendor.py.
# MathJax uses SVG output, which needs no separate font files.
_FONT_FACES = "\n".join(
    f"@font-face {{ font-family: 'Computer Modern Serif'; src: url('{asset_url('fonts/' + name)}') format('woff');"
    f" font-weight: {weight}; font-style: {style}; }}"
    for name, weight, style in (
        ("cmunrm.woff", "normal", "normal"),
        ("cmunbx.woff", "bold", "normal"),
        ("cmunti.woff", "normal", "italic"),
        ("cmunbi.woff", "bold", "italic"),
    )
)
_MATHJAX_URL = asset_url("mathjax/tex-svg-full.js")

# LaTeX commands AoPS statements use that MathJax does not define. Each one only
# affects input that would otherwise be a TeX error (a failed render), so adding
# entries never changes an existing successful image and needs no TEMPLATE_VERSION bump.
# Layout-only commands become no-ops; they have no meaning in a single formula.
_MACROS = {
    "emph": [r"\textit{#1}", 1],
    "hdots": r"\ldots",
    "cent": r"\unicode{xA2}",
    "textdollar": r"\$",
    "mathdollar": r"\$",
    "O": r"\unicode{xD8}",
    "thickspace": r"\;",
    "textsc": [r"\style{font-variant-caps: small-caps}{\text{#1}}", 1],
    "textcircled": [r"\enclose{circle}{\text{#1}}", 1],
    "-": "",
    "vspace": ["", 1],
    "setlength": ["", 2],
    "usepackage": ["", 1],
    "centerline": [r"#1", 1],
    "begingroup": "",
    "endgroup": "",
}
_MACROS_JS = json.dumps(_MACROS)

_CSS = """
html, body { margin: 0; padding: 0; background: #ffffff; }
html { line-height: 1.45; font-family: "Computer Modern Serif", "Latin Modern Roman", Georgia, serif;
       font-size: 20px; color: #171717; }
body { margin: 0 auto; max-width: 1000px; padding: 32px 42px; }
h1 { text-align: center; font-size: 34px; margin-top: 1.4em; }
h2.problem-title { margin: 0 0 18px 0; padding: 0 0 8px 0; font-size: 25px; font-weight: bold;
                   line-height: 1.25; border-bottom: 1px solid #cccccc; }
h2 { font-size: 1.2em; margin-top: 1.2em; }
h3 { font-size: 1.1em; margin-top: 1.4em; }
h4, h5, h6 { margin-top: 1.4em; }
p { margin: 0 0 0.75em 0; }
ol, ul { padding-left: 1.7em; margin-top: 1em; }
li > ol, li > ul { margin-top: 0; }
ol[type="a"] > li { margin-bottom: 0.6em; }
ol.horizontal { width: 100%; counter-reset: list list-item; display: flex; justify-content: flex-start;
                align-items: baseline; flex-wrap: wrap; padding-left: 0; }
ol.horizontal li { list-style: none; width: 185px; padding: 2px 0; }
ol.horizontal li::before { content: " (" counter(list, upper-alpha) ") "; counter-increment: list; }
ol.mc { counter-reset: mc; padding-left: 0; }
ol.mc > li { list-style: none; margin: 4px 0; }
ol.mc > li::before { content: "(" counter(mc, upper-alpha) ") "; counter-increment: mc; vertical-align: middle; }
div.tabbing { margin: 0.4em 0 0.8em 1em; }
img { max-width: 100%; height: auto; }
img.latex { vertical-align: middle; }
.static { max-width: 100%; min-width: 0%; }
.center { text-align: center; }
.latexcenter, .mw-file-element, .asy-image { display: block; max-width: 100%; height: auto; margin: 14px auto; }
table { border-collapse: collapse; margin: 0.6em auto; }
td, th { padding: 4px 10px; }
.column-five { float: left; width: 20%; }
.row::after { content: ""; display: table; clear: both; }
mjx-container[display="true"] { margin: 0.6em 0; }
.aops-tex-display { margin: 0.6em 0; text-align: center; }
"""

# Resolves once MathJax, web fonts and every image are ready and layout has settled.
# Rejects (failing the render) rather than photographing a half-loaded page.
READY_SCRIPT = """
async (needsMathJax) => {
  if (needsMathJax) {
    if (!window.MathJax || !window.MathJax.startup || !window.MathJax.startup.promise) {
      throw new Error('MathJax did not load');
    }
    await window.MathJax.startup.promise;
    // A TeX error must fail the render, not ship a picture with an error box in it.
    const errors = Array.from(document.querySelectorAll('[data-mml-node="merror"]'));
    if (errors.length) {
      const message = errors[0].getAttribute('data-mjx-error') || errors[0].textContent || 'unknown';
      throw new Error('TeX error: ' + message.slice(0, 160));
    }
  }
  await document.fonts.ready;
  await Promise.all(Array.from(document.images).map((img) => {
    const label = (img.getAttribute('alt') || img.currentSrc || '').slice(0, 80);
    if (img.complete) {
      return img.naturalWidth > 0 ? null : Promise.reject(new Error('image failed to load: ' + label));
    }
    return new Promise((resolve, reject) => {
      img.addEventListener('load', resolve, { once: true });
      img.addEventListener('error', () => reject(new Error('image failed to load: ' + label)), { once: true });
    });
  }));
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  return true;
}
"""


def needs_mathjax(body_html: str) -> bool:
    """CEMC statements carry TeX for MathJax; AoPS statements use AoPS's PNGs, plus TeX
    for any formula whose PNG could not be downloaded."""
    return "\\(" in body_html or "\\[" in body_html


def renderer_version(settings: RenderSettings) -> str:
    """Identifies everything that changes the pixels: template, width and scale."""
    return f"{TEMPLATE_VERSION}-w{settings.viewport_width}-s{settings.device_scale_factor:g}"


def build_document(title: str, body_html: str) -> str:
    mathjax = ""
    if needs_mathjax(body_html):
        mathjax = (
            # tex-svg-full bundles every TeX extension. textmacros handles \textbf etc. inside
            # \text{}; noerrors / noundefined are removed so a TeX mistake becomes an merror
            # node, which READY_SCRIPT turns into a failed render instead of a bad picture.
            "<script>window.MathJax = {tex: {inlineMath: [['\\\\(', '\\\\)']], displayMath: [['\\\\[', '\\\\]']],"
            " packages: {'[+]': ['textmacros'], '[-]': ['noerrors', 'noundefined']},"
            f" macros: {_MACROS_JS}}},"
            " svg: {fontCache: 'global'}, options: {enableMenu: false}};</script>\n"
            f'<script src="{_MATHJAX_URL}"></script>'
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{escape(title)}</title>
<style>{_FONT_FACES}
{_CSS}</style>
{mathjax}
</head>
<body>
<h2 class="problem-title">{escape(title)}</h2>
{body_html}
</body>
</html>
"""
