"""
Telegram AutoForwarder - Free Community Edition (Ultra-Light)
- 100% Free Access Event
- Inline Dashboard
- Individual Route Toggles (/onroute, /offroute)
- Resilient Error Handling
- Heavy Media Scrapers & Tollbooths Removed
- Auto-detects dead sessions, safely cleans up, and alerts users via DM
"""

import asyncio
import logging
import os
import json
import re
import time
import html
from datetime import datetime
import psutil
from dotenv import load_dotenv
import aiosqlite
import io

from aiogram.types import (
    MessageOriginUser,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    CallbackQuery,
)
from aiogram import Bot, Dispatcher, Router, types, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.session.aiohttp import AiohttpSession
from typing import Callable, Dict, Any, Awaitable
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

import forwarder_core

try:
    import uvloop
    uvloop.install()
except ImportError:
    pass

# -------------------------------
# Config & Environment
# -------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH") or ""

# Load Admin & Bug Channel from environment variables
BUG_CHANNEL_ID = int(os.getenv("BUG_CHANNEL_ID") or 0)
SUPERUSERS = {
    int(x.strip()) for x in os.getenv("SUPERUSERS", "").split(",") if x.strip().isdigit()
}

CONFIG_FILE = "data/users.json"
SESSIONS_DIR = "data/sessions"
TEMP_DIR = "downloads" 

PAYMENTS_ENABLED = False  # Global toggle for free event

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class TelegramBugChannelHandler(logging.Handler):
    def emit(self, record):
        if record.levelno < logging.ERROR or not BUG_CHANNEL_ID:
            return
            
        log_entry = self.format(record)
        
        try:
            loop = asyncio.get_running_loop()
            
            # Safe wrapper to catch unavoidable network drops
            async def safe_send(coro):
                try:
                    await coro
                except Exception:
                    pass 

            if len(log_entry) > 4000:
                file_bytes = io.BytesIO(log_entry.encode("utf-8"))
                document = types.BufferedInputFile(
                    file_bytes.getvalue(),
                    filename=f"crash_log_{int(time.time())}.txt"
                )
                caption = "🚨 <b>Massive System Crash Caught</b>"
                loop.create_task(
                    safe_send(
                        bot.send_document(
                            BUG_CHANNEL_ID, document, caption=caption, parse_mode="HTML"
                        )
                    )
                )
            else:
                error_text = (
                    f"🚨 <b>Log Error Caught</b>\n"
                    f"<pre><code class='language-python'>{html.escape(log_entry)}</code></pre>"
                )
                loop.create_task(
                    safe_send(
                        bot.send_message(
                            BUG_CHANNEL_ID, error_text, parse_mode="HTML"
                        )
                    )
                )
                
        except Exception:
            pass 


log = logging.getLogger("manager_bot")


class SilentAntiSpamMiddleware(BaseMiddleware):
    def __init__(self, limit_seconds: float = 2.0):
        self.limit_seconds = limit_seconds
        self.user_cooldowns = {}

    async def __call__(
        self,
        handler: Callable[[types.Message, Dict[str, Any]], Awaitable[Any]],
        event: types.Message,
        data: Dict[str, Any]
    ) -> Any:
        uid = event.from_user.id
        now = time.time()
        last_time = self.user_cooldowns.get(uid, 0)

        if now - last_time < self.limit_seconds:
            self.user_cooldowns[uid] = now 
            if isinstance(event, types.CallbackQuery):
                try:
                    await event.answer("⚠️ Please slow down!", show_alert=True)
                except Exception:
                    pass
            return 

        self.user_cooldowns[uid] = now
        users_db = await load_users()
        if users_db.get(str(uid), {}).get("banned", False):
            if isinstance(event, types.CallbackQuery):
                try:
                    await event.answer("🚫 You have been banned.", show_alert=True)
                except Exception:
                    pass
            return 

        return await handler(event, data)


# Apply 60s timeout for stability
session = AiohttpSession(timeout=60)
bot = Bot(token=BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
telegram_handler = TelegramBugChannelHandler()
telegram_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s\n%(message)s"))
logging.getLogger().addHandler(telegram_handler)
router = Router()

router.message.middleware(SilentAntiSpamMiddleware(limit_seconds=1.0))
router.callback_query.middleware(SilentAntiSpamMiddleware(limit_seconds=1.0))

dp.include_router(router)

_save_lock = asyncio.Lock()
MAX_LOAD = 0.8  

forwarder_tasks = {}
bg_tasks = set()

# -------------------------------
# FSM States & Callback Data
# -------------------------------
class LogoutStates(StatesGroup):
    waiting_for_feedback = State()
    waiting_confirmation = State()


class LoginStates(StatesGroup):
    waiting_for_phone = State()


class MenuCB(CallbackData, prefix="menu"):
    action: str


class RouteCB(CallbackData, prefix="route"):
    action: str
    route_idx: int


class ConfigState(StatesGroup):
    waiting_for_filter_value = State()
    waiting_for_new_route_src = State()
    waiting_for_new_route_dest = State()
    waiting_for_route_name = State()
    waiting_for_contact = State()


class IdFinderState(StatesGroup):
    waiting_for_input = State()


class TestRouteState(StatesGroup):
    waiting_for_sample = State()

# -------------------------------
# Safe File I/O (SQLite Engine)
# -------------------------------
DB_FILE = "data/users.db"


def ensure_dirs():
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)


