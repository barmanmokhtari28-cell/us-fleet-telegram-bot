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
    if os.environ.get("FORCE_POST") == "true":
        print("FORCE_POST enabled: ignoring last_posted.txt")
        return False
    return os.path.exists(LAST_POSTED_FILE) and open(LAST_POSTED_FILE).read().strip() == url


def save_last_posted(url):
    with open(LAST_POSTED_FILE, "w") as f:
        f.write(url)


# ---------------------------------------------------------------- data sources
def meta(soup, prop):
    tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
    return tag["content"].strip() if tag and tag.get("content") else None


STOP_PREFIXES = ("In addition to these major formations", "Share to")


def clean_text(el):
    for br in el.find_all("br"):
        br.replace_with(" ")
    return re.sub(r"\s+", " ", el.get_text("")).strip()


def is_caption(el, text):
    """Photo captions / the italic intro line are not part of the report."""
    if text.endswith(("Navy photo", "USNI News graphic", "Navy photo.")):
        return True
    em = el.find(["em", "i"])
    return bool(em and re.sub(r"\s+", " ", em.get_text("")).strip() == text)


def blocks_from(elements):
    """Turns HTML elements into [(kind, text)] where kind is h2/h3/p/li/stats."""
    blocks, seen_table = [], False
    for el in elements:
        if el.name == "table":
            if not seen_table:
                rows = el.find_all("tr")
                if len(rows) >= 2:
                    cells = [clean_text(c) for c in rows[1].find_all(["td", "th"])]
                    blocks.append(("stats", cells))
                    seen_table = True
            continue
        text = clean_text(el)
        if not text:
            continue
        if text.startswith(STOP_PREFIXES):
            break
        if el.name in ("p", "li") and is_caption(el, text):
            continue
        blocks.append((el.name, text))
    return blocks


TAGS = ["h2", "h3", "p", "li", "table"]


def source_article_page(url, **_):
    soup = BeautifulSoup(fetch(url).text, "html.parser")
    h1 = soup.find("h1")
    title = meta(soup, "og:title") or (h1.get_text(strip=True) if h1 else "")
    return title, meta(soup, "og:image"), blocks_from((h1 or soup).find_all_next(TAGS))


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
    return title, img, blocks_from(BeautifulSoup(content, "html.parser").find_all(TAGS))


def source_rss(url, **_):
    xml = BeautifulSoup(fetch(f"{SOURCE_URL}/feed").text, "xml")
    for item in xml.find_all("item"):
        link = item.find("link")
        if link and link.get_text(strip=True).rstrip("/") == url.rstrip("/"):
            content = item.find("content:encoded") or item.find("description")
            frag = content.get_text() if content else ""
            m = re.search(r'<img[^>]+src="([^"]+)"', frag)
            blocks = blocks_from(BeautifulSoup(frag, "html.parser").find_all(TAGS))
            return item.find("title").get_text(strip=True), (m.group(1) if m else None), blocks
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
                blocks = [("p", clean_text(p)) for p in node.find_all("p")]
                return title, src, blocks
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


# ---------------------------------------------------------------- formatting
REGION_FA = {
    "japan": ("🇯🇵", "ژاپن"),
    "eastern pacific": ("🌅", "شرق اقیانوس آرام"),
    "western pacific": ("🌏", "غرب اقیانوس آرام"),
    "pacific": ("🌊", "اقیانوس آرام"),
    "caribbean": ("🏝", "دریای کارائیب"),
    "mediterranean": ("🏛", "دریای مدیترانه"),
    "red sea": ("🔴", "دریای سرخ"),
    "arabian sea": ("🛢", "دریای عرب"),
    "persian gulf": ("⛽", "خلیج فارس"),
    "gulf of oman": ("⚓", "خلیج عمان"),
    "indian ocean": ("🌴", "اقیانوس هند"),
    "atlantic": ("🌐", "اقیانوس اطلس"),
    "south china sea": ("🧭", "دریای چین جنوبی"),
    "philippine sea": ("🧭", "دریای فیلیپین"),
    "east china sea": ("🧭", "دریای چین شرقی"),
    "korea": ("🇰🇷", "کره"),
    "europe": ("🇪🇺", "اروپا"),
    "baltic": ("🧊", "دریای بالتیک"),
    "black sea": ("🌊", "دریای سیاه"),
}
SQUADRON_RE = re.compile(r"\b(VFA|VAQ|VAW|VRM|VMFA|HSM|HSC|HM|VAQ|Squadron \(|Air Wing)\b|Squadron \(")
E = html.escape


def region_style(title):
    low = title.lower()
    for key, (emoji, fa) in REGION_FA.items():
        if key in low:
            return emoji, fa
    return "📍", None


