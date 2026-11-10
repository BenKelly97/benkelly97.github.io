"""Build bkkelly.com/blog from the Medium feed.

Usage: python3 .github/blog/build_blog.py FEED_XML [--no-images]

Run from the repository root (the daily "Blog update" workflow does this).

- Reads the Medium RSS feed (medium.com/feed/@bkellyohio). Medium lists only its
  latest posts there, so every post read is kept in .github/blog/posts.json and stays
  on the site after it leaves the feed. A post whose Medium "updated" time changes is
  read again, so edits made on Medium are copied over.
- Post HTML is reduced to a fixed list of tags and attributes; Medium's view-counter
  image is dropped; YouTube embeds become youtube-nocookie players and other embeds
  become a link to the post on Medium.
- Images are downloaded to blog/images/ so the site does not depend on Medium's image
  server. A failed download keeps the Medium image address and is retried next run.
- Every page is rebuilt from posts.json on every run: blog/index.html (the list) and
  blog/<slug>/index.html (one per post). Files are only rewritten when they change.
"""
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

DATA_FILE = ".github/blog/posts.json"
OUT_DIR = "blog"
IMG_DIR = "blog/images"
SITE = "https://bkkelly.com"
EASTERN = ZoneInfo("America/New_York")
MAX_IMAGE_BYTES = 10_000_000
NS = {"content": "http://purl.org/rss/1.0/modules/content/",
      "dc": "http://purl.org/dc/elements/1.1/",
      "atom": "http://www.w3.org/2005/Atom"}


# ---------------------------------------------------------------- reading the feed

def read_feed(path):
    """Posts in the feed, as dicts (newest first, as Medium lists them)."""
    channel = ET.parse(path).getroot().find("channel")
    posts = []
    for item in channel.findall("item"):
        guid = (item.findtext("guid") or "").strip()
        pid = guid.rstrip("/").rsplit("/", 1)[-1]
        link = (item.findtext("link") or "").strip()
        medium_url = link.split("?", 1)[0]
        posts.append({
            "id": pid,
            "title": (item.findtext("title") or "").strip(),
            "author": (item.findtext("dc:creator", namespaces=NS) or "").strip(),
            "published": email.utils.parsedate_to_datetime(item.findtext("pubDate")).astimezone(timezone.utc).isoformat(),
            "updated": (item.findtext("atom:updated", namespaces=NS) or "").strip(),
            "tags": [c.text.strip() for c in item.findall("category") if c.text],
            "medium_url": medium_url,
            "raw_html": item.findtext("content:encoded", namespaces=NS) or "",
        })
    return [p for p in posts if p["id"] and p["title"]]


def slug_from(post, taken):
    """URL name from the Medium address, without Medium's id suffix; unique on the site."""
    last = urllib.parse.urlparse(post["medium_url"]).path.rstrip("/").rsplit("/", 1)[-1]
    base = re.sub(r"-" + re.escape(post["id"]) + r"$", "", last)
    base = re.sub(r"[^a-z0-9-]+", "-", base.lower()).strip("-") or post["id"]
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


# ---------------------------------------------------------------- cleaning post HTML

KEEP = {"p", "br", "hr", "strong", "b", "em", "i", "a", "blockquote", "ul", "ol", "li",
        "figure", "figcaption", "img", "pre", "code", "sup", "sub", "h2", "h3"}
RENAME = {"h1": "h2", "h2": "h2", "h3": "h2", "h4": "h3", "h5": "h3", "h6": "h3"}
VOID = {"br", "hr", "img"}
DROP_CONTENT = {"script", "style", "iframe", "noscript", "svg", "object", "embed"}
YOUTUBE_ID = re.compile(r"(?:youtube\.com/(?:embed/|watch\?v=)|youtu\.be/)([A-Za-z0-9_-]{6,20})")


def safe_href(url):
    url = (url or "").strip()
    return url if re.match(r"(https?:|mailto:|#)", url, re.I) else ""


