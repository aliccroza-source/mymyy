import asyncio
import sqlite3
import random
import os
import re
import logging
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Chat, Channel
from telethon.errors import (
    FloodWaitError, SessionPasswordNeededError,
    PhoneCodeInvalidError, PhoneCodeExpiredError,
    ChannelPrivateError, ChatAdminRequiredError, UserBannedInChannelError
)

BOT_TOKEN = "8787120916:AAGEw6sDiXz_SMLsoa6OtyPqNd7gxYOOq_A"
API_ID = 36781759
API_HASH = "31a2abacece3f047a878d001aa3fbd95"

DB_PATH = "bot_data.db"
SESSIONS_DIR = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

SUPER_KEYWORDS = [
    "مقابل", "بيع", "شراء", "اسيا", "آسيا",
    "ماستر", "اثير", "أثير", "مميزة", "مميزه",
    "هدايه", "هداية", "ارقام", "أرقام", "مطلوب"
]
SUPER_NAME_KEYWORDS = ["سوبر", "super", "Super", "SUPER"]

(
    MAIN_MENU,
    ADD_PHONE, ADD_CODE, ADD_PASSWORD,
    SELECT_ACCOUNT,
    SEND_TEMPLATE,
    SELECT_TARGETS,
    SET_DELAY,
    CHOOSE_MODE,
    SUPER_SEND_CONTENT,
    SUPER_SET_DELAY,
    SUPER_CHOOSE_MODE,
    PAUSE_INPUT,
    PRESET_MENU,
    PRESET_ADD_NAME,
    PRESET_ADD_TYPE,
    PRESET_ADD_TARGETS,
    PRESET_ADD_CONTENT,
    PRESET_ADD_DELAY,
    PRESET_ADD_MODE,
    PRESET_VIEW,
    PRESET_RUN_SELECT_ACCOUNT,
) = range(21)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        telegram_id INTEGER UNIQUE,
        username TEXT,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY,
        user_id INTEGER,
        phone TEXT,
        session_string TEXT,
        name TEXT,
        username TEXT,
        tg_id INTEGER,
        status TEXT DEFAULT 'active',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS campaigns (
        id INTEGER PRIMARY KEY,
        user_id INTEGER,
        account_id INTEGER,
        targets TEXT,
        delay_seconds INTEGER,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY,
        campaign_id INTEGER,
        content TEXT,
        media_type TEXT,
        media_file_id TEXT,
        order_index INTEGER
    );
    CREATE TABLE IF NOT EXISTS stats (
        id INTEGER PRIMARY KEY,
        user_id INTEGER,
        sent_ok INTEGER DEFAULT 0,
        sent_fail INTEGER DEFAULT 0,
        last_date DATE DEFAULT CURRENT_DATE
    );
    CREATE TABLE IF NOT EXISTS running_jobs (
        id INTEGER PRIMARY KEY,
        chat_id INTEGER,
        user_id INTEGER,
        account_id INTEGER,
        status TEXT DEFAULT 'running',
        pause_until TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS presets (
        id INTEGER PRIMARY KEY,
        user_id INTEGER,
        name TEXT,
        type TEXT,
        targets TEXT,
        messages TEXT,
        delay INTEGER,
        mode TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.commit()
    conn.close()


def db_exec(query, params=(), fetch=False):
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(query, params)
    if fetch:
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    conn.commit()
    last_id = c.lastrowid
    conn.close()
    return last_id


def get_user(telegram_id):
    rows = db_exec("SELECT * FROM users WHERE telegram_id=?", (telegram_id,), fetch=True)
    if rows:
        return rows[0]
    uid = db_exec(
        "INSERT INTO users (telegram_id, username) VALUES (?,?)",
        (telegram_id, "")
    )
    return {"id": uid, "telegram_id": telegram_id}


def update_stats(user_id, success=True):
    rows = db_exec("SELECT * FROM stats WHERE user_id=? AND last_date=DATE('now')",
                   (user_id,), fetch=True)
    if not rows:
        db_exec("INSERT INTO stats (user_id, sent_ok, sent_fail) VALUES (?,?,?)",
                (user_id, 1 if success else 0, 0 if success else 1))
    else:
        col = "sent_ok" if success else "sent_fail"
        db_exec(f"UPDATE stats SET {col}={col}+1 WHERE user_id=? AND last_date=DATE('now')",
                (user_id,))


class AntiBan:
    async def wait_before_send(self, account_id, peer, base_delay):
        jitter = base_delay * random.uniform(0.8, 1.2)
        await asyncio.sleep(jitter)

    async def rest(self):
        await asyncio.sleep(random.uniform(300, 900))


anti_ban = AntiBan()

running_jobs = {}


async def check_pause(job_id: int):
    while True:
        job = running_jobs.get(job_id)
        if not job:
            return False
        if job["status"] == "stopped":
            return False
        if job["status"] == "paused":
            remaining = (job["pause_until"] - datetime.now()).total_seconds()
            if remaining > 0:
                await asyncio.sleep(min(remaining, 5))
                continue
            else:
                job["status"] = "running"
                return True
        return True


def is_valid_code_format(text: str) -> bool:
    pattern = r'^\d(\s+\d)+$'
    return bool(re.match(pattern, text.strip()))


def parse_code(text: str) -> str:
    return re.sub(r'\s+', '', text.strip())


def normalize_arabic(text: str) -> str:
    text = re.sub(r'[\u064B-\u0652]', '', text)
    text = text.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا')
    text = text.replace('ة', 'ه').replace('ى', 'ي')
    return text


def contains_keywords(text: str, keywords: List[str]) -> Optional[str]:
    if not text:
        return None
    normalized = normalize_arabic(text.lower())
    for kw in keywords:
        kw_norm = normalize_arabic(kw.lower())
        if kw_norm in normalized:
            return kw
    return None


def safe_name(name: str) -> str:
    return (name or "").strip()[:50] or "حساب"


def is_group(dialog) -> bool:
    try:
        entity = dialog.entity
        if isinstance(entity, Chat):
            return True
        if isinstance(entity, Channel):
            if entity.megagroup:
                return True
            return False
        return False
    except Exception:
        return False


class AccountManager:
    def __init__(self):
        self.active_clients = {}

    async def create_client(self, session_str: str = ""):
        return TelegramClient(StringSession(session_str), API_ID, API_HASH)

    async def get_client(self, account_id: int, session_str: str):
        if account_id in self.active_clients:
            client = self.active_clients[account_id]
            if client.is_connected():
                return client
        client = await self.create_client(session_str)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return None
        self.active_clients[account_id] = client
        return client

    async def close_all(self):
        for client in self.active_clients.values():
            try:
                await client.disconnect()
            except Exception:
                pass


account_mgr = AccountManager()


class SuperSearch:
    @staticmethod
    async def search_by_name(client: TelegramClient):
        results = []
        try:
            async for dialog in client.iter_dialogs():
                if not is_group(dialog):
                    continue
                name = dialog.name or ""
                if any(kw.lower() in name.lower() for kw in SUPER_NAME_KEYWORDS):
                    results.append({
                        "id": dialog.id,
                        "name": name,
                        "username": getattr(dialog.entity, 'username', None),
                        "type": "name",
                        "matched": "سوبر"
                    })
        except Exception as e:
            logging.error(f"search_by_name error: {e}")
        return results

    @staticmethod
    async def search_in_messages(client: TelegramClient,
                                 messages_to_check: int = 200):
        results = []
        try:
            async for dialog in client.iter_dialogs():
                if not is_group(dialog):
                    continue
                name = dialog.name or ""
                if any(kw.lower() in name.lower() for kw in SUPER_NAME_KEYWORDS):
                    continue
                try:
                    found_kw = None
                    async for msg in client.iter_messages(dialog.id, limit=messages_to_check):
                        if msg.text:
                            found_kw = contains_keywords(msg.text, SUPER_KEYWORDS)
                            if found_kw:
                                break
                        await asyncio.sleep(0.05)
                    if found_kw:
                        results.append({
                            "id": dialog.id,
                            "name": name,
                            "username": getattr(dialog.entity, 'username', None),
                            "type": "messages",
                            "matched": found_kw
                        })
                except (ChannelPrivateError, ChatAdminRequiredError, UserBannedInChannelError):
                    continue
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds + 5)
                except Exception:
                    continue
                await asyncio.sleep(random.uniform(0.3, 0.8))
        except Exception as e:
            logging.error(f"search_in_messages error: {e}")
        return results


class Publisher:
    @staticmethod
    async def publish_to_target(client: TelegramClient, target,
                                messages: List[Dict], delay: int,
                                account_id: int, user_id: int,
                                job_id: int = None, mode: str = "once"):
        sent = 0
        failed = 0

        peer = None
        try:
            peer = await client.get_entity(target)
        except Exception as e:
            logging.error(f"get_entity error for {target}: {e}")
            try:
                if isinstance(target, str) and target.startswith("https://t.me/"):
                    username = target.replace("https://t.me/", "").strip("/")
                    peer = await client.get_entity(username)
                elif isinstance(target, str) and target.startswith("@"):
                    peer = await client.get_entity(target)
                else:
                    peer = await client.get_entity(int(target))
            except Exception as e2:
                logging.error(f"second attempt failed: {e2}")
                return 0, len(messages)

        if peer is None:
            return 0, len(messages)

        try:
            await client.action(peer, 'typing')
        except Exception:
            pass

        async def send_round():
            nonlocal sent, failed
            total = len(messages)
            i = 0
            while i < total:
                if job_id is not None:
                    if not await check_pause(job_id):
                        return False

                batch = messages[i:i + 2]
                try:
                    await asyncio.sleep(random.uniform(0.5, 1.5))

                    if len(batch) == 2:
                        msg1, msg2 = batch
                        try:
                            if msg1["media_type"] == "text":
                                await client.send_message(peer, msg1["content"])
                            else:
                                await client.send_file(peer, msg1["media_file_id"],
                                                      caption=msg1["content"] or "")
                            sent += 1
                            update_stats(user_id, True)
                        except Exception as e:
                            failed += 1
                            update_stats(user_id, False)
                            logging.error(f"send msg1 error: {e}")

                        await asyncio.sleep(random.uniform(0.8, 2.0))

                        try:
                            if msg2["media_type"] == "text":
                                await client.send_message(peer, msg2["content"])
                            else:
                                await client.send_file(peer, msg2["media_file_id"],
                                                      caption=msg2["content"] or "")
                            sent += 1
                            update_stats(user_id, True)
                        except Exception as e:
                            failed += 1
                            update_stats(user_id, False)
                            logging.error(f"send msg2 error: {e}")
                    else:
                        msg1 = batch[0]
                        try:
                            if msg1["media_type"] == "text":
                                await client.send_message(peer, msg1["content"])
                            else:
                                await client.send_file(peer, msg1["media_file_id"],
                                                      caption=msg1["content"] or "")
                            sent += 1
                            update_stats(user_id, True)
                        except Exception as e:
                            failed += 1
                            update_stats(user_id, False)
                            logging.error(f"send msg error: {e}")

                    i += 2

                    if i < total:
                        wait_time = delay * random.uniform(0.8, 1.2)
                        await asyncio.sleep(wait_time)

                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds + 10)
                except Exception as e:
                    failed += len(batch)
                    update_stats(user_id, False)
                    logging.error(f"batch error: {e}")
                    i += 2
            return True

        if mode == "once":
            await send_round()
            return sent, failed
        else:
            while True:
                if job_id is not None:
                    if not await check_pause(job_id):
                        return sent, failed

                ok = await send_round()
                if not ok:
                    return sent, failed

                if job_id is not None:
                    if not await check_pause(job_id):
                        return sent, failed

                wait_time = delay * random.uniform(0.8, 1.2)
                logging.info(f"التأخير بين الجولات: {wait_time:.0f} ثانية")
                await asyncio.sleep(wait_time)


