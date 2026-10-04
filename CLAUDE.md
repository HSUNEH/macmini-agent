# macmini-agent

공개 저장소(github.com/HSUNEH/macmini-agent)이고, 맥미니의 `~/macmini_agent`가 이 저장소의 git clone이다.

- 코드·구조를 고치면(`bin/`, `examples/`, `deploy.sh`, `README.md` 등) 동작을 확인한 뒤 바로 커밋하고 `git push`한다. 따로 묻지 않는다.
- `local/`은 개인 설정이라 git에 올리지 않는다. 커밋에 Discord ID·토큰·개인 경로·대화 내용이 들어가지 않게 `git diff --cached`로 확인한다.
- `chat_bot.py`·`engines.py`·`orca_session.py`를 고쳤으면 봇 재시작(`!restart`)이 필요하다고 알린다.
- README는 바뀐 동작에 맞춰 같이 고친다.
