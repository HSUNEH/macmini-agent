#!/usr/bin/env python3
"""Allow configured macOS privacy prompts using Orca's accessibility interface."""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parent.parent
LABEL = 'com.macmini-agent.permission-auto-allow'
CONFIG = ROOT / 'local/permission-auto-allow.json'
STATE = Path.home() / '.macmini-agent'
OWNERS = {'com.apple.UserNotificationCenter', 'com.apple.CoreServicesUIAgent'}
RESOURCES = {
    'microphone': ['microphone', '마이크'],
    'camera': ['camera', '카메라'],
    'contacts': ['contacts', '연락처'],
    'calendar': ['calendar', 'calendars', '캘린더'],
    'reminders': ['reminders', '미리 알림'],
    'photos': ['photos', 'photo library', '사진', '사진 보관함'],
    'bluetooth': ['bluetooth', 'Bluetooth'],
    'speech': ['speech recognition', '음성 인식'],
    'files': ['files in your desktop folder', 'files in your documents folder',
              'files in your downloads folder', 'files on a removable volume',
              'files on a network volume'],
}
DEFAULT = {'enabled': True, 'apps': ['Orca'], 'resources': list(RESOURCES) + ['notifications', 'local_network'],
           'poll_seconds': 2, 'orca': str(Path.home() / '.local/bin/orca')}
LOG = logging.getLogger('permission-auto-allow')


def candidate(snapshot, config):
    """Only accept an OS-owned dialog with one recognized headline and exact buttons."""
    if snapshot.get('app', {}).get('bundleId') not in OWNERS:
        return None
    if snapshot.get('truncation', {}).get('truncated'):
        return None
    tree = snapshot.get('treeText', '')
    if not re.search(r'^\s*\d+ system dialog(?: alert)?$', tree, re.M):
        return None
    buttons = re.findall(r'^\s*(\d+) button (.+)$', tree, re.M)
    yes = [(int(i), label) for i, label in buttons if label in ('Allow', '허용', 'OK', '확인')]
    if len(yes) != 1 or not any(label in ('Don’t Allow', "Don't Allow", '허용 안 함', '허용하지 않음') for _, label in buttons):
        return None
    texts = re.findall(r'^\s*\d+ text (.+)$', tree, re.M)
    matches = []
    for line in texts[:1]:  # The first text is the OS headline; usage descriptions are untrusted.
        app = resource = None
        m = re.fullmatch(r'[“"]([^”"\n]+)[”"] would like to (.+?)[.]?', line, re.I)
        if m:
            app, request = m.groups()
            request = request.rstrip('.').lower()
            for key, names in RESOURCES.items():
                if any(request == 'access ' + prefix + name.lower() for name in names for prefix in ('', 'the ', 'your ')):
                    resource = key
            if request == 'send you notifications':
                resource = 'notifications'
            if request in ('find and connect to devices on your local network', 'find devices on local networks'):
                resource = 'local_network'
        # Korean OS permission headline, not the app-supplied usage description.
        m = re.fullmatch(r'[“"‘\']([^”"’\'\n]+)[”"’\'](?:이|가) (.+?)(?:에 접근하려고 합니다|에 접근하도록 허용하겠습니까)[.?]?', line)
        if m:
            app, name = m.groups()
            for key, names in RESOURCES.items():
                if name in names:
                    resource = key
        if app and resource and resource in config['resources'] and ('*' in config['apps'] or app in config['apps']):
            matches.append((app, resource, yes[0][0]))
    return matches[0] if len(matches) == 1 else None


