---
# Optional: check the Kakao session before the 07:00 collection. Quiet when healthy; when Kakao has
# dropped the device it posts a code to the alert channel — type it into KakaoTalk on your phone.
schedule: ["06:50"]
engine: none
pre: bin/kakao_healthcheck.sh
pre_timeout: 480
notify: digest
---
