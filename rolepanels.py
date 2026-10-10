import discord
from discord.ext import commands
from database import role_panels
from bson import ObjectId

STYLE_MAP = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}

class RolePanelButton(discord.ui.Button):
    def __init__(self, panel_id: str, role_id: int, label: str, emoji: str | None, style: str):
        kwargs = {
            "label": label[:80],
            "style": STYLE_MAP.get(style, discord.ButtonStyle.secondary),
            "custom_id": f"rp:{panel_id}:{role_id}",
        }
        if emoji:
            kwargs["emoji"] = emoji
        super().__init__(**kwargs)
        self.panel_id = panel_id
        self.role_id = role_id

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Solo en servidores.", ephemeral=True)

        doc = await role_panels.find_one({"_id": ObjectId(self.panel_id)})
        if not doc:
            return await interaction.response.send_message("Este panel ya no existe.", ephemeral=True)

        role = interaction.guild.get_role(self.role_id)
        if not role:
            return await interaction.response.send_message("Ese rol ya no existe.", ephemeral=True)

        member = interaction.user
        exclusive = bool(doc.get("exclusive"))

        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Role panel")
                await interaction.response.send_message(f"Rol quitado: **{role.name}**", ephemeral=True)
            else:
                if exclusive:
                    other_ids = [int(b["role_id"]) for b in doc.get("buttons", []) if int(b["role_id"]) != self.role_id]
                    to_remove = [r for r in member.roles if r.id in other_ids]
                    if to_remove:
                        await member.remove_roles(*to_remove, reason="Role panel exclusive")
                await member.add_roles(role, reason="Role panel")
                await interaction.response.send_message(f"Rol añadido: **{role.name}**", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(
                "No tengo permisos para gestionar ese rol (jerarquía o Manage Roles).",
                ephemeral=True,
            )
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

class RolePanelView(discord.ui.View):
    def __init__(self, panel_id: str, buttons: list):
        super().__init__(timeout=None)
        for b in buttons[:25]:
            self.add_item(RolePanelButton(
                panel_id=panel_id,
                role_id=int(b["role_id"]),
                label=b.get("label") or "Rol",
                emoji=b.get("emoji") or None,
                style=b.get("style") or "secondary",
            ))

class RolePanels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.loop.create_task(self._register_views())

    async def _register_views(self):
        await self.bot.wait_until_ready()
        cursor = role_panels.find({})
        async for doc in cursor:
            try:
                view = RolePanelView(str(doc["_id"]), doc.get("buttons") or [])
                self.bot.add_view(view)
            except Exception as e:
                print(f"[rolepanels] register {doc.get('_id')}: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(RolePanels(bot))
