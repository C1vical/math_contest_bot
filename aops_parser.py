import re
import cloudscraper
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ============================================================
# Extract LaTeX
# ============================================================

def extract_latex(alt: str):
    alt = alt.strip()

    if alt.startswith("$$") and alt.endswith("$$"):
        return alt[2:-2]

    if alt.startswith("$") and alt.endswith("$"):
        return alt[1:-1]

    if alt.startswith(r"\[") and alt.endswith(r"\]"):
        return alt[2:-2]

    return None


# ============================================================
# Normalize image URL
# ============================================================

def normalize_image_url(img):
    src = img.get("src")

    if src and src.startswith("//"):
        img["src"] = "https:" + src

def fetch_aops_page(page_title: str) -> str:
    url = "https://artofproblemsolving.com/wiki/api.php"


    scraper = cloudscraper.create_scraper()

    params = {
        "action": "parse",
        "page": page_title,
        "format": "json",
        "prop": "text",
        "disablelimitreport": True,
    }

    response = scraper.get(url=url, params=params)
    response.raise_for_status()

    #print(response.url)

    data = response.json()

    print(f"Fetched AoPS page: {page_title}")
    return data.get("parse", {}).get("text", {}).get("*", "")

    # url = f"https://artofproblemsolving.com/wiki/index.php?title={page_title}"
    # scraper = cloudscraper.create_scraper()
    #
    # response = scraper.get(url)
    # response.raise_for_status()
    #
    # print(f"Fetched AoPS page: {page_title}")
    # return response.text

def extract_problems_from_html(raw_wikitext: str) -> list:
    soup = BeautifulSoup(raw_wikitext, "html.parser")
    problems = []

    # Find problem headers (h2)
    for header in soup.find_all("h2"):
        headline = header.find(class_="mw-headline")
        if headline and "problem" in headline.get("id").lower():
            content = []
            curr = header
            while curr:
                if curr.name == "p" and curr.find("a", string=re.compile(r"Solution", re.I)):
                    break
                content.append(str(curr))
                curr = curr.next_sibling
            problems.append("".join(content))

    print(f"Extracted {len(problems)} problems from AoPS HTML.")
    return problems

def fetch_aops_problem_set(year: int, wiki_name: str) -> list:
    page = f"{year}_{wiki_name.replace(' ', '_')}_Problems"

    raw_wikitext = fetch_aops_page(page)

    problems = extract_problems_from_html(raw_wikitext)

    print(f"Fetched {len(problems)} problems from AoPS Wiki for {year} {wiki_name}.")
    return problems

# ============================================================
# Convert AoPS HTML
# ============================================================

def convert_aops_html(aops_html: str) -> str:

    soup = BeautifulSoup(aops_html, "html.parser")

    for img in soup.find_all("img"):

        classes = img.get("class", [])
        alt = img.get("alt", "") or ""

        normalize_image_url(img)

        # asymptote
        if alt.lstrip().startswith("[asy]"):

            img["class"] = list(
                dict.fromkeys(
                    classes + ["aops-block-image"]
                )
            )

            continue

        # inline latex
        if "latex" in classes:

            latex = extract_latex(alt)

            if latex is None:

                img["class"] = list(
                    dict.fromkeys(
                        classes + ["aops-block-image"]
                    )
                )

                continue

            replacement = soup.new_string(
                r"\(" + latex + r"\)"
            )

            img.replace_with(replacement)

            continue

        # display latex
        if "latexcenter" in classes:

            latex = extract_latex(alt)

            if latex is None:

                img["class"] = list(
                    dict.fromkeys(
                        classes + ["aops-block-image"]
                    )
                )

                continue

            replacement = soup.new_string(
                r"\[" + latex + r"\]"
            )

            img.replace_with(replacement)

            continue

        # otherwise, normal image
        img["class"] = list(
            dict.fromkeys(
                classes + ["aops-block-image"]
            )
        )

    return str(soup)


# ============================================================
# HTML document
# ============================================================

