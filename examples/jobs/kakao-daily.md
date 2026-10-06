---
# Optional (KakaoTalk users): summarize open-chat rooms from local/kakao/rooms.yaml and post
# candidates you can approve with ✅. Needs agent-messenger in ~/.macmini-agent/kakao-cli (see README).
schedule: ["07:00"]
engine: codex
search: true
timeout: 1800
pre: bin/kakao_collect.py
pre_timeout: 1200
cards: true
also_messages: true
---
카카오톡 오픈채팅방 데일리 요약을 카드뉴스로 만들고 저장 후보를 만드는 작업입니다.

# 입력
`pre.json`: status, matched_rooms, window_start_at, window_end_at, raw_path, candidate_markdown.
- `candidate_markdown`(시간창 안의 메시지를 방별로 정리한 파일)을 먼저 읽으세요. `raw_path`는 필요할 때만 보세요.
- status가 ok가 아니면 cards.json에는 `items: []`을 쓰고 문제와 조치만 담은 짧은 메시지 1개를 messages.json의 digest로 보내고 끝내세요.

# 선별 기준
- 포함: 중요한 질의응답, AI 모델·툴 출시와 업데이트, 실제 사용법, 유용한 원문 링크, 도구 추천과 설치 명령.
- 제외: 잡담, 감탄, 반복 홍보, 개인정보성 내용. 원문을 길게 인용하거나 개인 이름을 쓰지 마세요.
- 후보 링크는 공식 사이트·GitHub·문서를 우선합니다. 짧은 링크만 있으면 웹 검색으로 원문을 찾으세요.

# 카드와 후보 메시지
1) digest 카드뉴스용 cards.json 1개: channel은 `digest`, title은 `오늘의 카카오 AI방 요약`, theme은 `ai`, emoji는 `🗨️`입니다. 핵심 이슈·유용한 Q&A·실전 사용법을 3~7개 카드 항목으로 정리합니다. tag는 짧은 분류, source는 카카오방 이름, url은 공식 원문이 있을 때만 넣으세요. 개인 이름은 넣지 마세요.
2) messages.json에는 candidates 채널의 후보 메시지만 넣습니다. 각 후보에 `"react": "✅"`와 `"card"`를 붙이세요. card는 제목 `ai_info 후보`, theme `ai`, emoji `✅`, items 1개이며 headline은 후보 이름, tag는 카테고리, summary는 저장 이유·핵심 포인트, point_label은 `언제 쓰나`, point는 실사용처, source는 출처 방, url은 메인 링크입니다. 본문 첫 줄은 `CANDIDATE ✅ 후보 N — {{TODAY}}`로 시작하고 이름, 저장 이유, 핵심 포인트, 메인 링크를 적습니다.
후보가 없으면 messages.json에 `[]`을 쓰고 카드 내용을 텍스트로 중복 발송하지 마세요.
