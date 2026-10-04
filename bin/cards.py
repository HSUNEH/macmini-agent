#!/usr/bin/env python3
"""Render card news (1080x1350 PNGs) from a cards.json spec with headless Chrome.

  cards.py <cards.json> [out_dir]     render and print the PNG paths as JSON

Spec (written by the LLM; layout is ours so text is never garbled):
  {"channel": "news", "title": "오늘의 AI/Tech Top5", "theme": "ai" | "finance", "brand": "MY NEWS" (optional),
   "items": [{"tag": "보안", "headline": "...", "summary": "...", "point_label": "왜 중요",
              "point": "...", "source": "연합뉴스", "url": "https://...", "date": "10/02 (optional)"}]}
"""
from __future__ import annotations

import html
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlparse

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
.badge { margin-top: 64px; display: flex; gap: 20px; align-items: center; }
.num { font-size: 120px; font-weight: 800; color: %(accent)s; line-height: 1; letter-spacing: -0.04em; }
.tag { font-size: 32px; font-weight: 700; padding: 12px 26px; border-radius: 999px; background: %(soft)s; color: %(ink)s; }
.date { font-size: 30px; color: %(muted)s; font-weight: 600; }
h2 { margin-top: 44px; font-size: 68px; line-height: 1.22; font-weight: 800; letter-spacing: -0.02em; max-height: 4.9em; }
.summary { margin-top: 40px; font-size: 40px; line-height: 1.55; font-weight: 500; color: %(ink)s; opacity: 0.92; max-height: 7.8em; }
.point { margin-top: 44px; background: %(soft)s; border-left: 10px solid %(accent)s; border-radius: 20px; padding: 32px 36px; }
.point .label { font-size: 30px; font-weight: 800; color: %(accent)s; }
.point .text { margin-top: 10px; font-size: 38px; line-height: 1.45; font-weight: 600; max-height: 4.35em; }
</style></head><body><div class="page">
<div class="top"><span class="brand">%(title)s</span><span>%(date_head)s</span></div>
<div class="badge"><span class="num">%(n)02d</span><span class="tag">%(tag)s</span>%(item_date)s</div>
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
                               headline=esc(it["headline"]), summary=esc(it.get("summary")), point=point,
                               source=esc(it.get("source") or domain(it.get("url", ""))),
                               domain=esc(domain(it.get("url", ""))), page=i + 1, total=total))
    return out


def render(spec: Dict, out_dir: Path) -> List[Path]:
    if not spec.get("items"):
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    with tempfile.TemporaryDirectory() as tmp:
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
        for key in ("headline", "summary", "url"):
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
