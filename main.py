# ==============================================================
# 🤖 بوت النشر التلقائي في تيليكرام
# ==============================================================
# المتطلبات:
#   pip install telethon python-telegram-bot aiosqlite cryptg
#
# التشغيل:
#   python auto_publisher_bot.py
#
# الملفات المنتجة:
#   bot_data.db       - قاعدة البيانات
#   sessions/         - ملفات الجلسات
# ==============================================================

import asyncio
import sqlite3
import random
import os
import re
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)
from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession
from telethon.errors import (
    FloodWaitError, SessionPasswordNeededError,
    PhoneCodeInvalidError, PhoneCodeExpiredError,
    UserAlreadyParticipantError, ChannelPrivateError,
    ChatAdminRequiredError, UserBannedInChannelError
)

# ==============================================================
# ⚙️ الإعدادات - عدّلها قبل التشغيل
# ==============================================================

BOT_TOKEN = "8787120916:AAGEw6sDiXz_SMLsoa6OtyPqNd7gxYOOq_A"
API_ID =  36781759                         # من my.telegram.org
API_HASH = "31a2abacece3f047a878d001aa3fbd95"
ADMIN_ID = 8862852738                     # آيدي المالك

DB_PATH = "bot_data.db"
SESSIONS_DIR = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

# كلمات مفتاحية لميزة سوبر
SUPER_KEYWORDS = [
    "مقابل", "بيع", "شراء", "اسيا", "آسيا",
    "ماستر", "اثير", "أثير", "مميزة", "مميزه",
    "هدايه", "هداية", "ارقام", "أرقام", "مطلوب"
]

# كلمات اسم سوبر
SUPER_NAME_KEYWORDS = ["سوبر", "super", "Super", "SUPER"]

# ==============================================================
# 📋 حالات المحادثة
# ==============================================================

(
    MAIN_MENU,
    ADD_PHONE, ADD_CODE,
    SELECT_ACCOUNT,
    SEND_TEMPLATE,
    SELECT_TARGETS,
    SET_DELAY,
    SUPER_SEND_CONTENT,
    SUPER_SET_DELAY,
) = range(9)

# ==============================================================
# 🗄️ قاعدة البيانات
# ==============================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        telegram_id INTEGER UNIQUE,
        username TEXT,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        is_admin INTEGER DEFAULT 0
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
        "INSERT INTO users (telegram_id, username, is_admin) VALUES (?,?,?)",
        (telegram_id, "", 1 if telegram_id == ADMIN_ID else 0)
    )
    return {"id": uid, "telegram_id": telegram_id, "is_admin": telegram_id == ADMIN_ID}


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


# ==============================================================
# 🛡️ نظام مكافحة الحظر
# ==============================================================

class AntiBan:
    """نظام ذكي لمنع الحظر"""
    
    def __init__(self):
        self.account_sends = {}  # {account_id: [timestamps]}
        self.peer_sends = {}     # {(account_id, peer): [timestamps]}
    
    async def wait_before_send(self, account_id, peer, base_delay):
        """تأخير عشوائي مع مراقبة الحدود"""
        # تأخير عشوائي ± 40%
        jitter = base_delay * random.uniform(0.6, 1.4)
        await asyncio.sleep(jitter)
    
    async def human_delay(self):
        """محاكاة السلوك البشري"""
        await asyncio.sleep(random.uniform(1.5, 4.5))
    
    def should_rest(self, sent_count):
        """هل يحتاج استراحة؟"""
        return sent_count > 0 and sent_count % 20 == 0
    
    async def rest(self):
        """استراحة عشوائية"""
        await asyncio.sleep(random.uniform(300, 900))  # 5-15 دقيقة


anti_ban = AntiBan()


# ==============================================================
# 🔧 أدوات مساعدة
# ==============================================================

def is_valid_code_format(text: str) -> bool:
    """التأكد أن الكود بالصيغة 0 0 0 0 0 (أرقام مع فراغات)"""
    # يجب أن يحتوي على فراغات بين كل رقم
    # الصيغة: رقم فراغ رقم فراغ رقم...
    pattern = r'^\d(\s+\d)+$'
    return bool(re.match(pattern, text.strip()))


def parse_code(text: str) -> str:
    """استخراج الكود بدون فراغات"""
    return re.sub(r'\s+', '', text.strip())


