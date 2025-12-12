import asyncio
import aiohttp
import logging
import json
import os
import re
from datetime import datetime, timezone, timedelta
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler, \
    PreCheckoutQueryHandler
from google.oauth2.service_account import Credentials
import gspread

# === НАСТРОЙКИ ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8596237465:AAFnMQCXP4j8O-ItSu219N4EsopjFPIeJBo")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID","1IyLZ5kopVWzA7vpvkcdDXXyBw3M9paR0IOARuKVAmLo")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "383302760"))

TON_WALLET = "UQDhNEz5ZySFjfQxLM1l_RXScRC1rM3Y2cNLLyZkYRXKfK9X"
YOOMONEY_WALLET = "41001203402135"
NICEGRAM_ID = "6939917410"
TAXOBOT_USERNAME = "@taxobot"

# === ИНИЦИАЛИЗАЦИЯ GOOGLE SHEETS ===
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
try:
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1
except Exception as e:
    logging.error(f"Google Sheets init error: {e}")
    sheet = None

bot = Bot(token=TELEGRAM_BOT_TOKEN)
subs_db = {}
user_memos = {}
exchange_orders = []


def load_db():
    for file, var in [("subs.json", subs_db), ("memos.json", user_memos)]:
        if os.path.exists(file):
            with open(file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if file == "subs.json":
                    var.update({int(k): v for k, v in data.items()})
                else:
                    var.update({int(k): v for k, v in data.items()})


def save_db():
    with open("subs.json", "w", encoding='utf-8') as f:
        json.dump(subs_db, f)
    with open("memos.json", "w", encoding='utf-8') as f:
        json.dump(user_memos, f)


def is_sub_active(user_id):
    return subs_db.get(user_id, 0) > datetime.now(timezone.utc).timestamp()


def generate_memo(user_id):
    import random
    return f"WA{user_id}{random.randint(100000, 999999)}"


# === GOOGLE ТАБЛИЦА: ФОРМАТИРОВАНИЕ ===
def setup_sheet():
    if not sheet:
        return
    try:
        sheet.format("A1:I1", {
            "backgroundColor": {"red": 0.1, "green": 0.3, "blue": 0.6},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1.0, "green": 1.0, "blue": 1.0}},
            "horizontalAlignment": "CENTER"
        })
        sheet.format("A2:A1000", {
            "numberFormat": {"type": "DATE_TIME", "pattern": "dd.mm.yyyy hh:mm:ss"},
            "horizontalAlignment": "CENTER"
        })
        sheet.format("D2:E1000", {
            "numberFormat": {"type": "CURRENCY", "pattern": "#,##0"},
            "horizontalAlignment": "RIGHT"
        })
        sheet.set_basic_filter("A1:I1000")
        logging.info("✅ Google Таблица настроена!")
    except Exception as e:
        logging.error(f"Sheet setup error: {e}")


# === GOOGLE ТАБЛИЦА: ЗАПИСЬ СДЕЛКИ ===
def get_explorer_url(blockchain, tx_hash):
    urls = {
        "bitcoin": f"https://blockchair.com/bitcoin/transaction/{tx_hash}",
        "ethereum": f"https://etherscan.io/tx/{tx_hash}",
        "binance": f"https://bscscan.com/tx/{tx_hash}",
        "tron": f"https://tronscan.org/#/transaction/{tx_hash}",
        "solana": f"https://solscan.io/tx/{tx_hash}",
    }
    return urls.get(blockchain, f"https://google.com/search?q={tx_hash}")


def log_transaction(tx):
    if not sheet:
        return
    try:
        to_owner = tx["to"]["owner"].lower()
        tx_type = "📥 Вход на биржу" if any(
            ex in to_owner for ex in ["binance", "coinbase", "kraken", "bybit", "okx"]) else "Межкошельковый"

        explorer_url = get_explorer_url(tx["blockchain"], tx["transaction_hash"])
        tx_hash_link = f'=HYPERLINK("{explorer_url}", "{tx["transaction_hash"][:12]}...")'

        row = [
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            tx["blockchain"].title(),
            tx.get("symbol", "").upper(),
            tx["amount"],
            tx["usd_value"],
            tx["from"]["owner"],
            tx["to"]["owner"],
            tx_hash_link,
            tx_type
        ]
        sheet.append_row(row, value_input_option="USER_ENTERED")
        logging.info(f"📊 Записана сделка: {tx['usd_value']:,.0 f} USD")
    except Exception as e:
        logging.error(f"Ошибка записи в таблицу: {e}")


