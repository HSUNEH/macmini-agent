#!/usr/bin/env bash
# Set KEY in ~/.macmini-agent/.env without echoing the value or leaving it in shell history.
#   ssh -t macmini '~/macmini_agent/bin/set_secret.sh CLAUDE_CODE_OAUTH_TOKEN'
set -euo pipefail
key="${1:?usage: set_secret.sh KEY}"
env_file="$HOME/.macmini-agent/.env"
read -rsp "$key 값을 붙여넣고 Enter: " value
echo
[[ -n "$value" ]] || { echo "빈 값이라 취소합니다."; exit 1; }
umask 077
touch "$env_file"
grep -v "^${key}=" "$env_file" > "$env_file.tmp" || true
printf '%s=%s\n' "$key" "$value" >> "$env_file.tmp"
mv "$env_file.tmp" "$env_file"
chmod 600 "$env_file"
echo "$key 저장 완료 ($env_file)"