class Cleaner(HTMLParser):
    def __init__(self, medium_url):
        super().__init__(convert_charrefs=True)
        self.medium_url = medium_url
        self.out, self.stack, self.skip, self.images = [], [], 0, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.skip:
            if tag in DROP_CONTENT:
                self.skip += 1
            return
        if tag == "iframe":
            self.out.append(self.embed(a.get("src", "")))
        if tag in DROP_CONTENT:
            if tag not in VOID:
                self.skip = 1
            return
        tag = RENAME.get(tag, tag)
        if tag not in KEEP:
            return
        if tag == "img":
            src = (a.get("src") or "").strip()
            if not src.startswith("https://") or "/_/stat" in src:
                return
            self.images.append(src)
            self.out.append(f'<img src="{html.escape(src)}" alt="{html.escape(a.get("alt") or "")}" loading="lazy">')
            return
        if tag == "a":
            href = safe_href(a.get("href"))
            self.out.append(f'<a href="{html.escape(href)}">' if href else "<a>")
        else:
            self.out.append(f"<{tag}>")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and RENAME.get(tag, tag) in self.stack and not self.skip:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.skip:
            if tag in DROP_CONTENT:
                self.skip -= 1
            return
        tag = RENAME.get(tag, tag)
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def embed(self, src):
        m = YOUTUBE_ID.search(urllib.parse.unquote(src or ""))
        if m:
            return (f'<div class="embed"><iframe src="https://www.youtube-nocookie.com/embed/{m.group(1)}" '
                    'title="YouTube video" allow="encrypted-media; picture-in-picture" allowfullscreen '
                    'loading="lazy"></iframe></div>')
        return f'<p class="note"><a href="{html.escape(self.medium_url)}">Embedded content: view it on Medium</a></p>'

    def result(self):
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def clean_html(raw, medium_url):
    c = Cleaner(medium_url)
    c.feed(raw)
    c.close()
    body = re.sub(r"<p>\s*</p>", "", c.result())
    return body, c.images


def plain_text(fragment):
    text = re.sub(r"<figcaption>.*?</figcaption>", " ", fragment, flags=re.S)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text).strip()


def summary_of(body, limit=220):
    for p in re.findall(r"<p>(.*?)</p>", body, flags=re.S):
        text = plain_text(p)
        if len(text) >= 40:
            if len(text) <= limit:
                return text
            return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return ""


# ---------------------------------------------------------------- images

def download_image(url, pid, fetch=None):
    """Local path under blog/images for url, or None when the download fails."""
    if fetch is None:
        def fetch(u):
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (compatible; bkkelly.com blog updater)"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.headers.get("Content-Type", ""), r.read(MAX_IMAGE_BYTES + 1)
    try:
        ctype, data = fetch(url)
    except Exception as e:
        print(f"  image failed ({e.__class__.__name__}): {url}")
        return None
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}.get(ctype.split(";")[0].strip())
    if not ext or not data or len(data) > MAX_IMAGE_BYTES:
        print(f"  image skipped ({ctype}, {len(data or b'')} bytes): {url}")
        return None
    os.makedirs(IMG_DIR, exist_ok=True)
    path = f"{IMG_DIR}/{pid}-{hashlib.sha1(url.encode()).hexdigest()[:10]}.{ext}"
    with open(path, "wb") as f:
        f.write(data)
    return "/" + path


# ---------------------------------------------------------------- pages

STYLE = """*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#ffffff;--bg2:#f7f6f3;--text:#1a1a18;--text2:#5a5a55;--text3:#9a9a92;--border:#e0dfd8;--accent:#2255cc}
@media(prefers-color-scheme:dark){:root{--bg:#1c1c1a;--bg2:#242422;--text:#f0efe8;--text2:#a0a098;--text3:#6a6a64;--border:#3a3a36;--accent:#6b9bf0}}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;line-height:1.5;-webkit-font-smoothing:antialiased}
main{max-width:680px;margin:0 auto;padding:40px 16px 64px}
.crumbs{font-size:14px;color:var(--text3)}
.crumbs a{color:var(--text2);text-decoration:none}
.crumbs a:hover{color:var(--accent)}
h1{font-size:30px;line-height:1.2;font-weight:700;letter-spacing:-0.015em;margin-top:28px}
.meta{color:var(--text3);font-size:14px;margin-top:10px}
footer{margin-top:48px;padding-top:16px;border-top:1px solid var(--border);color:var(--text3);font-size:13px}
footer a{color:var(--text2)}
"""

