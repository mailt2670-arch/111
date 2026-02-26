import asyncio
import logging
import os
import zipfile
import aiosqlite
import aiohttp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, Document, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════
#                   НАСТРОЙКИ
# ═══════════════════════════════════════════════

BOT_TOKEN = "8471533403:AAHO0Fa9_kPJFFEu4Jg9aqRs5nJQvv9hjWU8471533403:AAHO0Fa9_kPJFFEu4Jg9aqRs5nJQvv9hjWU"                  # @BotFather
TRONACCS_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZCI6NzE5MCwicmFuZG9tX3N0cmluZzMwIjoiNzhGQTFqaFluZm1UUVRmeGFQeGdjNk0ydWlzNDUzIiwicmFuZG9tX3N0cmluZzIwIjoiVFZMSU50a0dycmhSYXp4SDF1c1UiLCJyYW5kb21fc3RyaW5nMTAiOiJvZllsNnRMbjJBIiwicmFuZG9tX3N0cmluZzUiOiJmSmJFbSIsInJhbmRvbV9zdHJpbmcxIjoidCIsImlhdCI6MTc3MTk0MDk2OCwiZXhwIjoxODAzNDc2OTY4fQ.AGUEJ6Nr7FdXd2Jhdv9k3FS7jvpt3YfAeI5v6ZIIuKg"    # tronaccs.market API ключ
TRONACCS_BASE_URL = "https://tronaccs.market/api"
SEND_BOT_TOKEN = "538340:AAYfiIXhIM1j9Pmox8EOWnk4qyKelRwqF2H"        # токен от @send бота
SEND_API_URL = "https://pay.send.tg/api/v1"
PAYOUT_AMOUNT_RUB = 60                        # выплата продавцу в рублях
ADMIN_ID = 6789128239                          # твой Telegram ID

DB_PATH = "bot.db"
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Обязательные файлы внутри tdata zip для валидации
TDATA_REQUIRED_FILES = ["key_datas", "D877F783D5D3EF8C"]

# ═══════════════════════════════════════════════
#              ВЕРИФИКАЦИЯ TDATA
# ═══════════════════════════════════════════════

