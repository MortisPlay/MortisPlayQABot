import json
import logging
import os
import time
import asyncio
import hashlib
import re
import difflib
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.helpers import escape_markdown
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv(".env")
TOKEN = os.getenv("TELEGRAM_TOKEN")
QUESTIONS_FILE = os.getenv("QUESTIONS_FILE", "questions.json")
if not TOKEN:
    raise ValueError("Не задан TELEGRAM_TOKEN в .env файле!")

# Константы
ADMIN_ID = 335236137
BLACKLIST_FILE = "blacklist.json"
QA_WEBSITE = "https://mortisplay.ru/qa.html"
MAX_PENDING_QUESTIONS = 3
SIMILARITY_THRESHOLD = 0.6

# Перевод статусов
STATUS_TRANSLATIONS = {
    "pending": "Рассматривается",
    "approved": "Принят",
    "rejected": "Отклонён",
    "cancelled": "Аннулирован"
}

# Инициализация JSON
if not os.path.exists(QUESTIONS_FILE):
    with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump({"questions": []}, f, ensure_ascii=False, indent=2)

if not os.path.exists(BLACKLIST_FILE):
    with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
        json.dump({"blacklist": []}, f, ensure_ascii=False, indent=2)

# Защита от спама
spam_protection = {}
processed_updates = set()
question_hashes = {}

def get_question_hash(question: str) -> str:
    return hashlib.md5(question.lower().encode('utf-8')).hexdigest()

def check_blacklist(question: str) -> bool:
    try:
        with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        blacklist = data.get("blacklist", [])
        question_lower = question.lower()
        for word in blacklist:
            if word.lower() in question_lower:
                logger.info(f"Обнаружено запрещённое слово '{word}' в вопросе: {question}")
                return True
        return False
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Ошибка чтения {BLACKLIST_FILE}: {e}")
        return False

def check_question_meaning(question: str) -> tuple[bool, str]:
    question_lower = question.lower().strip()
    
    # Проверка на упоминание ботов в вопросе
    bot_keywords = ["бот", "telegram", "телега", "телеграм", "bot", "@groupanonymousbot"]
    if any(keyword in question_lower for keyword in bot_keywords):
        logger.info(f"Вопрос отклонён: содержит упоминание бота ({question})")
        return False, "Вопросы о боте или от имени бота запрещены. Задайте вопрос о контенте Mortis Play!"

    # Проверка длины вопроса
    if len(question_lower) < 10:
        return False, "Вопрос слишком короткий (менее 10 символов)."
    
    # Проверка на повторяющиеся символы
    if re.match(r'^(.)\1{4,}$', question_lower.replace(' ', '')) or re.match(r'^(\W)\1{4,}$', question_lower):
        return False, "Вопрос содержит повторяющиеся символы."
    
    # Проверка на повторяющиеся слова
    words = question_lower.split()
    if len(words) > 1 and len(set(words)) == 1:
        return False, "Вопрос состоит из повторяющихся слов."
    
    # Проверка на подозрительные слова с цифрами (например, "мортис1 мортис2")
    if re.search(r'\b\w*\d+\w*\b\s+\b\w*\d+\w*\b', question_lower):
        return False, "Вопрос содержит подозрительные слова с цифрами (например, 'мортис1 мортис2')."
    
    # Проверка на бессмысленные строки (например, "автавававтам")
    if re.match(r'^\w{2,}(\w)\1{2,}', question_lower.replace(' ', '')):
        return False, "Вопрос содержит бессмысленные повторяющиеся последовательности."
    
    # Проверка на наличие вопросительных слов и минимальной длины
    question_words = ["что", "как", "почему", "где", "когда", "какой", "какая", "какое", "кто", "зачем", "сколько"]
    has_question_word = any(word in question_lower for word in question_words) or "?" in question_lower
    has_multiple_words = len(words) >= 3
    if not (has_question_word and has_multiple_words):
        return False, "Вопрос не содержит вопросительных слов или слишком прост."
    
    return True, ""

def check_question_similarity(new_question: str, existing_questions: list) -> tuple[bool, str]:
    new_question_lower = new_question.lower().strip()
    for q in existing_questions:
        if not q.get("cancelled", False):
            existing_question = q["question"].lower().strip()
            similarity = difflib.SequenceMatcher(None, new_question_lower, existing_question).ratio()
            if similarity > SIMILARITY_THRESHOLD:
                logger.info(f"Обнаружен похожий вопрос: '{new_question}' ~ '{q['question']}' (схожесть: {similarity:.2f})")
                return True, q["question"]
    return False, ""

