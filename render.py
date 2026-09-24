import asyncio
import sqlite3
import aiohttp
from playwright.async_api import async_playwright
from constants import DATABASE, RENDERS_DIR, R2_BUCKET_URL

def create_html_document(body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">

<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Problem Render</title>

    <!-- Computer Modern Font -->
    <link rel="stylesheet" type="text/css" href="https://cdn.jsdelivr.net/gh/dreampulse/computer-modern-web-font@master/fonts.css">

    <style>
        html,
        body {{
            margin: 0;
            padding: 0;
            background: #ffffff;
        }}

        html {{
            line-height: 1.45;
            font-family: "Computer Modern Serif", serif;
            font-size: 20px;
            color: #171717;
        }}

        body {{
            margin: 0 auto;
            max-width: 1000px;
            padding: 32px 42px;
        }}

        @media (max-width: 720px) {{
            body {{
                padding: 16px;
            }}
        }}

        h1 {{
            text-align: center;
            font-size: 34px;
            margin-top: 1.4em;
        }}

        h2 {{
            margin: 0 0 18px 0;
            padding: 0 0 8px 0;
            font-size: 25px;
            font-weight: bold;
            line-height: 1.25;
            border-bottom: 1px solid #cccccc;
        }}

        h3 {{
            font-size: 1.1em;
            margin-top: 1.4em;
        }}

        h4, h5, h6 {{
            margin-top: 1.4em;
        }}

        p {{
            margin: 0 0 0.75em 0;
        }}

        ol, ul {{
            padding-left: 1.7em;
            margin-top: 1em;
        }}

        li > ol, li > ul {{
            margin-top: 0;
        }}

        ol.horizontal {{
            width: 100%;
            counter-reset: list list-item;
            display: flex;
            justify-content: flex-start;
            align-items: baseline;
            flex-wrap: wrap;
            padding-left: 0;
        }}

        ol.horizontal li {{
            list-style: none;
            width: 185px;
            padding: 2px 0;
        }}

        ol.horizontal li::before {{
            content: " (" counter(list, upper-alpha) ") ";
            counter-increment: list;
        }}

        img {{
            max-width: 100%;
            height: auto;
        }}

        .static {{
            max-width: 100%;
            min-width: 0%;
        }}

        .center {{
            text-align: center;
        }}

        .latexcenter, .mw-file-element, .asy-image {{
            display: block;
            max-width: 100%;
            height: auto;
            margin: 14px auto;
        }}

        a {{
            color: inherit;
            text-decoration: none;
        }}

        .column-five {{
            float: left;
            width: 20%;
        }}

        .row::after {{
            content: "";
            display: table;
            clear: both;
        }}

        @media screen and (max-width: 960px) {{
            .column-five {{
                width: 50%;
            }}
        }}
    </style>

    <script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml-full.js" type="text/javascript"></script>
</head>

<body>
    {body_html}
</body>

</html>
"""

async def render_problem(semaphore, context, html: str, problem_id: str):
    """Render a single problem into PNG format if it doesn't already exist."""
    output_path = RENDERS_DIR / f"{problem_id}.png"

    if output_path.exists():
        return

    async with semaphore:
        page = await context.new_page()
        try:
            document = create_html_document(html)
            await page.set_content(document, wait_until="load")
            await page.locator("body").screenshot(path=output_path)
            print(f"Successfully rendered problem id: {problem_id}")
        except Exception as e:
            print(f"Failed to render problem id {problem_id}: {e}")
        finally:
            await page.close()

async def render_all_problems():
    """Fetch all rows from the database once and render them in batch."""
    print("Rendering problems...")

    # Create directory if it doesn't exist
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Fetch database records once upfront
    with sqlite3.connect(DATABASE) as conn:
        rows = conn.execute("SELECT id, question_statement FROM math_problems").fetchall()

    # 2. Render all unrendered problems using asyncio.gather
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(device_scale_factor=2)
        semaphore = asyncio.Semaphore(8)  # Set concurrency limit

        tasks = [render_problem(semaphore, context, html, problem_id) for problem_id, html in rows]

        await asyncio.gather(*tasks)
        await browser.close()

    print("Done rendering all problems!")

async def get_image_data(problem_id: str):
    image_url = f"{R2_BUCKET_URL}/{problem_id}.png"

    async with aiohttp.ClientSession() as session:
        async with session.get(image_url) as resp:
            return await resp.read()

if __name__ == "__main__":
    asyncio.run(render_all_problems())