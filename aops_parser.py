import re
import cloudscraper
from curl_cffi import requests
from bs4 import BeautifulSoup
import asyncio
from playwright.async_api import async_playwright
import sqlite3

def extract_latex(alt: str):
    alt = alt.strip()

    if alt.startswith("$$") and alt.endswith("$$"):
        return alt[2:-2]

    if alt.startswith("$") and alt.endswith("$"):
        return alt[1:-1]

    if alt.startswith(r"\[") and alt.endswith(r"\]"):
        return alt[2:-2]

    return alt

# add "https:" to start of image url
def normalize_image_url(img):
    src = img.get("src")

    if src and src.startswith("//"):
        img["src"] = "https:" + src

# convert html to katex
def convert_aops_html(aops_html: str) -> str:

    soup = BeautifulSoup(aops_html, "html.parser")

    for img in soup.find_all("img"):

        classes = img.get("class", [])
        alt = img.get("alt", "") or ""

        normalize_image_url(img)

        # asymptote
        if alt.lstrip().startswith("[asy]"):
            img["class"] = classes + ["aops-block-image"]
            continue

        # inline latex
        if "latex" in classes:
            latex = extract_latex(alt)
            replacement = soup.new_string(f"\\({latex}\\)")
            img.replace_with(replacement)
            continue

        # display latex
        if "latexcenter" in classes:
            latex = extract_latex(alt)
            replacement = soup.new_string(f"\\[{latex}\\]")
            img.replace_with(replacement)
            continue

        # otherwise, normal image
        img["class"] = classes + ["aops-block-image"]

    return str(soup)

# html template with katex
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

                font-family: "Computer Modern Serif";

                font-size: 20px;
                line-height: 1.45;
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

def fetch_raw_wikitext(page_title: str) -> str:
    # url = "https://artofproblemsolving.com/wiki/api.php"
    #
    # scraper = cloudscraper.create_scraper()
    #
    # params = {
    #     "action": "parse",
    #     "page": page_title,
    #     "format": "json",
    #     "prop": "text",
    #     "disablelimitreport": True,
    # }
    #
    # response = scraper.get(url=url, params=params)
    # response.raise_for_status()
    #
    # #print(response.url)
    #
    # data = response.json()
    #
    # print("Fetched html!")
    # return data.get("parse", {}).get("text", {}).get("*", "")

    url = f"https://artofproblemsolving.com/wiki/index.php?title={page_title}"
    # scraper = cloudscraper.create_scraper()
    #
    # response = scraper.get(url)
    response = requests.get(url, impersonate="chrome")
    response.raise_for_status()

    print(f"Fetched AoPS page: {page_title}")
    return response.text

def extract_problems_from_html(raw_wikitext: str) -> list:
    soup = BeautifulSoup(raw_wikitext, "html.parser")
    problems = []

    # Find problem headers (h2)
    for header in soup.find_all("h2"):
        headline = header.find(class_="mw-headline")
        if headline and "problem" in headline.get("id").lower():
            content = [str(header)]
            curr = header.next_sibling
            while curr:
                if curr.name == "h2":
                    break
                if curr.name == "p" and curr.find("a", string=re.compile(r"Solution", re.I)):
                    curr.a.decompose()
                content.append(str(curr))
                curr = curr.next_sibling
            problems.append("".join(content))

    # convert
    problems = [convert_aops_html(problem) for problem in problems]

    print("Problems extracted and converted!")
    return problems

def fetch_all_problems(year: int, wiki_name: str) -> list:
    page_title = f"{year}_{wiki_name}_Problems"

    raw_wikitext = fetch_raw_wikitext(page_title)

    problems = extract_problems_from_html(raw_wikitext)

    return problems

async def render_problem(semaphore, context, html: str, output_path: str, id: int):
    async with semaphore:
        page = await context.new_page()

        document = create_html_document(html)

        await page.set_content(document)

        await page.locator("body").screenshot(path=output_path)

        await page.close()

        with sqlite3.connect("math_problems.db") as conn:
            conn.execute("UPDATE math_problems SET rendered = 1 WHERE id = ?", (id,))
        print(f"Successfully rendered problem id: {id}!")

async def render_problems():
    print("Starting...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 900, "height": 800}, device_scale_factor=2)
        semaphore = asyncio.Semaphore(4)

        with sqlite3.connect("math_problems.db") as conn:
            rows = conn.execute("SELECT question_statement, image_path, id FROM math_problems WHERE rendered = 0").fetchall()

        tasks = [render_problem(semaphore, context, html=row[0], output_path=row[1], id=row[2]) for row in rows]
        await asyncio.gather(*tasks)

        await browser.close()
        print("Done!")

if __name__ == "__main__":
    asyncio.run(render_problems())