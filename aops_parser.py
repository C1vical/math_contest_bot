import html
import re
import cloudscraper

def extract_problem_statement(raw_wikitext: str) -> str:
    """Extracts only the problem statement section from raw wikitext,

    ignoring solutions, video links, templates, and see-also sections.
    """
    # Match content from '==Problem==' up to the next '==' section header
    pattern = r"==\s*Problem\s*==\s*(.*?)(?=\n==|\Z)"
    match = re.search(pattern, raw_wikitext, re.DOTALL | re.IGNORECASE)

    if match:
        problem_text = match.group(1)
    else:
        # Fallback: if no explicit ==Problem== header exists, take everything before the first section header
        problem_text = re.split(
            r"\n==\s*Solution", raw_wikitext, flags=re.IGNORECASE
        )[0]

    # Remove any MediaWiki template tags like {{AMC10 box...}} or {{MAA Notice}}
    problem_text = re.sub(r"\{\{[^}]*\}\}", "", problem_text)

    return problem_text.strip()


def latexify(raw_content: str) -> str:
    """Converts raw AoPS wikitext, BBCodes, and HTML into clean LaTeX syntax."""
    text = raw_content

    # Extract raw source inside <textarea> if passing full HTML edit page
    textarea_match = re.search(
        r"<textarea[^>]*>(.*?)</textarea>", text, re.DOTALL | re.IGNORECASE
    )
    if textarea_match:
        text = textarea_match.group(1)

    # Decode HTML entities and dash variants
    text = html.unescape(text)
    text = text.replace("&ndash;", "--").replace("&mdash;", "---")
    text = re.sub(r"<!--[^>]*-->", "", text)
    text = re.sub(r"</?onlyinclude>", "", text, flags=re.IGNORECASE)

    # --- MATH TAG CONVERSIONS ---
    # Convert <imath> and <math> tags to inline LaTeX delimiters ($...$)
    text = re.sub(r"</?(imath|math)>", "$", text, flags=re.IGNORECASE)

    # Convert <cmath> tags to display LaTeX delimiters (\[...\])
    text = re.sub(r"<cmath>", lambda _: r"\[ ", text, flags=re.IGNORECASE)
    text = re.sub(r"</cmath>", lambda _: r" \]", text, flags=re.IGNORECASE)

    # Math symbols & character escapes
    text = text.replace("±", r"\pm").replace("∘", r"^\circ")
    text = text.replace(r"\implies", r"\Rightarrow")
    text = re.sub(r"([^\\])%", lambda m: m.group(1) + r"\%", text)

    # --- ASYMPTOTE DIAGRAMS ---
    asy_start = (
        "\n\n\\begin{minipage}[c]{\\linewidth}\n\\centering\n\\begin{asy}"
    )
    asy_end = "\n\\end{asy}\n\\end{minipage}\n\n"
    text = re.sub(r"<asy>", lambda _: asy_start, text, flags=re.IGNORECASE)
    text = re.sub(r"</asy>", lambda _: asy_end, text, flags=re.IGNORECASE)

    def clean_asy(match):
        lines = [line for line in match.group(0).splitlines() if line.strip()]
        return "\n".join(lines)

    text = re.sub(
        r"\\begin\{asy\}.*?\\end\{asy\}", clean_asy, text, flags=re.DOTALL
    )

    # --- LATEX SYNTAX FIXES ---
    # Remove redundant math wrappers ($ or \[) around align/eqnarray blocks
    text = re.sub(
        r"\$\s*\\begin\{(align|eqnarray)\*?\}",
        lambda m: f"\\begin{{{m.group(1)}}}",
        text,
    )
    text = re.sub(
        r"\\end\{(align|eqnarray)\*?\}\s*\$",
        lambda m: f"\\end{{{m.group(1)}}}",
        text,
    )
    text = re.sub(
        r"\\\[\s*\\begin\{(align|eqnarray)\*?\}",
        lambda m: f"\\begin{{{m.group(1)}}}",
        text,
    )
    text = re.sub(
        r"\\end\{(align|eqnarray)\*?\}\s*\\\]",
        lambda m: f"\\end{{{m.group(1)}}}",
        text,
    )

    # Trigonometric formatting (\sinA -> \sin A)
    text = re.sub(
        r"\\(sin|cos|tan)([A-Za-z])",
        lambda m: f"\\{m.group(1)} {m.group(2)}",
        text,
    )

    # --- BBCODE & HTML CONVERSIONS ---
    # Bold: '''text''', [b]text[/b], <b>text</b>
    text = re.sub(r"'''(.*?)'''", lambda m: f"\\textbf{{{m.group(1)}}}", text)
    text = re.sub(
        r"\[b\](.*?)\[/b\]",
        lambda m: f"\\textbf{{{m.group(1)}}}",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"<b>(.*?)</b>",
        lambda m: f"\\textbf{{{m.group(1)}}}",
        text,
        flags=re.IGNORECASE,
    )

    # Italics: ''text'', [i]text[/i], <i>text</i>
    text = re.sub(r"''(.*?)''", lambda m: f"\\textit{{{m.group(1)}}}", text)
    text = re.sub(
        r"\[i\](.*?)\[/i\]",
        lambda m: f"\\textit{{{m.group(1)}}}",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"<i>(.*?)</i>",
        lambda m: f"\\textit{{{m.group(1)}}}",
        text,
        flags=re.IGNORECASE,
    )

    # Underline: [u]text[/u], <u>text</u>
    text = re.sub(
        r"\[u\](.*?)\[/u\]",
        lambda m: f"\\underline{{{m.group(1)}}}",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"<u>(.*?)</u>",
        lambda m: f"\\underline{{{m.group(1)}}}",
        text,
        flags=re.IGNORECASE,
    )

    # Strikethrough: [s]text[/s], <s>text</s>, <del>text</del>
    text = re.sub(
        r"\[s\](.*?)\[/s\]",
        lambda m: f"\\sout{{{m.group(1)}}}",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"</?(s|del)>",
        lambda m: f"\\sout{{{m.group(1)}}}",
        text,
        flags=re.IGNORECASE,
    )

    # Color: [color=red]text[/color]
    text = re.sub(
        r"\[color=([^\]]+)\](.*?)\[/color\]",
        lambda m: f"\\textcolor{{{m.group(1)}}}{{{m.group(2)}}}",
        text,
        flags=re.IGNORECASE,
    )

    # Code: [code]text[/code]
    text = re.sub(
        r"\[code\](.*?)\[/code\]",
        lambda m: f"\\texttt{{{m.group(1)}}}",
        text,
        flags=re.IGNORECASE,
    )

    # Quotes: [quote]text[/quote]
    text = re.sub(
        r"\[quote\](.*?)\[/quote\]",
        lambda m: f"\\begin{{quote}}\n{m.group(1)}\n\\end{{quote}}",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # --- HTML / WIKI CLEANUP ---
    text = re.sub(r"</?center>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?\s*br\s*/?>", "", text, flags=re.IGNORECASE)
    text = text.replace("__TOC__", "")
    text = re.sub(
        r"<geogebra>(\w*)</geogebra>",
        lambda m: f"Geogebra: {m.group(1)}",
        text,
        flags=re.IGNORECASE,
    )

    # Links & Categories
    text = re.sub(r"\[\[Category:[^]]*\]\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[\[[^]|]+\|([^]]+)\]\]", lambda m: m.group(1), text)
    text = re.sub(r"\[\[([^]|]+)\]\]", lambda m: m.group(1), text)

    # Lists (BBCode [list], [*] and HTML <ol>, <ul>, <li>)
    text = re.sub(
        r"\[list\]", lambda _: " \\begin{itemize} ", text, flags=re.IGNORECASE
    )
    text = re.sub(
        r"\[/list\]", lambda _: " \\end{itemize} ", text, flags=re.IGNORECASE
    )
    text = re.sub(r"\[\*\]", lambda _: r"\item ", text)

    text = re.sub(
        r"<ol>", lambda _: " \\begin{enumerate} ", text, flags=re.IGNORECASE
    )
    text = re.sub(
        r"</ol>", lambda _: " \\end{enumerate} ", text, flags=re.IGNORECASE
    )
    text = re.sub(
        r"<ul>", lambda _: " \\begin{itemize} ", text, flags=re.IGNORECASE
    )
    text = re.sub(
        r"</ul>", lambda _: " \\end{itemize} ", text, flags=re.IGNORECASE
    )
    text = re.sub(r"<li>", lambda _: r"\item ", text, flags=re.IGNORECASE)
    text = re.sub(r"</li>", "", text, flags=re.IGNORECASE)

    return text.strip()

def fetch_aops_page(page_title: str, max_redirects: int = 5) -> str:
    """Fetches raw wikitext directly from the AoPS MediaWiki API,

    automatically following page redirects if encountered.
    """
    url = "https://artofproblemsolving.com/wiki/api.php"
    scraper = cloudscraper.create_scraper()

    current_title = page_title

    for _ in range(max_redirects):
        params = {
            "action": "query",
            "prop": "revisions",
            "rvprop": "content",
            "format": "json",
            "titles": current_title,
            "redirects": 1,  # MediaWiki API automatically resolves standard redirects
        }

        response = scraper.get(url=url, params=params)
        response.raise_for_status()

        data = response.json()
        pages = data.get("query", {}).get("pages", {})

        content = ""
        for page_id, page_info in pages.items():
            if page_id == "-1":
                raise ValueError(
                    f"Page '{current_title}' does not exist on AoPS Wiki."
                )
            content = page_info["revisions"][0]["*"]

        # Manual fallback check for unbracketed or leftover #redirect syntax
        redirect_match = re.match(
            r"#redirect\s*:?\s*\[?\[?([^\]\n]+)\]?\]?",
            content.strip(),
            re.IGNORECASE,
        )

        if redirect_match:
            # Extract the redirected page title and fetch again
            current_title = redirect_match.group(1).strip()
            print(f"Redirecting to: {current_title}")
        else:
            return content

    raise RuntimeError(
        f"Too many redirects encountered while fetching '{page_title}'."
    )

# --- Test Execution ---
if __name__ == "__main__":
    sample_page = "2009_AMC_10A_Problems/Problem_25"

    print("Fetching raw page...")
    raw_wikitext = fetch_aops_page(sample_page)

    # 1. Extract problem + options section only
    problem_only_wikitext = extract_problem_statement(raw_wikitext)

    # 2. Convert to clean LaTeX syntax
    parsed_latex = latexify(problem_only_wikitext)

    print("--- PARSED LATEX OUTPUT ---")
    print(parsed_latex)