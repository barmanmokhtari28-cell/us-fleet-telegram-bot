import html
import os
import re
from datetime import datetime, timezone
import requests
from playwright.sync_api import sync_playwright

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SOURCE_URL = "https://news.usni.org/category/fleet-tracker"
OUTPUT_IMAGE = "armada_map.png"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}


def fetch_via_proxy_reader(url):
    """Uses the free Jina Reader proxy to bypass Cloudflare Turnstile challenges."""
    reader_url = f"https://r.jina.ai/{url}"
    res = requests.get(reader_url, headers=HEADERS, timeout=30)
    res.raise_for_status()
    return res.text


def get_data_via_proxy():
    """Extracts article content, strike group deployments, and direct map image URL."""
    print("Fetching fleet archive via reader proxy...")
    archive_text = fetch_via_proxy_reader(SOURCE_URL)

    # Locate latest fleet tracker article link
    links = re.findall(r"https://news\.usni\.org/\d{4}/\d{2}/\d{2}/[a-zA-Z0-9\-]+", archive_text)
    tracker_links = [l for l in links if "fleet-and-marine-tracker" in l]

    if not tracker_links:
        raise Exception("Could not locate tracker link in archive.")

    latest_url = tracker_links[0]
    print(f"Latest report: {latest_url}")

    # Fetch report content
    article_text = fetch_via_proxy_reader(latest_url)

    # Extract title
    title_match = re.search(r"Title:\s*(.+)", article_text)
    raw_title = title_match.group(1).strip() if title_match else "Fleet Tracker"
    clean_title = raw_title.replace("USNI News Fleet and Marine Tracker:", "").strip()

    # Extract original high-res armada infographic map
    img_candidates = re.findall(r"https://news\.usni\.org/wp-content/uploads/[^\s\)\"\'\<\>]+\.(?:png|jpg|jpeg)", article_text)
    map_url = None
    for img in img_candidates:
        if not any(x in img.lower() for x in ["logo", "avatar", "icon", "banner", "author"]):
            map_url = img
            break

    if not map_url:
        raise Exception("Could not find high-res armada map image.")

    print(f"Downloading original fleet map: {map_url}")
    img_res = requests.get(map_url, headers=HEADERS, timeout=30)
    img_res.raise_for_status()
    with open(OUTPUT_IMAGE, "wb") as f:
        f.write(img_res.content)

    # Extract strike groups and deployments
    deployments = []
    for line in article_text.splitlines():
        line = line.strip()
        if any(k in line.lower() for k in ["carrier strike group", "amphibious ready group", "uss "]):
            # Clean markdown formatting like brackets or leading dashes
            clean_line = re.sub(r"^[-\*\#\s\d\.]+", "", line).strip()
            if 12 < len(clean_line) < 140 and clean_line not in deployments:
                deployments.append(f"🔹 <i>{html.escape(clean_line)}</i>")

    return clean_title, deployments


def get_data_via_stealth_playwright():
    """Fallback method using stealth headless browser with Turnstile solver."""
    print("Using stealth browser fallback...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=HEADERS["User-Agent"]
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()

        page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        # Handle Cloudflare verification if it appears
        for _ in range(8):
            title = page.title().lower()
            if "security verification" in title or "just a moment" in title:
                print("Cloudflare verification screen detected. Attempting to click...")
                for frame in page.frames:
                    try:
                        box = frame.locator("input[type='checkbox'], #challenge-stage, .ctp-checkbox-label").first
                        if box.is_visible():
                            box.click()
                            page.wait_for_timeout(4000)
                            break
                    except Exception:
                        pass
                page.wait_for_timeout(2000)
            else:
                break

        page.screenshot(path=OUTPUT_IMAGE)
        clean_title = page.title().split(" - ")[0].replace("USNI News Fleet and Marine Tracker:", "").strip()
        body_text = page.inner_text("body")

        deployments = []
        for line in body_text.splitlines():
            line = line.strip()
            if any(k in line.lower() for k in ["carrier strike group", "amphibious ready group", "uss "]):
                if 12 < len(line) < 140 and line not in deployments:
                    deployments.append(f"🔹 <i>{html.escape(line)}</i>")

        browser.close()
        return clean_title, deployments


def build_caption(clean_title, deployments):
    summary_text = "\n".join(deployments[:5]) if deployments else "🔹 <i>اطلاعات تکمیلی در نقشه گزارش USNI درج شده است.</i>"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return (
        f"🧭 <b>آخرین موقعیت ناوگان و ناوهای جنگی آمریکا</b>\n"
        f"<blockquote><b>گزارش:</b> {html.escape(clean_title)}\n"
        f"🕒 <b>به‌روزرسانی:</b> {now_str}</blockquote>\n\n"
        f"📍 <b>موقعیت ناوهای هواپیمابر و گروه‌های رزمی:</b>\n"
        f"{summary_text}\n\n"
        f"📫 @secretollah\n"
        f"#USNI\n"
        f"#ناو"
    )


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

    try:
        title, deployments = get_data_via_proxy()
    except Exception as e:
        print(f"Proxy method encountered an issue ({e}). Switching to stealth browser...")
        title, deployments = get_data_via_stealth_playwright()

    caption = build_caption(title, deployments)

    print("Posting clean armada update to Telegram...")
    send_telegram_alert(OUTPUT_IMAGE, caption)
    print("Update successfully delivered!")
