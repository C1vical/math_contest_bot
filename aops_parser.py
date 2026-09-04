import asyncio
import os
import random
import sqlite3
import time

import cloudscraper
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from constants import DATABASE


# ============================================================
# LaTeX & URL Normalization
# ============================================================

def extract_latex(alt: str):
    """
    Extract raw LaTeX string from standard delimiters in image alt text.

    Args:
        alt (str): The alt text of an image tag.

    Returns:
        str | None: Cleaned LaTeX string if delimited, else None.
    """
    alt = alt.strip()

    if alt.startswith("$$") and alt.endswith("$$"):
        return alt[2:-2]

    if alt.startswith("$") and alt.endswith("$"):
        return alt[1:-1]

    if alt.startswith(r"\[") and alt.endswith(r"\]"):
        return alt[2:-2]

    return None


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
            # Politeness delay between attempts
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
    """
    Parse AoPS Wiki HTML content to extract problem section blocks under H2/H3 headlines.

    Args:
        raw_wikitext (str): Raw HTML source from AoPS wiki page.

    Returns:
        list: A list of extracted problem HTML string snippets.
    """
    soup = BeautifulSoup(raw_wikitext, "html.parser")
    problems = []

    for header in soup.find_all(["h2", "h3"]):
        headline = header.find(class_="mw-headline")
        if headline and "problem" in headline.get("id", "").lower():
            content = []
            curr = header.next_sibling
            while curr:
                # Stop when reaching the next section header
                if curr.name in ["h2", "h3"]:
                    break
                content.append(str(curr))
                curr = curr.next_sibling

            problem_html = "".join(content).strip()
            if problem_html:
                problems.append(problem_html)

    print(f"Extracted {len(problems)} problems from HTML.")
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
            img["class"] = list(dict.fromkeys(classes + ["aops-block-image"]))
            continue

        # Inline LaTeX math tags
        if "latex" in classes:
            latex = extract_latex(alt)
            if latex is None:
                img["class"] = list(dict.fromkeys(classes + ["aops-block-image"]))
                continue

            img.replace_with(soup.new_string(rf"\({latex}\)"))
            continue

        # Display/Centered LaTeX math tags
        if "latexcenter" in classes:
            latex = extract_latex(alt)
            if latex is None:
                img["class"] = list(dict.fromkeys(classes + ["aops-block-image"]))
                continue

            img.replace_with(soup.new_string(rf"\[{latex}\]"))
            continue

        # Standard standalone images
        img["class"] = list(dict.fromkeys(classes + ["aops-block-image"]))

    return str(soup)


def create_html_document(body_html: str) -> str:
    """
    Wrap problem HTML fragment in a full standalone HTML document configured with KaTeX,
    Computer Modern fonts, and Discord image layout rules.

    Args:
        body_html (str): Converted inner HTML problem statement.

    Returns:
        str: Complete renderable HTML document string.
    """
    return f"""<!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">

        <title>AoPS Problem</title>

        <!-- Computer Modern Font -->
        <link
            rel="stylesheet"
            type="text/css"
            href="https://cdn.jsdelivr.net/gh/dreampulse/computer-modern-web-font@master/fonts.css"
        >

        <!-- KaTeX Math Engine -->
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


# ============================================================
# Playwright Core Rendering Engine
# ============================================================

async def wait_for_images(page):
    """
    Wait until all images on the active browser page are fully downloaded and loaded.

    Args:
        page (playwright.async_api.Page): Active Playwright page object.
    """
    await page.wait_for_function(
        "() => Array.from(document.images).every(img => img.complete)"
    )


async def render_aops_page(
        page,
        aops_html: str,
        output_path: str = "problem.png",
):
    """
    Render transformed problem HTML string into a PNG image using a Playwright page instance.

    Args:
        page (playwright.async_api.Page): Active Playwright browser page instance.
        aops_html (str): Problem HTML string snippet to render.
        output_path (str): File destination path for generated image.
    """
    converted_html = convert_aops_html(aops_html)
    document = create_html_document(converted_html)

    await page.set_content(document, wait_until="networkidle")

    # Wait for KaTeX math typesetting to execute if applicable
    try:
        await page.wait_for_selector(".katex", timeout=3000)
    except Exception:
        pass

    await wait_for_images(page)

    # Force double animation frame render buffer before taking screenshot
    await page.evaluate(
        """() => new Promise(resolve => {
            requestAnimationFrame(() => {
                requestAnimationFrame(resolve);
            });
        })"""
    )

    await page.locator("body").screenshot(path=output_path)


# ============================================================
# Database Batch Renderer
# ============================================================

async def render_all_problems_from_db(
        db_path: str = DATABASE,
        width: int = 900,
        device_scale_factor: int = 2,
        concurrency: int = 4,
        poll_interval: float = 3.0,
):
    """
    Continuously monitor math_problems.db and render unrendered problem entries to PNG files.

    Args:
        db_path (str): Path to SQLite database file.
        width (int): Target viewport width for browser page.
        device_scale_factor (int): Scale factor multiplier for crisp high-DPI screenshot output.
        concurrency (int): Maximum concurrent tab rendering workers.
        poll_interval (float): Database polling pause delay in seconds.
    """
    print("Starting Playwright image rendering daemon...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        semaphore = asyncio.Semaphore(concurrency)

        try:
            while True:
                if not os.path.exists(db_path):
                    await asyncio.sleep(poll_interval)
                    continue

                # Query database for problem entries
                with sqlite3.connect(db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT id, question_statement, image_path FROM math_problems")
                    rows = cursor.fetchall()

                # Filter items missing an image file on disk
                pending = [
                    (pid, question_statement, path)
                    for pid, question_statement, path in rows
                    if not os.path.exists(path)
                ]

                if pending:
                    print(f"Found {len(pending)} new problems to render...")

                    async def worker(pid, statement, image_path, idx):
                        async with semaphore:
                            # Create destination directory structure if needed
                            os.makedirs(os.path.dirname(image_path), exist_ok=True)

                            page = await browser.new_page(
                                viewport={"width": width, "height": 800},
                                device_scale_factor=device_scale_factor,
                            )
                            try:
                                await render_aops_page(page, statement, output_path=image_path)
                                print(f"[{idx}/{len(pending)}] Rendered: {image_path}")
                            except Exception as e:
                                print(f"Failed to render {pid}: {e}")
                            finally:
                                await page.close()

                    tasks = [
                        worker(pid, stmt, path, idx)
                        for idx, (pid, stmt, path) in enumerate(pending, start=1)
                    ]
                    await asyncio.gather(*tasks)

                await asyncio.sleep(poll_interval)

        except KeyboardInterrupt:
            print("\nStopping render daemon.")
        finally:
            await browser.close()


# ============================================================
# Main Execution
# ============================================================

if __name__ == "__main__":
    asyncio.run(render_all_problems_from_db())