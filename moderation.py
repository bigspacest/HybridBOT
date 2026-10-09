import discord
from discord.ext import commands
from database import guilds, timed_roles
from security import Security
from datetime import datetime, timedelta, timezone
import asyncio
import re

class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.security = Security(bot)
        self.bot.loop.create_task(self._process_tempbans())

    async def _process_tempbans(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            now = datetime.now(timezone.utc)
            cursor = timed_roles.find({"type": "tempban", "execute_at": {"$lte": now}})
            async for doc in cursor:
                guild = self.bot.get_guild(doc["guild_id"])
                if guild:
                    try:
                        await guild.unban(discord.Object(id=doc["user_id"]), reason="Tempban expired")
                    except (discord.NotFound, discord.Forbidden):
                        pass
                await timed_roles.delete_one({"_id": doc["_id"]})
            await asyncio.sleep(30)

    async def _get_mod_data(self, guild_id: int, user_id: int) -> dict:
        cfg = await guilds.find_one({"_id": guild_id})
        if not cfg:
            return {"warns": [], "notes": []}
        users = cfg.get("moderation", {}).get(str(user_id), {"warns": [], "notes": []})
        return users

    async def _save_mod_data(self, guild_id: int, user_id: int, data: dict):
        await guilds.update_one(
            {"_id": guild_id},
            {"$set": {f"moderation.{user_id}": data}},
            upsert=True
        )

    def _parse_time(self, time_str: str) -> int | None:
        match = re.match(r"^(\d+)([smhd])$", time_str.lower())
        if not match:
            return None
        value, unit = int(match.group(1)), match.group(2)
        multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        return value * multipliers[unit]

    # ─── LOCK / UNLOCK ───────────────────────────────────────────────
    @commands.command(name="lock")
    @commands.has_permissions(manage_channels=True)
    async def lock(self, ctx: commands.Context, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        await ctx.send(f"🔒 {channel.mention} has been locked.")

    @commands.command(name="unlock")
    @commands.has_permissions(manage_channels=True)
    async def unlock(self, ctx: commands.Context, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = None
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        await ctx.send(f"🔓 {channel.mention} has been unlocked.")

    # ─── BAN / TEMPBAN / UNBAN ───────────────────────────────────────
    @commands.command(name="ban")
    @commands.has_permissions(ban_members=True)
    async def ban(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("You cannot ban this member.")
        await member.ban(reason=f"{ctx.author}: {reason}")
        await ctx.send(f"🔨 **{member}** has been banned.\nReason: {reason}")

    @commands.command(name="tempban")
    @commands.has_permissions(ban_members=True)
    async def tempban(self, ctx: commands.Context, member: discord.Member, time: str, *, reason: str = "No reason provided"):
        seconds = self._parse_time(time)
        if seconds is None:
            return await ctx.send("Invalid time format. Use `10m`, `2h`, `1d`, etc.")
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("You cannot ban this member.")

        await member.ban(reason=f"{ctx.author}: {reason} | Duration: {time}")
        execute_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        await timed_roles.insert_one({
            "type": "tempban",
            "guild_id": ctx.guild.id,
            "user_id": member.id,
            "execute_at": execute_at
        })
        await ctx.send(f"⏳ **{member}** has been temporarily banned for `{time}`.\nReason: {reason}")

    @commands.command(name="unban")
    @commands.has_permissions(ban_members=True)
    async def unban(self, ctx: commands.Context, user_id: int):
        try:
            user = await self.bot.fetch_user(user_id)
            await ctx.guild.unban(user)
            await timed_roles.delete_many({"type": "tempban", "guild_id": ctx.guild.id, "user_id": user_id})
            await ctx.send(f"✅ **{user}** has been unbanned.")
        except discord.NotFound:
            await ctx.send("User not found or not banned.")

    # ─── WARN SYSTEM ─────────────────────────────────────────────────
    @commands.command(name="warn")
    @commands.has_permissions(moderate_members=True)
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        data = await self._get_mod_data(ctx.guild.id, member.id)
        warn_id = len(data["warns"]) + 1
        data["warns"].append({
            "id": warn_id,
            "reason": reason,
            "moderator": ctx.author.id,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        await self._save_mod_data(ctx.guild.id, member.id, data)
        await ctx.send(f"⚠️ **{member}** has been warned (#{warn_id}).\nReason: {reason}")

    @commands.command(name="delwarn")
    @commands.has_permissions(moderate_members=True)
    async def delwarn(self, ctx: commands.Context, member: discord.Member, warn_id: int):
        data = await self._get_mod_data(ctx.guild.id, member.id)
        original = len(data["warns"])
        data["warns"] = [w for w in data["warns"] if w["id"] != warn_id]
        if len(data["warns"]) == original:
            return await ctx.send("Warn ID not found.")
        # Re-number
        for i, w in enumerate(data["warns"], 1):
            w["id"] = i
        await self._save_mod_data(ctx.guild.id, member.id, data)
        await ctx.send(f"✅ Warn #{warn_id} removed from **{member}**.")

    @commands.command(name="warnings")
    async def warnings(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        data = await self._get_mod_data(ctx.guild.id, member.id)
        if not data["warns"]:
            return await ctx.send(f"**{member}** has no warnings.")

        embed = discord.Embed(title=f"Warnings — {member}", color=0xFEE75C)
        for w in data["warns"]:
            embed.add_field(
                name=f"#{w['id']}",
                value=f"**Reason:** {w['reason']}\n**By:** <@{w['moderator']}>\n**Date:** {w['timestamp'][:10]}",
                inline=False
            )
        await ctx.send(embed=embed)

    # ─── NOTES SYSTEM ────────────────────────────────────────────────
    @commands.command(name="noteadd")
    @commands.has_permissions(moderate_members=True)
    async def noteadd(self, ctx: commands.Context, member: discord.Member, *, note: str):
        data = await self._get_mod_data(ctx.guild.id, member.id)
        note_id = len(data["notes"]) + 1
        data["notes"].append({
            "id": note_id,
            "content": note,
            "moderator": ctx.author.id,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        await self._save_mod_data(ctx.guild.id, member.id, data)
        await ctx.send(f"📝 Note #{note_id} added to **{member}**.")

    @commands.command(name="removenote")
    @commands.has_permissions(moderate_members=True)
    async def removenote(self, ctx: commands.Context, member: discord.Member, note_id: int):
        data = await self._get_mod_data(ctx.guild.id, member.id)
        original = len(data["notes"])
        data["notes"] = [n for n in data["notes"] if n["id"] != note_id]
        if len(data["notes"]) == original:
            return await ctx.send("Note ID not found.")
        for i, n in enumerate(data["notes"], 1):
            n["id"] = i
        await self._save_mod_data(ctx.guild.id, member.id, data)
        await ctx.send(f"✅ Note #{note_id} removed from **{member}**.")

    @commands.command(name="viewnotes")
    @commands.has_permissions(moderate_members=True)
    async def viewnotes(self, ctx: commands.Context, member: discord.Member):
        data = await self._get_mod_data(ctx.guild.id, member.id)
        if not data["notes"]:
            return await ctx.send(f"**{member}** has no notes.")

        embed = discord.Embed(title=f"Notes — {member}", color=0x57F287)
        for n in data["notes"]:
            embed.add_field(
                name=f"#{n['id']}",
                value=f"{n['content']}\n**By:** <@{n['moderator']}> • {n['timestamp'][:10]}",
                inline=False
            )
        await ctx.send(embed=embed)

    # ─── INFO COMMANDS ───────────────────────────────────────────────
    @commands.command(name="userinfo")
    async def userinfo(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        roles = [r.mention for r in member.roles if r != ctx.guild.default_role]
        embed = discord.Embed(title=f"User Info — {member}", color=member.color or 0x2b2d31)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=member.id, inline=True)
        embed.add_field(name="Nickname", value=member.nick or "None", inline=True)
        embed.add_field(name="Account Created", value=discord.utils.format_dt(member.created_at, "R"), inline=False)
        embed.add_field(name="Joined Server", value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "Unknown", inline=False)
        embed.add_field(name=f"Roles [{len(roles)}]", value=" ".join(roles[:15]) or "None", inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="serverinfo")
    async def serverinfo(self, ctx: commands.Context):
        guild = ctx.guild
        embed = discord.Embed(title=f"Server Info — {guild.name}", color=0x5865F2)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="Owner", value=guild.owner.mention if guild.owner else "Unknown", inline=True)
        embed.add_field(name="Members", value=guild.member_count, inline=True)
        embed.add_field(name="Channels", value=len(guild.channels), inline=True)
        embed.add_field(name="Roles", value=len(guild.roles), inline=True)
        embed.add_field(name="Boosts", value=guild.premium_subscription_count, inline=True)
        embed.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, "R"), inline=True)
        embed.add_field(name="ID", value=guild.id, inline=False)
        await ctx.send(embed=embed)

    # ─── CMDS PANEL ──────────────────────────────────────────────────
    @commands.command(name="cmds")
    async def cmds(self, ctx: commands.Context):
        embed = discord.Embed(
            title="HybridBOT — Command List",
            description="Prefix: `?`  •  Case-insensitive",
            color=0x5865F2
        )
        embed.add_field(
            name="🛡️ Moderation",
            value=(
                "`?lock` `[channel]` — Lock a channel\n"
                "`?unlock` `[channel]` — Unlock a channel\n"
                "`?ban` `<user> [reason]` — Ban a member\n"
                "`?tempban` `<user> <time> [reason]` — Temporary ban (10m, 2h, 1d)\n"
                "`?unban` `<user_id>` — Unban a user\n"
                "`?warn` `<user> [reason]` — Warn a member\n"
                "`?delwarn` `<user> <id>` — Remove a warn\n"
                "`?warnings` `[user]` — View warnings"
            ),
            inline=False
        )
        embed.add_field(
            name="📝 Notes",
            value=(
                "`?noteadd` `<user> <note>` — Add a staff note\n"
                "`?removenote` `<user> <id>` — Remove a note\n"
                "`?viewnotes` `<user>` — View staff notes"
            ),
            inline=False
        )
        embed.add_field(
            name="ℹ️ Utility",
            value=(
                "`?userinfo` `[user]` — User information\n"
                "`?serverinfo` — Server information\n"
                "`?cmds` — This panel"
            ),
            inline=False
        )
        embed.add_field(
            name="⚙️ Configuration (Slash)",
            value=(
                "`/bot-setup` — Staff hierarchy roles\n"
                "`/welcomer-setup` — Welcome messages\n"
                "`/auto-roles` — Auto-roles panel"
            ),
            inline=False
        )
        embed.set_footer(text="HybridBOT • Manager+ required for config commands")
        await ctx.send(embed=embed)

    # ─── ERROR HANDLER ───────────────────────────────────────────────
    @lock.error
    @unlock.error
    @ban.error
    @tempban.error
    @unban.error
    @warn.error
    @delwarn.error
    @noteadd.error
    @removenote.error
    async def mod_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You lack the required permissions.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"Missing argument: `{error.param.name}`")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("Member not found.")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("Invalid argument.")
        else:
            raise error

async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
