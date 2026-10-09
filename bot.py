import discord
from discord.ext import commands
from config import TOKEN

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

class HybridBOT(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="?",
            intents=intents,
            case_insensitive=True,
            help_command=None
        )

    async def setup_hook(self):
        await self.load_extension("security")
        await self.load_extension("welcomer")
        await self.load_extension("autoroles")
        await self.load_extension("moderation")
        await self.tree.sync()

    async def on_ready(self):
        print(f"Logged in as {self.user} | {self.user.id}")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="HybridBOT | ?cmds"
            )
        )

bot = HybridBOT()