def normalize_arabic(text: str) -> str:
    """تطبيع النص العربي للمقارنة"""
    text = re.sub(r'[\u064B-\u0652]', '', text)  # إزالة التشكيل
    text = text.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا')
    text = text.replace('ة', 'ه').replace('ى', 'ي')
    return text


def contains_keywords(text: str, keywords: List[str]) -> Optional[str]:
    """فحص النص لكلمات مفتاحية"""
    if not text:
        return None
    normalized = normalize_arabic(text.lower())
    for kw in keywords:
        kw_norm = normalize_arabic(kw.lower())
        if kw_norm in normalized:
            return kw
    return None


def safe_name(name: str) -> str:
    """تنظيف اسم الحساب"""
    return (name or "").strip()[:50] or "حساب"


# ==============================================================
# 📱 إدارة حسابات Telethon
# ==============================================================

class AccountManager:
    """إدارة جلسات الحسابات"""
    
    def __init__(self):
        self.active_clients = {}  # {account_id: TelegramClient}
    
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


# ==============================================================
# 🔍 محرك البحث الذكي "سوبر"
# ==============================================================

class SuperSearch:
    """محرك البحث الذكي عن الأهداف"""
    
    @staticmethod
    async def search_by_name(client: TelegramClient, limit: int = 500):
        """البحث عن الكروبات/القنوات التي اسمها يحتوي كلمات سوبر"""
        results = []
        try:
            dialogs = await client.get_dialogs(limit=limit)
            for dialog in dialogs:
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
            logging.error(f"خطأ في البحث بالاسم: {e}")
        return results
    
    @staticmethod
    async def search_in_messages(client: TelegramClient, limit: int = 300,
                                 messages_to_check: int = 100):
        """البحث في رسائل الكروبات عن كلمات مفتاحية"""
        results = []
        try:
            dialogs = await client.get_dialogs(limit=limit)
            for dialog in dialogs:
                name = dialog.name or ""
                # تجاهل الكروبات اللي اسمها فيه سوبر (تم العثور عليها)
                if any(kw.lower() in name.lower() for kw in SUPER_NAME_KEYWORDS):
                    continue
                
                # تجاهل الخاص
                if dialog.is_user:
                    continue
                
                try:
                    found_kw = None
                    async for msg in client.iter_messages(dialog.id, limit=messages_to_check):
                        if msg.text:
                            found_kw = contains_keywords(msg.text, SUPER_KEYWORDS)
                            if found_kw:
                                break
                        await asyncio.sleep(0.05)  # تأخير بسيط
                    
                    if found_kw:
                        results.append({
                            "id": dialog.id,
                            "name": name,
                            "username": getattr(dialog.entity, 'username', None),
                            "type": "messages",
                            "matched": found_kw
                        })
                except (ChannelPrivateError, ChatAdminRequiredError,
                        UserBannedInChannelError):
                    continue
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds + 5)
                except Exception as e:
                    logging.debug(f"تخطي {name}: {e}")
                    continue
                
                await asyncio.sleep(random.uniform(0.3, 0.8))
        except Exception as e:
            logging.error(f"خطأ في البحث بالرسائل: {e}")
        return results


# ==============================================================
# 📤 محرك النشر
# ==============================================================

class Publisher:
    """محرك النشر الفعلي"""
    
    @staticmethod
    async def publish_to_target(client: TelegramClient, target,
                                messages: List[Dict], delay: int,
                                account_id: int, user_id: int,
                                progress_callback=None):
        """نشر الرسائل في هدف واحد"""
        sent = 0
        failed = 0
        
        # حل الكيان
        try:
            peer = await client.get_entity(target)
        except Exception as e:
            logging.error(f"لا يمكن الوصول لـ {target}: {e}")
            return 0, len(messages)
        
        # إرسال علامة "يكتب"
        try:
            await client.action(peer, 'typing')
        except Exception:
            pass
        
        for idx, msg in enumerate(messages):
            try:
                await anti_ban.human_delay()
                
                if msg["media_type"] == "text":
                    await client.send_message(peer, msg["content"])
                else:
                    await client.send_file(peer, msg["media_file_id"],
                                          caption=msg["content"] or "")
                
                sent += 1
                update_stats(user_id, True)
                
                # تأخير بين الرسائل
                if idx < len(messages) - 1:
                    await anti_ban.wait_before_send(account_id, peer, delay)
                
                # استراحة دورية
                if anti_ban.should_rest(sent):
                    if progress_callback:
                        await progress_callback(f"⏸ استراحة دورية بعد {sent} رسالة...")
                    await anti_ban.rest()
            
            except FloodWaitError as e:
                if progress_callback:
                    await progress_callback(f"⏳ FloodWait: {e.seconds} ثانية")
                await asyncio.sleep(e.seconds + 10)
            except Exception as e:
                failed += 1
                update_stats(user_id, False)
                logging.error(f"فشل الإرسال: {e}")
        
        return sent, failed


