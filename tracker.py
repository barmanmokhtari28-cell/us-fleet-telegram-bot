import html
import os
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SOURCE_URL = "https://news.usni.org/category/fleet-tracker"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


def get_latest_article():
    """Extracts the latest fleet update URL and title."""
    res = requests.get(SOURCE_URL, headers=HEADERS, timeout=20)
    res.raise_for_status()

    soup = BeautifulSoup(res.text, "html.parser")
    # Find the first link pointing to a fleet-and-marine-tracker report
    for a in soup.find_all("a", href=True):
        if "fleet-and-marine-tracker" in a["href"]:
            return a["href"], a.get_text(strip=True)

    article = soup.find("article") or soup.find("div", class_="post")
    if article and article.find("a", href=True):
        link_tag = article.find("a", href=True)
        return link_tag["href"], link_tag.get_text(strip=True)

    raise Exception("Could not find any fleet reports.")


def capture_fleet_data(article_url, output_image="armada_map.png"):
    """Downloads the high-res fleet map (or screenshots the page) and extracts ship locations."""
    res = requests.get(article_url, headers=HEADERS, timeout=20)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")

    # 1. Extract Title
    raw_title = soup.title.get_text(strip=True).split(" - ")[0] if soup.title else "Fleet Tracker"
    clean_title = raw_title.replace("USNI News Fleet and Marine Tracker:", "").strip()

    # 2. Try to grab the official high-res map image directly
    image_downloaded = False
    img_url = None

    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        img_url = og_img["content"]
    else:
        for img in soup.find_all("img", src=True):
            if "uploads" in img["src"] and not any(x in img["src"].lower() for x in ["avatar", "logo", "icon"]):
                img_url = img["src"]
                break

    if img_url:
        try:
            img_res = requests.get(img_url, headers=HEADERS, timeout=20)
            img_res.raise_for_status()
            with open(output_image, "wb") as f:
                f.write(img_res.content)
            image_downloaded = True
            print(f"Downloaded high-res fleet map: {img_url}")
        except Exception as e:
            print(f"Direct image download failed ({e}), falling back to browser screenshot...")

    # 3. Fallback: Take a full viewport screenshot via Playwright (no fragile CSS selectors)
    if not image_downloaded:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                user_agent=HEADERS["User-Agent"]
            )
            page = context.new_page()
            page.goto(article_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            page.screenshot(path=output_image)
            browser.close()

    # 4. Extract ship and strike group locations from article body
    main_content = soup.find("article") or soup.find("main") or soup.body
    body_text = main_content.get_text(separator="\n") if main_content else ""

    deployments = []
    for line in body_text.splitlines():
        line = line.strip()
        if any(k in line.lower() for k in ["carrier strike group", "amphibious ready group", "uss "]):
            if 12 < len(line) < 140 and line not in deployments:
                deployments.append(f"🔹 <i>{html.escape(line)}</i>")

    summary_text = "\n".join(deployments[:5]) if deployments else "🔹 <i>اطلاعات تکمیلی در نقشه گزارش USNI درج شده است.</i>"

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Persian Rich Text Caption
    caption = (
        f"🧭 <b>آخرین موقعیت ناوگان و ناوهای جنگی آمریکا</b>\n"
        f"<blockquote><b>گزارش:</b> {html.escape(clean_title)}\n"
        f"🕒 <b>به‌روزرسانی:</b> {now_str}</blockquote>\n\n"
        f"📍 <b>موقعیت ناوهای هواپیمابر و گروه‌های رزمی:</b>\n"
        f"{summary_text}\n\n"
        f"📫 @secretollah\n"
        f"#USNI\n"
        f"#ناو"
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
        if not res.ok:
            print(f"Telegram API Error: {res.text}")
        res.raise_for_status()


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("Telegram Bot Token or Chat ID is missing!")

    latest_url, title = get_latest_article()
    print(f"Tracking report: {latest_url}")

    print("Capturing live fleet map and data...")
    img, caption = capture_fleet_data(latest_url)

    print("Sending live update to Telegram...")
    send_telegram_alert(img, caption)
    print("Update successfully delivered!")
