import pytest

from mathbot.htmlnorm import InvalidContentError, extract_text, normalize_fragment, remote_images

BASE = "https://artofproblemsolving.com/wiki/index.php/Some_Page"


def test_urls_become_absolute_and_latex_alt_survives():
    content = normalize_fragment(
        '<p>Solve <img src="//latex.artofproblemsolving.com/a/b.png" class="latex" alt="$x^2=4$"> now.</p>'
        '<p><img src="/wiki/images/diagram.png" alt="A square"></p>',
        BASE,
    )
    assert 'src="https://latex.artofproblemsolving.com/a/b.png"' in content.html
    assert 'src="https://artofproblemsolving.com/wiki/images/diagram.png"' in content.html
    assert 'alt="$x^2=4$"' in content.html
    assert remote_images(content.html) == [
        "https://latex.artofproblemsolving.com/a/b.png",
        "https://artofproblemsolving.com/wiki/images/diagram.png",
    ]
    assert content.text == "Solve $x^2=4$ now. [image: A square]"


def test_unsafe_and_irrelevant_markup_is_removed():
    content = normalize_fragment(
        '<p onclick="steal()">Find <a href="javascript:alert(1)">the</a> <a href="/wiki/X">value</a> of x.</p>'
        "<script>alert(1)</script><style>p{color:red}</style><button>Show</button><!-- note -->"
        '<span class="mw-editsection">[edit]</span>',
        BASE,
    )
    assert content.html == "<p>Find the value of x.</p>"


def test_mathjax_tex_and_data_uris_are_left_alone():
    fragment = (
        '<p>If <span class="math inline">\\(x&gt;0\\)</span> then</p>'
        '<img alt="Lightbulb" src="data:image/png;base64,AAAA">'
    )
    content = normalize_fragment(fragment, "https://cemc.uwaterloo.ca/x.html")
    assert "\\(x&gt;0\\)" in content.html
    assert 'src="data:image/png;base64,AAAA"' in content.html
    assert remote_images(content.html) == []
    assert "\\(x>0\\)" in content.text


def test_hash_is_stable_and_content_sensitive():
    a = normalize_fragment("<p>What is 1 + 1?</p>", BASE)
    b = normalize_fragment("<p>What is 1 + 1?</p>", BASE)
    c = normalize_fragment("<p>What is 1 + 2?</p>", BASE)
    assert a.content_hash == b.content_hash != c.content_hash
    assert len(a.content_hash) == 64


@pytest.mark.parametrize(
    "fragment", ["", "   ", "<p></p>", "<div><span> </span></div>", "<script>x</script>", "<p>ab</p>"]
)
def test_empty_statements_are_rejected(fragment):
    with pytest.raises(InvalidContentError):
        normalize_fragment(fragment, BASE)


def test_image_only_statement_is_valid():
    content = normalize_fragment('<img src="https://example.org/p.png" alt="">', BASE)
    assert content.text == "[image]"


def test_extract_text_marks_asymptote_diagrams():
    assert (
        extract_text('<p>See <img class="latexcenter" alt="[asy] draw(); [/asy]" src="x.png"></p>') == "See [diagram]"
    )


def test_script_urls_hidden_by_whitespace_or_case_are_removed():
    """Browsers drop tabs and newlines inside a URL, so "java\tscript:" still runs."""
    html = normalize_fragment(
        '<p>Statement text here.<img src="java\tscript:alert(1)"><img src=" JaVaScRiPt:alert(2)">'
        '<iframe srcdoc="x"></iframe><svg><a href="java&#10;script:alert(3)">x</a></svg></p>',
        "https://example.org/",
    ).html
    assert "script:" not in html.lower() and "alert" not in html
