import html
import os
import re
import requests
from urllib.parse import urlparse
from bs4 import BeautifulSoup

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
BASE = "https://news.usni.org"
SOURCE_URL = f"{BASE}/category/fleet-tracker"
LAST_POSTED_FILE = "last_posted.txt"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": SOURCE_URL,
})


def fetch(url, **kw):
    res = session.get(url, timeout=30, **kw)
    if not res.ok:
        print(f"  ! {res.status_code} for {url} (server={res.headers.get('server')}, "
              f"cf-mitigated={res.headers.get('cf-mitigated')})")
    res.raise_for_status()
    return res


# ---------------------------------------------------------------- latest report
def get_latest_article():
    soup = BeautifulSoup(fetch(SOURCE_URL).text, "html.parser")
    for a in soup.find_all("a", href=True):
        if re.search(r"/\d{4}/\d{2}/\d{2}/usni-news-fleet-and-marine-tracker", a["href"]):
            return a["href"], a.get_text(strip=True), soup
    raise Exception("Could not find any fleet reports on the category page.")


def has_already_been_posted(url):
    return os.path.exists(LAST_POSTED_FILE) and open(LAST_POSTED_FILE).read().strip() == url


def save_last_posted(url):
    with open(LAST_POSTED_FILE, "w") as f:
        f.write(url)


# ---------------------------------------------------------------- data sources
def meta(soup, prop):
    tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
    return tag["content"].strip() if tag and tag.get("content") else None


def paragraphs_from_fragment(fragment_html):
    frag = BeautifulSoup(fragment_html, "html.parser")
    out = []
    for p in frag.find_all("p"):
        t = p.get_text(" ", strip=True)
        if t.startswith("In addition to these major formations"):
            break
        out.append(t)
    return out


def source_article_page(url, **_):
    soup = BeautifulSoup(fetch(url).text, "html.parser")
    h1 = soup.find("h1")
    paras = []
    for el in (h1 or soup).find_all_next("p"):
        t = el.get_text(" ", strip=True)
        if t.startswith("In addition to these major formations"):
            break
        paras.append(t)
    img = meta(soup, "og:image")
    title = meta(soup, "og:title") or (h1.get_text(strip=True) if h1 else "")
    return title, img, paras


def source_rest_api(url, **_):
    slug = urlparse(url).path.strip("/").split("/")[-1]
    data = fetch(f"{BASE}/wp-json/wp/v2/posts", params={"slug": slug, "_embed": "1"}).json()
    if not data:
        raise Exception("slug not found in REST API")
    post = data[0]
    title = BeautifulSoup(post["title"]["rendered"], "html.parser").get_text()
    content = post["content"]["rendered"]
    img = None
    media = (post.get("_embedded") or {}).get("wp:featuredmedia") or []
    if media and media[0].get("source_url"):
        img = media[0]["source_url"]
    if not img:
        m = re.search(r'<img[^>]+src="([^"]+)"', content)
        img = m.group(1) if m else None
    return title, img, paragraphs_from_fragment(content)


def source_rss(url, **_):
    xml = BeautifulSoup(fetch(f"{SOURCE_URL}/feed").text, "xml")
    for item in xml.find_all("item"):
        link = item.find("link")
        if link and link.get_text(strip=True).rstrip("/") == url.rstrip("/"):
            content = item.find("content:encoded") or item.find("description")
            frag = content.get_text() if content else ""
            m = re.search(r'<img[^>]+src="([^"]+)"', frag)
            return item.find("title").get_text(strip=True), (m.group(1) if m else None), paragraphs_from_fragment(frag)
    raise Exception("report not in RSS feed")


