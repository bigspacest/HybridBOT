import discord
from discord import app_commands
from discord.ext import commands
from database import guilds
from security import Security

class Welcomer(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.security = Security(bot)

    @app_commands.command(name="welcomer-setup", description="Configure welcome system")
    @app_commands.describe(
        channel="Channel where welcome messages will be sent",
        message="Message template. Variables: {member} {membercount} {server}"
    )
    async def welcomer_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: str
    ):
        cfg = await self.security.get_guild_config(interaction.guild.id)
        if not self.security.has_permission(interaction.user, "manager", cfg):
            return await interaction.response.send_message(
                "Insufficient permissions (manager+ required).",
                ephemeral=True
            )

        await guilds.update_one(
            {"_id": interaction.guild.id},
            {"$set": {
                "welcome.channel_id": channel.id,
                "welcome.message": message
            }},
            upsert=True
        )
        await interaction.response.send_message(
            f"Welcome system configured.\nChannel: {channel.mention}\nMessage: `{message}`",
            ephemeral=True
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = await guilds.find_one({"_id": member.guild.id})
        if not cfg or not cfg.get("welcome", {}).get("channel_id"):
            return

        channel = member.guild.get_channel(cfg["welcome"]["channel_id"])
        if not channel:
            return

        msg = cfg["welcome"]["message"]
        msg = msg.replace("{member}", member.mention)
        msg = msg.replace("{membercount}", str(member.guild.member_count))
        msg = msg.replace("{server}", member.guild.name)

        try:
            await channel.send(msg)
        except discord.Forbidden:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(Welcomer(bot))
