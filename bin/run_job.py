#!/usr/bin/env python3
"""Run one scheduled job: optional pre step -> LLM (codex | claude) -> post messages.json to Discord.

Usage:
  run_job.py <job> [--dry-run] [--engine codex|claude]

A job is local/jobs/<job>.md: a `---` header of `key: value` lines (values parsed as JSON when possible)
followed by the prompt. Header keys:
  schedule  ["HH:MM", ...]          launchd times (used by install.py)
  engine    codex | claude | ... | none   (see engines.py); none = run `pre` only and post its stdout to `notify`
  search    true | false            enable web search (codex --search; claude has WebSearch built in)
  timeout   seconds for the LLM step (default 1800)
  pre       command run before the LLM; stdout saved to RUN_DIR/pre.json
  after     command run only after every message was posted
  notify    channel alias for engine=none output
  cards     true = the LLM writes RUN_DIR/cards.json and the job posts rendered card-news images

The LLM never sees the Discord token: it writes RUN_DIR/messages.json and this script posts it.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import cards  # noqa: E402
import discord_api  # noqa: E402
import engines  # noqa: E402

HOME = Path.home() / ".macmini-agent"
STATE, DATA, LOGS, RUNS = HOME / "state", HOME / "data", HOME / "logs", HOME / "runs"
KST = timezone(timedelta(hours=9))
WEEKDAY_KO = "월화수목금토일"
WEEKDAY_EMOJI = ["🌙", "🔥", "💧", "🌳", "🥇", "🌍", "🌞"]
LOCAL = ROOT / "local"  # your config, jobs, prompts (not in git; start from examples/)
CONFIG = json.loads((LOCAL / "config.json").read_text(encoding="utf-8"))

OUTPUT_CONTRACT = """
# 출력 규칙 (반드시 지킬 것)
- Discord로 직접 보내지 마세요. 토큰도 없습니다. 전송은 작업이 끝난 뒤 스크립트가 합니다.
- 보낼 메시지를 `{{RUN_DIR}}/messages.json`에 JSON 배열로 저장하세요.
  `[{"channel": "<채널 별칭>", "content": "<본문>"}]`
  - 선택 필드 `"react": "✅"`: 보낸 뒤 봇이 그 이모지를 달아 둡니다.
  - 채널 별칭: {{CHANNELS}}
- 배열 순서대로 보냅니다. content 하나는 1900자 이하로 쓰고, 넘치면 메시지를 나누세요.
- Discord 마크다운 표는 쓰지 마세요. 링크 미리보기가 쌓이지 않도록 URL은 `<https://...>`처럼 꺾쇠로 감싸세요.
- 보낼 것이 없으면 `[]`을 저장하세요.
- 끝내기 전에 `python3 -m json.tool {{RUN_DIR}}/messages.json`으로 JSON이 올바른지 확인하세요.
"""


CARDS_CONTRACT = """
# 출력 규칙 (반드시 지킬 것)
- Discord로 직접 보내지 마세요. 카드 이미지는 작업이 끝난 뒤 스크립트가 그립니다. 디자인은 신경 쓰지 말고 내용만 쓰세요.
- `{{RUN_DIR}}/cards.json`에 아래 형식으로 저장하세요.
```json
{"channel": "news", "title": "카드 제목", "theme": "ai 또는 finance", "emoji": "{{WEEKDAY_EMOJI}}",
 "items": [{"tag": "짧은 분류 2~6자", "headline": "기사 제목 40자 이내", "summary": "무슨 일인지 2문장, 110자 이내",
            "point_label": "왜 중요", "point": "한 문장 60자 이내", "source": "매체·기관 이름",
            "url": "https://원문", "date": "24시간 넘은 기사만 MM/DD, 아니면 생략"}]}
```
- 항목은 최대 9개. 글자 수 제한을 지켜야 카드에서 잘리지 않습니다. 마크다운·이모지는 본문에 넣지 마세요.
- 보낼 것이 없으면 `"items": []`로 저장하세요.
- 끝내기 전에 `python3 -m json.tool {{RUN_DIR}}/cards.json`으로 JSON이 올바른지 확인하세요.
"""


def card_messages(run_dir: Path, note) -> List[Dict]:
    """cards.json -> [images message, links message]; plain text if rendering fails."""
    path = run_dir / "cards.json"
    if not path.exists():
        raise JobError("LLM이 cards.json을 만들지 않았습니다")
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
        cards.validate(spec)
    except ValueError as exc:
        raise JobError(f"cards.json 오류: {exc}")
    if not spec["items"]:
        return []
    channel = str(spec.get("channel") or "news")
    spec.setdefault("brand", CONFIG.get("cards", {}).get("brand", "DAILY NEWS"))
    try:
        pngs = cards.render(spec, run_dir / "cards")
    except Exception as exc:  # keep the news flowing even if Chrome breaks
        note(f"card render failed, sending text instead: {exc}")
        return [{"channel": channel, "content": cards.text_fallback(spec)}]
    return [{"channel": channel, "content": "", "files": [str(p) for p in pngs]},
            {"channel": channel, "content": cards.links_text(spec)}]


class JobError(Exception):
    pass


def parse_job(name: str) -> Tuple[Dict, str]:
    path = LOCAL / "jobs" / f"{name}.md"
    if not path.exists():
        raise SystemExit(f"no such job: {path}")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", path.read_text(encoding="utf-8"), re.S)
    if not m:
        raise SystemExit(f"{path}: missing --- header")
    meta: Dict = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, val = line.split(":", 1)
        val = val.strip()
        try:
            meta[key.strip()] = json.loads(val)
        except ValueError:
            meta[key.strip()] = val
    return meta, m.group(2)


def render(text: str, ctx: Dict[str, str]) -> str:
    for key, val in ctx.items():
        text = text.replace("{{" + key + "}}", val)
    return text


def run(cmd: List[str], cwd: Path, env: Dict[str, str], timeout: int, stdin: Optional[str] = None) -> Tuple[int, str, str]:
    """Run in its own process group so a timeout also kills the CLI's children."""
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, text=True, start_new_session=True,
                            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
        out, err = proc.communicate()
        return 124, out, (err or "") + f"\n[run_job] timed out after {timeout}s"


