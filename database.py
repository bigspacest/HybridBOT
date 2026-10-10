from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timezone
from config import MONGO_URI, DATABASE_NAME

client = AsyncIOMotorClient(MONGO_URI)
db = client[DATABASE_NAME]

guilds = db.guilds
timed_roles = db.timed_roles
mod_logs = db.mod_logs
reminders = db.reminders
afk = db.afk
snipe = db.snipe

async def ensure_indexes():
    await mod_logs.create_index([("guild_id", 1), ("timestamp", -1)])
    await mod_logs.create_index([("guild_id", 1), ("action", 1)])
    await mod_logs.create_index([("guild_id", 1), ("moderator_id", 1)])
    await mod_logs.create_index([("guild_id", 1), ("target_id", 1)])
    await timed_roles.create_index([("guild_id", 1), ("execute_at", 1)])
    await timed_roles.create_index([("type", 1), ("execute_at", 1)])
    await reminders.create_index([("execute_at", 1)])
    await afk.create_index([("user_id", 1), ("guild_id", 1)])
    await snipe.create_index([("channel_id", 1)])

async def log_action(
    guild_id: int,
    action: str,
    moderator_id: int,
    moderator_name: str,
    target_id: int | None = None,
    target_name: str | None = None,
    reason: str = "",
    duration: str | None = None,
    command_used: str = "",
    channel_id: int | None = None,
    source: str = "discord",
    extra: dict | None = None,
):
    doc = {
        "guild_id": guild_id,
        "action": action,
        "moderator_id": moderator_id,
        "moderator_name": moderator_name,
        "target_id": target_id,
        "target_name": target_name,
        "reason": reason,
        "duration": duration,
        "command_used": command_used,
        "channel_id": channel_id,
        "source": source,
        "timestamp": datetime.now(timezone.utc),
    }
    if extra:
        doc["extra"] = extra
    await mod_logs.insert_one(doc)
