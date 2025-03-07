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

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

if not TOKEN:
    raise ValueError("Ошибка: Переменная окружения BOT_TOKEN не задана!")
if not CHANNEL_ID:
    raise ValueError("Ошибка: Переменная окружения CHANNEL_ID не задана!")

# --- Инициализация глобальных переменных/объектов ---
router = Router()
queue = asyncio.Queue()  # Очередь для отправки сообщений

# Определяем состояния
class Survey(StatesGroup):
    collecting_answers = State()

questions = [
    "Представьтесь, пожалуйста (ФИО)",
    "Ты тратишь время на фоллоупы или это формальность и лучше приступить к задаче быстрее?",
    "Тебя добавили во встречу, где нет повестки. Пойдешь?",
    "Ты реализовал свою идею и получил результат. Узнал, что в соседней команде сделали аналог. Вам предстоит объединить решения. Как будешь действовать?",
    "Назревает конфликт с коллегой, есть ощущение, что у вас разнонаправленные цели. Как будешь решать их? Стоит ли это решать? В каком случае эскалация была бы эффективнее?",
    "На встрече договорились взять задачу, но она не приоритетна и неясно зачем делать. Как поступишь?",
    "Коллеги сдали проект, но он не соответствует ожиданиям. Ты сообщил, что все плохо. Попросишь переделать? Эскалируешь? Что-то другое?",
]

async def sender_worker(bot: Bot):
    while True:
        msg = await queue.get()
        await bot.send_message(CHANNEL_ID, msg)
        await asyncio.sleep(0.5)  # Антиспам-защита

@router.message(Command("start"))
async def start_survey(message: types.Message, state: FSMContext):
    await message.answer("Привет! Давай начнем опрос. Отвечай на вопросы честно.")
    await state.update_data(answers={}, current_question=0)
    await state.set_state(Survey.collecting_answers)
    await message.answer(questions[0])

    # Фоновая задача автоочистки (30 минут)
    asyncio.create_task(auto_reset_state(state))

@router.message()
async def process_answer(message: types.Message, state: FSMContext):
    data = await state.get_data()
    current_question = data.get("current_question", 0)
    answers = data.get("answers", {})
    answers[questions[current_question]] = message.text
    await state.update_data(answers=answers)

    if current_question + 1 < len(questions):
        # Задаём следующий вопрос
        await state.update_data(current_question=current_question + 1)
        await message.answer(questions[current_question + 1])
    else:
        # Все вопросы заданы – сохраняем результат
        user_id = message.from_user.id
        username = message.from_user.username or "Неизвестно"
        full_name = message.from_user.full_name or "Неизвестно"

        answers_json = json.dumps(answers, ensure_ascii=False)
        response_text = (
            f"<b>Пользователь:</b> {full_name} (@{username})\n\n"
            + "\n\n".join([f"<b>{q}</b>\nОтвет: {a}" for q, a in answers.items()])
        )

        # Сохранение в БД
        async with aiosqlite.connect("bot_data.db") as db:
            await db.execute(
                "INSERT INTO responses (user_id, username, full_name, answers) "
                "VALUES (?, ?, ?, ?)",
                (user_id, username, full_name, answers_json),
            )
            await db.commit()

        # Отправка в канал через очередь
        await queue.put(response_text)

        await message.answer("Спасибо за участие в опросе!")
        await state.clear()

async def auto_reset_state(state: FSMContext):
    await asyncio.sleep(1800)  # 30 минут
    # Если пользователь до сих пор «завис» в этом состоянии, чистим
    await state.clear()

async def init_db():
    # Создаём таблицу, если не существует
    async with aiosqlite.connect("bot_data.db") as db:
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

async def get_redis_client() -> Redis:
    return Redis(host="localhost", port=6379, db=5)

async def init_storage() -> RedisStorage:
    redis_client = await get_redis_client()
    return RedisStorage(redis_client)

async def main():
    logging.basicConfig(level=logging.INFO)

    # Инициализация БД
    await init_db()

    # Инициализация FSM-хранилища (Redis)
    storage = await init_storage()

    # Создаём бота и диспетчер
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=storage)
    dp.include_router(router)

    # Запускаем фоновую задачу для отправки сообщений
    asyncio.create_task(sender_worker(bot))

    # Стартуем поллинг
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
