#!/usr/bin/env bun
// Re-register the Mac mini's KakaoTalk tablet slot, phone-only.
//
// When Kakao has dropped the registered device (login status -100), it asks for a code shown on the
// new device to be typed into KakaoTalk on the phone. agent-kakaotalk only prints that code in an
// interactive terminal and waits ~30s; this script sends it to Discord (local/config.json alert_channel) and waits up
// to WAIT_MINUTES, so the whole re-login can be done from the phone.
//
//   bun ~/macmini_agent/bin/kakao_relogin.mjs        (run by kakao_healthcheck.sh, kakao_login.sh, and `!kakao`)
// Exit 0 = logged in (or already was), 1 = failed. Never prints the password or tokens.
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const HOME = homedir();
const KAKAO = join(HOME, '.macmini-agent/kakao-cli/node_modules/agent-messenger/dist/src/platforms/kakaotalk');
const { attemptLogin, requestPasscode, registerDevice, generateDeviceUuid } = await import(join(KAKAO, 'auth/kakao-login.js'));
const { CredentialManager } = await import(join(KAKAO, 'credential-manager.js'));
const ROOT = new URL('..', import.meta.url).pathname;
const WAIT_MINUTES = 5;
const ALERT = JSON.parse(readFileSync(join(ROOT, 'local/config.json'), 'utf8')).alert_channel;

function env() {
  const out = {};
  for (const line of readFileSync(join(HOME, '.macmini-agent/.env'), 'utf8').split('\n')) {
    const i = line.indexOf('=');
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim().replace(/^["']|["']$/g, '');
  }
  return out;
}

function notify(text) {
  console.log(text);
  try {
    const channel = process.env.NOTIFY_CHANNEL || ALERT;  // `!kakao` passes the thread it was typed in
    execFileSync('/usr/bin/python3', [join(ROOT, 'bin/discord_api.py'), 'send', channel, text], { stdio: 'ignore' });
  } catch {
    console.log('(Discord 알림 실패)');
  }
}

async function save(creds, result) {
  const now = new Date().toISOString();
  const c = result.credentials;
  await creds.setAccount({
    account_id: c.user_id || 'default', oauth_token: c.access_token, user_id: c.user_id,
    refresh_token: c.refresh_token, device_uuid: c.device_uuid, device_type: c.device_type,
    auth_method: 'login', created_at: now, updated_at: now,
  });
  await creds.setCurrentAccount(c.user_id || 'default');
  await creds.clearPendingLogin();
}

const e = env();
const email = e.KAKAO_TALK_EMAIL || e.KAKAO_EMAIL;
const password = e.KAKAO_TALK_PASSWORD || e.KAKAO_PASSWORD;
const deviceType = e.KAKAO_DEVICE_TYPE || 'tablet';
if (!email || !password) {
  notify('⚠️ 카카오 재로그인: ~/.macmini-agent/.env에 KAKAO_TALK_EMAIL / KAKAO_TALK_PASSWORD가 없습니다.');
  process.exit(1);
}

const creds = new CredentialManager();
const existing = await creds.getAccount();
const deviceUuid = (existing?.auth_method === 'login' && existing?.device_uuid) || generateDeviceUuid();

let result = await attemptLogin(email, password, deviceUuid, deviceType, true);
if (!result.authenticated && result.next_action === 'provide_passcode') {
  const pc = await requestPasscode(email, password, deviceUuid);
  if (!pc.passcode) {
    notify(`⚠️ 카카오 재로그인: 인증 코드를 받지 못했습니다 (${pc.message || pc.error}).`);
    process.exit(1);
  }
  notify(`🔐 카카오 재로그인 코드: **${pc.passcode}**\n휴대폰 카카오톡에 뜨는 입력창에 이 숫자를 넣어 주세요. ${WAIT_MINUTES}분 동안 기다립니다.`);
  const deadline = Date.now() + WAIT_MINUTES * 60_000;
  let reg;
  do {  // registerDevice polls ~10 times per call; keep calling until the phone confirms or time runs out
    reg = await registerDevice(email, password, '', deviceUuid);
  } while (reg.error === 'registration_timeout' && Date.now() < deadline);
  if (reg.error) {
    notify(`⚠️ 카카오 재로그인 실패: ${reg.message || reg.error}. Discord에서 \`!kakao\`로 다시 시도할 수 있습니다.`);
    process.exit(1);
  }
  result = await attemptLogin(email, password, deviceUuid, deviceType, true);
}

if (!result.authenticated) {
  notify(`⚠️ 카카오 재로그인 실패: ${result.message || result.error}`);
  process.exit(1);
}
await save(creds, result);
notify('✅ 카카오 재로그인 완료. 카카오 AI방 수집이 다시 됩니다.');