def main_menu_kb():
    rows = [
        [InlineKeyboardButton("➕ إضافة حساب", callback_data="add_account")],
        [InlineKeyboardButton("📂 حساباتي", callback_data="my_accounts")],
        [InlineKeyboardButton("🚀 بدء النشر", callback_data="start_publish")],
        [InlineKeyboardButton("⭐ سوبر", callback_data="super_mode")],
        [InlineKeyboardButton("📋 النماذج الجاهزة", callback_data="preset_menu")],
        [InlineKeyboardButton("📊 الإحصائيات", callback_data="stats")],
    ]
    return InlineKeyboardMarkup(rows)


def back_kb(callback="main_menu"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=callback)]])


def welcome_text():
    return (
        "🤖 بوت النشر التلقائي\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "مرحباً بك! 👋\n\n"
        "هذا البوت يمكنك من:\n"
        "➕ إضافة حسابات تيليكرام\n"
        "📢 النشر التلقائي في المجموعات\n"
        "⭐ البحث الذكي بكلمة سوبر\n"
        "📋 حفظ نماذج جاهزة\n"
        "🛡 نظام ذكي ضد الحظر\n"
        "📊 إحصائيات مفصّلة\n\n"
        "اختر من الأزرار أدناه: 👇"
    )


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_tg = update.effective_user
    get_user(user_tg.id)
    context.user_data.clear()
    text = welcome_text()
    kb = main_menu_kb()
    if update.message:
        await update.message.reply_text(text, reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, reply_markup=kb)
    return MAIN_MENU


async def s_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "(s)":
        return await start_cmd(update, context)


async def add_account_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["new_account"] = {}
    context.user_data["auth_client"] = None
    await q.edit_message_text(
        "📱 إضافة حساب جديد\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "أرسل رقم هاتفك بالصيغة الدولية:\n"
        "مثال: +9647701234567\n\n"
        "⚠️ الرقم يجب أن يكون مسجّلاً في تيليكرام.",
        reply_markup=back_kb()
    )
    return ADD_PHONE


async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not re.match(r'^\+\d{8,15}$', phone):
        await update.message.reply_text(
            "❌ صيغة الرقم غير صحيحة!\n"
            "أرسل الرقم هكذا: +9647701234567"
        )
        return ADD_PHONE
    msg = await update.message.reply_text("⏳ جاري إرسال كود التحقق...")
    try:
        client = await account_mgr.create_client()
        await client.connect()
        sent = await client.send_code_request(phone)
        context.user_data["new_account"]["phone"] = phone
        context.user_data["new_account"]["phone_code_hash"] = sent.phone_code_hash
        context.user_data["auth_client"] = client
        await msg.edit_text(
            "🔐 تم إرسال كود التحقق\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            "أرسل الكود بالصيغة التالية:\n"
            "0 0 0 0 0\n\n"
            "⚠️ مهم جداً:\n"
            "يجب أن يكون بين كل رقم ورقم فراغ\n"
            "وإلا لن يكتمل تسجيل الدخول!\n\n"
            "📌 مثال: إذا وصلك 12345 أرسله 1 2 3 4 5"
        )
        return ADD_CODE
    except Exception as e:
        await msg.edit_text(f"❌ خطأ: {e}", reply_markup=back_kb())
        return MAIN_MENU


async def finish_login(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        client: TelegramClient, phone: str):
    try:
        me = await client.get_me()
        session_str = client.session.save()
        user = get_user(update.effective_user.id)
        acc_id = db_exec(
            "INSERT INTO accounts (user_id, phone, session_string, name, username, tg_id) "
            "VALUES (?,?,?,?,?,?)",
            (user["id"], phone, session_str, safe_name(me.first_name),
             me.username or "", me.id)
        )
        account_mgr.active_clients[acc_id] = client
        context.user_data.pop("auth_client", None)
        context.user_data.pop("new_account", None)
        await update.message.reply_text(
            "✅ تم إضافة الحساب بنجاح!\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 الاسم: {me.first_name or 'بدون'}\n"
            f"📱 الرقم: {phone}\n"
            f"🆔 الآيدي: {me.id}\n"
            f"🔗 اليوزر: @{me.username or 'لا يوجد'}\n\n"
            "الحساب جاهز للنشر 🚀",
            reply_markup=back_kb()
        )
        return MAIN_MENU
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ في حفظ الحساب: {e}", reply_markup=back_kb())
        return MAIN_MENU


