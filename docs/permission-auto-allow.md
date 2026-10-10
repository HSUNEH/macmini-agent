# macOS 권한 팝업 자동 허용

`bin/permission_auto_allow.py`는 Orca의 접근성 인터페이스로 macOS 시스템 권한창을 확인하고 `Allow`/`허용`을 누릅니다. 기본 간격은 2초이고, 로그인 후 launchd로 계속 실행됩니다. Orca가 실행 중이고 접근성 권한이 있어야 합니다. 잠금 화면이나 접근성으로 보이지 않는 창에서는 동작을 보장하지 않습니다.

```sh
python3 bin/permission_auto_allow.py --install
```

첫 설치 시 `local/permission-auto-allow.json`을 만듭니다. 기본값은 Orca 앱만 대상으로 하며, 모든 앱을 허용하려면 `apps`를 `["*"]`로 바꾸세요. 설정은 매 주기 다시 읽으므로 재시작하지 않아도 반영됩니다.

```json
{
  "enabled": true,
  "apps": ["*"],
  "resources": ["microphone", "camera", "contacts", "calendar", "reminders", "photos", "bluetooth", "speech", "files", "notifications", "local_network"],
  "poll_seconds": 2,
  "orca": "/absolute/path/to/orca"
}
```

- `files`: 바탕화면·문서·다운로드 폴더, 외장 및 네트워크 볼륨의 정해진 요청 문구.
- macOS `UserNotificationCenter`/`CoreServicesUIAgent`의 시스템 대화상자만 처리합니다. 요청 제목, 권한 종류, 허용·거부 버튼이 모두 맞아야 클릭합니다.
- 영어 요청 문구와 일부 한국어 접근 요청 문구를 지원합니다. OS 버전이나 문구가 다르면 건너뜁니다. 모든 권한 UI를 지원하는 것은 아닙니다.
- 키체인·관리자 암호·결제·삭제·앱 제어(Automation)·전체 디스크 접근·보안 설정 변경은 자동 처리하지 않습니다. 앱이나 웹페이지 내부의 Allow 버튼도 처리하지 않습니다.
- 클릭 직전 같은 창을 다시 읽습니다. 클릭 뒤 팝업이 사라지는지 확인해 로그에 남깁니다. 이는 대화상자 처리 확인이며 권한 데이터베이스 검증은 아닙니다.
- 확인 시 스크린샷을 찍거나 창을 앞으로 가져오지 않습니다. 실제 버튼 조작은 화면을 깨울 수 있습니다.

로그는 `~/.macmini-agent/logs/permission-auto-allow.log`에 기록하고 크기에 따라 회전합니다. 중지하려면 설정의 `enabled`를 `false`로 바꾸세요. 서비스 등록까지 해제하려면:

```sh
launchctl bootout "gui/$(id -u)/com.macmini-agent.permission-auto-allow"
```

설치 시 생성된 plist는 `local/launchd/`에도 저장되어 기존 배포에 포함됩니다. 개인 설정은 git에 포함되지 않으므로 맥에서 바꿨다면 노트북에서 `./pull.sh`로 가져오세요.

검증:

```sh
python3 -m unittest discover -s tests -p test_permission_auto_allow.py -v
# 상주 서비스를 중지한 상태에서 클릭 없이 인식만 확인
python3 bin/permission_auto_allow.py --once --dry-run
```
