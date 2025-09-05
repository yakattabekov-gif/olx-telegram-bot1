# olx_bot_full_v7.py
import logging
import requests
import re
from html import escape
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, ContextTypes, filters
from urllib.parse import quote
from datetime import datetime, timedelta

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = "8112382224:AAFLpLO-nTgVvrAb2zFJ29AjFbK73yDHuNc"
ADMIN_USERNAME = "yakattabekov"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120 Safari/537.36"
}
ADMIN_PAGE_SIZE = 5
MAX_SAVED_QUERIES = 2000

# ---------------- State ----------------
user_queries = []  # {"id","username","name","text","time"}
admin_state = {}   # {user_id: {"mode":"main","page":0,"selected_user":None,"waiting_username":False,"waiting_days":False,"target_username":""}}
subscriptions = {}  # {user_id: {"active": True, "until": datetime}}
CURRENCY_SYMBOLS = {"KZT": "₸"}

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s - %(message)s")

# ---------------- Helpers ----------------
def _format_amount(amount, currency="KZT"):
    try:
        val = float(amount)
        if val.is_integer():
            formatted = f"{int(val):,}".replace(",", " ")
        else:
            formatted = f"{val:,.2f}".replace(",", " ").replace(".", ",")
        sym = CURRENCY_SYMBOLS.get(currency, "")
        return f"{formatted} {sym}"
    except:
        return str(amount)

def _normalize_price_text(s):
    if not s: return s
    s = s.strip()
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'(?i)\bтг\.?\b', '₸', s)
    s = re.sub(r'(?i)\bтенге\b', '₸', s)
    return s

def fetch_price_from_html(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=12)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        container = soup.select_one('div[data-testid="ad-price-container"] h3')
        if container:
            return _normalize_price_text(container.get_text(" ", strip=True))
        return None
    except:
        return None

def get_unique_users():
    """Получить уникальных пользователей из логов"""
    unique_users = {}
    for query in user_queries:
        user_id = query["id"]
        if user_id not in unique_users:
            unique_users[user_id] = {
                "id": user_id,
                "username": query["username"],
                "name": query["name"],
                "last_activity": query["time"],
                "query_count": 0
            }
        unique_users[user_id]["query_count"] += 1
        if query["time"] > unique_users[user_id]["last_activity"]:
            unique_users[user_id]["last_activity"] = query["time"]
    return list(unique_users.values())

def get_user_queries(user_id):
    """Получить все запросы конкретного пользователя"""
    return [q for q in user_queries if q["id"] == user_id]

def find_user_by_username(username):
    """Найти пользователя по username"""
    username = username.replace("@", "").lower()
    for query in user_queries:
        if query["username"].lower() == username:
            return query["id"]
    return None

def is_subscription_active(user_id):
    """Проверить активность подписки"""
    if user_id in subscriptions:
        sub = subscriptions[user_id]
        if sub["active"] and sub["until"] > datetime.now():
            return True
        else:
            # Автоматически деактивируем просроченные подписки
            subscriptions[user_id]["active"] = False
    return False

# ---------------- Extractors ----------------
def _extract_city(offer):
    loc = offer.get("locations_resolved") or offer.get("location") or {}
    if isinstance(loc, dict):
        if "name" in loc and isinstance(loc["name"], str):
            return loc["name"]
        if isinstance(loc.get("city"), dict):
            return loc.get("city", {}).get("name")
    return "Город не указан"

def _extract_date(offer):
    dt = offer.get("created_time") or offer.get("created_at") or offer.get("publication_time") or offer.get("date")
    if isinstance(dt, str) and "T" in dt:
        return dt.split("T")[0]
    return dt or "Дата не указана"

def _extract_url(offer):
    link = offer.get("url")
    if not link:
        path = offer.get("path")
        if path:
            if path.startswith("/"): link = "https://www.olx.kz" + path
            else: link = "https://www.olx.kz/" + path
    return link

