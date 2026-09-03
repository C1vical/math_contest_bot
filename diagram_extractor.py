import cloudscraper
from bs4 import BeautifulSoup
from urllib.parse import urljoin

BASE_URL = "https://artofproblemsolving.com/wiki/index.php"
CONTEST_TITLE = "2004_AMC_10A_Problems"

NUM_PROBLEMS = 25

url = f"{BASE_URL}?title={CONTEST_TITLE}"

scraper = cloudscraper.create_scraper()

try:
    response = scraper.get(url)

    if response.status_code == 200:
        soup = BeautifulSoup(response.text, 'html.parser')

        # Loop through problems 1 to 25
        for x in range(1, NUM_PROBLEMS+1):
            target_id = f"Problem_{x}"
            headline = soup.find(class_='mw-headline', id=target_id)
            if not headline:
                headline = soup.find(class_='mw-headline', id=f"problem_{x}")

            if headline:
                section_imgs = []

                # Iterate through subsequent sibling elements to gather images for THIS problem only
                current_element = headline.parent
                while True:
                    current_element = current_element.next_sibling
                    if not current_element:
                        break

                    # Stop if we hit the next problem heading
                    if current_element.name in ['h1', 'h2', 'h3', 'h4']:
                        next_headline = current_element.find(class_='mw-headline')
                        if next_headline and (
                                next_headline.get('id', '').lower().startswith('problem_') or
                                next_headline.get('id', '').lower().startswith('see_also')):
                            break

                    # Find all images inside the current element container
                    if hasattr(current_element, 'find_all'):
                        found_imgs = current_element.find_all('img')
                        for img in found_imgs:
                            if img.get('src'):
                                section_imgs.append(img)

                # Prioritize images with '[asy]' in their alt text
                asy_matches = [img for img in section_imgs if '[asy]' in (img.get('alt') or '')]

                selected_img_src = None
                if asy_matches:
                    # Choose the last [asy] image if multiple exist
                    selected_img_src = asy_matches[-1].get('src')
                else:
                    # Fallback: look for images where class is neither 'latex' nor 'latexcenter'
                    valid_fallbacks = []
                    for img in section_imgs:
                        css_classes = img.get('class', [])
                        if isinstance(css_classes, str):
                            css_classes = css_classes.split()

                        if 'latex' not in css_classes and 'latexcenter' not in css_classes:
                            valid_fallbacks.append(img)

                    if valid_fallbacks:
                        selected_img_src = valid_fallbacks[-1].get('src')

                if selected_img_src:
                    absolute_image_url = urljoin(url, selected_img_src)
                    print(f"Problem {x}: {absolute_image_url}")
                else:
                    print(f"Problem {x}: No matching images found in this section.")
            else:
                print(f"Problem {x}: Headline ID not found on the page.")

    else:
        print(f"Failed to retrieve the page. Status Code: {response.status_code}")

except Exception as e:
    print(f"An error occurred: {e}")