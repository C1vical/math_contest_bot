from patchright.async_api import async_playwright
import asyncio
import json

base_url = "https://artofproblemsolving.com/community/"
session_id = "21d6f40cfb511982e4424e0e250a9557" # same ID for not logged in

# contest and its AOPS collection id
contests = {
    "CMO": "3277",
    "CJMO": "1231801",
    "CMOQR": "3280"
}

async def fetch_category_data(page, collection_id):
    return await page.evaluate("""
        async ([collection_id, session_id]) => {
        const response = await fetch(
                "https://artofproblemsolving.com/m/community/ajax.php",
                {
                    headers: {
                        "accept": "application/json, text/javascript, */*; q=0.01",
                        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "x-requested-with": "XMLHttpRequest"
                    },
                    body: `category_id=${collection_id}&a=fetch_category_data&aops_logged_in=false&aops_user_id=1&aops_session_id=${session_id}`,
                    method: "POST",
                    credentials: "include"
                }
            );

        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        return await response.json();
    }""", [collection_id, session_id])

async def fetch_item_ids(page) -> dict:
    ids = {}
    for contest, collection_id in contests.items():
        print(f"Fetching {contest}...")

        response = await fetch_category_data(page, collection_id)
        items = response.get("response", {}).get("category", {}).get("items", [])

        for item in items:
            ids[f"{item['item_score']} {contest}"] = item["item_id"]

    print(f"Fetched item ids successfully.")
    return ids

async def main():
    async with async_playwright() as p:
        print("Initializing session...")
        browser = await p.chromium.launch(headless=True, channel="chrome")
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        await page.goto(base_url)

        ids = await fetch_item_ids(page)

        # convert to json and write to file
        print("Writing to file...")
        with open("ids.json", "w", encoding="utf-8") as f:
            json.dump(ids, f, indent=4)

        # for p in problems:
        #     print(p["post_data"]["post_rendered"])
        #     print("\n")
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())