# ==============================================================
# 🎨 واجهات لوحة المفاتيح
# ==============================================================

def main_menu_kb(is_admin=False):
    rows = [
        [InlineKeyboardButton("➕ إضافة حساب", callback_data="add_account")],
        [InlineKeyboardButton("📂 حساباتي", callback_data="my_accounts")],
        [InlineKeyboardButton("🚀 بدء النشر", callback_data="start_publish")],
        [InlineKeyboardButton("⭐ سوبر", callback_data="super_mode")],
        [InlineKeyboardButton("📊 الإحصائيات", callback_data="stats")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("👑 لوحة المالك", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def back_kb(callback="main_menu"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data=callback)]])


def welcome_text():
    return (
        "🤖 *بوت النشر التلقائي*\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "مرحباً بك! 👋\n\n"
        "هذا البوت يمكنك من:\n"
        "➕ إضافة حسابات تيليكرام\n"
        "📢 النشر التلقائي في المجموعات\n"
        "⭐ البحث الذكي بكلمة \"سوبر\"\n"
        "🛡 نظام ذكي ضد الحظر\n"
        "📊 إحصائيات مفصّلة\n\n"
        "اختر من الأزرار أدناه: 👇"
    )


# ==============================================================
# 🎯 الأوامر الأساسية
# ==============================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(s) أو /start"""
    user_tg = update.effective_user
    user = get_user(user_tg.id)
    context.user_data.clear()
    
    text = welcome_text()
    kb = main_menu_kb(user["is_admin"])
    
    if update.message:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
    return MAIN_MENU