def resolve_cmd(command: str) -> List[str]:
    """`bin/foo.py args` -> absolute path inside the repo; other commands are left as-is.

    Python helpers run with this interpreter (macOS /usr/bin/python3), which is where
    youtube_transcript_api is installed; Homebrew's python3 comes first on PATH and lacks it.
    """
    parts = shlex.split(command)
    if (ROOT / parts[0]).exists():
        parts[0] = str(ROOT / parts[0])
        if parts[0].endswith(".py"):
            parts.insert(0, sys.executable)
    return parts


def load_messages(path: Path) -> List[Dict]:
    if not path.exists():
        raise JobError("LLM이 messages.json을 만들지 않았습니다")
    try:
        msgs = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise JobError(f"messages.json 파싱 실패: {exc}")
    if not isinstance(msgs, list):
        raise JobError("messages.json이 배열이 아닙니다")
    for i, msg in enumerate(msgs):
        if not isinstance(msg, dict) or not str(msg.get("content") or "").strip():
            raise JobError(f"messages.json[{i}]에 content가 없습니다")
        if str(msg.get("channel")) not in CONFIG["channels"] and not str(msg.get("channel")).isdigit():
            raise JobError(f"messages.json[{i}]의 채널 별칭이 잘못됐습니다: {msg.get('channel')}")
    return msgs


def pre_skip(pre_out: str) -> bool:
    """A pre step can print {"skip": true, ...} to say there is nothing new (saves an LLM call)."""
    try:
        data = json.loads(pre_out)
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("skip") is True


