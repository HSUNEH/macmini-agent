#!/usr/bin/env python3
"""Minimal Discord REST client for macmini_agent.

CLI:
  discord_api.py send <channel> <text>
  discord_api.py selected <channel> [--emoji ✅] [--marker AI_INFO_CANDIDATE] [--since ISO]

<channel> is a raw ID or an alias from local/config.json (e.g. "news").
The bot token comes from DISCORD_BOT_TOKEN or ~/.macmini-agent/.env and is never printed.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

API = "https://discord.com/api/v10"
KST = timezone(timedelta(hours=9))
LIMIT = 1900
ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = Path.home() / ".macmini-agent" / ".env"


def load_env() -> Dict[str, str]:
    env: Dict[str, str] = {}
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def token() -> str:
    tok = os.environ.get("DISCORD_BOT_TOKEN", "").strip() or load_env().get("DISCORD_BOT_TOKEN", "")
    if not tok:
        raise RuntimeError("DISCORD_BOT_TOKEN not found in env or ~/.macmini-agent/.env")
    return tok


def resolve_channel(name: str) -> str:
    if name.isdigit():
        return name
    channels = json.loads((ROOT / "local" / "config.json").read_text(encoding="utf-8"))["channels"]
    if name not in channels:
        raise ValueError(f"unknown channel alias: {name}")
    return channels[name]


def request(method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bot {token()}", "User-Agent": "macmini-agent/1.0"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(4):
        req = urllib.request.Request(f"{API}{path}", data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310 - fixed Discord API URL
                raw = resp.read()
                return resp.status, json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 3:
                try:
                    wait = float(json.loads(exc.read().decode("utf-8")).get("retry_after", 1))
                except Exception:
                    wait = 1.0
                time.sleep(wait + 0.2)
                continue
            raise
    raise RuntimeError("unreachable")


def split_text(text: str, limit: int = LIMIT) -> List[str]:
    """Split on line boundaries so links and bullets stay intact."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks: List[str] = []
    cur = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # a single overlong line: hard cut
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        chunks.append(cur)
    return [c.strip() for c in chunks if c.strip()]


def send(channel: str, text: str, files: Optional[List[str]] = None) -> List[str]:
    """Send text (split if needed) and optional file attachments (max 10, on the first message); returns message IDs."""
    cid = resolve_channel(channel)
    ids = []
    chunks = split_text(text) or ([""] if files else [])
    for i, chunk in enumerate(chunks):
        body = {"content": chunk, "allowed_mentions": {"parse": []}}
        if i == 0 and files:
            msg = upload(cid, body, files)
        else:
            _, msg = request("POST", f"/channels/{cid}/messages", body)
        ids.append(str(msg["id"]))
    return ids


def upload(cid: str, body: dict, files: List[str]) -> dict:
    """POST a message with attachments as multipart/form-data."""
    boundary = uuid.uuid4().hex
    parts = [(f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
              f'Content-Type: application/json\r\n\r\n').encode() + json.dumps(body).encode() + b"\r\n"]
    for i, path in enumerate(files[:10]):
        name = Path(path).name
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="files[{i}]"; filename="{name}"\r\n'
                      f"Content-Type: {ctype}\r\n\r\n").encode() + Path(path).read_bytes() + b"\r\n")
    data = b"".join(parts) + f"--{boundary}--\r\n".encode()
    headers = {"Authorization": f"Bot {token()}", "User-Agent": "macmini-agent/1.0",
               "Content-Type": f"multipart/form-data; boundary={boundary}"}
    for attempt in range(4):
        req = urllib.request.Request(f"{API}/channels/{cid}/messages", data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310 - fixed Discord API URL
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 3:
                time.sleep(float(json.loads(exc.read().decode("utf-8")).get("retry_after", 1)) + 0.2)
                continue
            raise
    raise RuntimeError("unreachable")


def react(channel: str, message_id: str, emoji: str) -> None:
    cid = resolve_channel(channel)
    enc = urllib.parse.quote(emoji, safe="")
    request("PUT", f"/channels/{cid}/messages/{message_id}/reactions/{enc}/@me")


def selected(channel: str, emoji: str = "✅", marker: str = "AI_INFO_CANDIDATE", since: Optional[str] = None) -> dict:
    """Candidate messages the user approved: the bot adds one ✅, so count >= 2 means a human clicked it."""
    cid = resolve_channel(channel)
    _, messages = request("GET", f"/channels/{cid}/messages?limit=100")
    if since:
        since_dt = datetime.fromisoformat(since)
    else:  # today's 07:00 digest, with a buffer
        since_dt = datetime.now(KST).replace(hour=6, minute=30, second=0, microsecond=0)
    seen, picked = 0, []
    for msg in messages or []:
        content = str(msg.get("content") or "")
        if marker not in content:
            continue
        ts = datetime.fromisoformat(str(msg.get("timestamp")).replace("Z", "+00:00"))
        if ts < since_dt.astimezone(timezone.utc):
            continue
        seen += 1
        count = next((int(r.get("count") or 0) for r in msg.get("reactions") or []
                      if (r.get("emoji") or {}).get("name") == emoji), 0)
        if count >= 2:
            picked.append({"message_id": msg.get("id"), "timestamp": msg.get("timestamp"),
                           "reaction_count": count, "content": content})
    return {"ok": True, "since": since_dt.isoformat(), "candidate_messages_seen": seen,
            "selected_count": len(picked), "selected": picked}


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_send = sub.add_parser("send")
    p_send.add_argument("channel")
    p_send.add_argument("text")
    p_sel = sub.add_parser("selected")
    p_sel.add_argument("channel")
    p_sel.add_argument("--emoji", default="✅")
    p_sel.add_argument("--marker", default="AI_INFO_CANDIDATE")
    p_sel.add_argument("--since", default=None)
    args = ap.parse_args()
    try:
        if args.cmd == "send":
            print(json.dumps({"ok": True, "message_ids": send(args.channel, args.text)}))
        else:
            print(json.dumps(selected(args.channel, args.emoji, args.marker, args.since), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