def create_html_document(body_html: str) -> str:

    return f"""<!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">

        <title>AoPS Problem</title>

        <!-- Computer Modern -->
        <link
            rel="stylesheet"
            type="text/css"
            href="https://cdn.jsdelivr.net/gh/dreampulse/computer-modern-web-font@master/fonts.css"
        >

        <!-- KaTeX -->
        <link
            rel="stylesheet"
            href="https://cdn.jsdelivr.net/npm/katex@0.18.5/dist/katex.min.css"
            integrity="sha384-2dNi/m6JtSiviznrOIZ5fTiZ5As0In2QwkuXSgoqcQtCNplvJAbt+jveeN+8en73"
            crossorigin="anonymous"
        >

        <script
            defer
            src="https://cdn.jsdelivr.net/npm/katex@0.18.5/dist/katex.min.js"
            integrity="sha384-TTF8eEsEKInX2meLzP5V1z/npGYIElXYGksx93f0qBZHu6IL3PdzVB8objytx+TR"
            crossorigin="anonymous">
        </script>

        <script
            defer
            src="https://cdn.jsdelivr.net/npm/katex@0.18.5/dist/contrib/auto-render.min.js"
            integrity="sha384-bjyGPfbij8/NDKJhSGZNP/khQVgtHUE5exjm4Ydllo42FwIgYsdLO2lXGmRBf5Mz"
            crossorigin="anonymous"
            onload="renderMathInElement(document.body);">
        </script>

        <style>

            * {{
                box-sizing: border-box;
            }}

            html, body {{
                margin: 0;
                padding: 0;
                background: #ffffff;
            }}

            body {{
                /*
                 * Discord-friendly width.
                 * At device_scale_factor=2 this produces
                 * a sharp 1800px-wide PNG.
                 */
                width: 900px;

                padding: 32px 42px;

                background: #ffffff;
                color: #171717;

                font-family:
                    "Computer Modern Serif",
                    "Latin Modern Roman",
                    "Times New Roman",
                    serif;

                font-size: 20px;
                line-height: 1.45;

                -webkit-font-smoothing: antialiased;
                text-rendering: optimizeLegibility;
            }}


            /* ================================================
               Problem heading
            ================================================ */

            h2 {{
                margin: 0 0 18px 0;

                padding: 0 0 8px 0;

                font-size: 25px;
                font-weight: bold;

                line-height: 1.25;

                border-bottom: 1px solid #cccccc;
            }}

            h3, h4 {{
                margin-top: 1em;
                margin-bottom: 0.5em;
            }}


            /* ================================================
               Text
            ================================================ */

            p {{
                margin: 0 0 0.75em 0;
            }}


            /* ================================================
               Math
            ================================================ */

            .katex {{
                font-size: 1.05em;
            }}

            .katex-display {{
                margin: 0.75em 0;
            }}


            /* ================================================
               Images / diagrams
            ================================================ */

            img {{
                max-width: 100%;
                height: auto;
            }}

            img.aops-block-image {{
                display: block;

                max-width: 90%;

                height: auto;

                margin: 14px auto;
            }}


            /* ================================================
               Lists
            ================================================ */

            ul, ol {{
                margin-top: 0.4em;
                margin-bottom: 0.7em;

                padding-left: 1.4em;
            }}

            li {{
                margin-bottom: 0.15em;
            }}


            /* ================================================
               Tables
            ================================================ */

            table {{
                border-collapse: collapse;

                margin: 0.75em auto;
            }}

            td, th {{
                padding: 4px 10px;
            }}


            /* ================================================
               AoPS cleanup
            ================================================ */

            .mw-editsection {{
                display: none;
            }}

            a {{
                color: inherit;
                text-decoration: none;
            }}

        </style>
    </head>

    <body>

        {body_html}

    </body>
    </html>"""

# ============================================================
# Render
# ============================================================

def render_problems(year: int, contest: str):
    from database import get_all_problems

    problems = get_all_problems(year, contest)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={
                "width": 900,
                "height": 800,
            },
            device_scale_factor=2,
        )

        x=1
        for problem in problems:
            converted_html = convert_aops_html(
                 problem[4]
            )

            document = create_html_document(
                converted_html
            )

            page.set_content(document)

            body_element = page.locator("body")

            body_element.screenshot(path=problem[6])

            print(f"Problem {x} complete!")
            x+=1

        browser.close()

# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    render_problems(
        year=2025,
        contest="AMC10A"
    )
    print(f"Done!")