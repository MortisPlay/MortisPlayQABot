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
SIMILARITY_THRESHOLD = 0.8

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
    return hashlib.sha256(question.lower().encode('utf-8')).hexdigest()

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
    if len(question_lower) < 10:
        return False, "Вопрос слишком короткий (менее 10 символов)."
    if re.match(r'^(.)\1{4,}$', question_lower.replace(' ', '')) or re.match(r'^(\W)\1{4,}$', question_lower):
        return False, "Вопрос содержит повторяющиеся символы."
    words = question_lower.split()
    if len(words) > 1 and len(set(words)) == 1:
        return False, "Вопрос состоит из повторяющихся слов."
    question_words = ["что", "как", "почему", "где", "когда", "какой", "какая", "какое", "кто", "зачем", "сколько"]
    has_question_word = any(word in question_lower for word in question_words) or "?" in question_lower
    has_multiple_words = len(words) >= 3
    if not (has_question_word and has_multiple_words):
        return False, "Вопрос не содержит вопросительных слов или слишком прост."
    context_keywords = ["игра", "стрим", "видео", "mortis", "mortisplay", "канал", "youtube", "twitch"]
    has_context = any(keyword in question_lower for keyword in context_keywords) or len(words) >= 5
    if not has_context:
        return False, "Вопрос не содержит контекста (например, про игры, стримы или Mortis Play)."
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

async def check_update(update: Update, context: ContextTypes.DEFAULT_TYPE, callback):
    update_id = update.update_id
    if update_id in processed_updates:
        logger.info(f"Дубликат update_id {update_id}, пропускаем")
        return
    processed_updates.add(update_id)
    if not update.message or not update.message.text:
        logger.info("Пропущено невалидное или удалённое сообщение")
        return
    await callback(update, context)

