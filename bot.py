import discord
from discord.ext import commands
from config import TOKEN
from database import ensure_indexes

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True
intents.moderation = True

class HybridBOT(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="?",
            intents=intents,
            case_insensitive=True,
            help_command=None
        )
        self.start_time = None

    async def setup_hook(self):
        await ensure_indexes()
        for ext in ("security", "welcomer", "autoroles", "moderation", "utility", "automod", "events"):
            await self.load_extension(ext)
        await self.tree.sync()

    async def on_ready(self):
        import time
        if self.start_time is None:
            self.start_time = time.time()
        print(f"Logged in as {self.user} | {self.user.id}")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="HybridBOT | ?cmds"
            )
        )

bot = HybridBOT()
