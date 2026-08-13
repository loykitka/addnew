import telebot
import sqlite3
import time
import threading
import requests
from telebot import types

TOKEN = "8603057361:AAGCn2s9eUHfbm_I-c9lj8aBgvxZ4489jBY"  # Замените на свой токен
bot = telebot.TeleBot(TOKEN)

# ---------- Работа с базой данных ----------
DB_NAME = "subscriptions.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    # Таблица пользователей (создаётся, если её нет)
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            subscription_end INTEGER,
            trial_used INTEGER DEFAULT 0,
            dlc_tracking_enabled INTEGER DEFAULT 0
        )
    """)

    # Проверяем, есть ли столбец dlc_tracking_enabled (миграция для старых БД)
    c.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in c.fetchall()]
    if 'dlc_tracking_enabled' not in columns:
        c.execute("ALTER TABLE users ADD COLUMN dlc_tracking_enabled INTEGER DEFAULT 0")
        conn.commit()

    # Таблица отслеживаемых игр
    c.execute("""
        CREATE TABLE IF NOT EXISTS tracked_games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            appid INTEGER,
            game_name TEXT,
            last_known_price INTEGER,
            UNIQUE(user_id, appid)
        )
    """)

    # Таблица отслеживаемых DLC
    c.execute("""
        CREATE TABLE IF NOT EXISTS tracked_dlc (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            game_appid INTEGER,
            dlc_appid INTEGER,
            dlc_name TEXT,
            last_known_price INTEGER,
            UNIQUE(user_id, dlc_appid)
        )
    """)

    conn.commit()
    conn.close()

# ---------- Вспомогательные функции БД ----------
def get_user(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row

def upsert_user(user_id, username, first_name):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        INSERT INTO users (user_id, username, first_name, subscription_end, trial_used, dlc_tracking_enabled)
        VALUES (?, ?, ?, 0, 0, 0)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name
    """, (user_id, username, first_name))
    conn.commit()
    conn.close()

def set_subscription(user_id, days):
    now = int(time.time())
    row = get_user(user_id)
    if row and row[3] and row[3] > now:
        new_end = row[3] + days * 86400
    else:
        new_end = now + days * 86400
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET subscription_end = ? WHERE user_id = ?", (new_end, user_id))
    conn.commit()
    conn.close()

def set_trial_used(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET trial_used = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def activate_trial(user_id):
    row = get_user(user_id)
    if not row:
        return False, "Пользователь не найден"
    if row[4] == 1:
        return False, "Вы уже использовали пробный период"
    now = int(time.time())
    if row[3] and row[3] > now:
        new_end = row[3] + 86400
    else:
        new_end = now + 86400
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET subscription_end = ?, trial_used = 1 WHERE user_id = ?",
              (new_end, user_id))
    conn.commit()
    conn.close()
    return True, "Пробный период на 1 день активирован!"

def get_profile_text(user_id):
    row = get_user(user_id)
    if not row:
        return "Профиль не найден. Нажмите /start"
    username = row[1] if row[1] else "не указан"
    first_name = row[2] if row[2] else "не указано"
    subscription_end = row[3]
    now = int(time.time())
    if subscription_end and subscription_end > now:
        days_left = (subscription_end - now) // 86400
        sub_info = f"✅ Активна\nОсталось дней: {days_left}\nИстекает: {time.ctime(subscription_end)}"
    else:
        sub_info = "❌ Нет активной подписки"
    return (
        f"👤 Профиль\n"
        f"ID: <code>{user_id}</code>\n"
        f"Имя: {first_name}\n"
        f"Username: @{username}\n"
        f"Подписка: {sub_info}"
    )

