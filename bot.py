import sqlite3
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
import asyncio
import aiohttp
import re

__version__ = "0.5.0"

# ===== КОНФИГ =====
def load_env(path: Path = Path(__file__).with_name(".env")) -> None:
    """Читает .env в os.environ, не затирая уже заданные переменные окружения."""
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise SystemExit(
        "Не задан TELEGRAM_BOT_TOKEN.\n"
        "Создайте файл .env рядом с bot.py и добавьте строку:\n"
        "TELEGRAM_BOT_TOKEN=<токен от BotFather>"
    )

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ===== БАЗА ДАННЫХ SQLITE =====
class Database:
    def __init__(self, db_name="calorie_bot.db"):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self.create_tables()

    def create_tables(self):
        # Пользователи
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE,
                username TEXT,
                first_name TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                weight REAL,
                height REAL,
                age INTEGER,
                activity REAL,
                daily_norm INTEGER,
                is_premium BOOLEAN DEFAULT 0,
                referrer_id INTEGER,
                streak_days INTEGER DEFAULT 0,
                last_active DATE
            )
        ''')

        # Продукты
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS foods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT,
                calories REAL,
                protein REAL DEFAULT 0,
                fat REAL DEFAULT 0,
                carbs REAL DEFAULT 0,
                is_global BOOLEAN DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')

        # Дневник питания
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS diary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                date DATE,
                product_id INTEGER,
                product_name TEXT,
                grams REAL,
                calories REAL,
                time TIME,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')

        # Друзья
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS friends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                friend_id INTEGER,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(friend_id) REFERENCES users(id)
            )
        ''')

        # Достижения
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                achievement_type TEXT,
                earned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        ''')

        self.conn.commit()

    # ---- ПОЛЬЗОВАТЕЛИ ----
    def get_or_create_user(self, telegram_id, username, first_name):
        self.cursor.execute(
            "SELECT id FROM users WHERE telegram_id = ?",
            (telegram_id,)
        )
        user = self.cursor.fetchone()

        if user:
            self.cursor.execute(
                "UPDATE users SET last_active = ? WHERE telegram_id = ?",
                (datetime.now().date(), telegram_id)
            )
            self.conn.commit()
            return user[0]
        else:
            self.cursor.execute('''
                INSERT INTO users (telegram_id, username, first_name, last_active)
                VALUES (?, ?, ?, ?)
            ''', (telegram_id, username, first_name, datetime.now().date()))
            self.conn.commit()
            return self.cursor.lastrowid

    def update_user_profile(self, user_id, weight, height, age, activity):
        daily_norm = int((10 * weight + 6.25 * height - 5 * age - 161) * activity)
        self.cursor.execute('''
            UPDATE users
            SET weight = ?, height = ?, age = ?, activity = ?, daily_norm = ?
            WHERE id = ?
        ''', (weight, height, age, activity, daily_norm, user_id))
        self.conn.commit()
        return daily_norm

    def get_user_profile(self, user_id):
        self.cursor.execute(
            "SELECT weight, height, age, activity, daily_norm, is_premium FROM users WHERE id = ?",
            (user_id,)
        )
        return self.cursor.fetchone()

    def get_user_id(self, telegram_id):
        self.cursor.execute(
            "SELECT id FROM users WHERE telegram_id = ?",
            (telegram_id,)
        )
        result = self.cursor.fetchone()
        return result[0] if result else None

    # ---- ПРОДУКТЫ ----
    def add_food(self, user_id, name, calories, protein=0, fat=0, carbs=0, is_global=False):
        self.cursor.execute('''
            INSERT INTO foods (user_id, name, calories, protein, fat, carbs, is_global)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, name, calories, protein, fat, carbs, is_global))
        self.conn.commit()
        return self.cursor.lastrowid

    def get_food(self, user_id, name):
        self.cursor.execute(
            "SELECT id, calories, protein, fat, carbs FROM foods WHERE user_id = ? AND name = ?",
            (user_id, name)
        )
        return self.cursor.fetchone()

    def get_user_foods(self, user_id):
        self.cursor.execute(
            "SELECT name, calories, protein, fat, carbs FROM foods WHERE user_id = ? ORDER BY name",
            (user_id,)
        )
        return self.cursor.fetchall()

    def delete_food(self, user_id, name):
        self.cursor.execute(
            "DELETE FROM foods WHERE user_id = ? AND name = ?",
            (user_id, name)
        )
        self.conn.commit()
        return self.cursor.rowcount > 0

    # ---- ДНЕВНИК ----
    def add_meal(self, user_id, date, product_name, grams, calories, time=None):
        if time is None:
            time = datetime.now().strftime("%H:%M")

        self.cursor.execute('''
            INSERT INTO diary (user_id, date, product_name, grams, calories, time)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, date, product_name, grams, calories, time))
        self.conn.commit()
        return self.cursor.lastrowid

    def get_daily_meals(self, user_id, date):
        self.cursor.execute(
            "SELECT product_name, grams, calories, time FROM diary WHERE user_id = ? AND date = ? ORDER BY time",
            (user_id, date)
        )
        return self.cursor.fetchall()

    def get_daily_total(self, user_id, date):
        self.cursor.execute(
            "SELECT COALESCE(SUM(calories), 0) FROM diary WHERE user_id = ? AND date = ?",
            (user_id, date)
        )
        return self.cursor.fetchone()[0]

    def get_weekly_stats(self, user_id, days=7):
        self.cursor.execute('''
            SELECT date, COALESCE(SUM(calories), 0)
            FROM diary
            WHERE user_id = ? AND date >= date('now', ?)
            GROUP BY date
        ''', (user_id, f'-{days-1} days'))
        return self.cursor.fetchall()

    def clear_daily_history(self, user_id, date):
        self.cursor.execute(
            "DELETE FROM diary WHERE user_id = ? AND date = ?",
            (user_id, date)
        )
        self.conn.commit()

    # ---- ДРУЗЬЯ ----
    def add_friend_request(self, user_id, friend_id):
        self.cursor.execute(
            "INSERT INTO friends (user_id, friend_id) VALUES (?, ?)",
            (user_id, friend_id)
        )
        self.conn.commit()

    def accept_friend_request(self, user_id, friend_id):
        self.cursor.execute(
            "UPDATE friends SET status = 'accepted' WHERE user_id = ? AND friend_id = ?",
            (friend_id, user_id)
        )
        self.conn.commit()

    def get_friend_requests(self, user_id):
        self.cursor.execute('''
            SELECT u.id, u.username, u.first_name
            FROM friends f
            JOIN users u ON f.user_id = u.id
            WHERE f.friend_id = ? AND f.status = 'pending'
        ''', (user_id,))
        return self.cursor.fetchall()

    def get_friends(self, user_id):
        self.cursor.execute('''
            SELECT u.id, u.username, u.first_name, u.daily_norm
            FROM friends f
            JOIN users u ON f.friend_id = u.id
            WHERE f.user_id = ? AND f.status = 'accepted'
            UNION
            SELECT u.id, u.username, u.first_name, u.daily_norm
            FROM friends f
            JOIN users u ON f.user_id = u.id
            WHERE f.friend_id = ? AND f.status = 'accepted'
        ''', (user_id, user_id))
        return self.cursor.fetchall()

    def get_friend_leaderboard(self, user_id, days=7):
        """Топ друзей за неделю"""
        friends = self.get_friends(user_id)
        if not friends:
            return []

        friend_ids = [f[0] for f in friends]
        if not friend_ids:
            return []

        placeholders = ','.join(['?'] * len(friend_ids))
        self.cursor.execute(f'''
            SELECT u.id, u.username, u.first_name, u.daily_norm,
                   COALESCE(SUM(d.calories), 0) as week_calories,
                   COUNT(DISTINCT d.date) as days_logged
            FROM users u
            LEFT JOIN diary d ON d.user_id = u.id
                AND d.date >= date('now', ?)
            WHERE u.id IN ({placeholders})
            GROUP BY u.id
            ORDER BY week_calories DESC
        ''', (f'-{days-1} days', *friend_ids))

        return self.cursor.fetchall()

    # ---- ДОСТИЖЕНИЯ ----
    def add_achievement(self, user_id, achievement_type):
        self.cursor.execute(
            "INSERT INTO achievements (user_id, achievement_type) VALUES (?, ?)",
            (user_id, achievement_type)
        )
        self.conn.commit()

    def get_achievements(self, user_id):
        self.cursor.execute(
            "SELECT achievement_type, earned_at FROM achievements WHERE user_id = ?",
            (user_id,)
        )
        return self.cursor.fetchall()

    def has_achievement(self, user_id, achievement_type):
        self.cursor.execute(
            "SELECT id FROM achievements WHERE user_id = ? AND achievement_type = ?",
            (user_id, achievement_type)
        )
        return self.cursor.fetchone() is not None

# Инициализация БД
db = Database()

# ===== FSM СОСТОЯНИЯ =====
class AddFoodState(StatesGroup):
    waiting_for_product = State()
    waiting_for_calories = State()

class AddMealState(StatesGroup):
    waiting_for_product_name = State()
    waiting_for_grams = State()
    waiting_for_product_selection = State()

class SetupState(StatesGroup):
    waiting_for_weight = State()
    waiting_for_height = State()
    waiting_for_age = State()
    waiting_for_activity = State()

class FriendState(StatesGroup):
    waiting_for_friend_username = State()

# ===== ДОСТИЖЕНИЯ =====
ACHIEVEMENTS = {
    'first_meal': {'name': '🍽️ Первый шаг', 'desc': 'Добавил первый приём пищи'},
    'week_streak': {'name': '📅 Недельный стаж', 'desc': 'Заполнял дневник 7 дней подряд'},
    'perfect_day': {'name': '🎯 Идеальный день', 'desc': 'Попал в норму калорий (95-105%)'},
    'food_explorer': {'name': '🧭 Исследователь', 'desc': 'Добавил 10 разных продуктов'},
    'social_butterfly': {'name': '🦋 Социальный', 'desc': 'Добавил 3 друзей'},
    'weight_master': {'name': '🏋️ Мастер веса', 'desc': 'Скинул 5 кг (заглушка)'},
}

# ===== КЛАВИАТУРЫ =====

def get_main_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Сегодня")],
            [KeyboardButton(text="📈 Неделя")],
            [KeyboardButton(text="➕ Добавить приём пищи")],
            [KeyboardButton(text="📋 История")],
            [KeyboardButton(text="👥 Друзья")],  # Новая кнопка
            [KeyboardButton(text="⚙️ Настройки")]
        ],
        resize_keyboard=True
    )
    return keyboard

def get_settings_keyboard():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Мои параметры", callback_data="view_profile")],
        [InlineKeyboardButton(text="📝 Изменить параметры", callback_data="edit_profile")],
        [InlineKeyboardButton(text="📦 Мои продукты", callback_data="view_foods")],
        [InlineKeyboardButton(text="➕ Добавить продукт", callback_data="add_food")],
        [InlineKeyboardButton(text="🗑️ Очистить историю", callback_data="clear_history")],
        [InlineKeyboardButton(text="🏆 Достижения", callback_data="view_achievements")],  # Новая
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])
    return keyboard

def get_friends_keyboard():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить друга", callback_data="add_friend")],
        [InlineKeyboardButton(text="📋 Запросы", callback_data="view_requests")],
        [InlineKeyboardButton(text="🏆 Рейтинг друзей", callback_data="friend_leaderboard")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])
    return keyboard

def get_meal_actions_keyboard():
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить ещё", callback_data="add_again")],
        [InlineKeyboardButton(text="📊 Сегодня", callback_data="show_today")],
        [InlineKeyboardButton(text="📈 Неделя", callback_data="show_week")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu")]
    ])
    return keyboard

def get_foods_keyboard(user_id, page=0):
    foods = db.get_user_foods(user_id)
    if not foods:
        return None

    items_per_page = 10
    total_pages = (len(foods) + items_per_page - 1) // items_per_page
    start = page * items_per_page
    end = min(start + items_per_page, len(foods))

    keyboard = []
    for name, calories, protein, fat, carbs in foods[start:end]:
        keyboard.append([InlineKeyboardButton(
            text=f"{name.title()} - {calories} ккал/100г",
            callback_data=f"food_{name}"
        )])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"foods_page_{page-1}"))
    nav_buttons.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"foods_page_{page+1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_settings")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_product_selection_keyboard(products):
    keyboard = []
    for i, p in enumerate(products, 1):
        name = p['name'][:20] + '...' if len(p['name']) > 20 else p['name']
        button_text = f"{i}. {name} - {p['calories']} ккал"
        keyboard.append([InlineKeyboardButton(
            text=button_text,
            callback_data=f"select_product_{i-1}"
        )])

    keyboard.append([InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="manual_add")])
    keyboard.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel_search")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# ===== ФУНКЦИИ ДЛЯ РАБОТЫ С OPENFOODFACTS =====

async def search_openfoodfacts(query):
    url = f"https://world.openfoodfacts.org/cgi/search.pl"
    params = {
        'search_terms': query,
        'search_simple': 1,
        'action': 'process',
        'json': 1,
        'page_size': 10,
        'lc': 'ru'
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    products = data.get('products', [])
                    results = []

                    for product in products[:5]:
                        if product.get('product_name'):
                            nutriments = product.get('nutriments', {})
                            calories = nutriments.get('energy-kcal_100g') or nutriments.get('energy-kcal')
                            protein = nutriments.get('proteins_100g') or nutriments.get('proteins')
                            fat = nutriments.get('fat_100g') or nutriments.get('fat')
                            carbs = nutriments.get('carbohydrates_100g') or nutriments.get('carbohydrates')

                            if calories and calories > 0:
                                results.append({
                                    'name': product.get('product_name', '')[:50],
                                    'calories': round(float(calories), 1),
                                    'protein': round(float(protein), 1) if protein else 0,
                                    'fat': round(float(fat), 1) if fat else 0,
                                    'carbs': round(float(carbs), 1) if carbs else 0,
                                    'brand': product.get('brands', 'Неизвестный бренд')[:30],
                                })

                    return results
    except Exception as e:
        print(f"Ошибка при запросе к OpenFoodFacts: {e}")
        return []

def format_search_results(products):
    if not products:
        return "❌ Продукты не найдены. Попробуйте другое название или введите вручную."

    text = "🔍 *Найдено продуктов:*\n\n"
    for i, p in enumerate(products, 1):
        text += f"{i}. *{p['name']}*\n"
        text += f"   🏷️ {p['brand']}\n"
        text += f"   🔥 {p['calories']} ккал | 🥩 {p['protein']}г | 🧈 {p['fat']}г | 🍚 {p['carbs']}г\n\n"

    text += "Выберите номер продукта:"
    return text

# ===== ФУНКЦИИ ДЛЯ ВИЗУАЛИЗАЦИИ =====

def get_progress_bar(percent):
    bar_length = 20
    filled = int(bar_length * percent / 100)

    if percent < 50:
        color = "🟢"
    elif percent < 80:
        color = "🟡"
    elif percent < 100:
        color = "🟠"
    else:
        color = "🔴"

    bar = color * filled + "⬜" * (bar_length - filled)
    return bar

def get_mood_emoji(percent, remaining):
    if percent < 30:
        return "😊"
    elif percent < 70:
        return "🙂"
    elif percent < 100:
        return "🤔"
    elif percent < 120:
        return "😅"
    else:
        return "😰"

def get_advice(percent, remaining):
    if remaining > 500:
        return "💡 Можно позволить себе плотный ужин!"
    elif remaining > 200:
        return "💡 Хорошо, можно перекусить фруктами"
    elif remaining > 50:
        return "💡 Легкий перекус: овощи или йогурт"
    elif remaining > -100:
        return "💡 Отлично! Вы в норме!"
    else:
        return "⚠️ Сегодня перебор. Завтра будьте аккуратнее!"

def get_week_dates():
    today = datetime.now().date()
    week_dates = []

    for i in range(6, -1, -1):
        date = today - timedelta(days=i)
        week_dates.append(date)

    return week_dates

def get_day_emoji(day_of_week):
    days = {
        0: "🌙", 1: "🔥", 2: "⭐", 3: "💪", 4: "🎉", 5: "🌅", 6: "😎"
    }
    return days.get(day_of_week, "📅")

def get_day_name_short(day_of_week):
    days = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
    return days[day_of_week]

def create_weekly_chart(weekly_data, daily_norm):
    max_value = max([day['calories'] for day in weekly_data] + [daily_norm])
    min_value = min([day['calories'] for day in weekly_data] + [0])
    range_value = max(max_value - min_value, 100)
    chart_height = 10

    chart_lines = []

    # Верхняя линия с нормой
    norm_line = "📈 Норма: "
    for day in weekly_data:
        if day['calories'] >= daily_norm * 0.9 and day['calories'] <= daily_norm * 1.1:
            norm_line += "✅ "
        elif day['calories'] > daily_norm:
            norm_line += "⬆️ "
        else:
            norm_line += "⬇️ "
    chart_lines.append(norm_line)
    chart_lines.append("")

    # Строим столбчатый график
    for level in range(chart_height, 0, -1):
        threshold = min_value + (range_value * level / chart_height)
        line = f"{' ' * 4}│"

        for day in weekly_data:
            value = day['calories']
            if value >= threshold:
                if value > daily_norm * 1.1:
                    line += "█"
                elif value >= daily_norm * 0.8:
                    line += "▓"
                elif value > 0:
                    line += "▒"
                else:
                    line += "░"
            else:
                line += " "

        chart_lines.append(line)

    # Нижняя линия
    bottom_line = " " * 4 + "└"
    for i in range(7):
        if i == 6:
            bottom_line += "─" * 2
        else:
            bottom_line += "─" * 3
    chart_lines.append(bottom_line)

    # Подписи дней
    days_line = " " * 4 + " "
    for day in weekly_data:
        days_line += f" {day['emoji']}{day['day_name']}"
    chart_lines.append(days_line)

    # Калории
    cal_line = " " * 4 + "  "
    for day in weekly_data:
        if day['calories'] > 0:
            cal_line += f"{int(day['calories']):>3}"
        else:
            cal_line += "  -"
    chart_lines.append(cal_line)

    return "\n".join(chart_lines)

# ===== ДОСТИЖЕНИЯ (ПРОВЕРКА) =====

async def check_achievements(user_id, action_type, data=None):
    """Проверяет и выдает достижения"""
    new_achievements = []

    if action_type == 'add_meal':
        # Первый приём пищи
        if not db.has_achievement(user_id, 'first_meal'):
            db.add_achievement(user_id, 'first_meal')
            new_achievements.append('first_meal')

        # Проверяем количество разных продуктов
        if not db.has_achievement(user_id, 'food_explorer'):
            db.cursor.execute(
                "SELECT COUNT(DISTINCT product_name) FROM diary WHERE user_id = ?",
                (user_id,)
            )
            count = db.cursor.fetchone()[0]
            if count >= 10:
                db.add_achievement(user_id, 'food_explorer')
                new_achievements.append('food_explorer')

    if action_type == 'check_day' and data:
        # Идеальный день (95-105% от нормы)
        if not db.has_achievement(user_id, 'perfect_day'):
            total = data.get('total', 0)
            norm = data.get('norm', 1)
            percent = total / norm * 100 if norm > 0 else 0
            if 95 <= percent <= 105:
                db.add_achievement(user_id, 'perfect_day')
                new_achievements.append('perfect_day')

    if action_type == 'add_friend':
        # Социальная бабочка
        if not db.has_achievement(user_id, 'social_butterfly'):
            friends = db.get_friends(user_id)
            if len(friends) >= 3:
                db.add_achievement(user_id, 'social_butterfly')
                new_achievements.append('social_butterfly')

    return new_achievements

# ===== ОБРАБОТЧИКИ СООБЩЕНИЙ =====

@dp.message(Command("start"))
async def start(message: Message):
    telegram_id = message.from_user.id
    username = message.from_user.username or str(telegram_id)
    first_name = message.from_user.first_name or "Пользователь"

    user_id = db.get_or_create_user(telegram_id, username, first_name)

    profile = db.get_user_profile(user_id)

    if profile and profile[0] is not None:
        weight, height, age, activity, daily_norm, is_premium = profile
        await message.answer(
            f"👋 С возвращением, {first_name}!\n"
            f"🔥 Дневная норма: {daily_norm} ккал",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer(
            "👋 Привет! Я бот-трекер калорий.\n\n"
            "Давай сначала настроим твой профиль. Нажми кнопку ниже:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚙️ Настроить профиль", callback_data="start_setup")]
            ])
        )

@dp.message(lambda message: message.text == "📊 Сегодня")
async def show_today_from_menu(message: Message):
    await show_today(message.from_user.id, message)

@dp.message(lambda message: message.text == "📈 Неделя")
async def show_week_from_menu(message: Message):
    await show_week(message.from_user.id, message)

@dp.message(lambda message: message.text == "➕ Добавить приём пищи")
async def add_meal_from_menu(message: Message, state: FSMContext):
    await state.set_state(AddMealState.waiting_for_product_name)
    await message.answer(
        "🍽️ Введите название продукта:\n\n"
        "Бот автоматически найдет продукт в OpenFoodFacts\n"
        "Или можете ввести название вручную",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="🔙 Отмена")]],
            resize_keyboard=True
        )
    )

@dp.message(lambda message: message.text == "📋 История")
async def show_history_from_menu(message: Message):
    await show_history(message.from_user.id, message)

@dp.message(lambda message: message.text == "👥 Друзья")
async def friends_menu(message: Message):
    user_id = db.get_user_id(message.from_user.id)
    friends = db.get_friends(user_id)

    text = "👥 *Друзья*\n\n"
    if friends:
        text += f"У вас {len(friends)} друг(ей):\n"
        for friend_id, username, first_name, norm in friends:
            text += f"• {first_name} (@{username})\n"
    else:
        text += "У вас пока нет друзей. Добавьте первого друга!\n"

    # Проверяем запросы
    requests = db.get_friend_requests(user_id)
    if requests:
        text += f"\n📩 *Новых запросов: {len(requests)}*"

    await message.answer(text, parse_mode="Markdown", reply_markup=get_friends_keyboard())

@dp.message(lambda message: message.text == "⚙️ Настройки")
async def settings_menu(message: Message):
    await message.answer(
        "⚙️ Настройки:",
        reply_markup=get_settings_keyboard()
    )

@dp.message(lambda message: message.text == "🔙 Отмена")
async def cancel_action(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "❌ Действие отменено",
        reply_markup=get_main_keyboard()
    )

# ===== ОБРАБОТЧИКИ КОЛБЭКОВ =====

@dp.callback_query(lambda c: c.data == "start_setup")
async def start_setup(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "📝 Давай настроим твой профиль.\n\n"
        "Введите свой вес в килограммах (например: 70):"
    )
    await state.set_state(SetupState.waiting_for_weight)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    await callback.message.delete()
    await callback.message.answer(
        "🔙 Главное меню",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "back_to_settings")
async def back_to_settings(callback: CallbackQuery):
    await callback.message.edit_text("⚙️ Настройки:")
    await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard())
    await callback.answer()

@dp.callback_query(lambda c: c.data == "view_profile")
async def view_profile(callback: CallbackQuery):
    user_id = db.get_user_id(callback.from_user.id)
    profile = db.get_user_profile(user_id)

    if not profile or profile[0] is None:
        await callback.message.answer("⚠️ Профиль не найден. Используйте /start")
        await callback.answer()
        return

    weight, height, age, activity, daily_norm, is_premium = profile

    today = datetime.now().strftime("%Y-%m-%d")
    total = db.get_daily_total(user_id, today)
    percent = (total / daily_norm * 100) if daily_norm > 0 else 0

    text = (
        f"👤 *Ваш профиль*\n\n"
        f"⚖️ Вес: {weight} кг\n"
        f"📏 Рост: {height} см\n"
        f"🎂 Возраст: {age} лет\n"
        f"🏃 Активность: {get_activity_text(activity)}\n"
        f"🔥 Дневная норма: *{daily_norm} ккал*\n"
        f"📊 Сегодня: {total:.1f} ккал ({percent:.1f}%)\n"
        f"{get_progress_bar(percent)}\n\n"
        f"{'💎 Премиум' if is_premium else '🆓 Бесплатный'}"
    )
    await callback.message.edit_text(text, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "edit_profile")
async def edit_profile(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите новый вес в килограммах:")
    await state.set_state(SetupState.waiting_for_weight)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "view_foods")
async def view_foods(callback: CallbackQuery):
    user_id = db.get_user_id(callback.from_user.id)
    keyboard = get_foods_keyboard(user_id, 0)
    if keyboard:
        await callback.message.edit_text(
            "📦 *Мои продукты (калории на 100г):*\n\n"
            "Нажми на продукт, чтобы удалить его",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    else:
        await callback.message.edit_text(
            "📦 База продуктов пуста.\n"
            "Добавьте продукты через поиск OpenFoodFacts или вручную"
        )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("foods_page_"))
async def change_food_page(callback: CallbackQuery):
    page = int(callback.data.split("_")[2])
    user_id = db.get_user_id(callback.from_user.id)
    keyboard = get_foods_keyboard(user_id, page)
    if keyboard:
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("food_"))
async def delete_food(callback: CallbackQuery):
    user_id = db.get_user_id(callback.from_user.id)
    name = callback.data.split("_")[1]

    if db.delete_food(user_id, name):
        await callback.answer(f"✅ Продукт '{name}' удален")
        keyboard = get_foods_keyboard(user_id, 0)
        if keyboard:
            await callback.message.edit_reply_markup(reply_markup=keyboard)
        else:
            await callback.message.edit_text("📦 База продуктов пуста.")
    else:
        await callback.answer("❌ Продукт не найден")

@dp.callback_query(lambda c: c.data == "add_food")
async def add_food(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "➕ Добавление нового продукта\n\n"
        "Введите название продукта для поиска в OpenFoodFacts:"
    )
    await state.set_state(AddFoodState.waiting_for_product)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "clear_history")
async def clear_history(callback: CallbackQuery):
    user_id = db.get_user_id(callback.from_user.id)
    today = datetime.now().strftime("%Y-%m-%d")
    db.clear_daily_history(user_id, today)
    await callback.message.answer("✅ История за сегодня очищена")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "add_again")
async def add_again(callback: CallbackQuery, state: FSMContext):
    await callback.message.delete()
    await state.set_state(AddMealState.waiting_for_product_name)
    await callback.message.answer(
        "🍽️ Введите название продукта:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="🔙 Отмена")]],
            resize_keyboard=True
        )
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "show_today")
async def show_today_from_callback(callback: CallbackQuery):
    await callback.message.delete()
    await show_today(callback.from_user.id, callback.message)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "show_week")
async def show_week_from_callback(callback: CallbackQuery):
    await callback.message.delete()
    await show_week(callback.from_user.id, callback.message)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "show_history")
async def show_history_from_callback(callback: CallbackQuery):
    await callback.message.delete()
    await show_history(callback.from_user.id, callback.message)
    await callback.answer()

# ===== НОВЫЕ ОБРАБОТЧИКИ ДЛЯ ДРУЗЕЙ =====

@dp.callback_query(lambda c: c.data == "add_friend")
async def add_friend_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "👤 Введите username друга (без @) или его Telegram ID:\n\n"
        "Пример: /addfriend john_doe"
    )
    await state.set_state(FriendState.waiting_for_friend_username)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "view_requests")
async def view_requests(callback: CallbackQuery):
    user_id = db.get_user_id(callback.from_user.id)
    requests = db.get_friend_requests(user_id)

    if not requests:
        await callback.message.answer("📭 Нет новых запросов")
        await callback.answer()
        return

    text = "📩 *Запросы в друзья:*\n\n"
    keyboard = []

    for friend_id, username, first_name in requests:
        text += f"• {first_name} (@{username})\n"
        keyboard.append([
            InlineKeyboardButton(
                text=f"✅ Принять {first_name}",
                callback_data=f"accept_friend_{friend_id}"
            )
        ])

    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")])

    await callback.message.edit_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("accept_friend_"))
async def accept_friend(callback: CallbackQuery):
    user_id = db.get_user_id(callback.from_user.id)
    friend_id = int(callback.data.split("_")[2])

    db.accept_friend_request(user_id, friend_id)

    # Проверяем достижения
    new_achievements = await check_achievements(user_id, 'add_friend')
    await check_achievements(friend_id, 'add_friend')

    await callback.message.answer("✅ Друг добавлен!")

    # Показываем сообщение о достижениях
    if new_achievements:
        text = "🏆 *Новые достижения!*\n\n"
        for ach in new_achievements:
            text += f"• {ACHIEVEMENTS[ach]['name']} - {ACHIEVEMENTS[ach]['desc']}\n"
        await callback.message.answer(text, parse_mode="Markdown")

    await callback.answer()

@dp.callback_query(lambda c: c.data == "friend_leaderboard")
async def friend_leaderboard(callback: CallbackQuery):
    user_id = db.get_user_id(callback.from_user.id)

    leaderboard = db.get_friend_leaderboard(user_id)

    if not leaderboard:
        await callback.message.answer(
            "🏆 У вас пока нет друзей для рейтинга.\n"
            "Добавьте друзей через '👥 Друзья' → '➕ Добавить друга'"
        )
        await callback.answer()
        return

    medals = ["🥇", "🥈", "🥉"]
    text = "🏆 *Рейтинг друзей за неделю*\n\n"

    for i, (friend_id, username, first_name, norm, week_cal, days_logged) in enumerate(leaderboard):
        avg = week_cal / 7 if days_logged > 0 else 0
        percent = (avg / norm * 100) if norm > 0 else 0

        medal = medals[i] if i < 3 else f"{i+1}."

        # Эмодзи статуса
        if percent >= 90:
            status = "🌟"
        elif percent >= 70:
            status = "💪"
        elif percent >= 40:
            status = "👍"
        else:
            status = "😴"

        text += f"{medal} *{first_name}*\n"
        text += f"   {status} {week_cal:.0f} ккал за неделю\n"
        text += f"   📊 В среднем: {avg:.0f} ккал/день ({percent:.0f}%)\n"
        text += f"   📝 Дней с записями: {days_logged}/7\n\n"

    await callback.message.edit_text(text, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "view_achievements")
async def view_achievements(callback: CallbackQuery):
    user_id = db.get_user_id(callback.from_user.id)
    achievements = db.get_achievements(user_id)

    text = "🏆 *Мои достижения*\n\n"

    if achievements:
        for ach_type, earned_at in achievements:
            if ach_type in ACHIEVEMENTS:
                text += f"✅ {ACHIEVEMENTS[ach_type]['name']}\n"
                text += f"   {ACHIEVEMENTS[ach_type]['desc']}\n"
                text += f"   📅 {earned_at[:10]}\n\n"
    else:
        text += "Пока нет достижений. Продолжайте пользоваться ботом!\n\n"
        text += "💡 *Как получить достижения:*\n"
        for key, ach in ACHIEVEMENTS.items():
            text += f"• {ach['name']} - {ach['desc']}\n"

    await callback.message.edit_text(text, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(lambda c: c.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()

@dp.callback_query(lambda c: c.data == "cancel_search")
async def cancel_search(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.message.answer(
        "❌ Поиск отменен",
        reply_markup=get_main_keyboard()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "manual_add")
async def manual_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddMealState.waiting_for_product_name)
    await callback.message.delete()
    await callback.message.answer(
        "✏️ Введите название продукта вручную:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="🔙 Отмена")]],
            resize_keyboard=True
        )
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("select_product_"))
async def select_product_from_search(callback: CallbackQuery, state: FSMContext):
    index = int(callback.data.split("_")[2])
    data = await state.get_data()
    current_state = await state.get_state()

    results = data.get('search_results', [])

    if 0 <= index < len(results):
        selected = results[index]
        product = selected['name'].lower()
        user_id = db.get_user_id(callback.from_user.id)

        # Проверяем, есть ли уже в локальной базе
        existing = db.get_food(user_id, product)
        if not existing:
            # Добавляем в базу
            db.add_food(
                user_id,
                product,
                selected['calories'],
                selected['protein'],
                selected['fat'],
                selected['carbs']
            )

        if current_state == AddFoodState.waiting_for_product:
            await callback.message.delete()
            await callback.message.answer(
                f"✅ Продукт *{selected['name']}* добавлен!\n"
                f"🔥 {selected['calories']} ккал/100г\n"
                f"🥩 Белки: {selected['protein']}г | 🧈 Жиры: {selected['fat']}г | 🍚 Углеводы: {selected['carbs']}г",
                parse_mode="Markdown",
                reply_markup=get_main_keyboard()
            )
            await state.clear()
            await callback.answer()
            return

        await state.update_data(product=product)
        await callback.message.delete()
        await callback.message.answer(
            f"✅ Выбран продукт: *{selected['name']}*\n"
            f"🔥 {selected['calories']} ккал/100г\n\n"
            f"Сколько грамм вы съели?",
            parse_mode="Markdown"
        )
        await state.set_state(AddMealState.waiting_for_grams)
        await callback.answer()
    else:
        await callback.answer("❌ Продукт не найден")

# ===== ОБРАБОТЧИКИ СОСТОЯНИЙ =====

@dp.message(SetupState.waiting_for_weight)
async def process_weight(message: Message, state: FSMContext):
    try:
        weight = float(message.text)
        await state.update_data(weight=weight)
        await message.answer("Введите рост в сантиметрах:")
        await state.set_state(SetupState.waiting_for_height)
    except ValueError:
        await message.answer("❌ Введите число (например: 70)")

@dp.message(SetupState.waiting_for_height)
async def process_height(message: Message, state: FSMContext):
    try:
        height = float(message.text)
        await state.update_data(height=height)
        await message.answer("Введите возраст (полных лет):")
        await state.set_state(SetupState.waiting_for_age)
    except ValueError:
        await message.answer("❌ Введите число (например: 175)")

@dp.message(SetupState.waiting_for_age)
async def process_age(message: Message, state: FSMContext):
    try:
        age = int(message.text)
        await state.update_data(age=age)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛋️ Сидячий (1.2)", callback_data="activity_1.2")],
            [InlineKeyboardButton(text="🚶 Легкая (1.375)", callback_data="activity_1.375")],
            [InlineKeyboardButton(text="🏃 Средняя (1.55)", callback_data="activity_1.55")],
            [InlineKeyboardButton(text="🏋️ Высокая (1.725)", callback_data="activity_1.725")],
            [InlineKeyboardButton(text="🔥 Экстрим (1.9)", callback_data="activity_1.9")]
        ])
        await message.answer("Выберите уровень активности:", reply_markup=keyboard)
        await state.set_state(SetupState.waiting_for_activity)
    except ValueError:
        await message.answer("❌ Введите целое число (например: 30)")

@dp.callback_query(lambda c: c.data.startswith("activity_"))
async def process_activity(callback: CallbackQuery, state: FSMContext):
    activity = float(callback.data.split("_")[1])
    data = await state.get_data()

    user_id = db.get_user_id(callback.from_user.id)
    daily_norm = db.update_user_profile(
        user_id,
        data["weight"],
        data["height"],
        data["age"],
        activity
    )

    await callback.message.delete()
    await callback.message.answer(
        f"✅ *Профиль сохранен!*\n\n"
        f"Дневная норма калорий: *{daily_norm} ккал*\n"
        f"Уровень активности: {get_activity_text(activity)}",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )
    await state.clear()
    await callback.answer()

# --- Добавление продукта в базу ---

@dp.message(AddFoodState.waiting_for_product)
async def process_food_name(message: Message, state: FSMContext):
    product = message.text.lower().strip()
    await state.update_data(product=product)

    await message.answer("🔍 Ищу продукт в базе OpenFoodFacts...")
    results = await search_openfoodfacts(product)

    if results:
        await state.update_data(search_results=results)
        text = format_search_results(results)
        keyboard = get_product_selection_keyboard(results)
        await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await message.answer(
            "❌ Ничего не найдено в OpenFoodFacts.\n"
            "Введите калорийность для этого продукта вручную (ккал на 100г):"
        )
        await state.set_state(AddFoodState.waiting_for_calories)

@dp.message(AddFoodState.waiting_for_calories)
async def process_food_calories(message: Message, state: FSMContext):
    try:
        calories = float(message.text)
        data = await state.get_data()
        product = data["product"]
        user_id = db.get_user_id(message.from_user.id)

        db.add_food(user_id, product, calories)

        await message.answer(
            f"✅ Продукт '{product.title()}' добавлен!\n"
            f"Калорийность: {calories} ккал/100г",
            reply_markup=get_main_keyboard()
        )
        await state.clear()
    except ValueError:
        await message.answer("❌ Введите число (например: 130)")

# --- Добавление приема пищи ---

@dp.message(AddMealState.waiting_for_product_name)
async def process_meal_product(message: Message, state: FSMContext):
    product = message.text.lower().strip()
    user_id = db.get_user_id(message.from_user.id)

    # Проверяем, есть ли в локальной базе
    food = db.get_food(user_id, product)
    if food:
        await state.update_data(product=product)
        await message.answer(f"Сколько грамм '{product.title()}' вы съели?")
        await state.set_state(AddMealState.waiting_for_grams)
        return

    # Ищем в OpenFoodFacts
    await message.answer("🔍 Ищу продукт в OpenFoodFacts...")
    results = await search_openfoodfacts(product)

    if results:
        await state.update_data(search_results=results)
        text = format_search_results(results)
        keyboard = get_product_selection_keyboard(results)
        await message.answer(text, parse_mode="Markdown", reply_markup=keyboard)
        await state.set_state(AddMealState.waiting_for_product_selection)
    else:
        await message.answer(
            "❌ Продукт не найден в базе.\n"
            "Введите название вручную или попробуйте другой запрос:"
        )

@dp.message(AddMealState.waiting_for_grams)
async def process_meal_grams(message: Message, state: FSMContext):
    try:
        grams = float(message.text)
        data = await state.get_data()
        product = data["product"]
        user_id = db.get_user_id(message.from_user.id)

        # Получаем данные о продукте
        food = db.get_food(user_id, product)
        if food:
            calories_per_100 = food[1]
        else:
            calories_per_100 = 0

        total_cal = round((grams / 100) * calories_per_100, 1)
        today = datetime.now().strftime("%Y-%m-%d")

        # Сохраняем в дневник
        db.add_meal(user_id, today, product, grams, total_cal)

        # Проверяем достижения
        new_achievements = await check_achievements(user_id, 'add_meal')

        # Проверяем идеальный день
        daily_total = db.get_daily_total(user_id, today)
        profile = db.get_user_profile(user_id)
        if profile:
            _, _, _, _, daily_norm, _ = profile
            await check_achievements(
                user_id,
                'check_day',
                {'total': daily_total, 'norm': daily_norm}
            )

        # Сообщение о добавлении
        text = f"✅ *Добавлено:*\n🍽️ {product.title()} - {grams}г\n🔥 {total_cal} ккал\n\n"

        # Показываем новые достижения
        if new_achievements:
            text += "🏆 *Новые достижения!*\n\n"
            for ach in new_achievements:
                text += f"• {ACHIEVEMENTS[ach]['name']} - {ACHIEVEMENTS[ach]['desc']}\n"
            text += "\n"

        text += "Что дальше?"

        await message.answer(
            text,
            parse_mode="Markdown",
            reply_markup=get_meal_actions_keyboard()
        )
        await state.clear()
    except ValueError:
        await message.answer("❌ Введите число (например: 150)")

# --- Добавление друга ---

@dp.message(FriendState.waiting_for_friend_username)
async def process_add_friend(message: Message, state: FSMContext):
    username = message.text.strip()
    user_id = db.get_user_id(message.from_user.id)

    # Ищем друга по username или ID
    try:
        friend_telegram_id = int(username)
        friend_db_id = db.get_user_id(friend_telegram_id)
    except ValueError:
        # Ищем по username
        db.cursor.execute(
            "SELECT id FROM users WHERE username = ?",
            (username,)
        )
        result = db.cursor.fetchone()
        friend_db_id = result[0] if result else None

    if not friend_db_id:
        await message.answer(
            "❌ Пользователь не найден.\n"
            "Убедитесь, что пользователь уже запустил бота хотя бы раз.\n"
            "Попробуйте снова:"
        )
        return

    if friend_db_id == user_id:
        await message.answer("❌ Нельзя добавить себя в друзья!")
        return

    # Проверяем, не друзья ли уже
    friends = db.get_friends(user_id)
    for f_id, _, _, _ in friends:
        if f_id == friend_db_id:
            await message.answer("👥 Вы уже друзья!")
            await state.clear()
            return

    # Отправляем запрос
    db.add_friend_request(user_id, friend_db_id)

    await message.answer(
        f"✅ Запрос на дружбу отправлен!\n"
        f"Друг получит уведомление при следующем входе."
    )
    await state.clear()

# ===== ОСНОВНЫЕ ФУНКЦИИ =====

def get_activity_text(level):
    activities = {
        1.2: "🛋️ Сидячий",
        1.375: "🚶 Легкая",
        1.55: "🏃 Средняя",
        1.725: "🏋️ Высокая",
        1.9: "🔥 Экстрим"
    }
    return activities.get(level, "Неизвестно")

async def show_today(telegram_id, message):
    user_id = db.get_user_id(telegram_id)
    if not user_id:
        await message.answer("⚠️ Сначала настройте профиль через /start")
        return

    profile = db.get_user_profile(user_id)
    if not profile or profile[0] is None:
        await message.answer("⚠️ Сначала настройте профиль через /start")
        return

    weight, height, age, activity, daily_norm, is_premium = profile

    today = datetime.now().strftime("%Y-%m-%d")
    total = db.get_daily_total(user_id, today)
    meals = db.get_daily_meals(user_id, today)

    remaining = daily_norm - total
    percent = (total / daily_norm * 100) if daily_norm > 0 else 0

    progress_bar = get_progress_bar(percent)
    mood = get_mood_emoji(percent, remaining)
    advice = get_advice(percent, remaining)

    # Рассчитываем макронутриенты из продуктов
    protein_total = 0
    fat_total = 0
    carbs_total = 0

    for product_name, grams, calories, time in meals:
        food = db.get_food(user_id, product_name)
        if food:
            _, cal_per_100, protein, fat, carbs = food
            protein_total += (protein / 100) * grams
            fat_total += (fat / 100) * grams
            carbs_total += (carbs / 100) * grams

    macros_text = (
        f"🥩 Белки: {protein_total:.1f}г | "
        f"🧈 Жиры: {fat_total:.1f}г | "
        f"🍚 Углеводы: {carbs_total:.1f}г"
    )

    text = (
        f"📊 *Сегодня {today}*\n\n"
        f"🔥 Съедено: *{total:.1f}* / {daily_norm} ккал\n"
        f"📈 Прогресс: {percent:.1f}%\n"
        f"{progress_bar}\n"
        f"⚡️ Осталось: *{remaining:.1f}* ккал\n"
        f"🎯 Настроение: {mood}\n"
        f"{advice}\n\n"
        f"📝 *Записей:* {len(meals)}\n"
        f"{macros_text}\n\n"
    )

    if meals:
        text += "📋 *Последние записи:*\n"
        for product_name, grams, calories, time in meals[-3:]:
            emoji = "🍖" if calories > 200 else "🥗" if calories > 100 else "🍎"
            text += f"{emoji} {time} {product_name.title()} {grams}г = {calories} ккал\n"

    await message.answer(text, parse_mode="Markdown")

async def show_history(telegram_id, message):
    user_id = db.get_user_id(telegram_id)
    if not user_id:
        await message.answer("⚠️ Сначала настройте профиль")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    meals = db.get_daily_meals(user_id, today)

    if not meals:
        await message.answer("📭 За сегодня ничего не записано.")
        return

    text = f"📋 *История за сегодня ({len(meals)} записей)*\n\n"
    total = 0

    for i, (product_name, grams, calories, time) in enumerate(meals, 1):
        time_hour = int(time.split(':')[0])
        if time_hour < 11:
            time_emoji = "🌅"
        elif time_hour < 15:
            time_emoji = "☀️"
        elif time_hour < 19:
            time_emoji = "🌤️"
        else:
            time_emoji = "🌙"

        cal_emoji = "🍖" if calories > 200 else "🥗" if calories > 100 else "🍎"

        text += f"{i}. {time_emoji} {time} - {cal_emoji} {product_name.title()} {grams}г = *{calories}* ккал\n"
        total += calories

    text += f"\n*Итого: {total:.1f} ккал*"

    profile = db.get_user_profile(user_id)
    if profile and profile[0] is not None:
        daily_norm = profile[4]
        percent = (total / daily_norm * 100) if daily_norm > 0 else 0
        text += f" ({percent:.1f}% от нормы)"

    await message.answer(text, parse_mode="Markdown")

async def show_week(telegram_id, message):
    user_id = db.get_user_id(telegram_id)
    if not user_id:
        await message.answer("⚠️ Сначала настройте профиль")
        return

    profile = db.get_user_profile(user_id)
    if not profile or profile[0] is None:
        await message.answer("⚠️ Сначала настройте профиль")
        return

    daily_norm = profile[4]

    week_dates = get_week_dates()
    weekly_data = []

    for date in week_dates:
        date_str = date.strftime("%Y-%m-%d")
        total = db.get_daily_total(user_id, date_str)
        weekly_data.append({
            'date': date,
            'calories': total,
            'day_name': get_day_name_short(date.weekday()),
            'emoji': get_day_emoji(date.weekday())
        })

    # Статистика
    values = [day['calories'] for day in weekly_data]
    total = sum(values)
    avg = total / 7 if values else 0
    max_day = max(values) if values else 0
    min_day = min(values) if values else 0
    max_index = values.index(max_day) if values else -1
    min_index = values.index(min_day) if values else -1

    days_over = sum(1 for v in values if v > daily_norm)
    days_under = sum(1 for v in values if v < daily_norm and v > 0)
    days_zero = sum(1 for v in values if v == 0)

    # Оценка
    if days_zero >= 5:
        grade = "😱 Ужасно! Вы почти ничего не ели!"
    elif days_over >= 5:
        grade = "😰 Ого! Слишком много переборов!"
    elif days_over >= 3:
        grade = "🤔 Есть переборы, старайтесь держать норму"
    elif days_zero >= 3:
        grade = "😕 Много дней без записей"
    elif avg <= daily_norm * 0.7:
        grade = "😊 Хорошо! Но можно немного добавить калорий"
    elif avg >= daily_norm * 1.1:
        grade = "😅 Чуть перебираете в среднем"
    else:
        grade = "🌟 Отличная неделя! Так держать!"

    # Эмодзи для дней
    week_days = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
    best_day_name = week_days[max_index] if max_index >= 0 else "—"
    worst_day_name = week_days[min_index] if min_index >= 0 else "—"

    chart = create_weekly_chart(weekly_data, daily_norm)

    stats_text = (
        f"📊 *Статистика за неделю:*\n\n"
        f"📈 Среднее: *{avg:.0f}* ккал/день\n"
        f"📊 Всего: *{total:.0f}* ккал\n"
        f"🎯 От нормы: *{(avg / daily_norm * 100):.0f}%*\n\n"
        f"🏆 Лучший день: {best_day_name} ({max_day:.0f} ккал)\n"
        f"📉 Худший день: {worst_day_name} ({min_day:.0f} ккал)\n\n"
        f"📋 Детали:\n"
        f"  ✅ Дней в норме: {7 - days_over - days_zero}\n"
        f"  ⬆️ Дней с перебором: {days_over}\n"
        f"  ⬇️ Дней с недобором: {days_under}\n"
        f"  📭 Пустых дней: {days_zero}\n\n"
        f"💡 Оценка: {grade}"
    )

    full_text = (
        f"📈 *График калорий за неделю*\n"
        f"🎯 Норма: {daily_norm} ккал/день\n\n"
        f"```\n{chart}\n```\n\n"
        f"{stats_text}"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Сегодня", callback_data="show_today")],
        [InlineKeyboardButton(text="📋 История", callback_data="show_history")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_menu")]
    ])

    await message.answer(full_text, parse_mode="Markdown", reply_markup=keyboard)

# ===== ЗАПУСК =====

async def main():
    print(f"🤖 Calorie bot v{__version__} started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
