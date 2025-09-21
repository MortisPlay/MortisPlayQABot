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
SIMILARITY_THRESHOLD = 0.8  # Порог схожести для вопросов

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

def check_question_meaning(question: str) -> bool:
    question_lower = question.lower().strip()
    if len(question_lower) < 10:
        logger.info(f"Вопрос отклонён как бессмысленный: слишком короткий ({len(question_lower)} символов)")
        return False
    if re.match(r'^(.)\1{4,}$', question_lower.replace(' ', '')) or re.match(r'^(\W)\1{4,}$', question_lower):
        logger.info(f"Вопрос отклонён как бессмысленный: повторяющиеся символы ({question})")
        return False
    words = question_lower.split()
    if len(words) > 1 and len(set(words)) == 1:
        logger.info(f"Вопрос отклонён как бессмысленный: повторяющиеся слова ({question})")
        return False
    question_words = ["что", "как", "почему", "где", "когда", "какой", "какая", "какое", "кто", "зачем", "сколько"]
    has_question_word = any(word in question_lower for word in question_words) or "?" in question_lower
    has_multiple_words = len(words) >= 3
    if not (has_question_word or has_multiple_words):
        logger.info(f"Вопрос отклонён как бессмысленный: нет вопросительных слов или слишком прост ({question})")
        return False
    return True

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

def custom_escape_markdown(text: str) -> str:
    special_chars = r'_*[]()~`>#+-|=}{.!'
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

def get_remaining_attempts(user_id: int, data: dict) -> int:
    pending_questions = [q for q in data["questions"] if q["user_id"] == user_id and q["status"] == "pending" and not q.get("cancelled", False)]
    logger.info(f"Подсчёт попыток для user_id {user_id}: {len(pending_questions)} ожидающих вопросов")
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
    if not reply_to or (update.message and not update.message.text):
        logger.info("Пропущено невалидное или удалённое сообщение")
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
        f"📖 *Гайд для новичков*\n\n"
        f"Добро пожаловать в Q&A-бот Mortis Play! 😎\n\n"
        f"1️⃣ *Задай вопрос*: Пиши `/ask <вопрос>`, например: `/ask Какая твоя любимая игра?`\n"
        f"   Вопрос: 5–500 символов, осмысленный. *Осталось попыток*: {remaining_attempts} (макс. 3).\n\n"
        f"2️⃣ *Уведомления*: Нажми *Уведомить 🔔* после вопроса, чтобы узнать статус.\n\n"
        f"3️⃣ *Проверь вопросы*: Пиши `/myquestions` или жми *Мои вопросы*.\n\n"
        f"4️⃣ *Ответы на сайте*: Принятые вопросы публикуются на [сайте]({QA_WEBSITE}) за 1–48 часов.\n\n"
        f"5️⃣ *Вопрос не приняли?* Узнаешь, если включил уведомления. Пиши @dimap7221, если что-то не так.\n\n"
        f"6️⃣ *Лимит*: Пока 3 вопроса на рассмотрении, новые не добавишь.\n\n"
        f"🚀 *Готов?* Жми кнопки или пиши `/ask`!"
    )
    try:
        await reply_to.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        logger.info(f"Гайд отправлен пользователю user_id {user_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки гайда: {e}")
        text_plain = text.replace("*", "").replace("[сайте](https://mortisplay.ru/qa.html)", f"сайте {QA_WEBSITE}")
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
        f"• `/cancel <id> <причина>` — Аннулировать вопрос (админ)\n\n"
        f"📢 Вопросы должны быть осмысленными. Похожие вопросы не засчитываются в лимит!\n"
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
        escaped_question = custom_escape_markdown(q["question"])
        escaped_username = custom_escape_markdown(q["username"])
        cancel_reason = f", Причина: *{custom_escape_markdown(q['cancel_reason'])}*" if q.get("cancel_reason") and q["status"] == "cancelled" else ""
        response += f"**ID**: `{q['id']}`\n**От**: @{escaped_username}\n**Вопрос**: *{escaped_question}*\n**Статус**: `{status}`{cancel_reason}\n\n"

    logger.info(f"Формируем список вопросов для отправки: {response}")
    try:
        await update.message.reply_text(response, parse_mode="MarkdownV2")
        logger.info(f"Админ запросил список вопросов: {len(active_questions)} активных вопросов")
    except Exception as e:
        logger.error(f"Ошибка отправки списка вопросов: {e}")
        plain_response = "📋 Список активных вопросов:\n\n"
        for q in active_questions:
            status = STATUS_TRANSLATIONS.get(q["status"], q["status"])
            cancel_reason = f", Причина: {q['cancel_reason']}" if q.get("cancel_reason") and q["status"] == "cancelled" else ""
            plain_response += f"ID: {q['id']}\nОт: @{q['username']}\nВопрос: {q['question']}\nСтатус: {status}{cancel_reason}\n\n"
        await update.message.reply_text(plain_response)
        logger.info(f"Отправлен список вопросов в plain-text формате из-за ошибки MarkdownV2")