def prune(job_runs: Path, keep: int) -> None:
    runs = sorted(p for p in job_runs.iterdir() if p.is_dir())
    for old in runs[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job")
    ap.add_argument("--dry-run", action="store_true", help="run everything but do not post or run `after`")
    ap.add_argument("--engine", choices=sorted(engines.ENGINES), help="override the job's engine")
    args = ap.parse_args()

    meta, body = parse_job(args.job)
    for d in (STATE, DATA, LOGS, RUNS / args.job):
        d.mkdir(parents=True, exist_ok=True)

    lock = open(STATE / f".{args.job}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"{args.job}: previous run still in progress, skipping")
        return 0

    now = datetime.now(KST)
    run_dir = RUNS / args.job / now.strftime("%Y-%m-%d_%H%M%S")
    run_dir.mkdir(parents=True)
    log = open(run_dir / "run.log", "a", encoding="utf-8")

    def note(msg: str) -> None:
        line = f"{datetime.now(KST):%Y-%m-%d %H:%M:%S} {msg}"
        log.write(line + "\n")
        log.flush()
        print(line)

    state_dir, data_dir = STATE, DATA
    if args.dry_run:
        # The LLM appends to history files and the tracker; a dry run must not leave traces there.
        state_dir, data_dir = run_dir / "dry-state", run_dir / "dry-data"
        shutil.copytree(STATE, state_dir, ignore=shutil.ignore_patterns(".*.lock"))
        shutil.copytree(DATA, data_dir, ignore=shutil.ignore_patterns("kakao"))  # skip bulky raw Kakao dumps
        note(f"dry-run: using copies {state_dir} / {data_dir}")

    env = dict(os.environ)
    env["PATH"] = ":".join(os.path.expanduser(p) for p in CONFIG["path"])
    env.update(engines.credentials())
    env.update({"RUN_DIR": str(run_dir), "STATE_DIR": str(state_dir), "DATA_DIR": str(data_dir),
                "MACMINI_AGENT_ROOT": str(ROOT)})
    ctx = {"RUN_DIR": str(run_dir), "STATE_DIR": str(state_dir), "DATA_DIR": str(data_dir), "ROOT": str(ROOT),
           "TODAY": now.strftime("%Y-%m-%d"), "WEEKDAY_EMOJI": WEEKDAY_EMOJI[now.weekday()],
           "CHANNELS": ", ".join(CONFIG["channels"])}
    engine = args.engine or meta.get("engine", "codex")
    status = "ok"

    try:
        pre_out = ""
        if meta.get("pre"):
            note(f"pre: {meta['pre']}")
            rc, pre_out, pre_err = run(resolve_cmd(meta["pre"]), run_dir, env, int(meta.get("pre_timeout", 900)))
            (run_dir / "pre.json").write_text(pre_out, encoding="utf-8")
            (run_dir / "pre.err").write_text(pre_err, encoding="utf-8")
            if rc != 0:
                raise JobError(f"pre 단계 실패 (exit {rc}): {(pre_out + pre_err).strip()[-400:]}")

        if pre_skip(pre_out):
            note("pre reported nothing to do; skipping the LLM")
            messages: List[Dict] = []
        elif engine == "none":
            messages = [{"channel": meta["notify"], "content": pre_out.strip()}] if pre_out.strip() and meta.get("notify") else []
        else:
            header = (f"# 실행 정보 (macmini_agent가 자동으로 붙임)\n"
                      f"- 현재 시각: {now:%Y-%m-%d} ({WEEKDAY_KO[now.weekday()]}) {now:%H:%M} KST\n"
                      f"- 작업 폴더 RUN_DIR: {run_dir} (임시 파일은 여기에)\n"
                      f"- 상태 폴더 STATE_DIR: {state_dir}\n- 데이터 폴더 DATA_DIR: {data_dir}\n")
            if meta.get("pre"):
                header += f"- 사전 수집 결과: {run_dir}/pre.json\n"
            contract = CARDS_CONTRACT if meta.get("cards") else OUTPUT_CONTRACT
            prompt = render(header + "\n" + body.strip() + "\n" + contract, ctx)
            (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
            if engine not in engines.ENGINES:
                raise JobError(f"unknown engine: {engine}")
            note(f"engine: {engine} (search={bool(meta.get('search'))})")
            cmd = engines.ENGINES[engine].build(run_dir, None, bool(meta.get("search")), [state_dir, data_dir])
            rc, out, err = run(cmd, run_dir, env, int(meta.get("timeout", 1800)), stdin=prompt)
            (run_dir / "engine.log").write_text(out + "\n--- stderr ---\n" + err, encoding="utf-8")
            try:
                engines.ENGINES[engine].parse(out)  # the engine's own error message (e.g. not logged in), if any
            except engines.EngineError as exc:
                raise JobError(str(exc))
            if rc != 0:
                raise JobError(f"{engine} 실패 (exit {rc}): {(err or out).strip()[-400:]}")
            messages = card_messages(run_dir, note) if meta.get("cards") else load_messages(run_dir / "messages.json")

        note(f"messages: {len(messages)}")
        if args.dry_run:
            print(json.dumps(messages, ensure_ascii=False, indent=2))
            status = "dry-run"
        else:
            posted = []
            for msg in messages:
                ids = discord_api.send(str(msg["channel"]), str(msg["content"]), msg.get("files"))
                entry = {"channel": msg["channel"], "message_ids": ids}
                if msg.get("react") and ids:
                    try:
                        discord_api.react(str(msg["channel"]), ids[0], str(msg["react"]))
                        entry["react"] = "ok"
                    except Exception as exc:  # the post itself succeeded; keep going
                        entry["react"] = f"failed: {exc}"
                        note(f"react failed on {ids[0]}: {exc}")
                posted.append(entry)
            (run_dir / "posted.json").write_text(json.dumps(posted, ensure_ascii=False, indent=2), encoding="utf-8")
            if meta.get("after"):
                note(f"after: {meta['after']}")
                rc, out, err = run(resolve_cmd(meta["after"]), run_dir, env, 300)
                if rc != 0:
                    raise JobError(f"after 단계 실패 (exit {rc}): {(out + err).strip()[-400:]}")
    except Exception as exc:
        status = "failed"
        reason = str(exc) if isinstance(exc, JobError) else f"{exc.__class__.__name__}: {exc}"
        note(f"FAILED: {reason}")
        if not args.dry_run:
            try:
                discord_api.send(CONFIG["alert_channel"], f"⚠️ `{args.job}` 실패 — {reason[:1500]}\n로그: `{run_dir}`")
            except Exception as alert_exc:
                note(f"alert failed: {alert_exc}")
    finally:
        with open(LOGS / f"{args.job}.log", "a", encoding="utf-8") as summary:
            summary.write(f"{now:%Y-%m-%d %H:%M:%S} {status} {run_dir.name}\n")
        prune(RUNS / args.job, int(CONFIG.get("keep_runs", 30)))

    return 0 if status != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
