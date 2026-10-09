import discord
from discord import app_commands
from discord.ext import commands
from database import guilds, timed_roles
from security import Security
import asyncio
from datetime import datetime, timedelta, timezone

class AutoRoles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.security = Security(bot)
        self.bot.loop.create_task(self._process_pending_timed_roles())

    async def _process_pending_timed_roles(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            now = datetime.now(timezone.utc)
            cursor = timed_roles.find({"execute_at": {"$lte": now}, "type": {"$ne": "tempban"}})
            async for doc in cursor:
                guild = self.bot.get_guild(doc["guild_id"])
                if not guild:
                    await timed_roles.delete_one({"_id": doc["_id"]})
                    continue
                member = guild.get_member(doc["member_id"])
                role = guild.get_role(doc["role_id"])
                if member and role:
                    try:
                        await member.add_roles(role, reason="Timed auto-role")
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                await timed_roles.delete_one({"_id": doc["_id"]})
            await asyncio.sleep(15)

    def _parse_delay(self, delay: str) -> int | None:
        try:
            unit = delay[-1].lower()
            value = int(delay[:-1])
            if unit == "s":
                return value
            if unit == "m":
                return value * 60
            if unit == "h":
                return value * 3600
        except (ValueError, IndexError):
            return None
        return None

    @app_commands.command(name="auto-roles", description="Open auto-roles configuration panel")
    async def auto_roles(self, interaction: discord.Interaction):
        cfg = await self.security.get_guild_config(interaction.guild.id)
        if not self.security.has_permission(interaction.user, "manager", cfg):
            return await interaction.response.send_message(
                "Insufficient permissions (manager+ required).",
                ephemeral=True
            )

        embed = discord.Embed(
            title="Auto-Roles Configuration",
            description="Use the buttons below to manage join roles and timed roles.",
            color=0x5865F2
        )
        join_roles = [f"<@&{r}>" for r in cfg["autoroles"]["join"]]
        embed.add_field(
            name="Join Roles",
            value="\n".join(join_roles) or "None",
            inline=False
        )

        timed = cfg["autoroles"].get("timed", [])
        timed_str = "\n".join(
            [f"<@&{t['role_id']}> after {t['delay']}" for t in timed]
        ) or "None"
        embed.add_field(name="Timed Roles", value=timed_str, inline=False)

        view = AutoRolesView(self, interaction.guild.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = await guilds.find_one({"_id": member.guild.id})
        if not cfg:
            return

        for role_id in cfg.get("autoroles", {}).get("join", []):
            role = member.guild.get_role(role_id)
            if role:
                try:
                    await member.add_roles(role, reason="Auto-role on join")
                except (discord.Forbidden, discord.HTTPException):
                    pass

        for t in cfg.get("autoroles", {}).get("timed", []):
            delay_seconds = self._parse_delay(t["delay"])
            if delay_seconds is None:
                continue
            execute_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
            await timed_roles.insert_one({
                "guild_id": member.guild.id,
                "member_id": member.id,
                "role_id": t["role_id"],
                "execute_at": execute_at
            })

class AutoRolesView(discord.ui.View):
    def __init__(self, cog: AutoRoles, guild_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id

    @discord.ui.button(label="Add Join Role", style=discord.ButtonStyle.primary)
    async def add_join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddJoinRoleModal(self.cog, self.guild_id))

    @discord.ui.button(label="Remove Join Role", style=discord.ButtonStyle.danger)
    async def remove_join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RemoveJoinRoleModal(self.cog, self.guild_id))

    @discord.ui.button(label="Add Timed Role", style=discord.ButtonStyle.secondary)
    async def add_timed(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddTimedRoleModal(self.cog, self.guild_id))

    @discord.ui.button(label="Remove Timed Role", style=discord.ButtonStyle.danger)
    async def remove_timed(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RemoveTimedRoleModal(self.cog, self.guild_id))

class AddJoinRoleModal(discord.ui.Modal, title="Add Join Role"):
    role_id = discord.ui.TextInput(label="Role ID", placeholder="123456789012345678")

    def __init__(self, cog, guild_id):
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            rid = int(self.role_id.value)
        except ValueError:
            return await interaction.response.send_message("Invalid role ID.", ephemeral=True)

        await guilds.update_one(
            {"_id": self.guild_id},
            {"$addToSet": {"autoroles.join": rid}},
            upsert=True
        )
        await interaction.response.send_message(
            f"Role `{rid}` added to join roles.",
            ephemeral=True
        )

class RemoveJoinRoleModal(discord.ui.Modal, title="Remove Join Role"):
    role_id = discord.ui.TextInput(label="Role ID")

    def __init__(self, cog, guild_id):
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            rid = int(self.role_id.value)
        except ValueError:
            return await interaction.response.send_message("Invalid role ID.", ephemeral=True)

        await guilds.update_one(
            {"_id": self.guild_id},
            {"$pull": {"autoroles.join": rid}}
        )
        await interaction.response.send_message(
            f"Role `{rid}` removed from join roles.",
            ephemeral=True
        )

class AddTimedRoleModal(discord.ui.Modal, title="Add Timed Role"):
    role_id = discord.ui.TextInput(label="Role ID")
    delay = discord.ui.TextInput(label="Delay (e.g. 5m, 1h, 30s)", placeholder="5m")

    def __init__(self, cog, guild_id):
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            rid = int(self.role_id.value)
        except ValueError:
            return await interaction.response.send_message("Invalid role ID.", ephemeral=True)

        if self.cog._parse_delay(self.delay.value) is None:
            return await interaction.response.send_message(
                "Invalid delay format. Use Ns / Nm / Nh",
                ephemeral=True
            )

        await guilds.update_one(
            {"_id": self.guild_id},
            {"$push": {"autoroles.timed": {"role_id": rid, "delay": self.delay.value}}},
            upsert=True
        )
        await interaction.response.send_message(
            f"Timed role `{rid}` after `{self.delay.value}` added.",
            ephemeral=True
        )

class RemoveTimedRoleModal(discord.ui.Modal, title="Remove Timed Role"):
    role_id = discord.ui.TextInput(label="Role ID")

    def __init__(self, cog, guild_id):
        super().__init__()
        self.cog = cog
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        try:
            rid = int(self.role_id.value)
        except ValueError:
            return await interaction.response.send_message("Invalid role ID.", ephemeral=True)

        await guilds.update_one(
            {"_id": self.guild_id},
            {"$pull": {"autoroles.timed": {"role_id": rid}}}
        )
        await interaction.response.send_message(
            f"Timed role `{rid}` removed.",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(AutoRoles(bot))
