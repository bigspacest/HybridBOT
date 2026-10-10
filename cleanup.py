import asyncio
from datetime import datetime, timezone, timedelta
from discord.ext import commands
from database import (
    panel_meta, mod_logs, panel_audit, bot_errors,
    DEFAULT_RETENTION, MODERATION_ACTIONS, EVENT_ACTIONS,
)


class Cleanup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.loop.create_task(self._loop())

    async def run_cleanup(self) -> dict:
        doc = await panel_meta.find_one({"_id": "retention"})
        retention = (doc or {}).get("retention") or DEFAULT_RETENTION
        now = datetime.now(timezone.utc)
        deleted = {"moderation": 0, "events": 0, "commands": 0, "errors": 0, "audit": 0}

        async def _del_logs(actions: set | None, days: int, key: str):
            if not days or days <= 0:
                return
            cutoff = now - timedelta(days=days)
            q = {"timestamp": {"$lt": cutoff}}
            if actions is not None:
                q["action"] = {"$in": list(actions)}
            res = await mod_logs.delete_many(q)
            deleted[key] = res.deleted_count

        mod_days = int((retention.get("moderation") or {}).get("days") or 0)
        evt_days = int((retention.get("events") or {}).get("days") or 0)
        cmd_days = int((retention.get("commands") or {}).get("days") or 0)
        err_days = int((retention.get("errors") or {}).get("days") or 0)
        aud_days = int((retention.get("audit") or {}).get("days") or 0)

        await _del_logs(MODERATION_ACTIONS, mod_days, "moderation")
        await _del_logs(EVENT_ACTIONS, evt_days, "events")
        await _del_logs({"command"}, cmd_days, "commands")

        if err_days > 0:
            res = await bot_errors.delete_many({"last_seen": {"$lt": now - timedelta(days=err_days)}})
            deleted["errors"] = res.deleted_count

        if aud_days > 0:
            res = await panel_audit.delete_many({"timestamp": {"$lt": now - timedelta(days=aud_days)}})
            deleted["audit"] = res.deleted_count

        await panel_meta.update_one(
            {"_id": "retention"},
            {"$set": {"last_cleanup": now, "last_deleted": deleted}},
            upsert=True,
        )
        return deleted

    async def _loop(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self.run_cleanup()
            except Exception as e:
                print(f"[cleanup] {e}")
            await asyncio.sleep(6 * 3600)


async def setup(bot: commands.Bot):
    await bot.add_cog(Cleanup(bot))
