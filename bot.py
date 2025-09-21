import json
import logging
import os
import time
import asyncio
import hashlib
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from dotenv import load_dotenv

# Настройка логирования (stdout + файл)
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
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN не задан в переменной окружения или .env файле!")

# Константы
ADMIN_ID = 335236137
QUESTIONS_FILE = "questions.json"
BLACKLIST_FILE = "blacklist.json"
QA_WEBSITE = "https://mortisplay.ru/qa.html"
MAX_PENDING_QUESTIONS = 3  # Ограничение на количество ожидающих вопросов от пользователя

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
question_hashes = {}  # Для хранения хэшей вопросов

def get_question_hash(question: str) -> str:
    """Генерирует MD5 хэш вопроса для проверки дубликатов."""
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
    """Проверяет, имеет ли вопрос смысл."""
    question_lower = question.lower().strip()
    
    # Проверка минимальной длины
    if len(question_lower) < 10:
        logger.info(f"Вопрос отклонён как бессмысленный: слишком короткий ({len(question_lower)} символов)")
        return False
    
    # Проверка на повторяющиеся символы (например, "ааааа" или "!!!!")
    if re.match(r'^(.)\1{4,}$', question_lower.replace(' ', '')) or re.match(r'^(\W)\1{4,}$', question_lower):
        logger.info(f"Вопрос отклонён как бессмысленный: повторяющиеся символы ({question})")
        return False
    
    # Проверка на повторяющиеся слова (например, "лол лол лол")
    words = question_lower.split()
    if len(words) > 1 and len(set(words)) == 1:
        logger.info(f"Вопрос отклонён как бессмысленный: повторяющиеся слова ({question})")
        return False
    
    # Проверка на наличие вопросительных слов
    question_words = ["что", "как", "почему", "где", "когда", "какой", "какая", "какое", "кто", "зачем", "сколько"]
    has_question_word = any(word in question_lower for word in question_words) or "?" in question_lower
    has_multiple_words = len(words) >= 3  # Требуем минимум 3 слова для осмысленности
    
    if not (has_question_word or has_multiple_words):
        logger.info(f"Вопрос отклонён как бессмысленный: нет вопросительных слов или слишком прост ({question})")
        return False
    
    return True

def custom_escape_markdown(text: str) -> str:
    """Экранирование специальных символов для MarkdownV2, включая круглые скобки."""
    text = escape_markdown(text, version=2)
    # Дополнительно экранируем круглые скобки
    text = text.replace('(', r'\(').replace(')', r'\)')
    return text

