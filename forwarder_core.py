"""
Telegram Forwarder Core - Free Community Edition (Ultra-Light)
- Memory leaks fixed via TTLCache for message_map
- Forum / Topic Support (message_thread_id / reply_to)
- Auto-detects dead/unregistered sessions and notifies manager
- Safer entity resolution to avoid API bans
- Restricted Channels: Image scraping only. Heavy media skipped.
- Dynamic placeholders and Find & Replace rules
"""

import asyncio
import time
import os
import re
import logging
from cachetools import TTLCache
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

# -----------------------------
# Globals & State Tracking
# -----------------------------
clients_per_user = {}       
active_handlers = {}        
active_edit_handlers = {}   
active_delete_handlers = {} 
route_caches = {}           

# TTLCache to prevent memory leaks over time
message_map = TTLCache(maxsize=50000, ttl=172800) 

SESSIONS_DIR = "data/sessions"
DEFAULT_COOLDOWN = 600
TEMP_DIR = "downloads" 
GLOBAL_FORWARDING_ENABLED = True

log = logging.getLogger("forwarder_core")

# -----------------------------
# Helpers
# -----------------------------
def get_client(uid, api_id, api_hash):
    if uid in clients_per_user:
        return clients_per_user[uid]
    session_path = os.path.join(SESSIONS_DIR, str(uid))
    client = TelegramClient(session_path, api_id, api_hash)
    clients_per_user[uid] = client
    return client

def parse_target_and_topic(target):
    """
    Parses a target string into (chat_id, topic_id).
    Supports formats like:
      - '-100123456789:45' -> (-100123456789, 45)
      - '@publicgroup:12'  -> ('@publicgroup', 12)
      - '-100123456789'    -> (-100123456789, None)
    """
    target_str = str(target).strip()
    topic_id = None

    if ":" in target_str:
        parts = target_str.split(":", 1)
        base_target = parts[0]
        try:
            topic_id = int(parts[1])
        except ValueError:
            topic_id = None
    else:
        base_target = target_str

    if base_target.lstrip("-").isdigit():
        dest = int(base_target)
    else:
        dest = base_target

    return dest, topic_id

async def resolve_entity(client, cid):
    if isinstance(cid, str) and cid.lstrip("-").isdigit():
        cid = int(cid)
    try:
        return await client.get_entity(cid)
    except ValueError:
        log.warning(f"Could not directly resolve {cid}. Searching dialogs...")
        async for dialog in client.iter_dialogs(limit=100): 
            if str(dialog.id) == str(cid) or str(dialog.name) == str(cid):
                return dialog.entity
    except Exception as e:
        log.error(f"Entity resolution error for {cid}: {e}")
        err_msg = str(e).lower()
        if "not registered" in err_msg or "unauthorized" in err_msg or "authkey" in err_msg:
            raise e
    return None

async def start_client_background(uid, api_id, api_hash):
    client = clients_per_user.get(uid)
    if client and client.is_connected():
        return client
    client = get_client(uid, api_id, api_hash)
    await client.connect()
    
    # Check authorization explicitly to avoid hanging on dead sessions
    if not await client.is_user_authorized():
        raise Exception("AuthKeyUnregisteredError: The key is not registered in the system")
        
    log.info(f"✅ Client {uid} started")
    return client

