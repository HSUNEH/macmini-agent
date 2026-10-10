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
| 직접 연결 (`backend: direct`, 기본) | 봇이 `codex app-server`와 Claude Agent SDK에 직접 붙음. 화면 읽기·키 입력 없이 답·질문·플랜 승인을 데이터로 받아 Discord 버튼으로 처리. claude는 `bypassPermissions`(= `--dangerously-skip-permissions`), codex는 승인 없음+샌드박스 없음(yolo)으로 실행. 질문·플랜 승인만 버튼으로 옴. Python 3.10+ 필요(`~/.macmini-agent/chat-venv`) |
| Orca 연동 (`backend: orca`) | Claude·Codex 대화가 맥의 [Orca](https://github.com/stablyai/orca) 안의 실제 탭으로 열림. 메인 스레드는 메인 워크스페이스 탭, 프로젝트 스레드는 그 레포 탭. Discord에서 하던 대화를 Orca에서 바로 보고 이어 쓸 수 있고, 선택 창·플랜 승인·`/mcp` 같은 화면도 Discord에서 버튼으로 조작 |
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

맥에 Orca가 켜져 있으면 `local/config.json`의 `chat.backend`를 `"orca"`로 바꾸세요. `chat.workdir`(예: `~/macmini-discord`)가 메인 워크스페이스가 됩니다.

```
Discord 메인 채널에 글   → 새 스레드  ⇄  Orca [macmini-discord › main] 새 탭  (메인 = 전체 관장, memory/ 에 장기기억)
대화 중 프로젝트 감지    → "redbox 프로젝트로 감지됐어요! 새 세션으로 이어갈까요?" [이어가기]
                           → 새 스레드 ⇄  Orca [redbox › main] 새 Claude 탭     (그 레포의 CLAUDE.md·스킬)
```

- 탭은 `claude --dangerously-skip-permissions --session-id <id>` 또는 `codex --dangerously-bypass-approvals-and-sandbox`로 열립니다. 일반 메시지는 매번 Orca 수신 확인으로 보내고, 진행 중 추가 메시지만 빠른 키 입력으로 보냅니다. 봇은 세션 기록 파일을 읽어 답을 보내며, Codex 기록이 없는 경우에는 전송 전 화면과 다른 완료 답변만 예비 경로로 보냅니다.
- CLI가 답 대신 화면을 띄우면(선택 창, 플랜 승인, `/mcp`·`/model` 메뉴 등) 봇이 탭 화면에서 그 창 부분만 잘라 올리고, 선택지마다 이름 붙은 버튼(`1. 사과` …)과 `↑ ↓ ← → Space`·`Enter Esc ⇧Tab 🔄` 버튼을 붙입니다. 여러 개 고르기는 번호 버튼으로 켜고 끄고, 질문이 여러 개면 `← →`로 넘깁니다. 버튼을 누르면 그 키가 탭에 입력되고, 이어지는 답이나 바뀐 화면이 다시 옵니다. `!screen`으로 언제든 화면을 볼 수 있습니다.
- `/`로 시작하는 메시지(`/compact`, `/mcp`, `/model` …)는 탭에 그대로 입력됩니다. 플랜 모드는 `⇧Tab` 버튼으로 전환합니다.
- 탭을 닫거나 Orca가 재시작돼도 다음 메시지에서 `--resume`으로 같은 세션을 다시 엽니다. Orca에서 직접 이어 쓴 내용도 같은 세션에 남습니다.
- `./deploy.sh`가 메인 폴더(`git init` 포함)와 프로젝트를 Orca 워크스페이스로 등록하고, Claude의 폴더 신뢰 확인을 미리 처리합니다(Codex는 탭을 열 때 처리). 메인 폴더의 `CLAUDE.md`는 `local/main/CLAUDE.md`(예시: `examples/main/`)로 관리합니다.
- `!claude`/`!codex`로 모델을 바꾸면 탭도 그 CLI로 바뀌고, 각 모델의 세션은 따로 이어집니다.
- `!model`은 현재 CLI의 `/model` 화면을 직접 열어 GPT/Claude 세부 모델을 Discord 버튼으로 보냅니다. 10개까지는 개별 버튼, 더 많으면 드롭다운을 사용합니다. 모델 버튼을 누른 다음 effort 등 추가 선택이 나오면 새 버튼을 보내고 기다립니다. Claude/Codex CLI 전환은 `!claude`/`!codex`를 사용하며, 작업 중 전환하면 이전 작업을 중단하고 늦은 답변을 차단합니다.
- 선택 화면은 최대 25개 항목을 버튼/드롭다운으로 보냅니다. 한 번의 클릭은 현재 선택만 처리하며, 다음 선택 화면이 나오면 새 버튼을 보내고 기다립니다. 지난 메시지의 버튼이나 화면이 바뀐 선택지는 적용하지 않습니다.
- 이 동작은 모델 메뉴뿐 아니라 플랜 승인, 질문, 확인창에도 공통 적용됩니다. 다중 선택은 항목을 고른 뒤 `완료`, Tab 이동이 있는 질문은 `이전 질문`/`다음 질문` 버튼으로 처리합니다. 일반 입력창은 선택 화면으로 취급하지 않습니다.
- 버튼이 있는 선택 메시지는 질문과 버튼만 보여주며, 터미널 원본 화면과 중복 선택지 목록은 숨깁니다. 일반 답변·플랜 본문은 선택 버튼과 별도로 전달됩니다.

## 작업 파일 형식

```markdown
---
schedule: ["07:30", "17:00"]   # launchd 실행 시각
engine: codex                  # codex | claude | none(pre만 실행)
search: true                   # 웹 검색
timeout: 1800
cards: true                    # 카드뉴스로 보내기 (없으면 messages.json 텍스트)
also_messages: true            # 카드와 함께 messages.json도 전송 (예: ✅ 승인 후보)
pre: bin/youtube_collect.py    # LLM 전에 실행, 출력은 RUN_DIR/pre.json. {"skip": true}를 내면 LLM 생략
after: bin/youtube_collect.py mark-seen   # 전송 성공 후 실행
notify: digest                 # engine: none일 때 pre 출력을 보낼 곳
---
여기부터 프롬프트. {{TODAY}}, {{WEEKDAY_EMOJI}}, {{RUN_DIR}}, {{STATE_DIR}}, {{DATA_DIR}}, {{ROOT}} 치환.
```

출력 규칙(보낼 메시지를 `messages.json`/`cards.json`에 쓰는 법)은 `run_job.py`가 프롬프트 끝에 자동으로 붙입니다.
`also_messages: true`인 카드 작업은 messages.json 항목에 `card` 객체를 넣어, 승인 반응 등이 필요한 개별 메시지에도 카드 이미지를 붙일 수 있습니다.
새 작업을 추가하거나 시각을 바꾸면 `./deploy.sh`만 하면 됩니다.

## Discord 명령

| 명령 | 동작 |
|---|---|
| (그냥 쓰기) | 채널에 쓰면 새 스레드 + 새 세션, 스레드 안에 쓰면 이어서. 보낸 메시지에 상태 이모지: 🛠️ 처리 중 → ✅ 완료(⚠️ 실패). 작업 중에 보낸 메시지는 바로 탭에 입력돼 진행 중인 작업에 반영됨(📨). 선택 화면 대기 중이거나 헤드리스·`/`명령이면 ⏳를 달고 기다렸다가 보냄 |
| `!<프로젝트> 할 일` | 그 레포 폴더의 새 세션으로 시작 (`!home`은 일반 대화로) |
| `!claude` / `!codex` | 모델 전환. 최근 대화를 넘겨줘서 맥락 유지 |
| `!model` | 현재 CLI의 세부 모델 선택 화면을 바로 Discord 버튼으로 열기 |
| `!effort <단계>` | 이 스레드의 effort: `low`·`medium`·`high`·`xhigh`·`max`, `default`면 해제. 대화는 이어짐 (claude는 `--effort`, codex는 `model_reasoning_effort`) |
| `!new` · `!stop` · `!status` | 새 세션 · 실행 중단 · 상태와 터미널 이어가기 명령 |
| `!exit` | (Orca) 탭에 `/exit`을 보내고 탭을 닫음. 다음 메시지에 같은 세션으로 새 탭이 열림 |
| `/mcp`, `/compact` … | (Orca) 탭에 그대로 입력. 화면이 뜨면 키 버튼과 함께 옴 |
| `!screen` | (Orca) 지금 탭 화면 + 키 버튼 |
| `!resume <세션ID>` | 터미널에서 하던 세션을 이 스레드로 |
| `!restart` | 진행 중 작업이 끝나면 봇 재시작, 다시 켜지면 알림 |
| `!kakao` | (카카오) 재로그인 코드를 이 스레드로 받기 |

봇은 확인 창 없이 실행됩니다(`bypassPermissions` / `--dangerously-bypass-approvals-and-sandbox`). Discord에서는 "허용할까요?"에 답할 방법이 없기 때문입니다. `chat.users`에 본인만 넣고, 되돌리기 어려운 일은 먼저 묻도록 `PROMPT.md`에 적어 두세요.

## macOS 권한 팝업 자동 허용 (선택)

`python3 bin/permission_auto_allow.py --install`로 일반 접근 권한 팝업 감시를 켤 수 있습니다. 대상 앱·권한은 `local/permission-auto-allow.json`에서 설정합니다. [설정과 중지 방법](docs/permission-auto-allow.md)을 참고하세요.

## 잠금 뒤 화면만 끄기 (선택)

`python3 bin/lock_display_sleep.py --install`은 macOS 잠금을 감지하고 5초 뒤 `pmset displaysleepnow`로 디스플레이만 끕니다(감지 간격 0.5초). 잠금 해제 시 타이머를 취소하고, 잠금 한 번당 한 번만 실행하므로 키보드로 화면을 깨워 암호를 입력할 수 있습니다. Mac 본체의 잠자기 설정은 변경하지 않습니다. 디스코드 봇을 계속 실행하려면 본체 잠자기는 별도로 꺼두세요.

로그: `~/.macmini-agent/logs/lock-display-sleep.log`. 상태 확인: `python3 bin/lock_display_sleep.py --status`. 중지: `launchctl bootout "gui/$(id -u)/com.macmini-agent.lock-display-sleep"`. 로그인 시 자동 실행되며, 생성된 `local/launchd/` 설정은 노트북에서 `./pull.sh`로 가져갈 수 있습니다.

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
bin/codex_server.py   codex app-server 직접 연결 (backend: direct)
bin/claude_server.py  Claude Agent SDK 직접 연결 (backend: direct)
bin/discord_native.py 질문·승인·모델 선택을 Discord 버튼으로 (backend: direct)
bin/orca_session.py   Orca 탭에서 claude·codex 세션 운영 (backend: orca)
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