# ---------- Функции для работы с играми ----------
def add_game(user_id, appid):
    try:
        appid = int(appid)
    except:
        return False, "AppID должен быть числом"
    game_info = get_game_info(appid)
    if not game_info or "name" not in game_info or "price" not in game_info:
        return False, "Не удалось получить информацию об игре. Проверьте AppID."
    name = game_info["name"]
    price = game_info["price"]  # в копейках
    if price == 0:
        return False, "Игра бесплатная, отслеживание невозможно 67."
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO tracked_games (user_id, appid, game_name, last_known_price)
            VALUES (?, ?, ?, ?)
        """, (user_id, appid, name, price))
        conn.commit()
        return True, f"Игра '{name}' добавлена в отслеживание."
    except sqlite3.IntegrityError:
        return False, "Эта игра уже отслеживается."
    finally:
        conn.close()

def remove_game(user_id, appid):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM tracked_games WHERE user_id = ? AND appid = ?", (user_id, appid))
    conn.commit()
    conn.close()

def get_user_games(user_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT appid, game_name, last_known_price FROM tracked_games WHERE user_id = ?", (user_id,))
    rows = c.fetchall()
    conn.close()
    return rows

def toggle_dlc_tracking(user_id):
    row = get_user(user_id)
    if not row:
        return False, "Пользователь не найден"
    new_val = 1 if row[5] == 0 else 0
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE users SET dlc_tracking_enabled = ? WHERE user_id = ?", (new_val, user_id))
    conn.commit()
    conn.close()
    return True, f"Учёт DLC теперь {'включен' if new_val else 'выключен'}."

def get_dlc_tracking_status(user_id):
    row = get_user(user_id)
    if not row:
        return False
    return bool(row[5])

# ---------- Steam API ----------
def get_game_info(appid):
    url = f"https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic,price_overview&cc=ru&l=ru"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if not data or str(appid) not in data or not data[str(appid)]["success"]:
            return None
        app_data = data[str(appid)]["data"]
        if "name" not in app_data:
            return None
        price = 0
        if "price_overview" in app_data and app_data["price_overview"]:
            price = app_data["price_overview"]["final"]
        return {"name": app_data["name"], "price": price}
    except Exception as e:
        print(f"Error fetching game {appid}: {e}")
        return None

def get_dlc_list(game_appid):
    url = f"https://store.steampowered.com/api/appdetails?appids={game_appid}&filters=dlc&cc=ru&l=ru"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if not data or str(game_appid) not in data or not data[str(game_appid)]["success"]:
            return []
        return data[str(game_appid)]["data"].get("dlc", [])
    except Exception as e:
        print(f"Error fetching DLC for {game_appid}: {e}")
        return []

def get_dlc_price(dlc_appid):
    info = get_game_info(dlc_appid)
    if info and "price" in info:
        return info["price"]
    return None

# ---------- Проверка цен ----------
def check_price_updates():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT user_id, appid, game_name, last_known_price FROM tracked_games")
    games = c.fetchall()
    conn.close()

    for user_id, appid, game_name, last_price in games:
        try:
            current_info = get_game_info(appid)
            if not current_info:
                continue
            current_price = current_info["price"]
            if current_price < last_price:
                discount = last_price - current_price
                try:
                    bot.send_message(
                        user_id,
                        f"🔥 Цена на игру <b>{game_name}</b> упала!\n"
                        f"Старая цена: {last_price/100:.2f} руб.\n"
                        f"Новая цена: {current_price/100:.2f} руб.\n"
                        f"Скидка: {discount/100:.2f} руб.",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    print(f"Failed to notify user {user_id}: {e}")
                conn = sqlite3.connect(DB_NAME)
                c = conn.cursor()
                c.execute("UPDATE tracked_games SET last_known_price = ? WHERE user_id = ? AND appid = ?",
                          (current_price, user_id, appid))
                conn.commit()
                conn.close()
        except Exception as e:
            print(f"Error processing game {appid} for user {user_id}: {e}")

        if get_dlc_tracking_status(user_id):
            try:
                dlc_list = get_dlc_list(appid)
                for dlc_appid in dlc_list:
                    dlc_price = get_dlc_price(dlc_appid)
                    if dlc_price is None:
                        continue
                    conn = sqlite3.connect(DB_NAME)
                    c = conn.cursor()
                    c.execute("SELECT last_known_price FROM tracked_dlc WHERE user_id = ? AND dlc_appid = ?",
                              (user_id, dlc_appid))
                    row = c.fetchone()
                    if row:
                        last_dlc_price = row[0]
                        if dlc_price < last_dlc_price:
                            dlc_name = get_game_info(dlc_appid)["name"] if get_game_info(dlc_appid) else str(dlc_appid)
                            discount = last_dlc_price - dlc_price
                            try:
                                bot.send_message(
                                    user_id,
                                    f"🔥 Цена на DLC <b>{dlc_name}</b> для игры <b>{game_name}</b> упала!\n"
                                    f"Старая цена: {last_dlc_price/100:.2f} руб.\n"
                                    f"Новая цена: {dlc_price/100:.2f} руб.\n"
                                    f"Скидка: {discount/100:.2f} руб.",
                                    parse_mode="HTML"
                                )
                            except Exception as e:
                                print(f"Failed to notify user {user_id} about DLC: {e}")
                            c.execute("UPDATE tracked_dlc SET last_known_price = ? WHERE user_id = ? AND dlc_appid = ?",
                                      (dlc_price, user_id, dlc_appid))
                            conn.commit()
                    else:
                        dlc_name = get_game_info(dlc_appid)["name"] if get_game_info(dlc_appid) else str(dlc_appid)
                        c.execute("""
                            INSERT INTO tracked_dlc (user_id, game_appid, dlc_appid, dlc_name, last_known_price)
                            VALUES (?, ?, ?, ?, ?)
                        """, (user_id, appid, dlc_appid, dlc_name, dlc_price))
                        conn.commit()
                    conn.close()
            except Exception as e:
                print(f"Error processing DLC for game {appid}: {e}")

def price_checker_loop():
    while True:
        try:
            check_price_updates()
        except Exception as e:
            print(f"Price check error: {e}")
        time.sleep(6 * 3600)  # каждые 6 часов

# ---------- Клавиатуры ----------
def main_keyboard(user_id):
    row = get_user(user_id)
    markup = types.InlineKeyboardMarkup()
    if row and row[3] and row[3] > int(time.time()):
        # Подписка активна
        btn_games = types.InlineKeyboardButton("🎮 Игра", callback_data="games")
        btn_settings = types.InlineKeyboardButton("⚙️ Настройки", callback_data="settings")
        btn_profile = types.InlineKeyboardButton("📋 Профиль", callback_data="profile")
        btn_support = types.InlineKeyboardButton("🛟 Тех. поддержка", callback_data="support")
        markup.add(btn_games)
        markup.add(btn_settings, btn_profile)
        markup.add(btn_support)
    else:
        # Нет подписки
        btn_pay = types.InlineKeyboardButton("💳 Оплатить 30 дн. / 100⭐", callback_data="pay_sub")
        btn_trial = types.InlineKeyboardButton("🎁 Пробный 1 день", callback_data="trial")
        btn_profile = types.InlineKeyboardButton("📋 Профиль", callback_data="profile")
        markup.add(btn_pay)
        markup.add(btn_trial)
        markup.add(btn_profile)
    return markup

def back_keyboard():
    markup = types.InlineKeyboardMarkup()
    btn_back = types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
    markup.add(btn_back)
    return markup

def games_keyboard():
    markup = types.InlineKeyboardMarkup()
    btn_add = types.InlineKeyboardButton("➕ Добавить игру", callback_data="add_game")
    btn_list = types.InlineKeyboardButton("📃 Мои игры", callback_data="list_games")
    btn_back = types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
    markup.add(btn_add, btn_list)
    markup.add(btn_back)
    return markup

def settings_keyboard(user_id):
    markup = types.InlineKeyboardMarkup()
    status = get_dlc_tracking_status(user_id)
    status_text = "✅ Включено" if status else "❌ Выключено"
    btn_toggle = types.InlineKeyboardButton(f"Учитывать DLC: {status_text}", callback_data="toggle_dlc")
    btn_back = types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
    markup.add(btn_toggle)
    markup.add(btn_back)
    return markup

# ---------- Команда /start ----------
@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    upsert_user(user_id, username, first_name)

    name = first_name or username or "пользователь"
    text = f"Привет, {name}!\nВыберите действие:"
    bot.send_message(message.chat.id, text, reply_markup=main_keyboard(user_id))

# ---------- Обработчики кнопок ----------
@bot.callback_query_handler(func=lambda call: call.data == "pay_sub")
def callback_pay(call):
    prices = [telebot.types.LabeledPrice(label="30 дней подписки", amount=100)]
    bot.send_invoice(
        chat_id=call.message.chat.id,
        title="Подписка на 30 дней",
        description="Оплатите 100 Telegram Stars для активации подписки на 30 дней.",
        invoice_payload="sub_30_days",
        provider_token="",
        currency="XTR",
        prices=prices
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "trial")
def callback_trial(call):
    user_id = call.from_user.id
    success, msg = activate_trial(user_id)
    bot.answer_callback_query(call.id, text=msg, show_alert=True)
    if success:
        bot.send_message(call.message.chat.id, "🎉 Вы активировали пробный период на 1 день!")
        try:
            bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=main_keyboard(user_id)
            )
        except:
            pass

@bot.callback_query_handler(func=lambda call: call.data == "profile")
def callback_profile(call):
    profile_text = get_profile_text(call.from_user.id)
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=profile_text,
        parse_mode="HTML",
        reply_markup=back_keyboard()
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "back_to_main")
def callback_back(call):
    user_id = call.from_user.id
    row = get_user(user_id)
    if row:
        first_name = row[2] if row[2] else ""
        username = row[1] if row[1] else ""
        name = first_name or username or "пользователь"
        text = f"Привет, {name}!\nВыберите действие:"
    else:
        text = "Привет!\nВыберите действие:"
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=text,
        reply_markup=main_keyboard(user_id)
    )
    bot.answer_callback_query(call.id)

# ---------- Новые обработчики: Игра, Настройки, Поддержка ----------
@bot.callback_query_handler(func=lambda call: call.data == "games")
def callback_games(call):
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="🎮 Управление отслеживанием игр:",
        reply_markup=games_keyboard()
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "add_game")
def callback_add_game(call):
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="Введите AppID игры.\n\n"
             "Инструкция: откройте страницу игры в Steam, в адресной строке будет число после /app/.\n"
             "Например, для CS:GO это 730.",
        reply_markup=None
    )
    bot.answer_callback_query(call.id)
    bot.register_next_step_handler(call.message, process_appid_input, user_id=call.from_user.id)

def process_appid_input(message, user_id):
    appid_text = message.text.strip()
    success, msg = add_game(user_id, appid_text)
    bot.send_message(message.chat.id, msg)
    text = "Выберите действие:"
    bot.send_message(message.chat.id, text, reply_markup=main_keyboard(user_id))

@bot.callback_query_handler(func=lambda call: call.data == "list_games")
def callback_list_games(call):
    user_id = call.from_user.id
    games = get_user_games(user_id)
    if not games:
        text = "У вас нет отслеживаемых игр."
    else:
        text = "📃 Ваши отслеживаемые игры:\n\n"
        for appid, name, price in games:
            text += f"• {name} (AppID: {appid}) — текущая цена: {price/100:.2f} руб.\n"
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=text,
        reply_markup=back_keyboard()
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "settings")
def callback_settings(call):
    user_id = call.from_user.id
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="⚙️ Настройки:",
        reply_markup=settings_keyboard(user_id)
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "toggle_dlc")
def callback_toggle_dlc(call):
    user_id = call.from_user.id
    success, msg = toggle_dlc_tracking(user_id)
    bot.answer_callback_query(call.id, text=msg, show_alert=True)
    bot.edit_message_reply_markup(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=settings_keyboard(user_id)
    )

@bot.callback_query_handler(func=lambda call: call.data == "support")
def callback_support(call):
    text = "🛟 Техническая поддержка:\n"
    text += "По всем вопросам обращайтесь: @rat4ei"  # замените на свой контакт
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=text,
        reply_markup=back_keyboard()
    )
    bot.answer_callback_query(call.id)

# ---------- Обработка платежей ----------
@bot.pre_checkout_query_handler(func=lambda query: True)
def pre_checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True,
                                  error_message="Что-то пошло не так, попробуйте позже")

@bot.message_handler(content_types=['successful_payment'])
def successful_payment(message):
    payload = message.successful_payment.invoice_payload
    user_id = message.from_user.id
    if payload == "sub_30_days":
        set_subscription(user_id, days=30)
        bot.send_message(message.chat.id, "✅ Спасибо за оплату! Подписка на 30 дней активирована.")
    else:
        bot.send_message(message.chat.id, "⚠️ Неизвестный тип платежа.")
    upsert_user(user_id, message.from_user.username, message.from_user.first_name)

# ---------- Запуск ----------
if __name__ == "__main__":
    init_db()
    print("Бот запущен...")

    # Запускаем фоновый поток проверки цен
    price_thread = threading.Thread(target=price_checker_loop, daemon=True)
    price_thread.start()

    bot.infinity_polling()