# -----------------------------
# Handler Factory
# -----------------------------
def make_handler(client, bots, keywords, pattern, recent_posts, route_extra, on_auth_error=None):
    start_time = time.time()
    src_topic_id = route_extra.get("src_topic_id", None)
    
    async def handler(event):
        if not GLOBAL_FORWARDING_ENABLED: return 
        
        msg = event.message
        chat_id = event.chat_id
        
        # --- 1. Forum Topic Filter for Source ---
        if src_topic_id is not None:
            reply_header = getattr(msg, 'reply_to', None)
            msg_topic = None
            if reply_header:
                msg_topic = getattr(reply_header, 'reply_to_top_id', None) or getattr(reply_header, 'reply_to_msg_id', None)
            if msg_topic != src_topic_id:
                return # Ignore messages from other topics
        
        # --- 2. User Whitelist Filtering ---
        sender = await event.get_sender()
        s_uname = getattr(sender, 'username', '') or ''
        s_first = getattr(sender, 'first_name', '') or ''
        s_last = getattr(sender, 'last_name', '') or ''
        s_id = str(getattr(sender, 'id', ''))
        
        allowed_users = route_extra.get("allowed_users", [])
        if allowed_users and s_uname.lower() not in allowed_users and s_id not in allowed_users:
            return 

        # --- 3. Text vs Media Toggles ---
        if route_extra.get("ignore_media", False): msg.media = None
        raw_text = "" if route_extra.get("ignore_text", False) else (msg.message or "").strip()

        if msg.date.timestamp() < start_time: return

        if keywords and not any(kw.lower() in raw_text.lower() for kw in keywords): return
        if pattern and not pattern.search(raw_text): return
        blacklist = route_extra.get("blacklist", [])
        if blacklist and any(bl.lower() in raw_text.lower() for bl in blacklist): return 

        now = time.time()
        key = (chat_id, raw_text)
        if key in recent_posts: return
        recent_posts[key] = now
        
        delay_sec = route_extra.get("delay", 0)
        if delay_sec > 0: await asyncio.sleep(delay_sec)

        # --- 4. Find & Replace Text ---
        replacements = route_extra.get("replacements", {})
        for old_txt, new_txt in replacements.items():
            raw_text = raw_text.replace(old_txt, new_txt)

        # --- 5. Dynamic Placeholders & Monospace Migration ---
        def apply_placeholders(t):
            if not t: return t
            t = re.sub(
                r'\[\[Message\.Sender\.(Username|Id|FirstName|LastName)\]\]',
                lambda m: f"[user.{m.group(1).lower().replace('firstname', 'first_name').replace('lastname', 'last_name')}]",
                t,
                flags=re.IGNORECASE
            )
            
            t = t.replace("[user.username]", f"@{s_uname}" if s_uname else "")
            t = t.replace("[user.id]", s_id)
            t = t.replace("[user.first_name]", s_first)
            t = t.replace("[user.last_name]", s_last)
            
            t = re.sub(r'\[user\.username\s*\|\s*user\.id\]', f"@{s_uname}" if s_uname else s_id, t)
            t = t.replace("[mono]", "<code>").replace("[/mono]", "</code>")
            return t

        raw_text = apply_placeholders(raw_text)
        begin_text = apply_placeholders(route_extra.get("begin_text", ""))
        end_text = apply_placeholders(route_extra.get("end_text", ""))
        
        final_text = f"{begin_text}{raw_text}{end_text}".strip()
        link_preview = not route_extra.get("disable_preview", True)
        is_native_forward = route_extra.get("native_forward", False)

        if not final_text and not msg.media:
            return

        for bot in bots:
            target_dest, dest_topic = parse_target_and_topic(bot)
            sent = None
            try:
                media = msg.media
                if hasattr(media, 'webpage'): media = None

                if is_native_forward:
                    sent = await client.forward_messages(target_dest, msg, reply_to=dest_topic)
                else:
                    sent = await client.send_message(
                        target_dest, 
                        message=final_text if final_text else None, 
                        file=media, 
                        link_preview=link_preview,
                        reply_to=dest_topic
                    )

            except FloodWaitError as e:
                log.warning(f"FloodWait hit! Sleeping for {e.seconds}s...")
                await asyncio.sleep(e.seconds)
                if is_native_forward:
                    sent = await client.forward_messages(target_dest, msg, reply_to=dest_topic)
                else:
                    sent = await client.send_message(
                        target_dest, 
                        message=final_text if final_text else None, 
                        file=media, 
                        link_preview=link_preview,
                        reply_to=dest_topic
                    )

            except Exception as e:
                error_str = str(e).lower()
                
                # Detect dead/revoked session
                if "not registered" in error_str or "unauthorized" in error_str or "authkey" in error_str:
                    log.error(f"Auth error during forward for user {route_extra.get('owner')}: {e}")
                    if on_auth_error:
                        asyncio.create_task(on_auth_error(route_extra.get("owner")))
                    return

                if media and ("restricted" in error_str or "media_empty" in error_str or "not allowed" in error_str or "protected" in error_str):
                    # --- Restricted Channel Image Scraping ---
                    if getattr(msg, 'photo', None):
                        try:
                            image_bytes = await client.download_media(msg.photo, file=bytes)
                            sent = await client.send_file(
                                target_dest, 
                                file=image_bytes, 
                                caption=final_text if final_text else None,
                                reply_to=dest_topic
                            )
                        except Exception as img_e:
                            log.error(f"Image scrape failed: {img_e}")
                    else:
                        log.info("Skipped restricted heavy media. Only images are allowed.")
                        if final_text:
                            try:
                                sent = await client.send_message(
                                    target_dest, 
                                    message=final_text, 
                                    link_preview=link_preview, 
                                    reply_to=dest_topic
                                )
                            except Exception: 
                                pass
                else: 
                    log.error(f"Send Error to {bot}: {e}")
            
            if sent:
                map_key = (chat_id, msg.id)
                existing = message_map.get(map_key, [])
                existing.append((bot, sent.id))
                message_map[map_key] = existing

    return handler

