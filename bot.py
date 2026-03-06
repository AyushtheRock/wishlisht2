"""
SHEIN Wishlist Stock Alert Bot — FINAL VERSION
===============================================

ALERT BEHAVIOUR (exactly as requested):
  • Each check cycle: all newly restocked sizes of the SAME product
    are grouped into ONE message  →  "M, L available"
  • If in a LATER cycle a NEW size of that same product restocks,
    a FRESH message is sent for just those new sizes  →  "XL available"
  • 50 different products restock  →  50 separate messages, no cap
  • A size goes OOS again then comes back  →  alerts again (reset on OOS)

SESSION RESILIENCE:
  • Single 401/403 is retried up to AUTH_FAIL_TOLERANCE=3 times
    (Shein throws transient 403s even with valid cookies)
  • Added x-requested-with, origin, sec-fetch-* headers
  • Partial wishlist used if mid-pagination auth error occurs

OTHER FIXES:
  • Caption capped at 1024 chars (Telegram photo limit) — graceful
    fallback to text message if photo caption would be truncated badly
  • /instock shows all available items with buy links, auto-splits
  • Keepalive URL double-slash fix
  • Global menu button set in post_init
"""

import os
import sys
import asyncio
import time
import json
import threading
import re
import random
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, BotCommand, BotCommandScopeDefault, BotCommandScopeChat, MenuButtonCommands
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, ConversationHandler
)

# ─── COLORS ───────────────────────────────────────────────────────────────────
GREEN = "\033[92m"
RED   = "\033[91m"
CYAN  = "\033[96m"
RESET = "\033[0m"

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.environ["BOT_TOKEN"]
ADMIN_USER_ID  = int(os.environ["ADMIN_USER_ID"])
RENDER_URL     = os.environ.get("RENDER_URL", "").rstrip("/")
PORT           = int(os.environ.get("PORT", 8080))
BASE_URL       = "https://www.sheinindia.in"
WISHLIST_API      = BASE_URL + "/api/wishlist/getwishlist"
ADD_WISHLIST_API  = BASE_URL + "/api/wishlist/addProductToWishlist"
PRODUCT_API       = BASE_URL + "/api/p/fetchProducts/{code}?SearchExperimentFlag={{}}"
CHECK_INTERVAL    = 30        # seconds between checks
JITTER_RANGE      = 10        # ±seconds random jitter
MAX_BACKOFF       = 600       # max backoff seconds
AUTH_FAIL_TOLERANCE = 3       # consecutive auth failures before giving up
SESSIONS_FILE       = "sessions.json"  # persisted sessions across restarts

CAPTION_LIMIT = 1024   # Telegram photo caption hard limit
TEXT_LIMIT    = 4096   # Telegram text message hard limit

WAITING_FOR_COOKIES  = 1
WAITING_FOR_ADD_LINK = 2

# ─── HTTP SESSION ─────────────────────────────────────────────────────────────
_http = requests.Session()
_http.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=50, pool_maxsize=50, max_retries=1,
))
_http.mount("http://", requests.adapters.HTTPAdapter(
    pool_connections=10, pool_maxsize=10,
))

# ─── STATE ────────────────────────────────────────────────────────────────────
user_sessions: dict = {}
monitor_tasks: dict = {}

def _save_sessions():
    """Persist cookie strings to disk so sessions survive bot restarts."""
    try:
        data = {}
        for uid, s in user_sessions.items():
            cookie_str = s["headers"].get("cookie", "")
            if cookie_str:
                data[str(uid)] = {
                    "cookie":   cookie_str,
                    "username": s.get("username", str(uid)),
                }
        with open(SESSIONS_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"{RED}[sessions] save error: {e}{RESET}")

def _load_sessions() -> dict:
    """Load persisted sessions from disk."""
    try:
        with open(SESSIONS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"{RED}[sessions] load error: {e}{RESET}")
        return {}

# ─── HEALTH SERVER ────────────────────────────────────────────────────────────
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/health", "/"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, *args): pass

def _run_health_server():
    HTTPServer(("0.0.0.0", PORT), _HealthHandler).serve_forever()

# ─── KEEPALIVE ────────────────────────────────────────────────────────────────
def _keepalive_loop():
    if not RENDER_URL:
        print(f"{CYAN}[keepalive] RENDER_URL not set — skipping{RESET}")
        return
    url = RENDER_URL + "/health"
    print(f"{GREEN}[keepalive] Will ping {url} every 3 min{RESET}")
    while True:
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "WishlistBot-Keepalive/1.0"})
            print(f"{GREEN}[keepalive] {r.status_code}{RESET}")
        except Exception as e:
            print(f"{RED}[keepalive] failed: {e}{RESET}")
        time.sleep(180)

# ─── COOKIE HELPERS ───────────────────────────────────────────────────────────
def _parse_cookies(text: str) -> str:
    text = text.strip()
    if text.startswith("["):
        try:
            items = json.loads(text)
            return "; ".join(
                f"{i['name']}={i['value']}" for i in items
                if "name" in i and "value" in i
            )
        except Exception:
            pass
    if "=" in text and "\n" not in text:
        return text
    pairs = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            pairs.append(f"{parts[5]}={parts[6]}")
        elif "=" in line and not line.startswith("http"):
            pairs.append(line)
    return "; ".join(pairs) if pairs else text