async def receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not is_valid_code_format(text):
        await update.message.reply_text(
            "❌ صيغة الكود خاطئة!\n\n"
            "يجب أن ترسل الكود بأرقام متباعدة بفراغات:\n"
            "مثال: 1 2 3 4 5\n\n"
            "أعد الإرسال:"
        )
        return ADD_CODE
    code = parse_code(text)
    phone = context.user_data["new_account"]["phone"]
    phone_code_hash = context.user_data["new_account"]["phone_code_hash"]
    client: TelegramClient = context.user_data["auth_client"]
    if not client:
        await update.message.reply_text("❌ انتهت الجلسة، أعد المحاولة.", reply_markup=back_kb())
        return MAIN_MENU
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        return await finish_login(update, context, client, phone)
    except SessionPasswordNeededError:
        context.user_data["new_account"]["phone"] = phone
        context.user_data["auth_client"] = client
        await update.message.reply_text(
            "🔒 الحساب محمي بكلمة مرور ثنائية\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            "أرسل الآن كلمة مرور التحقق الثنائي (2FA):\n\n"
            "💡 هي كلمة المرور التي تُدخلها عند تسجيل الدخول لتيليكرام.\n"
            "🔐 ستبقى سرية ولن تُحفظ.\n\n"
            "📌 اكتبها يدوياً ولا تنسخها لتفادي المسافات الزائدة."
        )
        return ADD_PASSWORD
    except PhoneCodeInvalidError:
        await update.message.reply_text("❌ الكود غير صحيح! أعد الإرسال:")
        return ADD_CODE
    except PhoneCodeExpiredError:
        await update.message.reply_text("❌ انتهت صلاحية الكود! أعد المحاولة.", reply_markup=back_kb())
        return MAIN_MENU
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ: {e}", reply_markup=back_kb())
        return MAIN_MENU


async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text
    client: TelegramClient = context.user_data.get("auth_client")
    phone = context.user_data.get("new_account", {}).get("phone")
    if not client or not phone:
        await update.message.reply_text("❌ انتهت الجلسة، أعد المحاولة.", reply_markup=back_kb())
        return MAIN_MENU
    try:
        await update.message.delete()
    except Exception:
        pass
    try:
        await client.sign_in(password=password)
        return await finish_login(update, context, client, phone)
    except SessionPasswordNeededError:
        await update.message.reply_text(
            "❌ كلمة المرور لم تُقبل من تيليكرام.\n\n"
            "أسباب محتملة:\n"
            "1️⃣ كلمة المرور غير صحيحة\n"
            "2️⃣ مكتبة cryptg غير مثبتة\n"
            "   نفّذ: pip install cryptg\n"
            "3️⃣ أرسل الكلمة مرة أخرى بدون نسخ:"
        )
        return ADD_PASSWORD
    except Exception as e:
        err = str(e)
        if "invalid" in err.lower() or "password" in err.lower():
            await update.message.reply_text(
                "❌ كلمة المرور غير صحيحة\n\n"
                "تأكد من:\n"
                "• كتابتها يدوياً بدون نسخ ولصق\n"
                "• عدم وجود مسافات زائدة\n"
                "• أنها كلمة مرور 2FA الصحيحة\n\n"
                "📌 إذا كنت متأكداً منها، ثبّت مكتبة cryptg:\n"
                "pip install cryptg\n\n"
                "أعد الإرسال:"
            )
        else:
            await update.message.reply_text(f"❌ خطأ: {err}\n\nأعد الإرسال:")
        return ADD_PASSWORD


async def my_accounts_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = get_user(q.from_user.id)
    accounts = db_exec("SELECT * FROM accounts WHERE user_id=? ORDER BY id DESC",
                      (user["id"],), fetch=True)
    if not accounts:
        await q.edit_message_text(
            "📂 حساباتي\n\n"
            "لا توجد حسابات مضافة حتى الآن.\n"
            "اضغط ➕ لإضافة حساب جديد.",
            reply_markup=back_kb()
        )
        return MAIN_MENU
    text = f"📂 حساباتك ({len(accounts)})\n━━━━━━━━━━━━━━━━━━━\n\n"
    rows = []
    for i, acc in enumerate(accounts, 1):
        status_emoji = {"active": "🟢", "cooling": "🟡", "banned": "🔴"}.get(acc["status"], "⚪")
        text += f"{i}. {status_emoji} {acc['name']}\n   📱 {acc['phone']}\n\n"
        rows.append([InlineKeyboardButton(
            f"🗑 حذف {acc['name']}",
            callback_data=f"del_acc_{acc['id']}"
        )])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
    return MAIN_MENU


async def delete_account_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("🗑 تم الحذف")
    acc_id = int(q.data.split("_")[-1])
    user = get_user(q.from_user.id)
    db_exec("DELETE FROM accounts WHERE id=? AND user_id=?", (acc_id, user["id"]))
    if acc_id in account_mgr.active_clients:
        try:
            await account_mgr.active_clients[acc_id].disconnect()
        except Exception:
            pass
        del account_mgr.active_clients[acc_id]
    await my_accounts_cb(update, context)


async def start_publish_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = get_user(q.from_user.id)
    accounts = db_exec(
        "SELECT * FROM accounts WHERE user_id=? AND status='active'",
        (user["id"],), fetch=True
    )
    if not accounts:
        await q.edit_message_text(
            "❌ لا توجد حسابات نشطة.\nأضف حساباً أولاً.",
            reply_markup=back_kb()
        )
        return MAIN_MENU
    rows = [[InlineKeyboardButton(f"👤 {a['name']}", callback_data=f"pub_acc_{a['id']}")]
            for a in accounts]
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])
    await q.edit_message_text(
        "🚀 بدء النشر\n\nاختر الحساب الذي تريد النشر منه:",
        reply_markup=InlineKeyboardMarkup(rows)
    )
    return SELECT_ACCOUNT


async def select_account_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    acc_id = int(q.data.split("_")[-1])
    context.user_data["pub_account_id"] = acc_id
    context.user_data["pub_messages"] = []
    context.user_data["pub_targets"] = []
    await q.edit_message_text(
        "📝 أرسل الكليشة (الرسالة) التي تريد نشرها\n\n"
        "يمكن أن تحتوي على:\n"
        "• نص فقط\n"
        "• صورة + نص\n"
        "• فيديو + نص\n"
        "• ملف + نص\n\n"
        "بعد كل رسالة سيتم إعلامك.",
        reply_markup=back_kb()
    )
    return SEND_TEMPLATE


async def receive_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = msg.text or msg.caption or ""
    if text.strip() == ".":
        if not context.user_data.get("pub_messages"):
            await msg.reply_text("❌ لم ترسل أي رسالة بعد!")
            return SEND_TEMPLATE
        count = len(context.user_data["pub_messages"])
        await msg.reply_text(
            f"✅ تم حفظ {count} رسالة.\n\n"
            "🔗 أرسل رابط/آيدي الهدف:\n\n"
            "يدعم:\n"
            "• رابط عام أو خاص\n"
            "• @username\n"
            "• آيدي رقمي\n\n"
            "💡 يمكنك إرسال أكثر من هدف، افصل بينهم بسطر جديد."
        )
        return SELECT_TARGETS
    media_type = "text"
    media_file_id = None
    if msg.photo:
        media_type = "photo"
        media_file_id = msg.photo[-1].file_id
    elif msg.video:
        media_type = "video"
        media_file_id = msg.video.file_id
    elif msg.document:
        media_type = "document"
        media_file_id = msg.document.file_id
    elif msg.animation:
        media_type = "animation"
        media_file_id = msg.animation.file_id
    elif msg.audio:
        media_type = "audio"
        media_file_id = msg.audio.file_id
    context.user_data["pub_messages"].append({
        "content": text,
        "media_type": media_type,
        "media_file_id": media_file_id
    })
    n = len(context.user_data["pub_messages"])
    await msg.reply_text(
        f"✅ تم حفظ الرسالة رقم {n}\n\n"
        "هل تريد إضافة رسالة أخرى؟\n"
        "📤 أرسلها فقط\n"
        "⌨️ أو اكتب . لإنهاء اختيار الكليشة"
    )
    return SEND_TEMPLATE


