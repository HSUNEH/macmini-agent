---
# Summarize new videos from the channels in local/config.json "youtube.channels".
# The pre step fetches transcripts; when nothing is new it prints {"skip": true} and the LLM is not called.
schedule: ["07:10"]
engine: codex
timeout: 1800
pre: bin/youtube_collect.py
pre_timeout: 900
after: bin/youtube_collect.py mark-seen
cards: true
---
지켜보는 YouTube 채널의 새 영상을 카드뉴스로 만들어 digest 채널로 보내는 작업입니다.

# 입력
`pre.json`의 `selected`에 최근 7일 안에 올라온, 아직 요약하지 않은 영상이 최대 5개 있습니다.
각 항목: channel, title, url, published_at, description, transcript_path(없으면 null).
- transcript_path가 있으면 그 파일을 읽고 요약하세요.
- 없으면 제목과 설명만으로 짧게 쓰고 "자막 없음"이라고 표시하세요. 내용을 지어내지 마세요.

# 카드 내용
- `cards.json` 하나만 작성하세요. channel은 `digest`, title은 `오늘의 YouTube 요약`, theme은 `ai`, emoji는 `📺`입니다.
- 새 영상마다 카드 항목 하나씩, 최대 5개입니다. headline은 영상 제목을 40자 이내로 다듬고, tag는 `영상 요약`처럼 짧게 씁니다.
- summary에는 한 줄 요약과 핵심 주장·방법·수치·도구를 사실 위주 2문장으로 정리하세요. point_label은 `써먹을 점`, point는 바로 적용할 점 한 문장입니다.
- source는 채널명, url은 영상 URL, date는 게시일(MM/DD)입니다.
