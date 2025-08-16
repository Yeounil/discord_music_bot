# bot.py (핵심 부분만)
import os, asyncio, traceback
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.guilds = True

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self._synced = False  # 초기화!

    async def setup_hook(self):
        try:
            await self.load_extension("music")  # music.py가 같은 폴더거나 올바른 패키지 경로
            print("[OK] music extension loaded")
        except Exception:
            print("[ERR] failed to load music extension")
            traceback.print_exc()

        local_cmds = self.tree.get_commands()
        print(f"[INFO] local commands after load: {len(local_cmds)} -> {[c.name for c in local_cmds]}")

bot = MyBot()

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (id={bot.user.id})")

    # (선택) 로컬 트리에 뭐가 올라갔는지 확인
    locals = [c.name for c in bot.tree.get_commands()]
    print(f"[INFO] local commands: {locals}")

    # 각 길드에 글로벌 트리 복사 후, 길드 동기화(=즉시 반영)
    for g in bot.guilds:
        bot.tree.copy_global_to(guild=g)       # ★ 이 줄이 핵심
        synced = await bot.tree.sync(guild=g)  # 길드 즉시 반영
        print(f"[OK] synced {g.name}({g.id}): {[c.name for c in synced]}")

async def main():
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
