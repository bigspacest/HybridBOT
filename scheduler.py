import asyncio
from calendar import monthrange
from datetime import datetime, timezone, timedelta
import discord
from discord.ext import commands
from database import scheduled_messages

class Scheduler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.loop.create_task(self._loop())

    def _add_months(self, dt: datetime, months: int = 1) -> datetime:
        year = dt.year + (dt.month - 1 + months) // 12
        month = (dt.month - 1 + months) % 12 + 1
        day = min(dt.day, monthrange(year, month)[1])
        return dt.replace(year=year, month=month, day=day)

    def _next_occurrence(self, send_at: datetime, repeat: str, now: datetime) -> datetime | None:
        """Devuelve el próximo send_at futuro, saltando ocurrencias perdidas (sin ráfagas)."""
        if repeat == "none":
            return None
        next_at = send_at
        # avanzar hasta estar estrictamente en el futuro
        guard = 0
        while next_at <= now and guard < 500:
            if repeat == "daily":
                next_at = next_at + timedelta(days=1)
            elif repeat == "weekly":
                next_at = next_at + timedelta(weeks=1)
            elif repeat == "monthly":
                next_at = self._add_months(next_at, 1)
            else:
                return None
            guard += 1
        return next_at if next_at > now else None

    async def _build_payload(self, doc: dict):
        content = doc.get("content") or None
        embed = None
        ed = doc.get("embed")
        if ed and isinstance(ed, dict):
            title = (ed.get("title") or "")[:256] or None
            description = (ed.get("description") or "")[:4096] or None
            try:
                color = int(ed.get("color") if ed.get("color") is not None else 0x5865F2)
            except (ValueError, TypeError):
                color = 0x5865F2
            embed = discord.Embed(title=title, description=description, color=color)
            author = ed.get("author")
            if author and isinstance(author, dict) and author.get("name"):
                embed.set_author(name=str(author["name"])[:256], icon_url=author.get("icon_url") or None)
            if ed.get("thumbnail"):
                embed.set_thumbnail(url=ed["thumbnail"])
            if ed.get("image"):
                embed.set_image(url=ed["image"])
            footer = ed.get("footer")
            if footer and isinstance(footer, dict) and footer.get("text"):
                embed.set_footer(text=str(footer["text"])[:2048], icon_url=footer.get("icon_url") or None)
            if ed.get("timestamp"):
                embed.timestamp = datetime.now(timezone.utc)
            for f in (ed.get("fields") or [])[:25]:
                if isinstance(f, dict):
                    embed.add_field(
                        name=str(f.get("name") or "\u200b")[:256],
                        value=str(f.get("value") or "\u200b")[:1024],
                        inline=bool(f.get("inline")),
                    )

        view = None
        buttons = doc.get("buttons") or []
        items = []
        for b in buttons[:5]:
            if not isinstance(b, dict):
                continue
            label = (b.get("label") or "")[:80]
            url = b.get("url") or ""
            if label and url.startswith(("http://", "https://")):
                items.append(discord.ui.Button(label=label, url=url))
        if items:
            view = discord.ui.View()
            for btn in items:
                view.add_item(btn)
        return content, embed, view

    async def _loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            now = datetime.now(timezone.utc)
            cursor = scheduled_messages.find({"send_at": {"$lte": now}})
            async for doc in cursor:
                repeat = doc.get("repeat") or "none"
                channel = self.bot.get_channel(doc.get("channel_id"))

                # Si el bot estuvo apagado mucho tiempo y es recurrente:
                # no enviamos ráfagas → saltamos a la próxima ocurrencia futura.
                if repeat != "none":
                    next_at = self._next_occurrence(doc["send_at"], repeat, now)
                    # Solo enviamos si la ocurrencia vencida es "reciente" (< 2 min)
                    # o si next_at se calcula desde ahora; si send_at está muy atrasado, solo reprograma
                    lag = (now - doc["send_at"]).total_seconds()
                    should_send = lag <= 120
                else:
                    should_send = True
                    next_at = None

                if should_send and channel:
                    try:
                        content, embed, view = await self._build_payload(doc)
                        await channel.send(content=content, embed=embed, view=view)
                    except Exception:
                        pass

                if next_at:
                    await scheduled_messages.update_one(
                        {"_id": doc["_id"]},
                        {"$set": {"send_at": next_at}},
                    )
                else:
                    await scheduled_messages.delete_one({"_id": doc["_id"]})

            await asyncio.sleep(30)

async def setup(bot: commands.Bot):
    await bot.add_cog(Scheduler(bot))
