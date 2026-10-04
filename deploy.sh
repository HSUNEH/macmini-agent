#!/usr/bin/env bash
# Sync this repo to the Mac mini and (re)install its launchd agents.
#   ./deploy.sh              # host "macmini" (Tailscale)
#   HOST=macmini-lan ./deploy.sh
#   ./deploy.sh --restart    # also restart the chat bot (code changes in chat_bot.py / engines.py)
#
# The chat bot can edit ~/macmini_agent itself when asked from Discord. Before overwriting, this
# checks the Mac mini copy against what was last deployed and stops if it changed there; run
# ./pull.sh to bring those edits here first (or FORCE=1 to overwrite them).
set -euo pipefail
HOST="${HOST:-macmini}"
cd "$(dirname "$0")"
MANIFEST='cd ~/macmini_agent 2>/dev/null && find . -type f -not -path "*/__pycache__/*" -not -name .DS_Store | LC_ALL=C sort | xargs shasum'
STAMP='~/.macmini-agent/state/deployed.manifest'

if [[ "${FORCE:-0}" != 1 ]] && ssh "$HOST" "test -f $STAMP"; then
  changed="$(diff <(ssh "$HOST" "cat $STAMP") <(ssh "$HOST" "$MANIFEST") | grep '^[<>]' | awk '{print $3}' | sort -u || true)"
  if [[ -n "$changed" ]]; then
    echo "맥미니의 ~/macmini_agent가 마지막 배포 이후 바뀌었습니다 (Discord에서 고친 것일 수 있음):"
    echo "$changed" | sed 's/^/  /'
    echo "먼저 ./pull.sh 로 가져와 확인하세요. 버리고 덮어쓰려면 FORCE=1 ./deploy.sh"
    exit 1
  fi
fi

rsync -a --delete --exclude .git --exclude __pycache__ --exclude .DS_Store ./ "$HOST:macmini_agent/"
ssh "$HOST" "chmod +x ~/macmini_agent/bin/* && /usr/bin/python3 ~/macmini_agent/bin/install.py $*"
ssh "$HOST" "mkdir -p ~/.macmini-agent/state && ($MANIFEST) > $STAMP"