async def my_questions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /myquestions от user_id {update.effective_user.id}")
    user_id = update.effective_user.id
    reply_to = update.message or update.callback_query.message
    if not reply_to or (update.message and not update.message.text):
        logger.info("Пропущено невалидное или удалённое сообщение")
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
            f"📭 *Ты не задал вопросов*! Осталось попыток: *{remaining_attempts}*.\n"
            f"Пиши `/ask <вопрос>` или жми `/guide`! 🚀",
            parse_mode="Markdown"
        )
        logger.info(f"Пользователь user_id {user_id} запросил свои вопросы: список активных вопросов пуст")
        return

    response = f"*📋 Твои вопросы* (попыток: *{remaining_attempts}*):\n\n"
    for q in user_questions:
        status = STATUS_TRANSLATIONS.get(q["status"], q["status"])
        escaped_question = custom_escape_markdown(q["question"])
        escaped_answer = custom_escape_markdown(q["answer"]) if q["status"] == "approved" and "answer" in q else ""
        answer = f"\n**Ответ**: *{escaped_answer}*" if q["status"] == "approved" and "answer" in q else ""
        cancel_reason = f"\n**Причина**: *{custom_escape_markdown(q['cancel_reason'])}*" if q.get("cancel_reason") and q["status"] == "cancelled" else ""
        response += f"**ID**: `{q['id']}`\n**Вопрос**: *{escaped_question}*\n**Статус**: `{status}`{answer}{cancel_reason}\n\n"

    logger.info(f"Формируем список вопросов пользователя user_id {user_id}: {response}")
    try:
        await reply_to.reply_text(response, parse_mode="MarkdownV2")
        logger.info(f"Пользователь user_id {user_id} запросил свои вопросы: {len(user_questions)} активных вопросов")
    except Exception as e:
        logger.error(f"Ошибка отправки списка вопросов: {e}")
        plain_response = f"📋 Твои вопросы (попыток: {remaining_attempts}):\n\n"
        for q in user_questions:
            status = STATUS_TRANSLATIONS.get(q["status"], q["status"])
            answer = f"\nОтвет: {q['answer']}" if q["status"] == "approved" and "answer" in q else ""
            cancel_reason = f"\nПричина: {q['cancel_reason']}" if q.get("cancel_reason") and q["status"] == "cancelled" else ""
            plain_response += f"ID: {q['id']}\nВопрос: {q['question']}\nСтатус: {status}{answer}{cancel_reason}\n\n"
        await reply_to.reply_text(plain_response)
        logger.info(f"Отправлен список вопросов в plain-text формате из-за ошибки MarkdownV2")