def _extract_price_from_offer_object(offer):
    try:
        price_obj = offer.get("price") or {}
        value_obj = price_obj.get("value") or {}
        amount = None
        currency = None
        if isinstance(value_obj, dict):
            amount = value_obj.get("amount") or value_obj.get("value")
            currency = value_obj.get("currency") or price_obj.get("currency")
        if amount:
            return _format_amount(amount, currency or "KZT")
    except:
        pass
    return None

def _extract_description(offer):
    desc = offer.get("description") or offer.get("content") or ""
    desc = re.sub(r"<[^>]+>", "", desc)
    return (desc[:200] + "...") if len(desc) > 200 else desc

# ---------------- OLX search ----------------
def search_olx(query, max_results=6):
    query_for_url = quote(query)
    url = f"https://www.olx.kz/api/v1/offers/?offset=0&limit={max_results}&query={query_for_url}"
    try:
        r = requests.get(url, headers={"User-Agent": HEADERS["User-Agent"], "Accept": "application/json"}, timeout=12)
        r.raise_for_status()
        data = r.json()
        offers = data.get("data") or data.get("offers") or []
    except:
        offers = []

    results = []
    for offer in offers:
        title = offer.get("title") or offer.get("name") or "Без названия"
        price_text = _extract_price_from_offer_object(offer)
        city = _extract_city(offer)
        date = _extract_date(offer)
        link = _extract_url(offer)
        desc = _extract_description(offer)
        try:
            price_num = float(re.sub(r"[^\d]", "", str(price_text or "0")))
        except:
            price_num = 0
        results.append({
            "title": title,
            "price": price_text,
            "price_num": price_num,
            "city": city,
            "date": date,
            "url": link,
            "desc": desc
        })

    # сортировка по цене
    results.sort(key=lambda x: x["price_num"])
    return results

def calculate_profit_analysis(items):
    """Расчет профита - ИСПРАВЛЕННАЯ ВЕРСИЯ"""
    # Собираем все цены
    prices = [item["price_num"] for item in items if item["price_num"] > 0]
    
    if len(prices) < 2:
        return {"avg_price": 0, "min_price": 0, "max_price": 0}
    
    avg_price = sum(prices) / len(prices)
    min_price = min(prices)
    max_price = max(prices)
    
    return {
        "avg_price": avg_price,
        "min_price": min_price,
        "max_price": max_price
    }