class Database:
    def __init__(self, questions_file: str, blacklist_file: str):
        self.questions_file = questions_file
        self.blacklist_file = blacklist_file
        self.lock = asyncio.Lock()

    async def read_questions(self) -> dict:
        async with self.lock:
            try:
                with open(self.questions_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Ошибка чтения {self.questions_file}: {e}")
                return {"questions": []}

    async def write_questions(self, data: dict):
        async with self.lock:
            try:
                if os.path.exists(self.questions_file):
                    backup_file = self.questions_file + ".bak"
                    os.rename(self.questions_file, backup_file)
                with open(self.questions_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except IOError as e:
                logger.error(f"Ошибка записи в {self.questions_file}: {e}")
                if os.path.exists(backup_file):
                    os.rename(backup_file, self.questions_file)
                raise

db = Database(QUESTIONS_FILE, BLACKLIST_FILE)

def get_remaining_attempts(user_id: int, data: dict) -> int:
    pending_questions = [q for q in data["questions"] if q["user_id"] == user_id and q["status"] == "pending" and not q.get("cancelled", False)]
    return max(0, MAX_PENDING_QUESTIONS - len(pending_questions))

async def guide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info(f"Команда /guide от user_id {update.effective_user.id}")
        user_id = update.effective_user.id
        reply_to = update.message or update.callback_query.message
        if not reply_to:
            logger.info("Отсутствует reply_to")
            return
        data = await db.read_questions()
        remaining_attempts = get_remaining_attempts(user_id, data)
        keyboard = [
            [InlineKeyboardButton("Задать вопрос ❓", callback_data="ask")],
            [InlineKeyboardButton("Мои вопросы 📋", callback_data="myquestions")],
            [InlineKeyboardButton("На сайт 🌐", url=QA_WEBSITE)]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = (
            f"📖 *Гайд по Q&A-боту Mortis Play*\n\n"
            f"😎 Добро пожаловать! Вот как работает бот:\n\n"
            f"1️⃣ *Задай вопрос*: Пиши `/ask <вопрос>` (5–500 символов, про игры/стримы/Mortis Play).\n"
            f"   *Попыток*: {remaining_attempts}/3. Пример: `/ask Какая твоя любимая игра?`\n\n"
            f"2️⃣ *Статусы вопроса*:\n"
            f"   • *Рассматривается*: Ждёт проверки админом.\n"
            f"   • *Принят*: Опубликован на [сайте]({QA_WEBSITE}) за 1–48ч.\n"
            f"   • *Отклонён*: Не подходит (с причиной).\n"
            f"   • *Аннулирован*: Удалён за нарушение правил.\n\n"
            f"3️⃣ *Правила вопросов*:\n"
            f"   • Вопросы должны быть связаны с Mortis Play (игры, стримы, контент).\n"
            f"   • Запрещены: спам, оскорбления, реклама, оффтоп, личная информация.\n"
            f"   • Аннулирование: за нарушение правил или неуместный контент.\n\n"
            f"4️⃣ *Уведомления*: Нажми *Уведомить 🔔* для статуса вопроса.\n\n"
            f"5️⃣ *Проверь вопросы*: Пиши `/myquestions`.\n\n"
            f"📌 Проблемы? Пиши @MortisplayQABot.\n"
            f"🚀 Готов? Жми `/ask`!"
        )
        try:
            await reply_to.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
            logger.info(f"Гайд отправлен пользователю user_id {user_id}")
        except Exception as e:
            logger.error(f"Ошибка отправки гайда: {e}")
            text_plain = text.replace("*", "").replace("[сайте](https://mortisplay.ru/qa.html)", f"сайте {QA_WEBSITE}")
            await reply_to.reply_text(text_plain, reply_markup=reply_markup, parse_mode=None)
    if update.callback_query:
        await update.callback_query.answer()
        await callback(update, context)
    else:
        await check_update(update, context, callback)

async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info(f"Команда /approve от user_id {update.effective_user.id}")
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("🚫 *Только админ* может это делать! 😎", parse_mode="Markdown")
            logger.warning(f"Неавторизованная попытка /approve от user_id {update.effective_user.id}")
            return
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
            data = await db.read_questions()
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
            await db.write_questions(data)
            logger.info(f"Вопрос ID {question_id} принят, ответ: {answer}")
        except ValueError:
            await update.message.reply_text(
                f"❌ ID должен быть числом: `/approve <id> <ответ>`",
                parse_mode="Markdown"
            )
            logger.error(f"Ошибка в /approve: неверный формат ID, команда: {update.message.text}")
    await check_update(update, context, callback)

async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info(f"Команда /reject от user_id {update.effective_user.id}")
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("🚫 *Только админ* может это делать! 😎", parse_mode="Markdown")
            logger.warning(f"Неавторизованная попытка /reject от user_id {update.effective_user.id}")
            return
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
            data = await db.read_questions()
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
            await db.write_questions(data)
            logger.info(f"Вопрос ID {question_id} отклонён, причина: {reject_reason}")
        except ValueError:
            await update.message.reply_text(
                f"❌ ID должен быть числом: `/reject <id> <причина>`",
                parse_mode="Markdown"
            )
            logger.error(f"Ошибка в /reject: неверный формат ID, команда: {update.message.text}")
    await check_update(update, context, callback)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info(f"Команда /cancel от user_id {update.effective_user.id}")
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("🚫 *Только админ* может это делать! 😎", parse_mode="Markdown")
            logger.warning(f"Неавторизованная попытка /cancel от user_id {update.effective_user.id}")
            return
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
            data = await db.read_questions()
            for q in data["questions"]:
                if q["id"] == question_id and q["status"] == "pending" and not q.get("cancelled", False):
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
                    f"❌ Вопрос ID `{question_id}` не найден, обработан или уже аннулирован!",
                    parse_mode="Markdown"
                )
                logger.warning(f"Вопрос ID {question_id} не найден, уже обработан или аннулирован")
                return
            await db.write_questions(data)
            logger.info(f"Вопрос ID {question_id} аннулирован, причина: {cancel_reason}")
        except ValueError:
            await update.message.reply_text(
                f"❌ ID должен быть числом: `/cancel <id> <причина>`",
                parse_mode="Markdown"
            )
            logger.error(f"Ошибка в /cancel: неверный формат ID, команда: {update.message.text}")
    await check_update(update, context, callback)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "ask":
        await query.message.reply_text(
            "❓ Напиши `/ask <вопрос>`, например: `/ask Какая твоя любимая игра на стримах?`",
            parse_mode="Markdown"
        )
    elif data == "myquestions":
        await my_questions(update, context)
    elif data == "guide":
        await guide(update, context)
    elif data.startswith("notify_"):
        question_id = int(data.split("_")[1])
        data = await db.read_questions()
        for q in data["questions"]:
            if q["id"] == question_id:
                q["notify"] = True
                await db.write_questions(data)
                await query.message.reply_text(
                    f"🔔 Уведомления для вопроса `{question_id}` включены!",
                    parse_mode="Markdown"
                )
                logger.info(f"Уведомления включены для вопроса ID {question_id}, user_id {q['user_id']}")
                break
    elif data.startswith("send_notify_"):
        action, question_id = data.split("_")[2], int(data.split("_")[3])
        data = await db.read_questions()
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

async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info(f"Команда /ask от user_id {update.effective_user.id}")
        user = update.message.from_user
        user_id = user.id
        current_time = time.time()
        if user_id in spam_protection and current_time - spam_protection[user_id]["last_ask_time"] < 60:
            data = await db.read_questions()
            remaining_attempts = get_remaining_attempts(user_id, data)
            await update.message.reply_text(
                f"⏳ *Не так быстро!* Один вопрос в минуту.\n"
                f"📌 *Попыток*: {remaining_attempts}/3",
                parse_mode="Markdown"
            )
            logger.info(f"Спам-атака от user_id {user_id}: слишком частые вопросы")
            return
        question = " ".join(context.args) if context.args else update.message.text.split("/ask", 1)[-1].strip()
        question_hash = get_question_hash(question)
        if not question:
            data = await db.read_questions()
            remaining_attempts = get_remaining_attempts(user_id, data)
            await update.message.reply_text(
                f"❓ Напиши `/ask <вопрос>`, например: `/ask Какая твоя любимая игра на стримах?`\n"
                f"📌 *Попыток*: {remaining_attempts}/3",
                parse_mode="Markdown"
            )
            return
        is_valid, reason = check_question_meaning(question)
        if not is_valid:
            data = await db.read_questions()
            remaining_attempts = get_remaining_attempts(user_id, data)
            await update.message.reply_text(
                f"❌ Вопрос отклонён: {reason} 😿\n"
                f"📌 *Попыток*: {remaining_attempts}/3\n"
                f"Смотри `/guide` для подсказок!",
                parse_mode="Markdown"
            )
            logger.info(f"Вопрос отклонён от user_id {user_id}: {reason} ({question})")
            return
        data = await db.read_questions()
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
            "username": user.username or "Аноним",
            "question": question,
            "status": "pending",
            "notify": False,
            "cancelled": False,
            "cancel_reason": "",
            "reject_reason": ""
        })
        await db.write_questions(data)
        if user_id not in question_hashes:
            question_hashes[user_id] = []
        question_hashes[user_id].append(question_hash)
        spam_protection[user_id] = {"last_ask_time": current_time, "last_question": question}
        remaining_attempts = get_remaining_attempts(user_id, data)
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
                 f"**От**: @{escape_markdown(user.username or 'Аноним', version=2)}\n"
                 f"**Вопрос**: *{escape_markdown(question, version=2)}*\n"
                 f"• `/approve {question_id} <ответ>`\n"
                 f"• `/reject {question_id} <причина>`\n"
                 f"• `/cancel {question_id} <причина>`",
            parse_mode="MarkdownV2"
        )
    await check_update(update, context, callback)

async def my_questions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.info(f"Команда /myquestions от user_id {update.effective_user.id}")
        user_id = update.effective_user.id
        reply_to = update.message or update.callback_query.message
        if not reply_to:
            logger.info("Отсутствует reply_to")
            return
        data = await db.read_questions()
        user_questions = [q for q in data["questions"] if q["user_id"] == user_id and not q.get("cancelled", False)]
        remaining_attempts = get_remaining_attempts(user_id, data)
        if not user_questions:
            await reply_to.reply_text(
                f"📭 *Ты не задал вопросов*! *Попыток*: {remaining_attempts}/3.\n"
                f"Пиши `/ask` или `/guide`! 🚀",
                parse_mode="Markdown"
            )
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
    if update.callback_query:
        await update.callback_query.answer()
        await callback(update, context)
    else:
        await check_update(update, context, callback)

async def main_async():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("guide", guide))
    app.add_handler(CommandHandler("ask", ask))
    app.add_handler(CommandHandler("myquestions", my_questions))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(button_callback))
    logger.info("Бот запущен")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main_async())