#!/usr/bin/env bash
# Sync this repo to the Mac mini and (re)install its launchd agents.
#   ./deploy.sh              # host "macmini" (Tailscale)
#   HOST=macmini-lan ./deploy.sh
#   ./deploy.sh --restart    # also restart the chat bot (code changes in chat_bot.py / engines.py)
#
# The chat bot can edit ~/macmini_agent itself when asked from Discord. The Mac mini copy is a git
# clone too: code changes made there are committed and pushed to GitHub right away, so this stops
# if GitHub has commits you have not pulled. It also checks the Mac mini copy against what was last
# deployed and stops if it changed there without a push (e.g. local/); run ./pull.sh to bring those
# edits here first. FORCE=1 skips both checks.
set -euo pipefail
HOST="${HOST:-macmini}"
cd "$(dirname "$0")"
LIST='find . -type f -not -path "./.git/*" -not -path "*/__pycache__/*" -not -name .DS_Store | LC_ALL=C sort | xargs shasum'
MANIFEST="cd ~/macmini_agent 2>/dev/null && $LIST"
STAMP='~/.macmini-agent/state/deployed.manifest'

if [[ "${FORCE:-0}" != 1 ]] && git fetch -q origin 2>/dev/null && [[ -n "$(git rev-list HEAD..@{u} 2>/dev/null)" ]]; then
  echo "GitHub에 이 폴더에 없는 커밋이 있습니다 (맥미니에서 푸시한 것일 수 있음):"
  git log --oneline HEAD..@{u} | sed 's/^/  /'
  echo "먼저 git pull 하세요."
  exit 1
fi

if [[ "${FORCE:-0}" != 1 ]] && ssh "$HOST" "test -f $STAMP"; then
  # files changed (or deleted) on the Mac mini since the last deploy, unless this folder already matches
  changed="$(awk 'FILENAME == ARGV[1] {s[$2] = $1; next} FILENAME == ARGV[2] {h[$2] = $1; next} {r[$2] = $1}
    END {for (p in r) if (r[p] != s[p] && r[p] != h[p]) print p; for (p in s) if (!(p in r) && (p in h)) print p}' \
    <(ssh "$HOST" "cat $STAMP") <(eval "$LIST") <(ssh "$HOST" "$MANIFEST") | sort)"
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
