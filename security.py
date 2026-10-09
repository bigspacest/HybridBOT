    @app_commands.command(name="bot-setup", description="Configure staff hierarchy roles")
    @app_commands.describe(
        action="add / remove / list",
        level="admin / manager / staff",
        role="Role to assign"
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="add", value="add"),
            app_commands.Choice(name="remove", value="remove"),
            app_commands.Choice(name="list", value="list")
        ],
        level=[
            app_commands.Choice(name="admin", value="admin"),
            app_commands.Choice(name="manager", value="manager"),
            app_commands.Choice(name="staff", value="staff")
        ]
    )
    async def setup_bot(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        level: Optional[app_commands.Choice[str]] = None,
        role: Optional[discord.Role] = None
    ):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "Only server administrators can run this command.",
                ephemeral=True
            )

        cfg = await self.get_guild_config(interaction.guild.id)
        key = f"{level.value}_roles" if level else None

        if action.value == "list":
            embed = discord.Embed(title="HybridBOT Role Hierarchy", color=0x2b2d31)
            for lvl in ["admin", "manager", "staff"]:
                roles = [f"<@&{r}>" for r in cfg[f"{lvl}_roles"]]
                embed.add_field(
                    name=lvl.upper(),
                    value="\n".join(roles) or "None",
                    inline=False
                )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        if not level or not role:
            return await interaction.response.send_message(
                "Level and role are required for add/remove.",
                ephemeral=True
            )

        roles_list = cfg[key]

        if action.value == "add":
            if role.id not in roles_list:
                roles_list.append(role.id)
                await guilds.update_one(
                    {"_id": interaction.guild.id},
                    {"$set": {key: roles_list}}
                )
                await interaction.response.send_message(
                    f"Role {role.mention} added to **{level.value}**.",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "Role already configured.",
                    ephemeral=True
                )

        elif action.value == "remove":
            if role.id in roles_list:
                roles_list.remove(role.id)
                await guilds.update_one(
                    {"_id": interaction.guild.id},
                    {"$set": {key: roles_list}}
                )
                await interaction.response.send_message(
                    f"Role {role.mention} removed from **{level.value}**.",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "Role not found in that level.",
                    ephemeral=True
                )
