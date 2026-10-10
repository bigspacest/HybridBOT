import time
from datetime import datetime, timezone, timedelta
import discord
from discord.ext import commands
from discord import app_commands
from database import guilds, mod_logs, log_action

# id -> meta
COMMAND_META = {}  # se rellena en setup

LOCKED_IDS = {
    "s:bot-setup",
    "p:cmds",
    "p:help",
}

COG_CATEGORY = {
    "Moderation": "Moderación",
    "Security": "Seguridad",
    "Welcomer": "Bienvenidas",
    "Autoroles": "Roles",
    "Utility": "Utilidad",
    "Automod": "Automod",
    "Events": "Eventos",
    "Scheduler": "Sistema",
    "RolePanels": "Roles",
    "CommandControl": "Sistema",
    "ErrorTracker": "Sistema",
    "Cleanup": "Sistema",
}

# permisos por nombre de comando (prefijo y slash)
PERM_MAP = {
    "ban": "staff", "tempban": "staff", "unban": "manager", "kick": "staff",
    "timeout": "staff", "mute": "staff", "untimeout": "staff", "unmute": "staff",
    "warn": "staff", "delwarn": "staff", "warnings": "staff", "clearwarns": "staff",
    "lock": "staff", "unlock": "staff", "lockdown": "manager", "unlockdown": "manager",
    "purge": "staff", "slowmode": "staff", "nick": "staff", "role": "manager",
    "noteadd": "staff", "removenote": "staff", "viewnotes": "staff",
    "case": "staff", "modlogs": "staff", "banlist": "manager",
    "say": "manager", "announce": "manager",
    "bot-setup": "admin", "welcomer-setup": "admin", "auto-roles": "admin",
}

# caché: guild_id -> {"disabled": set, "notify": bool, "ts": float}
_cache: dict[int, dict] = {}
CACHE_TTL = 30


def invalidate_cache(guild_id: int | None = None):
    if guild_id is None:
        _cache.clear()
    else:
        _cache.pop(int(guild_id), None)


async def get_guild_cmd_cfg(guild_id: int) -> dict:
    now = time.time()
    cached = _cache.get(guild_id)
    if cached and now - cached["ts"] < CACHE_TTL:
        return cached
    doc = await guilds.find_one({"_id": int(guild_id)}, {"disabled_commands": 1, "commands_notify": 1})
    disabled = set(doc.get("disabled_commands") or []) if doc else set()
    notify = bool(doc.get("commands_notify")) if doc and "commands_notify" in doc else True
    entry = {"disabled": disabled, "notify": notify, "ts": now}
    _cache[guild_id] = entry
    return entry


def cmd_id_prefix(name: str) -> str:
    return f"p:{name.lower()}"


def cmd_id_slash(qualified: str) -> str:
    return f"s:{qualified.lower()}"