def get_remaining_attempts(user_id: int, data: dict) -> int:
    """Возвращает количество оставшихся попыток для пользователя."""
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
        await update.message.reply_text("Ошибка чтения данных! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
        return

    remaining_attempts = get_remaining_attempts(user_id, data)
    keyboard = [
        [InlineKeyboardButton("Задать вопрос 🔥", callback_data="ask")],
        [InlineKeyboardButton("Мои вопросы 😸", callback_data="myquestions")],
        [InlineKeyboardButton("Гайд для новичков 📖", callback_data="guide")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"Привет! 😎 Это *Q&A-бот Mortis Play*! Задавай вопросы для стримов и сайта! 🔥\n"
        f"У тебя осталось *{remaining_attempts} попыток* задать вопрос.\n"
        f"Новичок? Пиши `/guide` или жми *Гайд для новичков* ниже! 📖\n"
        f"Или используй `/ask` для вопроса!",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def guide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /guide от user_id {update.effective_user.id}")
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
        await update.message.reply_text("Ошибка чтения данных! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
        return

    remaining_attempts = get_remaining_attempts(user_id, data)
    keyboard = [
        [InlineKeyboardButton("Задать вопрос 🔥", callback_data="ask")],
        [InlineKeyboardButton("Мои вопросы 😸", callback_data="myquestions")],
        [InlineKeyboardButton("Посмотреть сайт 🌐", url=QA_WEBSITE)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"*Гайд для новичков* 📖\n\n"
        f"Добро пожаловать в *Q&A-бот Mortis Play*! 😎 Вот как начать:\n\n"
        f"1. **Задай вопрос**:\n"
        f"   Пиши `/ask <твой вопрос>`, например: `/ask Какая твоя любимая игра?`\n"
        f"   Вопрос должен быть осмысленным и от 5 до 500 символов. У тебя *{remaining_attempts} попыток* задать вопрос (до 3 ожидающих одновременно).\n\n"
        f"2. **Включи уведомления**:\n"
        f"   После отправки вопроса нажми *Уведомить о результате 🔔*, чтобы узнать, принят он или отклонён.\n\n"
        f"3. **Проверь свои вопросы**:\n"
        f"   Пиши `/myquestions` или жми *Мои вопросы 😸*, чтобы увидеть статус твоих вопросов.\n\n"
        f"4. **Смотри ответы на сайте**:\n"
        f"   Принятые вопросы с ответами публикуются на [сайте Q&A]({QA_WEBSITE}) в течение 1-48 часов.\n"
        f"   Жми *Посмотреть сайт 🌐* или переходи по ссылке: {QA_WEBSITE}\n\n"
        f"5. **Что, если вопрос не приняли?**\n"
        f"   Если вопрос отклонён или аннулирован, ты получишь уведомление (если включил 🔔).\n"
        f"   Пиши админу *@dimap7221* для уточнений, если вопрос не появился на сайте.\n\n"
        f"6. **Лимит вопросов**:\n"
        f"   Пока у тебя 3 вопроса на рассмотрении, новые не добавишь. Лимит обновляется, когда вопрос одобряют, отклоняют или аннулируют.\n\n"
        f"*Готов?* Жми кнопки ниже или пиши `/ask`! 🚀",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    logger.info(f"Гайд отправлен пользователю user_id {user_id}")

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
        await update.message.reply_text("Ошибка чтения данных! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
        return

    remaining_attempts = get_remaining_attempts(user_id, data)
    keyboard = [
        [InlineKeyboardButton("Задать вопрос 🔥", callback_data="ask")],
        [InlineKeyboardButton("Мои вопросы 😸", callback_data="myquestions")],
        [InlineKeyboardButton("Гайд для новичков 📖", callback_data="guide")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"Привет! 😎 Я бот Mortis Play, чтобы ты мог задавать вопросы для сайта!\n\n"
        f"*У тебя осталось {remaining_attempts} попыток задать вопрос.*\n\n"
        f"*Что я умею:*\n"
        f"- `/start` — Начни общение со мной! 😸\n"
        f"- `/guide` — Гайд для новичков 📖\n"
        f"- `/ask <вопрос>` — Задай вопрос, например: `/ask Какая твоя любимая игра?`\n"
        f"- `/myquestions` — Посмотри свои вопросы и их статус 😺\n"
        f"- `/help` — Покажу это сообщение с подсказками 🕶\n"
        f"- `/list` — *Только для админа*, показывает все вопросы\n"
        f"- `/clear` — *Только для админа*, очищает все вопросы\n"
        f"- `/delete <id>` — *Только для админа*, удаляет вопрос по ID\n"
        f"- `/edit <id> <новый_вопрос>` — *Только для админа*, редактирует вопрос по ID\n"
        f"- `/cancel <id> <причина>` — *Только для админа*, аннулирует вопрос с указанием причины\n\n"
        f"*Важно*: Вопрос должен быть осмысленным (например, содержать вопросительное слово или быть достаточно подробным). "
        f"Бессмысленные вопросы отклоняются без траты попыток! 🚀\n"
        f"Новичок? Пиши `/guide` или жми *Гайд для новичков* ниже! 📖",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def list_questions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /list от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("Йоу, *только админ* может это делать! 😎", parse_mode="Markdown")
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
        await update.message.reply_text("Ошибка чтения вопросов! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
        return

    active_questions = [q for q in data["questions"] if q.get("cancelled", False) == False]
    if not active_questions:
        await update.message.reply_text("Пока *нет активных вопросов*! 😿", parse_mode="Markdown")
        logger.info("Список активных вопросов пуст")
        return
    response = "*Список активных вопросов*:\n"
    for q in active_questions:
        status = STATUS_TRANSLATIONS.get(q["status"], q["status"])
        escaped_question = custom_escape_markdown(q["question"])
        escaped_username = custom_escape_markdown(q["username"])
        cancel_reason = f", Причина: *{custom_escape_markdown(q['cancel_reason'])}*" if q.get("cancel_reason") and q["status"] == "cancelled" else ""
        response += f"ID: `{q['id']}`, От: @{escaped_username}, Вопрос: *{escaped_question}*, Статус: `{status}`{cancel_reason}\n"
    
    logger.info(f"Формируем список вопросов для отправки: {response}")
    try:
        await update.message.reply_text(response, parse_mode="MarkdownV2")
        logger.info(f"Админ запросил список вопросов: {len(active_questions)} активных вопросов")
    except Exception as e:
        logger.error(f"Ошибка отправки списка вопросов: {e}")
        await update.message.reply_text("Ошибка отправки списка вопросов! 😿 Попробуй ещё раз или проверь логи.", parse_mode="Markdown")

async def my_questions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /myquestions от user_id {update.effective_user.id}")
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
        await update.message.reply_text("Ошибка чтения вопросов! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
        return

    user_questions = [q for q in data["questions"] if q["user_id"] == user_id and q.get("cancelled", False) == False]
    remaining_attempts = get_remaining_attempts(user_id, data)
    if not user_questions:
        await update.message.reply_text(
            f"Ты ещё *не задал активных вопросов*! 😿 У тебя осталось *{remaining_attempts} попыток*.\nПиши `/ask <вопрос>` или посмотри `/guide`!",
            parse_mode="Markdown"
        )
        logger.info(f"Пользователь user_id {user_id} запросил свои вопросы: список активных вопросов пуст")
        return
    response = f"*Твои активные вопросы* (осталось попыток: *{remaining_attempts}*):\n"
    for q in user_questions:
        status = STATUS_TRANSLATIONS.get(q["status"], q["status"])
        escaped_question = custom_escape_markdown(q["question"])
        escaped_answer = custom_escape_markdown(q["answer"]) if q["status"] == "approved" and "answer" in q else ""
        answer = f", Ответ: *{escaped_answer}*" if q["status"] == "approved" and "answer" in q else ""
        cancel_reason = f", Причина: *{custom_escape_markdown(q['cancel_reason'])}*" if q.get("cancel_reason") and q["status"] == "cancelled" else ""
        response += f"ID: `{q['id']}`, Вопрос: *{escaped_question}*, Статус: `{status}`{answer}{cancel_reason}\n"
    
    logger.info(f"Формируем список вопросов пользователя user_id {user_id}: {response}")
    try:
        await update.message.reply_text(response, parse_mode="MarkdownV2")
        logger.info(f"Пользователь user_id {user_id} запросил свои вопросы: {len(user_questions)} активных вопросов")
    except Exception as e:
        logger.error(f"Ошибка отправки списка вопросов: {e}")
        await update.message.reply_text("Ошибка отправки списка вопросов! 😿 Попробуй ещё раз или проверь логи.", parse_mode="Markdown")

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
    question = " ".join(context.args) if context.args else update.message.text
    question_hash = get_question_hash(question)

    # Проверка на осмысленность вопроса
    if not check_question_meaning(question):
        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("Ошибка записи вопроса! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
            return
        remaining_attempts = get_remaining_attempts(user_id, data)
        await update.message.reply_text(
            f"Йоу, твой вопрос *кажется бессмысленным*! 😿 Попробуй задать что-то вроде: `Какую игру ты стримишь чаще всего?`\nОсталось попыток: *{remaining_attempts}*.\nСмотри `/guide` для подсказок!",
            parse_mode="Markdown"
        )
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
                await update.message.reply_text("Ошибка записи вопроса! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
                return
            remaining_attempts = get_remaining_attempts(user_id, data)
            await update.message.reply_text(
                f"Йоу, *не так быстро*! 😎 Один вопрос в минуту! Осталось попыток: *{remaining_attempts}*.\nСмотри `/guide` для подсказок!",
                parse_mode="Markdown"
            )
            logger.info(f"Спам-атака от user_id {user_id}: слишком частые вопросы")
            return

    try:
        with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
        await update.message.reply_text("Ошибка записи вопроса! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
        return

    # Проверка на дубликат по хэшу
    if question_hash in question_hashes.get(user_id, []):
        remaining_attempts = get_remaining_attempts(user_id, data)
        await update.message.reply_text(
            f"Эй, ты *уже спрашивал* это или очень похожее! 😕 Попробуй другой вопрос. Осталось попыток: *{remaining_attempts}*.\nСмотри `/guide` для подсказок!",
            parse_mode="Markdown"
        )
        logger.info(f"Дубликат вопроса от user_id {user_id}: {question}")
        return

    # Проверка на максимальное количество ожидающих вопросов
    pending_questions = [q for q in data["questions"] if q["user_id"] == user_id and q["status"] == "pending" and q.get("cancelled", False) == False]
    if len(pending_questions) >= MAX_PENDING_QUESTIONS:
        await update.message.reply_text(
            f"Йоу, у тебя уже *{MAX_PENDING_QUESTIONS} вопроса* на рассмотрении! 😎 Дождись ответа или попробуй позже.\nСмотри `/guide` для подсказок!",
            parse_mode="Markdown"
        )
        logger.info(f"Превышен лимит ожидающих вопросов для user_id {user_id}: {len(pending_questions)}")
        return  # Прерываем выполнение, чтобы не добавить вопрос сверх лимита

    if not context.args and update.message.text.startswith("/ask"):
        remaining_attempts = get_remaining_attempts(user_id, data)
        await update.message.reply_text(
            f"Йоу, напиши *вопрос* после `/ask`, например: `/ask Какая твоя любимая игра?`\nОсталось попыток: *{remaining_attempts}*.\nСмотри `/guide` для подсказок!",
            parse_mode="Markdown"
        )
        return

    if len(question) < 5 or len(question) > 500:
        remaining_attempts = get_remaining_attempts(user_id, data)
        await update.message.reply_text(
            f"Вопрос должен быть от *5 до 500 символов*! 😎 Осталось попыток: *{remaining_attempts}*.\nСмотри `/guide` для подсказок!",
            parse_mode="Markdown"
        )
        logger.info(f"Недопустимая длина вопроса от user_id {user_id}: {len(question)} символов")
        return

    if check_blacklist(question):
        remaining_attempts = get_remaining_attempts(user_id, data)
        await update.message.reply_text(
            f"Йоу, твой вопрос содержит *запрещённые слова*! 😿 Попробуй другой. Осталось попыток: *{remaining_attempts}*.\nСмотри `/guide` для подсказок!",
            parse_mode="Markdown"
        )
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
    except IOError as e:
        logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
        await update.message.reply_text("Ошибка записи вопроса! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
        return

    # Обновление хэшей вопросов
    if user_id not in question_hashes:
        question_hashes[user_id] = []
    question_hashes[user_id].append(question_hash)
    spam_protection[user_id] = {"last_ask_time": current_time, "last_question": question}

    # Перечитываем файл после записи, чтобы убедиться в актуальности данных
    try:
        with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
            updated_data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Ошибка перечтения {QUESTIONS_FILE} после записи: {e}")
        await update.message.reply_text("Ошибка записи вопроса! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
        return

    remaining_attempts = get_remaining_attempts(user_id, updated_data)
    keyboard = [[InlineKeyboardButton("Уведомить о результате 🔔", callback_data=f"notify_{question_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"**Вопрос принят!** Жди ответа на *сайте*! 😸 *Доге одобряет* 🐶\n\n"
        f"*Добавление вопроса на сайт может занять от 1 до 48 часов.* Если вопрос не появился, пиши в личку *@dimap7221*! 😎\n"
        f"Осталось попыток: *{remaining_attempts}*.\nСмотри `/guide` для подробностей!",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

    escaped_question = custom_escape_markdown(question)
    escaped_username = custom_escape_markdown(user.username or "Аноним")
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"*Новый вопрос* \\(ID: `{question_id}`\\)\nОт: @{escaped_username}\nВопрос: *{escaped_question}*\n`/approve {question_id} <ответ>` — принять\n`/reject {question_id}` — отклонить\n`/cancel {question_id} <причина>` — аннулировать",
            parse_mode="MarkdownV2"
        )
        logger.info(f"Уведомление админу отправлено: вопрос ID {question_id} от @{user.username or 'Аноним'}")
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления админу: {e}")
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"*Новый вопрос* (ID: {question_id})\nОт: @{user.username or 'Аноним'}\nВопрос: {question}\n`/approve {question_id} <ответ>` — принять\n`/reject {question_id}` — отклонить\n`/cancel {question_id} <причина>` — аннулировать",
            parse_mode=None
        )

async def notify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Callback notify от user_id {update.effective_user.id}")
    query = update.callback_query
    await query.answer()
    question_id = int(query.data.split("_")[1])
    user_id = query.from_user.id

    try:
        with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
        await query.message.reply_text("Ошибка обработки уведомления! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
        return

    for q in data["questions"]:
        if q["id"] == question_id and q["user_id"] == user_id and not q.get("cancelled", False):
            q["notify"] = True
            break
    else:
        await query.message.reply_text("Вопрос не найден или аннулирован! 😿", parse_mode="Markdown")
        logger.warning(f"Вопрос ID {question_id} не найден или аннулирован для уведомления user_id {user_id}")
        return

    try:
        with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except IOError as e:
        logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
        await query.message.reply_text("Ошибка обработки уведомления! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
        return

    await query.message.edit_text(
        "**Вопрос принят!** Ты будешь *уведомлён* о результате! 😎\nСмотри `/guide` для подробностей!",
        parse_mode="Markdown"
    )
    logger.info(f"Пользователь user_id {user_id} включил уведомления для вопроса ID {question_id}")

async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /approve от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("Йоу, *только админ* может это делать! 😎", parse_mode="Markdown")
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
            "Укажи *ID вопроса* и *ответ*: `/approve <id> <ответ>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /approve: отсутствуют аргументы, команда: {update.message.text}")
        return

    try:
        question_id = int(args[0])
        answer = " ".join(args[1:]) if len(args) > 1 else None
        if not answer:
            await update.message.reply_text(
                "Укажи *ответ* для вопроса: `/approve <id> <ответ>`",
                parse_mode="Markdown"
            )
            logger.error(f"Ошибка в /approve: отсутствует ответ, команда: {update.message.text}")
            return

        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("Ошибка чтения вопросов! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
            return

        for q in data["questions"]:
            if q["id"] == question_id and q["status"] == "pending" and not q.get("cancelled", False):
                q["status"] = "approved"
                q["answer"] = answer
                website_button = [[InlineKeyboardButton("Посмотреть на сайте 🌐", url=QA_WEBSITE)]]
                reply_markup = InlineKeyboardMarkup(website_button)
                if q["notify"]:
                    try:
                        escaped_answer = custom_escape_markdown(answer)
                        await context.bot.send_message(
                            chat_id=q["user_id"],
                            text=f"*Твой вопрос принят!* 😎 Ответ: *{escaped_answer}*\nСмотри на сайте!\nПодробности в `/guide`",
                            reply_markup=reply_markup,
                            parse_mode="MarkdownV2"
                        )
                        logger.info(f"Уведомление о принятии отправлено user_id {q['user_id']} для вопроса ID {question_id}")
                    except Exception as e:
                        logger.error(f"Ошибка уведомления пользователя {q['user_id']}: {e}")
                        await context.bot.send_message(
                            chat_id=q["user_id"],
                            text=f"**Твой вопрос принят!** 😎 Ответ: {answer}\nСмотри на сайте!\nПодробности в `/guide`",
                            reply_markup=reply_markup,
                            parse_mode=None
                        )
                break
        else:
            await update.message.reply_text(
                f"Вопрос с ID `{question_id}` *не найден*, уже обработан или аннулирован!",
                parse_mode="Markdown"
            )
            logger.warning(f"Вопрос ID {question_id} не найден, уже обработан или аннулирован")
            return

        try:
            with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("Ошибка записи ответа! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
            return

        website_button = [[InlineKeyboardButton("Посмотреть на сайте 🌐", url=QA_WEBSITE)]]
        reply_markup = InlineKeyboardMarkup(website_button)
        await update.message.reply_text(
            f"Вопрос `{question_id}` *принят* с ответом: *{answer}* 🔥",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        logger.info(f"Вопрос ID {question_id} принят с ответом: {answer}")
    except ValueError:
        await update.message.reply_text(
            "ID вопроса должен быть *числом*: `/approve <id> <ответ>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /approve: неверный формат ID, команда: {update.message.text}")

async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /reject от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("Йоу, *только админ* может это делать! 😎", parse_mode="Markdown")
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
            "Укажи *ID вопроса*: `/reject <id>`",
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
            await update.message.reply_text("Ошибка чтения вопросов! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
            return

        for q in data["questions"]:
            if q["id"] == question_id and q["status"] == "pending" and not q.get("cancelled", False):
                q["status"] = "rejected"
                if q["notify"]:
                    try:
                        await context.bot.send_message(
                            chat_id=q["user_id"],
                            text="Твой вопрос *отклонён* 😕 Попробуй задать другой!\nСмотри `/guide` для подсказок!",
                            parse_mode="Markdown"
                        )
                        logger.info(f"Уведомление об отклонении отправлено user_id {q['user_id']} для вопроса ID {question_id}")
                    except Exception as e:
                        logger.error(f"Ошибка уведомления пользователя {q['user_id']}: {e}")
                break
        else:
            await update.message.reply_text(
                f"Вопрос с ID `{question_id}` *не найден*, уже обработан или аннулирован!",
                parse_mode="Markdown"
            )
            logger.warning(f"Вопрос ID {question_id} не найден, уже обработан или аннулирован")
            return

        try:
            with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("Ошибка записи статуса! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
            return

        await update.message.reply_text(
            f"Вопрос `{question_id}` *отклонён* 😿",
            parse_mode="Markdown"
        )
        logger.info(f"Вопрос ID {question_id} отклонён")
    except ValueError:
        await update.message.reply_text(
            "ID вопроса должен быть *числом*: `/reject <id>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /reject: неверный формат ID, команда: {update.message.text}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /cancel от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("Йоу, *только админ* может это делать! 😎", parse_mode="Markdown")
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
            "Укажи *ID вопроса* и *причину аннулирования*: `/cancel <id> <причина>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /cancel: отсутствует ID или причина, команда: {update.message.text}")
        return

    try:
        question_id = int(args[0])
        cancel_reason = " ".join(args[1:])
        if len(cancel_reason) > 500:
            await update.message.reply_text(
                "Причина аннулирования не должна превышать *500 символов*! 😎",
                parse_mode="Markdown"
            )
            logger.error(f"Ошибка в /cancel: слишком длинная причина, команда: {update.message.text}")
            return

        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("Ошибка чтения вопросов! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
            return

        for q in data["questions"]:
            if q["id"] == question_id and not q.get("cancelled", False):
                q["cancelled"] = True
                q["status"] = "cancelled"
                q["cancel_reason"] = cancel_reason
                if q["notify"]:
                    try:
                        escaped_reason = custom_escape_markdown(cancel_reason)
                        await context.bot.send_message(
                            chat_id=q["user_id"],
                            text=f"Твой вопрос *аннулирован* 😕 Причина: *{escaped_reason}*\nСвяжитесь с админом (@dimap7221) для уточнений!\nСмотри `/guide` для подсказок!",
                            parse_mode="MarkdownV2"
                        )
                        logger.info(f"Уведомление об аннулировании отправлено user_id {q['user_id']} для вопроса ID {question_id}")
                    except Exception as e:
                        logger.error(f"Ошибка уведомления пользователя {q['user_id']}: {e}")
                        await context.bot.send_message(
                            chat_id=q["user_id"],
                            text=f"Твой вопрос *аннулирован* 😕 Причина: {cancel_reason}\nСвяжитесь с админом (@dimap7221) для уточнений!\nСмотри `/guide` для подсказок!",
                            parse_mode=None
                        )
                break
        else:
            await update.message.reply_text(
                f"Вопрос с ID `{question_id}` *не найден* или уже аннулирован!",
                parse_mode="Markdown"
            )
            logger.warning(f"Вопрос ID {question_id} не найден или уже аннулирован")
            return

        try:
            with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("Ошибка аннулирования вопроса! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
            return

        escaped_reason = custom_escape_markdown(cancel_reason)
        await update.message.reply_text(
            f"Вопрос `{question_id}` *аннулирован* 😿 Причина: *{escaped_reason}*",
            parse_mode="MarkdownV2"
        )
        logger.info(f"Вопрос ID {question_id} аннулирован с причиной: {cancel_reason}")
    except ValueError:
        await update.message.reply_text(
            "ID вопроса должен быть *числом*: `/cancel <id> <причина>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /cancel: неверный формат ID, команда: {update.message.text}")

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /clear от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("Йоу, *только админ* может это делать! 😎", parse_mode="Markdown")
        logger.warning(f"Неавторизованная попытка /clear от user_id {update.message.from_user.id}")
        return

    update_id = update.update_id
    if update_id in processed_updates:
        logger.info(f"Дубликат update_id {update_id}, пропускаем")
        return
    processed_updates.add(update_id)

    logger.info("Команда /clear от админа: очистка всех вопросов")
    try:
        with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump({"questions": []}, f, ensure_ascii=False, indent=2)
        question_hashes.clear()  # Очистка хэшей вопросов
        await update.message.reply_text(
            "Все вопросы *очищены*! 😺 ID начнётся с 1.",
            parse_mode="Markdown"
        )
        logger.info("Все вопросы и хэши успешно очищены")
    except IOError as e:
        logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
        await update.message.reply_text("Ошибка очистки вопросов! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")

async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /delete от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("Йоу, *только админ* может это делать! 😎", parse_mode="Markdown")
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
            "Укажи *ID вопроса*: `/delete <id>`",
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
            await update.message.reply_text("Ошибка чтения вопросов! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
            return

        initial_length = len(data["questions"])
        data["questions"] = [q for q in data["questions"] if q["id"] != question_id]
        if len(data["questions"]) == initial_length:
            await update.message.reply_text(
                f"Вопрос с ID `{question_id}` *не найден*! 😿",
                parse_mode="Markdown"
            )
            logger.warning(f"Вопрос ID {question_id} не найден для удаления")
            return

        try:
            with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("Ошибка удаления вопроса! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
            return

        # Удаление хэша вопроса
        for user_id, hashes in question_hashes.items():
            for q in data["questions"]:
                if q["id"] == question_id:
                    question_hash = get_question_hash(q["question"])
                    if question_hash in hashes:
                        hashes.remove(question_hash)
                        logger.info(f"Хэш вопроса ID {question_id} удалён для user_id {user_id}")
                    break

        await update.message.reply_text(
            f"Вопрос `{question_id}` *удалён*!",
            parse_mode="Markdown"
        )
        logger.info(f"Вопрос ID {question_id} успешно удалён")
    except ValueError:
        await update.message.reply_text(
            "ID вопроса должен быть *числом*: `/delete <id>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /delete: неверный формат ID, команда: {update.message.text}")

async def edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Команда /edit от user_id {update.effective_user.id}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    if update.message.from_user.id != ADMIN_ID:
        await update.message.reply_text("Йоу, *только админ* может это делать! 😎", parse_mode="Markdown")
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
            "Укажи *ID вопроса* и *новый вопрос*: `/edit <id> <новый_вопрос>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /edit: отсутствуют аргументы, команда: {update.message.text}")
        return

    try:
        question_id = int(args[0])
        new_question = " ".join(args[1:])
        new_question_hash = get_question_hash(new_question)
        if len(new_question) < 5 or len(new_question) > 500:
            await update.message.reply_text(
                "Новый вопрос должен быть от *5 до 500 символов*! 😎",
                parse_mode="Markdown"
            )
            logger.error(f"Ошибка в /edit: недопустимая длина вопроса, команда: {update.message.text}")
            return

        if not check_question_meaning(new_question):
            await update.message.reply_text(
                "Йоу, новый вопрос *кажется бессмысленным*! 😿 Попробуй что-то вроде: `Какую игру ты стримишь чаще всего?`\nСмотри `/guide` для подсказок!",
                parse_mode="Markdown"
            )
            logger.info(f"Новый вопрос отклонён как бессмысленный: {new_question}")
            return

        if check_blacklist(new_question):
            await update.message.reply_text(
                "Йоу, новый вопрос содержит *запрещённые слова*! 😿 Попробуй другой.\nСмотри `/guide` для подсказок!",
                parse_mode="Markdown"
            )
            logger.info(f"Новый вопрос отклонён из-за чёрного списка: {new_question}")
            return

        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("Ошибка чтения вопросов! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
            return

        for q in data["questions"]:
            if q["id"] == question_id and not q.get("cancelled", False):
                old_question = q["question"]
                old_question_hash = get_question_hash(old_question)
                q["question"] = new_question
                try:
                    with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except IOError as e:
                    logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
                    await update.message.reply_text("Ошибка редактирования вопроса! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
                # Обновление хэша вопроса
                for user_id, hashes in question_hashes.items():
                    if old_question_hash in hashes:
                        hashes.remove(old_question_hash)
                        hashes.append(new_question_hash)
                        logger.info(f"Хэш вопроса ID {question_id} обновлён для user_id {user_id}")
                await update.message.reply_text(
                    f"Вопрос `{question_id}` *отредактирован*! 😺\nСтарый: *{old_question}*\nНовый: *{new_question}*",
                    parse_mode="Markdown"
                )
                logger.info(f"Вопрос ID {question_id} отредактирован: старый: {old_question}, новый: {new_question}")
                return

        await update.message.reply_text(
            f"Вопрос с ID `{question_id}` *не найден* или аннулирован! 😿",
            parse_mode="Markdown"
        )
        logger.warning(f"Вопрос ID {question_id} не найден или аннулирован для редактирования")
    except ValueError:
        await update.message.reply_text(
            "ID вопроса должен быть *числом*: `/edit <id> <новый_вопрос>`",
            parse_mode="Markdown"
        )
        logger.error(f"Ошибка в /edit: неверный формат ID, команда: {update.message.text}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Сообщение от user_id {update.effective_user.id}: {update.message.text}")
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное, удалённое или пустое сообщение")
        return
    update_id = update.update_id
    if update_id in processed_updates:
        logger.info(f"Дубликат update_id {update_id}, пропускаем")
        return
    processed_updates.add(update_id)

    if update.message.from_user.id == ADMIN_ID:
        await update.message.reply_text(
            "Йоу, *админ*! Используй `/approve`, `/reject`, `/cancel`, `/list`, `/clear`, `/delete`, `/edit` или `/ask` для теста вопроса 😎",
            parse_mode="Markdown"
        )
    else:
        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
            await update.message.reply_text("Ошибка чтения данных! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
            return
        remaining_attempts = get_remaining_attempts(update.message.from_user.id, data)
        await update.message.reply_text(
            f"Пиши `/ask <вопрос>`, чтобы задать *эпичный* вопрос! 😎 Осталось попыток: *{remaining_attempts}*.\nНовичок? Смотри `/guide`! 📖",
            parse_mode="Markdown"
        )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Callback button от user_id {update.effective_user.id}: {update.callback_query.data}")
    query = update.callback_query
    await query.answer()
    if query.data == "ask":
        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
            await query.message.reply_text("Ошибка чтения данных! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
            return
        remaining_attempts = get_remaining_attempts(query.from_user.id, data)
        await query.message.reply_text(
            f"Йоу, напиши `/ask <твой вопрос>`, например: `/ask Какая твоя любимая игра?`\nОсталось попыток: *{remaining_attempts}*.\nСмотри `/guide` для подсказок!",
            parse_mode="Markdown"
        )
    elif query.data == "myquestions":
        user_id = query.from_user.id
        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
            await query.message.reply_text("Ошибка чтения вопросов! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
            return

        user_questions = [q for q in data["questions"] if q["user_id"] == user_id and q.get("cancelled", False) == False]
        remaining_attempts = get_remaining_attempts(user_id, data)
        if not user_questions:
            await query.message.reply_text(
                f"Ты ещё *не задал активных вопросов*! 😿 У тебя осталось *{remaining_attempts} попыток*.\nПиши `/ask <вопрос>` или посмотри `/guide`!",
                parse_mode="Markdown"
            )
            return
        response = f"*Твои активные вопросы* (осталось попыток: *{remaining_attempts}*):\n"
        for q in user_questions:
            status = STATUS_TRANSLATIONS.get(q["status"], q["status"])
            escaped_question = custom_escape_markdown(q["question"])
            escaped_answer = custom_escape_markdown(q["answer"]) if q["status"] == "approved" and "answer" in q else ""
            answer = f", Ответ: *{escaped_answer}*" if q["status"] == "approved" and "answer" in q else ""
            cancel_reason = f", Причина: *{custom_escape_markdown(q['cancel_reason'])}*" if q.get("cancel_reason") and q["status"] == "cancelled" else ""
            response += f"ID: `{q['id']}`, Вопрос: *{escaped_question}*, Статус: `{status}`{answer}{cancel_reason}\n"
        
        logger.info(f"Формируем список вопросов для callback user_id {user_id}: {response}")
        try:
            await query.message.reply_text(response, parse_mode="MarkdownV2")
        except Exception as e:
            logger.error(f"Ошибка отправки списка вопросов: {e}")
            await query.message.reply_text("Ошибка отправки списка вопросов! 😿 Попробуй ещё раз или проверь логи.", parse_mode="Markdown")
    elif query.data == "guide":
        user_id = query.from_user.id
        try:
            with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
            await query.message.reply_text("Ошибка чтения данных! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
            return

        remaining_attempts = get_remaining_attempts(user_id, data)
        keyboard = [
            [InlineKeyboardButton("Задать вопрос 🔥", callback_data="ask")],
            [InlineKeyboardButton("Мои вопросы 😸", callback_data="myquestions")],
            [InlineKeyboardButton("Посмотреть сайт 🌐", url=QA_WEBSITE)]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text(
            f"*Гайд для новичков* 📖\n\n"
            f"Добро пожаловать в *Q&A-бот Mortis Play*! 😎 Вот как начать:\n\n"
            f"1. **Задай вопрос**:\n"
            f"   Пиши `/ask <твой вопрос>`, например: `/ask Какая твоя любимая игра?`\n"
            f"   Вопрос должен быть осмысленным и от 5 до 500 символов. У тебя *{remaining_attempts} попыток* задать вопрос (до 3 ожидающих одновременно).\n\n"
            f"2. **Включи уведомления**:\n"
            f"   После отправки вопроса нажми *Уведомить о результате 🔔*, чтобы узнать, принят он или отклонён.\n\n"
            f"3. **Проверь свои вопросы**:\n"
            f"   Пиши `/myquestions` или жми *Мои вопросы 😸*, чтобы увидеть статус твоих вопросов.\n\n"
            f"4. **Смотри ответы на сайте**:\n"
            f"   Принятые вопросы с ответами публикуются на [сайте Q&A]({QA_WEBSITE}) в течение 1-48 часов.\n"
            f"   Жми *Посмотреть сайт 🌐* или переходи по ссылке: {QA_WEBSITE}\n\n"
            f"5. **Что, если вопрос не приняли?**\n"
            f"   Если вопрос отклонён или аннулирован, ты получишь уведомление (если включил 🔔).\n"
            f"   Пиши админу *@dimap7221* для уточнений, если вопрос не появился на сайте.\n\n"
            f"6. **Лимит вопросов**:\n"
            f"   Пока у тебя 3 вопроса на рассмотрении, новые не добавишь. Лимит обновляется, когда вопрос одобряют, отклоняют или аннулируют.\n\n"
            f"*Готов?* Жми кнопки ниже или пиши `/ask`! 🚀",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        logger.info(f"Гайд отправлен через callback пользователю user_id {user_id}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Ошибка: {context.error}")
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"*Ошибка в боте*! 😿: {context.error}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить админа об ошибке: {e}")

async def notify_admin_on_start(app: Application):
    try:
        await app.bot.send_message(
            chat_id=ADMIN_ID,
            text="**Бот запустился на Railway!** 😎 *Кот одобряет* 🐾",
            parse_mode="Markdown"
        )
        logger.info("Уведомление админу о старте отправлено")
    except Exception as e:
        logger.error(f"Ошибка уведомления админа при старте: {e}")

async def main_async():
    logger.info(f"Бот стартовал с Python {os.sys.version}")
    logger.info(f"Используемый токен: {TOKEN[:10]}...{TOKEN[-10:]}")
    try:
        app = Application.builder().token(TOKEN).build()
        await app.initialize()
        logger.info("Application успешно инициализирован")
    except Exception as e:
        logger.error(f"Ошибка инициализации бота: {e}")
        if "InvalidToken" in str(e) or "401" in str(e):
            logger.error("Токен недействителен! Проверь TELEGRAM_TOKEN в .env или @BotFather.")
        raise
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("guide", guide))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("list", list_questions))
    app.add_handler(CommandHandler("myquestions", my_questions))
    app.add_handler(CommandHandler("ask", ask))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("delete", delete))
    app.add_handler(CommandHandler("edit", edit))
    app.add_handler(CallbackQueryHandler(notify_callback, pattern="^notify_"))
    app.add_handler(CallbackQueryHandler(button_callback, pattern="^(ask|myquestions|guide)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.StatusUpdate.ALL, lambda u, c: None))
    app.add_error_handler(error_handler)

    await notify_admin_on_start(app)
    await app.updater.start_polling()
    await app.start()
    logger.info("Бот запущен в режиме polling")
    try:
        await asyncio.Event().wait()  # Бесконечное ожидание
    except asyncio.CancelledError:
        logger.info("Бот остановлен")
        await app.stop()
        await app.updater.stop()

if __name__ == "__main__":
    asyncio.run(main_async())