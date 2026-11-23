
from flask import Flask, request, jsonify, render_template
import requests
import re
import io
import time
import socket
import ipaddress
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urldefrag
from PIL import Image
import pytesseract

app = Flask(__name__)

UA = "Mozilla/5.0 (compatible; CouponImageScanner/2.0)"
MAX_PAGES_DEFAULT = 25
MAX_IMAGES_DEFAULT = 100
MAX_IMAGE_SIZE = 8_000_000

def safe_url(url):
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False

        host = p.hostname.lower()

        if host in ("localhost", "metadata.google.internal"):
            return False

        addresses = socket.getaddrinfo(host, None)
        for item in addresses:
            ip = ipaddress.ip_address(item[4][0])
            if not ip.is_global:
                return False

        return True
    except Exception:
        return False

def valid_url(value):
    value = (value or "").strip()
    if not value:
        raise ValueError("آدرس سایت را وارد کنید.")

    if not urlparse(value).scheme:
        value = "https://" + value

    if not safe_url(value):
        raise ValueError("آدرس واردشده معتبر یا مجاز نیست.")

    return value

def same_host(root, candidate):
    a = urlparse(root).hostname or ""
    b = urlparse(candidate).hostname or ""
    return a.lower() == b.lower()

def add_url(out, url, base, limit, image=False):
    if not url or len(out) >= limit:
        return

    url = url.strip().strip('"').strip("'")
    if url.startswith("data:"):
        return

    full = urljoin(base, url)
    full = urldefrag(full)[0]

    if full not in out and safe_url(full):
        if not image or re.search(
            r"\.(jpg|jpeg|png|webp|gif|avif)(?:$|\?)",
            full.lower()
        ) or image:
            out.append(full)

def get_img_urls(soup, page, limit):
    out = []

    for tag in soup.find_all(["img", "source"]):
        for attr in (
            "src", "data-src", "data-lazy-src",
            "data-original", "data-image", "data-zoom-image"
        ):
            add_url(out, tag.get(attr), page, limit, True)

        for attr in ("srcset", "data-srcset"):
            srcset = tag.get(attr)
            if srcset:
                for item in srcset.split(","):
                    parts = item.strip().split()
                    if parts:
                        add_url(out, parts[0], page, limit, True)

        if len(out) >= limit:
            break

    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or "").lower()
        if key in ("og:image", "twitter:image"):
            add_url(out, tag.get("content"), page, limit, True)

    for tag in soup.find_all(style=True):
        styles = tag.get("style", "")
        for match in re.findall(
            r"url\(['\"]?(.*?)['\"]?\)",
            styles,
            re.I
        ):
            add_url(out, match, page, limit, True)

    for tag in soup.find_all("link", href=True):
        if tag.get("rel") and "image_src" in tag.get("rel"):
            add_url(out, tag.get("href"), page, limit, True)

    return out[:limit]

def text_ocr(content):
    try:
        im = Image.open(io.BytesIO(content))
        im.thumbnail((2400, 2400))

        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")

        return pytesseract.image_to_string(
            im,
            lang="fas+eng",
            config="--psm 11"
        )
    except Exception:
        return ""

def extract_candidates(text):
    t = text.upper()

    patterns = [
        (r"(?<![A-Z0-9])[A-Z]{1,8}[0-9]{2,12}(?![A-Z0-9])", "حروف و عدد"),
        (r"(?<![A-Z0-9])[A-Z0-9]{5,16}(?![A-Z0-9])", "ترکیبی"),
        (r"(?<!\d)\d{6,12}(?!\d)", "عدد"),
    ]

    ignored = {
        "DIGIKALA", "BLACKFRIDAY", "DISCOUNT",
        "COUPON", "OFFICIAL", "SALE", "TOMAN"
    }

    candidates = []

    for pattern, label in patterns:
        for match in re.finditer(pattern, t):
            token = match.group(0)

            if token in ignored:
                continue

            if token.isdigit() and (
                len(token) < 6 or len(set(token)) == 1
            ):
                continue

            if token not in [x[0] for x in candidates]:
                candidates.append((token, label))

    return candidates

@app.get("/")
def home():
    return render_template("index.html")

@app.post("/scan")
def scan():
    payload = request.get_json(silent=True) or {}

    try:
        root = valid_url(payload.get("url"))
    except ValueError as e:
        return jsonify(error=str(e)), 400

    pages_limit = max(
        1, min(25, int(payload.get("pages", MAX_PAGES_DEFAULT)))
    )
    images_limit = max(
        1, min(100, int(payload.get("images", MAX_IMAGES_DEFAULT)))
    )

    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    queue = [root]
    seen_pages = set()
    page_images = []
    pages_count = 0

    while queue and pages_count < pages_limit:
        page = urldefrag(queue.pop(0))[0]

        if page in seen_pages or not same_host(root, page):
            continue

        seen_pages.add(page)

        try:
            if not safe_url(page):
                continue

            response = session.get(
                page,
                timeout=15,
                allow_redirects=True
            )

            if response.status_code != 200:
                continue

            if not same_host(root, response.url):
                continue

            if "text/html" not in response.headers.get(
                "content-type", ""
            ):
                continue

            soup = BeautifulSoup(response.text, "html.parser")
            pages_count += 1

            for image_url in get_img_urls(
                soup, response.url, images_limit
            ):
                if image_url not in [x[1] for x in page_images]:
                    page_images.append((response.url, image_url))

                    if len(page_images) >= images_limit:
                        break

            if pages_count < pages_limit:
                for a in soup.find_all("a", href=True):
                    target = urldefrag(
                        urljoin(response.url, a["href"])
                    )[0]

                    if (
                        same_host(root, target)
                        and target not in seen_pages
                        and target not in queue
                        and safe_url(target)
                    ):
                        if not re.search(
                            r"\.(jpg|jpeg|png|gif|webp|avif|svg|pdf|zip|mp4)(?:$|\?)",
                            target.lower()
                        ):
                            queue.append(target)

            time.sleep(0.15)

        except requests.RequestException:
            continue

        if len(page_images) >= images_limit:
            break

    results = []
    images_done = 0
    seen_codes = set()

    for source, image_url in page_images:
        try:
            if not safe_url(image_url):
                continue

            ir = session.get(
                image_url,
                timeout=15,
                headers={"Referer": source}
            )

            if ir.status_code != 200:
                continue

            if not ir.headers.get(
                "content-type", ""
            ).lower().startswith("image/"):
                continue

            if len(ir.content) > MAX_IMAGE_SIZE:
                continue

            recognized = text_ocr(ir.content)
            images_done += 1

            for code, kind in extract_candidates(recognized):
                if code in seen_codes:
                    continue

                seen_codes.add(code)

                results.append({
                    "code": code,
                    "kind": kind,
                    "page": source,
                    "image": image_url,
                    "ocr": recognized.strip()[:350]
                })

            time.sleep(0.1)

        except requests.RequestException:
            continue

    return jsonify(
        pages=pages_count,
        images=images_done,
        results=results
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
