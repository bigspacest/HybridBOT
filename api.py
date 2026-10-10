import os
import re
import secrets
import time
from datetime import datetime, timezone, timedelta
from functools import wraps
from urllib.parse import urlencode

import discord
import requests
from bson import ObjectId
from flask import Blueprint, request, jsonify, redirect, make_response, send_file, g
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from config import (
    DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, REDIRECT_URI,
    SESSION_SECRET, DASHBOARD_URL, OWNER_ID,
)
from database import (
    guilds, timed_roles, mod_logs, log_action, db,
    panel_audit, panel_meta, scheduled_messages, role_panels, write_audit,
)

api = Blueprint("api", __name__)

serializer = URLSafeTimedSerializer(SESSION_SECRET)
COOKIE_NAME = "hybrid_session"
COOKIE_MAX_AGE = 86400

DB_COLLECTIONS = {
    "guilds": guilds,
    "timed_roles": timed_roles,
    "mod_logs": mod_logs,
}

_rate = {}

# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════

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
    future = __import__("asyncio").run_coroutine_threadsafe(coro, bot.loop)
    return future.result(timeout=25)


def client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.remote_addr or "")[:64]


def client_ua() -> str:
    return (request.headers.get("User-Agent") or "")[:200]


async def _get_session_version() -> int:
    meta = await panel_meta.find_one({"_id": "settings"})
    if not meta:
        return 1
    return int(meta.get("session_version", 1))


def create_session(user_id: str, version: int) -> str:
    return serializer.dumps({"uid": str(user_id), "v": int(version)})


def get_session_user() -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        data = serializer.loads(token, max_age=COOKIE_MAX_AGE)
        uid = str(data["uid"])
        cookie_v = int(data.get("v", 0))
        current_v = run_async(_get_session_version())
        if cookie_v != current_v:
            return None
        return uid
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError):
        return None


def require_owner(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        uid = get_session_user()
        if not uid or uid != str(OWNER_ID):
            return jsonify({"ok": False, "error": "Unauthorized"}), 401
        g.panel_user = uid
        return f(*args, **kwargs)
    return wrapped


def _parse_duration(duration: str) -> int | None:
    if not duration:
        return None
    unit = duration[-1].lower()
    try:
        value = int(duration[:-1])
    except ValueError:
        return None
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if unit not in multipliers:
        return None
    return value * multipliers[unit]


def _serialize_doc(doc: dict) -> dict:
    out = {}
    for k, v in doc.items():
        if isinstance(v, ObjectId):
            out[k] = str(v)
        elif isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            out[k] = v.isoformat()
        elif isinstance(v, int) and v > 10**15:
            out[k] = str(v)
        elif isinstance(v, dict):
            out[k] = _serialize_doc(v)
        elif isinstance(v, list):
            out[k] = [
                _serialize_doc(i) if isinstance(i, dict)
                else (str(i) if isinstance(i, ObjectId) else i)
                for i in v
            ]
        else:
            out[k] = v
    return out


def _modlogs_filter(guild_id, action=None, search=None, date_from=None, date_to=None, user_id=None):
    query = {"guild_id": int(guild_id)}
    or_clauses = []
    if action:
        query["action"] = action
    if search:
        safe = re.escape(search)
        or_clauses.extend([
            {"target_name": {"$regex": safe, "$options": "i"}},
            {"reason": {"$regex": safe, "$options": "i"}},
            {"moderator_name": {"$regex": safe, "$options": "i"}},
        ])
    if user_id is not None:
        try:
            uid = int(user_id)
            or_clauses.extend([{"target_id": uid}, {"moderator_id": uid}])
        except (ValueError, TypeError):
            pass
    if or_clauses:
        query["$or"] = or_clauses
    ts = {}
    if date_from:
        try:
            ts["$gte"] = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    if date_to:
        try:
            ts["$lte"] = datetime.fromisoformat(date_to).replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc
            )
        except ValueError:
            pass
    if ts:
        query["timestamp"] = ts
    return query


async def _get_alerts() -> dict:
    meta = await panel_meta.find_one({"_id": "settings"})
    if not meta:
        return {"login": True, "denied": True, "destructive": True}
    return meta.get("alerts") or {"login": True, "denied": True, "destructive": True}


async def _dm_owner(text: str):
    try:
        from bot import bot
        user = await bot.fetch_user(int(OWNER_ID))
        await user.send(text[:1900])
    except Exception:
        pass


def _make_preview(content, embed) -> str:
    if content:
        return str(content)[:60]
    if embed and isinstance(embed, dict):
        if embed.get("title"):
            return str(embed["title"])[:60]
        if embed.get("description"):
            return str(embed["description"])[:60]
    return "(sin texto)"


def _cache_avatar(bot, user_id) -> str | None:
    if not user_id:
        return None
    try:
        u = bot.get_user(int(user_id))
        if u:
            return str(u.display_avatar.url)
    except Exception:
        pass
    return None


def _audit_category(method: str, path: str) -> str | None:
    path = path.lower()
    if method == "DELETE":
        return "delete"
    if method in ("POST", "PUT"):
        if "/actions/" in path:
            return "action"
        if "/settings" in path:
            return "settings"
        if "/embeds" in path or "/rolepanels" in path or "/scheduled-messages" in path:
            return "embed"
    return None


@api.after_request
def audit_after_request(response):
    try:
        if not request.path.startswith("/api"):
            return response
        if request.method not in ("POST", "PUT", "DELETE"):
            return response
        cat = _audit_category(request.method, request.path)
        if not cat:
            return response
        detail = f"{request.method} {request.path}"[:300]
        run_async(write_audit(cat, detail, client_ip(), client_ua()))
        if cat == "delete" and response.status_code < 400:
            alerts = run_async(_get_alerts())
            if alerts.get("destructive"):
                run_async(_dm_owner(
                    f"⚠️ **Panel destructive action**\n`{detail}`\nIP: `{client_ip()}`"
                ))
    except Exception:
        pass
    return response


# ═══════════════════════════════════════════════════════════
# Dashboard
# ═══════════════════════════════════════════════════════════

@api.route("/dashboard")
def dashboard():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    return send_file(path)


# ═══════════════════════════════════════════════════════════
# Auth
# ═══════════════════════════════════════════════════════════