def get_remaining_attempts(user_id: int, data: dict) -> int:
    pending_questions = [q for q in data["questions"] if q["user_id"] == user_id and q["status"] == "pending" and not q.get("cancelled", False)]
    return max(0, MAX_PENDING_QUESTIONS - len(pending_questions))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /start от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    update_id = update.update_id
    if update_id in processed_updates:
        logger.info(f"Дубликат update_id {update_id}, пропускаем")
        return
    processed_updates.add(update_id)

    user_id = update.message.from_user.id
    try:
        with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
        await update.message.reply_text("🚨 Ошибка данных! Свяжитесь с @dimap7221.", parse_mode="Markdown")
        return

    remaining_attempts = get_remaining_attempts(user_id, data)
    keyboard = [
        [InlineKeyboardButton("Задать вопрос ❓", callback_data="ask")],
        [InlineKeyboardButton("Мои вопросы 📋", callback_data="myquestions")],
        [InlineKeyboardButton("Гайд 📖", callback_data="guide")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"👋 *Привет!* Это Q&A-бот Mortis Play! 😎\n"
        f"Задавай вопросы для стримов и сайта.\n\n"
        f"📌 *Осталось попыток*: {remaining_attempts}\n"
        f"Новичок? Жми *Гайд* или пиши `/guide`!\n"
        f"Хочешь вопрос? Пиши `/ask`! 🚀",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def guide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /guide от user_id {update.effective_user.id}")
    user_id = update.effective_user.id
    reply_to = update.message or update.callback_query.message
    if not reply_to:
        logger.info("Отсутствует reply_to")
        return
    update_id = update.update_id
    if update_id in processed_updates:
        logger.info(f"Дубликат update_id {update_id}, пропускаем")
        return
    processed_updates.add(update_id)

    try:
        with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
        await reply_to.reply_text("🚨 Ошибка данных! Свяжитесь с @dimap7221.", parse_mode="Markdown")
        return

    remaining_attempts = get_remaining_attempts(user_id, data)
    keyboard = [
        [InlineKeyboardButton("Задать вопрос ❓", callback_data="ask")],
        [InlineKeyboardButton("Мои вопросы 📋", callback_data="myquestions")],
        [InlineKeyboardButton("На сайт 🌐", url=QA_WEBSITE)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        f"📖 *Гайд по Q&A-боту Mortis Play*\n\n"
        f"😎 *Добро пожаловать!* Вот как работает бот:\n\n"
        f"1️⃣ **Задай вопрос**: Пиши `/ask <вопрос>` (5–500 символов).\n"
        f"   *Попыток*: {remaining_attempts}/3. Пример: `/ask Какая твоя любимая игра?`\n"
        f"   *Совет*: _Чтобы ваш вопрос приняли быстро, добавьте контекст к вашему вопросу — так он быстрее попадёт на сайт!_\n\n"
        f"2️⃣ **Статусы вопроса**:\n"
        f"   • *Рассматривается*: Ждёт проверки админом.\n"
        f"   • *Принят*: Опубликован на [сайте]({QA_WEBSITE}) за 1–48 ч.\n"
        f"   • *Отклонён*: Не подходит (с причиной).\n"
        f"   • *Аннулирован*: Удалён за нарушение правил.\n\n"
        f"3️⃣ **Правила вопросов**:\n"
        f"   • Вопросы должны быть связаны с контентом Mortis Play (игры, стримы, контент).\n"
        f"   • Запрещены: спам, оскорбления, реклама, оффтоп, личная информация, вопросы о боте, вопросы от анонимных ботов.\n"
        f"   • Аннулирование: за нарушение правил или неуместный контент, включая подозрительные символы или цифры.\n\n"
        f"4️⃣ **Уведомления**: Нажми *Уведомить 🔔* для статуса вопроса.\n\n"
        f"5️⃣ **Проверь вопросы**: Пиши `/myquestions`.\n\n"
        f"📌 *Проблемы?* Пиши админу @dimap7221.\n"
        f"🚀 *Готов?* Жми `/ask`!"
    )
    try:
        await reply_to.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        logger.info(f"Гайд отправлен пользователю user_id {user_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки гайда: {e}")
        text_plain = text.replace("*", "").replace("_", "").replace("[сайте](https://mortisplay.ru/qa.html)", f"сайте {QA_WEBSITE}")
        await reply_to.reply_text(text_plain, reply_markup=reply_markup, parse_mode=None)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /help от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    update_id = update.update_id
    if update_id in processed_updates:
        logger.info(f"Дубликат update_id {update_id}, пропускаем")
        return
    processed_updates.add(update_id)

    user_id = update.message.from_user.id
    try:
        with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
        await update.message.reply_text("🚨 Ошибка данных! Свяжитесь с @dimap7221.", parse_mode="Markdown")
        return

    remaining_attempts = get_remaining_attempts(user_id, data)
    keyboard = [
        [InlineKeyboardButton("Задать вопрос ❓", callback_data="ask")],
        [InlineKeyboardButton("Мои вопросы 📋", callback_data="myquestions")],
        [InlineKeyboardButton("Гайд 📖", callback_data="guide")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        f"👋 *Привет!* Я Q&A-бот Mortis Play 😎\n"
        f"📌 *Осталось попыток*: {remaining_attempts}\n\n"
        f"📋 *Команды*:\n"
        f"• `/start` — Начало работы\n"
        f"• `/guide` — Гайд для новичков\n"
        f"• `/ask <вопрос>` — Задать вопрос\n"
        f"• `/myquestions` — Твои вопросы\n"
        f"• `/help` — Список команд\n"
        f"• `/list` — Все вопросы (админ)\n"
        f"• `/clear` — Очистить вопросы (админ)\n"
        f"• `/delete <id>` — Удалить вопрос (админ)\n"
        f"• `/edit <id> <вопрос>` — Редактировать вопрос (админ)\n"
        f"• `/cancel <id> <причина>` — Аннулировать вопрос (админ)\n"
        f"• `/approve <id> <ответ>` — Принять вопрос (админ)\n"
        f"• `/approve_all <id1,id2,...> <ответ>` — Принять несколько вопросов (админ)\n"
        f"• `/reject <id> <причина>` — Отклонить вопрос (админ)\n"
        f"• `/reject_all <id1,id2,...> <причина>` — Отклонить несколько вопросов (админ)\n\n"
        f"📢 Вопросы должны быть осмысленными и связанными с контентом Mortis Play. Запрещены вопросы о боте и от анонимных ботов!\n"
        f"Новичок? Жми *Гайд* или пиши `/guide`! 🚀"
    )
    try:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Ошибка отправки /help: {e}")
        text_plain = text.replace("*", "").replace("[сайте](https://mortisplay.ru/qa.html)", f"сайте {QA_WEBSITE}")
        await update.message.reply_text(text_plain, reply_markup=reply_markup, parse_mode=None)

async def list_questions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /list от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 *Только админ* может это делать! 😎", parse_mode="Markdown")
        logger.warning(f"Неавторизованная попытка /list от user_id {update.message.from_user.id}")
        return
    update_id = update.update_id
    if update_id in processed_updates:
        logger.info(f"Дубликат update_id {update_id}, пропускаем")
        return
    processed_updates.add(update_id)

    try:
        with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
        await update.message.reply_text("🚨 Ошибка чтения вопросов! Свяжитесь с @dimap7221.", parse_mode="Markdown")
        return

    active_questions = [q for q in data["questions"] if not q.get("cancelled", False)]
    if not active_questions:
        await update.message.reply_text("📭 *Нет активных вопросов*!", parse_mode="Markdown")
        logger.info("Список активных вопросов пуст")
        return

    response = "*📋 Список активных вопросов*:\n\n"
    for q in active_questions:
        status = STATUS_TRANSLATIONS.get(q["status"], q["status"])
        escaped_question = escape_markdown(q["question"], version=2)
        escaped_username = escape_markdown(q["username"], version=2)
        cancel_reason = f"\n**Причина**: *{escape_markdown(q['cancel_reason'], version=2)}*" if q.get("cancel_reason") and q["status"] == "cancelled" else ""
        reject_reason = f"\n**Причина**: *{escape_markdown(q['reject_reason'], version=2)}*" if q.get("reject_reason") and q["status"] == "rejected" else ""
        response += f"**ID**: `{q['id']}`\n**От**: @{escaped_username}\n**Вопрос**: *{escaped_question}*\n**Статус**: `{status}`{cancel_reason}{reject_reason}\n\n"

    try:
        await update.message.reply_text(response, parse_mode="MarkdownV2")
        logger.info(f"Админ запросил список вопросов: {len(active_questions)} активных вопросов")
    except Exception as e:
        logger.error(f"Ошибка отправки списка вопросов: {e}")
        plain_response = "📋 Список активных вопросов:\n\n"
        for q in active_questions:
            status = STATUS_TRANSLATIONS.get(q["status"], q["status"])
            cancel_reason = f"\nПричина: {q['cancel_reason']}" if q.get("cancel_reason") and q["status"] == "cancelled" else ""
            reject_reason = f"\nПричина: {q['reject_reason']}" if q.get("reject_reason") and q["status"] == "rejected" else ""
            plain_response += f"ID: {q['id']}\nОт: @{q['username']}\nВопрос: {q['question']}\nСтатус: {status}{cancel_reason}{reject_reason}\n\n"
        await update.message.reply_text(plain_response)
        logger.info(f"Отправлен список вопросов в plain-text формате из-за ошибки MarkdownV2")

async def my_questions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /myquestions от user_id {update.effective_user.id}")
    user_id = update.effective_user.id
    reply_to = update.message or update.callback_query.message
    if not reply_to:
        logger.info("Отсутствует reply_to")
        return
    update_id = update.update_id
    if update_id in processed_updates:
        logger.info(f"Дубликат update_id {update_id}, пропускаем")
        return
    processed_updates.add(update_id)

    try:
        with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
        await reply_to.reply_text("🚨 Ошибка чтения вопросов! Свяжитесь с @dimap7221.", parse_mode="Markdown")
        return

    user_questions = [q for q in data["questions"] if q["user_id"] == user_id and not q.get("cancelled", False)]
    remaining_attempts = get_remaining_attempts(user_id, data)
    if not user_questions:
        await reply_to.reply_text(
            f"📭 *Ты не задал вопросов*! *Попыток*: {remaining_attempts}/3.\n"
            f"Пиши `/ask` или `/guide`! 🚀",
            parse_mode="Markdown"
        )
        logger.info(f"Пользователь user_id {user_id} запросил свои вопросы: список активных вопросов пуст")
        return

    response = f"*📋 Твои вопросы* (*Попыток*: {remaining_attempts}/3):\n\n"
    for q in user_questions:
        status = STATUS_TRANSLATIONS.get(q["status"], q["status"])
        escaped_question = escape_markdown(q["question"], version=2)
        escaped_answer = escape_markdown(q["answer"], version=2) if q["status"] == "approved" and "answer" in q else ""
        answer = f"\n**Ответ**: *{escaped_answer}*" if q["status"] == "approved" and "answer" in q else ""
        reject_reason = f"\n**Причина**: *{escape_markdown(q['reject_reason'], version=2)}*" if q.get("reject_reason") and q["status"] == "rejected" else ""
        cancel_reason = f"\n**Причина**: *{escape_markdown(q['cancel_reason'], version=2)}*" if q.get("cancel_reason") and q["status"] == "cancelled" else ""
        response += f"**ID**: `{q['id']}`\n**Вопрос**: *{escaped_question}*\n**Статус**: `{status}`{answer}{reject_reason}{cancel_reason}\n\n"

    try:
        await reply_to.reply_text(response, parse_mode="MarkdownV2")
        logger.info(f"Пользователь user_id {user_id} запросил свои вопросы: {len(user_questions)} активных вопросов")
    except Exception as e:
        logger.error(f"Ошибка отправки списка вопросов: {e}")
        plain_response = f"📋 Твои вопросы (Попыток: {remaining_attempts}/3):\n\n"
        for q in user_questions:
            status = STATUS_TRANSLATIONS.get(q["status"], q["status"])
            answer = f"\nОтвет: {q['answer']}" if q["status"] == "approved" and "answer" in q else ""
            reject_reason = f"\nПричина: {q['reject_reason']}" if q.get("reject_reason") and q["status"] == "rejected" else ""
            cancel_reason = f"\nПричина: {q['cancel_reason']}" if q.get("cancel_reason") and q["status"] == "cancelled" else ""
            plain_response += f"ID: {q['id']}\nВопрос: {q['question']}\nСтатус: {status}{answer}{reject_reason}{cancel_reason}\n\n"
        await reply_to.reply_text(plain_response)
        logger.info(f"Отправлен список вопросов в plain-text формате из-за ошибки MarkdownV2")

async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /ask от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    if update.message.chat.type != "private":
        await update.message.reply_text("🚫 Команда доступна только в личных сообщениях!", parse_mode="Markdown")
        logger.info(f"Попытка использовать /ask в чате {update.message.chat.type} от user_id {update.effective_user.id}")
        return
    update_id = update.update_id
    if update_id in processed_updates:
        logger.info(f"Дубликат update_id {update_id}, пропускаем")
        return
    processed_updates.add(update_id)

    user = update.message.from_user
    user_id = user.id
    username = user.username or "Аноним"
    question = " ".join(context.args) if context.args else update.message.text.split("/ask", 1)[-1].strip()
    question_hash = get_question_hash(question)

    if not question:
        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("🚨 Ошибка записи вопроса! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return
        remaining_attempts = get_remaining_attempts(user_id, data)
        await update.message.reply_text(
            f"❓ Напиши `/ask <вопрос>`, например: `/ask Какая твоя любимая игра?`\n"
            f"📌 *Попыток*: {remaining_attempts}/3",
            parse_mode="Markdown"
        )
        return

    is_valid, reason = check_question_meaning(question)
    if not is_valid:
        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("🚨 Ошибка записи вопроса! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return
        remaining_attempts = get_remaining_attempts(user_id, data)
        await update.message.reply_text(
            f"❌ Вопрос отклонён: {reason} 😿\n"
            f"📌 *Попыток*: {remaining_attempts}/3\n"
            f"Смотри `/guide` для подсказок!",
            parse_mode="Markdown"
        )
        logger.info(f"Вопрос отклонён от user_id {user_id}: {reason} ({question})")
        return

    current_time = time.time()
    if user_id in spam_protection and current_time - spam_protection[user_id]["last_ask_time"] < 60:
        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("🚨 Ошибка записи вопроса! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return
        remaining_attempts = get_remaining_attempts(user_id, data)
        await update.message.reply_text(
            f"⏳ *Не так быстро!* Один вопрос в минуту.\n"
            f"📌 *Попыток*: {remaining_attempts}/3",
            parse_mode="Markdown"
        )
        logger.info(f"Спам-атака от user_id {user_id}: слишком частые вопросы")
        return

    try:
        with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
        await update.message.reply_text("🚨 Ошибка записи вопроса! Свяжитесь с @dimap7221.", parse_mode="Markdown")
        return

    if question_hash in question_hashes.get(user_id, []):
        remaining_attempts = get_remaining_attempts(user_id, data)
        await update.message.reply_text(
            f"🔁 *Этот вопрос уже задан!* 😺\n"
            f"📌 *Попыток*: {remaining_attempts}/3",
            parse_mode="Markdown"
        )
        return

    is_similar, similar_question = check_question_similarity(question, data["questions"])
    if is_similar:
        remaining_attempts = get_remaining_attempts(user_id, data)
        await update.message.reply_text(
            f"⚠️ *Похожий вопрос*: *{escape_markdown(similar_question, version=2)}*\n"
            f"📌 *Попыток*: {remaining_attempts}/3",
            parse_mode="MarkdownV2"
        )
        return

    pending_questions = [q for q in data["questions"] if q["user_id"] == user_id and q["status"] == "pending" and not q.get("cancelled", False)]
    if len(pending_questions) >= MAX_PENDING_QUESTIONS:
        await update.message.reply_text(
            f"⚠️ *Лимит {MAX_PENDING_QUESTIONS} вопроса!* Дождись ответа.\n"
            f"Смотри `/guide`!",
            parse_mode="Markdown"
        )
        return

    if len(question) < 5 or len(question) > 500:
        remaining_attempts = get_remaining_attempts(user_id, data)
        await update.message.reply_text(
            f"📏 Вопрос должен быть 5–500 символов!\n"
            f"📌 *Попыток*: {remaining_attempts}/3",
            parse_mode="Markdown"
        )
        return

    if check_blacklist(question):
        remaining_attempts = get_remaining_attempts(user_id, data)
        await update.message.reply_text(
            f"🚫 Вопрос содержит *запрещённые слова*!\n"
            f"📌 *Попыток*: {remaining_attempts}/3",
            parse_mode="Markdown"
        )
        return

    question_id = len(data["questions"]) + 1
    data["questions"].append({
        "id": question_id,
        "user_id": user_id,
        "username": username,
        "question": question,
        "status": "pending",
        "notify": False,
        "cancelled": False,
        "cancel_reason": "",
        "reject_reason": ""
    })

    try:
        with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
            updated_data = json.load(f)
        if not any(q["id"] == question_id for q in updated_data["questions"]):
            raise IOError("Вопрос не был записан в questions.json")
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Ошибка записи/проверки {QUESTIONS_FILE}: {e}")
        await update.message.reply_text("🚨 Ошибка записи вопроса! Свяжитесь с @dimap7221.", parse_mode="Markdown")
        return

    if user_id not in question_hashes:
        question_hashes[user_id] = []
    question_hashes[user_id].append(question_hash)
    spam_protection[user_id] = {"last_ask_time": current_time, "last_question": question}

    remaining_attempts = get_remaining_attempts(user_id, updated_data)
    keyboard = [[InlineKeyboardButton("Уведомить 🔔", callback_data=f"notify_{question_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"✅ *Вопрос принят!* 😸 Жди ответа на [сайте]({QA_WEBSITE})\n"
        f"📌 *Попыток*: {remaining_attempts}/3",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"*🔔 Новый вопрос* \\(ID: `{question_id}`\\)\n"
             f"**От**: @{escape_markdown(username, version=2)}\n"
             f"**Вопрос**: *{escape_markdown(question, version=2)}*\n"
             f"• `/approve {question_id} <ответ>`\n"
             f"• `/reject {question_id} <причина>`\n"
             f"• `/cancel {question_id} <причина>`",
        parse_mode="MarkdownV2"
    )

async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /approve от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 *Только админ* может это делать! 😎", parse_mode="Markdown")
        logger.warning(f"Неавторизованная попытка /approve от user_id {update.message.from_user.id}")
        return
    update_id = update.update_id
    if update_id in processed_updates:
        logger.info(f"Дубликат update_id {update_id}, пропускаем")
        return
    processed_updates.add(update_id)

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            f"❌ Укажи ID и ответ: `/approve <id> <ответ>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /approve: отсутствует ID или ответ, команда: {update.message.text}")
        return

    try:
        question_id = int(args[0])
        answer = " ".join(args[1:])
        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("🚨 Ошибка чтения вопросов! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return

        for q in data["questions"]:
            if q["id"] == question_id and q["status"] == "pending" and not q.get("cancelled", False):
                q["status"] = "approved"
                q["answer"] = answer
                q["published"] = True
                notify_button = []
                if not q["notify"]:
                    notify_button = [[InlineKeyboardButton("Отправить уведомление 🔔", callback_data=f"send_notify_approved_{question_id}")]]
                reply_markup = InlineKeyboardMarkup(notify_button)
                await update.message.reply_text(
                    f"✅ Вопрос `{question_id}` *принят*!\n"
                    f"**Ответ**: *{answer}*\n"
                    f"Опубликован на [сайте]({QA_WEBSITE})",
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
                if q["notify"]:
                    try:
                        escaped_answer = escape_markdown(answer, version=2)
                        await context.bot.send_message(
                            chat_id=q["user_id"],
                            text=f"✅ *Вопрос принят!* 😎\n"
                                 f"**Ответ**: *{escaped_answer}*\n"
                                 f"Смотри на [сайте]({QA_WEBSITE})",
                            parse_mode="MarkdownV2"
                        )
                        logger.info(f"Уведомление о принятии отправлено user_id {q['user_id']} для вопроса ID {question_id}")
                    except Exception as e:
                        logger.error(f"Ошибка уведомления пользователя {q['user_id']}: {e}")
                        await context.bot.send_message(
                            chat_id=q["user_id"],
                            text=f"✅ Вопрос принят! 😎\n"
                                 f"Ответ: {answer}\n"
                                 f"Смотри на сайте: {QA_WEBSITE}",
                            parse_mode=None
                        )
                break
        else:
            await update.message.reply_text(
                f"❌ Вопрос ID `{question_id}` не найден, обработан или аннулирован!",
                parse_mode="Markdown"
            )
            logger.warning(f"Вопрос ID {question_id} не найден, уже обработан или аннулирован")
            return

        try:
            with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("🚨 Ошибка записи ответа! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return

        logger.info(f"Вопрос ID {question_id} принят, ответ: {answer}")
    except ValueError:
        await update.message.reply_text(
            f"❌ ID должен быть числом: `/approve <id> <ответ>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /approve: неверный формат ID, команда: {update.message.text}")

async def approve_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /approve_all от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 *Только админ* может это делать! 😎", parse_mode="Markdown")
        logger.warning(f"Неавторизованная попытка /approve_all от user_id {update.message.from_user.id}")
        return
    update_id = update.update_id
    if update_id in processed_updates:
        logger.info(f"Дубликат update_id {update_id}, пропускаем")
        return
    processed_updates.add(update_id)

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            f"❌ Укажи ID (через запятую) и ответ: `/approve_all <id1,id2,...> <ответ>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /approve_all: отсутствует ID или ответ, команда: {update.message.text}")
        return

    try:
        question_ids = [int(x) for x in args[0].split(",")]
        answer = " ".join(args[1:])
        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("🚨 Ошибка чтения вопросов! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return

        processed_ids = []
        failed_ids = []
        for question_id in question_ids:
            for q in data["questions"]:
                if q["id"] == question_id and q["status"] == "pending" and not q.get("cancelled", False):
                    q["status"] = "approved"
                    q["answer"] = answer
                    q["published"] = True
                    processed_ids.append(question_id)
                    if q["notify"]:
                        try:
                            escaped_answer = escape_markdown(answer, version=2)
                            await context.bot.send_message(
                                chat_id=q["user_id"],
                                text=f"✅ *Вопрос принят!* 😎\n"
                                     f"**Ответ**: *{escaped_answer}*\n"
                                     f"Смотри на [сайте]({QA_WEBSITE})",
                                parse_mode="MarkdownV2"
                            )
                            logger.info(f"Уведомление о принятии отправлено user_id {q['user_id']} для вопроса ID {question_id}")
                        except Exception as e:
                            logger.error(f"Ошибка уведомления пользователя {q['user_id']}: {e}")
                            await context.bot.send_message(
                                chat_id=q["user_id"],
                                text=f"✅ Вопрос принят! 😎\n"
                                     f"Ответ: {answer}\n"
                                     f"Смотри на сайте: {QA_WEBSITE}",
                                parse_mode=None
                            )
                    break
            else:
                failed_ids.append(question_id)

        if not processed_ids:
            await update.message.reply_text(
                f"❌ Все указанные ID ({', '.join(map(str, question_ids))}) не найдены, обработаны или аннулированы!",
                parse_mode="Markdown"
            )
            logger.warning(f"Вопросы ID {', '.join(map(str, question_ids))} не найдены, уже обработаны или аннулированы")
            return

        try:
            with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("🚨 Ошибка записи ответа! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return

        response = f"✅ Вопросы `{', '.join(map(str, processed_ids))}` *приняты*!\n**Ответ**: *{answer}*\nОпубликованы на [сайте]({QA_WEBSITE})"
        if failed_ids:
            response += f"\n❌ Не обработаны ID: `{', '.join(map(str, failed_ids))}` (не найдены, обработаны или аннулированы)"
        notify_buttons = [[InlineKeyboardButton(f"Отправить уведомление 🔔 для ID {qid}", callback_data=f"send_notify_approved_{qid}")]
                         for qid in processed_ids if any(q["id"] == qid and not q["notify"] for q in data["questions"])]
        reply_markup = InlineKeyboardMarkup(notify_buttons) if notify_buttons else None
        await update.message.reply_text(response, reply_markup=reply_markup, parse_mode="Markdown")
        logger.info(f"Вопросы ID {', '.join(map(str, processed_ids))} приняты, ответ: {answer}")
    except ValueError:
        await update.message.reply_text(
            f"❌ ID должны быть числами, разделёнными запятыми: `/approve_all <id1,id2,...> <ответ>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /approve_all: неверный формат ID, команда: {update.message.text}")

async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /reject от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 *Только админ* может это делать! 😎", parse_mode="Markdown")
        logger.warning(f"Неавторизованная попытка /reject от user_id {update.message.from_user.id}")
        return
    update_id = update.update_id
    if update_id in processed_updates:
        logger.info(f"Дубликат update_id {update_id}, пропускаем")
        return
    processed_updates.add(update_id)

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            f"❌ Укажи ID и причину: `/reject <id> <причина>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /reject: отсутствует ID или причина, команда: {update.message.text}")
        return

    try:
        question_id = int(args[0])
        reject_reason = " ".join(args[1:])
        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("🚨 Ошибка чтения вопросов! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return

        for q in data["questions"]:
            if q["id"] == question_id and q["status"] == "pending" and not q.get("cancelled", False):
                q["status"] = "rejected"
                q["reject_reason"] = reject_reason
                notify_button = []
                if not q["notify"]:
                    notify_button = [[InlineKeyboardButton("Отправить уведомление 🔔", callback_data=f"send_notify_rejected_{question_id}")]]
                reply_markup = InlineKeyboardMarkup(notify_button)
                await update.message.reply_text(
                    f"❌ Вопрос `{question_id}` *отклонён*!\n"
                    f"**Причина**: *{reject_reason}*",
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
                if q["notify"]:
                    try:
                        escaped_reason = escape_markdown(reject_reason, version=2)
                        await context.bot.send_message(
                            chat_id=q["user_id"],
                            text=f"❌ *Вопрос отклонён!* 😕\n"
                                 f"**Причина**: *{escaped_reason}*\n"
                                 f"Попробуй другой. Подробности: `/guide`",
                            parse_mode="MarkdownV2"
                        )
                        logger.info(f"Уведомление об отклонении отправлено user_id {q['user_id']} для вопроса ID {question_id}")
                    except Exception as e:
                        logger.error(f"Ошибка уведомления пользователя {q['user_id']}: {e}")
                        await context.bot.send_message(
                            chat_id=q["user_id"],
                            text=f"❌ Вопрос отклонён! 😕\n"
                                 f"Причина: {reject_reason}\n"
                                 f"Попробуй другой. Подробности: /guide",
                            parse_mode=None
                        )
                break
        else:
            await update.message.reply_text(
                f"❌ Вопрос ID `{question_id}` не найден, обработан или аннулирован!",
                parse_mode="Markdown"
            )
            logger.warning(f"Вопрос ID {question_id} не найден, уже обработан или аннулирован")
            return

        try:
            with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("🚨 Ошибка записи статуса! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return

        logger.info(f"Вопрос ID {question_id} отклонён, причина: {reject_reason}")
    except ValueError:
        await update.message.reply_text(
            f"❌ ID должен быть числом: `/reject <id> <причина>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /reject: неверный формат ID, команда: {update.message.text}")

async def reject_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /reject_all от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 *Только админ* может это делать! 😎", parse_mode="Markdown")
        logger.warning(f"Неавторизованная попытка /reject_all от user_id {update.message.from_user.id}")
        return
    update_id = update.update_id
    if update_id in processed_updates:
        logger.info(f"Дубликат update_id {update_id}, пропускаем")
        return
    processed_updates.add(update_id)

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            f"❌ Укажи ID (через запятую) и причину: `/reject_all <id1,id2,...> <причина>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /reject_all: отсутствует ID или причина, команда: {update.message.text}")
        return

    try:
        question_ids = [int(x) for x in args[0].split(",")]
        reject_reason = " ".join(args[1:])
        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("🚨 Ошибка чтения вопросов! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return

        processed_ids = []
        failed_ids = []
        for question_id in question_ids:
            for q in data["questions"]:
                if q["id"] == question_id and q["status"] == "pending" and not q.get("cancelled", False):
                    q["status"] = "rejected"
                    q["reject_reason"] = reject_reason
                    processed_ids.append(question_id)
                    if q["notify"]:
                        try:
                            escaped_reason = escape_markdown(reject_reason, version=2)
                            await context.bot.send_message(
                                chat_id=q["user_id"],
                                text=f"❌ *Вопрос отклонён!* 😕\n"
                                     f"**Причина**: *{escaped_reason}*\n"
                                     f"Попробуй другой. Подробности: `/guide`",
                                parse_mode="MarkdownV2"
                            )
                            logger.info(f"Уведомление об отклонении отправлено user_id {q['user_id']} для вопроса ID {question_id}")
                        except Exception as e:
                            logger.error(f"Ошибка уведомления пользователя {q['user_id']}: {e}")
                            await context.bot.send_message(
                                chat_id=q["user_id"],
                                text=f"❌ Вопрос отклонён! 😕\n"
                                     f"Причина: {reject_reason}\n"
                                     f"Попробуй другой. Подробности: /guide",
                                parse_mode=None
                            )
                    break
            else:
                failed_ids.append(question_id)

        if not processed_ids:
            await update.message.reply_text(
                f"❌ Все указанные ID ({', '.join(map(str, question_ids))}) не найдены, обработаны или аннулированы!",
                parse_mode="Markdown"
            )
            logger.warning(f"Вопросы ID {', '.join(map(str, question_ids))} не найдены, уже обработаны или аннулированы")
            return

        try:
            with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("🚨 Ошибка записи статуса! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return

        response = f"❌ Вопросы `{', '.join(map(str, processed_ids))}` *отклонены*!\n**Причина**: *{reject_reason}*"
        if failed_ids:
            response += f"\n❌ Не обработаны ID: `{', '.join(map(str, failed_ids))}` (не найдены, обработаны или аннулированы)"
        notify_buttons = [[InlineKeyboardButton(f"Отправить уведомление 🔔 для ID {qid}", callback_data=f"send_notify_rejected_{qid}")]
                         for qid in processed_ids if any(q["id"] == qid and not q["notify"] for q in data["questions"])]
        reply_markup = InlineKeyboardMarkup(notify_buttons) if notify_buttons else None
        await update.message.reply_text(response, reply_markup=reply_markup, parse_mode="Markdown")
        logger.info(f"Вопросы ID {', '.join(map(str, processed_ids))} отклонены, причина: {reject_reason}")
    except ValueError:
        await update.message.reply_text(
            f"❌ ID должны быть числами, разделёнными запятыми: `/reject_all <id1,id2,...> <причина>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /reject_all: неверный формат ID, команда: {update.message.text}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /cancel от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 *Только админ* может это делать! 😎", parse_mode="Markdown")
        logger.warning(f"Неавторизованная попытка /cancel от user_id {update.message.from_user.id}")
        return
    update_id = update.update_id
    if update_id in processed_updates:
        logger.info(f"Дубликат update_id {update_id}, пропускаем")
        return
    processed_updates.add(update_id)

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            f"❌ Укажи ID и причину: `/cancel <id> <причина>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /cancel: отсутствует ID или причина, команда: {update.message.text}")
        return

    try:
        question_id = int(args[0])
        cancel_reason = " ".join(args[1:])
        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("🚨 Ошибка чтения вопросов! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return

        for q in data["questions"]:
            if q["id"] == question_id and not q.get("cancelled", False):
                q["status"] = "cancelled"
                q["cancelled"] = True
                q["cancel_reason"] = cancel_reason
                notify_button = []
                if not q["notify"]:
                    notify_button = [[InlineKeyboardButton("Отправить уведомление 🔔", callback_data=f"send_notify_cancelled_{question_id}")]]
                reply_markup = InlineKeyboardMarkup(notify_button)
                await update.message.reply_text(
                    f"❌ Вопрос `{question_id}` *аннулирован*!\n"
                    f"**Причина**: *{cancel_reason}*",
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
                if q["notify"]:
                    try:
                        escaped_reason = escape_markdown(cancel_reason, version=2)
                        await context.bot.send_message(
                            chat_id=q["user_id"],
                            text=f"❌ *Вопрос аннулирован!* 😿\n"
                                 f"**Причина**: *{escaped_reason}*\n"
                                 f"Подробности: `/guide`",
                            parse_mode="MarkdownV2"
                        )
                        logger.info(f"Уведомление об аннулировании отправлено user_id {q['user_id']} для вопроса ID {question_id}")
                    except Exception as e:
                        logger.error(f"Ошибка уведомления пользователя {q['user_id']}: {e}")
                        await context.bot.send_message(
                            chat_id=q["user_id"],
                            text=f"❌ Вопрос аннулирован! 😿\n"
                                 f"Причина: {cancel_reason}\n"
                                 f"Подробности: /guide",
                            parse_mode=None
                        )
                break
        else:
            await update.message.reply_text(
                f"❌ Вопрос ID `{question_id}` не найден или уже аннулирован!",
                parse_mode="Markdown"
            )
            logger.warning(f"Вопрос ID {question_id} не найден или уже аннулирован")
            return

        try:
            with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("🚨 Ошибка записи статуса! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return

        logger.info(f"Вопрос ID {question_id} аннулирован, причина: {cancel_reason}")
    except ValueError:
        await update.message.reply_text(
            f"❌ ID должен быть числом: `/cancel <id> <причина>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /cancel: неверный формат ID, команда: {update.message.text}")

async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /delete от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 *Только админ* может это делать! 😎", parse_mode="Markdown")
        logger.warning(f"Неавторизованная попытка /delete от user_id {update.message.from_user.id}")
        return
    update_id = update.update_id
    if update_id in processed_updates:
        logger.info(f"Дубликат update_id {update_id}, пропускаем")
        return
    processed_updates.add(update_id)

    args = context.args
    if not args:
        await update.message.reply_text(
            f"❌ Укажи ID: `/delete <id>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /delete: отсутствует ID, команда: {update.message.text}")
        return

    try:
        question_id = int(args[0])
        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("🚨 Ошибка чтения вопросов! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return

        original_length = len(data["questions"])
        data["questions"] = [q for q in data["questions"] if q["id"] != question_id]
        if len(data["questions"]) == original_length:
            await update.message.reply_text(
                f"❌ Вопрос ID `{question_id}` *не найден*!",
                parse_mode="Markdown"
            )
            logger.warning(f"Вопрос ID {question_id} не найден для удаления")
            return

        try:
            with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("🚨 Ошибка удаления вопроса! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return

        await update.message.reply_text(
            f"🗑️ Вопрос `{question_id}` *удалён*!",
            parse_mode="Markdown"
        )
        logger.info(f"Вопрос ID {question_id} удалён")
    except ValueError:
        await update.message.reply_text(
            f"❌ ID должен быть числом: `/delete <id>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /delete: неверный формат ID, команда: {update.message.text}")

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /clear от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 *Только админ* может это делать! 😎", parse_mode="Markdown")
        logger.warning(f"Неавторизованная попытка /clear от user_id {update.message.from_user.id}")
        return
    update_id = update.update_id
    if update_id in processed_updates:
        logger.info(f"Дубликат update_id {update_id}, пропускаем")
        return
    processed_updates.add(update_id)

    try:
        data = {"questions": []}
        with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except IOError as e:
        logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
        await update.message.reply_text("🚨 Ошибка очистки вопросов! Свяжитесь с @dimap7221.", parse_mode="Markdown")
        return

    await update.message.reply_text(
        f"🗑️ *Все вопросы очищены*!",
        parse_mode="Markdown"
    )
    logger.info("Все вопросы очищены")

async def edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /edit от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("🚫 *Только админ* может это делать! 😎", parse_mode="Markdown")
        logger.warning(f"Неавторизованная попытка /edit от user_id {update.message.from_user.id}")
        return
    update_id = update.update_id
    if update_id in processed_updates:
        logger.info(f"Дубликат update_id {update_id}, пропускаем")
        return
    processed_updates.add(update_id)

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            f"❌ Укажи ID и новый вопрос: `/edit <id> <вопрос>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /edit: отсутствует ID или новый вопрос, команда: {update.message.text}")
        return

    try:
        question_id = int(args[0])
        new_question = " ".join(args[1:])
        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("🚨 Ошибка чтения вопросов! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return

        for q in data["questions"]:
            if q["id"] == question_id and not q.get("cancelled", False):
                old_question = q["question"]
                q["question"] = new_question
                break
        else:
            await update.message.reply_text(
                f"❌ Вопрос ID `{question_id}` не найден или аннулирован!",
                parse_mode="Markdown"
            )
            logger.warning(f"Вопрос ID {question_id} не найден или аннулирован для редактирования")
            return

        try:
            with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("🚨 Ошибка записи вопроса! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return

        await update.message.reply_text(
            f"✏️ Вопрос `{question_id}` *отредактирован*!\n"
            f"**Новый вопрос**: *{new_question}*",
            parse_mode="Markdown"
        )
        logger.info(f"Вопрос ID {question_id} отредактирован: {old_question} -> {new_question}")
    except ValueError:
        await update.message.reply_text(
            f"❌ ID должен быть числом: `/edit <id> <вопрос>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /edit: неверный формат ID, команда: {update.message.text}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    callback_data = query.data
    logger.info(f"Callback {callback_data} от user_id {user_id}")

    if callback_data.startswith("notify_"):
        try:
            question_id = int(callback_data.split("_")[1])
            try:
                with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
                await query.message.reply_text("🚨 Ошибка уведомления! Свяжитесь с @dimap7221.", parse_mode="Markdown")
                return

            for q in data["questions"]:
                if q["id"] == question_id and q["user_id"] == user_id and not q.get("cancelled", False):
                    q["notify"] = True
                    break
            else:
                await query.message.reply_text("❌ Вопрос не найден или аннулирован!", parse_mode="Markdown")
                logger.warning(f"Вопрос ID {question_id} не найден или аннулирован для уведомления user_id {user_id}")
                return

            try:
                with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except IOError as e:
                logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
                await query.message.reply_text("🚨 Ошибка уведомления! Свяжитесь с @dimap7221.", parse_mode="Markdown")
                return

            await query.message.edit_text(
                f"✅ *Вопрос принят!* 😸 Ты будешь уведомлён.\n"
                f"Подробности: `/guide`",
                parse_mode="Markdown"
            )
            logger.info(f"Пользователь user_id {user_id} включил уведомления для вопроса ID {question_id}")
        except ValueError:
            logger.error(f"Ошибка в обработке notify callback: неверный формат ID в {callback_data}")
            await query.message.reply_text("🚨 Ошибка обработки уведомления! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return

    elif callback_data.startswith("send_notify_"):
        try:
            action, question_id = callback_data.split("_")[2], int(callback_data.split("_")[3])
            try:
                with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
                await query.message.reply_text("🚨 Ошибка уведомления! Свяжитесь с @dimap7221.", parse_mode="Markdown")
                return

            for q in data["questions"]:
                if q["id"] == question_id:
                    if action == "approved":
                        try:
                            escaped_answer = escape_markdown(q["answer"], version=2)
                            await context.bot.send_message(
                                chat_id=q["user_id"],
                                text=f"✅ *Вопрос принят!* 😎\n"
                                     f"**Ответ**: *{escaped_answer}*\n"
                                     f"Смотри на [сайте]({QA_WEBSITE})",
                                parse_mode="MarkdownV2"
                            )
                            await query.message.reply_text(
                                f"🔔 Уведомление о принятии вопроса `{question_id}` отправлено!",
                                parse_mode="Markdown"
                            )
                            logger.info(f"Уведомление о принятии вопроса ID {question_id} отправлено user_id {q['user_id']}")
                        except Exception as e:
                            logger.error(f"Ошибка отправки уведомления пользователю {q['user_id']}: {e}")
                            await context.bot.send_message(
                                chat_id=q["user_id"],
                                text=f"✅ Вопрос принят! 😎\n"
                                     f"Ответ: {q['answer']}\n"
                                     f"Смотри на сайте: {QA_WEBSITE}",
                                parse_mode=None
                            )
                            await query.message.reply_text(
                                f"🔔 Уведомление о принятии вопроса `{question_id}` отправлено (без Markdown)!",
                                parse_mode="Markdown"
                            )
                    elif action == "rejected":
                        try:
                            escaped_reason = escape_markdown(q["reject_reason"], version=2)
                            await context.bot.send_message(
                                chat_id=q["user_id"],
                                text=f"❌ *Вопрос отклонён!* 😕\n"
                                     f"**Причина**: *{escaped_reason}*\n"
                                     f"Попробуй другой. Подробности: `/guide`",
                                parse_mode="MarkdownV2"
                            )
                            await query.message.reply_text(
                                f"🔔 Уведомление об отклонении вопроса `{question_id}` отправлено!",
                                parse_mode="Markdown"
                            )
                            logger.info(f"Уведомление об отклонении вопроса ID {question_id} отправлено user_id {q['user_id']}")
                        except Exception as e:
                            logger.error(f"Ошибка отправки уведомления пользователю {q['user_id']}: {e}")
                            await context.bot.send_message(
                                chat_id=q["user_id"],
                                text=f"❌ Вопрос отклонён! 😕\n"
                                     f"Причина: {q['reject_reason']}\n"
                                     f"Попробуй другой. Подробности: /guide",
                                parse_mode=None
                            )
                            await query.message.reply_text(
                                f"🔔 Уведомление об отклонении вопроса `{question_id}` отправлено (без Markdown)!",
                                parse_mode="Markdown"
                            )
                    elif action == "cancelled":
                        try:
                            escaped_reason = escape_markdown(q["cancel_reason"], version=2)
                            await context.bot.send_message(
                                chat_id=q["user_id"],
                                text=f"❌ *Вопрос аннулирован!* 😿\n"
                                     f"**Причина**: *{escaped_reason}*\n"
                                     f"Подробности: `/guide`",
                                parse_mode="MarkdownV2"
                            )
                            await query.message.reply_text(
                                f"🔔 Уведомление об аннулировании вопроса `{question_id}` отправлено!",
                                parse_mode="Markdown"
                            )
                            logger.info(f"Уведомление об аннулировании вопроса ID {question_id} отправлено user_id {q['user_id']}")
                        except Exception as e:
                            logger.error(f"Ошибка отправки уведомления пользователю {q['user_id']}: {e}")
                            await context.bot.send_message(
                                chat_id=q["user_id"],
                                text=f"❌ Вопрос аннулирован! 😿\n"
                                     f"Причина: {q['cancel_reason']}\n"
                                     f"Подробности: /guide",
                                parse_mode=None
                            )
                            await query.message.reply_text(
                                f"🔔 Уведомление об аннулировании вопроса `{question_id}` отправлено (без Markdown)!",
                                parse_mode="Markdown"
                            )
                    break
            else:
                await query.message.reply_text("❌ Вопрос не найден!", parse_mode="Markdown")
                logger.warning(f"Вопрос ID {question_id} не найден для уведомления")
        except (ValueError, IndexError) as e:
            logger.error(f"Ошибка обработки send_notify callback: {e}, callback_data: {callback_data}")
            await query.message.reply_text("🚨 Ошибка обработки уведомления! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return

    elif callback_data == "ask":
        await query.message.reply_text(
            f"❓ Напиши `/ask <вопрос>`, например: `/ask Какая твоя любимая игра?`\n"
            f"Смотри `/guide` для подсказок!",
            parse_mode="Markdown"
        )

    elif callback_data == "myquestions":
        await my_questions(update, context)

    elif callback_data == "guide":
        await guide(update, context)

async def main_async():
    logger.info("Бот стартовал")
    try:
        app = Application.builder().token(TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("guide", guide))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("list", list_questions))
        app.add_handler(CommandHandler("myquestions", my_questions))
        app.add_handler(CommandHandler("ask", ask))
        app.add_handler(CommandHandler("approve", approve))
        app.add_handler(CommandHandler("approve_all", approve_all))
        app.add_handler(CommandHandler("reject", reject))
        app.add_handler(CommandHandler("reject_all", reject_all))
        app.add_handler(CommandHandler("cancel", cancel))
        app.add_handler(CommandHandler("delete", delete))
        app.add_handler(CommandHandler("clear", clear))
        app.add_handler(CommandHandler("edit", edit))
        app.add_handler(CallbackQueryHandler(button_callback, pattern="^(notify_|send_notify_|ask|myquestions|guide)"))
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Бот успешно запущен в режиме polling")
        while True:
            await asyncio.sleep(3600)
    except Exception as e:
        logger.error(f"Ошибка инициализации бота: {e}")
        raise

if __name__ == "__main__":
    import sys
    asyncio.run(main_async())
