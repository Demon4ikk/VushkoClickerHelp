import logging
import os
import nest_asyncio

import re
import asyncio
from aiohttp import web

from telegram import Update, MessageOriginUser
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

# Временное хранилище заблокированных пользователей (ID). При перезапуске сбрасывается.
MUTED_USERS = set()

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

    # Проверяем, не замучен ли пользователь
    if user.id in MUTED_USERS:
        logger.info(f"Получено сообщение от заблокированного пользователя {user.id}. Игнорируем.")
        return # Молча игнорируем

    logger.info(f"Получено сообщение от пользователя {user.full_name} (ID: {user.id})")

    # Не пересылаем сообщения от админов, чтобы избежать путаницы
    if user.id in ADMIN_IDS:
        logger.info(f"Сообщение от админа {user.full_name}. Пересылка не требуется.")
        return

    try:
        # Формируем красивое сообщение для админов
        header = (
            f"❗️ Новое обращение от пользователя:\n"
            f"Имя: {user.full_name}\n"
            f"ID: `{user.id}`" # ID нужен для ответа
        )
        
        # Отправляем заголовок в админский чат
        logger.info(f"Пересылка сообщения в группу {ADMIN_GROUP_ID}")
        header_message = await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            text=header,
        )
        # Копируем сообщение пользователя как ответ на заголовок, создавая ветку
        await context.bot.copy_message(
            chat_id=ADMIN_GROUP_ID,
            from_chat_id=user.id,
            message_id=message.message_id,
            reply_to_message_id=header_message.message_id
        )
        
        await update.message.reply_text("✅ Ваше сообщение отправлено в поддержку. Ожидайте, пожалуйста, ответа.")
        logger.info(f"Сообщение от {user.id} успешно скопировано в группу. Пользователю отправлено подтверждение.")
    except Exception as e:
        logger.error(f"Ошибка при пересылке сообщения от {user.id}: {e}", exc_info=True)
        await update.message.reply_text("Произошла внутренняя ошибка при отправке вашего сообщения. Мы уже уведомлены и работаем над решением.")

def _extract_user_id_from_thread(message: 'telegram.Message') -> int | None:
    """
    Проходит по цепочке ответов, чтобы найти исходное сообщение-заголовок от бота
    и извлечь из него ID пользователя.
    """
    header_message = None
    
    # Вариант 1: Ответ на само сообщение-заголовок
    if message.text and "ID:" in message.text and message.from_user.is_bot:
        header_message = message
        
    # Вариант 2: Ответ на скопированное сообщение (которое является ответом на заголовок)
    elif message.reply_to_message:
        potential_header = message.reply_to_message
        if potential_header.text and "ID:" in potential_header.text and potential_header.from_user.is_bot:
            header_message = potential_header

    if header_message:
        match = re.search(r"ID: `(\d+)`", header_message.text)
        if match:
            return int(match.group(1))
    return None