def _build_headers(cookie_str: str) -> dict:
    return {
        "cookie":           cookie_str,
        "user-agent":       "Mozilla/5.0 (Linux; Android 13; SM-G981B) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/145.0.0.0 Mobile Safari/537.36",
        "accept":           "application/json, text/plain, */*",
        "accept-language":  "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
        "accept-encoding":  "gzip, deflate, br",
        "content-type":     "application/json",
        "x-client-type":    "STOS",
        "x-tenant-id":      "SHEIN",
        "x-requested-with": "XMLHttpRequest",
        "origin":           "https://www.sheinindia.in",
        "referer":          "https://www.sheinindia.in/wishlist",
        "sec-fetch-dest":   "empty",
        "sec-fetch-mode":   "cors",
        "sec-fetch-site":   "same-origin",
    }

def _validate_cookies(c: str) -> bool:
    if not c or "=" not in c or len(c) < 20:
        return False
    session_keys = ["abt_medusa", "cookieId", "memberId", "sessionId",
                    "shein_sbn", "_shein", "acSite", "sheinCookieId"]
    if not any(k.lower() in c.lower() for k in session_keys):
        print(f"{CYAN}[cookies] Warning: no known session key found — proceeding anyway{RESET}")
    return True

# ─── WISHLIST FETCHING ────────────────────────────────────────────────────────
async def _fetch_page_throttled(headers: dict, page: int):
    """Async wrapper for fetching a wishlist page."""
    return await asyncio.to_thread(_fetch_page, headers, page)

def _fetch_page(headers: dict, page: int):
    try:
        r = _http.get(WISHLIST_API, params={
            "currentPage": page, "pageSize": 10,
            "store": "shein", "tagV2Enabled": "true", "tagExperiment": "A",
        }, headers=headers, timeout=20)
        if r.status_code == 200:
            return r.json(), 200
        return {}, r.status_code
    except Exception as e:
        print(f"{RED}[fetch] network error page={page}: {e}{RESET}")
        return {}, None

def _fetch_full_wishlist(headers: dict):
    """
    Returns one of:
      ("AUTH_FAILED", None)          — confirmed auth failure
      ("RATE_LIMITED", None)         — HTTP 429
      (None, None)                   — transient network/parse error
      (snapshot_dict, products_dict) — success

    snapshot_dict : { variant_code: "inStock" | "outOfStock" }
    products_dict : { variant_code: { name, size, price, image_url, product_url } }
    """
    first, status = _fetch_page(headers, 1)

    if status == 429:
        return "RATE_LIMITED", None
    if status in (401, 403):
        print(f"{RED}[fetch] HTTP {status} on page 1{RESET}")
        return "AUTH_FAILED", None
    if status is None or not first or "products" not in first:
        return None, None

    total_pages = first.get("pagination", {}).get("totalPages", 1)
    all_pages   = [first]

    if total_pages > 1:
        def _fetch_with_retry(page):
            for attempt in range(1, 4):
                p, st = _fetch_page(headers, page)
                if st is not None:
                    return page, p, st
                wait = attempt * 5
                print(f"{CYAN}[fetch] page={page} timeout attempt={attempt}/3 — retrying in {wait}s{RESET}")
                time.sleep(wait)
            return page, {}, None

        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_fetch_with_retry, pg): pg for pg in range(2, total_pages + 1)}
            results = {}
            for future in as_completed(futures):
                pg, p, st = future.result()
                results[pg] = (p, st)

        for pg in range(2, total_pages + 1):
            p, st = results.get(pg, ({}, None))
            if st in (401, 403):
                print(f"{RED}[fetch] AUTH on page {pg} — using partial results{RESET}")
                break
            if st is None:
                print(f"{RED}[fetch] page={pg} failed after 3 retries — skipping{RESET}")
                continue
            if p and "products" in p:
                all_pages.append(p)

    snapshot = {}
    products = {}
    for page_data in all_pages:
        for product in page_data.get("products", []):
            name        = product.get("name", "Unknown")
            product_url = BASE_URL + product.get("url", "")
            price       = product.get("price", {}).get("formattedValue", "")
            images      = product.get("images", [])
            image_url   = images[0]["url"] if images else None
            for variant in product.get("variantOptions", []):
                code    = variant.get("code", "")
                vstatus = variant.get("stock", {}).get("stockLevelStatus", "outOfStock")
                size    = "?"
                for q in variant.get("variantOptionQualifiers", []):
                    if q.get("qualifier") == "size":
                        size = q.get("value", "?")
                        break
                snapshot[code] = vstatus
                products[code] = {
                    "name":        name,
                    "size":        size,
                    "price":       price,
                    "image_url":   image_url,
                    "product_url": product_url,
                }
    return snapshot, products

def _download_image(url: str):
    try:
        r = _http.get(url, timeout=10)
        return r.content if r.status_code == 200 else None
    except Exception:
        return None

