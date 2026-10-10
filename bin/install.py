#!/usr/bin/env python3
"""Install launchd agents on the Mac mini (idempotent).

- ~/.macmini-agent/venv with requirements.txt (for the chat bot)
- local/skills/<name> symlinked into ~/.claude/skills and ~/.codex/skills
- chat.backend "orca": main workspace + projects registered in Orca and trusted by Claude Code
- local/jobs/<job>.md with a `schedule` -> com.macmini-agent.<job> (calendar job)
- com.macmini-agent.chat -> bin/chat_bot.py (always running)
- local/launchd/*.plist (standalone services) are copied as-is
- com.macmini-agent.* agents that are no longer defined are removed
A plist is reloaded only when its content changed (pass --restart to reload the chat bot anyway).
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
from run_job import parse_job  # noqa: E402

HOME = Path.home() / ".macmini-agent"
AGENTS = Path.home() / "Library" / "LaunchAgents"
LOGS = HOME / "logs"
VENV = HOME / "venv"
CHAT_VENV = HOME / "chat-venv"
PREFIX = "com.macmini-agent."
DOMAIN = f"gui/{subprocess.check_output(['id', '-u'], text=True).strip()}"


def ensure_venv() -> None:
    if not (VENV / "bin" / "python").exists():
        subprocess.run(["/usr/bin/python3", "-m", "venv", str(VENV)], check=True)
    subprocess.run([str(VENV / "bin" / "pip"), "install", "-q", "--disable-pip-version-check",
                    "-r", str(ROOT / "requirements.txt")], check=True)


def direct_chat() -> bool:
    return json.loads((ROOT / "local" / "config.json").read_text()).get("chat", {}).get("backend") == "direct"


def ensure_chat_venv() -> None:
    if not (CHAT_VENV / "bin" / "python").exists():
        candidates = [shutil.which("python3.11"), str(Path.home() / ".local/bin/python3.11"),
                      "/opt/homebrew/bin/python3", shutil.which("python3")]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                probe = subprocess.run([candidate, "-c", "import sys; sys.exit(sys.version_info < (3, 10))"])
                if probe.returncode == 0:
                    subprocess.run([candidate, "-m", "venv", str(CHAT_VENV)], check=True)
                    break
        else:
            raise RuntimeError("직접 연결에는 Python 3.10+가 필요합니다. Python 3.11을 먼저 설치해주세요.")
    subprocess.run([str(CHAT_VENV / "bin/pip"), "install", "-q", "--disable-pip-version-check",
                    "-r", str(ROOT / "requirements-chat.txt")], check=True)


def link_skills() -> None:
    """Expose skills/<name> to both CLIs as symlinks, so a deploy updates them in place."""
    skills = ROOT / "local" / "skills"
    if not skills.is_dir():
        return
    for name in sorted(p.name for p in skills.iterdir() if p.is_dir()):
        for skills_dir in (Path.home() / ".claude" / "skills", Path.home() / ".codex" / "skills"):
            skills_dir.mkdir(parents=True, exist_ok=True)
            dest = skills_dir / name
            if dest.exists() and not dest.is_symlink():
                print(f"skill {name}: {dest} exists and is not ours, skipped")
                continue
            if dest.is_symlink():
                dest.unlink()
            dest.symlink_to(skills / name)
            print(f"skill {name}: linked into {skills_dir}")


def ensure_orca() -> None:
    """chat.backend "orca": the main workspace folder and every project are Orca workspaces that
    Claude Code already trusts, so a tab opened from Discord starts without a dialog."""
    chat = json.loads((ROOT / "local" / "config.json").read_text(encoding="utf-8")).get("chat", {})
    if chat.get("backend") != "orca":
        return
    main = Path(os.path.expanduser(chat.get("workdir", "~")))
    (main / "memory").mkdir(parents=True, exist_ok=True)
    src, dest = ROOT / "local" / "main" / "CLAUDE.md", main / "CLAUDE.md"
    if src.exists() and (dest.is_symlink() or not dest.exists()):
        if dest.is_symlink():
            dest.unlink()
        dest.symlink_to(src)
    if not (main / ".git").exists():  # Orca only takes git repos; history of memory/ is a bonus
        subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
        subprocess.run(["git", "-C", str(main), "commit", "-q", "--allow-empty", "-m", "main workspace"], check=True)
    dirs = [str(main)] + [os.path.expanduser(p["workdir"]) for p in chat.get("projects", {}).values()]

    orca = "/Applications/Orca.app/Contents/Resources/bin/orca"
    listed = subprocess.run([orca, "repo", "list", "--json"], capture_output=True, text=True)
    known = {r.get("path") for r in json.loads(listed.stdout or "{}").get("result", {}).get("repos", [])}
    for d in dirs:
        if d not in known:
            r = subprocess.run([orca, "repo", "add", "--path", d, "--json"], capture_output=True, text=True)
            print(f"orca workspace {d}: {'added' if r.returncode == 0 else 'FAILED ' + r.stdout[-200:]}")

    cfg = Path.home() / ".claude.json"
    data = json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
    projects = data.setdefault("projects", {})
    missing = [d for d in dirs if not projects.get(d, {}).get("hasTrustDialogAccepted")]
    if missing:
        for d in missing:
            projects.setdefault(d, {})["hasTrustDialogAccepted"] = True
        tmp = cfg.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(cfg)
        print(f"claude trust: {', '.join(missing)}")


def job_plist(job: str, schedule: list) -> bytes:
    times = []
    for hhmm in schedule:
        hour, minute = (int(x) for x in hhmm.split(":"))
        times.append({"Hour": hour, "Minute": minute})
    return plistlib.dumps({
        "Label": PREFIX + job,
        "ProgramArguments": ["/usr/bin/python3", str(ROOT / "bin" / "run_job.py"), job],
        "StartCalendarInterval": times,
        "StandardOutPath": str(LOGS / f"launchd-{job}.log"),
        "StandardErrorPath": str(LOGS / f"launchd-{job}.log"),
    })


def chat_plist() -> bytes:
    return plistlib.dumps({
        "Label": PREFIX + "chat",
        "ProgramArguments": [str((CHAT_VENV if direct_chat() else VENV) / "bin" / "python"), "-u", str(ROOT / "bin" / "chat_bot.py")],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(LOGS / "chat.log"),
        "StandardErrorPath": str(LOGS / "chat.log"),
    })


def load(label: str, path: Path, content: bytes, force: bool = False) -> str:
    if path.exists() and path.read_bytes() == content and not force:
        return "unchanged"
    subprocess.run(["launchctl", "bootout", f"{DOMAIN}/{label}"], capture_output=True)
    # bootout returns before a running service has fully exited; bootstrapping too early fails with EIO (5)
    for _ in range(40):
        if subprocess.run(["launchctl", "print", f"{DOMAIN}/{label}"], capture_output=True).returncode != 0:
            break
        time.sleep(0.25)
    path.write_bytes(content)
    subprocess.run(["launchctl", "bootstrap", DOMAIN, str(path)], check=True)
    return "loaded"


def main() -> int:
    restart = "--restart" in sys.argv
    LOGS.mkdir(parents=True, exist_ok=True)
    ensure_venv()
    if direct_chat():
        ensure_chat_venv()
    link_skills()
    ensure_orca()
    wanted = set()
    for md in sorted((ROOT / "local" / "jobs").glob("*.md")):
        meta, _ = parse_job(md.stem)
        if not meta.get("schedule"):
            continue
        label = PREFIX + md.stem
        wanted.add(label)
        print(f"{label}: {load(label, AGENTS / f'{label}.plist', job_plist(md.stem, meta['schedule']))}")

    label = PREFIX + "chat"
    wanted.add(label)
    print(f"{label}: {load(label, AGENTS / f'{label}.plist', chat_plist(), force=restart)}")

    for static in sorted((ROOT / "local" / "launchd").glob("*.plist")):
        label = plistlib.loads(static.read_bytes())["Label"]
        wanted.add(label)
        print(f"{label}: {load(label, AGENTS / static.name, static.read_bytes())}")

    for stale in AGENTS.glob(PREFIX + "*.plist"):
        if stale.stem not in wanted:
            subprocess.run(["launchctl", "bootout", f"{DOMAIN}/{stale.stem}"], capture_output=True)
            stale.unlink()
            print(f"{stale.stem}: removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
