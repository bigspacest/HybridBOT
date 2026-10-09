import discord
from discord.ext import commands
from database import guilds, log_action
import re
import time

class AutoMod(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.spam_tracker = {}  # {user_id: [timestamps]}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        cfg = await guilds.find_one({"_id": message.guild.id})
        if not cfg:
            return
        automod = cfg.get("automod", {})
        if not automod:
            return

        # Anti-spam
        if automod.get("anti_spam"):
            uid = message.author.id
            now = time.time()
            self.spam_tracker.setdefault(uid, [])
            self.spam_tracker[uid] = [t for t in self.spam_tracker[uid] if now - t < 5]
            self.spam_tracker[uid].append(now)
            if len(self.spam_tracker[uid]) >= 6:
                try:
                    await message.delete()
                    await message.channel.send(f"⚠️ {message.author.mention} slow down (anti-spam).", delete_after=5)
                    await log_action(message.guild.id, "automod_spam", self.bot.user.id, str(self.bot.user),
                                     target_id=message.author.id, target_name=str(message.author),
                                     channel_id=message.channel.id, source="discord")
                except discord.Forbidden:
                    pass
                return

        # Anti-links
        if automod.get("anti_links"):
            if re.search(r"https?://|discord\.gg/", message.content, re.I):
                if not message.author.guild_permissions.manage_messages:
                    try:
                        await message.delete()
                        await message.channel.send(f"🔗 {message.author.mention} links are not allowed.", delete_after=5)
                        await log_action(message.guild.id, "automod_link", self.bot.user.id, str(self.bot.user),
                                         target_id=message.author.id, target_name=str(message.author),
                                         channel_id=message.channel.id, source="discord")
                    except discord.Forbidden:
                        pass
                    return

        # Bad words
        bad_words = automod.get("bad_words") or []
        if bad_words:
            content_lower = message.content.lower()
            for word in bad_words:
                if word.lower() in content_lower:
                    if not message.author.guild_permissions.manage_messages:
                        try:
                            await message.delete()
                            await message.channel.send(f"🚫 {message.author.mention} that word is not allowed.", delete_after=5)
                            await log_action(message.guild.id, "automod_badword", self.bot.user.id, str(self.bot.user),
                                             target_id=message.author.id, target_name=str(message.author),
                                             reason=word, channel_id=message.channel.id, source="discord")
                        except discord.Forbidden:
                            pass
                    return

async def setup(bot):
    await bot.add_cog(AutoMod(bot))
