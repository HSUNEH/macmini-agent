# macmini-agent

구독 중인 **Claude Code / Codex CLI**를 집에 있는 맥(맥미니 등)에서 24시간 돌리는 개인 비서 운영 틀입니다.
에이전트 프레임워크 없이 `launchd` + 작은 파이썬 스크립트 + Discord만 씁니다. API 키는 필요 없습니다.

- **예약 작업:** 매일 정해진 시간에 뉴스 카드, YouTube 요약, 메신저 요약 같은 걸 만들어 Discord로 보냅니다.
- **Discord에서 대화·개발:** 휴대폰에서 메시지를 보내면 맥의 순정 `claude`/`codex`가 그 레포 폴더에서 일하고, 진행 상황이 실시간으로 보입니다.

## 왜 이렇게 만들었나

별도 에이전트 런타임(자체 에이전트 루프, 메모리, 프로필 등)을 두면 순정 CLI보다 작업 품질이 떨어지고, 업데이트 때마다 고장 나서 정작 그걸 고치는 데 시간을 쓰게 됩니다.
여기서는 봇이 **생각하지 않습니다.** 메시지를 받아 `claude -p` / `codex exec`를 그대로 실행하고 결과를 전달할 뿐입니다.
그래서 터미널에서 쓰는 것과 같은 모델, 같은 `CLAUDE.md`·스킬·도구가 그대로 적용됩니다.

```
휴대폰/PC Discord ──▶ chat_bot.py ──▶ cd <레포> && claude -p --resume <세션>   (스레드 하나 = 세션 하나)
                                     └ 진행 상황(읽은 파일·고친 파일·명령)을 메시지 하나에 실시간 갱신

launchd (매일 07:30 등) ──▶ run_job.py <작업>
                            ├ pre: 수집 스크립트 (YouTube 자막, 메신저 메시지 …)
                            ├ LLM: codex exec / claude -p  ← 프롬프트 = local/jobs/<작업>.md
                            └ 결과(messages.json / cards.json)를 Discord로 전송, 실패하면 알림
```

LLM은 Discord 토큰을 모릅니다. 보낼 내용을 파일로 쓰면 스크립트가 보냅니다.

## 기능