async def receive_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    targets = [t.strip() for t in text.split("\n") if t.strip()]
    if not targets:
        await update.message.reply_text("❌ لم ترسل أي هدف صحيح!")
        return SELECT_TARGETS
    context.user_data["pub_targets"] = targets
    await update.message.reply_text(
        f"✅ تم حفظ {len(targets)} هدف.\n\n"
        "⏱ حدد الوقت بين كل رسالة ورسالة (بالثواني)\n\n"
        "مثال: 60 (يعني دقيقة بين كل رسالة)\n"
        "💡 كل ما زاد الوقت، قلّ احتمال الحظر."
    )
    return SET_DELAY


async def receive_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        delay = int(update.message.text.strip())
        if delay < 5:
            await update.message.reply_text("❌ الحد الأدنى 5 ثواني.")
            return SET_DELAY
        if delay > 3600:
            await update.message.reply_text("❌ الحد الأقصى 3600 ثانية (ساعة).")
            return SET_DELAY
    except ValueError:
        await update.message.reply_text("❌ أرسل رقماً صحيحاً.")
        return SET_DELAY
    context.user_data["pub_delay"] = delay

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("1️⃣ مرة واحدة", callback_data="mode_once")],
        [InlineKeyboardButton("🔄 تكرار لانهائي", callback_data="mode_loop")]
    ])
    await update.message.reply_text(
        "🔄 اختر نمط النشر:\n\n"
        "1️⃣ مرة واحدة: كل كروب يستلم الكليشة مرة\n"
        "🔄 تكرار لانهائي: يستمر بالإرسال حتى الإيقاف",
        reply_markup=kb
    )
    return CHOOSE_MODE


async def choose_mode_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mode = "once" if q.data == "mode_once" else "loop"
    context.user_data["pub_mode"] = mode

    n_msgs = len(context.user_data["pub_messages"])
    n_targets = len(context.user_data["pub_targets"])
    delay = context.user_data["pub_delay"]
    mode_str = "مرة واحدة" if mode == "once" else "🔄 تكرار لانهائي"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأكيد وبدء النشر", callback_data="confirm_publish")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="main_menu")]
    ])
    await q.edit_message_text(
        f"📋 تأكيد العملية\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📝 عدد الرسائل: {n_msgs}\n"
        f"🎯 عدد الأهداف: {n_targets}\n"
        f"⏱ التأخير: {delay} ثانية\n"
        f"🔄 النمط: {mode_str}\n"
        f"🛡 الحماية الذكية: مفعّلة\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"هل تريد البدء؟",
        reply_markup=kb
    )
    return CHOOSE_MODE


async def confirm_publish_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("🚀 جاري البدء...")
    user_id_tg = q.from_user.id
    user = get_user(user_id_tg)
    acc_id = context.user_data["pub_account_id"]
    messages = context.user_data["pub_messages"]
    targets = context.user_data["pub_targets"]
    delay = context.user_data["pub_delay"]
    mode = context.user_data.get("pub_mode", "once")

    acc = db_exec("SELECT * FROM accounts WHERE id=?", (acc_id,), fetch=True)
    if not acc:
        await q.edit_message_text("❌ الحساب غير موجود!", reply_markup=back_kb())
        return MAIN_MENU
    acc = acc[0]

    job_id = db_exec(
        "INSERT INTO running_jobs (chat_id, user_id, account_id) VALUES (?,?,?)",
        (q.message.chat_id, user["id"], acc_id)
    )
    running_jobs[job_id] = {"status": "running", "pause_until": None}

    control_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏸ إيقاف مؤقت", callback_data=f"pause_{job_id}")],
        [InlineKeyboardButton("⏹ إيقاف نهائي", callback_data=f"stop_{job_id}")]
    ])

    mode_str = "مرة واحدة" if mode == "once" else "🔄 تكرار لانهائي"

    await q.edit_message_text(
        f"🚀 بدأ النشر\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 الحساب: {acc['name']}\n"
        f"📝 الرسائل: {len(messages)}\n"
        f"🎯 الأهداف: {len(targets)}\n"
        f"⏱ التأخير: {delay} ثانية\n"
        f"🔄 النمط: {mode_str}\n"
        f"🆔 رقم المهمة: {job_id}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ جاري التنفيذ...",
        reply_markup=control_kb
    )
    asyncio.create_task(
        run_publish_task(q.message.chat_id, user["id"], acc, messages, targets, delay, mode, job_id)
    )
    return MAIN_MENU


async def run_publish_task(chat_id: int, user_id: int, acc: dict,
                            messages: List[Dict], targets: List[str],
                            delay: int, mode: str = "once", job_id: int = None):
    try:
        client = await account_mgr.get_client(acc["id"], acc["session_string"])
        if not client:
            await send_msg(chat_id, "❌ فشل الاتصال بالحساب!")
            if job_id in running_jobs:
                del running_jobs[job_id]
            return

        total_sent = 0
        total_failed = 0
        success_targets = 0
        failed_targets = 0

        await send_msg(
            chat_id,
            f"🚀 بدأ النشر في {len(targets)} هدف\n"
            f"⏳ جاري العمل..."
        )

        for idx, t in enumerate(targets, 1):
            if job_id is not None and job_id in running_jobs:
                if running_jobs[job_id]["status"] == "stopped":
                    await send_msg(
                        chat_id,
                        f"⏹ تم إيقاف النشر نهائياً\n"
                        f"✅ رسائل ناجحة: {total_sent}\n"
                        f"❌ رسائل فاشلة: {total_failed}"
                    )
                    del running_jobs[job_id]
                    return

            sent, failed = await Publisher.publish_to_target(
                client, t, messages, delay, acc["id"], user_id, job_id, mode
            )
            total_sent += sent
            total_failed += failed

            if sent > 0:
                success_targets += 1
            else:
                failed_targets += 1

            status_emoji = "✅" if sent > 0 else "❌"
            await send_msg(
                chat_id,
                f"{status_emoji} [{idx}/{len(targets)}] {t}\n"
                f"   ✔ {sent} | ✖ {failed}"
            )

        await send_msg(
            chat_id,
            f"🏁 اكتمل النشر\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 الأهداف الكلية: {len(targets)}\n"
            f"✅ أهداف نجحت: {success_targets}\n"
            f"🚫 أهداف فشلت: {failed_targets}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📨 رسائل ناجحة: {total_sent}\n"
            f"📭 رسائل فاشلة: {total_failed}\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )
        if job_id in running_jobs:
            del running_jobs[job_id]
    except Exception as e:
        await send_msg(chat_id, f"❌ خطأ في المهمة: {e}")
        if job_id in running_jobs:
            del running_jobs[job_id]


async def pause_job_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    job_id = int(q.data.split("_")[-1])
    if job_id not in running_jobs:
        await q.answer("❌ المهمة غير موجودة!", show_alert=True)
        return MAIN_MENU
    context.user_data["pause_job_id"] = job_id
    await q.answer()
    await q.edit_message_text(
        "⏸ إيقاف مؤقت\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "أرسل مدة الإيقاف بالثواني:\n"
        "مثال: 300 (يعني 5 دقائق)\n\n"
        "💡 بعد انتهاء المدة، سيستأنف النشر تلقائياً.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 رجوع", callback_data=f"back_to_job_{job_id}")]
        ])
    )
    return PAUSE_INPUT


