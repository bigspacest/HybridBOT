import discord
from discord.ext import commands
from database import guilds, timed_roles, log_action, snipe
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
        return cfg.get("moderation", {}).get(str(user_id), {"warns": [], "notes": []})

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
        return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]

    async def _is_staff(self, member: discord.Member) -> bool:
        cfg = await self.security.get_guild_config(member.guild.id)
        return self.security.has_permission(member, "staff", cfg)

    async def _is_manager(self, member: discord.Member) -> bool:
        cfg = await self.security.get_guild_config(member.guild.id)
        return self.security.has_permission(member, "manager", cfg)

    async def _check_warn_punishment(self, member: discord.Member, ctx: commands.Context):
        cfg = await guilds.find_one({"_id": member.guild.id})
        if not cfg:
            return
        pun = cfg.get("warn_punishment", {})
        threshold = pun.get("threshold")
        action = pun.get("action")
        if not threshold or not action:
            return
        data = await self._get_mod_data(member.guild.id, member.id)
        if len(data["warns"]) < threshold:
            return
        reason = f"Automatic punishment: reached {threshold} warns"
        try:
            if action == "timeout":
                await member.timeout(timedelta(hours=1), reason=reason)
            elif action == "kick":
                await member.kick(reason=reason)
            elif action == "ban":
                await member.ban(reason=reason)
            await log_action(member.guild.id, action, self.bot.user.id, str(self.bot.user),
                             target_id=member.id, target_name=str(member),
                             reason=reason, command_used="warn_punishment", source="discord")
            await ctx.send(f"⚖️ Auto-punishment applied to **{member}**: `{action}` ({threshold} warns).")
        except (discord.Forbidden, discord.HTTPException):
            pass

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context):
        if ctx.command is None or not ctx.guild:
            return
        await log_action(
            guild_id=ctx.guild.id,
            action="command",
            moderator_id=ctx.author.id,
            moderator_name=str(ctx.author),
            command_used=ctx.command.qualified_name,
            channel_id=ctx.channel.id if ctx.channel else None,
            source="discord"
        )

    # ── LOCK / UNLOCK ──
    @commands.command(name="lock")
    @commands.has_permissions(manage_channels=True)
    async def lock(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        await ctx.send(f"🔒 {channel.mention} locked.")
        await log_action(ctx.guild.id, "lock", ctx.author.id, str(ctx.author),
                         channel_id=channel.id, command_used="lock")

    @commands.command(name="unlock")
    @commands.has_permissions(manage_channels=True)
    async def unlock(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = None
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        await ctx.send(f"🔓 {channel.mention} unlocked.")
        await log_action(ctx.guild.id, "unlock", ctx.author.id, str(ctx.author),
                         channel_id=channel.id, command_used="unlock")

    @commands.command(name="lockdown")
    async def lockdown(self, ctx):
        if not await self._is_manager(ctx.author):
            return await ctx.send("Manager+ required.")
        count = 0
        for ch in ctx.guild.text_channels:
            try:
                ow = ch.overwrites_for(ctx.guild.default_role)
                ow.send_messages = False
                await ch.set_permissions(ctx.guild.default_role, overwrite=ow)
                count += 1
            except discord.Forbidden:
                pass
        await ctx.send(f"🔒 Lockdown: {count} channels locked.")
        await log_action(ctx.guild.id, "lockdown", ctx.author.id, str(ctx.author), command_used="lockdown")

    @commands.command(name="unlockdown")
    async def unlockdown(self, ctx):
        if not await self._is_manager(ctx.author):
            return await ctx.send("Manager+ required.")
        count = 0
        for ch in ctx.guild.text_channels:
            try:
                ow = ch.overwrites_for(ctx.guild.default_role)
                ow.send_messages = None
                await ch.set_permissions(ctx.guild.default_role, overwrite=ow)
                count += 1
            except discord.Forbidden:
                pass
        await ctx.send(f"🔓 Unlockdown: {count} channels unlocked.")
        await log_action(ctx.guild.id, "unlockdown", ctx.author.id, str(ctx.author), command_used="unlockdown")

    # ── BAN / TEMPBAN / UNBAN / BANLIST ──
    @commands.command(name="ban")
    @commands.has_permissions(ban_members=True)
    async def ban(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("You cannot ban this member.")
        await member.ban(reason=f"{ctx.author}: {reason}")
        await ctx.send(f"🔨 **{member}** banned.\nReason: {reason}")
        await log_action(ctx.guild.id, "ban", ctx.author.id, str(ctx.author),
                         target_id=member.id, target_name=str(member), reason=reason, command_used="ban")

    @commands.command(name="tempban")
    @commands.has_permissions(ban_members=True)
    async def tempban(self, ctx, member: discord.Member, time: str, *, reason: str = "No reason provided"):
        seconds = self._parse_time(time)
        if seconds is None:
            return await ctx.send("Invalid time. Use `10m`, `2h`, `1d`.")
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("You cannot ban this member.")
        await member.ban(reason=f"{ctx.author}: {reason} | {time}")
        await timed_roles.insert_one({
            "type": "tempban", "guild_id": ctx.guild.id, "user_id": member.id,
            "execute_at": datetime.now(timezone.utc) + timedelta(seconds=seconds)
        })
        await ctx.send(f"⏳ **{member}** tempbanned for `{time}`.\nReason: {reason}")
        await log_action(ctx.guild.id, "tempban", ctx.author.id, str(ctx.author),
                         target_id=member.id, target_name=str(member), reason=reason,
                         duration=time, command_used="tempban")

    @commands.command(name="unban")
    @commands.has_permissions(ban_members=True)
    async def unban(self, ctx, user_id: int):
        try:
            user = await self.bot.fetch_user(user_id)
            await ctx.guild.unban(user)
            await timed_roles.delete_many({"type": "tempban", "guild_id": ctx.guild.id, "user_id": user_id})
            await ctx.send(f"✅ **{user}** unbanned.")
            await log_action(ctx.guild.id, "unban", ctx.author.id, str(ctx.author),
                             target_id=user_id, target_name=str(user), command_used="unban")
        except discord.NotFound:
            await ctx.send("User not found or not banned.")

    @commands.command(name="banlist")
    async def banlist(self, ctx):
        if not await self._is_manager(ctx.author):
            return await ctx.send("Manager+ required.")
        bans = [entry async for entry in ctx.guild.bans(limit=50)]
        if not bans:
            return await ctx.send("No banned users.")
        lines = [f"`{e.user.id}` — **{e.user}** — {e.reason or 'No reason'}" for e in bans]
        embed = discord.Embed(title="Ban List", description="\n".join(lines[:25]), color=0xED4245)
        embed.set_footer(text=f"Showing {min(25, len(bans))} of {len(bans)}")
        await ctx.send(embed=embed)

    # ── KICK ──
    @commands.command(name="kick")
    @commands.has_permissions(kick_members=True)
    async def kick(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("You cannot kick this member.")
        await member.kick(reason=f"{ctx.author}: {reason}")
        await ctx.send(f"👢 **{member}** kicked.\nReason: {reason}")
        await log_action(ctx.guild.id, "kick", ctx.author.id, str(ctx.author),
                         target_id=member.id, target_name=str(member), reason=reason, command_used="kick")

    # ── TIMEOUT / MUTE ──
    @commands.command(name="timeout", aliases=["mute"])
    @commands.has_permissions(moderate_members=True)
    async def timeout(self, ctx, member: discord.Member, time: str, *, reason: str = "No reason provided"):
        seconds = self._parse_time(time)
        if seconds is None or seconds > 2419200:
            return await ctx.send("Invalid time (max 28d). Use `10m`, `2h`, `1d`.")
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("You cannot timeout this member.")
        await member.timeout(timedelta(seconds=seconds), reason=f"{ctx.author}: {reason}")
        await ctx.send(f"🔇 **{member}** timed out for `{time}`.\nReason: {reason}")
        await log_action(ctx.guild.id, "timeout", ctx.author.id, str(ctx.author),
                         target_id=member.id, target_name=str(member), reason=reason,
                         duration=time, command_used="timeout")

    @commands.command(name="untimeout", aliases=["unmute"])
    @commands.has_permissions(moderate_members=True)
    async def untimeout(self, ctx, member: discord.Member):
        await member.timeout(None)
        await ctx.send(f"🔊 **{member}** timeout removed.")
        await log_action(ctx.guild.id, "untimeout", ctx.author.id, str(ctx.author),
                         target_id=member.id, target_name=str(member), command_used="untimeout")

    # ── PURGE ──
    @commands.command(name="purge")
    @commands.has_permissions(manage_messages=True)
    async def purge(self, ctx, amount: int, member: discord.Member = None):
        if amount < 1 or amount > 500:
            return await ctx.send("Amount must be 1–500.")
        def check(m):
            return m.author.id == member.id if member else True
        deleted = await ctx.channel.purge(limit=amount + 1, check=check)
        msg = await ctx.send(f"🗑️ Deleted **{len(deleted) - 1}** messages.", delete_after=5)
        await log_action(ctx.guild.id, "purge", ctx.author.id, str(ctx.author),
                         target_id=member.id if member else None,
                         target_name=str(member) if member else None,
                         reason=f"{len(deleted) - 1} messages",
                         channel_id=ctx.channel.id, command_used="purge")

    # ── SLOWMODE ──
    @commands.command(name="slowmode")
    @commands.has_permissions(manage_channels=True)
    async def slowmode(self, ctx, seconds: str, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        if seconds.lower() == "off":
            val = 0
        else:
            try:
                val = int(seconds)
            except ValueError:
                return await ctx.send("Use a number or `off`.")
            if val < 0 or val > 21600:
                return await ctx.send("Slowmode must be 0–21600 seconds.")
        await channel.edit(slowmode_delay=val)
        await ctx.send(f"🐌 Slowmode in {channel.mention}: **{val}s**." if val else f"🐌 Slowmode off in {channel.mention}.")
        await log_action(ctx.guild.id, "slowmode", ctx.author.id, str(ctx.author),
                         channel_id=channel.id, duration=str(val), command_used="slowmode")

    # ── WARN SYSTEM ──
    @commands.command(name="warn")
    @commands.has_permissions(moderate_members=True)
    async def warn(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        data = await self._get_mod_data(ctx.guild.id, member.id)
        warn_id = len(data["warns"]) + 1
        data["warns"].append({
            "id": warn_id, "reason": reason, "moderator": ctx.author.id,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        await self._save_mod_data(ctx.guild.id, member.id, data)
        await ctx.send(f"⚠️ **{member}** warned (#{warn_id}).\nReason: {reason}")
        await log_action(ctx.guild.id, "warn", ctx.author.id, str(ctx.author),
                         target_id=member.id, target_name=str(member), reason=reason, command_used="warn")
        await self._check_warn_punishment(member, ctx)

    @commands.command(name="delwarn")
    @commands.has_permissions(moderate_members=True)
    async def delwarn(self, ctx, member: discord.Member, warn_id: int):
        data = await self._get_mod_data(ctx.guild.id, member.id)
        original = len(data["warns"])
        data["warns"] = [w for w in data["warns"] if w["id"] != warn_id]
        if len(data["warns"]) == original:
            return await ctx.send("Warn ID not found.")
        for i, w in enumerate(data["warns"], 1):
            w["id"] = i
        await self._save_mod_data(ctx.guild.id, member.id, data)
        await ctx.send(f"✅ Warn #{warn_id} removed from **{member}**.")
        await log_action(ctx.guild.id, "delwarn", ctx.author.id, str(ctx.author),
                         target_id=member.id, target_name=str(member),
                         reason=f"Removed warn #{warn_id}", command_used="delwarn")

    @commands.command(name="clearwarns")
    @commands.has_permissions(moderate_members=True)
    async def clearwarns(self, ctx, member: discord.Member):
        data = await self._get_mod_data(ctx.guild.id, member.id)
        count = len(data["warns"])
        data["warns"] = []
        await self._save_mod_data(ctx.guild.id, member.id, data)
        await ctx.send(f"✅ Cleared **{count}** warns from **{member}**.")
        await log_action(ctx.guild.id, "clearwarns", ctx.author.id, str(ctx.author),
                         target_id=member.id, target_name=str(member),
                         reason=f"{count} warns cleared", command_used="clearwarns")

    @commands.command(name="warnings")
    async def warnings(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        data = await self._get_mod_data(ctx.guild.id, member.id)
        if not data["warns"]:
            return await ctx.send(f"**{member}** has no warnings.")
        embed = discord.Embed(title=f"Warnings — {member}", color=0xFEE75C)
        for w in data["warns"]:
            embed.add_field(name=f"#{w['id']}", value=f"**Reason:** {w['reason']}\n**By:** <@{w['moderator']}>\n**Date:** {w['timestamp'][:10]}", inline=False)
        await ctx.send(embed=embed)

    # ── NOTES ──
    @commands.command(name="noteadd")
    @commands.has_permissions(moderate_members=True)
    async def noteadd(self, ctx, member: discord.Member, *, note: str):
        data = await self._get_mod_data(ctx.guild.id, member.id)
        note_id = len(data["notes"]) + 1
        data["notes"].append({
            "id": note_id, "content": note, "moderator": ctx.author.id,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        await self._save_mod_data(ctx.guild.id, member.id, data)
        await ctx.send(f"📝 Note #{note_id} added to **{member}**.")
        await log_action(ctx.guild.id, "noteadd", ctx.author.id, str(ctx.author),
                         target_id=member.id, target_name=str(member), reason=note, command_used="noteadd")

    @commands.command(name="removenote")
    @commands.has_permissions(moderate_members=True)
    async def removenote(self, ctx, member: discord.Member, note_id: int):
        data = await self._get_mod_data(ctx.guild.id, member.id)
        original = len(data["notes"])
        data["notes"] = [n for n in data["notes"] if n["id"] != note_id]
        if len(data["notes"]) == original:
            return await ctx.send("Note ID not found.")
        for i, n in enumerate(data["notes"], 1):
            n["id"] = i
        await self._save_mod_data(ctx.guild.id, member.id, data)
        await ctx.send(f"✅ Note #{note_id} removed from **{member}**.")
        await log_action(ctx.guild.id, "removenote", ctx.author.id, str(ctx.author),
                         target_id=member.id, target_name=str(member),
                         reason=f"Removed note #{note_id}", command_used="removenote")

    @commands.command(name="viewnotes")
    @commands.has_permissions(moderate_members=True)
    async def viewnotes(self, ctx, member: discord.Member):
        data = await self._get_mod_data(ctx.guild.id, member.id)
        if not data["notes"]:
            return await ctx.send(f"**{member}** has no notes.")
        embed = discord.Embed(title=f"Notes — {member}", color=0x57F287)
        for n in data["notes"]:
            embed.add_field(name=f"#{n['id']}", value=f"{n['content']}\n**By:** <@{n['moderator']}> • {n['timestamp'][:10]}", inline=False)
        await ctx.send(embed=embed)

    # ── NICK / ROLE ──
    @commands.command(name="nick")
    @commands.has_permissions(manage_nicknames=True)
    async def nick(self, ctx, member: discord.Member, *, name: str = "reset"):
        new_nick = None if name.lower() == "reset" else name[:32]
        await member.edit(nick=new_nick)
        await ctx.send(f"✏️ Nickname of **{member}** set to `{new_nick or member.name}`.")
        await log_action(ctx.guild.id, "nick", ctx.author.id, str(ctx.author),
                         target_id=member.id, target_name=str(member),
                         reason=new_nick or "reset", command_used="nick")

    @commands.command(name="role")
    async def role(self, ctx, member: discord.Member, role: discord.Role):
        if not await self._is_manager(ctx.author):
            return await ctx.send("Manager+ required.")
        if role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send("You cannot manage this role.")
        if role in member.roles:
            await member.remove_roles(role, reason=f"By {ctx.author}")
            await ctx.send(f"➖ Role {role.mention} removed from **{member}**.")
            action = "role_remove"
        else:
            await member.add_roles(role, reason=f"By {ctx.author}")
            await ctx.send(f"➕ Role {role.mention} added to **{member}**.")
            action = "role_add"
        await log_action(ctx.guild.id, action, ctx.author.id, str(ctx.author),
                         target_id=member.id, target_name=str(member),
                         reason=role.name, command_used="role")

    # ── CASE / MODLOGS ──
    @commands.command(name="case")
    async def case(self, ctx, case_id: str):
        if not await self._is_staff(ctx.author):
            return await ctx.send("Staff+ required.")
        from bson import ObjectId
        try:
            doc = await mod_logs.find_one({"_id": ObjectId(case_id), "guild_id": ctx.guild.id})
        except Exception:
            return await ctx.send("Invalid case ID.")
        if not doc:
            return await ctx.send("Case not found.")
        embed = discord.Embed(title=f"Case `{case_id}`", color=0x5865F2)
        embed.add_field(name="Action", value=doc["action"], inline=True)
        embed.add_field(name="Moderator", value=doc.get("moderator_name", "?"), inline=True)
        embed.add_field(name="Target", value=doc.get("target_name") or "—", inline=True)
        embed.add_field(name="Reason", value=doc.get("reason") or "—", inline=False)
        if doc.get("duration"):
            embed.add_field(name="Duration", value=doc["duration"], inline=True)
        embed.add_field(name="Source", value=doc.get("source", "discord"), inline=True)
        embed.timestamp = doc["timestamp"]
        await ctx.send(embed=embed)

    @commands.command(name="modlogs")
    async def modlogs_cmd(self, ctx, member: discord.Member):
        if not await self._is_staff(ctx.author):
            return await ctx.send("Staff+ required.")
        from database import mod_logs as ml
        cursor = ml.find({"guild_id": ctx.guild.id, "target_id": member.id}).sort("timestamp", -1).limit(15)
        items = [doc async for doc in cursor]
        if not items:
            return await ctx.send(f"No mod logs for **{member}**.")
        embed = discord.Embed(title=f"Mod Logs — {member}", color=0x5865F2)
        for d in items:
            embed.add_field(
                name=f"{d['action'].upper()} • {d['timestamp'].strftime('%Y-%m-%d %H:%M')}",
                value=f"**By:** {d.get('moderator_name')}\n**Reason:** {d.get('reason') or '—'}\n`{d['_id']}`",
                inline=False
            )
        await ctx.send(embed=embed)

    # ── SNIPE ──
    @commands.command(name="snipe")
    async def snipe_cmd(self, ctx):
        doc = await snipe.find_one({"channel_id": ctx.channel.id})
        if not doc:
            return await ctx.send("Nothing to snipe.")
        embed = discord.Embed(description=doc.get("content") or "*empty*", color=0xED4245)
        embed.set_author(name=doc.get("author_name", "Unknown"))
        embed.timestamp = doc.get("timestamp", datetime.now(timezone.utc))
        await ctx.send(embed=embed)

    # ── SAY / ANNOUNCE ──
    @commands.command(name="say")
    async def say(self, ctx, channel: discord.TextChannel, *, text: str):
        if not await self._is_manager(ctx.author):
            return await ctx.send("Manager+ required.")
        await channel.send(text)
        await ctx.message.add_reaction("✅")
        await log_action(ctx.guild.id, "say", ctx.author.id, str(ctx.author),
                         channel_id=channel.id, reason=text[:100], command_used="say")

    @commands.command(name="announce")
    async def announce(self, ctx, channel: discord.TextChannel, *, text: str):
        if not await self._is_manager(ctx.author):
            return await ctx.send("Manager+ required.")
        embed = discord.Embed(description=text, color=0x5865F2)
        embed.set_footer(text=f"Announced by {ctx.author}")
        await channel.send(embed=embed)
        await ctx.message.add_reaction("✅")
        await log_action(ctx.guild.id, "announce", ctx.author.id, str(ctx.author),
                         channel_id=channel.id, reason=text[:100], command_used="announce")

    # ── INFO ──
    @commands.command(name="userinfo")
    async def userinfo(self, ctx, member: discord.Member = None):
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
    async def serverinfo(self, ctx):
        g = ctx.guild
        embed = discord.Embed(title=f"Server Info — {g.name}", color=0x5865F2)
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        embed.add_field(name="Owner", value=g.owner.mention if g.owner else "?", inline=True)
        embed.add_field(name="Members", value=g.member_count, inline=True)
        embed.add_field(name="Channels", value=len(g.channels), inline=True)
        embed.add_field(name="Roles", value=len(g.roles), inline=True)
        embed.add_field(name="Boosts", value=g.premium_subscription_count, inline=True)
        embed.add_field(name="Created", value=discord.utils.format_dt(g.created_at, "R"), inline=True)
        embed.add_field(name="ID", value=g.id, inline=False)
        await ctx.send(embed=embed)

    # ── CMDS ──
    @commands.command(name="cmds")
    async def cmds(self, ctx):
        embed = discord.Embed(title="HybridBOT — Commands", description="Prefix: `?` • Case-insensitive", color=0x5865F2)
        embed.add_field(name="🛡️ Moderation (Staff)", value=(
            "`?kick` `?ban` `?tempban` `?unban` `?timeout`/`?mute` `?untimeout`/`?unmute`\n"
            "`?warn` `?delwarn` `?clearwarns` `?warnings` `?purge` `?slowmode`\n"
            "`?lock` `?unlock` `?nick` `?case` `?modlogs` `?snipe`\n"
            "`?noteadd` `?removenote` `?viewnotes`"
        ), inline=False)
        embed.add_field(name="🔒 Moderation (Manager)", value=(
            "`?lockdown` `?unlockdown` `?role` `?banlist` `?say` `?announce`"
        ), inline=False)
        embed.add_field(name="ℹ️ Utility", value=(
            "`?ping` `?uptime` `?botinfo` `?membercount` `?userinfo` `?serverinfo`\n"
            "`?avatar` `?banner` `?roleinfo` `?channelinfo` `?poll` `?remind`\n"
            "`?afk` `?suggest` `?embed` `?cmds`"
        ), inline=False)
        embed.add_field(name="⚙️ Config (Slash)", value=(
            "`/bot-setup` `/welcomer-setup` `/auto-roles`"
        ), inline=False)
        embed.set_footer(text="HybridBOT • Dashboard: /dashboard")
        await ctx.send(embed=embed)

    # ── ERRORS ──
    @kick.error
    @ban.error
    @tempban.error
    @timeout.error
    @purge.error
    async def mod_error(self, ctx, error):
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

async def setup(bot):
    await bot.add_cog(Moderation(bot))
