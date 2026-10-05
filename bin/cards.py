#!/usr/bin/env python3
"""Render card news (1080x1350 PNGs) from a cards.json spec with headless Chrome.

  cards.py <cards.json> [out_dir]     render and print the PNG paths as JSON

Spec (written by the LLM; layout is ours so text is never garbled):
  {"channel": "news", "title": "오늘의 AI/Tech Top5", "theme": "ai" | "finance", "brand": "MY NEWS" (optional),
   "items": [{"tag": "보안", "headline": "...", "summary": "...", "point_label": "왜 중요",
              "point": "...", "source": "연합뉴스", "url": "https://...", "image_url": "https://... (optional)",
              "date": "10/02 (optional)"}]}

Each story card uses the supplied representative image, or discovers the article's
Open Graph image.  It also displays the publisher's favicon, so the visual source
is clear even when an article does not expose a usable photo.
"""
from __future__ import annotations

import html
import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
W, H = 1080, 1350
KST = timezone(timedelta(hours=9))
WEEKDAY = "월화수목금토일"
THEMES = {  # background, card surface, accent, ink, muted
    "ai": {"bg": "#0F172A", "accent": "#F97316", "soft": "#1E293B", "ink": "#F8FAFC", "muted": "#94A3B8"},
    "finance": {"bg": "#052E2B", "accent": "#34D399", "soft": "#0B3B37", "ink": "#F0FDF4", "muted": "#99B8AE"},
}

BASE_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { width: %(W)dpx; height: %(H)dpx; }
body { background: %(bg)s; color: %(ink)s; overflow: hidden;
       font-family: "Apple SD Gothic Neo", "Pretendard", -apple-system, sans-serif;
       word-break: keep-all; overflow-wrap: anywhere; -webkit-font-smoothing: antialiased; }
.page { position: absolute; inset: 0; padding: 88px 84px 80px; display: flex; flex-direction: column; }
.top { display: flex; justify-content: space-between; align-items: center; font-size: 30px; color: %(muted)s; font-weight: 600; }
.brand { letter-spacing: 0.04em; }
.foot { margin-top: auto; display: flex; justify-content: space-between; align-items: flex-end;
        font-size: 28px; color: %(muted)s; font-weight: 500; padding-top: 32px; border-top: 2px solid %(soft)s; }
.foot b { color: %(ink)s; font-weight: 700; }
.fit { overflow: hidden; }
"""

COVER = """<!doctype html><html><head><meta charset="utf-8"><style>%(css)s
.date { margin-top: 96px; font-size: 40px; font-weight: 700; color: %(accent)s; }
h1 { margin-top: 20px; font-size: 104px; line-height: 1.12; font-weight: 800; letter-spacing: -0.02em; }
ol { margin-top: 72px; list-style: none; display: flex; flex-direction: column; gap: 30px; }
li { display: flex; gap: 26px; align-items: baseline; font-size: 38px; line-height: 1.35; font-weight: 600; }
li .n { flex: none; width: 56px; height: 56px; border-radius: 28px; background: %(accent)s; color: %(bg)s;
        font-size: 30px; font-weight: 800; display: flex; align-items: center; justify-content: center; transform: translateY(-4px); }
