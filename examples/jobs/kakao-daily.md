---
# Optional (KakaoTalk users): summarize open-chat rooms from local/kakao/rooms.yaml and post
# candidates you can approve with ✅. Needs agent-messenger in ~/.macmini-agent/kakao-cli (see README).
schedule: ["07:00"]
engine: codex
search: true
timeout: 1800
pre: bin/kakao_collect.py
pre_timeout: 1200
---
카카오톡 오픈채팅방 데일리 요약과 저장 후보를 만드는 작업입니다.

# 입력
`pre.json`: status, matched_rooms, window_start_at, window_end_at, raw_path, candidate_markdown.
- `candidate_markdown`(시간창 안의 메시지를 방별로 정리한 파일)을 먼저 읽으세요. `raw_path`는 필요할 때만 보세요.
- status가 ok가 아니면 문제와 조치만 담은 짧은 메시지 1개를 digest로 보내고 끝내세요.

# 선별 기준
- 포함: 중요한 질의응답, AI 모델·툴 출시와 업데이트, 실제 사용법, 유용한 원문 링크, 도구 추천과 설치 명령.
- 제외: 잡담, 감탄, 반복 홍보, 개인정보성 내용. 원문을 길게 인용하거나 개인 이름을 쓰지 마세요.
- 후보 링크는 공식 사이트·GitHub·문서를 우선합니다. 짧은 링크만 있으면 웹 검색으로 원문을 찾으세요.

# 보낼 메시지
1) digest에 요약 1개: `🗨️ 카카오 데일리 요약 — {{TODAY}}`, 핵심 3~7개, 중요 Q&A, 후보 수.
2) candidates에 후보마다 메시지 1개, 각각 `"react": "✅"`. 첫 줄은 `CANDIDATE ✅ 후보 N — {{TODAY}}`로 시작하고 이름, 저장 이유, 핵심 포인트, 메인 링크를 적습니다.
후보가 없으면 요약에 "후보 없음"이라고 쓰세요.