def parse_report(blocks):
    stats, sections, cur = None, [], None
    for kind, text in blocks:
        if kind == "stats":
            stats = text
        elif kind == "h2":
            cur = {"title": re.sub(r"^In (the )?", "", text).strip(), "items": []}
            sections.append(cur)
        else:
            if cur is None:
                cur = {"title": "", "items": []}
                sections.append(cur)
            if kind == "h3":
                cur["items"].append(("sub", text))
            elif kind == "li":
                if SQUADRON_RE.search(text):
                    continue  # skip air-wing squadron lists, keep ship lists
                cur["items"].append(("li", re.sub(r",? homeported.*$", "", text).rstrip(".")))
            elif kind == "p":
                if len(text) < 30:
                    continue  # headings like "Carrier Air Wing 9", "Air Defense Commander"
                cur["items"].append(("p", text))
    return stats, [s for s in sections if s["items"]]


def render_section(sec):
    emoji, fa = region_style(sec["title"])
    head = f"{emoji} <b>{E(sec['title'])}</b>" if sec["title"] else "📍 <b>Overview</b>"
    if fa:
        head = f"{emoji} <b>{fa}</b> · <i>{E(sec['title'])}</i>"
    parts, prev = [], None
    for kind, text in sec["items"]:
        if kind == "sub":
            parts.append(f"\n🔸 <b>{E(text)}</b>")
        elif kind == "li":
            parts.append(("" if prev == "li" else "") + f"▫️ {E(text)}")
        else:
            parts.append(("\n" if prev not in (None, "sub") else "") + E(text))
        prev = kind
    # keep list items on consecutive lines, paragraphs separated by blank lines
    body = "\n".join(parts).strip()
    open_tag = "<blockquote expandable>" if len(body) > 700 else "<blockquote>"
    return f"{head}\n{open_tag}{body}</blockquote>"


def stat_numbers(stats):
    nums = []
    for c in stats or []:
        m = re.match(r"\s*(\d+)", c)
        nums.append(m.group(1) if m else c)
    return nums


def build_caption(title, stats):
    clean_title = title.split(" - ")[0].replace("USNI News Fleet and Marine Tracker:", "").strip()
    lines = [
        "🧭 <b>آخرین موقعیت ناوگان و ناوهای جنگی آمریکا</b>",
        f"<blockquote><b>📅 گزارش USNI:</b> {E(clean_title)}</blockquote>",
    ]
    n = stat_numbers(stats)
    if len(n) >= 3:
        lines += [
            "",
            f"⚓️ کل ناوگان: <b>{n[0]}</b>  |  🌐 مستقر: <b>{n[1]}</b>  |  🌊 در حال حرکت: <b>{n[2]}</b>",
        ]
    lines += ["", "👇 جزئیات کامل موقعیت‌ها در پیام بعدی", "", "📫 @secretollah", "#USNI #ناو"]
    return "\n".join(lines)


def build_messages(sections, clean_title):
    """Splits the rendered sections into Telegram messages under the 4096-char limit."""
    rendered = [render_section(s) for s in sections]
    messages, cur = [], ""
    for r in rendered:
        if cur and len(cur) + len(r) + 2 > 3800:
            messages.append(cur)
            cur = ""
        cur = f"{cur}\n\n{r}" if cur else r
    if cur:
        messages.append(cur)
    if messages:
        messages[0] = f"📋 <b>گزارش کامل ناوگان</b> — <i>{E(clean_title)}</i>\n\n" + messages[0]
        messages[-1] += "\n\n📫 @secretollah\n#USNI #ناو"
    return messages


# ---------------------------------------------------------------- telegram
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


def tg(method, **kw):
    res = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}", timeout=60, **kw)
    if not res.ok:
        raise Exception(f"Telegram {method} error {res.status_code}: {res.text}")


def send_photo(img_url, local_path, caption):
    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
    if local_path:
        with open(local_path, "rb") as f:
            tg("sendPhoto", data=data, files={"photo": f})
    else:
        data["photo"] = img_url  # Telegram fetches it itself, bypassing the runner's IP block
        tg("sendPhoto", data=data)


def send_text(text):
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": "true"}
    try:
        tg("sendMessage", data=data)
    except Exception as e:
        if "can't parse entities" not in str(e):
            raise
        print("  HTML parse error, resending as plain text")
        data["text"] = html.unescape(re.sub(r"</?[a-z][^>]*>", "", text))
        del data["parse_mode"]
        tg("sendMessage", data=data)


if __name__ == "__main__":
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("Telegram Bot Token or Chat ID is missing!")

    latest_url, title, cat_soup = get_latest_article()
    print(f"Checking report: {latest_url}")

    if has_already_been_posted(latest_url):
        print("This deployment report has already been posted. Skipping.")
    else:
        print("New armada deployment found!")
        import time
        full_title, img_url, blocks = get_report(latest_url, title, cat_soup)
        stats, sections = parse_report(blocks)
        clean_title = full_title.split(" - ")[0].replace("USNI News Fleet and Marine Tracker:", "").strip()
        caption = build_caption(full_title, stats)
        messages = build_messages(sections, clean_title)
        print(f"Parsed {len(sections)} sections -> {len(messages)} message(s)")

        local = download_image(img_url)
        print("Sending to Telegram...")
        send_photo(img_url, local, caption)
        for m in messages:
            time.sleep(1)
            send_text(m)
        save_last_posted(latest_url)
        print("Done!")