# -----------------------------
# Route Management
# -----------------------------
def _remove_handlers_for_user(target_uid):
    for registry in (active_handlers, active_edit_handlers, active_delete_handlers):
        for unique_key in list(registry.keys()):
            uid = unique_key[0] 
            if target_uid is None or uid == target_uid:
                client = clients_per_user.get(uid)
                if client:
                    try:
                        client.remove_event_handler(registry[unique_key])
                    except Exception:
                        pass
                registry.pop(unique_key, None)
                if registry is active_handlers:
                    route_caches.pop(unique_key, None)

async def setup_routes_for_user(routes_dict, api_id, api_hash, target_uid=None, on_auth_error=None):
    _remove_handlers_for_user(target_uid)

    for src_str, route_list in routes_dict.items():
        for idx, route in enumerate(route_list):
            uid = route["owner"]
            if target_uid is not None and uid != target_uid:
                continue 

            client = clients_per_user.get(uid)
            if not client or not client.is_connected():
                try:
                    client = await start_client_background(uid, api_id, api_hash)
                except Exception as e:
                    err_msg = str(e).lower()
                    log.error(f"Failed to start client for user {uid}: {e}")
                    if "not registered" in err_msg or "unauthorized" in err_msg or "authkey" in err_msg:
                        if on_auth_error:
                            asyncio.create_task(on_auth_error(uid))
                    continue 

            # Separate source chat from source topic if present
            base_src_str, src_topic_id = parse_target_and_topic(src_str)

            try:
                src_entity = await resolve_entity(client, base_src_str)
            except Exception as e:
                err_msg = str(e).lower()
                if "not registered" in err_msg or "unauthorized" in err_msg or "authkey" in err_msg:
                    if on_auth_error:
                        asyncio.create_task(on_auth_error(uid))
                continue

            if not src_entity:
                log.warning(f"Could not resolve source: {src_str} for user {uid}")
                continue

            bots = route["to"] if isinstance(route["to"], list) else [route["to"]]
            keywords = route.get("keywords", [])
            pattern_str = route.get("pattern", "")
            pattern = re.compile(pattern_str, re.IGNORECASE) if pattern_str else None
            
            recent_posts = TTLCache(maxsize=5000, ttl=route.get("cooldown", DEFAULT_COOLDOWN))
            
            unique_key = (uid, f"{src_str}_{idx}")
            route_caches[unique_key] = recent_posts

            route_extra = {
                "blacklist": route.get("blacklist", []),
                "delay": route.get("delay", 0),
                "begin_text": route.get("begin_text", ""),
                "end_text": route.get("end_text", ""),
                "owner": uid,
                "route_name": route.get("route_name", "Unknown Route"), 
                "replacements": route.get("replacements", {}),
                "allowed_users": route.get("allowed_users", []),
                "ignore_text": route.get("ignore_text", False),
                "ignore_media": route.get("ignore_media", False),
                "native_forward": route.get("native_forward", False),
                "disable_preview": route.get("disable_preview", True),
                "src_topic_id": src_topic_id,
            }

            handler_fn = make_handler(client, bots, keywords, pattern, recent_posts, route_extra, on_auth_error=on_auth_error)
            client.add_event_handler(handler_fn, events.NewMessage(chats=src_entity))
            active_handlers[unique_key] = handler_fn
            log.info(f"✅ User {uid}: Listening to {src_str} → {', '.join(map(str, bots))}")

            if route.get("auto_update", False):
                async def edit_handler(event):
                    if not GLOBAL_FORWARDING_ENABLED: return
                    key = (event.chat_id, event.id)
                    dests = message_map.get(key)
                    if not dests: return

                    new_text = (event.message.message or "").strip()
                    for dest_chat, dest_msg_id in list(dests):
                        try:
                            target_dest, _ = parse_target_and_topic(dest_chat)
                            await client.edit_message(target_dest, dest_msg_id, new_text)
                        except Exception:
                            pass 
                
                client.add_event_handler(edit_handler, events.MessageEdited(chats=src_entity))
                active_edit_handlers[unique_key] = edit_handler
                log.info(f"🔁 User {uid}: Edit-updates enabled for {src_str}")

                async def delete_handler(event):
                    for deleted_id in event.deleted_ids:
                        key = (event.chat_id, deleted_id)
                        dests = message_map.pop(key, None)
                        if dests:
                            for dest_chat, dest_msg_id in dests:
                                try: 
                                    target_dest, _ = parse_target_and_topic(dest_chat)
                                    await client.delete_messages(target_dest, [dest_msg_id])
                                except Exception: 
                                    pass

                client.add_event_handler(delete_handler, events.MessageDeleted(chats=src_entity))
                active_delete_handlers[unique_key] = delete_handler
                log.info(f"🗑️ User {uid}: Delete-sync enabled for {src_str}")
