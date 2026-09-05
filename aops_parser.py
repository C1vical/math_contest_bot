import asyncio
import os
import random
import re

from curl_cffi import requests
import sqlite3
import time

import cloudscraper
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from constants import DATABASE


# ============================================================
# LaTeX & URL Normalization
# ============================================================

def extract_latex(text: str) -> str:
    """
    Extracts raw LaTeX, strips delimiters, cleans HTML entities,
    and normalizes macros/environments for KaTeX compatibility.
    """
    if not text:
        return ""

    # 1. Replace basic HTML entities
    text = (
        text.replace("&#160;", " ")
        .replace("&#39;", "'")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
    )

    # 2. Strip outer delimiters ($$, $, \[\], \(\))
    text = text.strip()
    if (text.startswith("$$") and text.endswith("$$")) or (text.startswith(r"\[") and text.endswith(r"\]")):
        text = text[2:-2]
    elif (text.startswith("$") and text.endswith("$")) or (text.startswith(r"\(") and text.endswith(r"\)")):
        text = text[1:-1]

    # Remove any remaining loose delimiter escape tags
    text = re.sub(r"\\\[|\\\]|\\\(|\\\)", "", text)

    # 3. Escape HTML comparison operators and raw dollar signs
    text = text.replace("&lt;", r"\lt ").replace("&gt;", r"\gt ")

    # 4. Transform environments and non-standard LaTeX macros
    text = re.sub(r"\{tabular\}(\[\w\])*", "{array}", text)

    text = (
        text.replace("align*", "aligned")
        .replace("eqnarray*", "aligned")
        .replace(r"\bold{", r"\mathbf{")
        .replace(r"\congruent", r"\cong")
        .replace(r"\overarc", r"\overgroup")
        .replace(r"\overparen", r"\overgroup")
        .replace(r"\underarc", r"\undergroup")
        .replace(r"\underparen", r"\undergroup")
        .replace(r"\mathdollar", r"\$")
        .replace(r"\textdollar", r"\$")
    )

    return text.strip()


# ============================================================
# Normalize image URL
# ============================================================

def normalize_image_url(img):
    """
    Ensure relative protocol URLs (starting with '//') are converted to full HTTPS URLs.

    Args:
        img (bs4.element.Tag): BeautifulSoup Image element to normalize in-place.
    """
    src = img.get("src")
    if src and src.startswith("//"):
        img["src"] = "https:" + src


# ============================================================
# AoPS Scraping & HTML Parsing
# ============================================================

def fetch_aops_page(page_title: str, max_retries: int = 3) -> str:
    """
    Fetch raw HTML content of an AoPS wiki page using Cloudscraper with exponential backoff.
    Falls back to headless Playwright if HTTP 403 or request limits are hit.

    Args:
        page_title (str): Title/path parameter of the AoPS wiki target.
        max_retries (int): Maximum retry attempts for Cloudscraper before fallback.

    Returns:
        str: Raw HTML source of the target page, or empty string on failure.
    """
    url = f"https://artofproblemsolving.com/wiki/index.php?title={page_title}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://artofproblemsolving.com/wiki/index.php",
        "Sec-Ch-Ua": '"Not-A.Brand";v="99", "Chromium";v="124", "Google Chrome";v="124"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    }

    scraper = cloudscraper.create_scraper()

    for attempt in range(1, max_retries + 1):
        try:
            time.sleep(random.uniform(2.5, 4.5))
            response = scraper.get(url, headers=headers)

            if response.status_code == 403:
                print(f"403 Forbidden for {page_title} on attempt {attempt}.")
                if attempt == max_retries:
                    return asyncio.run(fetch_aops_page_playwright(page_title))

                time.sleep(attempt * 5)
                continue

            response.raise_for_status()
            print(f"Fetched AoPS page: {page_title}")
            return response.text

        except Exception as e:
            if attempt == max_retries:
                print(f"Failed to fetch {page_title}: {e}")
                return asyncio.run(fetch_aops_page_playwright(page_title))
            time.sleep(attempt * 3)

    return ""


