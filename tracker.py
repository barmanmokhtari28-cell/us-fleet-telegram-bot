import html
import os
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SOURCE_URL = "https://news.usni.org/category/fleet-tracker"
LAST_POSTED_FILE = "last_posted.txt"


def get_latest_article():
    """Extracts the latest fleet update URL and title."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    res = requests.get(SOURCE_URL, headers=headers, timeout=15)
    res.raise_for_status()

    soup = BeautifulSoup(res.text, "html.parser")
    article = soup.find("article") or soup.find("div", class_="post")
    if not article:
        raise Exception("Could not find any fleet reports.")

    link_tag = article.find("a", href=True)
    return link_tag["href"], link_tag.get_text(strip=True)


def has_already_been_posted(article_url):
    """Checks if this specific report has already been sent to Telegram."""
    if os.path.exists(LAST_POSTED_FILE):
        with open(LAST_POSTED_FILE, "r") as f:
            last_url = f.read().strip()
            if last_url == article_url:
                return True
    return False


def save_last_posted(article_url):
    with open(LAST_POSTED_FILE, "w") as f:
        f.write(article_url)


def capture_fleet_data(article_url, output_image="armada_map.png"):
    """Launches headless Chromium to take a screenshot and parse armada details."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        page.goto(article_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)  # Wait for images to render

        title = page.title().split(" - ")[0].strip()

        # Find and screenshot the armada map/infographic image
        map_img = page.locator(".entry-content img, .single56__post_content img, article img").first
        if map_img.is_visible():
            map_img.screenshot(path=output_image)
        else:
            page.locator(".entry-content, .single56__post_content, #wi-content").first.screenshot(path=output_image)

        # Extract text safely using .first to prevent strict-mode violations
        content_elem = page.locator(".entry-content, .single56__post_content, #wi-content, article").first
        body_text = content_elem.inner_text()

        deployments = []
        for line in body_text.splitlines():
            line = line.strip()
            if any(k in line.lower() for k in ["carrier strike group", "amphibious ready group", "uss "]):
                if 10 < len(line) < 180 and line not in deployments:
                    deployments.append(f"🚢 {html.escape(line)}")

        browser.close()

    summary_text = "\n".join(deployments[:6]) if deployments else "Latest fleet movements available in full report."
    
    caption = (
        f"⚓ <b>{html.escape(title)}</b>\n\n"
        f"<b>Live U.S. Armada / Strike Group Locations:</b>\n"
        f"{summary_text}\n\n"
        f"🔗 <a href='{article_url}'>Full Deployment Source</a>"
    )
    return output_image, caption


def send_telegram_alert(image_path, caption):
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    with open(image_path, "rb") as img:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": caption,
            "parse_mode": "HTML"
        }
        files = {"photo": img}
        res = requests.post(api_url, data=payload, files=files, timeout=30)
        res.raise_for_status()


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("Telegram Bot Token or Chat ID is missing!")

    latest_url, title = get_latest_article()
    print(f"Checking report: {latest_url}")

    if has_already_been_posted(latest_url):
        print("This deployment report has already been posted. Skipping.")
    else:
        print("New armada deployment found! Taking screenshot...")
        img, caption = capture_fleet_data(latest_url)

        print("Sending to Telegram...")
        send_telegram_alert(img, caption)

        # Save memory
        save_last_posted(latest_url)
        print("Done!")