# ---------------- Handlers ----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    kb = [[KeyboardButton("Проверить подписку")]]
    if user and user.username == ADMIN_USERNAME:
        kb.append([KeyboardButton("🔐 Admin Panel")])
    await update.message.reply_text("Привет! Я помогу тебе искать объявления на OLX и рассчитывать профит 📊", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = msg.from_user
    text = (msg.text or "").strip()

    # Проверяем состояние админа
    if user.username == ADMIN_USERNAME and user.id in admin_state:
        state = admin_state[user.id]
        
        # Админ вводит username для подписки
        if state.get("waiting_username"):
            username = text.replace("@", "").lower()
            user_id = find_user_by_username(username)
            if user_id:
                state["target_user_id"] = user_id
                state["target_username"] = username
                state["waiting_username"] = False
                state["waiting_days"] = True
                await msg.reply_text(f"✅ Пользователь @{username} найден!\n\n📅 Теперь напишите количество дней для подписки:")
                return
            else:
                await msg.reply_text(f"❌ Пользователь @{username} не найден в базе.\n\n👤 Попробуйте другой username или отмените действие командой /cancel")
                return
        
        # Админ вводит количество дней
        elif state.get("waiting_days"):
            try:
                days = int(text)
                if days <= 0:
                    await msg.reply_text("❌ Количество дней должно быть больше 0")
                    return
                
                target_user_id = state["target_user_id"]
                target_username = state["target_username"]
                
                # Активируем подписку
                subscriptions[target_user_id] = {
                    "active": True,
                    "until": datetime.now() + timedelta(days=days)
                }
                
                # Очищаем состояние
                state["waiting_username"] = False
                state["waiting_days"] = False
                state["target_user_id"] = None
                state["target_username"] = None
                
                await msg.reply_text(
                    f"✅ <b>Подписка активирована!</b>\n\n"
                    f"👤 Пользователь: @{target_username}\n"
                    f"⏰ Срок: {days} дней\n"
                    f"📅 Активна до: {(datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d %H:%M')}",
                    parse_mode="HTML"
                )
                return
                
            except ValueError:
                await msg.reply_text("❌ Введите корректное число дней")
                return

    # логируем запрос
    user_queries.append({
        "id": user.id,
        "username": user.username or "",
        "name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
        "text": text,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    if len(user_queries) > MAX_SAVED_QUERIES:
        user_queries.pop(0)

    # Пользователь проверяет подписку
    if text == "Проверить подписку":
        if user.username == ADMIN_USERNAME:
            await msg.reply_text("✅ Подписка админа всегда активна")
        elif is_subscription_active(user.id):
            sub = subscriptions[user.id]
            await msg.reply_text(f"✅ Подписка активна до {sub['until'].strftime('%Y-%m-%d %H:%M')}")
        else:
            await msg.reply_text("❌ Подписка неактивна")
        return

    # Админ панель
    if text == "🔐 Admin Panel" and user.username == ADMIN_USERNAME:
        await admin_panel_command(update, context)
        return

    if not text:
        await msg.reply_text("Напиши, что искать на OLX 🙂")
        return

    # Проверяем подписку пользователя
    if user.username != ADMIN_USERNAME:
        if not is_subscription_active(user.id):
            # Красивое сообщение с кнопкой на профиль админа
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("💬 Связаться с админом", url="https://t.me/yakattabekov")
            ]])
            await msg.reply_text(
                "🚫 <b>Подписка неактивна</b>\n\n"
                "💎 Для использования бота необходима активная подписка.\n"
                "📞 Свяжитесь с администратором для её активации:",
                parse_mode="HTML",
                reply_markup=keyboard
            )
            return

    await msg.reply_text("🔎 Ищу объявления на OLX...")
    items = search_olx(text)
    if not items:
        await msg.reply_text("😔 Объявлений не найдено. Попробуй другой запрос.")
        return

    # Дополняем цены из HTML если нужно
    for item in items:
        if not item["price"] or item["price"] == "Цена не указана":
            if item.get("url"):
                html_price = fetch_price_from_html(item["url"])
                if html_price: 
                    item["price"] = html_price
                    try:
                        item["price_num"] = float(re.sub(r"[^\d]", "", html_price))
                    except:
                        item["price_num"] = 0

    # Рассчитываем профит-анализ
    analysis = calculate_profit_analysis(items)
    
    # Отправляем сводку
    if analysis["avg_price"] > 0:
        summary = (
            f"📊 <b>Анализ цен по запросу:</b>\n"
            f"💰 Средняя цена: {_format_amount(analysis['avg_price'])}\n"
            f"🟢 Минимальная: {_format_amount(analysis['min_price'])}\n"
            f"🔴 Максимальная: {_format_amount(analysis['max_price'])}\n"
            f"📦 Найдено объявлений: {len(items)}\n\n"
        )
        await msg.reply_text(summary, parse_mode="HTML")

    for item in items:
        # ИСПРАВЛЕННАЯ логика профита
        if item["price_num"] > 0 and analysis["avg_price"] > 0:
            # Профит = насколько можно выгодно купить относительно среднего
            profit_amount = analysis["avg_price"] - item["price_num"]
            profit_percent = (profit_amount / analysis["avg_price"]) * 100
            
            if profit_amount > 1000:  # Цена ниже средней - ВЫГОДНО
                profit_text = f"🟢 +{_format_amount(profit_amount)} (+{profit_percent:.1f}%)"
            elif profit_amount < -1000:  # Цена выше средней - ДОРОГО
                profit_text = f"🔴 {_format_amount(profit_amount)} ({profit_percent:.1f}%)"
            else:  # Цена близка к средней
                profit_text = f"🟡 {_format_amount(profit_amount)} ({profit_percent:.1f}%)"
        else:
            profit_text = "—"

        text_msg = (
            f"📌 <b>{escape(item['title'])}</b>\n"
            f"💰 Цена: <b>{escape(str(item['price']))}</b>\n"
            f"💎 Профит: {profit_text}\n"
            f"📍 Город: {escape(item['city'])}\n"
            f"🗓 Дата: {escape(item['date'])}\n"
            f"📝 {escape(item['desc'])}"
        )
        
        reply_markup = None
        if item.get("url"):
            reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Открыть объявление", url=item["url"])]])

        await msg.reply_text(text_msg, parse_mode="HTML", reply_markup=reply_markup)