def source_category_page(url, title="", category_soup=None, **_):
    """Last resort: the category page itself (it loads fine from GitHub runners)."""
    soup = category_soup or BeautifulSoup(fetch(SOURCE_URL).text, "html.parser")
    for a in soup.find_all("a", href=url):
        node = a
        for _ in range(6):
            node = node.parent
            if node is None:
                break
            img = node.find("img")
            if img:
                src = img.get("data-src") or img.get("src") or ""
                if not src and img.get("srcset"):
                    src = img["srcset"].split()[0]
                src = re.sub(r"-\d+x\d+(\.\w+)$", r"\1", src)  # full-size version
                paras = [p.get_text(" ", strip=True) for p in node.find_all("p")]
                return title, src, paras
    raise Exception("report not found on category page")


SOURCES = [source_article_page, source_rest_api, source_rss, source_category_page]


def get_report(url, title, category_soup):
    for fn in SOURCES:
        try:
            print(f"Trying {fn.__name__} ...")
            t, img, paras = fn(url, title=title, category_soup=category_soup)
            if img:
                print(f"  OK via {fn.__name__}")
                return t or title, img, paras
            print("  no image found, trying next")
        except Exception as e:
            print(f"  failed: {e}")
    raise Exception("All sources failed.")


# ---------------------------------------------------------------- caption / image
def first_sentence(text, limit=130):
    text = re.sub(r"\s+", " ", text).strip()
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    s = m.group(1) if m else text
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def build_caption(title, paras):
    clean_title = title.split(" - ")[0].replace("USNI News Fleet and Marine Tracker:", "").strip()
    keys = ["carrier strike group", "amphibious ready group", "aircraft carrier", "uss "]
    priority, others = [], []
    for text in paras:
        low = text.lower()
        if len(text) < 25 or not any(k in low for k in keys):
            continue
        item = f"🔹 <i>{html.escape(first_sentence(text))}</i>"
        if item in priority or item in others:
            continue
        (priority if ("carrier" in low or "strike group" in low or "ready group" in low) else others).append(item)
    lines = (priority + others)[:4]

    def make(lines):
        summary = "\n".join(lines) if lines else "🔹 <i>اطلاعات تکمیلی در گزارش USNI منتشر شد.</i>"
        return (
            f"🧭 <b>آخرین موقعیت ناوگان و ناوهای جنگی آمریکا</b>\n"
            f"<blockquote><b>گزارش:</b> {html.escape(clean_title)}</blockquote>\n\n"
            f"📍 <b>موقعیت ناوهای هواپیمابر و گروه‌های رزمی:</b>\n{summary}\n\n"
            f"📫 @secretollah\n#USNI\n#ناو"
        )

    caption = make(lines)
    while len(caption) > 1024 and lines:  # Telegram caption limit
        lines.pop()
        caption = make(lines)
    return caption


def download_image(img_url, path="armada_map.jpg"):
    try:
        res = session.get(img_url, timeout=30, headers={"Accept": "image/*,*/*;q=0.8"})
        res.raise_for_status()
        with open(path, "wb") as f:
            f.write(res.content)
        return path
    except Exception as e:
        print(f"  image download failed ({e}); will let Telegram fetch it by URL")
        return None


def send_telegram_alert(img_url, local_path, caption):
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
    if local_path:
        with open(local_path, "rb") as f:
            res = requests.post(api, data=data, files={"photo": f}, timeout=60)
    else:
        data["photo"] = img_url  # Telegram's servers download it, bypassing the runner's IP block
        res = requests.post(api, data=data, timeout=60)
    if not res.ok:
        raise Exception(f"Telegram error {res.status_code}: {res.text}")


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("Telegram Bot Token or Chat ID is missing!")

    latest_url, title, cat_soup = get_latest_article()
    print(f"Checking report: {latest_url}")

    if has_already_been_posted(latest_url):
        print("This deployment report has already been posted. Skipping.")
    else:
        print("New armada deployment found!")
        full_title, img_url, paras = get_report(latest_url, title, cat_soup)
        caption = build_caption(full_title, paras)
        local = download_image(img_url)
        print("Sending to Telegram...")
        send_telegram_alert(img_url, local, caption)
        save_last_posted(latest_url)
        print("Done!")
