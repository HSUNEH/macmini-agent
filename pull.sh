#!/usr/bin/env bash
# Bring edits made on the Mac mini (e.g. by the chat bot from Discord) back into this repo.
#   ./pull.sh                 # host "macmini"; then review with `git diff`
#   HOST=macmini-lan ./pull.sh
set -euo pipefail
HOST="${HOST:-macmini}"
cd "$(dirname "$0")"
echo "맥미니와 다른 파일:"
rsync -anic --exclude .git --exclude __pycache__ --exclude .DS_Store "$HOST:macmini_agent/" ./ \
  | awk '$1 ~ /^[<>*.]f/ && $1 !~ /^\.f\.\.t/ {print "  " $2}'
read -r -p "가져올까요? 이 폴더의 해당 파일을 맥미니 것으로 바꿉니다 [y/N] " ok
[[ "$ok" == y || "$ok" == Y ]] || { echo "취소"; exit 0; }
rsync -a --exclude .git --exclude __pycache__ --exclude .DS_Store "$HOST:macmini_agent/" ./
echo "가져왔습니다. git diff 로 확인한 뒤 ./deploy.sh 하세요."
