import html
import os
import re
import requests
from bs4 import BeautifulSoup

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SOURCE_URL = "https://news.usni.org/category/fleet-tracker"
LAST_POSTED_FILE = "last_posted.txt"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


def fetch(url, **kw):
    res = requests.get(url, headers=HEADERS, timeout=30, **kw)
    res.raise_for_status()
    return res


def get_latest_article():
    """Returns the URL of the newest Fleet and Marine Tracker post."""
    soup = BeautifulSoup(fetch(SOURCE_URL).text, "html.parser")

    # Most robust: first link that looks like a tracker report (page is newest-first)
    for a in soup.find_all("a", href=True):
        if re.search(r"/\d{4}/\d{2}/\d{2}/usni-news-fleet-and-marine-tracker", a["href"]):
            return a["href"], a.get_text(strip=True)

    # Fallback to the old logic
    article = soup.find("article") or soup.find("div", class_="post")
    if not article:
        raise Exception("Could not find any fleet reports.")
    link_tag = article.find("a", href=True)
    return link_tag["href"], link_tag.get_text(strip=True)


def has_already_been_posted(article_url):
    if os.path.exists(LAST_POSTED_FILE):
        with open(LAST_POSTED_FILE, "r") as f:
            return f.read().strip() == article_url
    return False


def save_last_posted(article_url):
    with open(LAST_POSTED_FILE, "w") as f:
        f.write(article_url)


def meta(soup, prop):
    tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
    return tag["content"].strip() if tag and tag.get("content") else None


def first_sentence(text, limit=130):
    text = re.sub(r"\s+", " ", text).strip()
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    s = m.group(1) if m else text
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def capture_fleet_data(article_url, output_image="armada_map.jpg"):
    """Downloads the tracker map image and extracts key deployment lines (no browser needed)."""
    soup = BeautifulSoup(fetch(article_url).text, "html.parser")

    # --- Title ---
    h1 = soup.find("h1")
    raw_title = (meta(soup, "og:title") or (h1.get_text(strip=True) if h1 else "")
                 or (soup.title.get_text() if soup.title else "")).split(" - ")[0].strip()
    clean_title = raw_title.replace("USNI News Fleet and Marine Tracker:", "").strip()

    # --- Map image: og:image is the tracker graphic (FT_x_xx_xx.jpg) ---
    img_url = meta(soup, "og:image")
    if not img_url:
        for img in soup.find_all("img", src=True):
            if "/wp-content/uploads/" in img["src"] and "FT_" in img["src"]:
                img_url = img["src"]
                break
    if not img_url:
        raise Exception("Could not find the fleet map image on the page.")
    with open(output_image, "wb") as f:
        f.write(fetch(img_url).content)

    # --- Deployment text: everything between the title and the closing disclaimer ---
    lines = []
    start = h1 or soup
    for el in start.find_all_next(["p", "li", "h2", "h3"]):
        text = el.get_text(" ", strip=True)
        if text.startswith("In addition to these major formations") or text.startswith("Share to"):
            break
        lines.append((el.name, text))

    keys = ["carrier strike group", "amphibious ready group", "aircraft carrier", "uss "]
    priority, others = [], []
    for name, text in lines:
        low = text.lower()
        if name != "p" or not any(k in low for k in keys) or len(text) < 25:
            continue
        s = first_sentence(text)
        item = f"🔹 <i>{html.escape(s)}</i>"
        if item in priority or item in others:
            continue
        if "carrier" in low or "strike group" in low or "ready group" in low:
            priority.append(item)
        else:
            others.append(item)
    deployments = (priority + others)[:4]

    summary_text = "\n".join(deployments) if deployments else "🔹 <i>اطلاعات تکمیلی در گزارش USNI منتشر شد.</i>"

    caption = (
        f"🧭 <b>آخرین موقعیت ناوگان و ناوهای جنگی آمریکا</b>\n"
        f"<blockquote><b>گزارش:</b> {html.escape(clean_title)}</blockquote>\n\n"
        f"📍 <b>موقعیت ناوهای هواپیمابر و گروه‌های رزمی:</b>\n"
        f"{summary_text}\n\n"
        f"📫 @secretollah\n"
        f"#USNI\n"
        f"#ناو"
    )
    # Telegram photo captions are capped at 1024 characters
    while len(caption) > 1024 and deployments:
        deployments.pop()
        summary_text = "\n".join(deployments)
        caption = (
            f"🧭 <b>آخرین موقعیت ناوگان و ناوهای جنگی آمریکا</b>\n"
            f"<blockquote><b>گزارش:</b> {html.escape(clean_title)}</blockquote>\n\n"
            f"📍 <b>موقعیت ناوهای هواپیمابر و گروه‌های رزمی:</b>\n"
            f"{summary_text}\n\n📫 @secretollah\n#USNI\n#ناو"
        )

    return output_image, caption


def send_telegram_alert(image_path, caption):
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    with open(image_path, "rb") as img:
        res = requests.post(
            api_url,
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
            files={"photo": img},
            timeout=60,
        )
    if not res.ok:
        raise Exception(f"Telegram error {res.status_code}: {res.text}")


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("Telegram Bot Token or Chat ID is missing!")

    latest_url, title = get_latest_article()
    print(f"Checking report: {latest_url}")

    if has_already_been_posted(latest_url):
        print("This deployment report has already been posted. Skipping.")
    else:
        print("New armada deployment found! Fetching map...")
        img, caption = capture_fleet_data(latest_url)

        print("Sending to Telegram...")
        send_telegram_alert(img, caption)

        save_last_posted(latest_url)
        print("Done!")
