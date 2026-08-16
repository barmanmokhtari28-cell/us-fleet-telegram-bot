import os
import re
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
USNI_TRACKER_URL = "https://news.usni.org/category/fleet-tracker"


def get_latest_report_url():
    """Finds the URL of the most recent Fleet Tracker update."""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    response = requests.get(USNI_TRACKER_URL, headers=headers)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    # Locate first article link inside the archive listing
    article = soup.find("article") or soup.find("div", class_="post")
    if not article:
        raise Exception("Could not find recent fleet report listing.")

    link_tag = article.find("a", href=True)
    return link_tag["href"]


def capture_fleet_map_and_summary(article_url, output_image_path="fleet_map.png"):
    """Uses Playwright to render the page, extract key text, and screenshot the map."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Configure a desktop viewport size
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()
        page.goto(article_url, wait_until="networkidle")

        # Get article title
        title = page.title().split(" - ")[0].strip()

        # Locate the primary infographic / map on the page
        # USNI posts an infographic image/element representing fleet positions
        map_element = page.locator("article img").first
        if map_element.is_visible():
            map_element.screenshot(path=output_image_path)
        else:
            # Fallback: capture main article container
            page.locator("article").screenshot(path=output_image_path)

        # Extract carrier strike group summaries
        body_text = page.locator("article").inner_text()
        
        # Simple extraction of deployment points
        deployments = []
        for line in body_text.splitlines():
            line = line.strip()
            if any(term in line.lower() for term in ["carrier strike group", "amphibious ready group", "uss "]):
                if len(line) < 180 and line not in deployments:
                    deployments.append(f"• {line}")

        browser.close()

    summary = "\n".join(deployments[:6])  # Top 6 deployment notes
    caption = f"⚓ *{title}*\n\n*Key Deployments:*\n{summary}\n\n🔗 [Source]({article_url})"
    return output_image_path, caption


def send_to_telegram(image_path, caption):
    """Sends the captured screenshot and caption to Telegram channel/group."""
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"

    with open(image_path, "rb") as img_file:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": caption,
            "parse_mode": "Markdown",
        }
        files = {"photo": img_file}
        response = requests.post(api_url, data=payload, files=files)

    response.raise_for_status()
    print("Update successfully posted to Telegram.")


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in environment variables.")

    print("Fetching latest fleet tracker URL...")
    latest_url = get_latest_report_url()
    print(f"Latest update: {latest_url}")

    print("Rendering page and capturing screenshot...")
    img_path, message_caption = capture_fleet_map_and_summary(latest_url)

    print("Broadcasting to Telegram...")
    send_to_telegram(img_path, message_caption)