async def receive_pause_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        seconds = int(update.message.text.strip())
        if seconds < 5:
            await update.message.reply_text("❌ الحد الأدنى 5 ثواني.")
            return PAUSE_INPUT
        if seconds > 86400:
            await update.message.reply_text("❌ الحد الأقصى 86400 ثانية (24 ساعة).")
            return PAUSE_INPUT
    except ValueError:
        await update.message.reply_text("❌ أرسل رقماً صحيحاً.")
        return PAUSE_INPUT

    job_id = context.user_data.get("pause_job_id")
    if not job_id or job_id not in running_jobs:
        await update.message.reply_text("❌ المهمة غير موجودة!")
        return MAIN_MENU

    running_jobs[job_id]["status"] = "paused"
    running_jobs[job_id]["pause_until"] = datetime.now() + timedelta(seconds=seconds)

    minutes = seconds // 60
    secs = seconds % 60
    time_str = f"{minutes} دقيقة و {secs} ثانية" if minutes else f"{secs} ثانية"

    control_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏸ إيقاف مؤقت", callback_data=f"pause_{job_id}")],
        [InlineKeyboardButton("⏹ إيقاف نهائي", callback_data=f"stop_{job_id}")]
    ])
    await update.message.reply_text(
        f"⏸ تم إيقاف النشر مؤقتاً\n"
        f"⏱ المدة: {time_str}\n"
        f"▶️ سيستأنف تلقائياً",
        reply_markup=control_kb
    )
    return MAIN_MENU


async def stop_job_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    job_id = int(q.data.split("_")[-1])
    if job_id not in running_jobs:
        await q.answer("❌ المهمة غير موجودة!", show_alert=True)
        return MAIN_MENU
    running_jobs[job_id]["status"] = "stopped"
    await q.answer("⏹ تم الإيقاف")
    await q.edit_message_text(
        "⏹ تم إيقاف النشر نهائياً\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "لن يتم إرسال أي رسائل أخرى.\n"
        "يمكنك بدء حملة جديدة من القائمة.",
        reply_markup=back_kb()
    )
    return MAIN_MENU


async def back_to_job_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    job_id = int(q.data.split("_")[-1])
    await q.answer()
    control_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏸ إيقاف مؤقت", callback_data=f"pause_{job_id}")],
        [InlineKeyboardButton("⏹ إيقاف نهائي", callback_data=f"stop_{job_id}")]
    ])
    await q.edit_message_text(
        f"🆔 رقم المهمة: {job_id}\n"
        f"⏳ جاري التنفيذ...",
        reply_markup=control_kb
    )
    return MAIN_MENU


async def send_msg(chat_id: int, text: str):
    try:
        await application.bot.send_message(chat_id, text)
    except Exception as e:
        logging.error(f"send_msg: {e}")


async def super_mode_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = get_user(q.from_user.id)
    accounts = db_exec(
        "SELECT * FROM accounts WHERE user_id=? AND status='active'",
        (user["id"],), fetch=True
    )
    if not accounts:
        await q.edit_message_text(
            "❌ تحتاج حساباً واحداً على الأقل.\nأضف حساباً أولاً.",
            reply_markup=back_kb()
        )
        return MAIN_MENU
    rows = [[InlineKeyboardButton(f"👤 {a['name']}", callback_data=f"super_acc_{a['id']}")]
            for a in accounts]
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])
    await q.edit_message_text(
        "⭐ ميزة سوبر\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "هذه الميزة تقوم بالبحث الذكي عن:\n"
        "• كل المجموعات باسم سوبر أو Super\n"
        "• المجموعات التي رسائلها تحتوي كلمات تجارية\n"
        "  (مقابل، بيع، شراء، اسيا، ماستر، أثير...)\n\n"
        "⚠️ تستثني: القنوات والمحادثات الخاصة\n\n"
        "اختر الحساب للبحث:",
        reply_markup=InlineKeyboardMarkup(rows)
    )
    return SELECT_ACCOUNT


async def super_select_account_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    acc_id = int(q.data.split("_")[-1])
    context.user_data["super_account_id"] = acc_id
    context.user_data["super_messages"] = []
    await q.edit_message_text(
        "⭐ سوبر\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "📤 أرسل الكليشة/الرابط الذي تريد نشره:",
        reply_markup=back_kb()
    )
    return SUPER_SEND_CONTENT


async def super_receive_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = msg.text or msg.caption or ""
    if text.strip() == ".":
        if not context.user_data.get("super_messages"):
            await msg.reply_text("❌ لم ترسل أي رسالة!")
            return SUPER_SEND_CONTENT
        await msg.reply_text(
            f"✅ تم حفظ {len(context.user_data['super_messages'])} رسالة.\n\n"
            "⏱ حدد الوقت بين الرسائل (بالثواني):\n"
            "مثال: 30"
        )
        return SUPER_SET_DELAY
    media_type = "text"
    media_file_id = None
    if msg.photo:
        media_type = "photo"
        media_file_id = msg.photo[-1].file_id
    elif msg.video:
        media_type = "video"
        media_file_id = msg.video.file_id
    elif msg.document:
        media_type = "document"
        media_file_id = msg.document.file_id
    context.user_data["super_messages"].append({
        "content": text,
        "media_type": media_type,
        "media_file_id": media_file_id
    })
    n = len(context.user_data["super_messages"])
    await msg.reply_text(
        f"✅ تم حفظ الرسالة رقم {n}\n\n"
        "أرسل المزيد أو اكتب . للانتهاء."
    )
    return SUPER_SEND_CONTENT


async def super_receive_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        delay = int(update.message.text.strip())
        if delay < 5:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ أرسل رقماً صحيحاً (5 أو أكثر).")
        return SUPER_SET_DELAY
    context.user_data["super_delay"] = delay
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("1️⃣ مرة واحدة", callback_data="super_mode_once")],
        [InlineKeyboardButton("🔄 تكرار لانهائي", callback_data="super_mode_loop")]
    ])
    await update.message.reply_text(
        "🔄 اختر نمط النشر:\n\n"
        "1️⃣ مرة واحدة: كل مجموعة تستلم الكليشة مرة\n"
        "🔄 تكرار لانهائي: يستمر بالإرسال حتى الإيقاف",
        reply_markup=kb
    )
    return SUPER_CHOOSE_MODE


async def super_choose_mode_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mode = "once" if q.data == "super_mode_once" else "loop"
    acc_id = context.user_data["super_account_id"]
    messages = context.user_data["super_messages"]
    delay = context.user_data["super_delay"]
    user_id_tg = q.from_user.id
    user = get_user(user_id_tg)
    acc = db_exec("SELECT * FROM accounts WHERE id=?", (acc_id,), fetch=True)
    if not acc:
        await q.edit_message_text("❌ الحساب غير موجود!", reply_markup=back_kb())
        return MAIN_MENU
    acc = acc[0]

    job_id = db_exec(
        "INSERT INTO running_jobs (chat_id, user_id, account_id) VALUES (?,?,?)",
        (q.message.chat_id, user["id"], acc_id)
    )
    running_jobs[job_id] = {"status": "running", "pause_until": None}

    control_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏸ إيقاف مؤقت", callback_data=f"pause_{job_id}")],
        [InlineKeyboardButton("⏹ إيقاف نهائي", callback_data=f"stop_{job_id}")]
    ])

    mode_str = "مرة واحدة" if mode == "once" else "🔄 تكرار لانهائي"

    await q.edit_message_text(
        f"⭐ بدأ النشر الذكي\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 الحساب: {acc['name']}\n"
        f"📝 الرسائل: {len(messages)}\n"
        f"⏱ التأخير: {delay} ثانية\n"
        f"🔄 النمط: {mode_str}\n"
        f"🆔 رقم المهمة: {job_id}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🔍 جاري البحث والتنفيذ...",
        reply_markup=control_kb
    )
    asyncio.create_task(
        run_super_task(q.message.chat_id, user["id"], acc, messages, delay, mode, job_id)
    )
    return MAIN_MENU


