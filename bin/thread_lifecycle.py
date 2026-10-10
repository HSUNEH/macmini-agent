"""Close a Discord topic without deleting its history."""
import discord


def strike_title(name: str) -> str:
    # Thread names don't render Markdown. Combining strike marks remain plain
    # Unicode text and work in the sidebar; stay under Discord's 100-char limit.
    plain = name.replace("\u0336", "")
    return "".join(char + "\u0336" if not char.isspace() else char for char in plain[:48])


async def leave_and_archive(thread, user, conv):
    if not isinstance(thread, discord.Thread):
        await thread.send("에이전트 연결을 종료했습니다. DM에는 나갈 스레드가 없습니다.")
        return
    perms = thread.permissions_for(thread.guild.me)
    if not perms.manage_threads:
        await thread.send("에이전트는 종료했습니다. 스레드 나가기·종료 표시에는 봇의 ‘스레드 관리’ 권한이 필요합니다.")
        return
    conv.setdefault("original_title", thread.name)
    await thread.send("에이전트를 종료합니다. 이 스레드는 종료 표시 후 나가고 보관합니다. 대화 기록은 남습니다.")
    await thread.edit(name=strike_title(conv["original_title"]), reason="User requested !exit")
    # Membership endpoints require an unarchived thread. Archive last.
    await thread.remove_user(user)
    await thread.leave()
    await thread.edit(archived=True, reason="User requested !exit")
