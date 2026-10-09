import asyncio
import os
import secrets
import time
from datetime import datetime, timezone, timedelta
from functools import wraps
from urllib.parse import urlencode

import discord
import requests
from flask import Blueprint, request, jsonify, redirect, make_response, send_file
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from config import (
    DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, REDIRECT_URI,
    SESSION_SECRET, DASHBOARD_URL, OWNER_ID
)
from database import guilds, timed_roles, mod_logs, log_action

api = Blueprint("api", __name__)

serializer = URLSafeTimedSerializer(SESSION_SECRET)
COOKIE_NAME = "hybrid_session"
COOKIE_MAX_AGE = 86400  # 24h

_rate = {}

def rate_limit(max_calls=40, period=60):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            ip = request.remote_addr or "unknown"
            now = time.time()
            _rate.setdefault(ip, [])
            _rate[ip] = [t for t in _rate[ip] if now - t < period]
            if len(_rate[ip]) >= max_calls:
                return jsonify({"ok": False, "error": "Rate limit exceeded"}), 429
            _rate[ip].append(now)
            return f(*args, **kwargs)
        return wrapped
    return decorator

def run_async(coro):
    from bot import bot
    future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
    return future.result(timeout=20)

def create_session(user_id: str) -> str:
    return serializer.dumps({"uid": str(user_id)})

def get_session_user() -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        data = serializer.loads(token, max_age=COOKIE_MAX_AGE)
        return str(data["uid"])
    except (BadSignature, SignatureExpired, KeyError, TypeError):
        return None

def require_owner(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        uid = get_session_user()
        if not uid or uid != str(OWNER_ID):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapped

# ──────────────────────── Dashboard (público) ────────────────────────
@api.route("/dashboard")
def dashboard():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    return send_file(path)

# ──────────────────────── Auth ────────────────────────
@api.route("/auth/login")
@rate_limit()
def auth_login():
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state
    }
    url = f"https://discord.com/api/oauth2/authorize?{urlencode(params)}"
    resp = make_response(redirect(url))
    resp.set_cookie("oauth_state", state, httponly=True, samesite="Lax",
                    secure=True, max_age=300)
    return resp

@api.route("/auth/callback")
@rate_limit()
def auth_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    stored = request.cookies.get("oauth_state")

    if not code or not state or state != stored:
        return jsonify({"ok": False, "error": "Invalid state or code"}), 400

    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI
    }
    token_res = requests.post(
        "https://discord.com/api/oauth2/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    if token_res.status_code != 200:
        return jsonify({"ok": False, "error": "Token exchange failed"}), 400

    access_token = token_res.json()["access_token"]
    user_res = requests.get(
        "https://discord.com/api/users/@me",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    if user_res.status_code != 200:
        return jsonify({"ok": False, "error": "Failed to fetch user"}), 400

    user = user_res.json()
    user_id = str(user["id"])

    if user_id != str(OWNER_ID):
        return jsonify({"ok": False, "error": "Access denied. Owner only."}), 403

    session_token = create_session(user_id)

    # Evitar doble barra si DASHBOARD_URL termina en /
    base = DASHBOARD_URL.rstrip("/")
    resp = make_response(redirect(f"{base}/dashboard"))
    resp.set_cookie(
        COOKIE_NAME, session_token,
        httponly=True, secure=True, samesite="Lax",
        max_age=COOKIE_MAX_AGE
    )
    resp.delete_cookie("oauth_state", samesite="Lax", secure=True)
    return resp

@api.route("/auth/logout", methods=["POST"])
def auth_logout():
    resp = make_response(jsonify({"ok": True, "data": None}))
    resp.delete_cookie(COOKIE_NAME, samesite="Lax", secure=True)
    return resp

@api.route("/api/me")
@require_owner
def api_me():
    return jsonify({"ok": True, "data": {"id": get_session_user(), "is_owner": True}})

# ──────────────────────── Guilds ──────────────────────
@api.route("/api/guilds")
@require_owner
@rate_limit()
def api_guilds():
    from bot import bot
    data = [
        {
            "id": str(g.id),
            "name": g.name,
            "icon_url": str(g.icon.url) if g.icon else None,
            "member_count": g.member_count
        }
        for g in bot.guilds
    ]
    return jsonify({"ok": True, "data": data})

@api.route("/api/guilds/<guild_id>/channels")
@require_owner
def api_channels(guild_id):
    from bot import bot
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"ok": False, "error": "Guild not found"}), 404
    data = [{"id": str(c.id), "name": c.name} for c in guild.text_channels]
    return jsonify({"ok": True, "data": data})

