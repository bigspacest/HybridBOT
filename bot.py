import time
import discord
from discord.ext import commands
from config import TOKEN

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True


class HybridBOT(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="?",
            intents=intents,
            case_insensitive=True,
            help_command=None,
        )
        self.start_time = None

    async def setup_hook(self):
        from database import ensure_indexes, ensure_panel_meta

        await ensure_indexes()
        await ensure_panel_meta()

        extensions = (
            "security",
            "welcomer",
            "autoroles",
            "moderation",
            "utility",
            "automod",
            "events",
            "scheduler",
            "rolepanels",
            "command_control",
            "error_tracker",
            "cleanup",
        )

        for ext in extensions:
            try:
                await self.load_extension(ext)
                print(f"[load] OK {ext}")
            except Exception as e:
                print(f"[load] FAIL {ext}: {e}")

        try:
            from command_control import build_registry, COMMAND_META
            build_registry(self)
            print(f"[registry] {len(COMMAND_META)} commands registered")
        except Exception as e:
            print(f"[registry] FAIL: {e}")

        try:
            synced = await self.tree.sync()
            print(f"[tree] synced {len(synced)} slash commands")
        except Exception as e:
            print(f"[tree] sync FAIL: {e}")

    async def on_ready(self):
        if self.start_time is None:
            self.start_time = time.time()

        print(f"Logged in as {self.user} ({self.user.id})")
        print(f"Guilds: {len(self.guilds)}")
        print(f"Prefix commands: {len(self.commands)}")
        for cmd in sorted(self.commands, key=lambda c: c.name):
            print(f"  ?{cmd.name}")

        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="HybridBOT | ?cmds",
            )
        )


bot = HybridBOT()
