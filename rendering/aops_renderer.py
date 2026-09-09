import asyncio
import os
import sqlite3
from playwright.async_api import async_playwright
from constants import DATABASE, RENDERS_DIR
from logger_config import get_file_logger

logger = get_file_logger("renderer", "logging/renderer.log")

def create_html_document(body_html: str) -> str:
    return f"""<!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Page Title</title>
        <!-- Computer Modern -->
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

        .latexcenter, .mw-file-element {{
            display: block;
            max-width: 90%;
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

async def render_problem(semaphore, context, html: str, output_path: str, problem_id: str):
    """Render a single problem into PNG format if the output file does not exist."""
    if os.path.exists(output_path):
        return

    async with semaphore:
        if os.path.exists(output_path):
            return

        page = await context.new_page()
        try:
            document = create_html_document(html)

            await page.set_content(document, wait_until="networkidle")
            await page.evaluate("document.fonts.ready")

            await page.locator("body").screenshot(path=output_path)
            logger.info(f"Successfully rendered problem id: {problem_id}")

        except Exception as e:
            logger.error(f"Failed to render problem id {problem_id}: {e}", exc_info=True)
        finally:
            await page.close()

async def render_all_problems(scraper_finished_event: asyncio.Event = None):
    """Poll database for records and render missing problem images."""
    print("Starting image renderer...")
    os.makedirs(RENDERS_DIR, exist_ok=True)
    in_flight = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 900, "height": 800}, device_scale_factor=2)
        semaphore = asyncio.Semaphore(1)

        while True:
            with sqlite3.connect(DATABASE) as conn:
                rows = conn.execute("SELECT id, question_statement FROM math_problems").fetchall()

            unrendered_rows = []

            for problem_id, html in rows:
                output_path = os.path.join(RENDERS_DIR, f"{problem_id}.png")
                if not os.path.exists(output_path) and problem_id not in in_flight:
                    unrendered_rows.append((html, output_path, problem_id))

            for html, output_path, problem_id in unrendered_rows:
                in_flight.add(problem_id)

                async def task_wrapper(h=html, op=output_path, pid=problem_id):
                    try:
                        await render_problem(semaphore, context, h, op, pid)
                    finally:
                        in_flight.remove(pid)

                asyncio.create_task(task_wrapper())

            is_finished = scraper_finished_event.is_set() if scraper_finished_event else True

            if not in_flight and is_finished and not unrendered_rows:
                break

            await asyncio.sleep(1)

        await browser.close()
        logger.info("Done rendering all problems!")