@api.route("/api/guilds/<guild_id>/roles")
@require_owner
def api_roles(guild_id):
    from bot import bot
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"ok": False, "error": "Guild not found"}), 404
    data = [
        {"id": str(r.id), "name": r.name, "color": str(r.color)}
        for r in guild.roles if not r.is_default()
    ]
    return jsonify({"ok": True, "data": data})

# ──────────────────────── Overview ────────────────────
@api.route("/api/guilds/<guild_id>/overview")
@require_owner
def api_overview(guild_id):
    from bot import bot
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"ok": False, "error": "Guild not found"}), 404

    async def _overview():
        gid = int(guild_id)
        total_bans = await mod_logs.count_documents({"guild_id": gid, "action": "ban"})
        total_warns = await mod_logs.count_documents({"guild_id": gid, "action": "warn"})
        tempbans_pending = await timed_roles.count_documents({"guild_id": gid, "type": "tempban"})
        timed_roles_pending = await timed_roles.count_documents({
            "guild_id": gid,
            "$or": [{"type": {"$exists": False}}, {"type": "role"}]
        })

        seven_days = datetime.now(timezone.utc) - timedelta(days=7)
        commands_7d = await mod_logs.count_documents({
            "guild_id": gid,
            "action": "command",
            "timestamp": {"$gte": seven_days}
        })

        pipeline = [
            {"$match": {"guild_id": gid, "timestamp": {"$gte": seven_days}}},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$timestamp"}},
                "count": {"$sum": 1}
            }},
            {"$sort": {"_id": 1}}
        ]
        actions_per_day = [{"date": d["_id"], "count": d["count"]} async for d in mod_logs.aggregate(pipeline)]

        pipeline2 = [
            {"$match": {"guild_id": gid, "action": {"$ne": "command"}}},
            {"$group": {"_id": "$moderator_name", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 5}
        ]
        top_moderators = [{"name": d["_id"], "count": d["count"]} async for d in mod_logs.aggregate(pipeline2)]

        return {
            "member_count": guild.member_count,
            "total_bans": total_bans,
            "total_warns": total_warns,
            "tempbans_pending": tempbans_pending,
            "timed_roles_pending": timed_roles_pending,
            "commands_7d": commands_7d,
            "actions_per_day": actions_per_day,
            "top_moderators": top_moderators
        }

    return jsonify({"ok": True, "data": run_async(_overview())})

# ──────────────────────── Settings ────────────────────
@api.route("/api/guilds/<guild_id>/settings", methods=["GET"])
@require_owner
def get_settings(guild_id):
    async def _get():
        cfg = await guilds.find_one({"_id": int(guild_id)})
        if not cfg:
            return {
                "admin_roles": [],
                "manager_roles": [],
                "staff_roles": [],
                "welcome": {"channel_id": None, "message": None},
                "autoroles": {"join": [], "timed": []}
            }
        return {
            "admin_roles": [str(r) for r in cfg.get("admin_roles", [])],
            "manager_roles": [str(r) for r in cfg.get("manager_roles", [])],
            "staff_roles": [str(r) for r in cfg.get("staff_roles", [])],
            "welcome": {
                "channel_id": str(cfg["welcome"]["channel_id"]) if cfg.get("welcome", {}).get("channel_id") else None,
                "message": cfg.get("welcome", {}).get("message")
            },
            "autoroles": {
                "join": [str(r) for r in cfg.get("autoroles", {}).get("join", [])],
                "timed": [
                    {"role_id": str(t["role_id"]), "delay": t["delay"]}
                    for t in cfg.get("autoroles", {}).get("timed", [])
                ]
            }
        }
    return jsonify({"ok": True, "data": run_async(_get())})

@api.route("/api/guilds/<guild_id>/settings", methods=["PUT"])
@require_owner
def put_settings(guild_id):
    from bot import bot
    body = request.get_json(silent=True) or {}
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"ok": False, "error": "Guild not found"}), 404

    async def _save():
        update = {}

        for key in ("admin_roles", "manager_roles", "staff_roles"):
            if key in body and isinstance(body[key], list):
                valid = []
                for rid in body[key]:
                    try:
                        rid = int(rid)
                        if guild.get_role(rid):
                            valid.append(rid)
                    except (ValueError, TypeError):
                        continue
                update[key] = valid

        if "welcome" in body and isinstance(body["welcome"], dict):
            w = body["welcome"]
            channel_id = None
            if w.get("channel_id"):
                try:
                    cid = int(w["channel_id"])
                    if guild.get_channel(cid):
                        channel_id = cid
                except (ValueError, TypeError):
                    pass
            update["welcome"] = {
                "channel_id": channel_id,
                "message": w.get("message")
            }

        if "autoroles" in body and isinstance(body["autoroles"], dict):
            ar = body["autoroles"]
            join = []
            for rid in ar.get("join", []):
                try:
                    rid = int(rid)
                    if guild.get_role(rid):
                        join.append(rid)
                except (ValueError, TypeError):
                    continue

            timed = []
            for t in ar.get("timed", []):
                try:
                    rid = int(t["role_id"])
                    delay = str(t["delay"]).lower()
                    if guild.get_role(rid) and delay[-1] in "smh" and delay[:-1].isdigit():
                        timed.append({"role_id": rid, "delay": delay})
                except (ValueError, TypeError, KeyError):
                    continue
            update["autoroles"] = {"join": join, "timed": timed}

        if update:
            await guilds.update_one({"_id": int(guild_id)}, {"$set": update}, upsert=True)

    run_async(_save())
    return jsonify({"ok": True, "data": None})