@api.route("/auth/login")
@rate_limit()
def auth_login():
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state,
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
    ip = client_ip()
    ua = client_ua()

    if not code or not state or state != stored:
        return jsonify({"ok": False, "error": "Invalid state or code"}), 400

    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    token_res = requests.post(
        "https://discord.com/api/oauth2/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if token_res.status_code != 200:
        return jsonify({"ok": False, "error": "Token exchange failed"}), 400

    access_token = token_res.json()["access_token"]
    user_res = requests.get(
        "https://discord.com/api/users/@me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if user_res.status_code != 200:
        return jsonify({"ok": False, "error": "Failed to fetch user"}), 400

    user = user_res.json()
    user_id = str(user["id"])
    username = user.get("global_name") or user.get("username", "?")

    if user_id != str(OWNER_ID):
        run_async(write_audit("login_denied", f"{username} ({user_id})", ip, ua))
        alerts = run_async(_get_alerts())
        if alerts.get("denied"):
            run_async(_dm_owner(
                f"🚫 **Login denied**\nUser: **{username}** (`{user_id}`)\nIP: `{ip}`\nUA: `{ua[:80]}`"
            ))
        return jsonify({"ok": False, "error": "Access denied. Owner only."}), 403

    version = run_async(_get_session_version())
    session_token = create_session(user_id, version)
    base = (DASHBOARD_URL or "").rstrip("/")
    resp = make_response(redirect(f"{base}/dashboard"))
    resp.set_cookie(
        COOKIE_NAME, session_token,
        httponly=True, secure=True, samesite="Lax", max_age=COOKIE_MAX_AGE,
    )
    resp.delete_cookie("oauth_state", samesite="Lax", secure=True)

    run_async(write_audit("login", f"Owner login from {ip}", ip, ua))
    alerts = run_async(_get_alerts())
    if alerts.get("login"):
        run_async(_dm_owner(f"✅ **Panel login**\nIP: `{ip}`\nDevice: `{ua[:100]}`"))
    return resp


@api.route("/auth/logout", methods=["POST"])
def auth_logout():
    run_async(write_audit("logout", "Owner logout", client_ip(), client_ua()))
    resp = make_response(jsonify({"ok": True, "data": None}))
    resp.delete_cookie(COOKIE_NAME, samesite="Lax", secure=True)
    return resp


@api.route("/api/me")
@require_owner
def api_me():
    return jsonify({"ok": True, "data": {"id": get_session_user(), "is_owner": True}})


# ═══════════════════════════════════════════════════════════
# Panel settings / audit
# ═══════════════════════════════════════════════════════════

@api.route("/api/panel/settings", methods=["GET"])
@require_owner
def panel_settings_get():
    async def _get():
        meta = await panel_meta.find_one({"_id": "settings"})
        if not meta:
            return {"alerts": {"login": True, "denied": True, "destructive": True}}
        return {
            "alerts": meta.get("alerts") or {"login": True, "denied": True, "destructive": True},
        }
    return jsonify({"ok": True, "data": run_async(_get())})


@api.route("/api/panel/settings", methods=["PUT"])
@require_owner
def panel_settings_put():
    body = request.get_json(silent=True) or {}
    alerts = body.get("alerts")
    if not isinstance(alerts, dict):
        return jsonify({"ok": False, "error": "alerts object required"}), 400

    async def _save():
        await panel_meta.update_one(
            {"_id": "settings"},
            {"$set": {
                "alerts": {
                    "login": bool(alerts.get("login", True)),
                    "denied": bool(alerts.get("denied", True)),
                    "destructive": bool(alerts.get("destructive", True)),
                }
            }},
            upsert=True,
        )
    run_async(_save())
    return jsonify({"ok": True, "data": None})


@api.route("/api/panel/revoke-sessions", methods=["POST"])
@require_owner
def panel_revoke_sessions():
    async def _revoke():
        meta = await panel_meta.find_one({"_id": "settings"})
        current = int((meta or {}).get("session_version", 1))
        new_v = current + 1
        await panel_meta.update_one(
            {"_id": "settings"},
            {"$set": {"session_version": new_v}},
            upsert=True,
        )
        return new_v

    new_v = run_async(_revoke())
    token = create_session(str(OWNER_ID), new_v)
    resp = make_response(jsonify({"ok": True, "data": {"session_version": new_v}}))
    resp.set_cookie(
        COOKIE_NAME, token,
        httponly=True, secure=True, samesite="Lax", max_age=COOKIE_MAX_AGE,
    )
    return resp


@api.route("/api/audit")
@require_owner
def api_audit():
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        limit = min(50, max(1, int(request.args.get("limit", 20))))
    except ValueError:
        limit = 20
    event = request.args.get("event")

    async def _query():
        query = {}
        if event:
            query["event"] = event
        total = await panel_audit.count_documents(query)
        cursor = panel_audit.find(query).sort("timestamp", -1).skip((page - 1) * limit).limit(limit)
        items = []
        async for doc in cursor:
            items.append({
                "id": str(doc["_id"]),
                "event": doc.get("event"),
                "detail": doc.get("detail"),
                "ip": doc.get("ip"),
                "user_agent": doc.get("user_agent"),
                "timestamp": doc["timestamp"].isoformat() if doc.get("timestamp") else None,
            })
        return {
            "items": items,
            "total": total,
            "page": page,
            "pages": max(1, (total + limit - 1) // limit),
        }
    return jsonify({"ok": True, "data": run_async(_query())})


# ═══════════════════════════════════════════════════════════
# Status / Guilds
# ═══════════════════════════════════════════════════════════

@api.route("/api/status")
@require_owner
def api_status():
    from bot import bot
    uptime = int(time.time() - bot.start_time) if getattr(bot, "start_time", None) else 0
    return jsonify({"ok": True, "data": {
        "latency_ms": round(bot.latency * 1000) if bot.latency else 0,
        "uptime_seconds": uptime,
        "guilds": len(bot.guilds),
        "users": sum(g.member_count or 0 for g in bot.guilds),
    }})


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
            "member_count": g.member_count,
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


@api.route("/api/guilds/<guild_id>/members/search")
@require_owner
def members_search(guild_id):
    from bot import bot
    q = (request.args.get("q") or "").lower().strip()
    if len(q) < 1:
        return jsonify({"ok": True, "data": []})
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"ok": False, "error": "Guild not found"}), 404
    results = []
    for m in guild.members:
        if q in m.name.lower() or (m.nick and q in m.nick.lower()) or q in str(m.id):
            results.append({
                "id": str(m.id),
                "name": str(m),
                "avatar_url": str(m.display_avatar.url),
            })
            if len(results) >= 8:
                break
    return jsonify({"ok": True, "data": results})


# ═══════════════════════════════════════════════════════════
# Profile
# ═══════════════════════════════════════════════════════════

@api.route("/api/guilds/<guild_id>/members/<user_id>/profile")
@require_owner
def member_profile(guild_id, user_id):
    from bot import bot
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"ok": False, "error": "Guild not found"}), 404

    async def _profile():
        member = guild.get_member(int(user_id))
        if member:
            return {
                "id": str(member.id),
                "name": str(member),
                "avatar_url": str(member.display_avatar.url),
                "created_at": member.created_at.isoformat(),
                "joined_at": member.joined_at.isoformat() if member.joined_at else None,
                "roles": [
                    {"id": str(r.id), "name": r.name, "color": str(r.color)}
                    for r in member.roles if not r.is_default()
                ],
                "is_bot": member.bot,
                "in_guild": True,
            }
        try:
            user = await bot.fetch_user(int(user_id))
        except Exception:
            return None
        return {
            "id": str(user.id),
            "name": str(user),
            "avatar_url": str(user.display_avatar.url),
            "created_at": user.created_at.isoformat(),
            "joined_at": None,
            "roles": [],
            "is_bot": user.bot,
            "in_guild": False,
        }

    data = run_async(_profile())
    if data is None:
        return jsonify({"ok": False, "error": "User not found"}), 404
    return jsonify({"ok": True, "data": data})