| 기능 | 설명 |
|---|---|
| 예약 작업 | `local/jobs/<이름>.md` 파일 하나 = 작업 하나. 위쪽 헤더에 시각·모델, 아래에 프롬프트 |
| 카드뉴스 | `cards: true`인 작업은 LLM이 내용만 쓰고, 템플릿이 1080×1350 PNG로 그려 Discord에 올림 (헤드리스 Chrome). 원문 대표 이미지(Open Graph)와 매체 로고를 자동으로 넣으며, 한글·숫자가 깨지지 않음 |
| 대화 봇 | 채널에 쓰면 스레드가 열리고 스레드마다 CLI 세션 하나. 작업 중 `!stop`, 모델 전환 `!claude`/`!codex`(대화 유지) |
| 프로젝트 개발 | 대화가 등록된 프로젝트 개발이면 모델이 `[프로젝트] 스레드에서 이어가기` 버튼을 달고, 누르면 그 레포 폴더에서 새 세션이 시작됨 |
| Orca 연동 (`backend: orca`) | Claude 대화가 맥의 [Orca](https://github.com/stablyai/orca) 안의 실제 탭으로 열림. 메인 스레드는 메인 워크스페이스 탭, 프로젝트 스레드는 그 레포 탭. Discord에서 하던 대화를 Orca에서 바로 보고 이어 쓸 수 있음 |
| 터미널과 세션 공유 | `!status`가 알려주는 `cd <폴더> && claude --resume <id>`로 맥에서 이어서 작업, 반대로 `!resume <id>` |
| 모델 교체 | `bin/engines.py`에 CLI 하나당 build/parse/progress 함수만 쓰면 새 모델 추가 |
| (선택) YouTube | 지정한 채널의 새 영상 자막을 받아 요약. 새 영상 없으면 LLM 호출 안 함 |
| (선택) 카카오톡 | [agent-messenger](https://github.com/agent-messenger/agent-messenger)로 오픈채팅방을 읽어 요약, ✅ 누른 후보만 저장. 세션이 끊기면 재로그인 코드를 Discord로 보내 휴대폰에서 입력 |

## 준비물

- 늘 켜 두는 macOS 기기 (맥미니 등)와 SSH 접속 (같은 네트워크 또는 Tailscale)
- 그 맥에 로그인된 `claude` (Claude 구독) 그리고/또는 `codex` (ChatGPT 구독)
- Discord 봇 1개: [Developer Portal](https://discord.com/developers/applications)에서 만들고 **Message Content Intent** 켜기. 서버 권한은 메시지 보내기·스레드 만들기·스레드에서 보내기·반응 추가·파일 첨부·메시지 기록 보기
- Google Chrome (카드뉴스용), macOS 기본 `/usr/bin/python3`
- (YouTube 선택) `pip3 install --user youtube-transcript-api`
- (카카오 선택) `bun`, 그리고 `~/.macmini-agent/kakao-cli`에 `npm install agent-messenger`

## 설치

노트북(작업용 PC)에서:

```bash
git clone https://github.com/HSUNEH/macmini-agent.git && cd macmini-agent
mkdir -p local && cp -R examples/jobs examples/chat examples/kakao examples/main local/
cp examples/config.example.json local/config.json
```

`local/`은 git에 올라가지 않는 **내 설정** 폴더입니다. 여기를 고칩니다.

1. `local/config.json`
   - `chat.channel`: 대화용 Discord 채널 ID, `chat.users`: 내 Discord 사용자 ID (이 사람 말에만 반응)
   - `channels`: 예약 작업이 보낼 곳(채널이나 스레드 ID)에 별칭 붙이기. 작업 프롬프트는 이 별칭을 씀
   - `path`: 맥에서 `claude`·`codex`·`bun`이 있는 경로
   - `chat.projects`: Discord에서 개발할 레포와 별칭
2. `local/jobs/*.md`: 필요 없는 예시는 지우고, 프롬프트를 내 취향대로
3. `local/chat/PROMPT.md`: 일반 대화의 기본 지침

맥에 설치하고 비밀값 넣기 (값은 화면과 기록에 남지 않음):

```bash
./deploy.sh                    # 처음 한 번: 코드 복사 + launchd 등록 (HOST=<ssh 호스트>, 기본 macmini)
ssh -t macmini '~/macmini_agent/bin/set_secret.sh DISCORD_BOT_TOKEN'
# 카카오를 쓰면 KAKAO_TALK_EMAIL, KAKAO_TALK_PASSWORD 도
./deploy.sh --restart          # 대화 봇 재시작
```

확인:

```bash
ssh macmini '~/macmini_agent/bin/run_job.py ai-news --dry-run'   # Discord로 보내지 않고 결과만 출력
```

> SSH 세션에서는 macOS 키체인이 잠겨 있어서 `claude`가 로그아웃으로 보일 수 있습니다. launchd로 도는 봇과 예약 작업은 GUI 세션이라 상관없습니다. SSH에서도 쓰려면 `claude setup-token`으로 받은 토큰을 `set_secret.sh CLAUDE_CODE_OAUTH_TOKEN`으로 넣으세요.

## Orca 연동 (선택)

맥에 Orca가 켜져 있으면 `local/config.json`의 `chat.backend`를 `"orca"`로 바꾸세요. `chat.workdir`(예: `~/assistant`)가 메인 워크스페이스가 됩니다.

```
Discord 메인 채널에 글   → 새 스레드  ⇄  Orca [assistant › main] 새 Claude 탭  (메인 = 전체 관장, memory/ 에 장기기억)
대화 중 프로젝트 감지    → "redbox 프로젝트로 감지됐어요! 새 세션으로 이어갈까요?" [이어가기]
                           → 새 스레드 ⇄  Orca [redbox › main] 새 Claude 탭     (그 레포의 CLAUDE.md·스킬)
```

- 탭은 `claude --dangerously-skip-permissions --session-id <id>`로 열리고, 봇은 Orca CLI로 메시지를 입력한 뒤 세션 기록 파일을 읽어 진행 상황과 답을 Discord로 보냅니다.
- 탭을 닫거나 Orca가 재시작돼도 다음 메시지에서 `--resume`으로 같은 세션을 다시 엽니다. Orca에서 직접 이어 쓴 내용도 같은 세션에 남습니다.
- `./deploy.sh`가 메인 폴더(`git init` 포함)와 프로젝트를 Orca 워크스페이스로 등록하고, Claude의 폴더 신뢰 확인을 미리 처리합니다. 메인 폴더의 `CLAUDE.md`는 `local/main/CLAUDE.md`(예시: `examples/main/`)로 관리합니다.
- 지금은 Claude만 Orca 탭으로 돕니다. `!codex`는 백그라운드 `codex exec`로 실행됩니다.

## 작업 파일 형식

```markdown
---
schedule: ["07:30", "17:00"]   # launchd 실행 시각
engine: codex                  # codex | claude | none(pre만 실행)
search: true                   # 웹 검색
timeout: 1800
cards: true                    # 카드뉴스로 보내기 (없으면 messages.json 텍스트)
pre: bin/youtube_collect.py    # LLM 전에 실행, 출력은 RUN_DIR/pre.json. {"skip": true}를 내면 LLM 생략
after: bin/youtube_collect.py mark-seen   # 전송 성공 후 실행
notify: digest                 # engine: none일 때 pre 출력을 보낼 곳
---
여기부터 프롬프트. {{TODAY}}, {{WEEKDAY_EMOJI}}, {{RUN_DIR}}, {{STATE_DIR}}, {{DATA_DIR}}, {{ROOT}} 치환.
```

출력 규칙(보낼 메시지를 `messages.json`/`cards.json`에 쓰는 법)은 `run_job.py`가 프롬프트 끝에 자동으로 붙입니다.
새 작업을 추가하거나 시각을 바꾸면 `./deploy.sh`만 하면 됩니다.

## Discord 명령

| 명령 | 동작 |
|---|---|
| (그냥 쓰기) | 채널에 쓰면 새 스레드 + 새 세션, 스레드 안에 쓰면 이어서 |
| `!<프로젝트> 할 일` | 그 레포 폴더의 새 세션으로 시작 (`!home`은 일반 대화로) |
| `!claude` / `!codex` | 모델 전환. 최근 대화를 넘겨줘서 맥락 유지 |
| `!effort <단계>` | 이 스레드의 effort: `low`·`medium`·`high`·`xhigh`·`max`, `default`면 해제. 대화는 이어짐 (claude는 `--effort`, codex는 `model_reasoning_effort`) |
| `!new` · `!stop` · `!status` | 새 세션 · 실행 중단 · 상태와 터미널 이어가기 명령 |
| `!resume <세션ID>` | 터미널에서 하던 세션을 이 스레드로 |
| `!restart` | 진행 중 작업이 끝나면 봇 재시작, 다시 켜지면 알림 |
| `!kakao` | (카카오) 재로그인 코드를 이 스레드로 받기 |

봇은 확인 창 없이 실행됩니다(`bypassPermissions` / `--dangerously-bypass-approvals-and-sandbox`). Discord에서는 "허용할까요?"에 답할 방법이 없기 때문입니다. `chat.users`에 본인만 넣고, 되돌리기 어려운 일은 먼저 묻도록 `PROMPT.md`에 적어 두세요.

## 운영

- **배포:** `./deploy.sh`는 맥의 `~/macmini_agent`로 rsync 후 launchd를 다시 등록합니다.
- **맥에서 고친 코드:** 맥의 `~/macmini_agent`도 이 저장소의 git clone입니다. Discord에서 봇에게 자기 코드를 고치게 하면 그 자리에서 커밋하고 GitHub에 바로 푸시합니다(`CLAUDE.md`). 그래서 노트북에서는 `git pull` 후 배포하면 되고, 받지 않은 커밋이 있으면 `deploy.sh`가 멈춥니다. 푸시되지 않은 변경(예: `local/`)이 맥에 있으면 덮어쓰지 않고 멈추니 `./pull.sh`로 가져와 확인하세요.
- **런타임 데이터:** 맥의 `~/.macmini-agent/` — `.env`(비밀값), `state/`(중복 방지 기록, 대화 세션), `data/`, `runs/<작업>/<시각>/`(프롬프트·로그·결과, 최근 30개), `logs/`
- **LLM과 무관한 상주 작업:** `local/launchd/*.plist`에 두면 그대로 설치됩니다.
- **모델이 막히면:** 구독 CLI의 기본 모델이 계정에서 막히는 경우가 있습니다. 실패 알림이 오면 맥의 `~/.codex/config.toml`(또는 claude 설정)에서 모델을 바꾸세요.

## 구조

```
bin/run_job.py        예약 작업 실행기
bin/chat_bot.py       Discord 대화 봇 (launchd 상주, discord.py)
bin/engines.py        CLI 정의: codex, claude
bin/orca_session.py   Orca 탭에서 claude 세션 운영 (backend: orca)
bin/cards.py          카드뉴스 렌더러
bin/discord_api.py    Discord REST (전송, 첨부, 반응, ✅ 승인 조회)
bin/install.py        launchd 등록 (맥에서 실행됨)
bin/youtube_collect.py, bin/kakao_*   선택 기능 수집기
examples/             local/로 복사해서 시작하는 예시 설정
deploy.sh, pull.sh    배포 / 맥에서 바뀐 코드 가져오기
local/                내 설정 (git 제외)
```

## License

MIT
