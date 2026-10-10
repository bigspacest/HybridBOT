import os
import re
import traceback
import logging
from datetime import datetime, timezone
import discord
from discord.ext import commands
from discord import app_commands
from database import bot_errors, log_action

SECRET_ENV_KEYS = (
    "DISCORD_TOKEN", "TOKEN", "MONGO_URI", "MONGODB_URI",
    "DISCORD_CLIENT_SECRET", "SESSION_SECRET", "X_BEARER_TOKEN",
)


def scrub(text: str) -> str:
    if not text:
        return text
    out = text
    for key in SECRET_ENV_KEYS:
        val = os.getenv(key)
        if val and len(val) > 4:
            out = out.replace(val, "***")
    return out[:4000]


def fingerprint(exc: BaseException, command: str = "") -> str:
    msg = str(exc).split("\n")[0][:200]
    return f"{type(exc).__name__}|{msg}|{command}"[:300]


async def record_error(
    level: str,
    source: str,
    exc: BaseException,
    command: str = "",
    guild_id: int | None = None,
    user_id: int | None = None,
    user_name: str | None = None,
):
    try:
        tb = scrub("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        message = scrub(str(exc))[:500]
        fp = fingerprint(exc, command)
        now = datetime.now(timezone.utc)
        await bot_errors.update_one(
            {"fingerprint": fp},
            {
                "$set": {
                    "level": level,
                    "source": source,
                    "command": command,
                    "message": message,
                    "traceback": tb,
                    "guild_id": guild_id,
                    "user_id": user_id,
                    "user_name": user_name,
                    "last_seen": now,
                },
                "$setOnInsert": {"first_seen": now, "fingerprint": fp},
                "$inc": {"count": 1},
            },
            upsert=True,
        )
    except Exception:
        pass


class MongoErrorHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        if record.levelno < logging.ERROR:
            return
        try:
            msg = self.format(record)
            exc = record.exc_info[1] if record.exc_info else Exception(msg)
            # fire-and-forget desde hilo de logging es delicado; solo si hay loop
            import asyncio
            from bot import bot
            if bot.loop and bot.loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    record_error("error", "api" if "flask" in record.name.lower() else "tarea",
                                 exc if isinstance(exc, BaseException) else Exception(str(exc)),
                                 command=record.name),
                    bot.loop,
                )
        except Exception:
            pass


EXPECTED = (
    commands.CommandNotFound,
    commands.CheckFailure,
    commands.MissingPermissions,
    commands.BotMissingPermissions,
    commands.UserInputError,
    commands.MissingRequiredArgument,
    commands.BadArgument,
    app_commands.CheckFailure,
    app_commands.CommandOnCooldown,
    app_commands.MissingPermissions,
)


class ErrorTracker(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        handler = MongoErrorHandler()
        handler.setLevel(logging.ERROR)
        logging.getLogger().addHandler(handler)
        logging.getLogger("discord").addHandler(handler)

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        err = getattr(error, "original", error)
        if isinstance(err, EXPECTED) or isinstance(error, EXPECTED):
            return
        # CommandDisabled silencioso
        if type(error).__name__ == "CommandDisabled":
            return
        name = ""
        if ctx.command:
            name = f"?{ctx.command.qualified_name if hasattr(ctx.command, 'qualified_name') else ctx.command.name}"
        await record_error(
            "error", "comando", err, command=name,
            guild_id=ctx.guild.id if ctx.guild else None,
            user_id=ctx.author.id, user_name=str(ctx.author),
        )
        if ctx.guild and ctx.command:
            await log_action(
                ctx.guild.id, "command",
                ctx.author.id, str(ctx.author),
                command_used=name,
                channel_id=ctx.channel.id if ctx.channel else None,
                command_id=f"p:{(ctx.command.name or '').lower()}",
                success=False,
                user_id=ctx.author.id,
                user_name=str(ctx.author),
                reason=scrub(str(err))[:200],
            )

    @commands.Cog.listener()
    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        err = getattr(error, "original", error)
        if isinstance(err, EXPECTED) or isinstance(error, EXPECTED):
            return
        name = f"/{interaction.command.qualified_name}" if interaction.command else ""
        await record_error(
            "error", "comando", err, command=name,
            guild_id=interaction.guild_id,
            user_id=interaction.user.id if interaction.user else None,
            user_name=str(interaction.user) if interaction.user else None,
        )
        if interaction.guild_id and interaction.command:
            await log_action(
                interaction.guild_id, "command",
                interaction.user.id, str(interaction.user),
                command_used=name,
                channel_id=interaction.channel_id,
                command_id=f"s:{interaction.command.qualified_name.lower()}",
                success=False,
                user_id=interaction.user.id,
                user_name=str(interaction.user),
                reason=scrub(str(err))[:200],
            )

    async def cog_load(self):
        # bot.on_error
        async def _on_error(event_method, *args, **kwargs):
            try:
                raise
            except Exception as e:
                await record_error("error", "evento", e, command=event_method)
        self.bot.on_error = _on_error  # type: ignore


async def setup(bot: commands.Bot):
    await bot.add_cog(ErrorTracker(bot))