# ═══════════════════════════════════════════════════════════
# Overview
# ═══════════════════════════════════════════════════════════

@api.route("/api/guilds/<guild_id>/overview")
@require_owner
def api_overview(guild_id):
    from bot import bot
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"ok": False, "error": "Guild not found"}), 404

    try:
        days = int(request.args.get("days", "7"))
        if days not in (7, 30, 90):
            days = 7
    except ValueError:
        days = 7

    try:
        tz_offset = int(request.args.get("tz", "0"))
        if tz_offset < -720 or tz_offset > 840:
            tz_offset = 0
    except ValueError:
        tz_offset = 0

    async def _overview():
        gid = int(guild_id)
        now = datetime.now(timezone.utc)
        period = timedelta(days=days)
        since = now - period
        prev_since = since - period
        prev_until = since

        total_bans = await mod_logs.count_documents({"guild_id": gid, "action": "ban"})
        total_warns = await mod_logs.count_documents({"guild_id": gid, "action": "warn"})
        tempbans_pending = await timed_roles.count_documents({"guild_id": gid, "type": "tempban"})
        timed_roles_pending = await timed_roles.count_documents({
            "guild_id": gid,
            "$or": [{"type": {"$exists": False}}, {"type": "role"}],
        })

        async def _count(action, start, end):
            return await mod_logs.count_documents({
                "guild_id": gid,
                "action": action,
                "timestamp": {"$gte": start, "$lt": end},
            })

        joins_now = await _count("join", since, now)
        leaves_now = await _count("leave", since, now)
        joins_prev = await _count("join", prev_since, prev_until)
        leaves_prev = await _count("leave", prev_since, prev_until)
        bans_now = await _count("ban", since, now)
        bans_prev = await _count("ban", prev_since, prev_until)
        warns_now = await _count("warn", since, now)
        warns_prev = await _count("warn", prev_since, prev_until)
        cmds_now = await _count("command", since, now)
        cmds_prev = await _count("command", prev_since, prev_until)

        trend = {
            "members": {"now": joins_now - leaves_now, "prev": joins_prev - leaves_prev},
            "bans": {"now": bans_now, "prev": bans_prev},
            "warns": {"now": warns_now, "prev": warns_prev},
            "commands": {"now": cmds_now, "prev": cmds_prev},
        }

        pipeline_action = [
            {"$match": {
                "guild_id": gid,
                "timestamp": {"$gte": since, "$lt": now},
                "action": {"$nin": ["command", "join", "leave", "msgdelete"]},
            }},
            {"$group": {"_id": "$action", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
        ]
        by_action = [
            {"action": d["_id"], "count": d["count"]}
            async for d in mod_logs.aggregate(pipeline_action)
        ]

        # actions_per_day con tz
        actions_per_day = []
        try:
            pipeline_day = [
                {"$match": {
                    "guild_id": gid,
                    "timestamp": {"$gte": since, "$lt": now},
                    "action": {"$nin": ["command", "join", "leave", "msgdelete"]},
                }},
                {"$project": {
                    "local": {
                        "$dateAdd": {
                            "startDate": "$timestamp",
                            "unit": "minute",
                            "amount": tz_offset,
                        }
                    }
                }},
                {"$group": {
                    "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$local"}},
                    "count": {"$sum": 1},
                }},
                {"$sort": {"_id": 1}},
            ]
            actions_per_day = [
                {"date": d["_id"], "count": d["count"]}
                async for d in mod_logs.aggregate(pipeline_day)
            ]
        except Exception:
            pipeline_day_fb = [
                {"$match": {
                    "guild_id": gid,
                    "timestamp": {"$gte": since, "$lt": now},
                    "action": {"$nin": ["command", "join", "leave", "msgdelete"]},
                }},
                {"$group": {
                    "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$timestamp"}},
                    "count": {"$sum": 1},
                }},
                {"$sort": {"_id": 1}},
            ]
            actions_per_day = [
                {"date": d["_id"], "count": d["count"]}
                async for d in mod_logs.aggregate(pipeline_day_fb)
            ]

        # growth
        growth_map = {}
        try:
            pipeline_growth = [
                {"$match": {
                    "guild_id": gid,
                    "timestamp": {"$gte": since, "$lt": now},
                    "action": {"$in": ["join", "leave"]},
                }},
                {"$project": {
                    "action": 1,
                    "local": {
                        "$dateAdd": {
                            "startDate": "$timestamp",
                            "unit": "minute",
                            "amount": tz_offset,
                        }
                    },
                }},
                {"$group": {
                    "_id": {
                        "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$local"}},
                        "action": "$action",
                    },
                    "count": {"$sum": 1},
                }},
            ]
            async for d in mod_logs.aggregate(pipeline_growth):
                date = d["_id"]["date"]
                act = d["_id"]["action"]
                if date not in growth_map:
                    growth_map[date] = {"date": date, "joins": 0, "leaves": 0}
                if act == "join":
                    growth_map[date]["joins"] = d["count"]
                else:
                    growth_map[date]["leaves"] = d["count"]
        except Exception:
            pipeline_growth_fb = [
                {"$match": {
                    "guild_id": gid,
                    "timestamp": {"$gte": since, "$lt": now},
                    "action": {"$in": ["join", "leave"]},
                }},
                {"$group": {
                    "_id": {
                        "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$timestamp"}},
                        "action": "$action",
                    },
                    "count": {"$sum": 1},
                }},
            ]
            async for d in mod_logs.aggregate(pipeline_growth_fb):
                date = d["_id"]["date"]
                act = d["_id"]["action"]
                if date not in growth_map:
                    growth_map[date] = {"date": date, "joins": 0, "leaves": 0}
                if act == "join":
                    growth_map[date]["joins"] = d["count"]
                else:
                    growth_map[date]["leaves"] = d["count"]
        growth = sorted(growth_map.values(), key=lambda x: x["date"])

        # heatmap 7x24 (lun=0 … dom=6)
        heatmap = [[0 for _ in range(24)] for _ in range(7)]
        try:
            pipeline_heat = [
                {"$match": {
                    "guild_id": gid,
                    "timestamp": {"$gte": since, "$lt": now},
                }},
                {"$project": {
                    "local": {
                        "$dateAdd": {
                            "startDate": "$timestamp",
                            "unit": "minute",
                            "amount": tz_offset,
                        }
                    }
                }},
                {"$group": {
                    "_id": {
                        "dow": {"$subtract": [{"$dayOfWeek": "$local"}, 2]},
                        "hour": {"$hour": "$local"},
                    },
                    "count": {"$sum": 1},
                }},
            ]
            async for d in mod_logs.aggregate(pipeline_heat):
                dow = d["_id"]["dow"]
                if dow < 0:
                    dow = 6
                hour = d["_id"]["hour"]
                if 0 <= dow <= 6 and 0 <= hour <= 23:
                    heatmap[dow][hour] = d["count"]
        except Exception:
            cursor = mod_logs.find({
                "guild_id": gid,
                "timestamp": {"$gte": since, "$lt": now},
            }, {"timestamp": 1})
            async for doc in cursor:
                ts = doc["timestamp"]
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                local = ts + timedelta(minutes=tz_offset)
                heatmap[local.weekday()][local.hour] += 1

        # top_moderators + avatar
        pipeline2 = [
            {"$match": {
                "guild_id": gid,
                "timestamp": {"$gte": since, "$lt": now},
                "action": {"$nin": ["command", "join", "leave", "msgdelete"]},
            }},
            {"$group": {
                "_id": {"id": "$moderator_id", "name": "$moderator_name"},
                "count": {"$sum": 1},
            }},
            {"$sort": {"count": -1}},
            {"$limit": 5},
        ]
        top_moderators = []
        async for d in mod_logs.aggregate(pipeline2):
            mid = d["_id"].get("id")
            name = d["_id"].get("name") or "?"
            top_moderators.append({
                "name": name,
                "count": d["count"],
                "avatar_url": _cache_avatar(bot, mid),
            })

        return {
            "member_count": guild.member_count,
            "total_bans": total_bans,
            "total_warns": total_warns,
            "tempbans_pending": tempbans_pending,
            "timed_roles_pending": timed_roles_pending,
            "commands_7d": cmds_now,
            "trend": trend,
            "by_action": by_action,
            "actions_per_day": actions_per_day,
            "growth": growth,
            "heatmap": heatmap,
            "top_moderators": top_moderators,
        }

    return jsonify({"ok": True, "data": run_async(_overview())})


