import asyncio
import logging
import aiosqlite
import os
import json
from redis.asyncio import Redis
from aiogram import Bot, Dispatcher, types, Router
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
DB_PATH = os.getenv("DB_PATH")
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = os.getenv("REDIS_PORT")

if not TOKEN:
    raise ValueError("Ошибка: Переменная окружения BOT_TOKEN не задана!")
if not CHANNEL_ID:
    raise ValueError("Ошибка: Переменная окружения CHANNEL_ID не задана!")

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- Инициализация глобальных объектов ---
router = Router()

# Подключение к Redis
async def get_redis_client() -> Redis:
    return Redis(host=REDIS_HOST, port=REDIS_PORT, db=5)

# Инициализация FSM-хранилища
async def init_storage() -> RedisStorage:
    redis_client = await get_redis_client()
    return RedisStorage(redis_client)

# Определяем состояния
class Survey(StatesGroup):
    collecting_answers = State()

questions = [
    "Представьтесь, пожалуйста (ФИО)",
    "Ты тратишь время на фоллоу-апы или это скорее формальность и лучше приступить к задаче быстрее?",
    "Тебя добавили во встречу, где нет повестки. Пойдешь?",
    "Ты реализовал свою идею и получил неплохой результат (построил процесс/ внедрил свой инструмент). А после узнал, что подобное решение уже сделано иначе в соседней команде. Но вам предстоит работать над общим решением - как будешь действовать?",
    "Назревает конфликт с коллегой, есть ощущение, что у вас разнонаправленные цели. Как ты будешь решать их? Пригласишь на встречу в офисе или онлайн? Стоит ли это как-то решать в принципе? В каком случае эскалация была бы эффективнее?",
    "На встрече договорились, что ты с командой возьмёте задачу на себя. Но вы не успеваете, есть более приоритетные задачи. Плюс, ты так до конца и не понял, зачем ее делать. Как поступишь?",
    "Скоро перформанс ревью, а коллеги сдали проект, но он не соответствует твоим ожиданиям. Ты им сообщил, что всё плохо. Попросишь переделать? Эскалируешь? Что-то другое?",
]

async def send_message_to_channel(bot: Bot, response_text: str):
    try:
        chunk_size = 4096
        messages = response_text.split("\n\n")  # Разбиваем по логическим блокам

        current_message = ""
        for part in messages:
            if len(current_message) + len(part) + 2 < chunk_size:
                current_message += part + "\n\n"
            else:
                await bot.send_message(CHANNEL_ID, current_message)
                current_message = part + "\n\n"
        
        if current_message:
            await bot.send_message(CHANNEL_ID, current_message)
    except Exception as e:
        logging.error(f"Ошибка при отправке в канал: {e}")

@router.message(Command("start"))
async def start_survey(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Привет! Давай начнем опрос.")
    await state.update_data(answers={}, current_question=0)
    await state.set_state(Survey.collecting_answers)
    await message.answer(questions[0])
    asyncio.create_task(auto_reset_state(message.from_user.id, state))

@router.message()
async def process_answer(message: types.Message, state: FSMContext):
    data = await state.get_data()
    current_question = data.get("current_question", 0)
    answers = data.get("answers", {})
    answers[questions[current_question]] = message.text
    await state.update_data(answers=answers)

    if current_question + 1 < len(questions):
        await state.update_data(current_question=current_question + 1)
        await message.answer(questions[current_question + 1])
    else:
        user_id = message.from_user.id
        username = message.from_user.username or "Неизвестно"
        full_name = message.from_user.full_name or "Неизвестно"
        answers_json = json.dumps(answers, ensure_ascii=False)
        response_text = f"<b>Пользователь:</b> {full_name} (@{username})\n\n" + "\n\n".join([f"<b>{q}</b>\nОтвет: {a}" for q, a in answers.items()])

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA journal_mode=WAL;")
                await db.execute(
                    "INSERT INTO responses (user_id, username, full_name, answers) VALUES (?, ?, ?, ?)",
                    (user_id, username, full_name, answers_json),
                )
                await db.commit()
                logging.info(f"✅ Данные записаны в БД для user_id={user_id}")
        except Exception as e:
            logging.error(f"❌ Ошибка записи в БД: {e}")

        await send_message_to_channel(message.bot, response_text)
        await message.answer("Спасибо за участие в опросе! Чтобы пройти опрос снова, отправьте /start.")
        await state.clear()
        redis = await get_redis_client()
        await redis.delete(f"fsm:{user_id}")

async def auto_reset_state(user_id: int, state: FSMContext):
    await asyncio.sleep(1800)
    await state.clear()
    redis = await get_redis_client()
    await redis.delete(f"fsm:{user_id}")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                answers TEXT
            )
        """)
        await db.commit()
        logging.info("✅ База данных инициализирована")

async def main():
    await init_db()
    storage = await init_storage()
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=storage)
    dp.include_router(router)
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