async def run_super_task(chat_id: int, user_id: int, acc: dict,
                          messages: List[Dict], delay: int,
                          mode: str = "once", job_id: int = None):
    try:
        client = await account_mgr.get_client(acc["id"], acc["session_string"])
        if not client:
            await send_msg(chat_id, "❌ فشل الاتصال بالحساب!")
            if job_id in running_jobs:
                del running_jobs[job_id]
            return

        await send_msg(chat_id, "🔍 المرحلة 1: البحث بالاسم...")
        name_results = await SuperSearch.search_by_name(client)
        await send_msg(chat_id, f"✅ باسم سوبر: {len(name_results)}")

        await send_msg(
            chat_id,
            "🔍 المرحلة 2: فحص الرسائل...\n"
            "⏳ (قد يستغرق وقتاً طويلاً)"
        )
        msg_results = await SuperSearch.search_in_messages(client)
        await send_msg(chat_id, f"✅ بناءً على الرسائل: {len(msg_results)}")

        all_targets = {}
        for r in name_results + msg_results:
            all_targets[r["id"]] = r
        targets = list(all_targets.values())

        if not targets:
            await send_msg(chat_id, "❌ لم يتم العثور على أي مجموعة!")
            if job_id in running_jobs:
                del running_jobs[job_id]
            return

        await send_msg(
            chat_id,
            f"📊 إجمالي المجموعات: {len(targets)}\n"
            f"📝 عدد الرسائل في الكليشة: {len(messages)}\n"
            f"🔄 النمط: {'مرة واحدة' if mode == 'once' else '🔄 تكرار لانهائي'}\n\n"
            f"⏳ جاري بدء النشر..."
        )

        total_sent = 0
        total_failed = 0
        success_targets = 0
        failed_targets = 0

        for i, target in enumerate(targets, 1):
            if job_id is not None and job_id in running_jobs:
                if running_jobs[job_id]["status"] == "stopped":
                    await send_msg(
                        chat_id,
                        f"⏹ تم إيقاف النشر نهائياً\n"
                        f"📨 رسائل ناجحة: {total_sent}\n"
                        f"📭 رسائل فاشلة: {total_failed}"
                    )
                    del running_jobs[job_id]
                    return

            try:
                peer_entity = await client.get_entity(target["id"])
                sent, failed = await Publisher.publish_to_target(
                    client, peer_entity, messages, delay, acc["id"], user_id, job_id, mode
                )
                total_sent += sent
                total_failed += failed

                if sent > 0:
                    success_targets += 1
                else:
                    failed_targets += 1

                if i % 5 == 0 or i == len(targets):
                    await send_msg(
                        chat_id,
                        f"📊 التقدم: {i}/{len(targets)}\n"
                        f"   ✅ نجحت: {success_targets}\n"
                        f"   🚫 فشلت: {failed_targets}"
                    )
            except Exception as e:
                logging.error(f"super target error: {e}")
                failed_targets += 1
                continue

        await send_msg(
            chat_id,
            f"🏁 اكتمل النشر\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 المجموعات الكلية: {len(targets)}\n"
            f"✅ مجموعات نجحت: {success_targets}\n"
            f"🚫 مجموعات فشلت: {failed_targets}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📨 رسائل ناجحة: {total_sent}\n"
            f"📭 رسائل فاشلة: {total_failed}\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )
        if job_id in running_jobs:
            del running_jobs[job_id]
    except Exception as e:
        await send_msg(chat_id, f"❌ خطأ: {e}")
        if job_id in running_jobs:
            del running_jobs[job_id]


async def stats_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = get_user(q.from_user.id)
    accounts_count = db_exec("SELECT COUNT(*) as c FROM accounts WHERE user_id=?",
                             (user["id"],), fetch=True)[0]["c"]
    today = db_exec("SELECT * FROM stats WHERE user_id=? AND last_date=DATE('now')",
                    (user["id"],), fetch=True)
    today_ok = today[0]["sent_ok"] if today else 0
    today_fail = today[0]["sent_fail"] if today else 0
    total = db_exec("SELECT SUM(sent_ok) as ok, SUM(sent_fail) as fail FROM stats WHERE user_id=?",
                    (user["id"],), fetch=True)[0]
    total_ok = total["ok"] or 0
    total_fail = total["fail"] or 0
    await q.edit_message_text(
        f"📊 إحصائياتك\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 الحسابات: {accounts_count}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📅 اليوم:\n"
        f"   ✅ نجح: {today_ok}\n"
        f"   ❌ فشل: {today_fail}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📈 الإجمالي:\n"
        f"   ✅ نجح: {total_ok}\n"
        f"   ❌ فشل: {total_fail}\n"
        f"━━━━━━━━━━━━━━━━━━━",
        reply_markup=back_kb()
    )
    return MAIN_MENU


async def preset_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = get_user(q.from_user.id)
    presets = db_exec("SELECT * FROM presets WHERE user_id=? ORDER BY id DESC",
                      (user["id"],), fetch=True)

    text = "📋 النماذج الجاهزة\n━━━━━━━━━━━━━━━━━━━\n\n"
    rows = []

    if presets:
        text += f"عدد النماذج: {len(presets)}\n\n"
        for i, p in enumerate(presets, 1):
            type_str = "⭐ سوبر" if p["type"] == "super" else "🎯 كروبات محددة"
            text += f"{i}. {p['name']} - {type_str}\n"
            rows.append([InlineKeyboardButton(f"📄 {p['name']}", callback_data=f"preset_view_{p['id']}")])
    else:
        text += "لا توجد نماذج محفوظة حتى الآن.\n"

    rows.append([InlineKeyboardButton("➕ إضافة نموذج", callback_data="preset_add")])
    rows.append([InlineKeyboardButton("🗑 مسح نموذج", callback_data="preset_delete_menu")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])

    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
    return PRESET_MENU


async def preset_add_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["preset_new"] = {}
    await q.edit_message_text(
        "➕ إضافة نموذج جديد\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "أرسل اسم النموذج (مثال: نموذج البيع اليومي):",
        reply_markup=back_kb("preset_menu")
    )
    return PRESET_ADD_NAME


async def preset_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()[:50]
    if not name:
        await update.message.reply_text("❌ اسم غير صحيح!")
        return PRESET_ADD_NAME
    context.user_data["preset_new"]["name"] = name
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ سوبر (بحث تلقائي)", callback_data="preset_type_super")],
        [InlineKeyboardButton("🎯 كروبات محددة", callback_data="preset_type_specific")]
    ])
    await update.message.reply_text(
        f"✅ الاسم: {name}\n\n"
        "اختر نوع النموذج:",
        reply_markup=kb
    )
    return PRESET_ADD_TYPE


async def preset_add_type_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "preset_type_super":
        context.user_data["preset_new"]["type"] = "super"
        context.user_data["preset_new"]["targets"] = []
        await q.edit_message_text(
            "⭐ نوع: سوبر\n\n"
            "📤 أرسل الكليشة/الرسائل التي تريد حفظها.\n"
            "بعد كل رسالة اضغط التالي، أو اكتب . للانتهاء:",
            reply_markup=back_kb("preset_menu")
        )
        return PRESET_ADD_CONTENT
    else:
        context.user_data["preset_new"]["type"] = "specific"
        await q.edit_message_text(
            "🎯 نوع: كروبات محددة\n\n"
            "🔗 أرسل الأهداف (رابط/آيدي/يوزر).\n"
            "يمكنك إرسال أكثر من هدف، افصل بينهم بسطر جديد:",
            reply_markup=back_kb("preset_menu")
        )
        return PRESET_ADD_TARGETS