async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /ask от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    update_id = update.update_id
    if update_id in processed_updates:
        logger.info(f"Дубликат update_id {update_id}, пропускаем")
        return
    processed_updates.add(update_id)

    user = update.message.from_user
    user_id = user.id
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
        text = (
            f"❓ Напиши вопрос после `/ask`, например: `/ask Какая твоя любимая игра?`\n"
            f"📌 Осталось попыток: *{remaining_attempts}*\n"
            f"Смотри `/guide` для подсказок!"
        )
        try:
            await update.message.reply_text(text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Ошибка отправки ответа на /ask (пустой вопрос): {e}")
            text_plain = text.replace("*", "")
            await update.message.reply_text(text_plain, parse_mode=None)
        return

    if not check_question_meaning(question):
        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("🚨 Ошибка записи вопроса! Свяжитесь с @dimap7221.", parse_mode="Markdown")
            return
        remaining_attempts = get_remaining_attempts(user_id, data)
        text = (
            f"❌ Вопрос *бессмысленный*! 😿 Пример: `/ask Какая твоя любимая игра?`\n"
            f"📌 Осталось попыток: *{remaining_attempts}*\n"
            f"Смотри `/guide` для подсказок!"
        )
        try:
            await update.message.reply_text(text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Ошибка отправки ответа на /ask (бессмысленный): {e}")
            text_plain = text.replace("*", "")
            await update.message.reply_text(text_plain, parse_mode=None)
        logger.info(f"Вопрос отклонён как бессмысленный от user_id {user_id}: {question}")
        return

    current_time = time.time()
    if user_id in spam_protection:
        last_ask_time = spam_protection[user_id]["last_ask_time"]
        if current_time - last_ask_time < 60:
            try:
                with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
                await update.message.reply_text("🚨 Ошибка записи вопроса! Свяжитесь с @dimap7221.", parse_mode="Markdown")
                return
            remaining_attempts = get_remaining_attempts(user_id, data)
            text = (
                f"⏳ *Не так быстро!* Один вопрос в минуту.\n"
                f"📌 Осталось попыток: *{remaining_attempts}*\n"
                f"Смотри `/guide` для подсказок!"
            )
            try:
                await update.message.reply_text(text, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Ошибка отправки ответа на /ask (спам): {e}")
                text_plain = text.replace("*", "")
                await update.message.reply_text(text_plain, parse_mode=None)
            logger.info(f"Спам-атака от user_id {user_id}: слишком частые вопросы")
            return

    try:
        with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
        await update.message.reply_text("🚨 Ошибка записи вопроса! Свяжитесь с @dimap7221.", parse_mode="Markdown")
        return

    # Проверка на точный дубликат
    if question_hash in question_hashes.get(user_id, []):
        remaining_attempts = get_remaining_attempts(user_id, data)
        text = (
            f"🔁 *Этот вопрос уже задан!* Попробуй другой.\n"
            f"📌 Осталось попыток: *{remaining_attempts}*\n"
            f"Смотри `/guide` для подсказок!"
        )
        try:
            await update.message.reply_text(text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Ошибка отправки ответа на /ask (дубликат): {e}")
            text_plain = text.replace("*", "")
            await update.message.reply_text(text_plain, parse_mode=None)
        logger.info(f"Дубликат вопроса от user_id {user_id}: {question}")
        return

    # Проверка на похожий вопрос
    is_similar, similar_question = check_question_similarity(question, data["questions"])
    if is_similar:
        remaining_attempts = get_remaining_attempts(user_id, data)
        escaped_similar = custom_escape_markdown(similar_question)
        text = (
            f"⚠️ *Похожий вопрос уже задан*: *{escaped_similar}*\n"
            f"Попробуй другой или уточни. 📌 Осталось попыток: *{remaining_attempts}*\n"
            f"Смотри `/guide` для подсказок!"
        )
        try:
            await update.message.reply_text(text, parse_mode="MarkdownV2")
        except Exception as e:
            logger.error(f"Ошибка отправки ответа на /ask (похожий): {e}")
            text_plain = text.replace("*", "").replace(f"*{escaped_similar}*", similar_question)
            await update.message.reply_text(text_plain, parse_mode=None)
        logger.info(f"Похожий вопрос от user_id {user_id}: {question} ~ {similar_question}")
        return

    pending_questions = [q for q in data["questions"] if q["user_id"] == user_id and q["status"] == "pending" and not q.get("cancelled", False)]
    if len(pending_questions) >= MAX_PENDING_QUESTIONS:
        text = (
            f"⚠️ *Лимит {MAX_PENDING_QUESTIONS} вопроса!* Дождись ответа.\n"
            f"Смотри `/guide` для подсказок!"
        )
        try:
            await update.message.reply_text(text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Ошибка отправки ответа на /ask (лимит): {e}")
            text_plain = text.replace("*", "")
            await update.message.reply_text(text_plain, parse_mode=None)
        logger.info(f"Превышен лимит ожидающих вопросов для user_id {user_id}: {len(pending_questions)}")
        return

    if len(question) < 5 or len(question) > 500:
        remaining_attempts = get_remaining_attempts(user_id, data)
        text = (
            f"📏 Вопрос должен быть 5–500 символов!\n"
            f"📌 Осталось попыток: *{remaining_attempts}*\n"
            f"Смотри `/guide` для подсказок!"
        )
        try:
            await update.message.reply_text(text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Ошибка отправки ответа на /ask (длина): {e}")
            text_plain = text.replace("*", "")
            await update.message.reply_text(text_plain, parse_mode=None)
        logger.info(f"Недопустимая длина вопроса от user_id {user_id}: {len(question)} символов")
        return

    if check_blacklist(question):
        remaining_attempts = get_remaining_attempts(user_id, data)
        text = (
            f"🚫 Вопрос содержит *запрещённые слова*! Попробуй другой.\n"
            f"📌 Осталось попыток: *{remaining_attempts}*\n"
            f"Смотри `/guide` для подсказок!"
        )
        try:
            await update.message.reply_text(text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Ошибка отправки ответа на /ask (чёрный список): {e}")
            text_plain = text.replace("*", "")
            await update.message.reply_text(text_plain, parse_mode=None)
        logger.info(f"Вопрос отклонён из-за чёрного списка: {question}")
        return

    question_id = len(data["questions"]) + 1
    data["questions"].append({
        "id": question_id,
        "user_id": user_id,
        "username": user.username or "Аноним",
        "question": question,
        "status": "pending",
        "notify": False,
        "cancelled": False,
        "cancel_reason": ""
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
    text = (
        f"✅ *Вопрос принят!* 😸 Жди ответа на [сайте]({QA_WEBSITE}) (1–48 часов).\n"
        f"📌 Осталось попыток: *{remaining_attempts}*\n"
        f"Не на сайте? Пиши @dimap7221!\n"
        f"Подробности: `/guide`"
    )
    try:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        logger.info(f"Вопрос принят от user_id {user_id}: ID {question_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки ответа на /ask (успех): {e}")
        text_plain = text.replace("*", "").replace("[сайте](https://mortisplay.ru/qa.html)", f"сайте {QA_WEBSITE}")
        await update.message.reply_text(text_plain, reply_markup=reply_markup, parse_mode=None)

    escaped_question = custom_escape_markdown(question)
    escaped_username = custom_escape_markdown(user.username or "Аноним")
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"*🔔 Новый вопрос* \\(ID: `{question_id}`\\)\n"
                 f"**От**: @{escaped_username}\n"
                 f"**Вопрос**: *{escaped_question}*\n"
                 f"• `/approve {question_id} <ответ>` — Принять\n"
                 f"• `/reject {question_id}` — Отклонить\n"
                 f"• `/cancel {question_id} <причина>` — Аннулировать",
            parse_mode="MarkdownV2"
        )
        logger.info(f"Уведомление админу отправлено: вопрос ID {question_id} от @{user.username or 'Аноним'}")
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления админу: {e}")
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🔔 Новый вопрос (ID: {question_id})\n"
                 f"От: @{user.username or 'Аноним'}\n"
                 f"Вопрос: {question}\n"
                 f"• /approve {question_id} <ответ> — Принять\n"
                 f"• /reject {question_id} — Отклонить\n"
                 f"• /cancel {question_id} <причина> — Аннулировать",
            parse_mode=None
        )

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
            logger.error(f"Ошибка обработки notify callback: неверный формат question_id {callback_data}")
            await query.message.reply_text("🚨 Ошибка уведомления! Свяжитесь с @dimap7221.", parse_mode="Markdown")

    elif callback_data == "ask":
        await query.message.reply_text(
            f"❓ Напиши `/ask <вопрос>`, например: `/ask Какая твоя любимая игра?`\n"
            f"Смотри `/guide` для подсказок!",
            parse_mode="Markdown"
        )
        logger.info(f"Пользователь user_id {user_id} нажал кнопку 'Задать вопрос'")

    elif callback_data == "myquestions":
        update.message = query.message  # Передаём message для my_questions
        await my_questions(update, context)

    elif callback_data == "guide":
        update.message = query.message  # Передаём message для guide
        await guide(update, context)

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

    logger.info(f"Команда /approve от админа: {update.message.text}")
    args = context.args
    if not args:
        await update.message.reply_text(
            f"❌ Укажи ID и ответ: `/approve <id> <ответ>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /approve: отсутствуют аргументы, команда: {update.message.text}")
        return

    try:
        question_id = int(args[0])
        answer = " ".join(args[1:]) if len(args) > 1 else None
        if not answer:
            await update.message.reply_text(
                f"❌ Укажи ответ: `/approve <id> <ответ>`",
                parse_mode="Markdown"
            )
            logger.error(f"Ошибка в /approve: отсутствует ответ, команда: {update.message.text}")
            return

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
                website_button = [[InlineKeyboardButton("На сайт 🌐", url=QA_WEBSITE)]]
                reply_markup = InlineKeyboardMarkup(website_button)
                if q["notify"]:
                    try:
                        escaped_answer = custom_escape_markdown(answer)
                        await context.bot.send_message(
                            chat_id=q["user_id"],
                            text=f"✅ *Вопрос принят!* 😎\n"
                                 f"**Ответ**: *{escaped_answer}*\n"
                                 f"Смотри на [сайте]({QA_WEBSITE})!\n"
                                 f"Подробности: `/guide`",
                            reply_markup=reply_markup,
                            parse_mode="MarkdownV2"
                        )
                        logger.info(f"Уведомление о принятии отправлено user_id {q['user_id']} для вопроса ID {question_id}")
                    except Exception as e:
                        logger.error(f"Ошибка уведомления пользователя {q['user_id']}: {e}")
                        await context.bot.send_message(
                            chat_id=q["user_id"],
                            text=f"✅ Вопрос принят! 😎\n"
                                 f"Ответ: {answer}\n"
                                 f"Смотри на сайте: {QA_WEBSITE}\n"
                                 f"Подробности: /guide",
                            reply_markup=reply_markup,
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

        website_button = [[InlineKeyboardButton("На сайт 🌐", url=QA_WEBSITE)]]
        reply_markup = InlineKeyboardMarkup(website_button)
        await update.message.reply_text(
            f"✅ Вопрос `{question_id}` *принят*!\n"
            f"**Ответ**: *{answer}* 🔥",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        logger.info(f"Вопрос ID {question_id} принят с ответом: {answer}")
    except ValueError:
        await update.message.reply_text(
            f"❌ ID должен быть числом: `/approve <id> <ответ>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /approve: неверный формат ID, команда: {update.message.text}")

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

    logger.info(f"Команда /reject от админа: {update.message.text}")
    args = context.args
    if not args:
        await update.message.reply_text(
            f"❌ Укажи ID: `/reject <id>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /reject: отсутствует ID, команда: {update.message.text}")
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

        for q in data["questions"]:
            if q["id"] == question_id and q["status"] == "pending" and not q.get("cancelled", False):
                q["status"] = "rejected"
                if q["notify"]:
                    try:
                        await context.bot.send_message(
                            chat_id=q["user_id"],
                            text=f"❌ *Вопрос отклонён!* 😕 Попробуй другой.\n"
                                 f"Подробности: `/guide`",
                            parse_mode="Markdown"
                        )
                        logger.info(f"Уведомление об отклонении отправлено user_id {q['user_id']} для вопроса ID {question_id}")
                    except Exception as e:
                        logger.error(f"Ошибка уведомления пользователя {q['user_id']}: {e}")
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

        await update.message.reply_text(
            f"❌ Вопрос `{question_id}` *отклонён*!",
            parse_mode="Markdown"
        )
        logger.info(f"Вопрос ID {question_id} отклонён")
    except ValueError:
        await update.message.reply_text(
            f"❌ ID должен быть числом: `/reject <id>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /reject: неверный формат ID, команда: {update.message.text}")

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

    logger.info(f"Команда /cancel от админа: {update.message.text}")
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
                q["cancel_reason"] = cancel_reason
                if q["notify"]:
                    try:
                        escaped_reason = custom_escape_markdown(cancel_reason)
                        await context.bot.send_message(
                            chat_id=q["user_id"],
                            text=f"❌ *Вопрос аннулирован!* 😿\n"
                                 f"**Причина**: *{escaped_reason}*\n"
                                 f"Попробуй другой! Подробности: `/guide`",
                            parse_mode="MarkdownV2"
                        )
                        logger.info(f"Уведомление об аннулировании отправлено user_id {q['user_id']} для вопроса ID {question_id}")
                    except Exception as e:
                        logger.error(f"Ошибка уведомления пользователя {q['user_id']}: {e}")
                        await context.bot.send_message(
                            chat_id=q["user_id"],
                            text=f"❌ Вопрос аннулирован! 😿\n"
                                 f"Причина: {cancel_reason}\n"
                                 f"Попробуй другой! Подробности: /guide",
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

        await update.message.reply_text(
            f"❌ Вопрос `{question_id}` *аннулирован*!\n"
            f"**Причина**: *{cancel_reason}*",
            parse_mode="Markdown"
        )
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

    logger.info(f"Команда /delete от админа: {update.message.text}")
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

    logger.info(f"Команда /edit от админа: {update.message.text}")
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

async def main_async():
    logger.info(f"Бот стартовал с Python {sys.version}")
    logger.info(f"Используемый токен: {TOKEN[:10]}...{TOKEN[-10:]}")

    try:
        app = Application.builder().token(TOKEN).build()

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("guide", guide))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("list", list_questions))
        app.add_handler(CommandHandler("myquestions", my_questions))
        app.add_handler(CommandHandler("ask", ask))
        app.add_handler(CommandHandler("approve", approve))
        app.add_handler(CommandHandler("reject", reject))
        app.add_handler(CommandHandler("cancel", cancel))
        app.add_handler(CommandHandler("delete", delete))
        app.add_handler(CommandHandler("clear", clear))
        app.add_handler(CommandHandler("edit", edit))
        app.add_handler(CallbackQueryHandler(button_callback, pattern="^(notify_|ask|myquestions|guide)"))

        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Бот успешно запущен в режиме polling")
        while True:
            await asyncio.sleep(3600)  # Держим бота активным
    except Exception as e:
        logger.error(f"Ошибка инициализации бота: {e}")
        raise

if __name__ == "__main__":
    import sys
    asyncio.run(main_async())