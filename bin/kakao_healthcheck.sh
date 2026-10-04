#!/usr/bin/env bash
set -euo pipefail

# KakaoTalk auth watchdog for agent-kakaotalk.
# Quiet on healthy state. Emits stdout only when it relogs in or needs attention.

AGENT_HOME="$HOME/.macmini-agent"
ENV_FILE="$AGENT_HOME/.env"

# Load only the Kakao keys we need instead of sourcing the whole .env.
# The .env may contain lines for unrelated tools; sourcing it could fail
# before the watchdog reaches the Kakao credentials.
if [[ -f "$ENV_FILE" ]]; then
  while IFS= read -r raw || [[ -n "$raw" ]]; do
    line="${raw#"${raw%%[![:space:]]*}"}"
    [[ -z "$line" || "${line:0:1}" == "#" || "$raw" != *"="* ]] && continue
    key="${raw%%=*}"
    key="${key//[[:space:]]/}"
    value="${raw#*=}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ ${#value} -ge 2 ]]; then
      first="${value:0:1}"
      last="${value: -1}"
      if [[ "$first" == "$last" && ( "$first" == "'" || "$first" == '"' ) ]]; then
        value="${value:1:${#value}-2}"
      fi
    fi
    case "$key" in
      KAKAO_AGENT_CLI|KAKAO_HEALTH_ROOM_ID|KAKAO_DEVICE_TYPE|KAKAO_TALK_EMAIL|KAKAO_TALK_PASSWORD|KAKAO_EMAIL|KAKAO_PASSWORD)
        printf -v "$key" '%s' "$value"
        export "$key"
        ;;
    esac
  done < "$ENV_FILE"
fi

CLI="${KAKAO_AGENT_CLI:-$AGENT_HOME/kakao-cli/node_modules/.bin/agent-kakaotalk}"
# agent-kakaotalk resolves dependencies from cwd; run it from $HOME (no node_modules there).
cd "$HOME"
ROOM_ID="${KAKAO_HEALTH_ROOM_ID:-}"  # optional: a room to test-read after re-login
DEVICE_TYPE="${KAKAO_DEVICE_TYPE:-tablet}"
EMAIL="${KAKAO_TALK_EMAIL:-${KAKAO_EMAIL:-}}"
PASSWORD="${KAKAO_TALK_PASSWORD:-${KAKAO_PASSWORD:-}}"

# Kakao's network/socket calls can hang indefinitely. The cron runner has a
# much longer global timeout, so enforce a per-command limit here instead.
# Perl is part of macOS, and exec preserves the CLI's exit status/output.
run_cli_timeout() {
  local seconds="$1"
  shift
  KAKAO_WATCHDOG_TIMEOUT="$seconds" perl -e '
    my $seconds = shift @ARGV;
    my $pid = fork();
    die "fork failed: $!" unless defined $pid;
    if ($pid == 0) {
      setpgrp(0, 0);
      exec @ARGV or die "exec failed: $!";
    }
    $SIG{ALRM} = sub {
      kill "TERM", -$pid;
      kill "KILL", -$pid;
      waitpid($pid, 0);
      print STDERR "kakao watchdog: command timed out after $ENV{KAKAO_WATCHDOG_TIMEOUT}s\\n";
      exit 124;
    };
    alarm $seconds;
    waitpid($pid, 0);
    alarm 0;
    exit(($? & 127) ? 128 + ($? & 127) : ($? >> 8));
  ' "$seconds" "$@"
}

if [[ ! -x "$CLI" ]]; then
  echo "🐾 Kakao auth watchdog: agent-kakaotalk CLI not executable: $CLI"
  exit 1
fi

whoami_out="$(run_cli_timeout 45 "$CLI" whoami --pretty 2>&1 || true)"
if echo "$whoami_out" | grep -q '"user_id"\|"profile"\|"account_id"'; then
  # Healthy: stay silent for no_agent cron.
  exit 0
fi

if [[ -z "$EMAIL" || -z "$PASSWORD" ]]; then
  echo "🐾 Kakao auth watchdog: session unhealthy, but env credentials missing. Set KAKAO_TALK_EMAIL and KAKAO_TALK_PASSWORD in ~/.macmini-agent/.env. whoami: ${whoami_out:0:300}"
  exit 2
fi

# Re-login. If Kakao dropped the device, kakao_relogin.mjs sends the phone code to Discord and waits
# up to 5 minutes; it posts its own success/failure there, so stay quiet here to avoid a duplicate.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PATH="/opt/homebrew/bin:$PATH" bun "$ROOT/bin/kakao_relogin.mjs" >&2 || true

verify_out="$(run_cli_timeout 45 "$CLI" whoami --pretty 2>&1 || true)"
if echo "$verify_out" | grep -q '"user_id"\|"profile"\|"account_id"'; then
  msg_check="$([[ -n "$ROOM_ID" ]] && run_cli_timeout 45 "$CLI" message list "$ROOM_ID" -n 1 --pretty 2>&1 || true)"
  if [[ -n "$ROOM_ID" ]] && echo "$msg_check" | grep -qi 'This socket has been ended\|401\|error'; then
    echo "🐾 Kakao auth watchdog: re-login succeeded, but message check still failed: ${msg_check:0:300}"
    exit 3
  fi
fi
exit 0