li .t { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
</style></head><body><div class="page">
<div class="top"><span class="brand">%(brand)s</span><span>%(count)d개 소식</span></div>
<div class="date">%(emoji)s %(date)s</div>
<h1>%(title)s</h1>
<ol class="fit">%(list)s</ol>
<div class="foot"><span>넘겨서 하나씩 보기 →</span><span><b>1</b> / %(total)d</span></div>
</div></body></html>"""

ITEM = """<!doctype html><html><head><meta charset="utf-8"><style>%(css)s
.badge { margin-top: 38px; display: flex; gap: 18px; align-items: center; }
.num { font-size: 92px; font-weight: 800; color: %(accent)s; line-height: 1; letter-spacing: -0.04em; }
.tag { font-size: 30px; font-weight: 700; padding: 11px 24px; border-radius: 999px; background: %(soft)s; color: %(ink)s; }
.date { font-size: 30px; color: %(muted)s; font-weight: 600; }
.hero { position: relative; height: 270px; margin-top: 26px; overflow: hidden; border-radius: 26px; background: %(soft)s; }
.hero > img { width: 100%%; height: 100%%; object-fit: cover; display: block; }
.hero::after { content: ""; position: absolute; inset: 45%% 0 0; background: linear-gradient(transparent, rgba(0,0,0,.6)); }
.hero-fallback { display: flex; align-items: flex-end; padding: 34px; font-size: 38px; font-weight: 800; color: %(muted)s; }
.publisher { position: absolute; z-index: 1; left: 24px; bottom: 20px; display: flex; gap: 12px; align-items: center; font-size: 25px; font-weight: 700; color: white; text-shadow: 0 1px 4px rgba(0,0,0,.65); }
.publisher img { width: 42px; height: 42px; border-radius: 12px; background: white; padding: 4px; object-fit: contain; }
h2 { margin-top: 28px; font-size: 58px; line-height: 1.2; font-weight: 800; letter-spacing: -0.02em; max-height: 3.6em; }
.summary { margin-top: 26px; font-size: 34px; line-height: 1.48; font-weight: 500; color: %(ink)s; opacity: 0.92; max-height: 4.45em; }
.point { margin-top: 28px; background: %(soft)s; border-left: 9px solid %(accent)s; border-radius: 18px; padding: 24px 30px; }
.point .label { font-size: 27px; font-weight: 800; color: %(accent)s; }
.point .text { margin-top: 7px; font-size: 32px; line-height: 1.4; font-weight: 600; max-height: 2.8em; }
</style></head><body><div class="page">
<div class="top"><span class="brand">%(title)s</span><span>%(date_head)s</span></div>
<div class="badge"><span class="num">%(n)02d</span><span class="tag">%(tag)s</span>%(item_date)s</div>
%(hero)s
<h2 class="fit">%(headline)s</h2>
<div class="summary fit">%(summary)s</div>
%(point)s
<div class="foot"><span><b>%(source)s</b> · %(domain)s</span><span><b>%(page)d</b> / %(total)d</span></div>
</div>
<script>
// shrink any block whose text overflows its max-height, so nothing is clipped mid-sentence
for (const el of document.querySelectorAll('.fit, .point .text')) {
  let size = parseFloat(getComputedStyle(el).fontSize);
  while (el.scrollHeight > el.clientHeight + 2 && size > 22) { size -= 2; el.style.fontSize = size + 'px'; }
}
</script></body></html>"""


def esc(text: str) -> str:
    return html.escape(str(text or "").strip())


def domain(url: str) -> str:
    host = urlparse(url or "").netloc
    return host[4:] if host.startswith("www.") else host


class MetaParser(HTMLParser):
    """Tiny dependency-free Open Graph parser for article cover images."""

    def __init__(self) -> None:
        super().__init__()
        self.values: Dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "meta":
            return
        data = {str(k).lower(): str(v) for k, v in attrs if k and v}
        key = (data.get("property") or data.get("name") or data.get("itemprop") or "").lower()
        value = data.get("content", "").strip()
        if key and value and key not in self.values:
            self.values[key] = value


def http_url(value: str) -> bool:
    parsed = urlparse(value or "")
    # Do not let card rendering load local files supplied by an LLM response.
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}


def og_image(article_url: str) -> str:
    """Return the first usable social-preview image advertised by an article."""
    if not http_url(article_url):
        return ""
    try:
        req = Request(article_url, headers={"User-Agent": "Mozilla/5.0 (compatible; macmini-agent/1.0)"})
        with urlopen(req, timeout=8) as response:  # nosec B310 - URL was selected by the news job
            if "html" not in response.headers.get_content_type():
                return ""
            raw = response.read(1_500_000)
            charset = response.headers.get_content_charset() or "utf-8"
        parser = MetaParser()
        parser.feed(raw.decode(charset, errors="replace"))
        for key in ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src"):
            candidate = parser.values.get(key, "").strip()
            if not candidate:
                continue
            image = urljoin(article_url, candidate)
            if http_url(image):
                return image
    except Exception:
        pass
    return ""


def download_image(image_url: str, target: Path) -> str:
    """Download a bounded image for Chrome.  A local copy prevents hotlink surprises."""
    if not http_url(image_url):
        return ""
    try:
        req = Request(image_url, headers={"User-Agent": "Mozilla/5.0 (compatible; macmini-agent/1.0)"})
        with urlopen(req, timeout=12) as response:  # nosec B310 - URL was selected by the news job
            if not response.headers.get_content_type().startswith("image/"):
                return ""
            raw = response.read(6_000_001)
        if not raw or len(raw) > 6_000_000:
            return ""
        target.write_bytes(raw)
        return target.as_uri()
    except Exception:
        return ""


def prepare_media(items: List[Dict], directory: Path) -> None:
    """Add local cover-image URLs to items, without letting a failed fetch block a digest."""
    directory.mkdir(parents=True, exist_ok=True)

    def one(pair) -> None:
        index, item = pair
        image = str(item.get("image_url") or "").strip()
        if not http_url(image):
            image = og_image(str(item.get("url") or ""))
        item["_image"] = download_image(image, directory / f"image-{index + 1}.img") if image else ""

    with ThreadPoolExecutor(max_workers=min(4, max(len(items), 1))) as pool:
        list(pool.map(one, enumerate(items)))


def hero(item: Dict) -> str:
    """The publisher favicon remains visible over both photos and the fallback tile."""
    publisher = esc(item.get("source") or domain(item.get("url", "")))
    host = domain(item.get("url", ""))
    favicon = f"https://www.google.com/s2/favicons?domain={quote(host)}&sz=128" if host else ""
    logo = f'<img src="{esc(favicon)}" alt="">' if favicon else ""
    byline = f'<div class="publisher">{logo}<span>{publisher}</span></div>'
    image = str(item.get("_image") or "")
    if image:
        return f'<div class="hero"><img src="{esc(image)}" alt="">{byline}</div>'
    return f'<div class="hero hero-fallback"><span>{publisher}</span>{byline}</div>'


def pages(spec: Dict) -> List[str]:
    theme = THEMES.get(spec.get("theme", "ai"), THEMES["ai"])
    css = BASE_CSS % dict(theme, W=W, H=H)
    now = datetime.now(KST)
    date = f"{now:%Y.%m.%d} ({WEEKDAY[now.weekday()]})"
    items = spec["items"]
    total = len(items) + 1
    listing = "".join(f'<li><span class="n">{i}</span><span class="t">{esc(it["headline"])}</span></li>'
                      for i, it in enumerate(items, 1))
    out = [COVER % dict(theme, css=css, brand=esc(spec.get("brand") or "DAILY NEWS"), count=len(items), emoji=esc(spec.get("emoji", "")), date=date,
                        title=esc(spec["title"]), list=listing, total=total)]
    for i, it in enumerate(items, 1):
        point = ""
        if it.get("point"):
            point = (f'<div class="point"><div class="label">{esc(it.get("point_label") or "왜 중요")}</div>'
                     f'<div class="text">{esc(it["point"])}</div></div>')
        out.append(ITEM % dict(theme, css=css, title=esc(spec["title"]), date_head=f"{now:%m.%d}", n=i,
                               tag=esc(it.get("tag") or "뉴스"),
                               item_date=f'<span class="date">{esc(it["date"])} 발행</span>' if it.get("date") else "",
                               hero=hero(it), headline=esc(it["headline"]), summary=esc(it.get("summary")), point=point,
                               source=esc(it.get("source") or domain(it.get("url", ""))),
                               domain=esc(domain(it.get("url", ""))), page=i + 1, total=total))
    return out


def render(spec: Dict, out_dir: Path) -> List[Path]:
    if not spec.get("items"):
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    with tempfile.TemporaryDirectory() as tmp:
        prepare_media(spec["items"], Path(tmp) / "images")
        for i, page in enumerate(pages(spec)):
            src = Path(tmp) / f"card{i}.html"
            src.write_text(page, encoding="utf-8")
            png = out_dir / f"card-{i + 1:02d}.png"
            subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                            f"--window-size={W},{H}", "--virtual-time-budget=1500", f"--screenshot={png}", src.as_uri()],
                           check=True, capture_output=True, timeout=60)
            paths.append(png)
    return paths


def links_text(spec: Dict) -> str:
    lines = ["🔗 출처"]
    for i, it in enumerate(spec["items"], 1):
        lines.append(f"{i}. {it['headline']} — <{it['url']}>" if it.get("url") else f"{i}. {it['headline']}")
    return "\n".join(lines)


def text_fallback(spec: Dict) -> str:
    """The same content as plain text, used if rendering fails."""
    lines = [f"{spec.get('emoji', '')} {spec['title']}".strip(), ""]
    for i, it in enumerate(spec["items"], 1):
        lines += [f"{i}. [{it.get('tag', '')}] {it['headline']}", f"- {it.get('summary', '')}"]
        if it.get("point"):
            lines.append(f"- {it.get('point_label') or '왜 중요'}: {it['point']}")
        lines += [f"- 출처: {it.get('source', '')} — <{it.get('url', '')}>", ""]
    return "\n".join(lines)


def validate(spec: Dict) -> None:
    if not isinstance(spec, dict) or not isinstance(spec.get("items"), list):
        raise ValueError("cards.json needs an object with an items list")
    for i, it in enumerate(spec["items"]):
        for key in ("headline", "summary"):
            if not str(it.get(key) or "").strip():
                raise ValueError(f"cards.json items[{i}] is missing {key}")
    if len(spec["items"]) > 9:  # cover + 9 = Discord's 10-attachment limit
        raise ValueError("cards.json has more than 9 items")


if __name__ == "__main__":
    spec_path = Path(sys.argv[1])
    data = json.loads(spec_path.read_text(encoding="utf-8"))
    validate(data)
    out = render(data, Path(sys.argv[2]) if len(sys.argv) > 2 else spec_path.parent / "cards")
    print(json.dumps([str(p) for p in out], ensure_ascii=False))