# ─── ALERT SENDER ─────────────────────────────────────────────────────────────
async def _send_stock_alert(bot, uid: int, product_name: str, sizes: list, info: dict):
    """
    Send ONE alert for a product, showing all sizes that restocked THIS cycle.
    Handles Telegram's caption limit gracefully.
    """
    price       = info.get("price", "")
    image_url   = info.get("image_url")
    product_url = info.get("product_url", "")
    count       = len(sizes)
    sizes_str   = ", ".join(sizes)

    body = (
        "🟢 *BACK IN STOCK!*\n\n"
        f"*{product_name}*\n"
        f"📏 Size{'s' if count > 1 else ''} now available ({count}): {sizes_str}\n"
        f"💰 Price: {price}\n\n"
        f"🛒 [Buy Now]({product_url})"
    )

    if len(body) > TEXT_LIMIT:
        body = body[:TEXT_LIMIT - 10] + "\n_..._"

    try:
        if image_url:
            img = await asyncio.to_thread(_download_image, image_url)
            if img:
                if len(body) <= CAPTION_LIMIT:
                    caption = body
                else:
                    buy_link  = f"\n\n🛒 [Buy Now]({product_url})"
                    max_body  = CAPTION_LIMIT - len(buy_link) - 10
                    caption   = body[:max_body] + "\n_..._" + buy_link

                try:
                    await bot.send_photo(
                        chat_id=uid,
                        photo=img,
                        caption=caption,
                        parse_mode="Markdown"
                    )
                    return
                except Exception as photo_err:
                    print(f"{RED}[alert] photo failed uid={uid}: {photo_err} — falling back to text{RESET}")

        await bot.send_message(
            uid,
            body,
            parse_mode="Markdown",
            disable_web_page_preview=False
        )

    except Exception as e:
        print(f"{RED}[alert] uid={uid} send error: {e}{RESET}")

