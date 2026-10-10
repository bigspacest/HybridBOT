import asyncio
from datetime import datetime, timezone, timedelta
import discord
from discord.ext import commands
from database import scheduled_messages

class Scheduler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.loop.create_task(self._loop())

    def _next_send(self, send_at: datetime, repeat: str) -> datetime | None:
        if repeat == "daily":
            return send_at + timedelta(days=1)
        if repeat == "weekly":
            return send_at + timedelta(weeks=1)
        if repeat == "monthly":
            # aprox +30 días
            return send_at + timedelta(days=30)
        return None

    async def _build_message(self, doc: dict):
        content = doc.get("content") or None
        embed = None
        ed = doc.get("embed")
        if ed and isinstance(ed, dict):
            title = (ed.get("title") or "")[:256] or None
            description = (ed.get("description") or "")[:4096] or None
            try:
                color = int(ed.get("color") or 0x5865F2)
            except (ValueError, TypeError):
                color = 0x5865F2
            embed = discord.Embed(title=title, description=description, color=color)
            if ed.get("footer", {}).get("text"):
                embed.set_footer(text=str(ed["footer"]["text"])[:2048])
            if ed.get("image"):
                embed.set_image(url=ed["image"])
            if ed.get("thumbnail"):
                embed.set_thumbnail(url=ed["thumbnail"])

        view = None
        buttons = doc.get("buttons") or []
        valid = []
        for b in buttons[:5]:
            if not isinstance(b, dict):
                continue
            label = (b.get("label") or "")[:80]
            url = b.get("url") or ""
            if label and url.startswith(("http://", "https://")):
                valid.append(discord.ui.Button(label=label, url=url))
        if valid:
            view = discord.ui.View()
            for btn in valid:
                view.add_item(btn)
        return content, embed, view

    async def _loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            now = datetime.now(timezone.utc)
            cursor = scheduled_messages.find({"send_at": {"$lte": now}})
            async for doc in cursor:
                channel = self.bot.get_channel(doc["channel_id"])
                if channel:
                    try:
                        content, embed, view = await self._build_message(doc)
                        await channel.send(content=content, embed=embed, view=view)
                    except Exception:
                        pass

                repeat = doc.get("repeat") or "none"
                next_at = self._next_send(doc["send_at"], repeat)
                if next_at:
                    await scheduled_messages.update_one(
                        {"_id": doc["_id"]},
                        {"$set": {"send_at": next_at}},
                    )
                else:
                    await scheduled_messages.delete_one({"_id": doc["_id"]})
            await asyncio.sleep(20)

async def setup(bot: commands.Bot):
    await bot.add_cog(Scheduler(bot))