# ---------------- Admin Panel ----------------
async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.username != ADMIN_USERNAME:
        await update.message.reply_text("🚫 У тебя нет доступа к админ-панели.")
        return
    
    admin_state[user.id] = {"mode": "main", "page": 0, "waiting_username": False, "waiting_days": False}
    await _send_main_admin_menu(update, user.id)

async def _send_main_admin_menu(update_or_query, user_id, edit=False):
    total_queries = len(user_queries)
    unique_users = get_unique_users()
    active_subs = sum(1 for uid in subscriptions if is_subscription_active(uid))
    
    text = (
        f"🔐 <b>Админ-панель</b>\n\n"
        f"📊 Статистика:\n"
        f"• Всего запросов: {total_queries}\n"
        f"• Уникальных пользователей: {len(unique_users)}\n"
        f"• Активных подписок: {active_subs}\n\n"
        f"🛠 Выберите действие:"
    )
    
    keyboard = [
        [InlineKeyboardButton("📋 Логи пользователей", callback_data="admin_user_logs")],
        [InlineKeyboardButton("🎫 Выдать подписку", callback_data="admin_give_sub")],
        [InlineKeyboardButton("🎫 Управление подписками", callback_data="admin_subs")],
        [InlineKeyboardButton("❌ Закрыть", callback_data="admin_close")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if hasattr(update_or_query, "message"):
        if edit: 
            await update_or_query.message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
        else: 
            await update_or_query.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
    else:
        await update_or_query.edit_message_text(text, parse_mode="HTML", reply_markup=reply_markup)

async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    
    if query.from_user.username != ADMIN_USERNAME:
        await query.answer("🚫 Нет доступа")
        return
    
    await query.answer()
    
    state = admin_state.get(user_id, {"mode": "main", "page": 0})
    
    if data == "admin_close":
        await query.message.delete()
        return
    
    elif data == "admin_back":
        state["mode"] = "main"
        await _send_main_admin_menu(query, user_id, edit=True)
        return
    
    elif data == "admin_user_logs":
        state["mode"] = "user_logs"
        state["page"] = 0
        await _send_users_list(query, user_id, edit=True)
        return
    
    elif data == "admin_give_sub":
        state["waiting_username"] = True
        state["waiting_days"] = False
        await query.edit_message_text(
            "🎫 <b>Выдача подписки</b>\n\n"
            "👤 Напишите username пользователя (без @):",
            parse_mode="HTML"
        )
        return
        
    elif data == "admin_subs":
        state["mode"] = "subs"
        await _send_subs_menu(query, user_id, edit=True)
        return
    
    elif data.startswith("user_select_"):
        selected_user_id = int(data.replace("user_select_", ""))
        state["selected_user"] = selected_user_id
        state["mode"] = "user_messages"
        state["page"] = 0
        await _send_user_messages(query, user_id, selected_user_id, edit=True)
        return
    
    elif data in ["users_prev", "users_next", "messages_prev", "messages_next"]:
        if "prev" in data:
            state["page"] = max(0, state["page"] - 1)
        else:
            state["page"] += 1
            
        if data.startswith("users_"):
            await _send_users_list(query, user_id, edit=True)
        elif data.startswith("messages_"):
            await _send_user_messages(query, user_id, state["selected_user"], edit=True)
        return
    
    elif data == "sub_on_all":
        for user_query in user_queries:
            uid = user_query["id"]
            subscriptions[uid] = {"active": True, "until": datetime.now() + timedelta(days=30)}
        await query.edit_message_text("✅ Всем пользователям активирована подписка на 30 дней!")
        return
    
    elif data == "sub_off_all":
        for uid in subscriptions:
            subscriptions[uid]["active"] = False
        await query.edit_message_text("❌ Все подписки деактивированы!")
        return
    
    admin_state[user_id] = state

async def _send_users_list(query, user_id, edit=True):
    state = admin_state[user_id]
    users = get_unique_users()
    
    page = state["page"]
    total = len(users)
    pages = max(1, (total - 1) // ADMIN_PAGE_SIZE + 1)
    
    start_idx = page * ADMIN_PAGE_SIZE
    end_idx = start_idx + ADMIN_PAGE_SIZE
    users_page = users[start_idx:end_idx]
    
    text = f"👥 <b>Выберите пользователя</b> (страница {page + 1}/{pages})\n\n"
    
    keyboard = []
    for user in users_page:
        status = "✅" if is_subscription_active(user["id"]) else "❌"
        user_text = f"{status} {user['name']} (@{user['username']}) - {user['query_count']} сообщений"
        keyboard.append([InlineKeyboardButton(user_text, callback_data=f"user_select_{user['id']}")])
    
    # Навигация
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data="users_prev"))
    if page < pages - 1:
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data="users_next"))
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="admin_back")])
    
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def _send_user_messages(query, admin_user_id, selected_user_id, edit=True):
    state = admin_state[admin_user_id]
    messages = get_user_queries(selected_user_id)
    
    if not messages:
        await query.edit_message_text("У этого пользователя нет сообщений")
        return
    
    page = state["page"]
    total = len(messages)
    pages = max(1, (total - 1) // ADMIN_PAGE_SIZE + 1)
    
    start_idx = page * ADMIN_PAGE_SIZE
    end_idx = start_idx + ADMIN_PAGE_SIZE
    messages_page = messages[start_idx:end_idx]
    
    user_info = messages[0]  # Берем инфо из первого сообщения
    
    text = f"💬 <b>Сообщения пользователя</b>\n"
    text += f"👤 {user_info['name']} (@{user_info['username']})\n"
    text += f"📄 Страница {page + 1}/{pages}\n\n"
    
    for i, msg in enumerate(messages_page, 1):
        text += f"<b>{i}. {msg['time']}</b>\n"
        text += f"📝 {escape(msg['text'][:150])}{'...' if len(msg['text']) > 150 else ''}\n\n"
    
    keyboard = []
    
    # Навигация
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data="messages_prev"))
    if page < pages - 1:
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data="messages_next"))
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    keyboard.append([InlineKeyboardButton("🔙 К списку пользователей", callback_data="admin_user_logs")])
    
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def _send_subs_menu(query, admin_user_id, edit=True):
    text = (
        f"🎫 <b>Управление подписками</b>\n\n"
        f"Выберите действие:"
    )
    
    keyboard = [
        [InlineKeyboardButton("✅ Активировать всем (30 дней)", callback_data="sub_on_all")],
        [InlineKeyboardButton("❌ Деактивировать всем", callback_data="sub_off_all")],
        [InlineKeyboardButton("🔙 Назад", callback_data="admin_back")]
    ]
    
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# ---------------- Main ----------------
def run_bot():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_admin_callback))
    logging.info("Bot started")
    app.run_polling()

if __name__ == "__main__":
    run_bot()
