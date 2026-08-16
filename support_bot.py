import logging
import os
import nest_asyncio

import asyncio
from aiohttp import web

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Патч для решения проблемы с event loop в некоторых средах, включая Render
nest_asyncio.apply()

# --- НАСТРОЙКИ (теперь из переменных окружения) ---

# Токен вашего бота. Получается из переменной окружения BOT_TOKEN.
BOT_TOKEN = os.getenv("BOT_TOKEN")

# ID администраторов. Получается из переменной окружения ADMIN_IDS (числа через запятую).
admin_ids_str = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(admin_id) for admin_id in admin_ids_str.split(",") if admin_id]

# ID приватной группы для администраторов. Получается из переменной окружения ADMIN_GROUP_ID.
try:
    ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID"))
except (TypeError, ValueError):
    ADMIN_GROUP_ID = 0

# Проверка, что все переменные окружения установлены, иначе бот не запустится
if not all([BOT_TOKEN, ADMIN_IDS, ADMIN_GROUP_ID]):
    # Эта ошибка будет видна в логах на сервере, если вы забудете что-то указать
    raise RuntimeError("Не все переменные окружения установлены! (BOT_TOKEN, ADMIN_IDS, ADMIN_GROUP_ID)")

# --- КОНЕЦ НАСТРОЕК ---

# Включаем логирование, чтобы видеть ошибки
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Веб-сервер для Health Check ---

async def health_check(request: web.Request) -> web.Response:
    """Отвечает на health-check от Render, чтобы сервис считался 'живым'."""
    return web.Response(text="OK")

async def run_web_server():
    """Запускает простой веб-сервер для ответов на health-check."""
    # Render предоставляет порт через переменную окружения PORT
    port = int(os.environ.get("PORT", 8080))
    
    app = web.Application()
    app.add_routes([web.get("/", health_check)])
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    
    try:
        await site.start()
        logger.info(f"Health check web server started on port {port}")
        # Эта корутина должна работать вечно, пока ее не отменят
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        logger.info("Web server stopped.")

# Функция для команды /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет приветственное сообщение при команде /start."""
    user = update.effective_user
    welcome_text = (
        f"👋 Привет, {user.full_name}!\n\n"
        "Это поддержка игры «Ушко Кликер».\n\n"
        "Чтобы задать вопрос, просто напиши его в этот чат. "
        "Мы постараемся ответить как можно скорее.\n\n"
        "Пожалуйста, опиши свою проблему как можно подробнее."
    )
    await update.message.reply_text(welcome_text)

# Функция для пересылки сообщений от пользователя администраторам
async def forward_to_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пересылает сообщение пользователя в админский чат."""
    user = update.effective_user
    message = update.message

    # Не пересылаем сообщения от админов, чтобы избежать путаницы
    if user.id in ADMIN_IDS:
        return

    # Формируем красивое сообщение для админов
    header = (
        f"❗️ Новое обращение от пользователя:\n"
        f"Имя: {user.full_name}\n"
        f"ID: `{user.id}`" # ID нужен для ответа
    )
    
    # Отправляем заголовок в админский чат
    await context.bot.send_message(
        chat_id=ADMIN_GROUP_ID,
        text=header,
        parse_mode='Markdown'
    )
    # Пересылаем само сообщение пользователя
    await context.bot.forward_message(
        chat_id=ADMIN_GROUP_ID,
        from_chat_id=user.id,
        message_id=message.message_id
    )
    
    await update.message.reply_text("✅ Ваше сообщение отправлено в поддержку. Ожидайте, пожалуйста, ответа.")

# Функция для ответа администратора пользователю
async def reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет ответ от админа пользователю."""
    # Проверяем, что это админ отвечает в админской группе
    if update.message.chat_id != ADMIN_GROUP_ID or update.message.from_user.id not in ADMIN_IDS:
        return

    # Проверяем, что это ответ на сообщение
    if not update.message.reply_to_message:
        await update.message.reply_text("Чтобы ответить пользователю, используйте функцию «Ответить» (Reply) на его сообщение.")
        return

    # Пытаемся извлечь ID пользователя из заголовка или пересланного сообщения
    user_id_to_reply = None
    
    # Вариант 1: Ответ на наш заголовок с ID
    if update.message.reply_to_message.text and "ID:" in update.message.reply_to_message.text:
        try:
            # Ищем строку "ID: `123456789`" и извлекаем число
            text = update.message.reply_to_message.text
            user_id_str = text.split("ID: `")[1].split("`")[0]
            user_id_to_reply = int(user_id_str)
        except (IndexError, ValueError):
            await update.message.reply_text("Не удалось извлечь ID пользователя из заголовка. Попробуйте ответить на пересланное сообщение.")
            return

    # Вариант 2: Ответ на пересланное сообщение
    elif update.message.reply_to_message.forward_from:
        user_id_to_reply = update.message.reply_to_message.forward_from.id
    
    if not user_id_to_reply:
        await update.message.reply_text("Не могу определить, какому пользователю отвечать. Убедитесь, что отвечаете на правильное сообщение.")
        return

    # Отправляем ответ пользователю
    try:
        admin_name = update.message.from_user.full_name
        reply_text = f"💬 Ответ от поддержки ({admin_name}):\n\n{update.message.text}"
        
        await context.bot.send_message(
            chat_id=user_id_to_reply,
            text=reply_text
        )
        # Уведомляем админа, что ответ успешно отправлен
        await update.message.reply_text("✅ Ответ успешно отправлен пользователю.")
    except Exception as e:
        logger.error(f"Ошибка при отправке ответа пользователю {user_id_to_reply}: {e}")
        await update.message.reply_text(f"❌ Не удалось отправить ответ. Ошибка: {e}")


async def main() -> None:
    """Основная функция для запуска бота."""
    application = Application.builder().token(BOT_TOKEN).build()

    # Добавляем обработчик команды /start
    application.add_handler(CommandHandler("start", start))

    # Добавляем обработчик для ответов админов в группе
    # Он должен стоять ПЕРЕД обработчиком сообщений от пользователей
    application.add_handler(MessageHandler(
        filters.Chat(chat_id=ADMIN_GROUP_ID) & filters.REPLY & filters.TEXT & ~filters.COMMAND,
        reply_to_user
    ))

    # Добавляем обработчик для всех остальных текстовых сообщений от пользователей
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, 
        forward_to_admins
    ))
    
    # Используем контекстный менеджер, который управляет запуском и остановкой.
    async with application:
        # Запускаем поллинг в фоновом режиме
        await application.start()
        await application.updater.start_polling()
        logger.info("Bot polling started...")
        
        # Запускаем веб-сервер. Он будет работать, пока процесс не будет остановлен.
        await run_web_server()

if __name__ == "__main__":
    asyncio.run(main())