async def preset_add_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    targets = [t.strip() for t in text.split("\n") if t.strip()]
    if not targets:
        await update.message.reply_text("❌ لم ترسل أي هدف!")
        return PRESET_ADD_TARGETS
    context.user_data["preset_new"]["targets"] = targets
    context.user_data["preset_new"]["messages"] = []
    await update.message.reply_text(
        f"✅ تم حفظ {len(targets)} هدف.\n\n"
        "📤 أرسل الكليشة/الرسائل.\n"
        "بعد كل رسالة اضغط التالي، أو اكتب . للانتهاء:"
    )
    return PRESET_ADD_CONTENT


async def preset_add_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = msg.text or msg.caption or ""
    if text.strip() == ".":
        if not context.user_data["preset_new"].get("messages"):
            await msg.reply_text("❌ لم ترسل أي رسالة!")
            return PRESET_ADD_CONTENT
        await msg.reply_text(
            f"✅ تم حفظ {len(context.user_data['preset_new']['messages'])} رسالة.\n\n"
            "⏱ أرسل الوقت بين الرسائل (بالثواني):\n"
            "مثال: 60"
        )
        return PRESET_ADD_DELAY
    media_type = "text"
    media_file_id = None
    if msg.photo:
        media_type = "photo"
        media_file_id = msg.photo[-1].file_id
    elif msg.video:
        media_type = "video"
        media_file_id = msg.video.file_id
    elif msg.document:
        media_type = "document"
        media_file_id = msg.document.file_id
    elif msg.animation:
        media_type = "animation"
        media_file_id = msg.animation.file_id
    elif msg.audio:
        media_type = "audio"
        media_file_id = msg.audio.file_id

    if "messages" not in context.user_data["preset_new"]:
        context.user_data["preset_new"]["messages"] = []

    context.user_data["preset_new"]["messages"].append({
        "content": text,
        "media_type": media_type,
        "media_file_id": media_file_id
    })
    n = len(context.user_data["preset_new"]["messages"])
    await msg.reply_text(
        f"✅ تم حفظ الرسالة رقم {n}\n\n"
        "أرسل المزيد أو اكتب . للانتهاء."
    )
    return PRESET_ADD_CONTENT


async def preset_add_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        delay = int(update.message.text.strip())
        if delay < 5:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ أرسل رقماً صحيحاً (5 أو أكثر).")
        return PRESET_ADD_DELAY
    context.user_data["preset_new"]["delay"] = delay
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("1️⃣ مرة واحدة", callback_data="preset_mode_once")],
        [InlineKeyboardButton("🔄 تكرار لانهائي", callback_data="preset_mode_loop")]
    ])
    await update.message.reply_text(
        "🔄 اختر نمط النشر:",
        reply_markup=kb
    )
    return PRESET_ADD_MODE


async def preset_add_mode_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    mode = "once" if q.data == "preset_mode_once" else "loop"
    context.user_data["preset_new"]["mode"] = mode

    user = get_user(q.from_user.id)
    p = context.user_data["preset_new"]

    preset_id = db_exec(
        "INSERT INTO presets (user_id, name, type, targets, messages, delay, mode) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            user["id"],
            p["name"],
            p["type"],
            json.dumps(p.get("targets", []), ensure_ascii=False),
            json.dumps(p.get("messages", []), ensure_ascii=False),
            p["delay"],
            p["mode"]
        )
    )

    context.user_data.pop("preset_new", None)

    type_str = "⭐ سوبر" if p["type"] == "super" else "🎯 كروبات محددة"
    mode_str = "مرة واحدة" if mode == "once" else "🔄 تكرار لانهائي"

    await q.edit_message_text(
        f"✅ تم حفظ النموذج بنجاح!\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📝 الاسم: {p['name']}\n"
        f"🏷 النوع: {type_str}\n"
        f"📨 عدد الرسائل: {len(p.get('messages', []))}\n"
        f"⏱ التأخير: {p['delay']} ثانية\n"
        f"🔄 النمط: {mode_str}\n"
        f"🆔 رقم النموذج: {preset_id}\n"
        f"━━━━━━━━━━━━━━━━━━━",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 رجوع للنماذج", callback_data="preset_menu")]
        ])
    )
    return PRESET_MENU


async def preset_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    preset_id = int(q.data.split("_")[-1])
    user = get_user(q.from_user.id)
    rows = db_exec("SELECT * FROM presets WHERE id=? AND user_id=?",
                   (preset_id, user["id"]), fetch=True)
    if not rows:
        await q.edit_message_text("❌ النموذج غير موجود!", reply_markup=back_kb("preset_menu"))
        return PRESET_MENU
    p = rows[0]

    try:
        targets = json.loads(p["targets"] or "[]")
    except Exception:
        targets = []
    try:
        messages = json.loads(p["messages"] or "[]")
    except Exception:
        messages = []

    type_str = "⭐ سوبر (بحث تلقائي)" if p["type"] == "super" else "🎯 كروبات محددة"
    mode_str = "مرة واحدة" if p["mode"] == "once" else "🔄 تكرار لانهائي"

    text = (
        f"📋 نموذج: {p['name']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🏷 النوع: {type_str}\n"
    )
    if p["type"] == "specific":
        text += f"🎯 الأهداف: {len(targets)}\n"
    text += (
        f"📨 عدد الرسائل: {len(messages)}\n"
        f"⏱ التأخير: {p['delay']} ثانية\n"
        f"🔄 النمط: {mode_str}\n"
        f"🆔 رقم النموذج: {p['id']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأكيد وتنفيذ النموذج", callback_data=f"preset_run_{p['id']}")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="preset_menu")]
    ])
    await q.edit_message_text(text, reply_markup=kb)
    return PRESET_VIEW


async def preset_run_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    preset_id = int(q.data.split("_")[-1])
    user = get_user(q.from_user.id)
    accounts = db_exec(
        "SELECT * FROM accounts WHERE user_id=? AND status='active'",
        (user["id"],), fetch=True
    )
    if not accounts:
        await q.edit_message_text(
            "❌ تحتاج حساباً واحداً على الأقل.\nأضف حساباً أولاً.",
            reply_markup=back_kb("preset_menu")
        )
        return PRESET_MENU
    context.user_data["preset_run_id"] = preset_id
    rows = [[InlineKeyboardButton(f"👤 {a['name']}", callback_data=f"preset_run_acc_{a['id']}")]
            for a in accounts]
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"preset_view_{preset_id}")])
    await q.edit_message_text(
        "👤 اختر الحساب لتنفيذ النموذج:",
        reply_markup=InlineKeyboardMarkup(rows)
    )
    return PRESET_RUN_SELECT_ACCOUNT


async def preset_run_account_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("🚀 جاري التنفيذ...")
    acc_id = int(q.data.split("_")[-1])
    preset_id = context.user_data.get("preset_run_id")
    if not preset_id:
        await q.edit_message_text("❌ لم يتم اختيار نموذج!", reply_markup=back_kb("preset_menu"))
        return PRESET_MENU

    user = get_user(q.from_user.id)
    presets = db_exec("SELECT * FROM presets WHERE id=? AND user_id=?",
                      (preset_id, user["id"]), fetch=True)
    if not presets:
        await q.edit_message_text("❌ النموذج غير موجود!", reply_markup=back_kb("preset_menu"))
        return PRESET_MENU
    p = presets[0]

    acc = db_exec("SELECT * FROM accounts WHERE id=?", (acc_id,), fetch=True)
    if not acc:
        await q.edit_message_text("❌ الحساب غير موجود!", reply_markup=back_kb("preset_menu"))
        return PRESET_MENU
    acc = acc[0]

    try:
        messages = json.loads(p["messages"] or "[]")
    except Exception:
        messages = []
    try:
        targets = json.loads(p["targets"] or "[]")
    except Exception:
        targets = []

    if not messages:
        await q.edit_message_text("❌ النموذج لا يحتوي على رسائل!", reply_markup=back_kb("preset_menu"))
        return PRESET_MENU

    job_id = db_exec(
        "INSERT INTO running_jobs (chat_id, user_id, account_id) VALUES (?,?,?)",
        (q.message.chat_id, user["id"], acc_id)
    )
    running_jobs[job_id] = {"status": "running", "pause_until": None}

    control_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏸ إيقاف مؤقت", callback_data=f"pause_{job_id}")],
        [InlineKeyboardButton("⏹ إيقاف نهائي", callback_data=f"stop_{job_id}")]
    ])

    mode_str = "مرة واحدة" if p["mode"] == "once" else "🔄 تكرار لانهائي"
    type_str = "⭐ سوبر" if p["type"] == "super" else "🎯 كروبات محددة"

    await q.edit_message_text(
        f"🚀 تنفيذ نموذج: {p['name']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 الحساب: {acc['name']}\n"
        f"🏷 النوع: {type_str}\n"
        f"📝 الرسائل: {len(messages)}\n"
        f"⏱ التأخير: {p['delay']} ثانية\n"
        f"🔄 النمط: {mode_str}\n"
        f"🆔 رقم المهمة: {job_id}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ جاري التنفيذ...",
        reply_markup=control_kb
    )

    if p["type"] == "super":
        asyncio.create_task(
            run_super_task(q.message.chat_id, user["id"], acc, messages,
                          p["delay"], p["mode"], job_id)
        )
    else:
        asyncio.create_task(
            run_publish_task(q.message.chat_id, user["id"], acc, messages,
                            targets, p["delay"], p["mode"], job_id)
        )
    return MAIN_MENU