def verify_tdata_zip(file_path: str) -> tuple[bool, str]:
    """
    Проверяет что zip содержит валидную структуру tdata.
    Возвращает (True, "ok") или (False, "причина ошибки").
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            names = zf.namelist()

            if not names:
                return False, "архив пустой"

            # Ищем папку tdata внутри архива
            tdata_found = any("tdata" in n.lower() for n in names)
            if not tdata_found:
                return False, "папка tdata не найдена внутри архива"

            # Проверяем наличие ключевых файлов tdata
            names_lower = [n.lower() for n in names]
            for required in TDATA_REQUIRED_FILES:
                if not any(required.lower() in n for n in names_lower):
                    return False, f"не найден обязательный файл/папка: {required}"

            return True, "ok"

    except zipfile.BadZipFile:
        return False, "файл повреждён или не является zip-архивом"
    except Exception as e:
        return False, f"ошибка проверки: {e}"


# ═══════════════════════════════════════════════
#                   БАЗА ДАННЫХ
# ═══════════════════════════════════════════════

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_telegram_id INTEGER NOT NULL,
                tronaccs_id TEXT,
                file_path TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                verified_at TIMESTAMP,
                sold_at TIMESTAMP,
                paid_at TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seller_telegram_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                check_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()
    logger.info("DB initialized")


async def add_account(seller_id, file_path):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO accounts (seller_telegram_id, file_path, status) VALUES (?, ?, 'pending')",
            (seller_id, file_path)
        )
        await db.commit()
        return cursor.lastrowid


async def update_account_verified(account_id, tronaccs_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts SET tronaccs_id = ?, status = 'uploaded', verified_at = CURRENT_TIMESTAMP WHERE id = ?",
            (tronaccs_id, account_id)
        )
        await db.commit()


async def get_uploaded_accounts():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, seller_telegram_id, tronaccs_id FROM accounts WHERE status = 'uploaded'"
        ) as c:
            return await c.fetchall()


async def get_sold_unpaid_accounts():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, seller_telegram_id FROM accounts WHERE status = 'sold'"
        ) as c:
            return await c.fetchall()


async def mark_account_sold(account_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts SET status = 'sold', sold_at = CURRENT_TIMESTAMP WHERE id = ?",
            (account_id,)
        )
        await db.commit()


async def mark_account_paid(account_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE accounts SET status = 'paid', paid_at = CURRENT_TIMESTAMP WHERE id = ?",
            (account_id,)
        )
        await db.commit()


async def add_payout(seller_telegram_id, account_id, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO payouts (seller_telegram_id, account_id, amount) VALUES (?, ?, ?)",
            (seller_telegram_id, account_id, amount)
        )
        await db.commit()
        return cursor.lastrowid


async def mark_payout_done(payout_id, check_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE payouts SET status = 'done', check_id = ? WHERE id = ?",
            (check_id, payout_id)
        )
        await db.commit()


# ═══════════════════════════════════════════════
#              tronaccs.market API
# ═══════════════════════════════════════════════

async def upload_account(file_path):
    try:
        with open(file_path, "rb") as f:
            file_data = f.read()
    except FileNotFoundError:
        logger.error(f"File not found: {file_path}")
        return None

    async with aiohttp.ClientSession() as session:
        try:
            form = aiohttp.FormData()
            form.add_field("type", "tdata")
            form.add_field("file", file_data, filename=os.path.basename(file_path))

            async with session.post(
                f"{TRONACCS_BASE_URL}/accounts/upload",
                data=form,
                headers={"Authorization": f"Bearer {TRONACCS_API_KEY}"}
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.error(f"Upload error {resp.status}: {await resp.text()}")
                return None
        except Exception as e:
            logger.error(f"Upload exception: {e}")
            return None


async def check_account_status(tronaccs_id):
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f"{TRONACCS_BASE_URL}/accounts/{tronaccs_id}",
                headers={"Authorization": f"Bearer {TRONACCS_API_KEY}"}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("status")
                return None
        except Exception as e:
            logger.error(f"Status check exception: {e}")
            return None


# ═══════════════════════════════════════════════
#              ВЫПЛАТЫ ЧЕРЕЗ @send (чек)
# ═══════════════════════════════════════════════

async def create_check(amount=PAYOUT_AMOUNT_RUB):
    """
    Создаёт чек в @send на сумму amount руб.
    Продавец активирует его сам — никакой адрес не нужен.
    Возвращает (check_id, bot_link) или (None, None).
    """
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                f"{SEND_API_URL}/createCheck",
                json={
                    "asset": "RUB",
                    "amount": str(amount),
                },
                headers={"Crypto-Pay-API-Token": SEND_BOT_TOKEN}
            ) as resp:
                data = await resp.json()
                if data.get("ok"):
                    result = data["result"]
                    return result.get("check_id"), result.get("bot_check_url")
                logger.error(f"Create check error: {data}")
                return None, None
        except Exception as e:
            logger.error(f"Create check exception: {e}")
            return None, None


# ═══════════════════════════════════════════════
#           ПЛАНИРОВЩИК (проверка продаж)
# ═══════════════════════════════════════════════

async def check_sold_accounts(bot: Bot):
    accounts = await get_uploaded_accounts()
    if not accounts:
        return

    logger.info(f"Checking {len(accounts)} accounts...")

    for account_id, seller_tg_id, tronaccs_id in accounts:
        if not tronaccs_id:
            continue

        status = await check_account_status(tronaccs_id)
        if status != "sold":
            continue

        logger.info(f"Account {account_id} SOLD!")
        await mark_account_sold(account_id)

        # Уведомляем продавца
        try:
            await bot.send_message(
                seller_tg_id,
                f"🎉 Ваш аккаунт продан!\n"
                f"💸 Выплата {PAYOUT_AMOUNT_RUB} руб. будет отправлена в ближайшее время."
            )
        except Exception as e:
            logger.error(f"Notify seller error: {e}")

        # Уведомляем админа с кнопками
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"💸 Выплатить {PAYOUT_AMOUNT_RUB} руб.",
                callback_data=f"payout:{account_id}:{seller_tg_id}"
            ),
            InlineKeyboardButton(
                text="❌ Отклонить",
                callback_data=f"reject_payout:{account_id}:{seller_tg_id}"
            )
        ]])

        try:
            await bot.send_message(
                ADMIN_ID,
                f"💰 Аккаунт продан!\n\n"
                f"🆔 Account ID: {account_id}\n"
                f"👤 Продавец: {seller_tg_id}\n"
                f"💵 Сумма: {PAYOUT_AMOUNT_RUB} руб.",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Notify admin error: {e}")


# ═══════════════════════════════════════════════
#                    ХЭНДЛЕРЫ
# ═══════════════════════════════════════════════

router = Router()


class SubmitAccount(StatesGroup):
    waiting_file = State()


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Бот для продажи Telegram аккаунтов.\n\n"
        "📦 Принимаем tdata (zip-архив)\n\n"
        f"💸 За каждый проданный аккаунт — {PAYOUT_AMOUNT_RUB} руб. чеком через @send\n\n"
        "/submit — сдать аккаунт"
    )


@router.message(Command("submit"))
async def cmd_submit(message: Message, state: FSMContext):
    await state.set_state(SubmitAccount.waiting_file)
    await message.answer("📎 Прикрепи .zip архив с папкой tdata:")


@router.message(SubmitAccount.waiting_file, F.document)
async def process_file(message: Message, state: FSMContext, bot: Bot):
    doc: Document = message.document

    if not doc.file_name.endswith(".zip"):
        await message.answer("❌ Нужен .zip архив с tdata")
        return

    await message.answer("⏳ Проверяю архив...")
    file_path = os.path.join(UPLOAD_DIR, f"{message.from_user.id}_{doc.file_name}")
    await bot.download(doc, destination=file_path)

    # ── ВЕРИФИКАЦИЯ TDATA ──
    valid, reason = verify_tdata_zip(file_path)
    if not valid:
        os.remove(file_path)
        await message.answer(
            f"❌ Архив не прошёл проверку: {reason}\n\n"
            "Убедись что:\n"
            "• Архив содержит папку tdata\n"
            "• Внутри есть файлы key_datas и D877F783D5D3EF8C\n"
            "• Архив не повреждён\n\n"
            "Попробуй ещё раз /submit"
        )
        await state.clear()
        return

    account_id = await add_account(message.from_user.id, file_path)
    result = await upload_account(file_path)

    if result:
        tronaccs_id = str(result.get("id", ""))
        await update_account_verified(account_id, tronaccs_id)
        await message.answer(
            f"✅ Аккаунт прошёл проверку и выставлен на продажу!\n"
            f"🆔 ID: {tronaccs_id}\n\n"
            f"💸 Получишь {PAYOUT_AMOUNT_RUB} руб. после продажи.\n\n"
            "/submit — сдать ещё"
        )
    else:
        await message.answer("❌ Ошибка загрузки на маркет. Обратись к администратору.")

    await state.clear()


@router.message(SubmitAccount.waiting_file)
async def process_file_wrong(message: Message):
    await message.answer("📎 Пожалуйста, отправь .zip архив с tdata:")


# ── КНОПКИ ВЫПЛАТЫ (только для админа) ───────────────────────────────────────

@router.callback_query(F.data.startswith("payout:"), F.from_user.id == ADMIN_ID)
async def callback_payout(callback: CallbackQuery, bot: Bot):
    _, account_id, seller_tg_id = callback.data.split(":")
    account_id = int(account_id)
    seller_tg_id = int(seller_tg_id)

    await callback.answer("⏳ Создаю чек...")

    check_id, check_url = await create_check()

    if check_id and check_url:
        payout_id = await add_payout(seller_tg_id, account_id, PAYOUT_AMOUNT_RUB)
        await mark_payout_done(payout_id, str(check_id))
        await mark_account_paid(account_id)
        await callback.message.edit_text(callback.message.text + f"\n\n✅ Чек создан и отправлен продавцу.")
        try:
            await bot.send_message(
                seller_tg_id,
                f"✅ Ваша выплата {PAYOUT_AMOUNT_RUB} руб. готова!\n\n"
                f"👇 Забери чек:\n{check_url}"
            )
        except Exception as e:
            logger.error(f"Notify seller error: {e}")
    else:
        await callback.message.edit_text(
            callback.message.text + "\n\n❌ Ошибка создания чека! Попробуй ещё раз."
        )


@router.callback_query(F.data.startswith("reject_payout:"), F.from_user.id == ADMIN_ID)
async def callback_reject_payout(callback: CallbackQuery, bot: Bot):
    _, account_id, seller_tg_id = callback.data.split(":")
    seller_tg_id = int(seller_tg_id)

    await callback.answer("Отклонено")
    await callback.message.edit_text(callback.message.text + "\n\n🚫 Выплата отклонена.")

    try:
        await bot.send_message(
            seller_tg_id,
            "⚠️ Выплата за аккаунт была отклонена администратором.\n"
            "Обратитесь в поддержку для уточнения."
        )
    except Exception:
        pass


# ── /pending — список ожидающих выплаты ───────────────────────────────────────

@router.message(Command("pending"), F.from_user.id == ADMIN_ID)
async def cmd_pending(message: Message):
    accounts = await get_sold_unpaid_accounts()
    if not accounts:
        await message.answer("✅ Нет аккаунтов, ожидающих выплаты.")
        return

    for account_id, seller_tg_id in accounts:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=f"💸 Выплатить {PAYOUT_AMOUNT_RUB} руб.",
                callback_data=f"payout:{account_id}:{seller_tg_id}"
            ),
            InlineKeyboardButton(
                text="❌ Отклонить",
                callback_data=f"reject_payout:{account_id}:{seller_tg_id}"
            )
        ]])

        await message.answer(
            f"💰 Account ID: {account_id}\n"
            f"👤 Продавец: {seller_tg_id}",
            reply_markup=keyboard
        )


# ── /stats ─────────────────────────────────────────────────────────────────────

@router.message(Command("stats"), F.from_user.id == ADMIN_ID)
async def cmd_stats(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM accounts") as c:
            total = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM accounts WHERE status='uploaded'") as c:
            on_sale = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM accounts WHERE status='sold'") as c:
            sold = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM accounts WHERE status='paid'") as c:
            paid = (await c.fetchone())[0]

    await message.answer(
        f"📊 Статистика:\n\n"
        f"📦 Всего аккаунтов: {total}\n"
        f"🛒 На продаже: {on_sale}\n"
        f"💰 Продано (ждут выплаты): {sold}\n"
        f"✅ Выплачено: {paid}"
    )


# ═══════════════════════════════════════════════
#                    ЗАПУСК
# ═══════════════════════════════════════════════

async def main():
    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_sold_accounts, "interval", minutes=5, args=[bot])
    scheduler.start()
    logger.info("Bot started!")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
