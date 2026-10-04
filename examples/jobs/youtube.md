---
# Summarize new videos from the channels in local/config.json "youtube.channels".
# The pre step fetches transcripts; when nothing is new it prints {"skip": true} and the LLM is not called.
schedule: ["07:10"]
engine: codex
timeout: 1800
pre: bin/youtube_collect.py
pre_timeout: 900
after: bin/youtube_collect.py mark-seen
---
지켜보는 YouTube 채널의 새 영상을 요약해 digest 채널로 보내는 작업입니다.

# 입력
`pre.json`의 `selected`에 최근 7일 안에 올라온, 아직 요약하지 않은 영상이 최대 5개 있습니다.
각 항목: channel, title, url, published_at, description, transcript_path(없으면 null).
- transcript_path가 있으면 그 파일을 읽고 요약하세요.
- 없으면 제목과 설명만으로 짧게 쓰고 "자막 없음"이라고 표시하세요. 내용을 지어내지 마세요.

# 보낼 메시지 (digest)
```
📺 YouTube 요약 · {{TODAY}}

1. [채널] 영상 제목 (게시일)
- 한 줄 요약
- 핵심: 3~5개 불릿
- 써먹을 점: 1문장
- <URL>
```
영상마다 메시지를 하나씩 나눠도 됩니다.
