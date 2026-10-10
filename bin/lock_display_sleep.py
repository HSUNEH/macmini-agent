#!/usr/bin/env python3
"""Turn off displays five seconds after locking, without putting the Mac to sleep."""
from __future__ import annotations

import argparse
import fcntl
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parent.parent
STATE = Path.home() / '.macmini-agent'
LABEL = 'com.macmini-agent.lock-display-sleep'
LOG = logging.getLogger(LABEL)


def console_locked():
    result = subprocess.run(['/usr/sbin/ioreg', '-n', 'Root', '-d', '1', '-a'],
                            capture_output=True, check=True, timeout=5)
    registry = plistlib.loads(result.stdout)
    if isinstance(registry, list):
        registry = registry[0]
    value = registry.get('IOConsoleLocked')
    if not isinstance(value, bool):
        raise RuntimeError('IOConsoleLocked not available; no display action taken')
    return value


class LockTimer:
    def __init__(self, delay=5.0):
        self.delay = delay
        self.locked_since = None
        self.fired = False

    def update(self, locked, now):
        if not locked:
            self.locked_since = None
            self.fired = False
            return False
        if self.locked_since is None:
            self.locked_since = now
        return not self.fired and now - self.locked_since >= self.delay


def install():
    logs = STATE / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    agent = {
        'Label': LABEL,
        'ProgramArguments': ['/usr/bin/python3', str(Path(__file__).resolve())],
        'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
        'LimitLoadToSessionType': 'Aqua',
        'StandardOutPath': str(logs / 'lock-display-sleep.launchd.log'),
        'StandardErrorPath': str(logs / 'lock-display-sleep.launchd.log'),
    }
    source = ROOT / 'local/launchd' / (LABEL + '.plist')
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(plistlib.dumps(agent))
    dest = Path.home() / 'Library/LaunchAgents' / source.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    domain = 'gui/' + str(os.getuid())
    subprocess.run(['launchctl', 'bootout', domain + '/' + LABEL], capture_output=True)
    subprocess.run(['launchctl', 'bootstrap', domain, str(dest)], check=True)
    print('Installed ' + LABEL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    if args.install:
        install()
        return
    if args.status:
        print('locked=' + str(console_locked()).lower())
        return
    (STATE / 'logs').mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=[
        RotatingFileHandler(STATE / 'logs/lock-display-sleep.log', maxBytes=500000, backupCount=2)])
    lock = (STATE / 'lock-display-sleep.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Already running')
    timer = LockTimer()
    last_error = None
    LOG.info('Started: display off 5 seconds after lock; poll interval 0.5s')
    while True:
        try:
            if timer.update(console_locked(), time.monotonic()):
                # Recheck immediately before the action in case the user just unlocked.
                if console_locked():
                    subprocess.run(['/usr/bin/pmset', 'displaysleepnow'],
                                   capture_output=True, check=True, timeout=5)
                    timer.fired = True
                    LOG.info('Requested display sleep after lock; system sleep unchanged')
                else:
                    timer.update(False, time.monotonic())
            last_error = None
        except Exception as error:
            timer = LockTimer()  # Require a fresh five seconds after observation recovers.
            if str(error) != last_error:
                LOG.error('%s', error)
                last_error = str(error)
            time.sleep(5)
        time.sleep(0.5)


if __name__ == '__main__':
    main()