async def preset_delete_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = get_user(q.from_user.id)
    presets = db_exec("SELECT * FROM presets WHERE user_id=? ORDER BY id DESC",
                      (user["id"],), fetch=True)
    if not presets:
        await q.edit_message_text(
            "🗑 لا توجد نماذج لحذفها.",
            reply_markup=back_kb("preset_menu")
        )
        return PRESET_MENU
    rows = [[InlineKeyboardButton(f"🗑 {p['name']}", callback_data=f"preset_del_{p['id']}")]
            for p in presets]
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="preset_menu")])
    await q.edit_message_text(
        "🗑 اختر النموذج لحذفه:",
        reply_markup=InlineKeyboardMarkup(rows)
    )
    return PRESET_MENU


async def preset_delete_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("🗑 تم الحذف")
    preset_id = int(q.data.split("_")[-1])
    user = get_user(q.from_user.id)
    db_exec("DELETE FROM presets WHERE id=? AND user_id=?", (preset_id, user["id"]))
    await preset_menu_cb(update, context)


async def main_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    for key in ["auth_client", "new_account"]:
        if key in context.user_data:
            client = context.user_data.get(key)
            if isinstance(client, TelegramClient):
                try:
                    await client.disconnect()
                except Exception:
                    pass
            context.user_data.pop(key, None)
    get_user(q.from_user.id)
    await q.edit_message_text(
        welcome_text(),
        reply_markup=main_menu_kb()
    )
    return MAIN_MENU


application: Application = None


def main():
    global application
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_cmd),
            MessageHandler(filters.Regex(r'^\(s\)$'), s_trigger),
        ],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(add_account_cb, pattern="^add_account$"),
                CallbackQueryHandler(my_accounts_cb, pattern="^my_accounts$"),
                CallbackQueryHandler(delete_account_cb, pattern="^del_acc_"),
                CallbackQueryHandler(start_publish_cb, pattern="^start_publish$"),
                CallbackQueryHandler(select_account_cb, pattern="^pub_acc_"),
                CallbackQueryHandler(confirm_publish_cb, pattern="^confirm_publish$"),
                CallbackQueryHandler(choose_mode_cb, pattern="^mode_"),
                CallbackQueryHandler(super_mode_cb, pattern="^super_mode$"),
                CallbackQueryHandler(super_select_account_cb, pattern="^super_acc_"),
                CallbackQueryHandler(super_choose_mode_cb, pattern="^super_mode_"),
                CallbackQueryHandler(preset_menu_cb, pattern="^preset_menu$"),
                CallbackQueryHandler(preset_add_cb, pattern="^preset_add$"),
                CallbackQueryHandler(preset_add_type_cb, pattern="^preset_type_"),
                CallbackQueryHandler(preset_add_mode_cb, pattern="^preset_mode_"),
                CallbackQueryHandler(preset_view_cb, pattern="^preset_view_"),
                CallbackQueryHandler(preset_run_cb, pattern="^preset_run_"),
                CallbackQueryHandler(preset_run_account_cb, pattern="^preset_run_acc_"),
                CallbackQueryHandler(preset_delete_menu_cb, pattern="^preset_delete_menu$"),
                CallbackQueryHandler(preset_delete_cb, pattern="^preset_del_"),
                CallbackQueryHandler(stats_cb, pattern="^stats$"),
                CallbackQueryHandler(pause_job_cb, pattern="^pause_"),
                CallbackQueryHandler(stop_job_cb, pattern="^stop_"),
                CallbackQueryHandler(back_to_job_cb, pattern="^back_to_job_"),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
                MessageHandler(filters.Regex(r'^\(s\)$'), s_trigger),
            ],
            ADD_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            ADD_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_code),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            ADD_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            SELECT_ACCOUNT: [
                CallbackQueryHandler(select_account_cb, pattern="^pub_acc_"),
                CallbackQueryHandler(super_select_account_cb, pattern="^super_acc_"),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            SEND_TEMPLATE: [
                MessageHandler(filters.ALL & ~filters.COMMAND, receive_template),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            SELECT_TARGETS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_targets),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            SET_DELAY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_delay),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            CHOOSE_MODE: [
                CallbackQueryHandler(choose_mode_cb, pattern="^mode_"),
                CallbackQueryHandler(confirm_publish_cb, pattern="^confirm_publish$"),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            SUPER_SEND_CONTENT: [
                MessageHandler(filters.ALL & ~filters.COMMAND, super_receive_content),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            SUPER_SET_DELAY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, super_receive_delay),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            SUPER_CHOOSE_MODE: [
                CallbackQueryHandler(super_choose_mode_cb, pattern="^super_mode_"),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            PAUSE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pause_duration),
                CallbackQueryHandler(back_to_job_cb, pattern="^back_to_job_"),
                CallbackQueryHandler(stop_job_cb, pattern="^stop_"),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            PRESET_MENU: [
                CallbackQueryHandler(preset_menu_cb, pattern="^preset_menu$"),
                CallbackQueryHandler(preset_add_cb, pattern="^preset_add$"),
                CallbackQueryHandler(preset_view_cb, pattern="^preset_view_"),
                CallbackQueryHandler(preset_run_cb, pattern="^preset_run_"),
                CallbackQueryHandler(preset_run_account_cb, pattern="^preset_run_acc_"),
                CallbackQueryHandler(preset_delete_menu_cb, pattern="^preset_delete_menu$"),
                CallbackQueryHandler(preset_delete_cb, pattern="^preset_del_"),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            PRESET_ADD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, preset_add_name),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            PRESET_ADD_TYPE: [
                CallbackQueryHandler(preset_add_type_cb, pattern="^preset_type_"),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            PRESET_ADD_TARGETS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, preset_add_targets),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            PRESET_ADD_CONTENT: [
                MessageHandler(filters.ALL & ~filters.COMMAND, preset_add_content),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            PRESET_ADD_DELAY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, preset_add_delay),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            PRESET_ADD_MODE: [
                CallbackQueryHandler(preset_add_mode_cb, pattern="^preset_mode_"),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            PRESET_VIEW: [
                CallbackQueryHandler(preset_run_cb, pattern="^preset_run_"),
                CallbackQueryHandler(preset_menu_cb, pattern="^preset_menu$"),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            PRESET_RUN_SELECT_ACCOUNT: [
                CallbackQueryHandler(preset_run_account_cb, pattern="^preset_run_acc_"),
                CallbackQueryHandler(preset_view_cb, pattern="^preset_view_"),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
        },
        fallbacks=[
            CommandHandler("start", start_cmd),
            CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
        ],
        allow_reentry=True,
    )
    application.add_handler(conv)
    print("البوت يعمل الآن...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