ARTICLE_STYLE = """article{margin-top:28px;font-size:18px;line-height:1.65}
article p,article ul,article ol,article blockquote,article pre,article figure,article .embed{margin-top:1.1em}
article h2{font-size:23px;line-height:1.3;margin-top:1.6em}
article h3{font-size:19px;line-height:1.35;margin-top:1.4em}
article a{color:var(--accent);overflow-wrap:anywhere}
article ul,article ol{padding-left:1.4em}
article li+li{margin-top:.35em}
article blockquote{border-left:3px solid var(--border);padding-left:16px;color:var(--text2)}
article figure{margin-left:0;margin-right:0}
article img{display:block;max-width:100%;height:auto;margin:0 auto;border-radius:4px}
article figcaption{font-size:14px;color:var(--text3);text-align:center;margin-top:8px;line-height:1.45}
article pre{background:var(--bg2);padding:12px 14px;border-radius:6px;overflow-x:auto;font-size:14px}
article code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9em}
article hr{border:0;text-align:center;margin:2em 0}
article hr::after{content:"\\2022  \\2022  \\2022";color:var(--text3);letter-spacing:.4em}
article sup{line-height:0}
article .embed{position:relative;padding-top:56.25%}
article .embed iframe{position:absolute;inset:0;width:100%;height:100%;border:0;border-radius:4px}
.note{font-size:15px}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:28px}
.tags span{font-size:13px;padding:3px 10px;border-radius:12px;background:var(--bg2);border:1px solid var(--border);color:var(--text2)}
"""

LIST_STYLE = """.lede{color:var(--text2);margin-top:8px}
.lede a{color:var(--accent)}
.posts{list-style:none;margin-top:28px}
.posts li{padding:20px 0;border-top:1px solid var(--border)}
.posts li:last-child{border-bottom:1px solid var(--border)}
.posts a{text-decoration:none;color:inherit;display:block}
.posts a:hover h2{color:var(--accent)}
.posts h2{font-size:20px;line-height:1.3;font-weight:650}
.posts .date{font-size:13px;color:var(--text3);margin-top:4px}
.posts p{color:var(--text2);font-size:15px;margin-top:8px}
"""


def head(title, description, url, extra_style, image=None, canonical=None, kind="website"):
    e = html.escape
    tags = [
        '<!DOCTYPE html>', '<html lang="en">', '<head>', '<meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
        '<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">',
        '<meta name="theme-color" content="#1c1c1a" media="(prefers-color-scheme: dark)">',
        f"<title>{e(title)}</title>",
        f'<meta name="description" content="{e(description)}">',
        f'<link rel="canonical" href="{e(canonical or url)}">',
        f'<meta property="og:type" content="{kind}">',
        f'<meta property="og:title" content="{e(title)}">',
        f'<meta property="og:description" content="{e(description)}">',
        f'<meta property="og:url" content="{e(url)}">',
    ]
    if image:
        tags.append(f'<meta property="og:image" content="{e(image)}">')
    tags += [f'<link rel="alternate" type="application/rss+xml" title="Ben Kelly on Medium" href="https://medium.com/feed/@bkellyohio">',
             "<style>", STYLE + extra_style + "</style>", "</head>"]
    return "\n".join(tags)


def show_date(iso):
    d = datetime.fromisoformat(iso).astimezone(EASTERN)
    return f"{d:%B} {d.day}, {d.year}"


def reading_time(body):
    words = len(plain_text(body).split())
    return f"{max(1, round(words / 230))} min read"