async def init_db():
    ensure_dirs()
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                uid TEXT PRIMARY KEY,
                data TEXT
            )
        """)
        await db.commit()


async def load_users():
    users = {}
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT uid, data FROM users") as cursor:
                async for row in cursor:
                    users[row[0]] = json.loads(row[1])
    except Exception as e:
        log.error(f"Error loading users from DB: {e}")
    return users


async def save_users(users):
    async with _save_lock:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.executemany(
                "INSERT OR REPLACE INTO users (uid, data) VALUES (?, ?)",
                [(uid, json.dumps(data)) for uid, data in users.items()]
            )
            await db.commit()

# -------------------------------
# Core Helpers
# -------------------------------
def get_client(uid: str) -> TelegramClient:
    session_path = os.path.join(SESSIONS_DIR, f"{uid}")
    return TelegramClient(session_path, API_ID, API_HASH)


async def is_user_linked(uid: str) -> bool:
    users = await load_users()
    return bool(users.get(uid, {}).get("linked", False))


async def system_load_ok() -> bool:
    return (psutil.cpu_percent(interval=None) / 100) < MAX_LOAD


def is_premium(uid: str, users: dict) -> bool:
    return True  # 100% Free Access for all users

# -------------------------------
# Engine Reloader & Recovery
# -------------------------------
async def handle_dead_session(uid: str):
    """Callback triggered when the core engine detects a dead Telegram session."""
    uid = str(uid)
    users = await load_users()
    if uid not in users:
        return

    # Check flag to prevent spamming in a loop
    if not users[uid].get("session_dead_notified", False):
        log.warning(f"Dead session detected for {uid}. Automating wipe and notifying user.")
        
        # 1. Safely stop engine and wipe broken session files
        forwarder_core._remove_handlers_for_user(uid)
        client = forwarder_core.clients_per_user.pop(uid, None)
        if client and client.is_connected():
            try:
                await client.disconnect()
            except Exception:
                pass
        
        old_task = forwarder_tasks.pop(uid, None)
        if old_task and not old_task.done():
            old_task.cancel()

        for ext in ("", ".session", ".session-journal", ".session.lock"):
            path = os.path.join(SESSIONS_DIR, uid) + ext
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

        # 2. Update database state
        users[uid]["linked"] = False
        users[uid]["enabled"] = False
        users[uid]["phone"] = ""
        users[uid]["session_dead_notified"] = True  # Anti-spam flag
        await save_users(users)

        # 3. Send automated alert with inline button
        kb = InlineKeyboardBuilder()
        kb.button(text="🔑 Secure Login", callback_data=MenuCB(action="start_login"))

        alert_text = (
            "⚠️ <b>Action Required: Telegram Session Expired</b>\n\n"
            "Your AutoForwarder connection has been dropped. This usually happens if you tapped 'Terminate Session' in your Telegram settings, or if Telegram revoked the connection for security.\n\n"
            "To protect your account, your bot engine has been paused and your old connection data has been safely cleared.\n\n"
            "👉 <b>Click the button below to securely log back in and resume forwarding:</b>"
        )
        try:
            await bot.send_message(int(uid), alert_text, reply_markup=kb.as_markup())
        except Exception as e:
            log.error(f"Could not send dead session alert to {uid}: {e}")


async def reload_forwarder_routes_for_user(uid=None):
    global forwarder_tasks
    users = await load_users()
    routes_dict = {}

    for u_id, u in users.items():
        if uid is not None and u_id != str(uid):
            continue
        if not u.get("enabled", False):
            continue

        has_prem = is_premium(u_id, users)

        for route in u.get("routes", []):
            if not route.get("is_active", True):
                continue

            src, dest = route["from"], route["to"]
            
            routes_dict.setdefault(str(src), []).append({
                "to": [dest] if isinstance(dest, str) else dest,
                "owner": u_id,
                "route_name": route.get("name", f"{src} ➡ {dest}"), 
                "keywords": route.get("keywords", []) if has_prem else [],
                "blacklist": route.get("blacklist", []) if has_prem else [],
                "pattern": route.get("pattern", "") if has_prem else "",
                "delay": route.get("delay", 0) if has_prem else 0,
                "cooldown": route.get("cooldown", 0) if has_prem else 0,
                "begin_text": route.get("begin_text", "") if has_prem else "",
                "end_text": route.get("end_text", "") if has_prem else "",
                "replacements": route.get("replacements", {}) if has_prem else {},
                "allowed_users": route.get("allowed_users", []) if has_prem else [],
                "ignore_text": route.get("ignore_text", False) if has_prem else False,
                "ignore_media": route.get("ignore_media", False) if has_prem else False,
                "native_forward": route.get("native_forward", False) if has_prem else False,
                "disable_preview": route.get("disable_preview", True) if has_prem else True,
                "auto_update": route.get("auto_update", False) if has_prem else False,
            })

    task_key = uid if uid else "all"
    old_task = forwarder_tasks.get(task_key)
    if old_task and not old_task.done():
        old_task.cancel()
    if not uid: 
        for t in forwarder_tasks.values():
            t.cancel()
        forwarder_tasks.clear()

    # Pass the dead session callback into the engine
    task = asyncio.create_task(
        forwarder_core.setup_routes_for_user(
            routes_dict, API_ID, API_HASH, target_uid=uid, on_auth_error=handle_dead_session
        )
    )
    forwarder_tasks[task_key] = task

# -------------------------------
# Interactive Usage & Help Engine
# -------------------------------
COMMAND_INFO = {
    "login": ("Links your Telegram account to the bot.", "<code>/login +&lt;country_code&gt;&lt;number&gt;</code>", "/login +19876543210"),
    "password": ("Submits your Two-Factor Authentication (2FA) password.", "<code>/password your_actual_password</code>", "/password MySecretPass123"),
    "addroute": ("Creates a new forwarding route.", "<code>/addroute &lt;from&gt; &lt;to&gt;</code>", "/addroute @source_channel @dest_channel"),
    "delroute": ("Deletes an existing route entirely.", "<code>/delroute &lt;id&gt;</code>", "/delroute 1"),
    "editroute": ("Changes the source or destination of an existing route.", "<code>/editroute &lt;id&gt; &lt;new_from&gt; &lt;new_to&gt;</code>", "/editroute 1 @new_src @new_dest"),
    "onroute": ("Resumes forwarding for a specific route.", "<code>/onroute &lt;id&gt;</code>", "/onroute 1"),
    "offroute": ("Pauses forwarding for a specific route.", "<code>/offroute &lt;id&gt;</code>", "/offroute 1"),
    "contact": ("Sends a message directly to the bot administrators.", "<code>/contact &lt;message&gt;</code>", "/contact I need help!"),
    "setkeywords": ("Sets a whitelist. Only messages containing these words will be forwarded.", "<code>/setkeywords &lt;id&gt; &lt;words&gt;</code>", "/setkeywords 1 bitcoin, crypto"),
    "setblacklist": ("Sets a blacklist. Messages containing these words will be ignored.", "<code>/setblacklist &lt;id&gt; &lt;words&gt;</code>", "/setblacklist 1 scam, vip"),
    "setpattern": ("Sets a RegEx pattern to filter messages.", "<code>/setpattern &lt;id&gt; &lt;regex&gt;</code>", "/setpattern 1 https?://"),
    "setdelay": ("Adds a delay (in seconds) before forwarding.", "<code>/setdelay &lt;id&gt; &lt;seconds&gt;</code>", "/setdelay 1 60"),
    "setcooldown": ("Sets a cooldown (in seconds) to ignore duplicate spam.", "<code>/setcooldown &lt;id&gt; &lt;seconds&gt;</code>", "/setcooldown 1 300"),
    "setbegin": ("Adds custom text to the BEGINNING of forwarded messages.", "<code>/setbegin &lt;id&gt; &lt;text&gt;</code>", "/setbegin 1 🚀 New Alert:\\n"),
    "setend": ("Adds custom text to the END of forwarded messages.", "<code>/setend &lt;id&gt; &lt;text&gt;</code>", "/setend 1 \\n\\nJoin my channel!"),
    "autoupdate": ("Toggles automatic syncing of edited and deleted messages.", "<code>/autoupdate &lt;id&gt;</code>", "/autoupdate 1"),
    "editroutename": ("Changes the custom display name of a route.", "<code>/editroutename &lt;id&gt; &lt;new name&gt;</code>", "/editroutename 1 VIP Crypto"),
    "filter": ("Finds a specific word/link in a message and replaces it.", "<code>/filter &lt;route_id&gt; &lt;FindWord&gt; | &lt;ReplaceWord&gt;</code>", "/filter 1 @OriginalVIP | @MyChannel"),
    "whitelistuser": ("Only forwards messages sent by specific users.", "<code>/whitelistuser &lt;route_id&gt; &lt;users&gt;</code>", "/whitelistuser 1 @admin1, @admin2"),
    "ignoretext": ("Strips all text/captions and forwards ONLY the media.", "<code>/ignoretext &lt;route_id&gt;</code>", "/ignoretext 1"),
    "ignoremedia": ("Strips all media and forwards ONLY the text.", "<code>/ignoremedia &lt;route_id&gt;</code>", "/ignoremedia 1"),
    "nativeforward": ("Forwards using Telegram's native 'Forwarded from...' header.", "<code>/nativeforward &lt;route_id&gt;</code>", "/nativeforward 1"),
    "linkpreview": ("Toggles the large thumbnail preview for URLs.", "<code>/linkpreview &lt;route_id&gt;</code>", "/linkpreview 1"),
}


async def send_usage(msg: types.Message, cmd_name: str):
    cmd_name = cmd_name.lower()
    if cmd_name.startswith("clear"):
        filter_name = cmd_name.replace("clear", "")
        friendly_names = {
            "pattern": "RegEx pattern",
            "begin": "prefix (begin text)",
            "end": "suffix (end text)",
            "whitelistuser": "whitelisted users",
        }
        display_name = friendly_names.get(filter_name, filter_name)
        desc = f"Removes the {display_name} filter from a specific route."
        usage = f"<code>/{cmd_name} &lt;id&gt;</code>"
        example = f"/{cmd_name} 1"
    else:
        info = COMMAND_INFO.get(
            cmd_name,
            ("Configures a setting for your route.", f"<code>/{cmd_name} &lt;id&gt; &lt;value&gt;</code>", f"/{cmd_name} 1 value")
        )
        desc, usage, example = info

    await msg.answer(f"ℹ️ <b>What this does:</b>\n{desc}")
    await asyncio.sleep(0.1) 
    await msg.answer(
        f"🛠 <b>Usage:</b>\n{usage.replace('<id>', '<route_id>')}\n\n"
        f"💡 <b>Example:</b>\n<code>{example}</code>\n\n"
        f"🔍 <i>Tip: Your &lt;route_id&gt; is the number assigned to your route. Type /routes to see a numbered list of your active routes!</i>"
    )

# -------------------------------
# Admin Commands
# -------------------------------
@router.message(Command("testbug"))
async def cmd_testbug(msg: types.Message):
    if msg.from_user.id not in SUPERUSERS:
        return
    await msg.answer("🧪 <b>Test Initiated.</b>\nCheck your Bug Channel!")
    try:
        1 / 0 
    except Exception:
        log.error("TEST BUG 1: Standard error caught successfully.", exc_info=True)
    try:
        raise ValueError(
            "Traceback (most recent call last):\n"
            + ("  File 'fake_file.py', line 99\n" * 200)
            + "Massive test error."
        )
    except Exception as e:
        log.error(f"TEST BUG 2: Massive error caught successfully.\n{str(e)}")


@router.message(Command("broadcast"))
async def cmd_broadcast(msg: types.Message):
    if msg.from_user.id not in SUPERUSERS:
        return
    
    args = msg.text.split(maxsplit=1)
    if len(args) < 2: 
        return await msg.answer(
            "❌ <b>Error:</b> Please provide a message to broadcast.\n\n"
            "🛠 <b>Usage:</b>\n<code>/broadcast Your message here</code>"
        )
        
    broadcast_msg = html.escape(args[1])
    users = await load_users()
    
    await msg.answer(f"📢 Starting broadcast to {len(users)} users...")
    success = 0
    for u_id in users.keys():
        try:
            await bot.send_message(int(u_id), broadcast_msg)
            success += 1
            await asyncio.sleep(0.1) 
        except Exception:
            pass
            
    await msg.answer(f"✅ Broadcast complete! Reached {success} users.")


@router.message(Command("admin", "adminhelp"))
async def cmd_admin_help(msg: types.Message):
    if msg.from_user.id not in SUPERUSERS:
        return
    text = (
        "🛠 <b>Superuser Commands Cheat Sheet</b>\n\n"
        "<b>System Monitoring:</b>\n"
        "• <code>/testbug</code> - Test sending bug report\n"
        "• <code>/sysload</code> - View live CPU, RAM, and Disk usage\n\n"
        "<b>Account Management:</b>\n"
        "• <code>/broadcast &lt;message&gt;</code> - Send announcements to all users\n"
        "• <code>/showusers</code> - List all users and route counts\n"
        "• <code>/ban &lt;id&gt;</code> - Ban User\n"
        "• <code>/unban &lt;id&gt;</code> - Unban User\n"
        "• <code>/wipeuser &lt;id&gt;</code> - DELETE User\n"
    )
    await msg.answer(text)


@router.message(Command("showusers"))
async def cmd_showusers(msg: types.Message):
    if msg.from_user.id not in SUPERUSERS:
        return
    users = await load_users()
    text = "👥 <b>Connected Users:</b>\n\n"
    for u_id, u in users.items():
        phone = u.get("phone", "No Phone")
        routes = len(u.get("routes", []))
        text += f"ID: <code>{u_id}</code> | {phone} | Routes: {routes}\n"
    if len(text) > 4000:
        text = text[:4000] + "...\n(Truncated)"
    await msg.answer(text)


@router.message(Command("sysload"))
async def cmd_sysload(msg: types.Message):
    if msg.from_user.id not in SUPERUSERS:
        return
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory().percent
    disk = psutil.disk_usage('/').percent
    await msg.answer(f"🖥 <b>System Load:</b>\nCPU: {cpu}%\nRAM: {mem}%\nDisk: {disk}%")


@router.message(Command("wipeuser"))
async def cmd_wipeuser(msg: types.Message):
    if msg.from_user.id not in SUPERUSERS:
        return
    args = msg.text.split()
    if len(args) != 2:
        return await msg.answer("🛠 <b>Usage:</b>\n<code>/wipeuser &lt;user_id&gt;</code>")
    target_uid = args[1]
    
    forwarder_core._remove_handlers_for_user(target_uid)
    client = forwarder_core.clients_per_user.pop(target_uid, None)
    if client and client.is_connected():
        try:
            await client.disconnect()
        except Exception:
            pass
        
    old_task = forwarder_tasks.pop(target_uid, None)
    if old_task and not old_task.done():
        old_task.cancel()

    for ext in ("", ".session", ".session-journal", ".session.lock"):
        path = os.path.join(SESSIONS_DIR, target_uid) + ext
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

    async with _save_lock:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("DELETE FROM users WHERE uid = ?", (target_uid,))
            await db.commit()
            
    await msg.answer(f"🗑️ ✅ All details for user <code>{target_uid}</code> have been permanently wiped.")


@router.message(Command("ban"))
async def cmd_ban(msg: types.Message):
    if msg.from_user.id not in SUPERUSERS:
        return
    args = msg.text.split()
    if len(args) != 2:
        return await msg.answer("🛠 <b>Usage:</b>\n<code>/ban &lt;user_id&gt;</code>")
    
    target_uid = args[1]
    users = await load_users()
    if target_uid not in users:
        return await msg.answer("❌ User not found.")
    
    users[target_uid]["banned"] = True
    users[target_uid]["enabled"] = False
    await save_users(users)
    await reload_forwarder_routes_for_user(target_uid)
    await msg.answer(f"🚫 ✅ User {target_uid} has been banned.")


@router.message(Command("unban"))
async def cmd_unban(msg: types.Message):
    if msg.from_user.id not in SUPERUSERS:
        return
    args = msg.text.split()
    if len(args) != 2:
        return await msg.answer("🛠 <b>Usage:</b>\n<code>/unban &lt;user_id&gt;</code>")
    
    target_uid = args[1]
    users = await load_users()
    if target_uid not in users:
        return await msg.answer("❌ User not found.")
    
    users[target_uid]["banned"] = False
    await save_users(users)
    await msg.answer(f"✅ User {target_uid} has been unbanned.")

# -------------------------------
# Fully Interactive Dashboard UI
# -------------------------------
async def finalize_new_route(uid: str, src: str, dest: str, route_name: str, msg_obj: types.Message, state: FSMContext):
    users = await load_users()
    if not route_name:
        await msg_obj.edit_text("⏳ <i>Fetching channel names from Telegram... Please wait.</i>")
        client = forwarder_core.clients_per_user.get(uid)
        is_temp_client = False
        
        if not client or not client.is_connected():
            client = await forwarder_core.start_client_background(uid, API_ID, API_HASH)
            is_temp_client = False 
            
        try:
            async def get_title(cid):
                try:
                    ent = await client.get_entity(int(cid) if str(cid).lstrip("-").isdigit() else cid)
                    return getattr(ent, 'title', getattr(ent, 'username', str(cid)))
                except Exception:
                    return str(cid)
                
            src_title = await get_title(src)
            dest_title = await get_title(dest)
            route_name = f"{src_title} ➡ {dest_title}"
        except Exception:
            route_name = f"{src} ➡ {dest}" 
        finally:
            if is_temp_client and client.is_connected(): 
                await client.disconnect()

    users.setdefault(uid, {}).setdefault("routes", []).append({
        "name": route_name, "from": src, "to": dest, "is_active": True
    })
    await save_users(users)
    await reload_forwarder_routes_for_user(uid)
    await state.clear()
    await msg_obj.answer(
        f"✅ Route successfully added!\n"
        f"<b>{route_name}</b>\n(<code>{src}</code> → <code>{dest}</code>)\n\n"
        f"Use /routes to configure it."
    )


async def get_dashboard_text_and_keyboard(uid: str, view: str = "main"):
    users = await load_users()
    user = users.get(uid, {})
    routes_active = user.get("enabled", False)
    routes_count = len(user.get("routes", []))

    header_stats = (
        "🎛 <b>Control Center</b>\n\n"
        "<b>Tier:</b> 👑 PREMIUM (Free Access Event)\n"
        "-----------------------------------\n"
    )

    kb = InlineKeyboardBuilder()

    if view == "main":
        text = header_stats + "🎯 <b>What would you like to do today?</b>\n<i>Select an operation below:</i>"
        kb.button(text="⏩ Auto-Forward Future Messages", callback_data=MenuCB(action="view_future"))
        kb.button(text="📨 Contact Support", callback_data=MenuCB(action="contact_support"))
        kb.button(text="🔄 Refresh", callback_data=MenuCB(action="main_menu"))
        kb.adjust(1, 1, 1)

    elif view == "future":
        text = (
            header_stats +
            "⏩ <b>Auto-Forwarding (Future Messages)</b>\n\n"
            f"<b>Bot Engine:</b> {'🟢 ON' if routes_active else '🔴 OFF'}\n"
            f"<b>Total Routes:</b> {routes_count}\n\n"
            "⚠️ <i>Disclaimer: This engine only listens for NEW messages sent after a route is created and turned ON.</i>"
        )
        if routes_active:
            kb.button(text="⏸ Turn Engine OFF", callback_data=MenuCB(action="toggle_off"))
        else:
            kb.button(text="▶️ Turn Engine ON", callback_data=MenuCB(action="toggle_on"))
            
        kb.button(text="🛣 Manage Saved Routes", callback_data=MenuCB(action="view_routes"))
        kb.button(text="➕ Add New Route", callback_data=MenuCB(action="add_route_start"))
        kb.button(text="🔍 ID Finder Tool", callback_data=MenuCB(action="open_id_finder"))
        kb.button(text="🔙 Back to Main Menu", callback_data=MenuCB(action="main_menu"))
        kb.adjust(1, 1, 1, 1, 1)

    return text, kb.as_markup()


@router.message(Command("dashboard"))
async def cmd_dashboard(msg: types.Message, state: FSMContext):
    await state.clear()
    uid = str(msg.from_user.id)
    if not await is_user_linked(uid):
        return await msg.answer("❌ You must log in first. Use <code>/login +&lt;number&gt;</code>")
    text, markup = await get_dashboard_text_and_keyboard(uid)
    await msg.answer(text, reply_markup=markup)


@router.callback_query(MenuCB.filter(F.action == "cancel_fsm"))
async def cb_cancel_fsm(query: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await query.message.edit_text("🛑 <b>Action Cancelled.</b>\nYou have safely exited the setup menu.")
    except Exception:
        pass 
    await query.answer()


@router.callback_query(MenuCB.filter(F.action.in_(["main_menu", "view_future", "refresh"])))
async def cb_dashboard_nav(query: CallbackQuery, callback_data: MenuCB, state: FSMContext):
    await state.clear()
    uid = str(query.from_user.id)
    view = "future" if callback_data.action == "view_future" else "main"
    text, markup = await get_dashboard_text_and_keyboard(uid, view=view)
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass
    await query.answer()


@router.callback_query(MenuCB.filter(F.action == "open_id_finder"))
async def cb_open_id_finder(query: types.CallbackQuery, state: FSMContext):
    uid = str(query.from_user.id)
    if not await is_user_linked(uid):
        return await query.answer("❌ You must link your account first to use the ID Finder.", show_alert=True)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Cancel", callback_data=MenuCB(action="main_menu"))
    text = (
        "🔍 <b>ID Finder</b>\n\n"
        "I can find IDs of users/channels/groups that you can use to setup forwarding routes.\n\n"
        "<b>What you can send me:</b>\n"
        "• A public <code>@username</code>\n"
        "• A private channel link (<code>https://t.me/c/...</code>)\n"
        "• Or, you can forward any message directly to me.\n\n"
        "<i>Type the username or link below:</i>"
    )
    await state.set_state(IdFinderState.waiting_for_input)
    await query.message.edit_text(text, reply_markup=kb.as_markup())
    await query.answer()


@router.callback_query(MenuCB.filter(F.action.in_(["toggle_on", "toggle_off"])))
async def cb_toggle_state(query: CallbackQuery, callback_data: MenuCB):
    uid = str(query.from_user.id)
    users = await load_users()
        
    if callback_data.action == "toggle_on":
        users[uid]["enabled"] = True
        await query.answer("▶️ Engine Turned ON")
    else:
        users[uid]["enabled"] = False
        await query.answer("⏸ Engine Turned OFF")
        
    await save_users(users)
    await reload_forwarder_routes_for_user(uid)
    
    text, markup = await get_dashboard_text_and_keyboard(uid, view="future")
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass


@router.callback_query(MenuCB.filter(F.action == "view_routes"))
async def cb_view_routes(query: CallbackQuery):
    uid = str(query.from_user.id)
    users = await load_users()
    routes = users.get(uid, {}).get("routes", [])
    
    if not routes:
        return await query.answer("No routes. Click 'Add New Route'.", show_alert=True)
        
    kb = InlineKeyboardBuilder()
    for idx, route in enumerate(routes):
        status_icon = "🟢" if route.get("is_active", True) else "🔴"
        name = route.get("name", f"{route['from']} ➡ {route['to']}")
        btn_text = f"{status_icon} {name}"
        kb.button(text=btn_text, callback_data=RouteCB(action="manage", route_idx=idx))

    kb.button(text="🔙 Back to Dashboard", callback_data=MenuCB(action="refresh"))
    kb.adjust(1) 
    try:
        await query.message.edit_text("🛣 <b>Your Routes</b>\nSelect a route to configure:", reply_markup=kb.as_markup())
    except Exception:
        pass
    await query.answer()


@router.callback_query(RouteCB.filter(F.action == "manage"))
async def cb_route_info(query: CallbackQuery, callback_data: RouteCB):
    uid = str(query.from_user.id)
    users = await load_users()
    idx = callback_data.route_idx
    routes = users.get(uid, {}).get("routes", [])
    
    if idx >= len(routes):
        return await query.answer("Route not found.", show_alert=True)
        
    r = routes[idx]
    is_active = r.get("is_active", True)
    status_str = "🟢 ON" if is_active else "🔴 PAUSED"
    route_name = r.get('name', f"{r.get('from')} ➡ {r.get('to')}")
    text = (
        f"🛣 <b>Route #{idx+1}: {route_name}</b>\n\n"
        f"<b>Status:</b> {status_str}\n"
        f"<b>From:</b> {r.get('from')}\n"
        f"<b>To:</b> {r.get('to')}\n\n"
        f"<b>💎 Premium Filters:</b>\n"
        f"• <b>Keywords:</b> {', '.join(r.get('keywords', [])) or 'None'}\n"
        f"• <b>Blacklist:</b> {', '.join(r.get('blacklist', [])) or 'None'}\n"
        f"• <b>Allowed Users:</b> {', '.join(r.get('allowed_users', [])) or 'None'}\n"
        f"• <b>Pattern:</b> {r.get('pattern', '') or 'None'}\n"
        f"• <b>Delay:</b> {r.get('delay', 0)}s\n"
        f"• <b>Cooldown:</b> {r.get('cooldown', 0)}s\n"
        f"• <b>Prefix (Begin):</b> {repr(r.get('begin_text', ''))}\n"
        f"• <b>Suffix (End):</b> {repr(r.get('end_text', ''))}\n"
        f"• <b>Auto-Update:</b> {'✅ On' if r.get('auto_update', False) else '❌ Off'}\n"
        f"• <b>Find & Replace:</b> {len(r.get('replacements', {}))} rules active\n"
        f"• <b>Link Previews:</b> {'❌ Disabled' if r.get('disable_preview', True) else '✅ Enabled'}\n"
        f"• <b>Ignore Text/Media:</b> {'Yes' if r.get('ignore_text') else 'No'} / {'Yes' if r.get('ignore_media') else 'No'}\n"
        f"• <b>Native Forward:</b> {'Yes' if r.get('native_forward') else 'No'}\n"
    )
    
    kb = InlineKeyboardBuilder()
    kb.button(text="🧪 Test / Dry Run", callback_data=RouteCB(action="test_route", route_idx=idx))
    kb.button(text="⏸ Pause Route" if is_active else "▶️ Resume Route", callback_data=RouteCB(action="toggle_active", route_idx=idx))
    kb.button(text="🗑 Delete Route", callback_data=RouteCB(action="delete", route_idx=idx))
    kb.button(text="✏️ Edit Name", callback_data=RouteCB(action="edit_name", route_idx=idx))
    kb.button(text="✏️ Edit Source", callback_data=RouteCB(action="edit_source", route_idx=idx))
    kb.button(text="✏️ Edit Dest", callback_data=RouteCB(action="edit_dest", route_idx=idx))
    kb.button(text="⏱ Delay", callback_data=RouteCB(action="edit_delay", route_idx=idx))
    kb.button(text="🛡 Cooldown", callback_data=RouteCB(action="edit_cooldown", route_idx=idx))
    kb.button(text="✅ Keywords", callback_data=RouteCB(action="edit_keywords", route_idx=idx))
    kb.button(text="🚫 Blacklist", callback_data=RouteCB(action="edit_blacklist", route_idx=idx))
    kb.button(text="📝 Prefix", callback_data=RouteCB(action="edit_begin_text", route_idx=idx))
    kb.button(text="📝 Suffix", callback_data=RouteCB(action="edit_end_text", route_idx=idx))
    kb.button(text="🔁 Auto-Update", callback_data=RouteCB(action="toggle_update", route_idx=idx))
    kb.button(text="👁️ Link Previews", callback_data=RouteCB(action="toggle_preview", route_idx=idx))
    kb.button(text="👤 Whitelist Users", callback_data=RouteCB(action="edit_whitelistuser", route_idx=idx))
    kb.button(text="✉️ Native Fwd", callback_data=RouteCB(action="toggle_native", route_idx=idx))
    kb.button(text="🔕 Drop Text", callback_data=RouteCB(action="toggle_ignoretext", route_idx=idx))
    kb.button(text="🖼️ Drop Media", callback_data=RouteCB(action="toggle_ignoremedia", route_idx=idx))
    kb.button(text="🔍 Find & Replace", callback_data=RouteCB(action="edit_replacements", route_idx=idx))
    kb.button(text="🧹 Clear Filters", callback_data=RouteCB(action="clear_all", route_idx=idx))
    kb.button(text="🔙 Back to Routes", callback_data=MenuCB(action="view_routes"))
    kb.adjust(1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1) 

    try:
        await query.message.edit_text(text, reply_markup=kb.as_markup())
    except Exception:
        pass
    await query.answer()


@router.callback_query(MenuCB.filter(F.action == "contact_support"))
async def cb_contact_support(query: CallbackQuery, state: FSMContext):
    await state.set_state(ConfigState.waiting_for_contact)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Cancel", callback_data=MenuCB(action="refresh"))
    
    await query.message.edit_text(
        "📨 <b>Contact Support</b>\n\n"
        "Found a bug or need help with a route? Please type your message below and I will forward it directly to the developer:", 
        reply_markup=kb.as_markup()
    )
    await query.answer()


@router.message(ConfigState.waiting_for_contact)
async def fsm_contact_msg(msg: types.Message, state: FSMContext):
    uid = str(msg.from_user.id)
    report = (
        f"📨 <b>New Bug Report / Contact</b>\n\n"
        f"👤 <b>From:</b> @{msg.from_user.username or msg.from_user.full_name}\n"
        f"🆔 <b>User ID:</b> <code>{uid}</code>\n\n"
        f"💬 <b>Message:</b>\n{html.escape(msg.text)}"
    )
    try:
        if BUG_CHANNEL_ID:
            await bot.send_message(BUG_CHANNEL_ID, report, parse_mode="HTML")
            await msg.answer("✅ Your message has been sent to the admin. Use /dashboard to return to the menu.")
        else:
            await msg.answer("ℹ️ Bug reporting channel is not configured on this instance.")
    except Exception: 
        await msg.answer("❌ Failed to send message. Please try again later.")
    finally:
        await state.clear()


@router.callback_query(
    RouteCB.filter(
        F.action.in_([
            "toggle_active",
            "toggle_update",
            "delete",
            "clear_all",
            "toggle_preview",
            "toggle_native",
            "toggle_ignoretext",
            "toggle_ignoremedia"
        ])
    )
)
async def cb_instant_actions(query: CallbackQuery, callback_data: RouteCB):
    uid = str(query.from_user.id)
    users = await load_users()
    idx = callback_data.route_idx
    routes = users.get(uid, {}).get("routes", [])
    
    if idx >= len(routes):
        return await query.answer("Route not found.", show_alert=True)
    
    action = callback_data.action

    if action == "delete":
        routes.pop(idx)
        await query.answer("🗑 Route Deleted!")
        users[uid]["routes"] = routes
        await save_users(users)
        await reload_forwarder_routes_for_user(uid)

        if len(routes) == 0:
            text, markup = await get_dashboard_text_and_keyboard(uid, view="future")
            try:
                await query.message.edit_text(text, reply_markup=markup)
            except Exception:
                pass
            return 
        else:
            return await cb_view_routes(query) 

    elif action == "toggle_active":
        routes[idx]["is_active"] = not routes[idx].get("is_active", True)
        await query.answer("Route state changed.")
        
    elif action == "toggle_update":
        routes[idx]["auto_update"] = not routes[idx].get("auto_update", False)
        await query.answer("Auto-Update toggled.")

    elif action in ["toggle_preview", "toggle_native", "toggle_ignoretext", "toggle_ignoremedia"]:
        key_map = {
            "toggle_preview": "disable_preview",
            "toggle_native": "native_forward",
            "toggle_ignoretext": "ignore_text",
            "toggle_ignoremedia": "ignore_media"
        }
        setting_key = key_map[action]
        default_val = True if setting_key == "disable_preview" else False
        routes[idx][setting_key] = not routes[idx].get(setting_key, default_val)
        await query.answer("Setting updated!")

    elif action == "clear_all":
        for key in ["keywords", "blacklist", "pattern", "begin_text", "end_text", "allowed_users"]:
            routes[idx][key] = [] if key in ["keywords", "blacklist", "allowed_users"] else ""
        routes[idx]["delay"] = 0
        routes[idx]["cooldown"] = 0
        routes[idx]["replacements"] = {}
        
        # Reset booleans to factory defaults
        routes[idx]["ignore_text"] = False
        routes[idx]["ignore_media"] = False
        routes[idx]["native_forward"] = False
        routes[idx]["disable_preview"] = True
        routes[idx]["auto_update"] = False
        
        await query.answer("All filters cleared and reset to defaults.")

    users[uid]["routes"] = routes
    await save_users(users)
    await reload_forwarder_routes_for_user(uid)
    await cb_route_info(query, callback_data)


@router.callback_query(RouteCB.filter(F.action.startswith("edit_")))
async def cb_edit_filter(query: CallbackQuery, callback_data: RouteCB, state: FSMContext):
    uid = str(query.from_user.id)
    users = await load_users()
    idx = callback_data.route_idx
    routes = users.get(uid, {}).get("routes", [])
    
    if idx >= len(routes):
        return await query.answer("Route not found.", show_alert=True)
        
    r = routes[idx]

    action_map = {
        "edit_name": ("Route Display Name", "Give this route a friendly name so you can easily identify it in your dashboard.", "VIP Crypto Signals", "name"),
        "edit_source": ("Source Channel", "Enter the channel ID or @username you want to forward FROM.", "@MySourceChannel", "from"),
        "edit_dest": ("Destination Channel", "Enter the channel ID or @username you want to forward TO.", "-100123456789", "to"),
        "edit_delay": ("Delay (Seconds)", "Delays the message from being forwarded immediately.", "60", "delay"),
        "edit_cooldown": ("Cooldown (Seconds)", "Ignores any identical messages sent within this time frame.", "300", "cooldown"),
        "edit_keywords": ("Whitelist Keywords", "ONLY messages containing these specific words will be forwarded. Separate with commas.", "bitcoin, eth, buy", "keywords"),
        "edit_blacklist": ("Blacklist Keywords", "Messages containing these specific words will be IGNORED. Separate with commas.", "scam, vip, join", "blacklist"),
        "edit_whitelistuser": ("Whitelist Users", "Only forwards messages sent by specific users (ID or @username).", "@admin1, @admin2", "allowed_users"),
        "edit_begin_text": ("Prefix (Begin Text)", "This text will be added to the very TOP of every forwarded message.", "🚀 New Alert:\\n", "begin_text"),
        "edit_end_text": ("Suffix (End Text)", "This text will be added to the very BOTTOM of every forwarded message.", "\\n\\nJoin my channel!", "end_text"),
        "edit_replacements": ("Find & Replace", "Finds a specific word or link in the original message and replaces it. Format: <b>FindWord | ReplaceWord</b>", "@OriginalCreator | @MyChannel", "replacements")
    }
    
    if callback_data.action not in action_map: 
        return await query.answer("Unknown action.")
        
    title, desc, example, prop_key = action_map[callback_data.action]
    
    curr_val = r.get(prop_key)
    if prop_key in ["keywords", "blacklist", "allowed_users"]:
        curr_str = "<code>" + html.escape(", ".join(curr_val)) + "</code>" if curr_val else "<i>None</i>"
    elif prop_key == "replacements":
        if curr_val:
            curr_str = "\n".join([f"• <code>{html.escape(k)}</code> ➡ <code>{html.escape(v)}</code>" for k, v in curr_val.items()])
        else:
            curr_str = "<i>None</i>"
    else:
        curr_str = f"<code>{html.escape(str(curr_val))}</code>" if curr_val not in [None, "", 0] else "<i>None</i>"
    
    await state.set_state(ConfigState.waiting_for_filter_value)
    await state.update_data(route_idx=idx, action=callback_data.action)
    
    kb = InlineKeyboardBuilder()
    if prop_key not in ["name", "from", "to"]:
        kb.button(text="🧹 Clear Filter", callback_data=MenuCB(action="fsm_clear_filter"))
        
    kb.button(text="❌ Cancel", callback_data=RouteCB(action="manage", route_idx=idx))
    kb.adjust(1)
    
    try:
        await query.message.edit_text(
            f"✏️ <b>Editing Route #{idx + 1}</b>\n"
            f"👉 <b>{title}</b>\n\n"
            f"ℹ️ <b>What this does:</b>\n{desc}\n\n"
            f"💡 <b>Example format:</b>\n<code>{example}</code>\n\n"
            f"🏷 <b>Current Configuration:</b>\n{curr_str}\n\n"
            f"<i>Please type your new setting in the chat below:</i>",
            reply_markup=kb.as_markup()
        )
    except Exception:
        pass
    await query.answer()


@router.callback_query(MenuCB.filter(F.action == "fsm_clear_filter"))
async def cb_fsm_clear_filter(query: CallbackQuery, state: FSMContext):
    uid = str(query.from_user.id)
    data = await state.get_data()
    
    if not data or 'route_idx' not in data or 'action' not in data:
        await state.clear()
        return await query.answer("Session expired.", show_alert=True)
        
    idx = data['route_idx']
    action = data['action']
    
    users = await load_users()
    routes = users.get(uid, {}).get("routes", [])
    
    if idx >= len(routes):
        await state.clear()
        return await query.answer("Route not found.", show_alert=True)
        
    if action in ["edit_keywords", "edit_blacklist"]:
        routes[idx][action.replace("edit_", "")] = []
    elif action == "edit_whitelistuser":
        routes[idx]["allowed_users"] = []
    elif action in ["edit_delay", "edit_cooldown"]:
        routes[idx][action.replace("edit_", "")] = 0
    elif action in ["edit_begin_text", "edit_end_text"]:
        routes[idx][action.replace("edit_", "")] = ""
    elif action == "edit_replacements":
        routes[idx]["replacements"] = {}
        
    users[uid]["routes"] = routes
    await save_users(users)
    await reload_forwarder_routes_for_user(uid)
    await state.clear()
    
    await query.answer("✅ Filter cleared successfully!")
    
    mock_cb = RouteCB(action="manage", route_idx=idx)
    await cb_route_info(query, mock_cb)


@router.message(ConfigState.waiting_for_filter_value)
async def fsm_catch_filter_value(msg: types.Message, state: FSMContext):
    uid = str(msg.from_user.id)
    data = await state.get_data()
    idx = data['route_idx']
    action = data['action']
    val = msg.text
    if not val:
        return await msg.answer("❌ Please send text or a valid link.")

    users = await load_users()
    routes = users.get(uid, {}).get("routes", [])
    if idx >= len(routes): 
        await state.clear()
        return await msg.answer("❌ Error: Route not found.")

    try:
        if action == "edit_name":
            routes[idx]["name"] = val
        elif action in ["edit_source", "edit_dest"]:
            parsed_val = parse_smart_input(val)
            if action == "edit_source":
                routes[idx]["from"] = parsed_val
            if action == "edit_dest":
                routes[idx]["to"] = parsed_val
        elif action in ["edit_delay", "edit_cooldown"]:
            routes[idx][action.replace("edit_", "")] = int(val)
        elif action in ["edit_keywords", "edit_blacklist"]:
            routes[idx][action.replace("edit_", "")] = [k.strip() for k in val.split(",")]
        elif action == "edit_whitelistuser":
            routes[idx]["allowed_users"] = [u.strip().lower() for u in val.split(",")]
        elif action in ["edit_begin_text", "edit_end_text"]:
            routes[idx][action.replace("edit_", "")] = val.replace("\\n", "\n")
        elif action == "edit_replacements":
            if "|" not in val:
                raise ValueError
            find_txt, replace_txt = [x.strip() for x in val.split("|", 1)]
            reps = routes[idx].get("replacements", {})
            reps[find_txt] = replace_txt
            routes[idx]["replacements"] = reps
            
        users[uid]["routes"] = routes
        await save_users(users)
        await reload_forwarder_routes_for_user(uid)

        await msg.answer(f"✅ Changes saved! Use /routes to view.")
    except ValueError:
        await msg.answer("❌ Invalid input format for this setting. Please refer to the guide and try again.")
    finally:
        await state.clear()


def parse_smart_input(text: str) -> str:
    t = text.strip()
    if "joinchat" in t or "t.me/+" in t: 
        return t
    
    # 1. Match private topic link format: t.me/c/1234567890/42/999
    m_topic = re.search(r't\.me/c/(\d+)/(\d+)/(\d+)', t)
    if m_topic:
        return f"-100{m_topic.group(1)}:{m_topic.group(2)}"
        
    # 2. Match private topic thread header: t.me/c/1234567890/42
    m_topic_header = re.search(r't\.me/c/(\d+)/(\d+)', t)
    if m_topic_header:
        return f"-100{m_topic_header.group(1)}:{m_topic_header.group(2)}"

    # 3. Match standard private message link: t.me/c/1234567890/999
    m = re.search(r't\.me/(?:c/)?(\d+)', t)
    if m: 
        return f"-100{m.group(1)}"
        
    # 4. Public username with topic: @group:42 or t.me/publicgroup/42
    m_pub_topic = re.search(r't\.me/([^/]+)/(\d+)', t)
    if m_pub_topic:
        return f"@{m_pub_topic.group(1)}:{m_pub_topic.group(2)}"

    if "t.me/" in t: 
        return "@" + t.split("t.me/")[-1].split("/")[0]
        
    if t.startswith("@") or t.lstrip("-").isdigit() or ":" in t: 
        return t
        
    return "@" + t


def simulate_route_pipeline(route: dict, sample_text: str, sender_uname: str = "DemoUser", sender_id: str = "12345678"):
    """Runs a simulated message through all filters of a route and returns a detailed report."""
    results = []
    text = sample_text.strip()
    is_blocked = False
    block_reason = ""

    # 1. Sender Check
    allowed_users = route.get("allowed_users", [])
    if allowed_users:
        if sender_uname.lower() not in allowed_users and sender_id not in allowed_users:
            results.append("👤 <b>Sender Whitelist:</b> ❌ <b>BLOCKED</b> (Sender not in whitelist)")
            is_blocked = True
            block_reason = "Sender Whitelist Violation"
        else:
            results.append("👤 <b>Sender Whitelist:</b> ✅ <b>PASSED</b>")
    else:
        results.append("👤 <b>Sender Whitelist:</b> ⚪ <i>Inactive (Allowed for everyone)</i>")

    # 2. Text vs Media
    if route.get("ignore_text", False):
        results.append("🔕 <b>Ignore Text:</b> ⚠️ <b>ACTIVE</b> (Text will be stripped)")
        text = ""
    else:
        results.append("🔕 <b>Ignore Text:</b> ⚪ <i>Inactive (Text retained)</i>")

    # 3. Keywords Whitelist
    keywords = route.get("keywords", [])
    if keywords and not is_blocked:
        matched_kw = [kw for kw in keywords if kw.lower() in text.lower()]
        if not matched_kw:
            results.append("✅ <b>Keywords Filter:</b> ❌ <b>BLOCKED</b> (No whitelist keywords found)")
            is_blocked = True
            block_reason = "Missing Required Keywords"
        else:
            results.append(f"✅ <b>Keywords Filter:</b> ✅ <b>PASSED</b> (Matched: <code>{', '.join(matched_kw)}</code>)")
    elif keywords:
        results.append("✅ <b>Keywords Filter:</b> ⚪ <i>Skipped (Already Blocked)</i>")
    else:
        results.append("✅ <b>Keywords Filter:</b> ⚪ <i>Inactive</i>")

    # 4. Blacklist Check
    blacklist = route.get("blacklist", [])
    if blacklist and not is_blocked:
        matched_bl = [bl for bl in blacklist if bl.lower() in text.lower()]
        if matched_bl:
            results.append(f"🚫 <b>Blacklist Filter:</b> ❌ <b>BLOCKED</b> (Contains blacklisted word: <code>{', '.join(matched_bl)}</code>)")
            is_blocked = True
            block_reason = "Contains Blacklisted Words"
        else:
            results.append("🚫 <b>Blacklist Filter:</b> ✅ <b>PASSED</b> (No blacklisted words)")
    elif blacklist:
        results.append("🚫 <b>Blacklist Filter:</b> ⚪ <i>Skipped (Already Blocked)</i>")
    else:
        results.append("🚫 <b>Blacklist Filter:</b> ⚪ <i>Inactive</i>")

    # 5. RegEx Pattern
    pattern_str = route.get("pattern", "")
    if pattern_str and not is_blocked:
        try:
            pattern = re.compile(pattern_str, re.IGNORECASE)
            if not pattern.search(text):
                results.append(f"🧠 <b>RegEx Pattern:</b> ❌ <b>BLOCKED</b> (Did not match <code>{pattern_str}</code>)")
                is_blocked = True
                block_reason = "RegEx Pattern Mismatch"
            else:
                results.append(f"🧠 <b>RegEx Pattern:</b> ✅ <b>PASSED</b>")
        except Exception as e:
            results.append(f"🧠 <b>RegEx Pattern:</b> ⚠️ <b>ERROR in RegEx:</b> <code>{e}</code>")
    elif pattern_str:
        results.append("🧠 <b>RegEx Pattern:</b> ⚪ <i>Skipped</i>")
    else:
        results.append("🧠 <b>RegEx Pattern:</b> ⚪ <i>Inactive</i>")

    # 6. Find & Replace
    replacements = route.get("replacements", {})
    applied_reps = []
    if replacements and not is_blocked:
        for old_txt, new_txt in replacements.items():
            if old_txt in text:
                text = text.replace(old_txt, new_txt)
                applied_reps.append(f"<code>{html.escape(old_txt)}</code> ➡ <code>{html.escape(new_txt)}</code>")
        if applied_reps:
            results.append("🔄 <b>Find & Replace:</b> " + " | ".join(applied_reps))
        else:
            results.append("🔄 <b>Find & Replace:</b> ⚪ <i>No target words found to replace</i>")
    elif replacements:
        results.append("🔄 <b>Find & Replace:</b> ⚪ <i>Skipped</i>")
    else:
        results.append("🔄 <b>Find & Replace:</b> ⚪ <i>None set</i>")

    # 7. Prefix & Suffix & Placeholders
    begin_text = route.get("begin_text", "").replace("\\n", "\n")
    end_text = route.get("end_text", "").replace("\\n", "\n")
    
    for tag, val in [
        ("[user.username]", f"@{sender_uname}"),
        ("[user.id]", sender_id),
        ("[user.first_name]", "Demo"),
        ("[user.last_name]", "User"),
    ]:
        text = text.replace(tag, val)
        begin_text = begin_text.replace(tag, val)
        end_text = end_text.replace(tag, val)

    final_preview = f"{begin_text}{text}{end_text}".strip()

    return is_blocked, block_reason, results, final_preview


@router.callback_query(RouteCB.filter(F.action == "test_route"))
async def cb_test_route_start(query: CallbackQuery, callback_data: RouteCB, state: FSMContext):
    await state.set_state(TestRouteState.waiting_for_sample)
    await state.update_data(route_idx=callback_data.route_idx)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Cancel", callback_data=RouteCB(action="manage", route_idx=callback_data.route_idx))
    
    await query.message.edit_text(
        f"🧪 <b>Route Simulator / Dry Run (Route #{callback_data.route_idx + 1})</b>\n\n"
        f"Send any sample message, alert, or text below.\n\n"
        f"The simulator will test it against all your filters, whitelist/blacklists, RegEx rules, and find-and-replace transformations without sending anything to your destination channel.",
        reply_markup=kb.as_markup()
    )
    await query.answer()


@router.message(TestRouteState.waiting_for_sample)
async def fsm_catch_test_sample(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    idx = data.get("route_idx")
    await state.clear()
    
    uid = str(msg.from_user.id)
    users = await load_users()
    routes = users.get(uid, {}).get("routes", [])
    
    if idx is None or idx >= len(routes):
        return await msg.answer("❌ Error: Route not found.")
        
    route = routes[idx]
    sample_text = msg.text or msg.caption or ""
    sender_uname = msg.from_user.username or "DemoUser"
    sender_id = str(msg.from_user.id)

    is_blocked, reason, step_results, final_preview = simulate_route_pipeline(
        route, sample_text, sender_uname=sender_uname, sender_id=sender_id
    )

    steps_formatted = "\n".join(step_results)
    verdict = (
        f"🔴 <b>REJECTED:</b> Message would NOT be forwarded ({reason})"
        if is_blocked
        else "🟢 <b>PASSED:</b> Message would be forwarded successfully!"
    )

    report = (
        f"🧪 <b>Dry Run Results for Route #{idx + 1} ({route.get('name', 'Route')})</b>\n\n"
        f"📊 <b>Pipeline Diagnostics:</b>\n"
        f"{steps_formatted}\n\n"
        f"🎯 <b>Final Verdict:</b>\n{verdict}\n\n"
    )
    
    if not is_blocked and final_preview:
        report += (
            f"👁️ <b>Mock Output Preview:</b>\n"
            f"-----------------------------------\n"
            f"{html.escape(final_preview)}\n"
            f"-----------------------------------"
        )
    elif is_blocked:
        report += "⚠️ <i>No output generated because the message was dropped by one of your filters.</i>"

    kb = InlineKeyboardBuilder()
    kb.button(text="🧪 Test Another Message", callback_data=RouteCB(action="test_route", route_idx=idx))
    kb.button(text="🔙 Back to Route", callback_data=RouteCB(action="manage", route_idx=idx))
    kb.adjust(1, 1)

    await msg.answer(report, reply_markup=kb.as_markup())


@router.message(Command("testroute"))
async def cmd_testroute(msg: types.Message, state: FSMContext):
    uid = str(msg.from_user.id)
    if not await is_user_linked(uid):
        return await msg.answer("❌ Log in first.")
    
    args = msg.text.split(maxsplit=2)
    if len(args) < 2:
        return await msg.answer(
            "🛠 <b>Usage:</b>\n<code>/testroute &lt;route_id&gt; [optional sample text]</code>\n\n"
            "💡 <b>Example:</b>\n<code>/testroute 1 Bitcoin is pumping to 100k!</code>"
        )
        
    users = await load_users()
    routes = users.get(uid, {}).get("routes", [])
    try:
        idx = int(args[1]) - 1
        if idx < 0 or idx >= len(routes):
            raise IndexError
    except (ValueError, IndexError):
        return await msg.answer("❌ Invalid route ID.")

    if len(args) == 3:
        sample_text = args[2]
        route = routes[idx]
        is_blocked, reason, step_results, final_preview = simulate_route_pipeline(
            route, sample_text, sender_uname=msg.from_user.username or "DemoUser", sender_id=str(msg.from_user.id)
        )
        steps_formatted = "\n".join(step_results)
        verdict = f"🔴 <b>REJECTED:</b> ({reason})" if is_blocked else "🟢 <b>PASSED & FORWARDED</b>"
        
        report = (
            f"🧪 <b>Dry Run: Route #{idx + 1}</b>\n\n"
            f"{steps_formatted}\n\n"
            f"🎯 <b>Result:</b> {verdict}\n\n"
        )
        if not is_blocked and final_preview:
            report += f"👁️ <b>Mock Output Preview:</b>\n<pre>{html.escape(final_preview)}</pre>"
        await msg.answer(report)
    else:
        await state.set_state(TestRouteState.waiting_for_sample)
        await state.update_data(route_idx=idx)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Cancel", callback_data=RouteCB(action="manage", route_idx=idx))
        await msg.answer(
            f"🧪 <b>Testing Route #{idx + 1}</b>\nPlease reply with the sample text you want to test:",
            reply_markup=kb.as_markup()
        )


@router.callback_query(MenuCB.filter(F.action == "add_route_start"))
async def cb_add_route_start(query: types.CallbackQuery, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Cancel", callback_data=MenuCB(action="refresh"))
    
    await state.set_state(ConfigState.waiting_for_new_route_src)
    await query.message.edit_text(
        "➕ <b>Add New Route</b>\n\n"
        "Step 1: Send the <b>Source</b> channel.\n\n"
        "<i>💡 Tip: Send a public @username, private message link (https://t.me/c/0987654321/12), invite link, or raw ID!</i>", 
        reply_markup=kb.as_markup()
    )
    await query.answer()


@router.message(ConfigState.waiting_for_new_route_src)
async def fsm_add_route_src(msg: types.Message, state: FSMContext):
    input_text = msg.text or msg.caption or ""
    if not input_text:
        return await msg.answer("❌ Please send text or a link.")
    
    src = parse_smart_input(input_text)
    await state.update_data(src=src)
    await state.set_state(ConfigState.waiting_for_new_route_dest)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Cancel", callback_data=MenuCB(action="refresh"))
    
    safe_src = html.escape(src)
    warn = ""
    if any(x in src for x in ["-100", "t.me/+", "joinchat"]):
        warn = f"✅ Extracted Private Source: <code>{safe_src}</code>\n\n⚠️ <i>CRITICAL: Your linked account MUST join this channel for the AutoForwarder to work!</i>\n\n"
        
    await msg.answer(f"{warn}Step 2: Send the <b>Destination</b> channel:\n\n<i>(Link, username, or ID)</i>", reply_markup=kb.as_markup())


@router.message(ConfigState.waiting_for_new_route_dest)
async def fsm_add_route_dest(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    input_text = msg.text or msg.caption or ""
    if not input_text:
        return await msg.answer("❌ Please send text or a link.")
    
    dest = parse_smart_input(input_text)
    await state.update_data(src=data['src'], dest=dest)
    await state.set_state(ConfigState.waiting_for_route_name)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Skip (Auto-Generate Name)", callback_data=MenuCB(action="skip_route_name"))
    kb.button(text="❌ Cancel", callback_data=MenuCB(action="refresh"))
    
    safe_dest = html.escape(dest)
    warn = ""
    if any(x in dest for x in ["-100", "t.me/+", "joinchat"]):
        warn = f"✅ Extracted Private Dest: <code>{safe_dest}</code>\n\n⚠️ <i>CRITICAL: Your linked account MUST have permission to post messages in the destination channel!</i>\n\n"

    await msg.answer(
        f"{warn}Step 3: What do you want to name this route?\n(e.g., 'VIP Signals')\n\n<i>Click Skip to automatically name it.</i>",
        reply_markup=kb.as_markup()
    )


@router.message(ConfigState.waiting_for_route_name)
async def fsm_catch_route_name(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    await finalize_new_route(str(msg.from_user.id), data['src'], data['dest'], msg.text.strip(), msg, state)


@router.callback_query(MenuCB.filter(F.action == "skip_route_name"))
async def cb_skip_route_name(query: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await finalize_new_route(str(query.from_user.id), data['src'], data['dest'], None, query.message, state)
    await query.answer()


async def execute_id_lookup(msg: types.Message, query: str = None):
    uid = str(msg.from_user.id)
    text_parts = []

    if msg.forward_origin:
        origin = msg.forward_origin
        if isinstance(origin, MessageOriginUser):
            text_parts.append(f"👤 <b>Forwarded User ID:</b>\n<code>{origin.sender_user.id}</code>")
        elif isinstance(origin, MessageOriginChannel):
            text_parts.append(f"📢 <b>Forwarded Channel ID:</b>\n<code>{origin.chat.id}</code>")
        elif isinstance(origin, MessageOriginChat):
            text_parts.append(f"🎭 <b>Forwarded Group ID:</b>\n<code>{origin.sender_chat.id}</code>")
        elif isinstance(origin, MessageOriginHiddenUser):
            text_parts.append(f"🔒 <b>Hidden Forward:</b>\nUser '{origin.sender_user_name}' hides their ID. Please provide their @username instead.")
            
    elif msg.reply_to_message:
        rep = msg.reply_to_message
        if rep.sender_chat:
            text_parts.append(f"🎭 <b>Sender Chat/Channel ID:</b>\n<code>{rep.sender_chat.id}</code>")
        elif rep.from_user:
            text_parts.append(f"👤 <b>Replied User ID:</b>\n<code>{rep.from_user.id}</code>")

    elif query or msg.text or msg.caption:
        raw_text = query or msg.text or msg.caption or ""
        search_text = raw_text.replace("/id", "").replace("/getid", "").strip()
        
        if search_text.lower() == "me":
            text_parts.append(f"🪪 <b>Your ID:</b>\n<code>{msg.from_user.id}</code>")
        elif search_text:
            private_link_match = re.search(r't\.me/(?:c/)?(\d+)/', search_text)
            if private_link_match:
                raw_id = private_link_match.group(1)
                text_parts.append(f"🔒 <b>Private Channel/Group ID:</b>\n<code>-100{raw_id}</code>")
            else:
                if not await is_user_linked(uid):
                    text_parts.append("❌ <b>Login Required</b> to resolve @usernames. Use /login.")
                else:
                    status_msg = await msg.answer("⏳ <i>Querying Telegram's database using your session...</i>")
                    client = forwarder_core.clients_per_user.get(uid)
                    is_temp_client = False
                    if not client or not client.is_connected():
                        try:
                            client = await forwarder_core.start_client_background(uid, API_ID, API_HASH)
                            is_temp_client = False 
                        except Exception:
                            client = None
                            
                    if client:
                        try:
                            if search_text.startswith("https://t.me/") and "+" not in search_text:
                                search_text = "@" + search_text.split("/")[-1]
                                
                            entity = await client.get_entity(search_text)
                            e_type = "User"
                            if hasattr(entity, 'broadcast') and entity.broadcast:
                                e_type = "Channel"
                            elif hasattr(entity, 'megagroup') and entity.megagroup:
                                e_type = "Supergroup"
                            elif hasattr(entity, 'participants_count'):
                                e_type = "Basic Group"
                            
                            final_id = entity.id
                            if e_type in ["Channel", "Supergroup"]:
                                final_id = f"-100{entity.id}"
                            elif e_type == "Basic Group":
                                final_id = f"-{entity.id}"

                            text_parts.append(
                                f"✅ <b>Resolved {e_type}:</b> {getattr(entity, 'title', getattr(entity, 'first_name', search_text))}\n"
                                f"🪪 <b>ID:</b>\n<code>{final_id}</code>"
                            )

                        except ValueError:
                            text_parts.append(f"❌ <b>Not Found:</b> Could not resolve '<code>{search_text}</code>'.")
                        except Exception as e:
                            text_parts.append(f"❌ <b>Unexpected API Error:</b>\n<code>{e}</code>")
                        finally:
                            if is_temp_client and client.is_connected():
                                await client.disconnect()
                    else:
                        text_parts.append("❌ <b>Session Error:</b> Could not connect to your Telegram account to run the search.")
                    await status_msg.delete()

    if not text_parts:
        text_parts.append(f"🪪 <b>Your ID:</b> <code>{msg.from_user.id}</code>\n🏷 <b>Current Chat ID:</b> <code>{msg.chat.id}</code>")

    await msg.answer("\n\n".join(text_parts))


@router.message(IdFinderState.waiting_for_input)
async def fsm_catch_id_finder(msg: types.Message, state: FSMContext):
    await state.clear()
    input_text = msg.text or msg.caption or ""
    await execute_id_lookup(msg, query=input_text.strip())


@router.message(Command("id", "getid"))
async def cmd_id_lookup(msg: types.Message):
    await execute_id_lookup(msg)


@router.message(Command("cancel"))
async def cmd_global_cancel(msg: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        return 
    await state.clear()
    await msg.answer("🛑 <b>Action Cancelled.</b>\nYou have been returned to the normal chat mode.")


@router.message(Command("instructions"))
async def cmd_instructions(msg: types.Message):
    uid = str(msg.from_user.id)
    users = await load_users()
    u = users.get(uid, {})
    
    is_linked = u.get("linked", False)
    has_routes = len(u.get("routes", [])) > 0
    is_engine_on = u.get("enabled", False)
    
    check_linked = "✅" if is_linked else "❌"
    check_routes = "✅" if has_routes else "❌"
    check_engine = "✅" if is_engine_on else "❌"
    
    text = (
        "📋 <b>Your AutoForwarder Setup Checklist</b>\n\n"
        f"{check_linked} <b>1. Link Account:</b> {'Done!' if is_linked else 'Type <code>/login +&lt;number&gt;</code> to connect.'}\n\n"
        f"{check_routes} <b>2. Add a Route:</b> {'Done!' if has_routes else 'Type /addroute to set up where messages go.'}\n\n"
        f"{check_engine} <b>3. Start Engine:</b> {'Running!' if is_engine_on else 'Open /dashboard and click Turn Engine ON.'}\n\n"
        "-----------------------------------\n"
        "💎 <b>Premium Status:</b> Active (Free Access Event)\n\n"
    )
    await msg.answer(text)


@router.message(Command("start"))
async def cmd_start(msg: types.Message, state: FSMContext):
    uid = str(msg.from_user.id)
    users = await load_users()

    if uid not in users:
        users[uid] = {
            "phone": "",
            "linked": False,
            "enabled": False, 
            "awaiting_password": False,
            "created_at": int(time.time()),
            "sub_expiry": 0,
        }
        await save_users(users)

    if await is_user_linked(uid):
        await cmd_dashboard(msg, state)
    else:
        kb = InlineKeyboardBuilder()
        kb.button(text="🔑 Secure Login", callback_data=MenuCB(action="start_login"))
        
        welcome_text = (
            "👋 <b>Welcome to AutoForwarder!</b>\n\n"
            "To get started, you need to securely link your Telegram account to the bot.\n\n"
            "👉 <b>Click the button below to begin:</b>\n\n"
            "📖 Use /help to see all commands."
        )
        await msg.answer(welcome_text, reply_markup=kb.as_markup())


@router.callback_query(MenuCB.filter(F.action == "start_login"))
async def cb_start_login(query: types.CallbackQuery, state: FSMContext):
    uid = str(query.from_user.id)
    if not await system_load_ok():
        return await query.answer("❌ Server under high load. Try later.", show_alert=True)
    if await is_user_linked(uid):
        return await query.answer("❌ Already linked. Use /dashboard.", show_alert=True)

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Cancel", callback_data=MenuCB(action="cancel_fsm"))
    
    await state.set_state(LoginStates.waiting_for_phone)
    await query.message.edit_text(
        "📱 <b>Secure Login Process</b>\n\n"
        "Please reply with your phone number, starting with the <b>+</b> and your <b>country code</b>.\n\n"
        "💡 <b>Example:</b> <code>+19876543210</code>\n\n"
        "🔍 <i>Don't know your country code? Check here:</i>\n"
        "<a href='https://en.wikipedia.org/wiki/List_of_country_calling_codes'>List of Country Codes</a>",
        reply_markup=kb.as_markup(),
        disable_web_page_preview=True
    )
    await query.answer()


@router.message(Command("status"))
async def cmd_status(msg: types.Message):
    uid = str(msg.from_user.id)
    users = await load_users()
    u = users.get(uid)
    if not u:
        return await msg.answer("No account data found. Use /start.")

    await msg.answer(
        f"📊 <b>Account Status</b>\n\n"
        f"- Logged in: {'✅ Yes' if u.get('linked') else '❌ No'}\n"
        f"- Bot Engine: {'✅ ON' if u.get('enabled') else '❌ OFF'}\n"
        f"- Tier: 👑 Premium (Free Access Event)\n"
    )


@router.message(Command("login"))
async def cmd_login(msg: types.Message, state: FSMContext):
    if not await system_load_ok():
        return await msg.answer("❌ Server under high load. Try later.")
    uid = str(msg.from_user.id)
    if await is_user_linked(uid):
        return await msg.answer("❌ Already linked. Logout first.")
        
    args = msg.text.split()
    if len(args) == 2:
        await process_login_phone(msg, args[1], uid)
    else:
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Cancel", callback_data=MenuCB(action="cancel_fsm"))
        await state.set_state(LoginStates.waiting_for_phone)
        await msg.answer(
            "📱 <b>Secure Login Process</b>\n\n"
            "Please reply with your phone number, starting with the <b>+</b> and your <b>country code</b>.\n\n"
            "💡 <b>Example:</b> <code>+19876543210</code>",
            reply_markup=kb.as_markup()
        )


@router.message(LoginStates.waiting_for_phone)
async def fsm_catch_phone(msg: types.Message, state: FSMContext):
    uid = str(msg.from_user.id)
    phone = msg.text.strip()
    
    if not phone.startswith("+"):
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Cancel", callback_data=MenuCB(action="cancel_fsm"))
        return await msg.answer(
            "❌ <b>Invalid Format:</b> Please make sure your number starts with a <b>+</b> followed by your country code.\n\n"
            "💡 <b>Example:</b> <code>+19876543210</code>",
            reply_markup=kb.as_markup()
        )
        
    await state.clear()
    await process_login_phone(msg, phone, uid)


async def process_login_phone(msg: types.Message, phone: str, uid: str):
    loading_msg = await msg.answer("⏳ <i>Connecting securely to Telegram servers... Please wait ~3 seconds.</i>")
    
    users = await load_users()
    users.setdefault(uid, {}).update({"phone": phone, "linked": False, "enabled": False, "awaiting_password": False})
    await save_users(users)

    client = get_client(uid)
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        users = await load_users()
        users[uid]["sent_code_hash"] = getattr(sent, "phone_code_hash", str(sent))
        await save_users(users)
        
        await loading_msg.edit_text(
            "✅ <b>Login Code Sent!</b>\n\n"
            "Telegram has just sent a <b>Login Code</b> to your Telegram App. "
            "Once you have it, reply to this message using the word ALLOW followed immediately by your Telegram code.\n\n"
            "💡 <b>Example:</b> If your code is 12345, type:\n<code>ALLOW12345</code>"
        )
    except Exception as e:
        await loading_msg.edit_text(f"❌ <b>Failed to send code:</b> {e}\nPlease check your number and try /login again.")
    finally:
        await client.disconnect()


@router.message(F.text.regexp(r"ALLOW\d+"))
async def handler_allow_code(msg: types.Message):
    uid = str(msg.from_user.id)
    match = re.match(r"ALLOW(\d+)", msg.text.strip())
    if not match:
        return
    
    users = await load_users()
    user = users.get(uid)
    if not user or not user.get("sent_code_hash"):
        return await msg.answer("❌ No login attempt found. Use /login.")

    client = get_client(uid)
    try:
        await client.connect()
        await client.sign_in(phone=user["phone"], code=match.group(1), phone_code_hash=user["sent_code_hash"])
        users = await load_users()
        
        users[uid].update({"linked": True, "enabled": True, "awaiting_password": False})
        users[uid].pop("sent_code_hash", None)
        users[uid].pop("session_dead_notified", None)
        
        await save_users(users)
        await reload_forwarder_routes_for_user(uid)
        await msg.answer("🎉 <b>Login Successful!</b>\nYour Telegram account is now securely linked to the AutoForwarder engine.\nUse /dashboard to get started.")
    except SessionPasswordNeededError:
        users = await load_users()
        users[uid]["awaiting_password"] = True
        await save_users(users)
        await msg.answer("🔒 <b>2FA Required</b>\nType: <code>/password your_actual_password</code>")
    except Exception as e:
        await msg.answer(f"❌ Login failed: {e}. Enter Again.")
    finally:
        await client.disconnect()


@router.message(Command("password"))
async def cmd_password(msg: types.Message):
    uid = str(msg.from_user.id)
    users = await load_users()
    if not users.get(uid, {}).get("awaiting_password"):
        return await msg.answer("❌ Not awaiting password.")
        
    args = msg.text.split(maxsplit=1)
    if len(args) != 2:
        return await send_usage(msg, "password")
    
    client = get_client(uid)
    try:
        await client.connect()
        await client.sign_in(password=args[1])
        users = await load_users()
        
        users[uid].update({"linked": True, "enabled": True, "awaiting_password": False})
        users[uid].pop("session_dead_notified", None)
        
        await save_users(users)
        await reload_forwarder_routes_for_user(uid) 
        await msg.answer("🎉 <b>Login Successful!</b>\nYour Telegram account is now securely linked to the AutoForwarder engine.\nUse /dashboard to get started.")
    except Exception as e:
        await msg.answer(f"❌ Failed: {e}. Enter Again.")
    finally:
        await client.disconnect()


@router.message(Command("logout"))
async def cmd_logout(msg: types.Message, state: FSMContext):
    if not await is_user_linked(str(msg.from_user.id)):
        return await msg.answer("❌ You are not logged in.")
        
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Cancel Logout", callback_data=MenuCB(action="cancel_logout"))
    
    await msg.answer(
        "😔 <b>Sorry to see you go!</b>\n\n"
        "Could you briefly tell us why you are leaving? (Type your reason below, or type <b>SKIP</b> to bypass this step).",
        reply_markup=kb.as_markup()
    )
    await state.set_state(LogoutStates.waiting_for_feedback)


@router.message(LogoutStates.waiting_for_feedback)
async def catch_logout_feedback(msg: types.Message, state: FSMContext):
    feedback = msg.text.strip()
    if feedback.upper() != "SKIP" and BUG_CHANNEL_ID:
        try:
            report = (
                f"👋 <b>User Leaving Feedback</b>\n"
                f"👤 User: <code>{msg.from_user.id}</code>\n"
                f"💬 Reason: {html.escape(feedback)}"
            )
            await bot.send_message(BUG_CHANNEL_ID, report, parse_mode="HTML")
        except Exception:
            pass

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Cancel Logout", callback_data=MenuCB(action="cancel_logout"))

    await msg.answer(
        "⚠️ <b>Final Confirmation</b>\n\n"
        "Are you sure you want to logout? All your active sessions and routes will be deleted.\n"
        "Type <code>DELETE</code> to confirm.",
        reply_markup=kb.as_markup()
    )
    await state.set_state(LogoutStates.waiting_confirmation)


@router.callback_query(MenuCB.filter(F.action == "cancel_logout"))
async def cb_cancel_logout(query: CallbackQuery, state: FSMContext):
    await state.clear()
    await query.message.edit_text("✅ <b>Logout Cancelled.</b>\nYou are still safely logged in!")
    await query.answer()


@router.message(LogoutStates.waiting_confirmation, F.text == "DELETE")
async def confirm_logout(msg: types.Message, state: FSMContext):
    uid = str(msg.from_user.id)
    users = await load_users()

    forwarder_core._remove_handlers_for_user(uid)
    client = forwarder_core.clients_per_user.pop(uid, None)
    if client and client.is_connected():
        try:
            await client.disconnect()
        except Exception:
            pass
        
    old_task = forwarder_tasks.pop(uid, None)
    if old_task and not old_task.done():
        old_task.cancel()

    for ext in ("", ".session", ".session-journal", ".session.lock"):
        path = os.path.join(SESSIONS_DIR, uid) + ext
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass

    if uid in users:
        users[uid]["linked"] = False
        users[uid]["enabled"] = False
        users[uid]["phone"] = ""
        users[uid]["routes"] = [] 
        
    await save_users(users)
    await msg.answer("✅ Logged out safely. Session deleted.")
    await state.clear()


@router.message(Command("on"))
async def cmd_on(msg: types.Message):
    uid = str(msg.from_user.id)
    if not await is_user_linked(uid):
        return await msg.answer("❌ Log in first.")
    users = await load_users()
    users[uid]["enabled"] = True
    await save_users(users)
    await reload_forwarder_routes_for_user(uid)
    await msg.answer("✅ Bot Engine is now ON.")


@router.message(Command("off"))
async def cmd_off(msg: types.Message):
    uid = str(msg.from_user.id)
    if not await is_user_linked(uid):
        return await msg.answer("❌ Log in first.")
    users = await load_users()
    users[uid]["enabled"] = False
    await save_users(users)
    await reload_forwarder_routes_for_user(uid)
    await msg.answer("✅ Bot Engine is now OFF.")


async def _toggle_route_state(msg, state_bool, success_msg):
    uid = str(msg.from_user.id)
    if not await is_user_linked(uid):
        return await msg.answer("❌ Log in first.")
    args = msg.text.split()
    if len(args) != 2:
        return await send_usage(msg, msg.text.split()[0][1:])
    users = await load_users()
    routes = users.get(uid, {}).get("routes", [])
    try:
        idx = int(args[1]) - 1
        if idx < 0 or idx >= len(routes):
            raise IndexError
        routes[idx]["is_active"] = state_bool
        await save_users(users)
        await reload_forwarder_routes_for_user(uid)
        await msg.answer(success_msg.format(idx+1))
    except (ValueError, IndexError):
        await msg.answer("❌ Invalid route ID.")


@router.message(Command("onroute"))
async def cmd_onroute(m: types.Message):
    await _toggle_route_state(m, True, "✅ Route {} is now ON.")


@router.message(Command("offroute"))
async def cmd_offroute(m: types.Message):
    await _toggle_route_state(m, False, "⏸ Route {} is now PAUSED.")


@router.message(Command("addroute"))
async def cmd_addroute(msg: types.Message, state: FSMContext):
    uid = str(msg.from_user.id)
    if not await is_user_linked(uid):
        return await msg.answer("❌ Log in first.")
    args = msg.text.split()
    if len(args) != 3:
        await state.set_state(ConfigState.waiting_for_new_route_src)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Cancel", callback_data=MenuCB(action="cancel_fsm"))
        return await msg.answer(
            "➕ <b>Add New Route</b>\n\nStep 1: Send the <b>Source</b> channel.\n\n"
            "<i>💡 Tip: Send a public @username, private message link (https://t.me/c/0987654321/12), invite link, or raw ID!</i>",
            reply_markup=kb.as_markup() 
        )
    src, dest = parse_smart_input(args[1]), parse_smart_input(args[2])
    await state.update_data(src=src, dest=dest)
    await state.set_state(ConfigState.waiting_for_route_name)
    
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Skip (Auto-Generate Name)", callback_data=MenuCB(action="skip_route_name"))
    kb.button(text="❌ Cancel", callback_data=MenuCB(action="refresh"))
    
    safe_src, safe_dest = html.escape(src), html.escape(dest)
    warn = ""
    if any(x in src or x in dest for x in ["-100", "t.me/+", "joinchat"]):
        warn = "⚠️ <i><b>CRITICAL:</b> Private link detected. Your linked account MUST physically join these channels, or forwarding will fail!</i>\n\n"
        
    await msg.answer(
        f"{warn}📝 Route: <code>{safe_src}</code> ➡ <code>{safe_dest}</code>.\n\n"
        f"What do you want to name this route?\n<i>Click Skip to fetch titles automatically.</i>",
        reply_markup=kb.as_markup()
    )


@router.message(Command("routes"))
async def cmd_routes(msg: types.Message, state: FSMContext):
    await state.clear()
    uid = str(msg.from_user.id)
    if not await is_user_linked(uid):
        return await msg.answer("❌ You must log in first. Use <code>/login +&lt;number&gt;</code>")
        
    users = await load_users()
    routes = users.get(uid, {}).get("routes", [])
    if not routes:
        return await msg.answer("No routes found. Use /addroute or the /dashboard to create one.")
        
    kb = InlineKeyboardBuilder()
    for idx, route in enumerate(routes):
        status_icon = "🟢" if route.get("is_active", True) else "🔴"
        btn_text = f"{status_icon} {route['from']} ➡ {route['to']}"
        kb.button(text=btn_text, callback_data=RouteCB(action="manage", route_idx=idx))
        
    kb.button(text="🔙 Back to Dashboard", callback_data=MenuCB(action="refresh"))
    kb.adjust(1) 
    await msg.answer("🛣 <b>Your Routes</b>\nSelect a route to configure:", reply_markup=kb.as_markup())


@router.message(Command("delroute"))
async def cmd_delroute(msg: types.Message):
    uid = str(msg.from_user.id)
    if not await is_user_linked(uid):
        return await msg.answer("❌ Log in first.")
    args = msg.text.split()
    if len(args) != 2:
        return await send_usage(msg, "delroute")

    users = await load_users()
    routes = users[uid].get("routes", [])
    try:
        idx = int(args[1]) - 1
        removed = routes.pop(idx)
        users[uid]["routes"] = routes
        await save_users(users)
        await reload_forwarder_routes_for_user(uid)
        await msg.answer(f"🗑️ Deleted route {idx + 1}: {removed['from']} → {removed['to']}")
    except (ValueError, IndexError):
        await msg.answer("❌ Invalid route ID.")


@router.message(Command("editroute"))
async def cmd_editroute(msg: types.Message):
    uid = str(msg.from_user.id)
    if not await is_user_linked(uid):
        return await msg.answer("❌ Log in first.")
    args = msg.text.split()
    if len(args) != 4:
        return await send_usage(msg, "editroute")
    try:
        idx = int(args[1]) - 1
    except ValueError:
        return await msg.answer("❌ Route ID must be a number.")

    new_src, new_dest = parse_smart_input(args[2]), parse_smart_input(args[3])
    users = await load_users()
    routes = users.get(uid, {}).get("routes", [])
    if idx < 0 or idx >= len(routes):
        return await msg.answer("❌ Invalid route ID.")
    routes[idx]["from"] = new_src
    routes[idx]["to"] = new_dest
    users[uid]["routes"] = routes
    await save_users(users)
    await reload_forwarder_routes_for_user(uid)
    safe_src, safe_dest = html.escape(new_src), html.escape(new_dest)
    await msg.answer(f"✅ Route {idx+1} updated to: <b>{safe_src}</b> → <b>{safe_dest}</b>")


@router.message(Command("editroutename"))
async def cmd_editroutename(msg: types.Message):
    uid = str(msg.from_user.id)
    if not await is_user_linked(uid):
        return await msg.answer("❌ Log in first.")
    args = msg.text.split(maxsplit=2)
    if len(args) != 3:
        return await send_usage(msg, "editroutename")
    users = await load_users()
    routes = users.get(uid, {}).get("routes", [])
    try: 
        idx = int(args[1]) - 1
        new_name = args[2]
        if idx < 0 or idx >= len(routes):
            raise IndexError
        routes[idx]["name"] = new_name
        users[uid]["routes"] = routes
        await save_users(users)
        await msg.answer(f"✅ Route {idx+1} renamed to: <b>{new_name}</b>")
    except (ValueError, IndexError):
        await msg.answer("❌ Invalid route ID.")


async def _update_route_prop(msg, prop_name, parser_func, success_msg):
    uid = str(msg.from_user.id)
    if not await is_user_linked(uid):
        return await msg.answer("❌ Log in first.")
    users = await load_users()
    args = msg.text.split(maxsplit=2)
    if len(args) != 3:
        return await send_usage(msg, msg.text.split()[0][1:])
    routes = users[uid].get("routes", [])
    try:
        idx = int(args[1]) - 1
        if idx < 0 or idx >= len(routes):
            raise IndexError
        routes[idx][prop_name] = parser_func(args[2])
        await save_users(users)
        await reload_forwarder_routes_for_user(uid)
        await msg.answer(success_msg.format(idx+1))
    except (ValueError, IndexError):
        await msg.answer("❌ Invalid route ID or value.")


async def _clear_route_prop(msg, prop_name, default_val, success_msg):
    uid = str(msg.from_user.id)
    if not await is_user_linked(uid):
        return await msg.answer("❌ Log in first.")
    args = msg.text.split()
    if len(args) != 2:
        return await send_usage(msg, args[0][1:])
    users = await load_users()
    routes = users.get(uid, {}).get("routes", [])
    try:
        idx = int(args[1]) - 1
        if idx < 0 or idx >= len(routes):
            raise IndexError
        routes[idx][prop_name] = default_val
        await save_users(users)
        await reload_forwarder_routes_for_user(uid)
        await msg.answer(success_msg.format(idx+1))
    except (ValueError, IndexError):
        await msg.answer("❌ Invalid route ID.")


@router.message(Command("filter"))
async def cmd_find_replace(msg: types.Message):
    uid = str(msg.from_user.id)
    if not await is_user_linked(uid):
        return
    users = await load_users()
    args = msg.text.split(maxsplit=2)
    if len(args) != 3 or "|" not in args[2]:
        return await send_usage(msg, "filter")

    find_txt, replace_txt = [x.strip() for x in args[2].split("|", 1)]
    routes = users[uid].get("routes", [])
    try:
        idx = int(args[1]) - 1
        if idx < 0 or idx >= len(routes):
            raise IndexError
        reps = routes[idx].get("replacements", {})
        reps[find_txt] = replace_txt
        routes[idx]["replacements"] = reps
        await save_users(users)
        await reload_forwarder_routes_for_user(uid)
        await msg.answer(f"✅ Route {idx+1}: Will now replace '<b>{find_txt}</b>' with '<b>{replace_txt}</b>'.")
    except Exception:
        await msg.answer("❌ Invalid route ID.")


@router.message(Command("whitelistuser"))
async def cmd_whitelist_user(m: types.Message):
    await _update_route_prop(
        m, "allowed_users", lambda x: [u.strip().lower() for u in x.split(",")],
        "✅ Whitelisted users for route {} updated."
    )


async def _toggle_route_setting(msg, setting_key, success_msg):
    uid = str(msg.from_user.id)
    if not await is_user_linked(uid):
        return
    users = await load_users()
    args = msg.text.split()
    if len(args) != 2:
        return await send_usage(msg, msg.text.split()[0][1:])
    try:
        idx = int(args[1]) - 1
        routes = users[uid].get("routes", [])
        routes[idx][setting_key] = not routes[idx].get(setting_key, False)
        await save_users(users)
        await reload_forwarder_routes_for_user(uid)
        status = "ON ✅" if routes[idx][setting_key] else "OFF ❌"
        await msg.answer(success_msg.format(idx+1) + f" is now {status}")
    except Exception:
        await msg.answer("❌ Invalid route ID.")


@router.message(Command("ignoretext"))
async def cmd_ignoretext(m: types.Message):
    await _toggle_route_setting(m, "ignore_text", "Stripping Text for Route {}")


@router.message(Command("ignoremedia"))
async def cmd_ignoremedia(m: types.Message):
    await _toggle_route_setting(m, "ignore_media", "Stripping Media for Route {}")


@router.message(Command("nativeforward"))
async def cmd_nativeforward(m: types.Message):
    await _toggle_route_setting(m, "native_forward", "Native 'Forwarded From' header for Route {}")


@router.message(Command("linkpreview"))
async def cmd_linkpreview(m: types.Message):
    await _toggle_route_setting(m, "disable_preview", "Disable URL Previews for Route {}")


@router.message(Command("autoupdate"))
async def cmd_autoupdate(msg: types.Message):
    uid = str(msg.from_user.id)
    if not await is_user_linked(uid):
        return await msg.answer("❌ Log in first.")
    users = await load_users()
    args = msg.text.split()
    if len(args) != 2:
        return await send_usage(msg, "autoupdate")
    routes = users.get(uid, {}).get("routes", [])
    try:
        idx = int(args[1]) - 1
        if idx < 0 or idx >= len(routes):
            raise IndexError
        current_state = routes[idx].get("auto_update", False)
        routes[idx]["auto_update"] = not current_state
        await save_users(users)
        await reload_forwarder_routes_for_user(uid)
        new_state_str = 'ENABLED ✅' if routes[idx]["auto_update"] else 'DISABLED ❌'
        await msg.answer(f"🔁 Auto-update for Route {idx+1} is now {new_state_str}.")
    except (ValueError, IndexError):
        await msg.answer("❌ Invalid route ID.")


@router.message(Command("setkeywords"))
async def cmd_kw(m: types.Message):
    await _update_route_prop(m, "keywords", lambda x: [k.strip() for k in x.split(",")], "✅ Keywords for route {} updated.")


@router.message(Command("setblacklist"))
async def cmd_bl(m: types.Message):
    await _update_route_prop(m, "blacklist", lambda x: [k.strip() for k in x.split(",")], "✅ Blacklist for route {} updated.")


@router.message(Command("setpattern"))
async def cmd_pat(m: types.Message):
    await _update_route_prop(m, "pattern", str, "✅ Pattern for route {} updated.")


@router.message(Command("setdelay"))
async def cmd_del(m: types.Message):
    await _update_route_prop(m, "delay", int, "✅ Delay for route {} set.")


@router.message(Command("setcooldown"))
async def cmd_cool(m: types.Message):
    await _update_route_prop(m, "cooldown", int, "✅ Cooldown for route {} set.")


@router.message(Command("setbegin"))
async def cmd_beg(m: types.Message):
    await _update_route_prop(m, "begin_text", lambda x: x.replace("\\n", "\n"), "✅ Prefix for route {} set.")


@router.message(Command("setend"))
async def cmd_end(m: types.Message):
    await _update_route_prop(m, "end_text", lambda x: x.replace("\\n", "\n"), "✅ Suffix for route {} set.")


@router.message(Command("clearkeywords"))
async def cmd_ckw(m: types.Message):
    await _clear_route_prop(m, "keywords", [], "✅ Keywords for route {} cleared.")


@router.message(Command("clearblacklist"))
async def cmd_cbl(m: types.Message):
    await _clear_route_prop(m, "blacklist", [], "✅ Blacklist for route {} cleared.")


@router.message(Command("clearpattern"))
async def cmd_cpat(m: types.Message):
    await _clear_route_prop(m, "pattern", "", "✅ Pattern for route {} cleared.")


@router.message(Command("clearend"))
async def cmd_cend(m: types.Message):
    await _clear_route_prop(m, "end_text", "", "✅ Suffix for route {} cleared.")


@router.message(Command("clearbegin"))
async def cmd_cbeg(m: types.Message):
    await _clear_route_prop(m, "begin_text", "", "✅ Prefix for route {} cleared.")


@router.message(Command("clearwhitelistuser"))
async def cmd_cwlu(m: types.Message):
    await _clear_route_prop(m, "allowed_users", [], "✅ Whitelisted users for route {} cleared.")


@router.message(Command("regex"))
async def cmd_regex_guide(msg: types.Message):
    guide = (
        "🧠 <b>Regular Expressions (Regex) Guide</b>\n\n"
        "Regex is a powerful system for matching complex text patterns. Instead of just matching a specific word like 'Bitcoin', you can match any crypto ticker, any price, or specific types of links.\n\n"
        "<b>1. The Basics:</b>\n"
        "• <code>|</code> means OR (e.g. <code>btc|eth</code> matches either)\n"
        "• <code>\\d+</code> matches any sequence of numbers\n"
        "• <code>.*</code> matches literally anything\n"
        "• <code>\\b</code> matches a whole word\n\n"
        "<b>2. Copy-Paste Examples for Forwarding:</b>\n\n"
        "🔗 <b>Match Any URL/Link:</b>\n<code>https?://\\S+</code>\n\n"
        "🐦 <b>Match Only Twitter/X Links:</b>\n<code>https?://(?:www\\.)?(twitter|x)\\.com/\\S+</code>\n\n"
        "💰 <b>Match Any Dollar Amount:</b>\n<code>\\$\\d+(?:\\.\\d+)?</code>\n\n"
        "📈 <b>Match Crypto Tickers (2 to 5 capital letters):</b>\n<code>\\b[A-Z]{2,5}\\b</code>\n\n"
        "<b>To use a pattern, type:</b>\n<code>/setpattern &lt;route_id&gt; &lt;your_regex&gt;</code>"
    )
    await msg.answer(guide)


@router.message(Command("help"))
async def cmd_help(msg: types.Message):
    basic_text = (
        "🟢 <b>Basic Commands</b>\n"
        "<i>Everything you need to run standard auto-forwarding.</i>\n\n"
        "<b>Account & Setup:</b>\n"
        "• /start - Welcome & onboarding\n"
        "• /login <code>+&lt;number&gt;</code> - Link your Telegram account\n"
        "• /logout - Safely unlink your account\n"
        "• /status - Check account tier\n"
        "• /instructions - View your AutoForwarder bot setup checklist\n\n"
        "<b>Dashboard & Routing:</b>\n"
        "• /dashboard - 🎛 Open the AutoForwarder bot Control Center\n"
        "• /addroute <code>&lt;from&gt; &lt;to&gt;</code> - Add a new route\n"
        "• /routes - List all active and paused routes\n"
        "• /editroute <code>&lt;id&gt; &lt;from&gt; &lt;to&gt;</code> - Change source/dest\n"
        "• /editroutename <code>&lt;id&gt; &lt;name&gt;</code> - Rename a route\n"
        "• /delroute <code>&lt;id&gt;</code> - Delete a route completely\n\n"
        "<b>Engine Controls:</b>\n"
        "• /on & /off - Turn the global bot engine ON or OFF\n"
        "• /onroute & /offroute <code>&lt;id&gt;</code> - Pause/resume a specific route\n\n"
        "<b>Support:</b>\n"
        "• /regex - Learn how to use RegEx pattern matching\n"
        "• /contact <code>&lt;msg&gt;</code> - Send a bug report or message to admin"
    )

    premium_text = (
        "💎 <b>Premium Filters</b>\n"
        "<i>Advanced tools for formatting, stripping, and filtering.</i>\n\n"
        "<b>Text & Media Manipulation:</b>\n"
        "• /filter <code>&lt;id&gt; &lt;Find&gt; | &lt;Replace&gt;</code> - Swap words/links\n"
        "• /setbegin & /setend <code>&lt;id&gt; &lt;text&gt;</code> - Add custom headers/footers\n"
        "• /ignoretext <code>&lt;id&gt;</code> - Drop text (Forward media only)\n"
        "• /ignoremedia <code>&lt;id&gt;</code> - Drop media (Forward text only)\n"
        "• /nativeforward <code>&lt;id&gt;</code> - Keep the original 'Forwarded from' tag\n"
        "• /linkpreview <code>&lt;id&gt;</code> - Turn URL thumbnail previews on/off\n\n"
        "<b>Advanced Filtering & Logic:</b>\n"
        "• /setkeywords <code>&lt;id&gt; &lt;words&gt;</code> - Only forward if words exist\n"
        "• /setblacklist <code>&lt;id&gt; &lt;words&gt;</code> - Never forward if words exist\n"
        "• /whitelistuser <code>&lt;id&gt; &lt;users&gt;</code> - Only forward specific senders\n"
        "• /setpattern <code>&lt;id&gt; &lt;regex&gt;</code> - Use RegEx to filter messages\n"
        "• /setdelay <code>&lt;id&gt; &lt;sec&gt;</code> - Add an artificial forwarding delay\n"
        "• /setcooldown <code>&lt;id&gt; &lt;sec&gt;</code> - Ignore duplicate spam for X seconds\n"
        "• /autoupdate <code>&lt;id&gt;</code> - Automatically sync edited/deleted messages\n"
    )
    
    await msg.answer(basic_text)
    await asyncio.sleep(1.2)
    await msg.answer(premium_text)


@router.message(Command("contact"))
async def cmd_contact(msg: types.Message):
    uid = msg.from_user.id
    args = msg.text.split(maxsplit=1)
    if len(args) < 2:
        return await send_usage(msg, "contact")
    report = (
        f"📨 <b>New Bug Report / Contact</b>\n\n"
        f"👤 <b>From:</b> @{msg.from_user.username or msg.from_user.full_name}\n"
        f"🆔 <b>User ID:</b> <code>{uid}</code>\n\n"
        f"💬 <b>Message:</b>\n{html.escape(args[1])}"
    )
    try:
        if BUG_CHANNEL_ID:
            await bot.send_message(BUG_CHANNEL_ID, report, parse_mode="HTML")
            await msg.answer("✅ Your message has been sent to the admin.")
        else:
            await msg.answer("ℹ️ Bug reporting channel is not configured on this instance.")
    except Exception:
        await msg.answer("❌ Failed to send message.")

# -------------------------------
# Lifecycle
# -------------------------------
async def main():
    log.info("Starting bot...")
    ensure_dirs()
    cleaned_files = 0
    for filename in os.listdir(TEMP_DIR):
        filepath = os.path.join(TEMP_DIR, filename)
        try:
            if os.path.isfile(filepath):
                os.remove(filepath)
                cleaned_files += 1
        except Exception as e:
            log.error(f"Failed to delete orphaned file {filepath}: {e}")
            
    if cleaned_files > 0:
        log.info(f"🧹 Swept away {cleaned_files} orphaned temp files from a previous crash.")

    await init_db()
    await reload_forwarder_routes_for_user(uid=None)
    
    try:
        await dp.start_polling(bot)
    finally:
        log.info("Shutting down safely...")
        await bot.session.close()
        for client in forwarder_core.clients_per_user.values():
            if client.is_connected():
                await client.disconnect()
        for t in forwarder_tasks.values():
            t.cancel()
        log.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