# === УПРОЩЁННАЯ ОПЛАТА ЗВЁЗДАМИ ===
async def pay_stars(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "💎 <b>Оплата Telegram Stars</b>\n\n"
        "1. Откройте Telegram (или Nicegram)\n"
        "2. Переведите звёзды на:\n"
        f"   • Бот: {TAXOBOT_USERNAME}\n"
        f"   • Или по ID: <code>{NICEGRAM_ID}</code>\n\n"
        "<b>Тарифы:</b>\n"
        "• 3 дня — 500 ⭐\n"
        "• 7 дней — 1000 ⭐\n"
        "• 14 дней — 1800 ⭐\n"
        "• 1 месяц — 3500 ⭐\n"
        "• 3 месяца — 9000 ⭐\n"
        "• 6 месяцев — 16000 ⭐\n"
        "• 1 год — 28000 ⭐\n\n"
        "3. После оплаты пришлите скриншот администратору."
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# === КРИПТО ОПЛАТА ===
async def check_ton_payments():
    while True:
        try:
            url = f"https://toncenter.com/api/v2/getTransactions?address={TON_WALLET}&limit=20"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for tx in data.get("result", []):
                            comment = tx.get("in_msg", {}).get("message", "")
                            if not comment:
                                continue
                            match = re.search(r"WA(\d+)\d{6}", comment)
                            if match:
                                user_id = int(match.group(1))
                                if not is_sub_active(user_id):
                                    subs_db[user_id] = (datetime.now(timezone.utc) + timedelta(days=30)).timestamp()
                                    save_db()
                                    await bot.send_message(chat_id=user_id,
                                                           text="✅ Оплата получена! Подписка активна на 30 дней.")
                                    await bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"TON-платёж от {user_id}")
        except Exception as e:
            logging.error(f"TON check error: {e}")
        await asyncio.sleep(60)