class Watcher:
    def __init__(self, config):
        self.config = config
        self.attempted = {}
        self.blocked = set()

    def call(self, command, *args):
        p = subprocess.run([self.config['orca'], 'computer', command, *args, '--no-screenshot', '--json'],
                           capture_output=True, text=True, timeout=15)
        data = json.loads(p.stdout)
        if data.get('ok'):
            return data.get('result', {}).get('snapshot', {})
        code = data.get('error', {}).get('code', 'unknown')
        if command == 'get-app-state' and code in ('app_not_found', 'window_not_found', 'window_stale'):
            return {}
        if code == 'app_blocked':
            self.blocked.add(args[args.index('--app') + 1])
        raise RuntimeError(code)

    def scan(self, dry_run=False):
        if not self.config.get('enabled', False):
            return
        p = subprocess.run(['/bin/ps', '-axo', 'pid=,comm='], capture_output=True, text=True, check=True)
        for line in p.stdout.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) != 2 or fields[1] not in (
                '/System/Library/CoreServices/UserNotificationCenter.app/Contents/MacOS/UserNotificationCenter',
                '/System/Library/CoreServices/CoreServicesUIAgent.app/Contents/MacOS/CoreServicesUIAgent',
            ):
                continue
            selector = 'pid:' + fields[0]
            if selector in self.blocked:
                continue
            first = self.call('get-app-state', '--app', selector)
            match = candidate(first, self.config)
            window = first.get('window', {}).get('id')
            if not match or not window:
                continue
            key = (selector, window, match[:2])
            now = time.monotonic()
            self.attempted = {k: v for k, v in self.attempted.items() if now - v < 3600}
            # Do not repeatedly click a stuck prompt on every poll.
            previous = self.attempted.get(key)
            if previous and now - previous < 30:
                continue
            if dry_run:
                LOG.info('would_allow app=%s resource=%s', *match[:2])
                continue
            # Refresh and reclassify the same window immediately before clicking.
            args = ('--app', selector, '--window-id', str(window))
            fresh = self.call('get-app-state', *args)
            target = candidate(fresh, self.config)
            if not target or target[:2] != match[:2] or fresh.get('window', {}).get('id') != window:
                continue
            self.attempted[key] = now
            self.call('click', *args, '--element-index', str(target[2]))
            time.sleep(0.3)
            after = self.call('get-app-state', *args)
            remaining = candidate(after, self.config)
            if remaining and remaining[:2] == target[:2]:
                LOG.warning('allow_unverified app=%s resource=%s', *target[:2])
            else:
                LOG.info('allow_clicked_dialog_closed app=%s resource=%s', *target[:2])


def install(config_path):
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(DEFAULT, indent=2) + '\n')
    logs = STATE / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    agent = {
        'Label': LABEL,
        'ProgramArguments': ['/usr/bin/python3', str(Path(__file__).resolve()), '--config', str(config_path)],
        'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
        'LimitLoadToSessionType': 'Aqua',
        'StandardOutPath': str(logs / 'permission-auto-allow.launchd.log'),
        'StandardErrorPath': str(logs / 'permission-auto-allow.launchd.log'),
    }
    src = ROOT / 'local/launchd' / (LABEL + '.plist')
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(plistlib.dumps(agent))
    dest = Path.home() / 'Library/LaunchAgents' / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    domain = 'gui/' + str(os.getuid())
    subprocess.run(['launchctl', 'bootout', domain + '/' + LABEL], capture_output=True)
    subprocess.run(['launchctl', 'bootstrap', domain, str(dest)], check=True)
    print('Installed ' + LABEL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=CONFIG)
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.install:
        install(args.config.resolve())
        return
    (STATE / 'logs').mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(STATE / 'logs/permission-auto-allow.log', maxBytes=1000000, backupCount=3)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=[handler, logging.StreamHandler()])
    lock = (STATE / 'permission-auto-allow.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Watcher already running')
    watcher = None
    last_error = None
    while True:
        try:
            config = json.loads(args.config.read_text())
            if not isinstance(config.get('apps'), list) or not isinstance(config.get('resources'), list):
                raise ValueError('apps and resources must be lists')
            interval = max(1, float(config.get('poll_seconds', 2)))
            if watcher is None:
                watcher = Watcher(config)
                LOG.info('started apps=%s resources=%s interval=%s', config['apps'], config['resources'], interval)
            watcher.config = config
            watcher.scan(args.dry_run)
            last_error = None
        except Exception as error:
            if str(error) != last_error:
                LOG.error('scan_failed %s', error)
                last_error = str(error)
            if args.once:
                raise
            interval = 10
        if args.once:
            return
        time.sleep(interval)


if __name__ == '__main__':
    main()
