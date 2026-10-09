import discord
from discord.ext import commands
from config import TOKEN

intents = discord.Intents.default()
intents.members = True
intents.message_content = False

class HybridBOT(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents
        )

    async def setup_hook(self):
        # Carga automática de los cogs (archivos en la raíz)
        await self.load_extension("security")
        await self.load_extension("welcomer")
        await self.load_extension("autoroles")
        await self.tree.sync()

    async def on_ready(self):
        print(f"Logged in as {self.user} | {self.user.id}")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="HybridBOT | /help"
            )
        )

bot = HybridBOT()