# ──────────────────────── Mod Logs ────────────────────
@api.route("/api/guilds/<guild_id>/modlogs")
@require_owner
def api_modlogs(guild_id):
    page = max(1, int(request.args.get("page", 1)))
    limit = min(50, max(1, int(request.args.get("limit", 20))))
    action = request.args.get("action")
    moderator_id = request.args.get("moderator_id")
    search = request.args.get("search")

    async def _query():
        query = {"guild_id": int(guild_id)}
        if action:
            query["action"] = action
        if moderator_id:
            query["moderator_id"] = int(moderator_id)
        if search:
            query["$or"] = [
                {"target_name": {"$regex": search, "$options": "i"}},
                {"reason": {"$regex": search, "$options": "i"}},
                {"moderator_name": {"$regex": search, "$options": "i"}}
            ]

        total = await mod_logs.count_documents(query)
        cursor = mod_logs.find(query).sort("timestamp", -1).skip((page - 1) * limit).limit(limit)
        items = []
        async for doc in cursor:
            items.append({
                "id": str(doc["_id"]),
                "action": doc["action"],
                "moderator_id": str(doc["moderator_id"]),
                "moderator_name": doc.get("moderator_name"),
                "target_id": str(doc["target_id"]) if doc.get("target_id") else None,
                "target_name": doc.get("target_name"),
                "reason": doc.get("reason"),
                "duration": doc.get("duration"),
                "command_used": doc.get("command_used"),
                "channel_id": str(doc["channel_id"]) if doc.get("channel_id") else None,
                "source": doc.get("source", "discord"),
                "timestamp": doc["timestamp"].isoformat()
            })
        return {
            "items": items,
            "total": total,
            "page": page,
            "pages": max(1, (total + limit - 1) // limit)
        }

    return jsonify({"ok": True, "data": run_async(_query())})

# ──────────────────────── Member moderation ───────────
@api.route("/api/guilds/<guild_id>/members/<user_id>/moderation")
@require_owner
def member_moderation(guild_id, user_id):
    async def _get():
        cfg = await guilds.find_one({"_id": int(guild_id)})
        if not cfg:
            return {"warns": [], "notes": []}
        data = cfg.get("moderation", {}).get(str(user_id), {"warns": [], "notes": []})
        return data
    return jsonify({"ok": True, "data": run_async(_get())})

@api.route("/api/guilds/<guild_id>/members/<user_id>/warns/<warn_id>", methods=["DELETE"])
@require_owner
def delete_warn(guild_id, user_id, warn_id):
    async def _del():
        cfg = await guilds.find_one({"_id": int(guild_id)})
        if not cfg:
            return False
        data = cfg.get("moderation", {}).get(str(user_id), {"warns": [], "notes": []})
        original = len(data["warns"])
        data["warns"] = [w for w in data["warns"] if w["id"] != int(warn_id)]
        if len(data["warns"]) == original:
            return False
        for i, w in enumerate(data["warns"], 1):
            w["id"] = i
        await guilds.update_one(
            {"_id": int(guild_id)},
            {"$set": {f"moderation.{user_id}": data}}
        )
        return True

    ok = run_async(_del())
    if not ok:
        return jsonify({"ok": False, "error": "Warn not found"}), 404
    return jsonify({"ok": True, "data": None})

@api.route("/api/guilds/<guild_id>/members/<user_id>/notes", methods=["POST"])
@require_owner
def add_note(guild_id, user_id):
    body = request.get_json(silent=True) or {}
    content = body.get("content")
    if not content:
        return jsonify({"ok": False, "error": "content required"}), 400

    async def _add():
        cfg = await guilds.find_one({"_id": int(guild_id)})
        data = cfg.get("moderation", {}).get(str(user_id), {"warns": [], "notes": []}) if cfg else {"warns": [], "notes": []}
        note_id = len(data["notes"]) + 1
        data["notes"].append({
            "id": note_id,
            "content": content,
            "moderator": int(OWNER_ID),
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        await guilds.update_one(
            {"_id": int(guild_id)},
            {"$set": {f"moderation.{user_id}": data}},
            upsert=True
        )
        return note_id

    note_id = run_async(_add())
    return jsonify({"ok": True, "data": {"id": note_id}})

@api.route("/api/guilds/<guild_id>/members/<user_id>/notes/<note_id>", methods=["DELETE"])
@require_owner
def delete_note(guild_id, user_id, note_id):
    async def _del():
        cfg = await guilds.find_one({"_id": int(guild_id)})
        if not cfg:
            return False
        data = cfg.get("moderation", {}).get(str(user_id), {"warns": [], "notes": []})
        original = len(data["notes"])
        data["notes"] = [n for n in data["notes"] if n["id"] != int(note_id)]
        if len(data["notes"]) == original:
            return False
        for i, n in enumerate(data["notes"], 1):
            n["id"] = i
        await guilds.update_one(
            {"_id": int(guild_id)},
            {"$set": {f"moderation.{user_id}": data}}
        )
        return True

    ok = run_async(_del())
    if not ok:
        return jsonify({"ok": False, "error": "Note not found"}), 404
    return jsonify({"ok": True, "data": None})

# ──────────────────────── Scheduled ───────────────────
@api.route("/api/guilds/<guild_id>/scheduled")
@require_owner
def api_scheduled(guild_id):
    async def _get():
        cursor = timed_roles.find({"guild_id": int(guild_id)})
        items = []
        async for doc in cursor:
            items.append({
                "id": str(doc["_id"]),
                "type": doc.get("type", "role"),
                "user_id": str(doc.get("user_id") or doc.get("member_id")),
                "role_id": str(doc["role_id"]) if doc.get("role_id") else None,
                "execute_at": doc["execute_at"].isoformat()
            })
        return items
    return jsonify({"ok": True, "data": run_async(_get())})

@api.route("/api/guilds/<guild_id>/scheduled/<doc_id>", methods=["DELETE"])
@require_owner
def delete_scheduled(guild_id, doc_id):
    from bson import ObjectId
    async def _del():
        res = await timed_roles.delete_one({"_id": ObjectId(doc_id), "guild_id": int(guild_id)})
        return res.deleted_count > 0
    ok = run_async(_del())
    if not ok:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify({"ok": True, "data": None})

# ──────────────────────── Actions ─────────────────────
@api.route("/api/guilds/<guild_id>/actions/<action>", methods=["POST"])
@require_owner
@rate_limit(max_calls=15, period=60)
def api_action(guild_id, action):
    from bot import bot
    body = request.get_json(silent=True) or {}
    target_id = body.get("target_id")
    reason = body.get("reason", "No reason provided")
    duration = body.get("duration")

    if action not in ("ban", "tempban", "unban", "warn"):
        return jsonify({"ok": False, "error": "Invalid action"}), 400
    if not target_id:
        return jsonify({"ok": False, "error": "target_id required"}), 400

    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"ok": False, "error": "Guild not found"}), 404

    async def _execute():
        owner = await bot.fetch_user(int(OWNER_ID))

        if action == "unban":
            try:
                user = await bot.fetch_user(int(target_id))
                await guild.unban(user, reason=f"[Dashboard] {reason}")
                await timed_roles.delete_many({"type": "tempban", "guild_id": int(guild_id), "user_id": int(target_id)})
                await log_action(
                    int(guild_id), "unban", int(OWNER_ID), str(owner),
                    target_id=int(target_id), target_name=str(user),
                    reason=reason, source="dashboard"
                )
                return {"ok": True}
            except discord.NotFound:
                return {"error": "User not banned or not found"}

        member = guild.get_member(int(target_id))
        if not member:
            try:
                member = await guild.fetch_member(int(target_id))
            except discord.NotFound:
                return {"error": "Member not found in guild"}

        me = guild.me
        if member.top_role >= me.top_role:
            return {"error": "Cannot moderate this member (role hierarchy)"}

        if action == "ban":
            await member.ban(reason=f"[Dashboard] {reason}")
            await log_action(
                int(guild_id), "ban", int(OWNER_ID), str(owner),
                target_id=member.id, target_name=str(member),
                reason=reason, source="dashboard"
            )
            return {"ok": True}

        if action == "tempban":
            if not duration:
                return {"error": "duration required (e.g. 1h, 30m)"}
            unit = duration[-1].lower()
            try:
                value = int(duration[:-1])
            except ValueError:
                return {"error": "Invalid duration"}
            multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
            if unit not in multipliers:
                return {"error": "Invalid duration unit"}
            seconds = value * multipliers[unit]

            await member.ban(reason=f"[Dashboard] {reason} | {duration}")
            execute_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
            await timed_roles.insert_one({
                "type": "tempban",
                "guild_id": int(guild_id),
                "user_id": member.id,
                "execute_at": execute_at
            })
            await log_action(
                int(guild_id), "tempban", int(OWNER_ID), str(owner),
                target_id=member.id, target_name=str(member),
                reason=reason, duration=duration, source="dashboard"
            )
            return {"ok": True}

        if action == "warn":
            cfg = await guilds.find_one({"_id": int(guild_id)})
            data = cfg.get("moderation", {}).get(str(member.id), {"warns": [], "notes": []}) if cfg else {"warns": [], "notes": []}
            warn_id = len(data["warns"]) + 1
            data["warns"].append({
                "id": warn_id,
                "reason": reason,
                "moderator": int(OWNER_ID),
                "timestamp": datetime.now(timezone.utc).isoformat()
            })
            await guilds.update_one(
                {"_id": int(guild_id)},
                {"$set": {f"moderation.{member.id}": data}},
                upsert=True
            )
            await log_action(
                int(guild_id), "warn", int(OWNER_ID), str(owner),
                target_id=member.id, target_name=str(member),
                reason=reason, source="dashboard"
            )
            return {"ok": True, "warn_id": warn_id}

        return {"error": "Unknown action"}

    result = run_async(_execute())
    if "error" in result:
        return jsonify({"ok": False, "error": result["error"]}), 400
    return jsonify({"ok": True, "data": result})
