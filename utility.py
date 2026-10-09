import discord
from discord.ext import commands
from database import reminders, afk, guilds, log_action
from datetime import datetime, timedelta, timezone
import asyncio
import re
import time

class Utility(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.loop.create_task(self._process_reminders())

    async def _process_reminders(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            now = datetime.now(timezone.utc)
            cursor = reminders.find({"execute_at": {"$lte": now}})
            async for doc in cursor:
                try:
                    user = self.bot.get_user(doc["user_id"]) or await self.bot.fetch_user(doc["user_id"])
                    channel = self.bot.get_channel(doc["channel_id"])
                    text = f"⏰ <@{doc['user_id']}> Reminder: {doc['text']}"
                    if channel:
                        await channel.send(text)
                    else:
                        await user.send(text)
                except Exception:
                    pass
                await reminders.delete_one({"_id": doc["_id"]})
            await asyncio.sleep(20)

    def _parse_time(self, time_str: str) -> int | None:
        match = re.match(r"^(\d+)([smhd])$", time_str.lower())
        if not match:
            return None
        value, unit = int(match.group(1)), match.group(2)
        return value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]

    @commands.command(name="ping")
    async def ping(self, ctx):
        latency = round(self.bot.latency * 1000)
        await ctx.send(f"🏓 Pong! `{latency}ms`")

    @commands.command(name="uptime")
    async def uptime(self, ctx):
        if not self.bot.start_time:
            return await ctx.send("Uptime unavailable.")
        seconds = int(time.time() - self.bot.start_time)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        await ctx.send(f"⏱️ Uptime: **{h}h {m}m {s}s**")

    @commands.command(name="botinfo")
    async def botinfo(self, ctx):
        embed = discord.Embed(title="HybridBOT", color=0x5865F2)
        embed.add_field(name="Servers", value=len(self.bot.guilds), inline=True)
        embed.add_field(name="Users", value=sum(g.member_count or 0 for g in self.bot.guilds), inline=True)
        embed.add_field(name="Latency", value=f"{round(self.bot.latency * 1000)}ms", inline=True)
        embed.add_field(name="Library", value="discord.py 2.4", inline=True)
        embed.set_footer(text="Dashboard: /dashboard")
        await ctx.send(embed=embed)

    @commands.command(name="membercount")
    async def membercount(self, ctx):
        await ctx.send(f"👥 **{ctx.guild.member_count}** members")

    @commands.command(name="avatar")
    async def avatar(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        embed = discord.Embed(title=f"Avatar — {member}", color=member.color or 0x2b2d31)
        embed.set_image(url=member.display_avatar.url)
        await ctx.send(embed=embed)

    @commands.command(name="banner")
    async def banner(self, ctx, member: discord.Member = None):
        member = member or ctx.author
        user = await self.bot.fetch_user(member.id)
        if not user.banner:
            return await ctx.send("This user has no banner.")
        embed = discord.Embed(title=f"Banner — {member}", color=0x5865F2)
        embed.set_image(url=user.banner.url)
        await ctx.send(embed=embed)

    @commands.command(name="roleinfo")
    async def roleinfo(self, ctx, role: discord.Role):
        embed = discord.Embed(title=f"Role — {role.name}", color=role.color or 0x2b2d31)
        embed.add_field(name="ID", value=role.id, inline=True)
        embed.add_field(name="Members", value=len(role.members), inline=True)
        embed.add_field(name="Position", value=role.position, inline=True)
        embed.add_field(name="Hoisted", value=role.hoist, inline=True)
        embed.add_field(name="Mentionable", value=role.mentionable, inline=True)
        embed.add_field(name="Created", value=discord.utils.format_dt(role.created_at, "R"), inline=True)
        await ctx.send(embed=embed)

    @commands.command(name="channelinfo")
    async def channelinfo(self, ctx, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        embed = discord.Embed(title=f"Channel — #{channel.name}", color=0x5865F2)
        embed.add_field(name="ID", value=channel.id, inline=True)
        embed.add_field(name="Category", value=channel.category.name if channel.category else "None", inline=True)
        embed.add_field(name="Slowmode", value=f"{channel.slowmode_delay}s", inline=True)
        embed.add_field(name="NSFW", value=channel.nsfw, inline=True)
        embed.add_field(name="Created", value=discord.utils.format_dt(channel.created_at, "R"), inline=True)
        await ctx.send(embed=embed)

    @commands.command(name="poll")
    async def poll(self, ctx, *, content: str):
        parts = [p.strip() for p in content.split("|")]
        if len(parts) < 3:
            return await ctx.send("Usage: `?poll Question | Option1 | Option2 | ...` (max 9 options)")
        question = parts[0]
        options = parts[1:10]
        emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
        desc = "\n".join(f"{emojis[i]} {opt}" for i, opt in enumerate(options))
        embed = discord.Embed(title=f"📊 {question}", description=desc, color=0x5865F2)
        embed.set_footer(text=f"Poll by {ctx.author}")
        msg = await ctx.send(embed=embed)
        for i in range(len(options)):
            await msg.add_reaction(emojis[i])

    @commands.command(name="remind")
    async def remind(self, ctx, time: str, *, text: str):
        seconds = self._parse_time(time)
        if seconds is None:
            return await ctx.send("Invalid time. Use `10m`, `2h`, `1d`.")
        execute_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        await reminders.insert_one({
            "user_id": ctx.author.id,
            "channel_id": ctx.channel.id,
            "guild_id": ctx.guild.id if ctx.guild else None,
            "text": text,
            "execute_at": execute_at
        })
        await ctx.send(f"⏰ Reminder set for `{time}`: {text}")

    @commands.command(name="afk")
    async def afk_cmd(self, ctx, *, reason: str = "AFK"):
        await afk.update_one(
            {"user_id": ctx.author.id, "guild_id": ctx.guild.id},
            {"$set": {"reason": reason, "since": datetime.now(timezone.utc)}},
            upsert=True
        )
        await ctx.send(f"💤 **{ctx.author.display_name}** is now AFK: {reason}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        # Remove AFK on speak
        doc = await afk.find_one({"user_id": message.author.id, "guild_id": message.guild.id})
        if doc:
            await afk.delete_one({"_id": doc["_id"]})
            try:
                await message.channel.send(f"👋 Welcome back **{message.author.display_name}**, AFK removed.", delete_after=8)
            except discord.Forbidden:
                pass
        # Notify AFK mentions
        for user in message.mentions:
            afk_doc = await afk.find_one({"user_id": user.id, "guild_id": message.guild.id})
            if afk_doc:
                try:
                    await message.channel.send(
                        f"💤 **{user.display_name}** is AFK: {afk_doc.get('reason', 'AFK')}",
                        delete_after=10
                    )
                except discord.Forbidden:
                    pass

    @commands.command(name="suggest")
    async def suggest(self, ctx, *, text: str):
        cfg = await guilds.find_one({"_id": ctx.guild.id})
        channel_id = cfg.get("suggestions_channel_id") if cfg else None
        channel = ctx.guild.get_channel(channel_id) if channel_id else ctx.channel
        embed = discord.Embed(title="💡 Suggestion", description=text, color=0xFEE75C)
        embed.set_author(name=str(ctx.author), icon_url=ctx.author.display_avatar.url)
        embed.set_footer(text=f"ID: {ctx.author.id}")
        msg = await channel.send(embed=embed)
        await msg.add_reaction("👍")
        await msg.add_reaction("👎")
        if channel != ctx.channel:
            await ctx.message.add_reaction("✅")

    @commands.command(name="embed")
    async def embed_cmd(self, ctx):
        await ctx.send("🎨 Embed builder: https://hybridbot-kiqm.onrender.com/dashboard")

async def setup(bot):
    await bot.add_cog(Utility(bot))
