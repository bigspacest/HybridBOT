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
panel_audit = db.panel_audit
panel_meta = db.panel_meta
scheduled_messages = db.scheduled_messages
role_panels = db.role_panels
bot_errors = db.bot_errors

DEFAULT_RETENTION = {
    "moderation": {"days": 0},
    "events": {"days": 30},
    "commands": {"days": 30},
    "errors": {"days": 30},
    "audit": {"days": 90},
}

MODERATION_ACTIONS = {
    "ban", "tempban", "unban", "warn", "delwarn", "kick", "timeout", "untimeout",
    "purge", "lock", "unlock", "noteadd", "removenote", "embed", "logs_cleared",
    "slowmode", "clearwarns", "nick", "role", "lockdown", "unlockdown",
}
EVENT_ACTIONS = {"join", "leave", "msgdelete"}


async def ensure_indexes():
    await mod_logs.create_index([("guild_id", 1), ("timestamp", -1)])
    await mod_logs.create_index([("guild_id", 1), ("action", 1)])
    await mod_logs.create_index([("guild_id", 1), ("moderator_id", 1)])
    await mod_logs.create_index([("guild_id", 1), ("target_id", 1)])
    await mod_logs.create_index([("guild_id", 1), ("command_id", 1), ("timestamp", -1)])
    await timed_roles.create_index([("guild_id", 1), ("execute_at", 1)])
    await timed_roles.create_index([("type", 1), ("execute_at", 1)])
    await reminders.create_index([("execute_at", 1)])
    await afk.create_index([("user_id", 1), ("guild_id", 1)])
    await snipe.create_index([("channel_id", 1)])

    # Quitar TTL antiguo (90 días) si existe; la retención la maneja cleanup.py
    try:
        await panel_audit.drop_index("timestamp_1")
    except Exception:
        pass
    try:
        await panel_audit.create_index([("timestamp", 1)], name="timestamp_1")
    except Exception:
        pass
    await panel_audit.create_index([("event", 1), ("timestamp", -1)])

    await scheduled_messages.create_index([("send_at", 1)])
    await scheduled_messages.create_index([("guild_id", 1), ("send_at", 1)])
    await role_panels.create_index([("guild_id", 1)])
    await bot_errors.create_index([("fingerprint", 1)], unique=True)
    await bot_errors.create_index([("last_seen", -1)])
    await bot_errors.create_index([("level", 1), ("last_seen", -1)])


async def ensure_panel_meta():
    existing = await panel_meta.find_one({"_id": "settings"})
    if not existing:
        await panel_meta.insert_one({
            "_id": "settings",
            "alerts": {"login": True, "denied": True, "destructive": True},
            "session_version": 1,
        })
    ret = await panel_meta.find_one({"_id": "retention"})
    if not ret:
        await panel_meta.insert_one({
            "_id": "retention",
            "retention": DEFAULT_RETENTION,
            "last_cleanup": None,
            "last_deleted": {},
        })


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
    command_id: str | None = None,
    success: bool | None = None,
    user_id: int | None = None,
    user_name: str | None = None,
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
    if command_id is not None:
        doc["command_id"] = command_id
    if success is not None:
        doc["success"] = success
    if user_id is not None:
        doc["user_id"] = user_id
    if user_name is not None:
        doc["user_name"] = user_name
    if extra:
        doc["extra"] = extra
    await mod_logs.insert_one(doc)


async def write_audit(event: str, detail: str = "", ip: str = "", user_agent: str = ""):
    await panel_audit.insert_one({
        "event": event,
        "detail": (detail or "")[:300],
        "ip": (ip or "")[:64],
        "user_agent": (user_agent or "")[:200],
        "timestamp": datetime.now(timezone.utc),
    })