# ─── MONITOR LOOP ─────────────────────────────────────────────────────────────
async def _monitor_loop(uid: int, app):
    session = user_sessions.get(uid)
    if not session:
        return
    print(f"{CYAN}[monitor] Started uid={uid} interval={CHECK_INTERVAL}s±{JITTER_RANGE}s{RESET}")

    consecutive_failures = 0
    auth_fail_streak     = 0
    backoff              = 0
    MAX_FAILURES         = 10

    await asyncio.sleep(random.randint(0, CHECK_INTERVAL))

    while True:
        sleep_time = CHECK_INTERVAL + random.randint(-JITTER_RANGE, JITTER_RANGE) + backoff
        sleep_time = max(sleep_time, 20)
        await asyncio.sleep(sleep_time)
        backoff = 0

        session = user_sessions.get(uid)
        if not session:
            print(f"{CYAN}[monitor] uid={uid} session removed — stopping{RESET}")
            break

        headers       = session["headers"]
        snapshot      = session["snapshot"]
        alerted_codes = session["alerted_codes"]

        try:
            new_snap, new_products = await asyncio.to_thread(_fetch_full_wishlist, headers)
        except Exception as e:
            consecutive_failures += 1
            print(f"{RED}[monitor] uid={uid} exception (#{consecutive_failures}): {e}{RESET}")
            continue

        # ── Auth failure handling ──────────────────────────────────────────────
        if new_snap == "AUTH_FAILED":
            auth_fail_streak += 1
            print(f"{RED}[monitor] uid={uid} AUTH streak={auth_fail_streak}/{AUTH_FAIL_TOLERANCE}{RESET}")
            if auth_fail_streak < AUTH_FAIL_TOLERANCE:
                backoff = min(60 * auth_fail_streak, 300)
                print(f"{CYAN}[monitor] uid={uid} transient — backing off {backoff}s{RESET}")
                continue
            print(f"{RED}[monitor] uid={uid} AUTH confirmed expired — stopping{RESET}")
            try:
                await app.bot.send_message(
                    uid,
                    "⚠️ *Your Shein session has expired.*\n\n"
                    "Confirmed after multiple attempts — cookies are no longer valid.\n\n"
                    "Please use /start to upload fresh cookies.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            user_sessions.pop(uid, None)
            _save_sessions()
            break

        elif new_snap == "RATE_LIMITED":
            backoff = min(backoff * 2 + 60, MAX_BACKOFF)
            print(f"{RED}[monitor] uid={uid} rate limited — backoff {backoff}s{RESET}")
            continue

        elif new_snap is None:
            auth_fail_streak = 0
            consecutive_failures += 1
            print(f"{RED}[monitor] uid={uid} temp failure #{consecutive_failures}{RESET}")
            if consecutive_failures >= MAX_FAILURES:
                print(f"{RED}[monitor] uid={uid} {MAX_FAILURES} failures — stopping{RESET}")
                try:
                    await app.bot.send_message(
                        uid,
                        f"⚠️ Could not reach Shein for ~{MAX_FAILURES * CHECK_INTERVAL // 60} min.\n"
                        f"Monitoring stopped. Use /restart to resume."
                    )
                except Exception:
                    pass
                user_sessions.pop(uid, None)
                _save_sessions()
                break
            continue

        else:
            consecutive_failures = 0
            auth_fail_streak     = 0
            backoff              = 0

        # ── Stock change detection ─────────────────────────────────────────────
        # Step 1: clear alerted flag for any size that went back OOS
        for code, new_status in new_snap.items():
            if new_status == "outOfStock" and code in alerted_codes:
                alerted_codes.discard(code)

        # Step 2: collect all newly restocked sizes, grouped by product name
        newly_restocked: dict = {}

        for code, new_status in new_snap.items():
            old_status = snapshot.get(code, "outOfStock")
            if old_status == "outOfStock" and new_status == "inStock" and code not in alerted_codes:
                info = new_products.get(code, {})
                name = info.get("name", "Unknown")
                if name not in newly_restocked:
                    newly_restocked[name] = []
                newly_restocked[name].append((info.get("size", "?"), code, info))
                alerted_codes.add(code)

        # Step 3: send alerts — one message per product, no cap
        for product_name, size_entries in newly_restocked.items():
            sizes_list  = sorted([s for s, _, _ in size_entries])
            sample_info = size_entries[0][2]
            print(f"{GREEN}[alert] uid={uid} '{product_name}' → sizes {sizes_list}{RESET}")
            await _send_stock_alert(app.bot, uid, product_name, sizes_list, sample_info)

        # Step 4: update snapshot for next cycle
        session["snapshot"] = new_snap
        session["products"] = new_products

    monitor_tasks.pop(uid, None)
    print(f"{CYAN}[monitor] Stopped uid={uid}{RESET}")

# ─── COMMANDS ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in monitor_tasks and not monitor_tasks[uid].done():
        monitor_tasks[uid].cancel()
        monitor_tasks.pop(uid, None)
    await update.message.reply_text(
        "👋 *SHEIN Wishlist Stock Alert Bot*\n\n"
        "Send your Shein cookies file (.txt or .json) to begin.\n\n"
        "Export from *sheinindia.in* while logged in using Cookie-Editor → Export as JSON.\n\n"
        "📋 *Commands:*\n"
        "/status — Check monitoring status\n"
        "/instock — Show in-stock items\n"
        "/list — Show out-of-stock items\n"
        "/restart — Refresh wishlist\n"
        "/addnewproduct — Add product by code\n"
        "/stop — Stop monitoring\n"
        "/help — Help",
        parse_mode="Markdown"
    )
    return WAITING_FOR_COOKIES

async def receive_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    user = update.effective_user
    if not update.message.document:
        await update.message.reply_text(
            "❌ Please *upload a file*, not paste text.\n"
            "Supported: `.txt`, `.json`",
            parse_mode="Markdown"
        )
        return WAITING_FOR_COOKIES
    try:
        tg_file    = await context.bot.get_file(update.message.document.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        raw        = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        await update.message.reply_text("❌ File encoding error. Please save as UTF-8.")
        return WAITING_FOR_COOKIES
    except Exception as e:
        await update.message.reply_text(f"❌ Could not read file: {e}")
        return WAITING_FOR_COOKIES

    print(f"{CYAN}[cookies] uid={uid} file received size={len(raw)} chars{RESET}")
    cookie_str = _parse_cookies(raw)
    print(f"{CYAN}[cookies] uid={uid} parsed length={len(cookie_str)}{RESET}")

    if not _validate_cookies(cookie_str):
        await update.message.reply_text(
            "❌ Could not extract valid cookies.\n\n"
            "Export from sheinindia.in via Cookie-Editor and try again."
        )
        return WAITING_FOR_COOKIES

    headers = _build_headers(cookie_str)
    msg     = await update.message.reply_text("⏳ Fetching your wishlist, please wait...")
    snapshot, products = await asyncio.to_thread(_fetch_full_wishlist, headers)

    if snapshot == "AUTH_FAILED":
        await msg.edit_text(
            "❌ *Session rejected by Shein.*\n\n"
            "Cookies appear invalid or expired.\n\n"
            "💡 Export while logged in at *sheinindia.in* using Cookie-Editor → Export as JSON.",
            parse_mode="Markdown"
        )
        return WAITING_FOR_COOKIES

    if snapshot is None:
        await msg.edit_text("❌ Network error fetching wishlist. Please try again.")
        return WAITING_FOR_COOKIES

    total   = len(snapshot)
    oos     = sum(1 for s in snapshot.values() if s == "outOfStock")
    instock = total - oos

    if uid in monitor_tasks:
        monitor_tasks[uid].cancel()
        monitor_tasks.pop(uid, None)

    user_sessions[uid] = {
        "headers":       headers,
        "snapshot":      snapshot,
        "products":      products,
        "username":      user.username or user.first_name or str(uid),
        "alerted_codes": set(),
    }

    await msg.edit_text(
        "✅ *Wishlist loaded!*\n\n"
        f"📦 Variants tracked: {total}\n"
        f"✅ In stock: {instock}\n"
        f"❌ Out of stock: {oos}\n\n"
        f"🔔 Monitoring started! You'll get alerted the moment any size comes back.\n"
        f"⏱ Check interval: ~{CHECK_INTERVAL}s\n\n"
        f"💡 Use /instock to browse what's available right now.",
        parse_mode="Markdown"
    )
    task = asyncio.create_task(_monitor_loop(uid, context.application))
    monitor_tasks[uid] = task
    _save_sessions()
    print(f"{GREEN}[session] uid={uid} (@{user_sessions[uid]['username']}) started monitoring{RESET}")
    return ConversationHandler.END

async def receive_text_in_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📂 Please *upload* cookies as a file, not text.",
        parse_mode="Markdown"
    )
    return WAITING_FOR_COOKIES

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in monitor_tasks:
        monitor_tasks[uid].cancel()
        monitor_tasks.pop(uid, None)
    user_sessions.pop(uid, None)
    _save_sessions()
    await update.message.reply_text("🛑 Monitoring stopped. Use /start to begin again.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in user_sessions:
        await update.message.reply_text("❌ No active session. Use /start.")
        return
    session    = user_sessions[uid]
    snapshot   = session["snapshot"]
    alerted    = len(session.get("alerted_codes", set()))
    is_running = uid in monitor_tasks and not monitor_tasks[uid].done()
    total      = len(snapshot)
    oos        = sum(1 for s in snapshot.values() if s == "outOfStock")
    await update.message.reply_text(
        "📊 *Monitoring Status*\n\n"
        f"🔄 Active: {'Yes ✅' if is_running else 'No ❌'}\n"
        f"📦 Variants tracked: {total}\n"
        f"✅ In stock: {total - oos}\n"
        f"❌ Out of stock: {oos}\n"
        f"🔔 Sizes alerted (not yet OOS again): {alerted}\n"
        f"⏱ Check interval: ~{CHECK_INTERVAL}s",
        parse_mode="Markdown"
    )

async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in user_sessions:
        await update.message.reply_text("❌ No session. Use /start.")
        return
    if uid in monitor_tasks:
        monitor_tasks[uid].cancel()
        monitor_tasks.pop(uid, None)
    headers = user_sessions[uid]["headers"]
    msg     = await update.message.reply_text("⏳ Re-fetching wishlist...")
    snapshot, products = await asyncio.to_thread(_fetch_full_wishlist, headers)
    if snapshot is None or snapshot == "AUTH_FAILED":
        await msg.edit_text("❌ Session expired. Use /start to send new cookies.")
        user_sessions.pop(uid, None)
        return
    user_sessions[uid]["snapshot"]      = snapshot
    user_sessions[uid]["products"]      = products
    user_sessions[uid]["alerted_codes"] = set()
    oos     = sum(1 for s in snapshot.values() if s == "outOfStock")
    instock = len(snapshot) - oos
    await msg.edit_text(
        f"✅ *Wishlist refreshed!*\n\n"
        f"📦 Total variants: {len(snapshot)}\n"
        f"✅ In stock: {instock}\n"
        f"❌ Out of stock: {oos}\n\n"
        f"🔔 Monitoring restarted.\n"
        f"💡 Use /instock to browse available items.",
        parse_mode="Markdown"
    )
    task = asyncio.create_task(_monitor_loop(uid, context.application))
    monitor_tasks[uid] = task
    _save_sessions()

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all OUT OF STOCK items."""
    uid = update.effective_user.id
    if uid not in user_sessions:
        await update.message.reply_text("❌ No active session. Use /start.")
        return
    session  = user_sessions[uid]
    snapshot = session["snapshot"]
    products = session["products"]

    oos_items = {
        code: products[code]
        for code, status in snapshot.items()
        if status == "outOfStock" and code in products
    }
    if not oos_items:
        await update.message.reply_text("✅ All wishlist items are currently in stock!")
        return

    grouped = {}
    for code, info in oos_items.items():
        name = info["name"]
        if name not in grouped:
            grouped[name] = {"sizes": [], "price": info["price"], "url": info["product_url"]}
        grouped[name]["sizes"].append(info["size"])

    lines = [f"*❌ Out of Stock — {len(grouped)} product(s):*\n"]
    for name, data in grouped.items():
        sizes = ", ".join(sorted(data["sizes"]))
        lines.append(f"❌ *{name}*")
        lines.append(f"   Sizes: {sizes}")
        lines.append(f"   💰 {data['price']}")
        lines.append(f"   [View Product]({data['url']})\n")

    text = "\n".join(lines)
    if len(text) > TEXT_LIMIT:
        text = text[:TEXT_LIMIT - 20] + "\n\n_...and more_"
    await update.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)

async def cmd_instock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all IN STOCK items — one photo per product with sizes and buy link."""
    uid = update.effective_user.id
    if uid not in user_sessions:
        await update.message.reply_text("❌ No active session. Use /start.")
        return
    session  = user_sessions[uid]
    snapshot = session["snapshot"]
    products = session["products"]

    in_stock = {
        code: products[code]
        for code, status in snapshot.items()
        if status == "inStock" and code in products
    }

    if not in_stock:
        await update.message.reply_text(
            "😔 Nothing from your wishlist is in stock right now.\n\n"
            "I'll alert you as soon as something comes back!"
        )
        return

    grouped = {}
    for code, info in in_stock.items():
        name = info["name"]
        if name not in grouped:
            grouped[name] = {
                "sizes":     [],
                "price":     info["price"],
                "url":       info["product_url"],
                "image_url": info.get("image_url"),
            }
        grouped[name]["sizes"].append(info["size"])

    total_products = len(grouped)
    total_variants = len(in_stock)

    await update.message.reply_text(
        f"*✅ In Stock — {total_products} product(s), {total_variants} size(s)*",
        parse_mode="Markdown"
    )

    for name, data in grouped.items():
        sizes = ", ".join(sorted(data["sizes"]))
        count = len(data["sizes"])
        caption = (
            f"🟢 *{name}*\n\n"
            f"📏 Size{'s' if count > 1 else ''} available ({count}): {sizes}\n"
            f"💰 {data['price']}\n\n"
            f"🛒 [Buy Now]({data['url']})"
        )
        if len(caption) > CAPTION_LIMIT:
            buy_link = f"\n\n🛒 [Buy Now]({data['url']})"
            caption  = caption[:CAPTION_LIMIT - len(buy_link) - 10] + "\n_..._" + buy_link

        sent = False
        if data["image_url"]:
            img = await asyncio.to_thread(_download_image, data["image_url"])
            if img:
                try:
                    await update.message.reply_photo(
                        photo=img,
                        caption=caption,
                        parse_mode="Markdown"
                    )
                    sent = True
                except Exception as e:
                    print(f"{RED}[instock] photo failed for '{name}': {e}{RESET}")

        if not sent:
            text = caption
            if len(text) > TEXT_LIMIT:
                text = text[:TEXT_LIMIT - 10] + "\n_..._"
            await update.message.reply_text(
                text,
                parse_mode="Markdown",
                disable_web_page_preview=False
            )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *SHEIN Wishlist Bot — Help*\n\n"
        "*Commands:*\n"
        "/start — Upload cookies and start monitoring\n"
        "/status — Monitoring status\n"
        "/list — All out-of-stock items\n"
        "/instock — All in-stock items with buy links\n"
        "/restart — Refresh wishlist and restart\n"
        "/stop — Stop monitoring\n"
        "/addnewproduct — Add product by product code\n"
        "/help — This message\n\n"
        "*How alerts work:*\n"
        "• Bot checks every ~60 seconds\n"
        "• Same cycle: M + L restock together → *1 message* showing both\n"
        "• Later cycle: XL restocks → *new message* showing XL only\n"
        "• Size goes OOS then comes back → alerts again\n"
        "• No cap — 50 products restock = 50 alert messages\n\n"
        "*Adding Products:*\n"
        "• /addnewproduct → send the Product Code from the Shein app\n"
        "• Find it: Product page → More Information → Product Code\n"
        "• Example: `443337635_multi`\n"
        "• Use /restart after adding to monitor the new item",
        parse_mode="Markdown"
    )

# ─── ADD TO WISHLIST ──────────────────────────────────────────────────────────
def _parse_product_code(text: str) -> str:
    text = text.strip()
    m = re.match(r'^(\d{6,12}(?:_[a-zA-Z0-9]+)?)$', text)
    if m:
        return m.group(1)
    m = re.search(r'/p/(\d{6,12}(?:_[a-zA-Z0-9]+)?)', text)
    if m:
        return m.group(1)
    m = re.search(r'-p-(\d{6,12})', text)
    if m:
        return m.group(1)
    m = re.search(r'(\d{6,12})', text)
    if m:
        return m.group(1)
    return ""

def _resolve_onelink(url: str) -> str:
    from urllib.parse import urlparse, parse_qs, unquote
    try:
        r = _http.get(url, timeout=10, allow_redirects=True)
        final_url = r.url
    except Exception:
        return ""
    parsed = urlparse(final_url)
    params = parse_qs(parsed.query)
    dlv = params.get('deep_link_value', [None])[0]
    if not dlv:
        return ""
    dlv = unquote(dlv)
    m = re.search(r'/p/(\d{6,12}(?:_[a-zA-Z0-9]+)?)', dlv)
    return m.group(1) if m else ""

def _fetch_product_detail(product_code: str, headers: dict) -> dict:
    url = PRODUCT_API.format(code=product_code)
    product_headers = {**headers, "referer": "https://www.sheinindia.in/"}
    try:
        r = _http.get(url, headers=product_headers, timeout=15)
        print(f"{CYAN}[product_detail] status={r.status_code}{RESET}")
        if r.status_code == 200:
            raw = r.text.strip()
            if not raw:
                return {"ok": False, "error": "Empty response. Try refreshing cookies."}
            data     = r.json()
            prods    = []
            if isinstance(data, dict):
                prods = data.get("products", [])
                if not prods and data.get("code"):
                    prods = [data]
            elif isinstance(data, list):
                prods = data
            if not prods:
                return {"ok": True, "numeric_code": product_code, "name": product_code}
            numeric_base = product_code.split("_")[0]
            matched = None
            for p in prods:
                if p.get("tags", {}).get("optionCode", "") == product_code:
                    matched = p; break
                if p.get("code", "").startswith(numeric_base):
                    matched = p; break
            if matched is None:
                matched = prods[0]
            return {
                "ok": True,
                "numeric_code": matched.get("code", "") or product_code,
                "name": matched.get("name", product_code),
            }
        if r.status_code in (401, 403):
            return {"ok": False, "error": "Session expired. Use /start."}
        if r.status_code == 404:
            return {"ok": False, "error": f"Product `{product_code}` not found."}
        return {"ok": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        print(f"{RED}[product_detail] error: {e}{RESET}")
        return {"ok": False, "error": str(e)}

def _add_to_wishlist_api(product_code_post: str, headers: dict) -> dict:
    add_headers = {
        **headers,
        "content-type": "application/json",
        "referer": "https://www.sheinindia.in/wishlist",
    }
    payload = {"productCodePost": product_code_post, "isCloset": 1}
    for attempt in range(1, 4):
        try:
            r = _http.post(ADD_WISHLIST_API, json=payload, headers=add_headers, timeout=20)
            print(f"{CYAN}[add_wishlist] attempt={attempt} status={r.status_code} body={r.text[:200]}{RESET}")
            if r.status_code == 200:
                data = r.json()
                sc   = data.get("statusCode")
                msg  = data.get("status", {}).get("messageDescription", "")
                if sc == 0 or "wishlist" in msg.lower() or "saved" in msg.lower():
                    return {"ok": True, "msg": msg or "Added!"}
                return {"ok": False, "error": msg or f"statusCode={sc}"}
            if r.status_code in (401, 403):
                return {"ok": False, "error": "Session expired. Use /start."}
            if r.status_code == 429:
                time.sleep(5 * attempt)
                continue
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        except Exception as e:
            print(f"{RED}[add_wishlist] attempt={attempt} error: {e}{RESET}")
            if attempt < 3:
                time.sleep(3 * attempt)
            else:
                return {"ok": False, "error": "Shein server not responding. Please try again in a moment."}
    return {"ok": False, "error": "Failed after 3 attempts. Please try again."}

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in user_sessions:
        await update.message.reply_text("❌ No active session. Use /start first.")
        return ConversationHandler.END
    await update.message.reply_text(
        "➕ *Add Product to Wishlist*\n\n"
        "Send any of the following:\n\n"
        "1️⃣ *Product Code* — `443337635` or `443337635_navy`\n"
        "2️⃣ *Shein link* — `https://www.sheinindia.in/.../p/443337635_navy`\n"
        "3️⃣ *Onelink* — `https://onelink.me/...`\n\n"
        "💡 Find the link by tapping *Share* on any Shein product page.",
        parse_mode="Markdown"
    )
    return WAITING_FOR_ADD_LINK

async def receive_add_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = (update.message.text or "").strip()
    if uid not in user_sessions:
        await update.message.reply_text("❌ Session expired. Use /start.")
        return ConversationHandler.END
    headers = user_sessions[uid]["headers"]
    if 'onelink.me' in text:
        resolved = await asyncio.to_thread(_resolve_onelink, text)
        text = resolved if resolved else text
    product_code = _parse_product_code(text)
    if not product_code:
        await update.message.reply_text(
            "❌ Could not find a product code.\n\nExample: `443337635_multi`",
            parse_mode="Markdown"
        )
        return WAITING_FOR_ADD_LINK
    msg = await update.message.reply_text(f"⏳ Looking up `{product_code}`...", parse_mode="Markdown")
    detail = await asyncio.to_thread(_fetch_product_detail, product_code, headers)
    if detail.get("ok") and detail.get("numeric_code", "").isdigit() and len(detail["numeric_code"]) == 12:
        post_code = detail["numeric_code"]
    else:
        post_code = product_code.split("_")[0]
    await msg.edit_text(f"⏳ Adding `{product_code}` to wishlist...", parse_mode="Markdown")
    result = await asyncio.to_thread(_add_to_wishlist_api, post_code, headers)
    if not result["ok"]:
        result = await asyncio.to_thread(_add_to_wishlist_api, product_code, headers)
    if result["ok"]:
        await msg.edit_text(
            f"✅ *Added to Wishlist!*\n\n"
            f"Product: `{product_code}`\n\n"
            f"🔔 Use /restart to start monitoring this item.",
            parse_mode="Markdown"
        )
    else:
        err = result['error']
        tip = "💡 Try /start to refresh cookies." if "Session expired" in err else "💡 Please try again in a moment."
        await msg.edit_text(
            f"❌ *Failed to add*\nCode: `{product_code}`\n\n{err}\n\n{tip}",
            parse_mode="Markdown"
        )
    return ConversationHandler.END

# ─── ADMIN ────────────────────────────────────────────────────────────────────
async def cmd_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔ Admin only.")
        return
    active = len(user_sessions)
    tasks  = sum(1 for t in monitor_tasks.values() if not t.done())
    lines  = [
        f"  • @{d.get('username', uid)} (ID: {uid}) — "
        f"{sum(1 for s in d.get('snapshot', {}).values() if s == 'outOfStock')} OOS | "
        f"{len(d.get('alerted_codes', set()))} alerted"
        for uid, d in user_sessions.items()
    ] or ["  None"]
    await update.message.reply_text(
        f"📊 *Admin Stats*\n\n"
        f"👥 Active sessions: {active}\n"
        f"🔄 Running monitors: {tasks}\n\n"
        f"*Sessions:*\n" + "\n".join(lines),
        parse_mode="Markdown"
    )

# ─── BOT LIFECYCLE ────────────────────────────────────────────────────────────
async def _restore_sessions(application):
    """On startup, reload persisted sessions and restart monitoring."""
    saved = _load_sessions()
    if not saved:
        return
    print(f"{CYAN}[sessions] Restoring {len(saved)} session(s)...{RESET}")
    for uid_str, data in saved.items():
        uid        = int(uid_str)
        cookie_str = data.get("cookie", "")
        username   = data.get("username", uid_str)
        if not cookie_str:
            continue
        headers  = _build_headers(cookie_str)
        snapshot, products = await asyncio.to_thread(_fetch_full_wishlist, headers)
        if snapshot in ("AUTH_FAILED", "RATE_LIMITED", None):
            print(f"{RED}[sessions] uid={uid} restore failed ({snapshot}) — skipping{RESET}")
            try:
                await application.bot.send_message(
                    uid,
                    "⚠️ *Bot restarted* but your session could not be restored.\n\nPlease use /start to upload your cookies again.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            continue
        user_sessions[uid] = {
            "headers":       headers,
            "snapshot":      snapshot,
            "products":      products,
            "username":      username,
            "alerted_codes": set(),
        }
        task = asyncio.create_task(_monitor_loop(uid, application))
        monitor_tasks[uid] = task
        print(f"{GREEN}[sessions] uid={uid} (@{username}) restored and monitoring{RESET}")
        try:
            await application.bot.send_message(
                uid,
                "✅ *Bot restarted* — your session was restored automatically.\nMonitoring continues as normal! 🔔",
                parse_mode="Markdown"
            )
        except Exception:
            pass

async def post_init(application):
    public_cmds = [
        BotCommand("start",         "Upload cookies and start monitoring"),
        BotCommand("status",        "Check monitoring status"),
        BotCommand("list",          "Show out-of-stock items"),
        BotCommand("instock",       "Show in-stock items with buy links"),
        BotCommand("restart",       "Refresh wishlist and restart"),
        BotCommand("stop",          "Stop monitoring"),
        BotCommand("help",          "Show help"),
        BotCommand("addnewproduct", "Add product by product code"),
    ]
    admin_extra = [BotCommand("stats", "Show all active sessions (admin)")]

    await application.bot.set_my_commands(public_cmds, scope=BotCommandScopeDefault())
    try:
        await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        print(f"{GREEN}[menu] Global menu button set{RESET}")
    except Exception as e:
        print(f"{RED}[menu] Could not set menu button: {e}{RESET}")

    if ADMIN_USER_ID:
        try:
            await application.bot.set_my_commands(
                public_cmds + admin_extra,
                scope=BotCommandScopeChat(chat_id=ADMIN_USER_ID),
            )
            print(f"{GREEN}[menu] Admin commands set for uid={ADMIN_USER_ID}{RESET}")
        except Exception as e:
            print(f"{RED}[warn] Admin commands not set: {e}{RESET}")

    await _restore_sessions(application)

async def post_shutdown(application):
    print(f"{CYAN}[shutdown] Cancelling {len(monitor_tasks)} task(s)...{RESET}")
    for task in monitor_tasks.values():
        task.cancel()
    monitor_tasks.clear()

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print(f"{CYAN}Starting SHEIN Wishlist Stock Alert Bot (final)...{RESET}")

    threading.Thread(target=_run_health_server, daemon=True).start()
    print(f"{GREEN}[health] Server on port {PORT}{RESET}")
    threading.Thread(target=_keepalive_loop, daemon=True).start()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(False)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .build()
    )

    app.add_handler(CommandHandler("stop",          cmd_stop),        group=0)
    app.add_handler(CommandHandler("status",        cmd_status),      group=0)
    app.add_handler(CommandHandler("restart",       cmd_restart),     group=0)
    app.add_handler(CommandHandler("list",          cmd_list),        group=0)
    app.add_handler(CommandHandler("instock",       cmd_instock),     group=0)
    app.add_handler(CommandHandler("help",          cmd_help),        group=0)
    app.add_handler(CommandHandler("stats",         cmd_admin_stats), group=0)

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            WAITING_FOR_COOKIES: [
                MessageHandler(filters.Document.ALL, receive_cookies),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text_in_cookies),
            ],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        allow_reentry=True, per_user=True, per_chat=False, block=False,
    )
    add_conv = ConversationHandler(
        entry_points=[CommandHandler("addnewproduct", cmd_add)],
        states={
            WAITING_FOR_ADD_LINK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_add_link),
            ],
        },
        fallbacks=[CommandHandler("addnewproduct", cmd_add)],
        allow_reentry=True, per_user=True, per_chat=False, block=False,
    )
    app.add_handler(conv,     group=1)
    app.add_handler(add_conv, group=1)

    print(f"{GREEN}Bot running!{RESET}")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

# ─── ENTRY POINT ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        main()
    except KeyboardInterrupt:
        print(f"{CYAN}Stopped by user{RESET}")
        sys.exit(0)
    except Exception as e:
        print(f"{RED}Fatal: {e}{RESET}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