# Функция для ответа администратора пользователю
async def reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет ответ от админа пользователю."""
    logger.info("Получено сообщение в группе поддержки. Проверяем, является ли оно ответом админа...")
    # Проверяем, что это админ отвечает в админской группе
    if update.message.chat_id != ADMIN_GROUP_ID or update.message.from_user.id not in ADMIN_IDS:
        # Это сообщение не от админа или не в той группе, игнорируем
        return

    # Проверяем, что это ответ на сообщение
    if not update.message.reply_to_message:
        logger.info("Сообщение в группе от админа, но не является ответом. Игнорируем.")
        return

    logger.info("Сообщение является ответом. Пытаемся извлечь ID пользователя для ответа.")
    # Пытаемся извлечь ID пользователя из заголовка или пересланного сообщения
    user_id_to_reply = _extract_user_id_from_thread(update.message.reply_to_message)
    
    if not user_id_to_reply:
        logger.warning("Не удалось определить ID пользователя для ответа. Не найдено сообщение-заголовок в цепочке ответов.")
        await update.message.reply_text(
            "⚠️ Не могу определить, какому пользователю отвечать.\n\n"
            "Пожалуйста, используйте функцию «Ответить» на любое сообщение от бота в ветке обращения."
        )
        return

    # Отправляем ответ пользователю.
    # Сначала отправляем заголовок, а потом копируем сообщение админа,
    # чтобы можно было отправлять не только текст, но и фото, стикеры и т.д.
    try:
        admin_reply_message = update.message
        header_text = "💬 Ответ от поддержки:"
        logger.info(f"Отправляем ответ пользователю {user_id_to_reply}.")

        # Если админ ответил текстом, отправляем одно сообщение
        if admin_reply_message.text:
            full_text = f"{header_text}\n\n{admin_reply_message.text}"
            await context.bot.send_message(
                chat_id=user_id_to_reply,
                text=full_text
            )
        # Если админ ответил медиа, стикером и т.д., копируем сообщение с заголовком в подписи
        else:
            original_caption = admin_reply_message.caption or ""
            new_caption = f"{header_text}\n\n{original_caption}".strip()
            await admin_reply_message.copy(
                chat_id=user_id_to_reply,
                caption=new_caption
            )

        # Уведомляем админа, что ответ успешно отправлен
        await update.message.reply_text("✅ Ответ успешно отправлен пользователю.")
        logger.info(f"Ответ пользователю {user_id_to_reply} успешно отправлен.")
    except Exception as e:
        logger.error(f"Ошибка при отправке ответа пользователю {user_id_to_reply}: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Не удалось отправить ответ. Ошибка: {e}")

async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Блокирует пользователя, запрещая ему отправлять сообщения боту."""
    user_id_to_mute = None
    
    # Способ 1: /mute <user_id>
    if context.args:
        try:
            user_id_to_mute = int(context.args[0])
        except (IndexError, ValueError):
            await update.message.reply_text("⚠️ Неверный формат. Используйте /mute <ID> или ответьте на сообщение командой /mute.")
            return
            
    # Способ 2: Ответить на сообщение в ветке командой /mute
    elif update.message.reply_to_message:
        user_id_to_mute = _extract_user_id_from_thread(update.message.reply_to_message)

    if not user_id_to_mute:
        await update.message.reply_text("⚠️ Не удалось определить пользователя для блокировки. Используйте /mute <ID> или ответьте на его сообщение.")
        return

    MUTED_USERS.add(user_id_to_mute)
    logger.info(f"Администратор {update.message.from_user.id} заблокировал пользователя {user_id_to_mute}.")
    await update.message.reply_text(f"✅ Пользователь с ID `{user_id_to_mute}` заблокирован.")

async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Разблокирует пользователя."""
    user_id_to_unmute = None
    
    # Способ 1: /unmute <user_id>
    if context.args:
        try:
            user_id_to_unmute = int(context.args[0])
        except (IndexError, ValueError):
            await update.message.reply_text("⚠️ Неверный формат. Используйте /unmute <ID> или ответьте на сообщение командой /unmute.")
            return
            
    # Способ 2: Ответить на сообщение в ветке командой /unmute
    elif update.message.reply_to_message:
        user_id_to_unmute = _extract_user_id_from_thread(update.message.reply_to_message)

    if not user_id_to_unmute:
        await update.message.reply_text("⚠️ Не удалось определить пользователя для разблокировки. Используйте /unmute <ID> или ответьте на его сообщение.")
        return

    if user_id_to_unmute in MUTED_USERS:
        MUTED_USERS.remove(user_id_to_unmute)
        logger.info(f"Администратор {update.message.from_user.id} разблокировал пользователя {user_id_to_unmute}.")
        await update.message.reply_text(f"✅ Пользователь с ID `{user_id_to_unmute}` разблокирован.")
    else:
        await update.message.reply_text(f"ℹ️ Пользователь с ID `{user_id_to_unmute}` не найден в списке заблокированных.")

async def main() -> None:
    """Основная функция для запуска бота."""
    application = Application.builder().token(BOT_TOKEN).build()

    # Добавляем обработчик команды /start
    application.add_handler(CommandHandler("start", start))

    # Команды для администрирования (доступны только админам в группе поддержки)
    admin_commands_filter = filters.Chat(chat_id=ADMIN_GROUP_ID) & filters.User(user_id=ADMIN_IDS)
    application.add_handler(CommandHandler("mute", mute_user, filters=admin_commands_filter))
    application.add_handler(CommandHandler("unmute", unmute_user, filters=admin_commands_filter))


    # Добавляем обработчик для ответов админов в группе
    # Он должен стоять ПЕРЕД обработчиком сообщений от пользователей
    application.add_handler(MessageHandler(
        filters.Chat(chat_id=ADMIN_GROUP_ID) & filters.REPLY & ~filters.COMMAND, # Ответы админов не должны быть командами
        reply_to_user
    ))

    # Добавляем обработчик для всех сообщений от пользователей в личке (кроме команд)
    application.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND, 
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
