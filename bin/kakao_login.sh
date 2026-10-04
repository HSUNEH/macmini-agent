#!/usr/bin/env bash
# Re-login the Mac mini's KakaoTalk tablet slot (same as `!kakao` in Discord).
#   ssh macmini '~/macmini_agent/bin/kakao_login.sh'
# A code is shown here and sent to Discord (the alert channel); type it into KakaoTalk on the phone within 5 minutes.
set -euo pipefail
cd "$HOME"   # agent-messenger resolves dependencies from cwd
PATH="/opt/homebrew/bin:$PATH" exec bun "$(cd "$(dirname "$0")/.." && pwd)/bin/kakao_relogin.mjs"
