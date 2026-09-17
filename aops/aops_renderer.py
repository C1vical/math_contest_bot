import asyncio
import os
import sqlite3
import aiohttp
from playwright.async_api import async_playwright
from constants import DATABASE, RENDERS_DIR, R2_BUCKET_URL

def create_html_document(body_html: str) -> str:
    return f"""<!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Problem Render</title>
        <!-- Computer Modern Font-->
        <link rel="stylesheet" type="text/css" href="https://cdn.jsdelivr.net/gh/dreampulse/computer-modern-web-font@master/fonts.css">
        <style>
        * {{
            box-sizing: border-box;
        }}

        html,
        body {{
            margin: 0;
            padding: 0;
            background: #ffffff;
        }}

        body {{
            width: 1000px;
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

        p {{
            margin: 0 0 0.75em 0;
        }}

        img {{
            max-width: 100%;
            height: auto;
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
    </style>
    </head>
    <body>
        {body_html}
    </body>
    </html>"""

async def render_problem(semaphore, context, html: str, problem_id: str):
    """Render a single problem into PNG format if it doesn't already exist."""
    output_path = os.path.join(RENDERS_DIR, f"{problem_id}.png")

    if os.path.exists(output_path):
        return

    async with semaphore:
        page = await context.new_page()
        try:
            document = create_html_document(html)
            await page.set_content(document)
            await page.locator("body").screenshot(path=output_path)
            print(f"Successfully rendered problem id: {problem_id}")
        except Exception as e:
            print(f"Failed to render problem id {problem_id}: {e}")
        finally:
            await page.close()

async def render_all_problems():
    """Fetch all rows from the database once and render them in batch."""
    print("Rendering problems...")
    os.makedirs(RENDERS_DIR, exist_ok=True)

    # 1. Fetch database records once upfront
    with sqlite3.connect(DATABASE) as conn:
        rows = conn.execute("SELECT id, question_statement FROM math_problems").fetchall()

    # 2. Render all unrendered problems using asyncio.gather
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # context = await browser.new_context(viewport={"width": 900, "height": 800}, device_scale_factor=2)
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