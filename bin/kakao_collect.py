#!/usr/bin/env python3
"""Collect recent KakaoTalk messages via agent-kakaotalk.

Flow:
1. Ensure agent-kakaotalk auth exists.
2. List chats, optionally resolving titles.
3. Filter by config/rooms.yaml search_keywords.
4. Fetch recent messages per matched room.
5. Save raw JSON and a lightweight ai_info candidate markdown.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python <3.9 fallback
    ZoneInfo = None  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
AGENT_HOME = Path.home() / ".macmini-agent"
BIN = Path(
    os.environ.get(
        "KAKAO_AGENT_CLI",
        str(AGENT_HOME / "kakao-cli" / "node_modules" / ".bin" / "agent-kakaotalk"),
    )
).expanduser()
CLI_CWD = Path(os.environ.get("KAKAO_AGENT_CLI_CWD", str(Path.home()))).expanduser()
CONFIG = ROOT / "local" / "kakao" / "rooms.yaml"
KAKAO_DATA = AGENT_HOME / "data" / "kakao"
RAW = KAKAO_DATA / "raw"
PARSED = KAKAO_DATA / "parsed"
SUMMARIES = KAKAO_DATA / "summaries"
LOGS = KAKAO_DATA / "logs"
URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+", re.I)


def run(args: list[str], timeout: int = 120) -> tuple[int, str, str]:
    # Bun resolves parts of agent-messenger's dependency tree from cwd. Running
    # it inside KakaoAICollector selects that project's stale node_modules and
    # leaves the CLI hanging even for `auth status`.
    try:
        p = subprocess.run(args, cwd=CLI_CWD, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        return 124, stdout, f"command timed out after {timeout}s\\n{stderr}".strip()


def load_config() -> dict[str, Any]:
    # Avoid adding PyYAML dependency; parse the tiny config shape manually enough for our file.
    if not CONFIG.exists():
        return {"rooms": [], "collection": {"messages_per_room": 120}}
    try:
        import yaml  # type: ignore
        return yaml.safe_load(CONFIG.read_text()) or {}
    except Exception:
        # Tiny YAML fallback for our config shape; avoids treating room objects as keywords.
        text = CONFIG.read_text()
        chat_ids = re.findall(r"^\s*chat_id:\s*[\"']?([^\"'\n#]+)", text, re.M)
        m = re.search(r"messages_per_room:\s*(\d+)", text)
        limit = int(m.group(1)) if m else 120
        tz_m = re.search(r"timezone:\s*[\"']?([^\"'\n#]+)", text)
        start_m = re.search(r"summary_window_start_hour:\s*(\d+)", text)
        end_m = re.search(r"summary_window_end_hour:\s*(\d+)", text)
        collection = {
            "messages_per_room": limit,
            "timezone": (tz_m.group(1).strip() if tz_m else "Asia/Seoul"),
            "summary_window_start_hour": int(start_m.group(1)) if start_m else 7,
            "summary_window_end_hour": int(end_m.group(1)) if end_m else 7,
        }
        keywords: list[str] = []
        in_keywords = False
        for line in text.splitlines():
            if re.match(r"^\s*search_keywords:\s*$", line):
                in_keywords = True
                continue
            if in_keywords:
                km = re.match(r"^\s*-\s*[\"']?([^\"'\n#]+)", line)
                if km:
                    keywords.append(km.group(1).strip())
                    continue
                if line.strip() and not line.startswith(' ' * 6):
                    in_keywords = False
        return {"rooms": [{"name": "fallback", "chat_ids": [c.strip() for c in chat_ids], "search_keywords": keywords}], "collection": collection}


def parse_json_output(text: str) -> Any:
    text = text.strip()
    if not text:
        raise ValueError("empty output")
    return json.loads(text)


def normalize_list(obj: Any) -> list[dict[str, Any]]:
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for key in ["chats", "rooms", "messages", "data", "items", "results"]:
            v = obj.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def chat_text(chat: dict[str, Any]) -> str:
    parts = []
    for k in ["title", "display_name", "name", "chat_name", "last_message"]:
        v = chat.get(k)
        if isinstance(v, dict):
            parts.append(json.dumps(v, ensure_ascii=False))
        elif v is not None:
            parts.append(str(v))
    return " ".join(parts)


def fmt_value(v: Any) -> str:
    """Format agent-kakaotalk Long-like objects and scalar values compactly."""
    if isinstance(v, dict) and {"high", "low"}.issubset(v):
        try:
            high = int(v.get("high") or 0)
            low = int(v.get("low") or 0)
            unsigned = bool(v.get("unsigned"))
            n = (high << 32) + (low & 0xFFFFFFFF)
            if not unsigned and n >= (1 << 63):
                n -= 1 << 64
            return str(n)
        except Exception:
            return json.dumps(v, ensure_ascii=False)
    if v is None:
        return ""
    return str(v)


def get_chat_id(chat: dict[str, Any]) -> str | None:
    for k in ["chat_id", "id", "chatId", "room_id"]:
        if chat.get(k) is not None:
            return str(chat[k])
    return None


def local_tz(name: str):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    # KST fallback; good enough for Asia/Seoul which has no DST in current era.
    return timezone(timedelta(hours=9))


def as_epoch_seconds(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, dict) and {"high", "low"}.issubset(v):
        try:
            high = int(v.get("high") or 0)
            low = int(v.get("low") or 0)
            n = (high << 32) + (low & 0xFFFFFFFF)
            if not bool(v.get("unsigned")) and n >= (1 << 63):
                n -= 1 << 64
            return n
        except Exception:
            return None
    try:
        return int(v)
    except Exception:
        return None


def message_dt(m: dict[str, Any], tz) -> datetime | None:
    ts = as_epoch_seconds(m.get("sent_at"))
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=tz)
    except Exception:
        return None


def summary_window(now: datetime, start_hour: int = 1, end_hour: int = 1) -> tuple[datetime, datetime]:
    """Return previous-day start_hour <= sent_at < current-day end_hour in local time.

    Daily cron sends at 07:00, but summary day closes at 01:00.
    Example: 2026-05-12 07:00 => 2026-05-11 01:00 <= t < 2026-05-12 01:00.
    """
    end = datetime.combine(now.date(), time(hour=end_hour), tzinfo=now.tzinfo)
    if now < end:
        end -= timedelta(days=1)
    start = end - timedelta(days=1)
    start = start.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    return start, end


def main() -> int:
    cfg = load_config()
    collection_cfg = cfg.get("collection") or {}
    tz = local_tz(str(collection_cfg.get("timezone") or "Asia/Seoul"))
    now_dt = datetime.now(tz)
    now = now_dt.strftime("%Y-%m-%d_%H-%M-%S")
    day = now_dt.strftime("%Y-%m-%d")
    start_at, end_at = summary_window(
        now_dt,
        int(collection_cfg.get("summary_window_start_hour") or 1),
        int(collection_cfg.get("summary_window_end_hour") or 1),
    )
    start_ts = int(start_at.timestamp())
    end_ts = int(end_at.timestamp())
    for d in [RAW / day, PARSED, SUMMARIES, LOGS]:
        d.mkdir(parents=True, exist_ok=True)

    if not BIN.exists():
        print(f"agent-kakaotalk missing: {BIN}", file=sys.stderr)
        return 2

    code, out, err = run([str(BIN), "auth", "status", "--pretty"], timeout=60)
    if code != 0:
        status_path = LOGS / f"auth_required_{now}.json"
        status_path.write_text(out or err)
        print(json.dumps({
            "status": "auth_required",
            "message": "Kakao session invalid. Run bin/kakao_healthcheck.sh (auto re-login from ~/.macmini-agent/.env).",
            "detail_path": str(status_path),
        }, ensure_ascii=False, indent=2))
        return 1

    rooms_cfg = cfg.get("rooms") or []
    keywords: list[str] = []
    configured_chat_ids: set[str] = set()
    for room in rooms_cfg:
        if isinstance(room, dict):
            keywords.extend([str(x) for x in room.get("search_keywords", [])])
            for cid in room.get("chat_ids", []) or []:
                configured_chat_ids.add(str(cid))
            if room.get("chat_id"):
                configured_chat_ids.add(str(room.get("chat_id")))
    keywords = [k for k in keywords if k]
    limit = int((collection_cfg.get("messages_per_room") or 2000))

    configured_rooms: list[dict[str, Any]] = []
    for room in rooms_cfg:
        if not isinstance(room, dict):
            continue
        room_id = room.get("chat_id")
        if room_id:
            configured_rooms.append({
                "chat_id": str(room_id),
                "title": room.get("name") or str(room_id),
                "display_name": room.get("name") or str(room_id),
                "source": "config_fallback",
            })
        for cid in room.get("chat_ids", []) or []:
            configured_rooms.append({
                "chat_id": str(cid),
                "title": room.get("name") or str(cid),
                "display_name": room.get("name") or str(cid),
                "source": "config_fallback",
            })

    # Use --all for better coverage; resolve titles may be slower but useful for open chats.
    code, out, err = run([str(BIN), "chat", "list", "--all", "--resolve-titles"], timeout=180)
    chat_list_error = None
    if code != 0:
        chat_list_error = {"stderr": err, "stdout": out}
        if not configured_rooms:
            print(json.dumps({"status": "chat_list_failed", **chat_list_error}, ensure_ascii=False, indent=2))
            return 3
        chats = []
    else:
        try:
            chats_obj = parse_json_output(out)
            chats = normalize_list(chats_obj)
        except Exception as e:
            chat_list_error = {"stderr": err, "stdout": out, "parse_error": str(e)}
            if not configured_rooms:
                print(json.dumps({"status": "chat_list_failed", **chat_list_error}, ensure_ascii=False, indent=2))
                return 3
            chats = []

    if configured_chat_ids:
        matched = [c for c in chats if (get_chat_id(c) or "") in configured_chat_ids]
        # Also allow keyword fallback for rooms not yet identified by chat_id.
        if keywords:
            seen = {get_chat_id(c) for c in matched}
            matched.extend([c for c in chats if get_chat_id(c) not in seen and any(k.lower() in chat_text(c).lower() for k in keywords)])
        # agent-kakaotalk chat list can occasionally return [] while direct
        # message list <chat_id> still works. In that case, synthesize room
        # records from config so the collector does not silently produce
        # matched_rooms=0 and skip all known source rooms.
        seen = {get_chat_id(c) for c in matched}
        matched.extend([c for c in configured_rooms if get_chat_id(c) not in seen])
    elif keywords:
        matched = [c for c in chats if any(k.lower() in chat_text(c).lower() for k in keywords)]
    else:
        matched = chats

    all_records: list[dict[str, Any]] = []
    md_lines = [f"# Kakao AI daily window — {now}", ""]
    md_lines.append(f"Window: {start_at.isoformat()} <= sent_at < {end_at.isoformat()}")
    md_lines.append(f"Matched rooms: {len(matched)} / {len(chats)}")
    md_lines.append(f"Fetched per room: {limit}")
    md_lines.append("")

    for chat in matched:
        chat_id = get_chat_id(chat)
        if not chat_id:
            continue
        title = chat.get("title") or chat.get("display_name") or chat_id
        code, mout, merr = run([str(BIN), "message", "list", chat_id, "-n", str(limit)], timeout=180)
        if code != 0:
            all_records.append({"chat": chat, "error": merr or mout})
            continue
        try:
            messages = normalize_list(parse_json_output(mout))
        except Exception as e:
            all_records.append({"chat": chat, "error": f"parse_failed: {e}", "raw": mout})
            continue
        window_messages = []
        for m in messages:
            ts = as_epoch_seconds(m.get("sent_at"))
            if ts is not None and start_ts <= ts < end_ts:
                window_messages.append(m)
        all_records.append({"chat": chat, "messages": messages, "window_messages": window_messages, "window": {"start_at": start_at.isoformat(), "end_at": end_at.isoformat()}})
        md_lines.append(f"## {title} (`{chat_id}`)")
        md_lines.append(f"- fetched: {len(messages)} / in_window: {len(window_messages)}")
        count = 0
        for m in window_messages:
            text = str(m.get("message") or "").strip()
            if not text:
                continue
            author = fmt_value(m.get("author_name") or m.get("author_id") or "unknown")
            dt = message_dt(m, tz)
            sent = dt.isoformat() if dt else fmt_value(m.get("sent_at") or "")
            snippet = text.replace("\n", " ")[:1000]
            md_lines.append(f"- **{author}** `{sent}`: {snippet}")
            urls = URL_RE.findall(text)
            for u in urls:
                md_lines.append(f"  - {u}")
            count += 1
        if count == 0:
            md_lines.append("- 시간창 내 메시지 없음")
        md_lines.append("")

    raw_path = RAW / day / f"kakao_messages_{now}.json"
    md_path = SUMMARIES / f"ai_candidates_{now}.md"
    raw_path.write_text(json.dumps({
        "timestamp": now,
        "timezone": str(collection_cfg.get("timezone") or "Asia/Seoul"),
        "window": {"start_at": start_at.isoformat(), "end_at": end_at.isoformat()},
        "keywords": keywords,
        "records": all_records,
    }, ensure_ascii=False, indent=2))
    md_path.write_text("\n".join(md_lines))

    fetched = [r for r in all_records if "error" not in r]
    if all_records and not fetched:  # every room failed: almost always an expired Kakao session
        print(json.dumps({
            "status": "fetch_failed",
            "message": "카카오 방 메시지를 하나도 못 가져왔습니다. 세션이 끊긴 것 같습니다: "
                       "ssh -t macmini '~/macmini_agent/bin/kakao_login.sh'",
            "first_error": str(all_records[0].get("error"))[:300],
            "raw_path": str(raw_path),
        }, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps({
        "status": "ok",
        "total_chats": len(chats),
        "matched_rooms": len(matched),
        "window_start_at": start_at.isoformat(),
        "window_end_at": end_at.isoformat(),
        "fetched_per_room": limit,
        "raw_path": str(raw_path),
        "candidate_markdown": str(md_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