def post_page(p):
    e = html.escape
    body = p["html"]
    for remote, local in p.get("images", {}).items():
        if local:
            body = body.replace(f'src="{e(remote)}"', f'src="{e(local)}"')
    urls = p.get("image_urls", [])
    first = urls[0] if urls else None
    local = p.get("images", {}).get(first) if first else None
    image = SITE + local if local else first
    url = f"{SITE}/blog/{p['slug']}/"
    tags = "".join(f"<span>{e(t.replace('-', ' '))}</span>" for t in p.get("tags", []))
    parts = [
        head(f"{p['title']} — Ben Kelly", p["summary"], url, ARTICLE_STYLE, image=image,
             canonical=p["medium_url"], kind="article"),
        "<body>", "<main>",
        '<nav class="crumbs"><a href="/">bkkelly.com</a> / <a href="/blog/">Blog</a></nav>',
        f"<h1>{e(p['title'])}</h1>",
        f'<p class="meta">{e(p.get("author") or "Ben Kelly")} · {show_date(p["published"])} · {reading_time(body)}</p>',
        f"<article>\n{body}\n</article>",
        f'<div class="tags">{tags}</div>' if tags else "",
        f'<footer>Originally published on <a href="{e(p["medium_url"])}">Medium</a>. <a href="/blog/">All posts</a></footer>',
        "</main>", "</body>", "</html>", ""]
    return "\n".join(x for x in parts if x != "")


def index_page(posts):
    e = html.escape
    items = "\n".join(
        f'  <li><a href="/blog/{e(p["slug"])}/"><h2>{e(p["title"])}</h2>'
        f'<div class="date">{show_date(p["published"])}</div>'
        + (f"<p>{e(p['summary'])}</p>" if p["summary"] else "") + "</a></li>"
        for p in posts)
    parts = [
        head("Blog — Ben Kelly", "Posts by Ben Kelly on housing, local government and policy.",
             f"{SITE}/blog/", LIST_STYLE),
        "<body>", "<main>",
        '<nav class="crumbs"><a href="/">bkkelly.com</a> / Blog</nav>',
        "<h1>Blog</h1>",
        '<p class="lede">Posts by Ben Kelly, also published on <a href="https://medium.com/@bkellyohio">Medium</a>.</p>',
        f'<ul class="posts">\n{items}\n</ul>' if posts else '<p class="lede">No posts yet.</p>',
        "<footer>Not an official City of Cincinnati site.</footer>",
        "</main>", "</body>", "</html>", ""]
    return "\n".join(parts)


def write_if_changed(path, text):
    old = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
    if old == text:
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True


# ---------------------------------------------------------------- main

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"posts": {}}


def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    write_if_changed(DATA_FILE, text)


def update(feed_path, images=True, fetch=None):
    data = load_data()
    stored = data["posts"]
    taken = {p["slug"] for p in stored.values()}
    new = changed = 0
    for item in read_feed(feed_path):
        old = stored.get(item["id"])
        if old and old.get("updated") == item["updated"] and old.get("html"):
            continue
        body, image_urls = clean_html(item.pop("raw_html"), item["medium_url"])
        post = dict(item, html=body, image_urls=image_urls, summary=summary_of(body),
                    slug=old["slug"] if old else slug_from(item, taken),
                    images={u: (old or {}).get("images", {}).get(u) for u in image_urls})
        taken.add(post["slug"])
        stored[item["id"]] = post
        if old:
            changed += 1
        else:
            new += 1
    fetched = failed = 0
    if images:
        for post in stored.values():
            for url in post.get("image_urls", []):
                if post["images"].get(url):
                    continue
                local = download_image(url, post["id"], fetch)
                post["images"][url] = local
                fetched += bool(local)
                failed += not local
    save_data(data)
    ordered = sorted(stored.values(), key=lambda p: p["published"], reverse=True)
    written = sum(write_if_changed(f"{OUT_DIR}/{p['slug']}/index.html", post_page(p)) for p in ordered)
    written += write_if_changed(f"{OUT_DIR}/index.html", index_page(ordered))
    summary = {"posts": len(stored), "new": new, "changed": changed, "images_fetched": fetched,
               "images_failed": failed, "pages_written": written}
    print(f"Blog: {summary}")
    return summary


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        sys.exit(__doc__)
    update(args[0], images="--no-images" not in sys.argv)