async def fetch_aops_page_playwright(page_title: str) -> str:
    """
    Fallback browser fetcher using Playwright when Cloudscraper encounters anti-bot protections.

    Args:
        page_title (str): Title/path parameter of the AoPS wiki target.

    Returns:
        str: Raw HTML source of the target page.
    """
    url = f"https://artofproblemsolving.com/wiki/index.php?title={page_title}"
    print(f"Falling back to Playwright for: {page_title}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US",
        )
        page = await context.new_page()

        response = await page.goto(url, wait_until="networkidle", timeout=15000)

        if response and response.status == 403:
            print(f"Playwright received 403 on {page_title}")
            await browser.close()
            return ""

        content = await page.content()
        await browser.close()
        return content


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

    print("Problems extracted and converted!")
    return problems


def fetch_aops_problem_set(year: int, wiki_name: str) -> list:
    """
    Construct problem page URL, request content, and extract problem array.

    Args:
        year (int): Year of contest.
        wiki_name (str): Wiki target category string.

    Returns:
        list: A list of extracted problem HTML snippets.
    """
    page = f"{year}_{wiki_name.replace(' ', '_')}_Problems"
    raw_wikitext = fetch_aops_page(page)
    problems = extract_problems_from_html(raw_wikitext)

    print(f"Fetched {len(problems)} problems from AoPS Wiki for {year} {wiki_name}.")
    return problems


# ============================================================
# HTML Transformation & Document Prep
# ============================================================

def convert_aops_html(aops_html: str) -> str:
    """
    Transform AoPS HTML tags, convert image math formulas to LaTeX inline/display tags,
    and attach utility classes for styling.

    Args:
        aops_html (str): Raw problem snippet HTML.

    Returns:
        str: Converted HTML string with LaTeX markup inline.
    """
    soup = BeautifulSoup(aops_html, "html.parser")

    for img in soup.find_all("img"):
        classes = img.get("class", [])
        alt = img.get("alt", "") or ""

        normalize_image_url(img)

        # Asymptote visual diagrams
        if alt.lstrip().startswith("[asy]"):
            img["class"] = classes + ["aops-block-image"]
            continue

        # Inline LaTeX math tags
        if "latex" in classes:
            latex = extract_latex(alt)
            replacement = soup.new_string(f"\\({latex}\\)")
            img.replace_with(replacement)
            continue

        # Display/Centered LaTeX math tags
        if "latexcenter" in classes:
            latex = extract_latex(alt)
            replacement = soup.new_string(f"\\[{latex}\\]")
            img.replace_with(replacement)
            continue

        # otherwise, normal image
        img["class"] = classes + ["aops-block-image"]

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
                width: 900px;
                padding: 32px 42px;
                background: #ffffff;
                color: #171717;
                font-family: "Computer Modern Serif";
                font-size: 20px;
                line-height: 1.45;
            }}

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

            p {{
                margin: 0 0 0.75em 0;
            }}

            .katex {{
                font-size: 1.05em;
            }}

            .katex-display {{
                margin: 0.75em 0;
            }}

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

            ul, ol {{
                margin-top: 0.4em;
                margin-bottom: 0.7em;
                padding-left: 1.4em;
            }}

            li {{
                margin-bottom: 0.15em;
            }}

            table {{
                border-collapse: collapse;
                margin: 0.75em auto;
            }}

            td, th {{
                padding: 4px 10px;
            }}

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

    os.makedirs("renders", exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 900, "height": 800}, device_scale_factor=2)
        semaphore = asyncio.Semaphore(10)

        with sqlite3.connect("math_problems.db") as conn:
            rows = conn.execute("SELECT question_statement, image_path, id FROM math_problems WHERE rendered = 0").fetchall()

        tasks = [render_problem(semaphore, context, html=row[0], output_path=row[1], id=row[2]) for row in rows]
        await asyncio.gather(*tasks)

        await browser.close()
        print("Done!")

if __name__ == "__main__":
    asyncio.run(render_problems())