async def s_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عند كتابة (s)"""
    text = update.message.text.strip()
    if text == "(s)":
        return await start_cmd(update, context)


# ==============================================================
# ➕ إضافة حساب
# ==============================================================

async def add_account_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["new_account"] = {}
    context.user_data["auth_client"] = None
    
    await q.edit_message_text(
        "📱 *إضافة حساب جديد*\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "أرسل رقم هاتفك بالصيغة الدولية:\n"
        "مثال: `+9647701234567`\n\n"
        "⚠️ الرقم يجب أن يكون مسجّلاً في تيليكرام.",
        reply_markup=back_kb(),
        parse_mode="Markdown"
    )
    return ADD_PHONE


async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    
    if not re.match(r'^\+\d{8,15}$', phone):
        await update.message.reply_text(
            "❌ صيغة الرقم غير صحيحة!\n"
            "أرسل الرقم هكذا: `+9647701234567`",
            parse_mode="Markdown"
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
            "🔐 *تم إرسال كود التحقق*\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            "أرسل الكود بالصيغة التالية:\n"
            "`0 0 0 0 0`\n\n"
            "⚠️ *مهم جداً:*\n"
            "يجب أن يكون بين كل رقم ورقم **فراغ**\n"
            "وإلا لن يكتمل تسجيل الدخول!\n\n"
            "📌 مثال: إذا وصلك `12345` أرسله `1 2 3 4 5`",
            parse_mode="Markdown"
        )
        return ADD_CODE
    
    except Exception as e:
        await msg.edit_text(f"❌ خطأ: {e}", reply_markup=back_kb())
        return MAIN_MENU


async def receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    # التحقق من الصيغة 0 0 0 0 0
    if not is_valid_code_format(text):
        await update.message.reply_text(
            "❌ *صيغة الكود خاطئة!*\n\n"
            "يجب أن ترسل الكود بأرقام متباعدة بفراغات:\n"
            "مثال: `1 2 3 4 5`\n\n"
            "أعد الإرسال:",
            parse_mode="Markdown"
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
            "✅ *تم إضافة الحساب بنجاح!*\n"
            "━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 *الاسم:* {me.first_name or 'بدون'}\n"
            f"📱 *الرقم:* `{phone}`\n"
            f"🆔 *الآيدي:* `{me.id}`\n"
            f"🔗 *اليوزر:* @{me.username or 'لا يوجد'}\n\n"
            "الحساب جاهز للنشر 🚀",
            reply_markup=back_kb(),
            parse_mode="Markdown"
        )
        return MAIN_MENU
    
    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔒 الحساب محمي بكلمة مرور ثنائية.\n"
            "الرجاء إزالة الحماية مؤقتاً ثم إعادة المحاولة.",
            reply_markup=back_kb()
        )
        return MAIN_MENU
    except PhoneCodeInvalidError:
        await update.message.reply_text("❌ الكود غير صحيح! أعد الإرسال:")
        return ADD_CODE
    except PhoneCodeExpiredError:
        await update.message.reply_text("❌ انتهت صلاحية الكود! أعد المحاولة.", reply_markup=back_kb())
        return MAIN_MENU
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ: {e}", reply_markup=back_kb())
        return MAIN_MENU


# ==============================================================
# 📂 حساباتي
# ==============================================================

async def my_accounts_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    user = get_user(q.from_user.id)
    accounts = db_exec("SELECT * FROM accounts WHERE user_id=? ORDER BY id DESC",
                      (user["id"],), fetch=True)
    
    if not accounts:
        await q.edit_message_text(
            "📂 *حساباتي*\n\n"
            "لا توجد حسابات مضافة حتى الآن.\n"
            "اضغط ➕ لإضافة حساب جديد.",
            reply_markup=back_kb(),
            parse_mode="Markdown"
        )
        return MAIN_MENU
    
    text = f"📂 *حساباتك ({len(accounts)})*\n━━━━━━━━━━━━━━━━━━━\n\n"
    rows = []
    for i, acc in enumerate(accounts, 1):
        status_emoji = {"active": "🟢", "cooling": "🟡", "banned": "🔴"}.get(acc["status"], "⚪")
        text += f"{i}. {status_emoji} *{acc['name']}*\n   📱 `{acc['phone']}`\n\n"
        rows.append([InlineKeyboardButton(
            f"🗑 حذف {acc['name']}",
            callback_data=f"del_acc_{acc['id']}"
        )])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])
    
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")
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


# ==============================================================
# 🚀 بدء النشر
# ==============================================================

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
            reply_markup=back_kb(), parse_mode="Markdown"
        )
        return MAIN_MENU
    
    rows = [[InlineKeyboardButton(f"👤 {a['name']}", callback_data=f"pub_acc_{a['id']}")]
            for a in accounts]
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])
    
    await q.edit_message_text(
        "🚀 *بدء النشر*\n\nاختر الحساب الذي تريد النشر منه:",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown"
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
        "📝 *أرسل الكليشة (الرسالة) التي تريد نشرها*\n\n"
        "يمكن أن تحتوي على:\n"
        "• نص فقط\n"
        "• صورة + نص\n"
        "• فيديو + نص\n"
        "• ملف + نص\n\n"
        "بعد كل رسالة سيتم إعلامك.",
        reply_markup=back_kb(), parse_mode="Markdown"
    )
    return SEND_TEMPLATE


async def receive_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = update.effective_user.id
    
    text = msg.text or msg.caption or ""
    
    # إنهاء الجمع
    if text.strip() == ".":
        if not context.user_data.get("pub_messages"):
            await msg.reply_text("❌ لم ترسل أي رسالة بعد!")
            return SEND_TEMPLATE
        
        count = len(context.user_data["pub_messages"])
        await msg.reply_text(
            f"✅ تم حفظ {count} رسالة.\n\n"
            "🔗 *أرسل رابط/آيدي الهدف:*\n\n"
            "يدعم:\n"
            "• رابط عام أو خاص\n"
            "• @username\n"
            "• آيدي رقمي\n\n"
            "💡 يمكنك إرسال أكثر من هدف، افصل بينهم بسطر جديد.",
            parse_mode="Markdown"
        )
        return SELECT_TARGETS
    
    # تحديد نوع الميديا
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
        "⌨️ أو اكتب `.` لإنهاء اختيار الكليشة",
        parse_mode="Markdown"
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
        "⏱ *حدد الوقت بين كل رسالة ورسالة (بالثواني)*\n\n"
        "مثال: `60` (يعني دقيقة بين كل رسالة)\n"
        "💡 كل ما زاد الوقت، قلّ احتمال الحظر.",
        parse_mode="Markdown"
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
    
    n_msgs = len(context.user_data["pub_messages"])
    n_targets = len(context.user_data["pub_targets"])
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأكيد وبدء النشر", callback_data="confirm_publish")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="main_menu")]
    ])
    
    await update.message.reply_text(
        f"📋 *تأكيد العملية*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📝 عدد الرسائل: *{n_msgs}*\n"
        f"🎯 عدد الأهداف: *{n_targets}*\n"
        f"⏱ التأخير: *{delay}* ثانية\n"
        f"🛡 الحماية الذكية: *مفعّلة*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"هل تريد البدء؟",
        reply_markup=kb, parse_mode="Markdown"
    )
    return SET_DELAY


async def confirm_publish_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("🚀 جاري البدء...")
    
    user_id_tg = q.from_user.id
    user = get_user(user_id_tg)
    
    acc_id = context.user_data["pub_account_id"]
    messages = context.user_data["pub_messages"]
    targets = context.user_data["pub_targets"]
    delay = context.user_data["pub_delay"]
    
    acc = db_exec("SELECT * FROM accounts WHERE id=?", (acc_id,), fetch=True)
    if not acc:
        await q.edit_message_text("❌ الحساب غير موجود!", reply_markup=back_kb())
        return MAIN_MENU
    acc = acc[0]
    
    await q.edit_message_text(
        f"🚀 *بدأ النشر*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 الحساب: *{acc['name']}*\n"
        f"📝 الرسائل: *{len(messages)}*\n"
        f"🎯 الأهداف: *{len(targets)}*\n"
        f"⏱ التأخير: *{delay}* ثانية\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ جاري التنفيذ...",
        parse_mode="Markdown"
    )
    
    asyncio.create_task(
        run_publish_task(q.message.chat_id, user["id"], acc, messages, targets, delay)
    )
    return MAIN_MENU


async def run_publish_task(chat_id: int, user_id: int, acc: dict,
                            messages: List[Dict], targets: List[str], delay: int):
    """مهمة النشر في الخلفية"""
    client = None
    try:
        client = await account_mgr.get_client(acc["id"], acc["session_string"])
        if not client:
            await send_msg(chat_id, "❌ فشل الاتصال بالحساب!")
            return
        
        total_sent = 0
        total_failed = 0
        
        for t in targets:
            await send_msg(chat_id, f"📤 جاري النشر في: `{t}`")
            
            sent, failed = await Publisher.publish_to_target(
                client, t, messages, delay, acc["id"], user_id
            )
            total_sent += sent
            total_failed += failed
            
            await send_msg(
                chat_id,
                f"✅ {t}\n"
                f"   ✔ نجح: {sent} | ✖ فشل: {failed}"
            )
        
        await send_msg(
            chat_id,
            f"🏁 *اكتمل النشر*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"✅ نجح: {total_sent}\n"
            f"❌ فشل: {total_failed}\n"
            f"━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )
    except Exception as e:
        await send_msg(chat_id, f"❌ خطأ في المهمة: {e}")
    finally:
        context_cleanup = None


async def send_msg(chat_id: int, text: str, parse_mode=None):
    """إرسال رسالة باستخدام البوت"""
    try:
        await application.bot.send_message(chat_id, text, parse_mode=parse_mode)
    except Exception as e:
        logging.error(f"send_msg: {e}")


# ==============================================================
# ⭐ ميزة سوبر
# ==============================================================

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
            reply_markup=back_kb(), parse_mode="Markdown"
        )
        return MAIN_MENU
    
    rows = [[InlineKeyboardButton(f"👤 {a['name']}", callback_data=f"super_acc_{a['id']}")]
            for a in accounts]
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="main_menu")])
    
    await q.edit_message_text(
        "⭐ *ميزة سوبر*\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "هذه الميزة تقوم بالبحث الذكي عن:\n"
        "• كل الكروبات/القنوات باسم \"سوبر\" أو \"Super\"\n"
        "• الكروبات التي رسائلها تحتوي كلمات تجارية\n"
        "  (مقابل، بيع، شراء، اسيا، ماستر، أثير...)\n\n"
        "اختر الحساب للبحث:",
        reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown"
    )
    return SELECT_ACCOUNT


async def super_select_account_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    acc_id = int(q.data.split("_")[-1])
    context.user_data["super_account_id"] = acc_id
    context.user_data["super_messages"] = []
    
    await q.edit_message_text(
        "⭐ *سوبر*\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "📤 أرسل الكليشة/الرابط الذي تريد نشره:",
        reply_markup=back_kb(), parse_mode="Markdown"
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
            "مثال: `30`",
            parse_mode="Markdown"
        )
        return SUPER_SET_DELAY
    
    media_type = "text"
    media_file_id = None
    
    if msg.photo:
        media_type = "photo"; media_file_id = msg.photo[-1].file_id
    elif msg.video:
        media_type = "video"; media_file_id = msg.video.file_id
    elif msg.document:
        media_type = "document"; media_file_id = msg.document.file_id
    
    context.user_data["super_messages"].append({
        "content": text,
        "media_type": media_type,
        "media_file_id": media_file_id
    })
    
    n = len(context.user_data["super_messages"])
    await msg.reply_text(
        f"✅ تم حفظ الرسالة رقم {n}\n\n"
        "أرسل المزيد أو اكتب `.` للانتهاء.",
        parse_mode="Markdown"
    )
    return SUPER_SEND_CONTENT


async def super_receive_delay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        delay = int(update.message.text.strip())
        if delay < 5: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ أرسل رقماً صحيحاً (≥ 5).")
        return SUPER_SET_DELAY
    
    acc_id = context.user_data["super_account_id"]
    messages = context.user_data["super_messages"]
    user_id_tg = update.effective_user.id
    user = get_user(user_id_tg)
    
    acc = db_exec("SELECT * FROM accounts WHERE id=?", (acc_id,), fetch=True)
    if not acc:
        await update.message.reply_text("❌ الحساب غير موجود!", reply_markup=back_kb())
        return MAIN_MENU
    acc = acc[0]
    
    status_msg = await update.message.reply_text(
        "🔍 *جاري البحث الذكي...*\n\n"
        "⏳ المرحلة 1: البحث بالاسم...",
        parse_mode="Markdown"
    )
    
    asyncio.create_task(
        run_super_task(status_msg.chat_id, user["id"], acc, messages, delay)
    )
    return MAIN_MENU


async def run_super_task(chat_id: int, user_id: int, acc: dict,
                          messages: List[Dict], delay: int):
    """مهمة البحث والنشر الذكي"""
    client = None
    try:
        client = await account_mgr.get_client(acc["id"], acc["session_string"])
        if not client:
            await send_msg(chat_id, "❌ فشل الاتصال بالحساب!")
            return
        
        # المرحلة 1: البحث بالاسم
        await send_msg(chat_id, "🔍 *المرحلة 1:* البحث بالاسم...", parse_mode="Markdown")
        name_results = await SuperSearch.search_by_name(client)
        
        await send_msg(
            chat_id,
            f"✅ تم العثور على {len(name_results)} كروب/قناة باسم سوبر",
        )
        
        # المرحلة 2: البحث في الرسائل
        await send_msg(
            chat_id,
            "🔍 *المرحلة 2:* فحص الرسائل...\n"
            "⏳ (قد يستغرق وقتاً طويلاً)",
            parse_mode="Markdown"
        )
        msg_results = await SuperSearch.search_in_messages(client)
        
        await send_msg(
            chat_id,
            f"✅ تم العثور على {len(msg_results)} كروب بناءً على الرسائل",
        )
        
        # دمج النتائج
        all_targets = {}
        for r in name_results + msg_results:
            all_targets[r["id"]] = r
        
        targets = list(all_targets.values())
        
        if not targets:
            await send_msg(chat_id, "❌ لم يتم العثور على أي هدف!")
            return
        
        await send_msg(
            chat_id,
            f"📊 *إجمالي الأهداف: {len(targets)}*\n\n"
            f"⏳ جاري بدء النشر...",
            parse_mode="Markdown"
        )
        
        # النشر
        total_sent = 0
        total_failed = 0
        
        for i, target in enumerate(targets, 1):
            try:
                peer_entity = await client.get_entity(target["id"])
                sent, failed = await Publisher.publish_to_target(
                    client, peer_entity, messages, delay, acc["id"], user_id
                )
                total_sent += sent
                total_failed += failed
                
                if i % 5 == 0:
                    await send_msg(
                        chat_id,
                        f"📊 تقدم: {i}/{len(targets)}\n"
                        f"   ✅ {total_sent} | ❌ {total_failed}"
                    )
            except Exception as e:
                logging.error(f"خطأ في {target['name']}: {e}")
                continue
        
        await send_msg(
            chat_id,
            f"🏁 *اكتمل النشر*\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 الأهداف: {len(targets)}\n"
            f"✅ نجح: {total_sent}\n"
            f"❌ فشل: {total_failed}\n"
            f"━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )
    except Exception as e:
        await send_msg(chat_id, f"❌ خطأ: {e}")


# ==============================================================
# 📊 الإحصائيات
# ==============================================================

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
        f"📊 *إحصائياتك*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 الحسابات: *{accounts_count}*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📅 *اليوم:*\n"
        f"   ✅ نجح: {today_ok}\n"
        f"   ❌ فشل: {today_fail}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📈 *الإجمالي:*\n"
        f"   ✅ نجح: {total_ok}\n"
        f"   ❌ فشل: {total_fail}\n"
        f"━━━━━━━━━━━━━━━━━━━",
        reply_markup=back_kb(), parse_mode="Markdown"
    )
    return MAIN_MENU


# ==============================================================
# 👑 لوحة المالك
# ==============================================================

async def admin_panel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    if q.from_user.id != ADMIN_ID:
        await q.answer("❌ هذه اللوحة للمالك فقط!", show_alert=True)
        return MAIN_MENU
    
    users = db_exec("SELECT COUNT(*) as c FROM users", fetch=True)[0]["c"]
    accounts = db_exec("SELECT COUNT(*) as c FROM accounts", fetch=True)[0]["c"]
    
    total = db_exec("SELECT SUM(sent_ok) as ok, SUM(sent_fail) as fail FROM stats", fetch=True)[0]
    total_ok = total["ok"] or 0
    total_fail = total["fail"] or 0
    
    await q.edit_message_text(
        f"👑 *لوحة تحكم المالك*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👥 المستخدمون: *{users}*\n"
        f"📱 الحسابات: *{accounts}*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"✅ إجمالي النجاح: *{total_ok}*\n"
        f"❌ إجمالي الفشل: *{total_fail}*\n"
        f"━━━━━━━━━━━━━━━━━━━",
        reply_markup=back_kb(), parse_mode="Markdown"
    )
    return MAIN_MENU


# ==============================================================
# 🔙 القائمة الرئيسية
# ==============================================================

async def main_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    # تنظيف الجلسات المؤقتة
    for key in ["auth_client", "new_account"]:
        if key in context.user_data:
            client = context.user_data.get(key)
            if isinstance(client, TelegramClient):
                try:
                    await client.disconnect()
                except Exception:
                    pass
            context.user_data.pop(key, None)
    
    user = get_user(q.from_user.id)
    await q.edit_message_text(
        welcome_text(),
        reply_markup=main_menu_kb(user["is_admin"]),
        parse_mode="Markdown"
    )
    return MAIN_MENU


async def cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await main_menu_cb(update, context)


# ==============================================================
# 🚀 تشغيل البوت
# ==============================================================

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
                CallbackQueryHandler(super_mode_cb, pattern="^super_mode$"),
                CallbackQueryHandler(super_select_account_cb, pattern="^super_acc_"),
                CallbackQueryHandler(stats_cb, pattern="^stats$"),
                CallbackQueryHandler(admin_panel_cb, pattern="^admin_panel$"),
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
            SELECT_ACCOUNT: [
                CallbackQueryHandler(select_account_cb, pattern="^pub_acc_"),
                CallbackQueryHandler(super_select_account_cb, pattern="^super_acc_"),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            SEND_TEMPLATE: [
                MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.Regex(r'^/'), receive_template),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            SELECT_TARGETS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_targets),
                CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
            ],
            SET_DELAY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_delay),
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
        },
        fallbacks=[
            CommandHandler("start", start_cmd),
            CallbackQueryHandler(main_menu_cb, pattern="^main_menu$"),
        ],
        allow_reentry=True,
    )
    
    application.add_handler(conv)
    
    print("🤖 البوت يعمل الآن...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