def build_registry(bot: commands.Bot):
    COMMAND_META.clear()

    for cmd in bot.commands:
        if cmd.hidden:
            continue
        names = [cmd.name] + list(cmd.aliases or [])
        for n in names:
            cid = cmd_id_prefix(n)
            cog_name = cmd.cog.qualified_name if cmd.cog else "General"
            COMMAND_META[cid] = {
                "id": cid,
                "display": f"?{n}",
                "name": n,
                "type": "prefix",
                "category": COG_CATEGORY.get(cog_name, cog_name),
                "description": (cmd.short_doc or cmd.help or "Sin descripción")[:120],
                "permission": PERM_MAP.get(n.lower(), "all"),
                "locked": cid in LOCKED_IDS or n.lower() in ("cmds", "help"),
            }
        # subcomandos de grupos
        if isinstance(cmd, commands.Group):
            for sub in cmd.commands:
                cid = cmd_id_prefix(f"{cmd.name} {sub.name}")
                cog_name = cmd.cog.qualified_name if cmd.cog else "General"
                COMMAND_META[cid] = {
                    "id": cid,
                    "display": f"?{cmd.name} {sub.name}",
                    "name": f"{cmd.name} {sub.name}",
                    "type": "prefix",
                    "category": COG_CATEGORY.get(cog_name, cog_name),
                    "description": (sub.short_doc or sub.help or "Sin descripción")[:120],
                    "permission": PERM_MAP.get(sub.name.lower(), PERM_MAP.get(cmd.name.lower(), "all")),
                    "locked": False,
                }

    for cmd in bot.tree.walk_commands():
        if isinstance(cmd, app_commands.Group):
            continue
        q = cmd.qualified_name
        cid = cmd_id_slash(q)
        cog = getattr(cmd, "binding", None)
        cog_name = cog.qualified_name if cog and hasattr(cog, "qualified_name") else "General"
        COMMAND_META[cid] = {
            "id": cid,
            "display": f"/{q}",
            "name": q,
            "type": "slash",
            "category": COG_CATEGORY.get(cog_name, cog_name),
            "description": (cmd.description or "Sin descripción")[:120],
            "permission": PERM_MAP.get(q.split()[-1].lower(), PERM_MAP.get(q.lower(), "all")),
            "locked": cid in LOCKED_IDS or q.lower() == "bot-setup",
        }


class CommandDisabled(commands.CheckFailure):
    pass


class CommandControl(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_check(self._prefix_check)
        bot.tree.interaction_check = self._slash_check

    async def _prefix_check(self, ctx: commands.Context) -> bool:
        if not ctx.guild or not ctx.command:
            return True
        cid = cmd_id_prefix(ctx.command.qualified_name if hasattr(ctx.command, "qualified_name") else ctx.command.name)
        # también probar solo el nombre
        meta = COMMAND_META.get(cid) or COMMAND_META.get(cmd_id_prefix(ctx.command.name))
        if not meta or meta.get("locked"):
            return True
        cfg = await get_guild_cmd_cfg(ctx.guild.id)
        if meta["id"] not in cfg["disabled"] and cmd_id_prefix(ctx.command.name) not in cfg["disabled"]:
            return True
        if cfg["notify"]:
            msg = await ctx.send("Este comando está desactivado.")
            try:
                await msg.delete(delay=5)
            except Exception:
                pass
        raise CommandDisabled("disabled")

    async def _slash_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not interaction.command:
            return True
        q = interaction.command.qualified_name
        cid = cmd_id_slash(q)
        meta = COMMAND_META.get(cid)
        if not meta or meta.get("locked"):
            return True
        cfg = await get_guild_cmd_cfg(interaction.guild.id)
        if cid not in cfg["disabled"]:
            return True
        if cfg["notify"]:
            if interaction.response.is_done():
                await interaction.followup.send("Este comando está desactivado.", ephemeral=True)
            else:
                await interaction.response.send_message("Este comando está desactivado.", ephemeral=True)
        return False

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context):
        if not ctx.guild or not ctx.command:
            return
        name = ctx.command.qualified_name if hasattr(ctx.command, "qualified_name") else ctx.command.name
        cid = cmd_id_prefix(name)
        if cid not in COMMAND_META:
            cid = cmd_id_prefix(ctx.command.name)
        await log_action(
            ctx.guild.id, "command",
            ctx.author.id, str(ctx.author),
            command_used=f"?{name}",
            channel_id=ctx.channel.id if ctx.channel else None,
            command_id=cid,
            success=True,
            user_id=ctx.author.id,
            user_name=str(ctx.author),
        )

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction: discord.Interaction, command: app_commands.Command):
        if not interaction.guild:
            return
        q = command.qualified_name
        cid = cmd_id_slash(q)
        await log_action(
            interaction.guild.id, "command",
            interaction.user.id, str(interaction.user),
            command_used=f"/{q}",
            channel_id=interaction.channel_id,
            command_id=cid,
            success=True,
            user_id=interaction.user.id,
            user_name=str(interaction.user),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(CommandControl(bot))
    # registry tras cargar todos los cogs
    build_registry(bot)
