import json
import logging
import os
import time
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from dotenv import load_dotenv
from flask import Flask, request
from hypercorn.config import Config
from hypercorn.asyncio import serve

# Flask для вебхуков
flask_app = Flask(__name__)

# Загрузка переменных окружения
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN не задан в переменной окружения или .env файле!")

# Настройка логирования (stdout + файл)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),  # Вывод в stdout для Railway
        logging.FileHandler("bot.log", encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

# Константы
ADMIN_ID = 335236137
QUESTIONS_FILE = "questions.json"
BLACKLIST_FILE = "blacklist.json"
QA_WEBSITE = "https://mortisplay.ru/qa.html"
WEBHOOK_URL = "https://mortisplayqabot-production.up.railway.app/webhook"

# Перевод статусов
STATUS_TRANSLATIONS = {
    "pending": "Рассматривается",
    "approved": "Принят",
    "rejected": "Отклонён"
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

# Глобальная переменная для Application
app = None

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

    keyboard = [
        [InlineKeyboardButton("Задать вопрос 🔥", callback_data="ask")],
        [InlineKeyboardButton("Мои вопросы 😸", callback_data="myquestions")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Привет! 😎 Это *Q&A-бот Mortis Play*! Задавай вопросы для стримов и сайта! 🔥 Пиши `/ask` или жми кнопки ниже!",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

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

    keyboard = [
        [InlineKeyboardButton("Задать вопрос 🔥", callback_data="ask")],
        [InlineKeyboardButton("Мои вопросы 😸", callback_data="myquestions")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Привет! 😎 Я бот Mortis Play, чтобы ты мог задавать вопросы для сайта!\n\n"
        "*Что я умею:*\n"
        "- `/start` — Начни общение со мной! 😸\n"
        "- `/ask <вопрос>` — Задай вопрос, например: `/ask Какая твоя любимая игра?`\n"
        "- `/myquestions` — Посмотри свои вопросы и их статус 😺\n"
        "- `/help` — Покажу это сообщение с подсказками 🕶\n"
        "- `/list` — *Только для админа*, показывает все вопросы\n"
        "- `/clear` — *Только для админа*, очищает все вопросы\n"
        "- `/delete <id>` — *Только для админа*, удаляет вопрос по ID\n"
        "- `/edit <id> <новый_вопрос>` — *Только для админа*, редактирует вопрос по ID\n\n"
        "Пиши `/ask` или жми кнопки ниже, чтобы начать! 🚀",
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

    if not data.get("questions"):
        await update.message.reply_text("Пока *нет вопросов*! 😿", parse_mode="Markdown")
        logger.info("Список вопросов пуст")
        return
    response = "*Список вопросов*:\n"
    for q in data["questions"]:
        status = STATUS_TRANSLATIONS.get(q["status"], q["status"])
        response += f"ID: `{q['id']}`, От: @{q['username']}, Вопрос: *{q['question']}*, Статус: `{status}`\n"
    await update.message.reply_text(response, parse_mode="Markdown")
    logger.info(f"Админ запросил список вопросов: {len(data['questions'])} вопросов")

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

    user_questions = [q for q in data["questions"] if q["user_id"] == user_id]
    if not user_questions:
        await update.message.reply_text("Ты ещё *не задал вопрос*! 😿 Пиши `/ask <вопрос>`", parse_mode="Markdown")
        logger.info(f"Пользователь user_id {user_id} запросил свои вопросы: список пуст")
        return
    response = "*Твои вопросы*:\n"
    for q in user_questions:
        status = STATUS_TRANSLATIONS.get(q["status"], q["status"])
        answer = f", Ответ: *{q['answer']}*" if q["status"] == "approved" and "answer" in q else ""
        response += f"ID: `{q['id']}`, Вопрос: *{q['question']}*, Статус: `{status}`{answer}\n"
    await update.message.reply_text(response, parse_mode="Markdown")
    logger.info(f"Пользователь user_id {user_id} запросил свои вопросы: {len(user_questions)} вопросов")

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

    current_time = time.time()
    if user_id in spam_protection:
        last_ask_time = spam_protection[user_id]["last_ask_time"]
        last_question = spam_protection[user_id]["last_question"]
        if current_time - last_ask_time < 60:
            await update.message.reply_text("Йоу, *не так быстро*! 😎 Один вопрос в минуту!", parse_mode="Markdown")
            logger.info(f"Спам-атака от user_id {user_id}: слишком частые вопросы")
            return
        if question == last_question:
            await update.message.reply_text("Эй, ты *уже спрашивал* это! 😕 Попробуй другой вопрос.", parse_mode="Markdown")
            logger.info(f"Дубликат вопроса от user_id {user_id}: {question}")
            return

    if not context.args and update.message.text.startswith("/ask"):
        await update.message.reply_text(
            "Йоу, напиши *вопрос* после `/ask`, например: `/ask Какая твоя любимая игра?`",
            parse_mode="Markdown"
        )
        return

    if len(question) < 5 or len(question) > 500:
        await update.message.reply_text(
            "Вопрос должен быть от *5 до 500 символов*! 😎",
            parse_mode="Markdown"
        )
        logger.info(f"Недопустимая длина вопроса от user_id {user_id}: {len(question)} символов")
        return

    if check_blacklist(question):
        await update.message.reply_text(
            "Йоу, твой вопрос содержит *запрещённые слова*! 😿 Попробуй другой.",
            parse_mode="Markdown"
        )
        logger.info(f"Вопрос отклонён из-за чёрного списка: {question}")
        return

    try:
        with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Ошибка чтения {QUESTIONS_FILE}: {e}")
        await update.message.reply_text("Ошибка записи вопроса! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
        return

    question_id = len(data["questions"]) + 1
    data["questions"].append({
        "id": question_id,
        "user_id": user_id,
        "username": user.username or "Аноним",
        "question": question,
        "status": "pending",
        "notify": False
    })

    try:
        with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except IOError as e:
        logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
        await update.message.reply_text("Ошибка записи вопроса! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
        return

    spam_protection[user_id] = {"last_ask_time": current_time, "last_question": question}

    keyboard = [[InlineKeyboardButton("Уведомить о результате 🔔", callback_data=f"notify_{question_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "**Вопрос принят!** Жди ответа на *сайте*! 😸 *Доге одобряет* 🐶\n\n"
        "*Добавление вопроса на сайт может занять от 1 до 48 часов.* Если вопрос не появился, пиши в личку *@dimap7221*! 😎",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

    escaped_question = escape_markdown(question, version=2)
    escaped_username = escape_markdown(user.username or "Аноним", version=2)
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"*Новый вопрос* \\(ID: `{question_id}`\\)\nОт: @{escaped_username}\nВопрос: *{escaped_question}*\n`/approve {question_id} <ответ>` — принять\n`/reject {question_id}` — отклонить",
            parse_mode="MarkdownV2"
        )
        logger.info(f"Уведомление админу отправлено: вопрос ID {question_id} от @{user.username or 'Аноним'}")
    except Exception as e:
        logger.error(f"Ошибка отправки уведомления админу: {e}")
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"*Новый вопрос* (ID: {question_id})\nОт: @{user.username or 'Аноним'}\nВопрос: {question}\n`/approve {question_id} <ответ>` — принять\n`/reject {question_id}` — отклонить",
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
        if q["id"] == question_id and q["user_id"] == user_id:
            q["notify"] = True
            break
    else:
        await query.message.reply_text("Вопрос не найден! 😿", parse_mode="Markdown")
        logger.warning(f"Вопрос ID {question_id} не найден для уведомления user_id {user_id}")
        return

    try:
        with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except IOError as e:
        logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
        await query.message.reply_text("Ошибка обработки уведомления! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
        return

    await query.message.edit_text(
        "**Вопрос принят!** Ты будешь *уведомлён* о результате! 😎",
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
            if q["id"] == question_id and q["status"] == "pending":
                q["status"] = "approved"
                q["answer"] = answer
                website_button = [[InlineKeyboardButton("Посмотреть на сайте 🌐", url=QA_WEBSITE)]]
                reply_markup = InlineKeyboardMarkup(website_button)
                if q["notify"]:
                    try:
                        escaped_answer = escape_markdown(answer, version=2)
                        await context.bot.send_message(
                            chat_id=q["user_id"],
                            text=f"*Твой вопрос принят!* 😎 Ответ: *{escaped_answer}*\nСмотри на сайте!",
                            reply_markup=reply_markup,
                            parse_mode="MarkdownV2"
                        )
                        logger.info(f"Уведомление о принятии отправлено user_id {q['user_id']} для вопроса ID {question_id}")
                    except Exception as e:
                        logger.error(f"Ошибка уведомления пользователя {q['user_id']}: {e}")
                        await context.bot.send_message(
                            chat_id=q["user_id"],
                            text=f"**Твой вопрос принят!** 😎 Ответ: {answer}\nСмотри на сайте!",
                            reply_markup=reply_markup,
                            parse_mode=None
                        )
                break
        else:
            await update.message.reply_text(
                f"Вопрос с ID `{question_id}` *не найден* или уже обработан!",
                parse_mode="Markdown"
            )
            logger.warning(f"Вопрос ID {question_id} не найден или уже обработан")
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
            if q["id"] == question_id and q["status"] == "pending":
                q["status"] = "rejected"
                if q["notify"]:
                    try:
                        await context.bot.send_message(
                            chat_id=q["user_id"],
                            text="Твой вопрос *отклонён* 😕 Попробуй задать другой!",
                            parse_mode="Markdown"
                        )
                        logger.info(f"Уведомление об отклонении отправлено user_id {q['user_id']} для вопроса ID {question_id}")
                    except Exception as e:
                        logger.error(f"Ошибка уведомления пользователя {q['user_id']}: {e}")
                break
        else:
            await update.message.reply_text(
                f"Вопрос с ID `{question_id}` *не найден* или уже обработан!",
                parse_mode="Markdown"
            )
            logger.warning(f"Вопрос ID {question_id} не найден или уже обработан")
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
        await update.message.reply_text(
            "Все вопросы *очищены*! 😺 ID начнётся с 1.",
            parse_mode="Markdown"
        )
        logger.info("Все вопросы успешно очищены")
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
        if len(new_question) < 5 or len(new_question) > 500:
            await update.message.reply_text(
                "Новый вопрос должен быть от *5 до 500 символов*! 😎",
                parse_mode="Markdown"
            )
            logger.error(f"Ошибка в /edit: недопустимая длина вопроса, команда: {update.message.text}")
            return

        if check_blacklist(new_question):
            await update.message.reply_text(
                "Йоу, новый вопрос содержит *запрещённые слова*! 😿 Попробуй другой.",
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
            if q["id"] == question_id:
                old_question = q["question"]
                q["question"] = new_question
                try:
                    with open(QUESTIONS_FILE, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except IOError as e:
                    logger.error(f"Ошибка записи в {QUESTIONS_FILE}: {e}")
                    await update.message.reply_text("Ошибка редактирования вопроса! 😿 Свяжитесь с разработчиком.", parse_mode="Markdown")
                    return
                await update.message.reply_text(
                    f"Вопрос `{question_id}` *отредактирован*! 😺\nСтарый: *{old_question}*\nНовый: *{new_question}*",
                    parse_mode="Markdown"
                )
                logger.info(f"Вопрос ID {question_id} отредактирован: старый: {old_question}, новый: {new_question}")
                return

        await update.message.reply_text(
            f"Вопрос с ID `{question_id}` *не найден*! 😿",
            parse_mode="Markdown"
        )
        logger.warning(f"Вопрос ID {question_id} не найден для редактирования")
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
            "Йоу, *админ*! Используй `/approve`, `/reject`, `/list`, `/clear`, `/delete`, `/edit` или `/ask` для теста вопроса 😎",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "Пиши `/ask <вопрос>`, чтобы задать *эпичный* вопрос! 😎",
            parse_mode="Markdown"
        )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Callback button от user_id {update.effective_user.id}: {update.callback_query.data}")
    query = update.callback_query
    await query.answer()
    if query.data == "ask":
        await query.message.reply_text(
            "Йоу, напиши `/ask <твой вопрос>`, например: `/ask Какая твоя любимая игра?`",
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

        user_questions = [q for q in data["questions"] if q["user_id"] == user_id]
        if not user_questions:
            await query.message.reply_text(
                "Ты ещё *не задал вопросов*! 😿 Пиши `/ask <вопрос>`",
                parse_mode="Markdown"
            )
            return
        response = "*Твои вопросы*:\n"
        for q in user_questions:
            status = STATUS_TRANSLATIONS.get(q["status"], q["status"])
            answer = f", Ответ: *{q['answer']}*" if q["status"] == "approved" and "answer" in q else ""
            response += f"ID: `{q['id']}`, Вопрос: *{q['question']}*, Статус: `{status}`{answer}\n"
        await query.message.reply_text(response, parse_mode="Markdown")

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

@flask_app.route("/", methods=["GET"])
async def health_check():
    logger.info(f"Получен запрос на /: headers={request.headers}")
    return "Bot is running!", 200

@flask_app.route("/webhook", methods=["POST", "GET"])
async def webhook():
    global app
    logger.info(f"Получен запрос на вебхук: метод={request.method}, url={request.url}, headers={request.headers}")
    if not app:
        logger.error("Application не инициализирован")
        return "Application not initialized", 500
    if request.method == "GET":
        logger.info("GET-запрос на вебхук, возвращаем OK для проверки")
        return "OK", 200
    try:
        json_data = request.get_json(force=True)
        logger.info(f"Получены данные вебхука: {json_data}")
        if not json_data or 'update_id' not in json_data:
            logger.warning("Получен невалидный JSON вебхука")
            return "Invalid webhook JSON", 400
        if 'message' in json_data and 'date' not in json_data['message']:
            logger.warning("Отсутствует поле 'date' в message")
            return "Missing 'date' in message", 400
        update = Update.de_json(json_data, app.bot)
        if not update:
            logger.warning("Получено пустое обновление")
            return "Empty update", 400
        logger.info(f"Обновление получено: update_id={update.update_id}, type={type(update)}")
        await app.process_update(update)
        logger.info(f"Обновление {update.update_id} обработано")
        return "OK", 200
    except Exception as e:
        logger.error(f"Ошибка в вебхуке: {str(e)}")
        return f"Error processing webhook: {str(e)}", 500

async def main_async():
    global app
    logger.info(f"Бот стартовал с Python {os.sys.version}")
    logger.info(f"Используемый токен: {TOKEN[:10]}...{TOKEN[-10:]}")
    try:
        app = Application.builder().token(TOKEN).updater(None).build()
        await app.initialize()  # Инициализация Application
        logger.info("Application успешно инициализирован")
    except Exception as e:
        logger.error(f"Ошибка инициализации бота: {e}")
        if "InvalidToken" in str(e) or "401" in str(e):
            logger.error("Токен недействителен! Проверь TELEGRAM_TOKEN в .env или @BotFather.")
        raise
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("list", list_questions))
    app.add_handler(CommandHandler("myquestions", my_questions))
    app.add_handler(CommandHandler("ask", ask))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("delete", delete))
    app.add_handler(CommandHandler("edit", edit))
    app.add_handler(CallbackQueryHandler(notify_callback, pattern="^notify_"))
    app.add_handler(CallbackQueryHandler(button_callback, pattern="^(ask|myquestions)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.StatusUpdate.ALL, lambda u, c: None))
    app.add_error_handler(error_handler)
    
    # Уведомление админа при старте
    await notify_admin_on_start(app)
    
    # Запускаем Hypercorn сервер
    port = int(os.getenv("PORT", 8080))
    logger.info(f"Запускаем Flask на порту {port} с Hypercorn")
    config = Config()
    config.bind = [f"0.0.0.0:{port}"]
    # Запускаем сервер в фоновой задаче
    server_task = asyncio.create_task(serve(flask_app, config))
    
    # Ждём завершения сервера (или прерывания)
    try:
        await server_task
    except asyncio.CancelledError:
        logger.info("Сервер Hypercorn остановлен")
        await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main_async())