async def pay_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_memos:
        user_memos[user_id] = generate_memo(user_id)
        save_db()
    memo = user_memos[user_id]
    msg = (
        f"💳 Отправьте платёж на:\n<code>{TON_WALLET}</code>\n\n"
        f"❗ Укажите комментарий:\n<code>{memo}</code>\n\n"
        "Поддерживаемые монеты:\nTON, USDT, ETH, BTC, XRP, SOL, DOGE, MNT"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# === ФИАТНАЯ ОПЛАТА ===
async def pay_fiat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    memo = f"WA{user_id}"
    msg = (
        f"💰 Переведите на ЮMoney:\n<code>{YOOMONEY_WALLET}</code>\n\n"
        f"❗ В комментарии укажите:\n<code>{memo}</code>\n\n"
        "Валюты: RUB, BYN, KZT, USD, EUR, GBP, CHF, CNY, JPY"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# === P2P ОБМЕН ===
async def exchange(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_sub_active(user_id):
        await update.message.reply_text("Требуется подписка.")
        return
    msg = (
        "🔁 <b>P2P Обмен</b>\n\n"
        "<b>Опубликовать заявку:</b>\n"
        "<code>/offer TON USD 5 18 @username</code>\n\n"
        "<b>Взять заявку в работу:</b>\n"
        "Напишите продавцу напрямую."
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_sub_active(user_id) or len(context.args) != 5:
        return
    try:
        from_coin, to_coin, amount, rate, contact = context.args
        amount = float(amount)
        rate = float(rate)
        exchange_orders.append({
            "id": len(exchange_orders) + 1,
            "user_id": user_id,
            "from_coin": from_coin.upper(),
            "to_coin": to_coin.upper(),
            "amount": amount,
            "rate": rate,
            "contact": contact,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        board = "📋 <b>Активные заявки:</b>\n\n"
        for o in exchange_orders[-3:]:
            board += f"ID: {o['id']} | {o['from_coin']} → {o['to_coin']}\n"
            board += f"{o['amount']} @ {o['rate']} ({o['contact']})\n\n"
        await update.message.reply_text(board, parse_mode="HTML")
    except:
        await update.message.reply_text("Ошибка формата.")


# === АДМИНКА ===
async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    if len(context.args) != 2:
        await update.message.reply_text("Используйте: /activate USER_ID DAYS")
        return
    try:
        user_id = int(context.args[0])
        days = int(context.args[1])
        subs_db[user_id] = (datetime.now(timezone.utc) + timedelta(days=days)).timestamp()
        save_db()
        await bot.send_message(chat_id=user_id, text=f"✅ Подписка активирована на {days} дней!")
        await update.message.reply_text(f"Готово! Пользователь {user_id} получил {days} дней.")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    msg = "🛠 <b>Админка</b>\n\n/activate USER_ID DAYS — активировать подписку"
    await update.message.reply_text(msg, parse_mode="HTML")


# === СТАРТ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in subs_db and user_id not in user_memos:
        subs_db[user_id] = (datetime.now(timezone.utc) + timedelta(days=1)).timestamp()
        save_db()
        trial_msg = "✅ <b>Пробный период на 1 день активирован!</b>\n\n"
    else:
        trial_msg = ""

    msg = (
        "🐋 <b>Whale Alert Premium</b>\n\n"
        "Получайте сигналы о крупных криптосделках в реальном времени:\n"
        "• Входы на биржи (Binance, Coinbase и др.)\n"
        "• Крупные перемещения (>500K USD)\n"
        "• Данные из 5+ блокчейнов\n"
        "• Все сигналы в Google Таблице\n\n"

        "<b>🔥 Тарифы:</b>\n"
        "• 3 дня — 500 ⭐ / 5 USD / 500 RUB / 1.35 TON\n"
        "• 7 дней — 1000 ⭐ / 10 USD / 1000 RUB / 2.7 TON\n"
        "• 14 дней — 1800 ⭐ / 18 USD / 1800 RUB / 4.9 TON\n"
        "• 1 месяц — 3500 ⭐ / 35 USD / 3500 RUB / 9.5 TON\n"
        "• 3 месяца — 9000 ⭐ / 90 USD / 9000 RUB / 24.3 TON\n"
        "• 6 месяцев — 16000 ⭐ / 160 USD / 16000 RUB / 43.2 TON\n"
        "• 1 год — 28000 ⭐ / 280 USD / 28000 RUB / 75.7 TON\n\n"

        "<b>📥 Способы оплаты:</b>\n"
        "• Telegram Stars: /pay_stars\n"
        "• Криптовалюта: /pay_crypto\n"
        "• Фиат: /pay_fiat\n\n"

        "<b>🔁 Для подписчиков:</b>\n"
        "P2P обмен: /exchange"
    )
    await update.message.reply_text(trial_msg + msg, parse_mode="HTML")


# === ДЕМО-СИГНАЛ (для теста) ===
async def demo_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        return
    tx = {
        "blockchain": "ethereum",
        "symbol": "ETH",
        "amount": 1250.0,
        "usd_value": 4_375_000,
        "from": {"owner": "0x123...abc"},
        "to": {"owner": "binance-hot-wallet"},
        "transaction_hash": "0xabc123def456...",
    }
    log_transaction(tx)
    await update.message.reply_text("✅ Демо-сделка записана в таблицу!")


# === ЗАПУСК ===
async def main():
    logging.basicConfig(level=logging.INFO)
    load_db()

    # Настройка таблицы
    if sheet:
        setup_sheet()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pay_stars", pay_stars))
    app.add_handler(CommandHandler("pay_crypto", pay_crypto))
    app.add_handler(CommandHandler("pay_fiat", pay_fiat))
    app.add_handler(CommandHandler("exchange", exchange))
    app.add_handler(CommandHandler("offer", offer))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("activate", activate))
    app.add_handler(CommandHandler("demo", demo_signal))  # Для теста

    asyncio.create_task(check_ton_payments())

    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logging.info("✅ Бот запущен!")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())