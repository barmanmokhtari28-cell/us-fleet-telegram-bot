import html
import os
from datetime import datetime, timezone
import requests
from playwright.sync_api import sync_playwright

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SOURCE_URL = "https://news.usni.org/category/fleet-tracker"


def run_tracker():
    with sync_playwright() as p:
        # Launch real browser instance to bypass Cloudflare/WAF 403 blocks
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # 1. Navigate to Fleet Tracker archive
        print("Navigating to Fleet Tracker archive...")
        page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        # 2. Extract latest report URL
        report_link = None
        for a in page.locator("a").all():
            href = a.get_attribute("href") or ""
            if "fleet-and-marine-tracker" in href:
                report_link = href
                break

        if not report_link:
            # Fallback to first article heading link
            first_article_link = page.locator("article a, .post a, h2 a").first
            report_link = first_article_link.get_attribute("href")

        if not report_link:
            raise Exception("Could not find any fleet report link on the page.")

        print(f"Tracking report: {report_link}")

        # 3. Navigate to the article
        page.goto(report_link, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)

        # 4. Extract and clean title
        raw_title = page.title().split(" - ")[0].strip()
        clean_title = raw_title.replace("USNI News Fleet and Marine Tracker:", "").strip()

        # 5. Capture the armada map
        output_image = "armada_map.png"
        map_captured = False

        # Attempt to find the specific fleet infographic image
        candidate_images = page.locator("img").all()
        for img in candidate_images:
            src = img.get_attribute("src") or ""
            if "uploads" in src and not any(x in src.lower() for x in ["logo", "avatar", "icon", "banner"]):
                try:
                    img.screenshot(path=output_image, timeout=5000)
                    map_captured = True
                    print(f"Captured fleet map element: {src}")
                    break
                except Exception:
                    continue

        if not map_captured:
            # Fallback to high-res page screenshot
            print("Taking viewport screenshot...")
            page.screenshot(path=output_image)

        # 6. Extract ship and strike group movements
        body_text = page.inner_text("body")
        deployments = []
        for line in body_text.splitlines():
            line = line.strip()
            if any(k in line.lower() for k in ["carrier strike group", "amphibious ready group", "uss "]):
                if 12 < len(line) < 140 and line not in deployments:
                    deployments.append(f"🔹 <i>{html.escape(line)}</i>")

        browser.close()

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

    img, caption = run_tracker()

    print("Sending live update to Telegram...")
    send_telegram_alert(img, caption)
    print("Update successfully delivered!")