# ═══════════════════════════════════════════════════════════
# Settings
# ═══════════════════════════════════════════════════════════

DEFAULT_AUTOMOD = {"anti_spam": False, "anti_links": False, "bad_words": []}
DEFAULT_WARN_PUNISHMENT = {"threshold": None, "action": None}


@api.route("/api/guilds/<guild_id>/settings", methods=["GET"])
@require_owner
def get_settings(guild_id):
    async def _get():
        cfg = await guilds.find_one({"_id": int(guild_id)})
        if not cfg:
            return {
                "admin_roles": [], "manager_roles": [], "staff_roles": [],
                "welcome": {"channel_id": None, "message": None},
                "autoroles": {"join": [], "timed": []},
                "automod": DEFAULT_AUTOMOD.copy(),
                "warn_punishment": DEFAULT_WARN_PUNISHMENT.copy(),
            }
        return {
            "admin_roles": [str(r) for r in cfg.get("admin_roles", [])],
            "manager_roles": [str(r) for r in cfg.get("manager_roles", [])],
            "staff_roles": [str(r) for r in cfg.get("staff_roles", [])],
            "welcome": {
                "channel_id": str(cfg["welcome"]["channel_id"]) if cfg.get("welcome", {}).get("channel_id") else None,
                "message": cfg.get("welcome", {}).get("message"),
            },
            "autoroles": {
                "join": [str(r) for r in cfg.get("autoroles", {}).get("join", [])],
                "timed": [
                    {"role_id": str(t["role_id"]), "delay": t["delay"]}
                    for t in cfg.get("autoroles", {}).get("timed", [])
                ],
            },
            "automod": cfg.get("automod") or DEFAULT_AUTOMOD.copy(),
            "warn_punishment": cfg.get("warn_punishment") or DEFAULT_WARN_PUNISHMENT.copy(),
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
            update["welcome"] = {"channel_id": channel_id, "message": w.get("message")}
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
        if "automod" in body and isinstance(body["automod"], dict):
            am = body["automod"]
            bad_words = am.get("bad_words") or []
            if not isinstance(bad_words, list):
                bad_words = []
            update["automod"] = {
                "anti_spam": bool(am.get("anti_spam")),
                "anti_links": bool(am.get("anti_links")),
                "bad_words": [str(w)[:50] for w in bad_words[:50]],
            }
        if "warn_punishment" in body and isinstance(body["warn_punishment"], dict):
            wp = body["warn_punishment"]
            threshold = wp.get("threshold")
            action = wp.get("action")
            if threshold is not None:
                try:
                    threshold = int(threshold)
                    if threshold < 1 or threshold > 20:
                        threshold = None
                except (ValueError, TypeError):
                    threshold = None
            if action not in ("timeout", "kick", "ban", None):
                action = None
            update["warn_punishment"] = {"threshold": threshold, "action": action}
        if update:
            await guilds.update_one({"_id": int(guild_id)}, {"$set": update}, upsert=True)

    run_async(_save())
    return jsonify({"ok": True, "data": None})


# ═══════════════════════════════════════════════════════════
# Mod Logs
# ═══════════════════════════════════════════════════════════

@api.route("/api/guilds/<guild_id>/modlogs")
@require_owner
def api_modlogs(guild_id):
    from bot import bot
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        limit = min(50, max(1, int(request.args.get("limit", 20))))
    except ValueError:
        limit = 20

    async def _query():
        query = _modlogs_filter(
            int(guild_id),
            action=request.args.get("action"),
            search=request.args.get("search"),
            date_from=request.args.get("from"),
            date_to=request.args.get("to"),
            user_id=request.args.get("user_id"),
        )
        moderator_id = request.args.get("moderator_id")
        if moderator_id:
            try:
                query["moderator_id"] = int(moderator_id)
            except ValueError:
                pass
        total = await mod_logs.count_documents(query)
        cursor = mod_logs.find(query).sort("timestamp", -1).skip((page - 1) * limit).limit(limit)
        items = []
        async for doc in cursor:
            items.append({
                "id": str(doc["_id"]),
                "action": doc["action"],
                "moderator_id": str(doc["moderator_id"]),
                "moderator_name": doc.get("moderator_name"),
                "moderator_avatar": _cache_avatar(bot, doc.get("moderator_id")),
                "target_id": str(doc["target_id"]) if doc.get("target_id") else None,
                "target_name": doc.get("target_name"),
                "target_avatar": _cache_avatar(bot, doc.get("target_id")),
                "reason": doc.get("reason"),
                "duration": doc.get("duration"),
                "command_used": doc.get("command_used"),
                "channel_id": str(doc["channel_id"]) if doc.get("channel_id") else None,
                "source": doc.get("source", "discord"),
                "timestamp": doc["timestamp"].isoformat(),
            })
        return {
            "items": items,
            "total": total,
            "page": page,
            "pages": max(1, (total + limit - 1) // limit),
        }
    return jsonify({"ok": True, "data": run_async(_query())})


@api.route("/api/guilds/<guild_id>/modlogs", methods=["DELETE"])
@require_owner
@rate_limit(max_calls=10, period=60)
def delete_modlogs(guild_id):
    body = request.get_json(silent=True) or {}
    gid = int(guild_id)

    async def _delete():
        deleted = 0
        if "ids" in body:
            ids = body["ids"]
            if not isinstance(ids, list) or len(ids) == 0:
                return {"error": "ids must be a non-empty list", "code": 400}
            if len(ids) > 500:
                return {"error": "max 500 ids", "code": 400}
            object_ids = []
            for i in ids:
                try:
                    object_ids.append(ObjectId(str(i)))
                except Exception:
                    return {"error": f"invalid id: {i}", "code": 400}
            res = await mod_logs.delete_many({"_id": {"$in": object_ids}, "guild_id": gid})
            deleted = res.deleted_count
        elif body.get("all") is True:
            if body.get("confirm") != "ELIMINAR":
                return {"error": 'confirm must be "ELIMINAR"', "code": 400}
            filters = body.get("filters") or {}
            query = _modlogs_filter(
                gid,
                action=filters.get("action"),
                search=filters.get("search"),
                date_from=filters.get("from"),
                date_to=filters.get("to"),
            )
            res = await mod_logs.delete_many(query)
            deleted = res.deleted_count
        else:
            return {"error": "provide ids or {all: true, confirm: ELIMINAR}", "code": 400}

        await log_action(
            gid, "logs_cleared", int(OWNER_ID), "Dashboard",
            reason=f"Deleted {deleted} mod log(s)", source="dashboard",
        )
        alerts = await _get_alerts()
        if alerts.get("destructive") and deleted > 0:
            await _dm_owner(f"🗑️ **Mod logs cleared**\nGuild `{gid}`\nDeleted: **{deleted}**")
        return {"deleted": deleted}

    result = run_async(_delete())
    if "error" in result:
        return jsonify({"ok": False, "error": result["error"]}), result.get("code", 400)
    return jsonify({"ok": True, "data": result})


# ═══════════════════════════════════════════════════════════
# Member moderation
# ═══════════════════════════════════════════════════════════

@api.route("/api/guilds/<guild_id>/members/<user_id>/moderation")
@require_owner
def member_moderation(guild_id, user_id):
    async def _get():
        cfg = await guilds.find_one({"_id": int(guild_id)})
        if not cfg:
            return {"warns": [], "notes": []}
        return cfg.get("moderation", {}).get(str(user_id), {"warns": [], "notes": []})
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
        await guilds.update_one({"_id": int(guild_id)}, {"$set": {f"moderation.{user_id}": data}})
        return True
    if not run_async(_del()):
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
        data = (cfg.get("moderation", {}).get(str(user_id), {"warns": [], "notes": []})
                if cfg else {"warns": [], "notes": []})
        note_id = len(data["notes"]) + 1
        data["notes"].append({
            "id": note_id, "content": content, "moderator": int(OWNER_ID),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        await guilds.update_one(
            {"_id": int(guild_id)},
            {"$set": {f"moderation.{user_id}": data}},
            upsert=True,
        )
        return note_id
    return jsonify({"ok": True, "data": {"id": run_async(_add())}})


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
        await guilds.update_one({"_id": int(guild_id)}, {"$set": {f"moderation.{user_id}": data}})
        return True
    if not run_async(_del()):
        return jsonify({"ok": False, "error": "Note not found"}), 404
    return jsonify({"ok": True, "data": None})


# ═══════════════════════════════════════════════════════════
# Timed roles / tempbans
# ═══════════════════════════════════════════════════════════

@api.route("/api/guilds/<guild_id>/scheduled")
@require_owner
def api_scheduled(guild_id):
    from bot import bot

    async def _get():
        cursor = timed_roles.find({"guild_id": int(guild_id)})
        items = []
        async for doc in cursor:
            uid = doc.get("user_id") or doc.get("member_id")
            user_name = None
            if uid:
                user = bot.get_user(uid)
                if not user:
                    try:
                        user = await bot.fetch_user(uid)
                    except Exception:
                        user = None
                user_name = str(user) if user else str(uid)
            items.append({
                "id": str(doc["_id"]),
                "type": doc.get("type", "role"),
                "user_id": str(uid) if uid else None,
                "user_name": user_name,
                "role_id": str(doc["role_id"]) if doc.get("role_id") else None,
                "execute_at": doc["execute_at"].isoformat(),
            })
        return items
    return jsonify({"ok": True, "data": run_async(_get())})


@api.route("/api/guilds/<guild_id>/scheduled/<doc_id>", methods=["DELETE"])
@require_owner
def delete_scheduled(guild_id, doc_id):
    async def _del():
        try:
            res = await timed_roles.delete_one({"_id": ObjectId(doc_id), "guild_id": int(guild_id)})
            return res.deleted_count > 0
        except Exception:
            return False
    if not run_async(_del()):
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify({"ok": True, "data": None})


# ═══════════════════════════════════════════════════════════
# Scheduled messages
# ═══════════════════════════════════════════════════════════

@api.route("/api/guilds/<guild_id>/scheduled-messages", methods=["GET"])
@require_owner
def list_scheduled_messages(guild_id):
    async def _get():
        cursor = scheduled_messages.find({"guild_id": int(guild_id)}).sort("send_at", 1)
        items = []
        async for doc in cursor:
            items.append({
                "id": str(doc["_id"]),
                "channel_id": str(doc["channel_id"]),
                "preview": doc.get("preview"),
                "send_at": doc["send_at"].isoformat(),
                "repeat": doc.get("repeat", "none"),
            })
        return items
    return jsonify({"ok": True, "data": run_async(_get())})


@api.route("/api/guilds/<guild_id>/scheduled-messages", methods=["POST"])
@require_owner
def create_scheduled_message(guild_id):
    from bot import bot
    body = request.get_json(silent=True) or {}
    channel_id = body.get("channel_id")
    send_at = body.get("send_at")
    repeat = body.get("repeat") or "none"
    if not channel_id or not send_at:
        return jsonify({"ok": False, "error": "channel_id y send_at son obligatorios"}), 400
    if repeat not in ("none", "daily", "weekly", "monthly"):
        return jsonify({"ok": False, "error": "repeat inválido"}), 400

    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"ok": False, "error": "Servidor no encontrado"}), 404
    channel = guild.get_channel(int(channel_id))
    if not channel or not isinstance(channel, discord.TextChannel):
        return jsonify({"ok": False, "error": "Canal no encontrado"}), 404

    try:
        when = datetime.fromisoformat(send_at.replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except ValueError:
        return jsonify({"ok": False, "error": "send_at debe ser ISO UTC"}), 400

    if when <= datetime.now(timezone.utc):
        return jsonify({"ok": False, "error": "send_at debe ser una fecha futura"}), 400

    content = body.get("content")
    embed = body.get("embed")
    buttons = body.get("buttons") or []
    preview = _make_preview(content, embed)

    async def _create():
        res = await scheduled_messages.insert_one({
            "guild_id": int(guild_id),
            "channel_id": int(channel_id),
            "content": content,
            "embed": embed,
            "buttons": buttons[:5] if isinstance(buttons, list) else [],
            "send_at": when,
            "repeat": repeat,
            "preview": preview,
        })
        return str(res.inserted_id)

    return jsonify({"ok": True, "data": {"id": run_async(_create())}})


@api.route("/api/guilds/<guild_id>/scheduled-messages/<msg_id>", methods=["DELETE"])
@require_owner
def delete_scheduled_message(guild_id, msg_id):
    async def _del():
        try:
            res = await scheduled_messages.delete_one({
                "_id": ObjectId(msg_id),
                "guild_id": int(guild_id),
            })
            return res.deleted_count > 0
        except Exception:
            return False
    if not run_async(_del()):
        return jsonify({"ok": False, "error": "No encontrado"}), 404
    return jsonify({"ok": True, "data": None})


# ═══════════════════════════════════════════════════════════
# Role panels
# ═══════════════════════════════════════════════════════════

@api.route("/api/guilds/<guild_id>/rolepanels", methods=["GET"])
@require_owner
def list_rolepanels(guild_id):
    async def _get():
        cursor = role_panels.find({"guild_id": int(guild_id)})
        items = []
        async for doc in cursor:
            items.append({
                "id": str(doc["_id"]),
                "channel_id": str(doc["channel_id"]),
                "title": doc.get("title"),
                "buttons": len(doc.get("buttons") or []),
            })
        return items
    return jsonify({"ok": True, "data": run_async(_get())})


@api.route("/api/guilds/<guild_id>/rolepanels", methods=["POST"])
@require_owner
def create_rolepanel(guild_id):
    from bot import bot
    from rolepanels import RolePanelView

    body = request.get_json(silent=True) or {}
    channel_id = body.get("channel_id")
    title = (body.get("title") or "Roles")[:256]
    description = (body.get("description") or "")[:4096]
    exclusive = bool(body.get("exclusive"))
    buttons = body.get("buttons") or []
    try:
        color = int(body.get("color") if body.get("color") is not None else 0x5865F2)
    except (ValueError, TypeError):
        color = 0x5865F2

    if not channel_id:
        return jsonify({"ok": False, "error": "channel_id es obligatorio"}), 400
    if not isinstance(buttons, list) or len(buttons) == 0:
        return jsonify({"ok": False, "error": "Necesitas al menos un botón"}), 400
    if len(buttons) > 25:
        return jsonify({"ok": False, "error": "Máximo 25 botones"}), 400

    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"ok": False, "error": "Servidor no encontrado"}), 404
    channel = guild.get_channel(int(channel_id))
    if not channel or not isinstance(channel, discord.TextChannel):
        return jsonify({"ok": False, "error": "Canal no encontrado"}), 404

    me = guild.me
    bot_top = me.top_role if me else None
    valid_buttons = []

    for b in buttons:
        if not isinstance(b, dict):
            return jsonify({"ok": False, "error": "Formato de botón inválido"}), 400
        try:
            rid = int(b["role_id"])
        except (KeyError, ValueError, TypeError):
            return jsonify({"ok": False, "error": "role_id inválido"}), 400
        role = guild.get_role(rid)
        if not role:
            return jsonify({"ok": False, "error": f"El rol {rid} no existe"}), 400
        if role.is_default():
            return jsonify({"ok": False, "error": "No se puede usar @everyone"}), 400
        if role.managed:
            return jsonify({"ok": False, "error": f"El rol «{role.name}» es de una integración y no se puede asignar"}), 400
        if bot_top and role >= bot_top:
            return jsonify({"ok": False, "error": f"El rol «{role.name}» está por encima (o al mismo nivel) del rol del bot"}), 400
        style = (b.get("style") or "secondary").lower()
        if style not in ("primary", "secondary", "success", "danger"):
            style = "secondary"
        valid_buttons.append({
            "role_id": rid,
            "label": (b.get("label") or role.name)[:80],
            "emoji": b.get("emoji") or None,
            "style": style,
        })

    async def _create():
        res = await role_panels.insert_one({
            "guild_id": int(guild_id),
            "channel_id": int(channel_id),
            "message_id": None,
            "title": title,
            "exclusive": exclusive,
            "buttons": valid_buttons,
        })
        panel_id = str(res.inserted_id)
        embed = discord.Embed(title=title, description=description or None, color=color)
        view = RolePanelView(panel_id, valid_buttons)
        try:
            msg = await channel.send(embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException) as e:
            await role_panels.delete_one({"_id": res.inserted_id})
            return {"error": f"No pude publicar el mensaje: {e}"}
        await role_panels.update_one({"_id": res.inserted_id}, {"$set": {"message_id": msg.id}})
        bot.add_view(view)
        return {"id": panel_id}

    result = run_async(_create())
    if "error" in result:
        return jsonify({"ok": False, "error": result["error"]}), 400
    return jsonify({"ok": True, "data": result})


@api.route("/api/guilds/<guild_id>/rolepanels/<panel_id>", methods=["DELETE"])
@require_owner
def delete_rolepanel(guild_id, panel_id):
    from bot import bot

    async def _del():
        try:
            oid = ObjectId(panel_id)
        except Exception:
            return {"error": "ID inválido", "code": 400}
        doc = await role_panels.find_one({"_id": oid, "guild_id": int(guild_id)})
        if not doc:
            return {"error": "No encontrado", "code": 404}
        channel = bot.get_channel(doc.get("channel_id"))
        if channel and doc.get("message_id"):
            try:
                msg = await channel.fetch_message(doc["message_id"])
                await msg.delete()
            except Exception:
                pass
        await role_panels.delete_one({"_id": oid})
        return {"deleted": 1}

    result = run_async(_del())
    if "error" in result:
        return jsonify({"ok": False, "error": result["error"]}), result.get("code", 400)
    return jsonify({"ok": True, "data": result})


# ═══════════════════════════════════════════════════════════
# DB Explorer
# ═══════════════════════════════════════════════════════════

@api.route("/api/guilds/<guild_id>/db")
@require_owner
def api_db_overview(guild_id):
    gid = int(guild_id)

    async def _get():
        collections = [
            {"name": "guilds", "count": await guilds.count_documents({"_id": gid})},
            {"name": "timed_roles", "count": await timed_roles.count_documents({"guild_id": gid})},
            {"name": "mod_logs", "count": await mod_logs.count_documents({"guild_id": gid})},
        ]
        result = {"collections": collections}
        try:
            stats = await db.command("dbstats")
            result["storage"] = {"used_bytes": int(stats.get("dataSize", 0)), "limit_bytes": None}
        except Exception:
            pass
        return result
    return jsonify({"ok": True, "data": run_async(_get())})


@api.route("/api/guilds/<guild_id>/db/<collection>")
@require_owner
def api_db_collection(guild_id, collection):
    if collection not in DB_COLLECTIONS:
        return jsonify({"ok": False, "error": "Collection not found"}), 404
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        limit = min(50, max(1, int(request.args.get("limit", 20))))
    except ValueError:
        limit = 20
    gid = int(guild_id)
    coll = DB_COLLECTIONS[collection]

    async def _get():
        query = {"_id": gid} if collection == "guilds" else {"guild_id": gid}
        total = await coll.count_documents(query)
        cursor = coll.find(query).skip((page - 1) * limit).limit(limit)
        items = [_serialize_doc(doc) async for doc in cursor]
        return {
            "items": items,
            "total": total,
            "page": page,
            "pages": max(1, (total + limit - 1) // limit),
        }
    return jsonify({"ok": True, "data": run_async(_get())})


@api.route("/api/guilds/<guild_id>/db/<collection>/<doc_id>", methods=["DELETE"])
@require_owner
@rate_limit(max_calls=20, period=60)
def api_db_delete_doc(guild_id, collection, doc_id):
    if collection == "guilds":
        return jsonify({"ok": False, "error": "Cannot delete guild config documents"}), 403
    if collection not in ("timed_roles", "mod_logs"):
        return jsonify({"ok": False, "error": "Collection not found"}), 404
    coll = DB_COLLECTIONS[collection]
    gid = int(guild_id)

    async def _del():
        try:
            oid = ObjectId(doc_id)
        except Exception:
            return {"error": "Invalid document id", "code": 400}
        res = await coll.delete_one({"_id": oid, "guild_id": gid})
        if res.deleted_count == 0:
            return {"error": "Not found", "code": 404}
        return {"deleted": 1}

    result = run_async(_del())
    if "error" in result:
        return jsonify({"ok": False, "error": result["error"]}), result.get("code", 400)
    return jsonify({"ok": True, "data": result})


# ═══════════════════════════════════════════════════════════
# Actions
# ═══════════════════════════════════════════════════════════

@api.route("/api/guilds/<guild_id>/actions/<action>", methods=["POST"])
@require_owner
@rate_limit(max_calls=15, period=60)
def api_action(guild_id, action):
    from bot import bot
    body = request.get_json(silent=True) or {}
    target_id = body.get("target_id")
    reason = body.get("reason", "No reason provided")
    duration = body.get("duration")
    channel_id = body.get("channel_id")
    seconds = body.get("seconds")
    amount = body.get("amount")

    allowed = (
        "ban", "tempban", "unban", "warn", "kick", "timeout", "untimeout",
        "lock", "unlock", "slowmode", "purge",
    )
    if action not in allowed:
        return jsonify({"ok": False, "error": "Invalid action"}), 400

    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"ok": False, "error": "Guild not found"}), 404

    async def _execute():
        owner = await bot.fetch_user(int(OWNER_ID))

        if action in ("lock", "unlock", "slowmode", "purge"):
            if not channel_id:
                return {"error": "channel_id required"}
            channel = guild.get_channel(int(channel_id))
            if not channel or not isinstance(channel, discord.TextChannel):
                return {"error": "Channel not found"}
            try:
                if action == "lock":
                    ow = channel.overwrites_for(guild.default_role)
                    ow.send_messages = False
                    await channel.set_permissions(guild.default_role, overwrite=ow)
                    await log_action(int(guild_id), "lock", int(OWNER_ID), str(owner),
                                     channel_id=channel.id, source="dashboard")
                    return {"ok": True}
                if action == "unlock":
                    ow = channel.overwrites_for(guild.default_role)
                    ow.send_messages = None
                    await channel.set_permissions(guild.default_role, overwrite=ow)
                    await log_action(int(guild_id), "unlock", int(OWNER_ID), str(owner),
                                     channel_id=channel.id, source="dashboard")
                    return {"ok": True}
                if action == "slowmode":
                    val = int(seconds) if seconds is not None else 0
                    if val < 0 or val > 21600:
                        return {"error": "seconds must be 0–21600"}
                    await channel.edit(slowmode_delay=val)
                    await log_action(int(guild_id), "slowmode", int(OWNER_ID), str(owner),
                                     channel_id=channel.id, duration=str(val), source="dashboard")
                    return {"ok": True}
                if action == "purge":
                    amt = int(amount) if amount else 0
                    if amt < 1 or amt > 500:
                        return {"error": "amount must be 1–500"}
                    deleted = await channel.purge(limit=amt)
                    await log_action(int(guild_id), "purge", int(OWNER_ID), str(owner),
                                     reason=f"{len(deleted)} messages",
                                     channel_id=channel.id, source="dashboard")
                    return {"ok": True, "deleted": len(deleted)}
            except (discord.Forbidden, discord.HTTPException) as e:
                return {"error": str(e)}

        if action == "unban":
            if not target_id:
                return {"error": "target_id required"}
            try:
                user = await bot.fetch_user(int(target_id))
                await guild.unban(user, reason=f"[Dashboard] {reason}")
                await timed_roles.delete_many({
                    "type": "tempban", "guild_id": int(guild_id), "user_id": int(target_id),
                })
                await log_action(int(guild_id), "unban", int(OWNER_ID), str(owner),
                                 target_id=int(target_id), target_name=str(user),
                                 reason=reason, source="dashboard")
                return {"ok": True}
            except discord.NotFound:
                return {"error": "User not banned or not found"}
            except (discord.Forbidden, discord.HTTPException) as e:
                return {"error": str(e)}

        if not target_id:
            return {"error": "target_id required"}

        member = guild.get_member(int(target_id))
        if not member and action not in ("ban", "tempban"):
            try:
                member = await guild.fetch_member(int(target_id))
            except discord.NotFound:
                return {"error": "Member not found in guild"}

        if member and guild.me and member.top_role >= guild.me.top_role:
            return {"error": "Cannot moderate this member (role hierarchy)"}

        try:
            if action == "ban":
                target = member or discord.Object(id=int(target_id))
                await guild.ban(target, reason=f"[Dashboard] {reason}")
                name = str(member) if member else str(target_id)
                await log_action(int(guild_id), "ban", int(OWNER_ID), str(owner),
                                 target_id=int(target_id), target_name=name,
                                 reason=reason, source="dashboard")
                return {"ok": True}

            if action == "tempban":
                if not duration:
                    return {"error": "duration required"}
                secs = _parse_duration(duration)
                if secs is None:
                    return {"error": "Invalid duration"}
                target = member or discord.Object(id=int(target_id))
                await guild.ban(target, reason=f"[Dashboard] {reason} | {duration}")
                await timed_roles.insert_one({
                    "type": "tempban",
                    "guild_id": int(guild_id),
                    "user_id": int(target_id),
                    "execute_at": datetime.now(timezone.utc) + timedelta(seconds=secs),
                })
                name = str(member) if member else str(target_id)
                await log_action(int(guild_id), "tempban", int(OWNER_ID), str(owner),
                                 target_id=int(target_id), target_name=name,
                                 reason=reason, duration=duration, source="dashboard")
                return {"ok": True}

            if action == "kick":
                if not member:
                    return {"error": "Member not found"}
                await member.kick(reason=f"[Dashboard] {reason}")
                await log_action(int(guild_id), "kick", int(OWNER_ID), str(owner),
                                 target_id=member.id, target_name=str(member),
                                 reason=reason, source="dashboard")
                return {"ok": True}

            if action == "timeout":
                if not member:
                    return {"error": "Member not found"}
                if not duration:
                    return {"error": "duration required"}
                secs = _parse_duration(duration)
                if secs is None or secs > 2419200:
                    return {"error": "Invalid duration (max 28d)"}
                await member.timeout(timedelta(seconds=secs), reason=f"[Dashboard] {reason}")
                await log_action(int(guild_id), "timeout", int(OWNER_ID), str(owner),
                                 target_id=member.id, target_name=str(member),
                                 reason=reason, duration=duration, source="dashboard")
                return {"ok": True}

            if action == "untimeout":
                if not member:
                    return {"error": "Member not found"}
                await member.timeout(None)
                await log_action(int(guild_id), "untimeout", int(OWNER_ID), str(owner),
                                 target_id=member.id, target_name=str(member), source="dashboard")
                return {"ok": True}

            if action == "warn":
                if not member:
                    return {"error": "Member not found"}
                cfg = await guilds.find_one({"_id": int(guild_id)})
                data = (cfg.get("moderation", {}).get(str(member.id), {"warns": [], "notes": []})
                        if cfg else {"warns": [], "notes": []})
                warn_id = len(data["warns"]) + 1
                data["warns"].append({
                    "id": warn_id, "reason": reason, "moderator": int(OWNER_ID),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                await guilds.update_one(
                    {"_id": int(guild_id)},
                    {"$set": {f"moderation.{member.id}": data}},
                    upsert=True,
                )
                await log_action(int(guild_id), "warn", int(OWNER_ID), str(owner),
                                 target_id=member.id, target_name=str(member),
                                 reason=reason, source="dashboard")
                return {"ok": True, "warn_id": warn_id}
        except (discord.Forbidden, discord.HTTPException) as e:
            return {"error": str(e)}
        return {"error": "Unknown action"}

    result = run_async(_execute())
    if "error" in result:
        return jsonify({"ok": False, "error": result["error"]}), 400
    return jsonify({"ok": True, "data": result})


# ═══════════════════════════════════════════════════════════
# Embeds
# ═══════════════════════════════════════════════════════════

@api.route("/api/guilds/<guild_id>/embeds/send", methods=["POST"])
@require_owner
@rate_limit(max_calls=10, period=60)
def send_embed(guild_id):
    from bot import bot
    body = request.get_json(silent=True) or {}
    channel_id = body.get("channel_id")
    content = body.get("content") or None
    buttons = body.get("buttons") or []
    embed_data = body.get("embed")
    if not channel_id:
        return jsonify({"ok": False, "error": "channel_id required"}), 400
    guild = bot.get_guild(int(guild_id))
    if not guild:
        return jsonify({"ok": False, "error": "Guild not found"}), 404
    channel = guild.get_channel(int(channel_id))
    if not channel or not isinstance(channel, discord.TextChannel):
        return jsonify({"ok": False, "error": "Channel not found"}), 404

    async def _send():
        me = guild.me
        perms = channel.permissions_for(me)
        if not perms.send_messages:
            return {"error": "Bot cannot send messages in that channel"}
        if embed_data and not perms.embed_links:
            return {"error": "Bot cannot embed links in that channel"}

        embed = None
        if embed_data and isinstance(embed_data, dict):
            title = (embed_data.get("title") or "")[:256] or None
            description = (embed_data.get("description") or "")[:4096] or None
            try:
                color = int(embed_data.get("color") if embed_data.get("color") is not None else 0x5865F2)
            except (ValueError, TypeError):
                color = 0x5865F2
            embed = discord.Embed(title=title, description=description, color=color)
            author = embed_data.get("author")
            if author and isinstance(author, dict) and author.get("name"):
                embed.set_author(
                    name=str(author["name"])[:256],
                    icon_url=author.get("icon_url") or None,
                )
            if embed_data.get("thumbnail"):
                embed.set_thumbnail(url=embed_data["thumbnail"])
            if embed_data.get("image"):
                embed.set_image(url=embed_data["image"])
            footer = embed_data.get("footer")
            if footer and isinstance(footer, dict) and footer.get("text"):
                embed.set_footer(
                    text=str(footer["text"])[:2048],
                    icon_url=footer.get("icon_url") or None,
                )
            if embed_data.get("timestamp"):
                embed.timestamp = datetime.now(timezone.utc)
            fields = embed_data.get("fields") or []
            total_len = len(title or "") + len(description or "")
            for f in fields[:25]:
                if not isinstance(f, dict):
                    continue
                name = str(f.get("name") or "\u200b")[:256]
                value = str(f.get("value") or "\u200b")[:1024]
                if total_len + len(name) + len(value) > 6000:
                    break
                embed.add_field(name=name, value=value, inline=bool(f.get("inline")))
                total_len += len(name) + len(value)

        view = None
        valid_buttons = []
        for b in buttons[:5]:
            if not isinstance(b, dict):
                continue
            label = (b.get("label") or "")[:80]
            url = b.get("url") or ""
            if label and url.startswith(("http://", "https://")):
                valid_buttons.append(discord.ui.Button(label=label, url=url))
        if valid_buttons:
            view = discord.ui.View()
            for btn in valid_buttons:
                view.add_item(btn)

        try:
            await channel.send(content=content, embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException) as e:
            return {"error": str(e)}

        owner = await bot.fetch_user(int(OWNER_ID))
        await log_action(
            int(guild_id), "embed", int(OWNER_ID), str(owner),
            channel_id=channel.id,
            reason=(content or (embed_data or {}).get("title") or "")[:100],
            source="dashboard",
        )
        return {"ok": True}

    result = run_async(_send())
    if "error" in result:
        return jsonify({"ok": False, "error": result["error"]}), 400
    return jsonify({"ok": True, "data": result})
