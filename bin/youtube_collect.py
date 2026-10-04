#!/usr/bin/env python3
"""Find new videos from the watched YouTube channels and fetch their transcripts.

  youtube_collect.py            collect -> prints JSON (pre step); transcripts go to $RUN_DIR/transcripts/
  youtube_collect.py mark-seen  after a successful post, record $RUN_DIR/pre.json's videos as seen

Prints {"skip": true} when there is nothing new, so run_job.py skips the LLM.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

KST = timezone(timedelta(hours=9))
STATE = Path(os.environ.get("STATE_DIR", Path.home() / ".macmini-agent" / "state"))
RUN_DIR = Path(os.environ.get("RUN_DIR", "."))
SEEN = STATE / "youtube_seen.json"
CHANNEL_CACHE = STATE / "youtube_channels.json"
MAX_VIDEOS = 5
WINDOW = timedelta(days=7)

ROOT = Path(__file__).resolve().parent.parent
# local/config.json "youtube.channels": [{"name": "...", "url": "https://www.youtube.com/@handle"}, ...]
# Listed order is priority: earlier channels are summarized first.
CHANNELS = [(c["name"], c["url"], i) for i, c in enumerate(
    json.loads((ROOT / "local" / "config.json").read_text(encoding="utf-8")).get("youtube", {}).get("channels", []))]
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ko,en"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310 - fixed YouTube URLs
        return resp.read()


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def channel_id(url: str, cache: Dict[str, str]) -> str:
    if "/channel/" in url:
        return url.rstrip("/").split("/channel/")[-1]
    if url in cache:
        return cache[url]
    html = get(url).decode("utf-8", errors="ignore")
    m = re.search(r'"externalId":"(UC[\w-]+)"', html) or re.search(r'"channelId":"(UC[\w-]+)"', html)
    if not m:
        raise RuntimeError(f"channel id not found: {url}")
    cache[url] = m.group(1)
    return cache[url]


def feed(cid: str) -> List[Dict]:
    root = ET.fromstring(get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"))
    out = []
    for e in root.findall("a:entry", NS):
        vid = (e.findtext("yt:videoId", "", NS) or "").strip()
        published = e.findtext("a:published", "", NS) or ""
        out.append({
            "video_id": vid,
            "title": (e.findtext("a:title", "", NS) or "").strip(),
            "url": f"https://www.youtube.com/watch?v={vid}",
            "published_at": datetime.fromisoformat(published.replace("Z", "+00:00")).astimezone(KST).isoformat() if published else None,
            "description": (e.findtext("media:group/media:description", "", NS) or "").strip()[:1500],
        })
    return out


def transcript(video_id: str, korean_first: bool) -> Optional[str]:
    from youtube_transcript_api import YouTubeTranscriptApi  # system python3 has it in user site-packages

    api = YouTubeTranscriptApi()
    for langs in (["ko", "en"] if korean_first else ["en", "ko"], None):
        try:
            result = api.fetch(video_id, languages=langs) if langs else api.fetch(video_id)
            return " ".join(seg.text for seg in result)
        except Exception:
            time.sleep(3)
    return None


def collect() -> dict:
    now = datetime.now(KST)
    seen = set(load_json(SEEN, {}).get("video_ids", []))
    cache = load_json(CHANNEL_CACHE, {})
    fresh, errors = [], []
    for name, url, prio in CHANNELS:
        try:
            for v in feed(channel_id(url, cache)):
                pub = datetime.fromisoformat(v["published_at"]) if v["published_at"] else None
                if pub and now - WINDOW <= pub <= now and v["video_id"] not in seen:
                    fresh.append(dict(v, channel=name, priority=prio))
        except Exception as exc:
            errors.append({"channel": name, "error": f"{exc.__class__.__name__}: {exc}"})
    CHANNEL_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    # by channel priority, newest first within a channel
    fresh.sort(key=lambda v: (v["priority"], -datetime.fromisoformat(v["published_at"]).timestamp()))
    picked = fresh[:MAX_VIDEOS]
    tdir = RUN_DIR / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    for i, v in enumerate(picked):
        if i:
            time.sleep(5)  # be gentle; YouTube rate-limits transcript fetches
        korean = bool(re.search(r"[가-힣]", v["title"] + v["description"]))
        text = transcript(v["video_id"], korean)
        if text:
            path = tdir / f"{v['video_id']}.txt"
            path.write_text(text, encoding="utf-8")
            v["transcript_path"], v["transcript_chars"] = str(path), len(text)
        else:
            v["transcript_path"] = None
    return {"skip": not picked, "now": now.isoformat(), "unseen_count": len(fresh),
            "selected": picked, "errors": errors}


def mark_seen() -> dict:
    pre = load_json(RUN_DIR / "pre.json", {})
    state = load_json(SEEN, {"video_ids": []})
    ids = [v["video_id"] for v in pre.get("selected", [])]
    state["video_ids"] = sorted(set(state.get("video_ids", [])) | set(ids))
    state["updated_at"] = datetime.now(KST).isoformat()
    SEEN.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "marked": ids}


if __name__ == "__main__":
    STATE.mkdir(parents=True, exist_ok=True)
    result = mark_seen() if sys.argv[1:] == ["mark-seen"] else collect()
    print(json.dumps(result, ensure_ascii=False, indent=2))
