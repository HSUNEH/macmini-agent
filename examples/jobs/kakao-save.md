---
# Optional: save the candidates you approved with ✅ (bot adds one ✅, so count >= 2 = approved).
schedule: ["12:00"]
engine: codex
timeout: 900
pre: bin/discord_api.py selected candidates --marker CANDIDATE
---
오전에 candidates 채널에 올린 후보 중 ✅로 승인된 것만 저장하는 작업입니다.

# 입력
`pre.json`: ok, candidate_messages_seen, selected_count, selected[] (message_id, content).

# 저장
- `{{DATA_DIR}}/saved-tools.md`에 항목마다 `### [YYYY-MM-DD] 이름`, 분류, 핵심 포인트 3~5개, 링크를 추가하세요(없으면 새로 만듦).
- 같은 URL이 이미 있으면 추가하지 말고 기존 항목을 고치세요. 쓴 뒤 다시 읽어 들어갔는지 확인하세요.

# 보낼 메시지 (digest 1개)
`✅ 저장 결과 — {{TODAY}}`: 후보 수 / 승인 수 / 저장 수 / 스킵 사유. 승인이 0개면 한 줄로.
