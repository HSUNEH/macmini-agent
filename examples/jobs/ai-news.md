---
# Daily AI news as card-news images. Header keys are documented at the top of bin/run_job.py.
schedule: ["07:30"]
engine: codex
search: true
timeout: 1800
cards: true
---
오늘의 AI/Tech 뉴스 Top5를 골라 news 채널로 보내는 작업입니다. 웹 검색으로 직접 찾으세요.

# 선정 기준
- OpenAI, Anthropic, Google DeepMind, Meta AI, Hugging Face 등의 공식 모델 릴리즈, 가격·가용성, API, 중대한 안전·정책 발표를 우선합니다.
- 실제로 써먹을 수 있는 도구·운영 사례를 홍보성 기사보다 우선합니다.
- 발행 24시간 이내 기사만 씁니다. 5개가 안 되면 72시간까지만 넓히고, 그 경우 항목에 발행일을 적습니다. 발행일을 확인할 수 없으면 제외합니다.

# 중복 방지
- `{{STATE_DIR}}/news/ai_news_history.jsonl`(없으면 새로 만듦)에서 최근 3일치와 같은 기사·사건은 제외하세요.
- 고른 항목을 같은 파일에 한 줄씩 추가하세요: `{"date": "{{TODAY}}", "title": "...", "url": "..."}`

# 카드 내용
- title: `오늘의 AI/Tech Top5`, theme: `ai`, channel: `news`
- tag는 주제 분류(예: 모델 출시, 에이전트, 보안), point_label은 `왜 중요`.
- 수치·가격·날짜는 원문에서 확인한 것만 쓰세요.
