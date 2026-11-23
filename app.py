\
from flask import Flask, request, jsonify, render_template
import requests, re, io, time
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urldefrag
from PIL import Image
import pytesseract

app = Flask(__name__)
UA = "CouponImageScanner/1.0"
MAX_PAGES_DEFAULT = 15
MAX_IMAGES_DEFAULT = 60

def valid_url(value):
    value=(value or "").strip()
    if not value: raise ValueError("آدرس سایت را وارد کنید.")
    if not urlparse(value).scheme: value="https://"+value
    p=urlparse(value)
    if p.scheme not in ("http","https") or not p.hostname:
        raise ValueError("آدرس باید با http یا https باشد.")
    # Prevent requests to localhost / private IPs (basic SSRF protection)
    host=p.hostname.lower()
    if host in ("localhost","127.0.0.1","0.0.0.0") or host.endswith(".local"):
        raise ValueError("آدرس محلی مجاز نیست.")
    return value

def same_host(root, candidate):
    return urlparse(root).netloc.lower()==urlparse(candidate).netloc.lower()

def get_img_urls(soup, page, root, limit):
    out=[]
    for tag in soup.find_all(["img","source"]):
        src=tag.get("src") or tag.get("data-src") or tag.get("data-lazy-src")
        if not src:
            srcset=tag.get("srcset") or tag.get("data-srcset")
            if srcset:
                src=srcset.split(",")[0].strip().split()[0]
        if src:
            u=urljoin(page,src)
            if same_host(root,u) and u not in out:
                out.append(u)
        if len(out)>=limit: break
    return out

def text_ocr(content):
    try:
        im=Image.open(io.BytesIO(content))
        im.thumbnail((2400,2400))
        if im.mode not in ("RGB","L"): im=im.convert("RGB")
        # English installed in container; Persian OCR language can be added if available.
        return pytesseract.image_to_string(im, lang="eng")
    except Exception:
        return ""

def extract_candidates(text):
    # Broadly find letter+digit codes and longer numeric strings.
    t=text.upper()
    patterns=[
        (r"(?<![A-Z0-9])[A-Z]{1,6}[0-9]{2,12}(?![A-Z0-9])","حروف و عدد"),
        (r"(?<![A-Z0-9])[A-Z0-9]{5,16}(?![A-Z0-9])","ترکیبی"),
        (r"(?<!\d)\d{6,12}(?!\d)","عدد"),
    ]
    ignored={"DIGIKALA","BLACKFRIDAY","DISCOUNT","COUPON","OFFICIAL","SALE","TOMAN"}
    candidates=[]
    for pattern,label in patterns:
        for m in re.finditer(pattern,t):
            token=m.group(0)
            if token in ignored: continue
            if token.isdigit() and (len(token)<6 or len(set(token))==1): continue
            if token not in [x[0] for x in candidates]:
                candidates.append((token,label))
    return candidates

@app.get("/")
def home():
    return render_template("index.html")

@app.post("/scan")
def scan():
    payload=request.get_json(silent=True) or {}
    try: root=valid_url(payload.get("url"))
    except ValueError as e: return jsonify(error=str(e)),400
    pages_limit=max(1,min(25,int(payload.get("pages",10))))
    images_limit=max(1,min(100,int(payload.get("images",40))))
    session=requests.Session()
    session.headers.update({"User-Agent":UA})
    queue=[root]; seen_pages=set(); page_images=[]; pages_count=0
    while queue and pages_count<pages_limit and len(page_images)<images_limit:
        page=urldefrag(queue.pop(0))[0]
        if page in seen_pages or not same_host(root,page): continue
        seen_pages.add(page)
        try:
            r=session.get(page,timeout=10,allow_redirects=True)
            if r.status_code!=200 or "text/html" not in r.headers.get("content-type",""): continue
            # Validate redirect destination remains on host
            if not same_host(root,r.url): continue
            soup=BeautifulSoup(r.text,"html.parser"); pages_count+=1
            for image_url in get_img_urls(soup,r.url,root,images_limit):
                if image_url not in [x[1] for x in page_images]:
                    page_images.append((r.url,image_url))
                    if len(page_images)>=images_limit: break
            if pages_count<pages_limit:
                for a in soup.find_all("a",href=True):
                    target=urldefrag(urljoin(r.url,a["href"]))[0]
                    if same_host(root,target) and target not in seen_pages and target not in queue:
                        if not re.search(r"\.(jpg|jpeg|png|gif|webp|svg|pdf|zip|mp4)(?:$|\?)",target.lower()):
                            queue.append(target)
            time.sleep(.15)
        except requests.RequestException: pass

    results=[]; images_done=0; seen_codes=set()
    for source,image_url in page_images:
        try:
            ir=session.get(image_url,timeout=10)
            if ir.status_code!=200 or not ir.headers.get("content-type","").startswith("image/"): continue
            if len(ir.content)>8_000_000: continue
            recognized=text_ocr(ir.content); images_done+=1
            for code,kind in extract_candidates(recognized):
                if code in seen_codes: continue
                seen_codes.add(code)
                results.append({"code":code,"kind":kind,"page":source,"image":image_url,"ocr":recognized.strip()[:350]})
            time.sleep(.1)
        except requests.RequestException: continue
    return jsonify(pages=pages_count,images=images_done,results=results)
