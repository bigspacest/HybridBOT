import discord
from discord.ext import commands
from database import mod_logs, log_action, snipe
from datetime import datetime, timezone

class Events(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await log_action(
            member.guild.id, "join", 0, "System",
            target_id=member.id, target_name=str(member), source="discord"
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await log_action(
            member.guild.id, "leave", 0, "System",
            target_id=member.id, target_name=str(member), source="discord"
        )

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        await snipe.update_one(
            {"channel_id": message.channel.id},
            {"$set": {
                "content": message.content,
                "author_name": str(message.author),
                "author_id": message.author.id,
                "timestamp": datetime.now(timezone.utc)
            }},
            upsert=True
        )
        await log_action(
            message.guild.id, "msgdelete", 0, "System",
            target_id=message.author.id, target_name=str(message.author),
            reason=(message.content or "")[:200],
            channel_id=message.channel.id, source="discord"
        )

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.roles == after.roles:
            return
        added = set(after.roles) - set(before.roles)
        removed = set(before.roles) - set(after.roles)
        for role in added:
            if role.is_default():
                continue
            await log_action(
                after.guild.id, "role_add", 0, "System",
                target_id=after.id, target_name=str(after),
                reason=role.name, source="discord"
            )
        for role in removed:
            if role.is_default():
                continue
            await log_action(
                after.guild.id, "role_remove", 0, "System",
                target_id=after.id, target_name=str(after),
                reason=role.name, source="discord"
            )

async def setup(bot):
    await bot.add_cog(Events(bot))
