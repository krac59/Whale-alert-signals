import asyncio
import aiohttp
import logging
import json
import os
import re
from datetime import datetime, timezone, timedelta
from io import BytesIO

from dotenv import load_dotenv
load_dotenv()

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    CallbackQueryHandler, filters
)
from google.oauth2.service_account import Credentials
import gspread
import qrcode

# === НАСТРОЙКИ ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("❌ TELEGRAM_BOT_TOKEN не задан! Укажите его в файле .env.")

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "1IyLZ5kopVWzA7vpvkcdDXXyBw3M9paR0IOARuKVAmLo")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "383302760"))

NICEGRAM_ID = "6939917410"
TAXOBOT_USERNAME = "@taxobot"
YOOMONEY_WALLET = "41001203402135"

# === КОШЕЛЬКИ ===
PAYMENT_ADDRESSES = {
    "ton": "UQDhNEz5ZySFjfQxLM1l_RXScRC1rM3Y2cNLLyZkYRXKfK9X",
    "eth": "0x438d601f248Bceb0f387a9f8dE6b4C3E5D53aFF1",
    "sol": "7HnwUaAj7tekiaXCiDWExzEShUDucqa3HcxExkPdVq2y",
    "doge": "DAxvB5ruRX8oE9rk5UWWRB18XwJyb8FVbj",
}

ASSET_TO_CHAIN = {
    "TON": "ton", "NOT": "ton",
    "ETH": "eth", "USDT": "eth", "USDC": "eth",
    "SOL": "sol",
    "DOGE": "doge",
    "BTC": "ton", "XRP": "ton", "TRX": "ton", "MNT": "ton",
}

NETWORK_NAMES = {
    "ton": "TON",
    "eth": "Ethereum",
    "sol": "Solana",
    "doge": "Dogecoin",
}

# === КУРСЫ STARS ===
STARS_TO_RUB = 1.75  # 100 ⭐ = 175 RUB
STARS_TO_USD = 1.75 / 93.5

# === ВАЛЮТЫ ===
FIAT_CURRENCIES = ["USD", "EUR", "RUB", "BYN", "KZT", "CNY", "JPY", "GBP"]
CRYPTO_ASSETS = ["BTC", "ETH", "TON", "SOL", "DOGE", "XRP", "USDT", "USDC", "MNT", "TRX"]
ALL_ASSETS = FIAT_CURRENCIES + CRYPTO_ASSETS

# === ГЛОБАЛЬНЫЕ ДАННЫЕ ===
bot = Bot(token=TELEGRAM_BOT_TOKEN)
subs_db = {}
p2p_subs_db = {}
user_memos = {}
p2p_usage = {}
exchange_orders = {}

# === GOOGLE SHEETS ===
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
sheet = p2p_sheet = None
try:
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1
    try:
        p2p_sheet = gc.open_by_key(GOOGLE_SHEET_ID).worksheet("P2P")
    except:
        p2p_sheet = gc.open_by_key(GOOGLE_SHEET_ID).add_worksheet(title="P2P", rows="1000", cols="8")
        p2p_sheet.append_row(["Дата", "User ID", "От", "К", "Сумма", "Контакт", "Комиссия", "Кошелёк комиссии"])
except Exception as e:
    logging.error(f"Google Sheets error: {e}")

# === ФУНКЦИИ ===
def load_db():
    files = [("subs.json", subs_db), ("p2p_subs.json", p2p_subs_db), ("memos.json", user_memos), ("p2p_usage.json", p2p_usage)]
    for fname, var in files:
        if os.path.exists(fname):
            try:
                with open(fname, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    var.update({int(k): v for k, v in data.items()})
            except: pass

def save_db():
    files = [("subs.json", subs_db), ("p2p_subs.json", p2p_subs_db), ("memos.json", user_memos), ("p2p_usage.json", p2p_usage)]
    for fname, var in files:
        with open(fname, "w", encoding='utf-8') as f:
            json.dump(var, f)

def is_main_sub_active(user_id):
    return subs_db.get(user_id, 0) > datetime.now(timezone.utc).timestamp()

def is_p2p_active(user_id):
    return is_main_sub_active(user_id) or p2p_subs_db.get(user_id, 0) > datetime.now(timezone.utc).timestamp()

def can_create_p2p_offer(user_id):
    return is_p2p_active(user_id) or p2p_usage.get(user_id, 0) < 3

def generate_memo(user_id):
    import random
    return f"WA{user_id}{random.randint(100000, 999999)}"

async def send_waiting(chat_id, bot):
    return await bot.send_message(chat_id, "⏳ Расчёт курса...")

async def _get_fiat_rate(from_curr: str, to_curr: str) -> float:
    if from_curr == to_curr:
        return 1.0
    try:
        url = f"https://api.exchangerate-api.com/v4/latest/{from_curr}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["rates"].get(to_curr, 1.0)
    except:
        return 1.0

async def _get_crypto_rate(asset: str) -> float:
    asset_map = {
        "BTC": "bitcoin", "ETH": "ethereum", "TON": "the-open-network",
        "SOL": "solana", "DOGE": "dogecoin", "XRP": "ripple",
        "USDT": "tether", "USDC": "usd-coin", "MNT": "mantle", "TRX": "tron"
    }
    asset_id = asset_map.get(asset.upper(), "bitcoin")
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={asset_id}&vs_currencies=usd"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get(asset_id, {}).get("usd", 1.0)
    except:
        return 1.0

async def get_exchange_rate(from_asset: str, to_asset: str) -> float:
    if from_asset == "STARS":
        if to_asset == "RUB":
            return STARS_TO_RUB
        elif to_asset == "USD":
            return STARS_TO_USD
        else:
            usd_fiat = await _get_fiat_rate("USD", to_asset)
            return STARS_TO_USD * usd_fiat
    if to_asset == "STARS":
        if from_asset == "RUB":
            return 1 / STARS_TO_RUB
        elif from_asset == "USD":
            return 1 / STARS_TO_USD
        else:
            fiat_usd = await _get_fiat_rate(from_asset, "USD")
            return fiat_usd / STARS_TO_USD
    if from_asset in FIAT_CURRENCIES and to_asset in FIAT_CURRENCIES:
        return await _get_fiat_rate(from_asset, to_asset)
    if from_asset in CRYPTO_ASSETS and to_asset in FIAT_CURRENCIES:
        crypto_usd = await _get_crypto_rate(from_asset)
        if to_asset == "USD":
            return crypto_usd
        usd_fiat = await _get_fiat_rate("USD", to_asset)
        return crypto_usd * usd_fiat
    if from_asset in FIAT_CURRENCIES and to_asset in CRYPTO_ASSETS:
        fiat_usd = 1 / await _get_fiat_rate(from_asset, "USD") if from_asset != "USD" else 1.0
        crypto_usd = await _get_crypto_rate(to_asset)
        return fiat_usd / crypto_usd if crypto_usd else 0.0
    if from_asset in CRYPTO_ASSETS and to_asset in CRYPTO_ASSETS:
        from_usd = await _get_crypto_rate(from_asset)
        to_usd = await _get_crypto_rate(to_asset)
        return from_usd / to_usd if to_usd else 1.0
    return 1.0

def get_min_amount(asset: str) -> str:
    if asset == "RUB":
        return "500 (кратно 10)"
    elif asset in FIAT_CURRENCIES:
        return "10 (кратно 10)"
    else:
        return "0.01"

def calculate_receive_amount(give: str, give_amt: float, receive: str) -> (float, float):
    if give == "RUB":
        usd_eq = give_amt / 93.5
        has_fee = give_amt >= 500
    elif give in FIAT_CURRENCIES:
        usd_eq = give_amt
        has_fee = give_amt >= 10
    else:
        usd_eq = give_amt * {"BTC":60000, "ETH":3000, "TON":5.5, "SOL":150}.get(give, 1)
        has_fee = usd_eq >= 10
    if has_fee:
        fee = give_amt * 0.0001
        return round(give_amt - fee, 8), round(fee, 8)
    else:
        return give_amt, 0

def get_similar_offers(give: str, receive: str):
    offers = []
    for uid, orders in exchange_orders.items():
        for o in orders[-5:]:
            if o["from_coin"] == give and o["to_coin"] == receive:
                offers.append({
                    "give_amt": o["amount"],
                    "give": o["from_coin"],
                    "receive_amt": o["final_amount"],
                    "receive": o["to_coin"],
                    "contact": o["contact"]
                })
    return offers[:3]

# === КОМАНДЫ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_new = user_id not in subs_db and user_id not in user_memos
    if is_new:
        subs_db[user_id] = (datetime.now(timezone.utc) + timedelta(days=1)).timestamp()
        save_db()
        trial = "🎁 <b>Добро пожаловать!</b> Пробный день активирован!\n\n"
    else:
        trial = ""
    msg = (
        f"{trial}"
        "🐋 <b>Whale Alert Premium</b>\n\n"
        "🔥 <b>Почему выбирают нас?</b>\n"
        "✅ Сигналы о входах на биржи (Binance, Coinbase)\n"
        "✅ Перемещения от $500K+\n"
        "✅ Данные из Ethereum, TON, Solana и др.\n"
        "✅ Все сделки — в Google Таблице\n\n"
        "🔁 <b>P2P-обмен</b>\n"
        "• Первые 3 заявки — бесплатно\n"
        "• Комиссия 0.01% от $10\n"
        "• Поддержка всех пар: фиат ↔ крипта\n\n"
        "💎 <b>100 ⭐ = 175 RUB</b>"
    )
    kb = [
        [InlineKeyboardButton("💎 Оплатить", callback_data="pay_main")],
        [InlineKeyboardButton("🔁 P2P", callback_data="p2p_main")],
        [InlineKeyboardButton("📄 Помощь", callback_data="help_main")]
    ]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

async def pay_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = [
        [InlineKeyboardButton("🐋 Основная", callback_data="paytype_main")],
        [InlineKeyboardButton("🔁 Только P2P", callback_data="paytype_p2p")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_to_start")]
    ]
    await query.message.edit_text("Выберите тип:", reply_markup=InlineKeyboardMarkup(kb))

async def handle_paytype(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pt = query.data.replace("paytype_", "")
    kb = []
    plans = [(3,500),(7,1000),(14,1800),(30,3500)] if pt == "main" else [("30d",30,"Пробный"),("90d",100,"3 мес"),("180d",280,"6 мес")]
    for item in plans:
        if pt == "main":
            days, stars = item
            rub = stars * STARS_TO_RUB
            kb.append([InlineKeyboardButton(f"{days} дн. — {stars} ⭐ ({rub:.0f} RUB)", callback_data=f"plan_{pt}_{days}")])
        else:
            pid, stars, desc = item
            rub = stars * STARS_TO_RUB
            kb.append([InlineKeyboardButton(f"{desc} — {stars} ⭐ ({rub:.0f} RUB)", callback_data=f"plan_{pt}_{pid}")])
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="pay_main")])
    await query.message.edit_text("Выберите тариф:", reply_markup=InlineKeyboardMarkup(kb))

async def handle_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, typ, plan_id = query.data.split("_")
    user_id = update.effective_user.id
    if user_id not in user_memos:
        user_memos[user_id] = generate_memo(user_id)
        save_db()
    context.user_data["pay_data"] = {"type": typ, "plan": plan_id}
    kb = [
        [InlineKeyboardButton("⭐ Stars (100 ⭐ = 175 RUB)", callback_data="paymethod_stars")],
        [InlineKeyboardButton("💎 Крипта", callback_data="paymethod_crypto")],
        [InlineKeyboardButton("💰 Фиат", callback_data="paymethod_fiat")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"paytype_{typ}")]
    ]
    await query.message.edit_text("Способ оплаты:", reply_markup=InlineKeyboardMarkup(kb))

async def handle_paymethod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    method = query.data.replace("paymethod_", "")
    user_id = update.effective_user.id
    if method == "stars":
        await query.message.edit_text(f"⭐ Переведите Stars на:\n• Бот: {TAXOBOT_USERNAME}\n• ID: {NICEGRAM_ID}", parse_mode="HTML")
    elif method == "fiat":
        memo = user_memos[user_id]
        await query.message.edit_text(f"💰 Реквизиты: <code>{YOOMONEY_WALLET}</code>\nКомментарий: <code>{memo}</code>", parse_mode="HTML")
    elif method == "crypto":
        assets = ["TON", "ETH", "SOL", "DOGE", "USDT"]
        kb = [[InlineKeyboardButton(a, callback_data=f"payasset_{a}") for a in assets[i:i+2]] for i in range(0, len(assets), 2)]
        kb.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"plan_{context.user_data['pay_data']['type']}_{context.user_data['pay_data']['plan']}")])
        await query.message.edit_text("Выберите актив:", reply_markup=InlineKeyboardMarkup(kb))

async def select_pay_asset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    asset = query.data.replace("payasset_", "")
    user_id = update.effective_user.id
    memo = user_memos[user_id]
    address = PAYMENT_ADDRESSES.get(ASSET_TO_CHAIN.get(asset.upper(), "ton"), PAYMENT_ADDRESSES["ton"])
    network = NETWORK_NAMES.get(ASSET_TO_CHAIN.get(asset.upper(), "ton"), "Unknown")
    waiting = await send_waiting(query.message.chat_id, context.bot)
    try:
        qr = qrcode.QRCode(box_size=5, border=2)
        url = f"ton://transfer/{address}?text={memo}" if asset == "TON" else address
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = BytesIO()
        img.save(buf, format="PNG")
        caption = f"📄 Актив: {asset}\n🌐 Сеть: {network}\n📍 Адрес: <code>{address}</code>\n📎 MEMO: <code>{memo}</code>"
        await context.bot.send_photo(chat_id=query.message.chat_id, photo=buf.getvalue(), caption=caption, parse_mode="HTML")
    finally:
        await waiting.delete()

# === P2P ===
async def p2p_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    if not can_create_p2p_offer(user_id):
        await query.message.reply_text("Доступ закрыт. Оплатите: /pay")
        return
    context.user_data["p2p_step"] = "give_asset"
    kb = [[InlineKeyboardButton(a, callback_data=f"p2p_give_{a}") for a in ALL_ASSETS[i:i+3]] for i in range(0, len(ALL_ASSETS), 3)]
    await query.message.edit_text("💱 Отдам:", reply_markup=InlineKeyboardMarkup(kb))

async def p2p_select_give(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    asset = query.data.replace("p2p_give_", "")
    context.user_data["p2p_data"] = {"give": asset}
    context.user_data["p2p_step"] = "give_amount"
    await query.message.edit_text(f"💰 Сумма (минимум: {get_min_amount(asset)}):")

async def p2p_enter_give_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("p2p_step") != "give_amount":
        return
    asset = context.user_data["p2p_data"]["give"]
    try:
        amount = float(update.message.text)
        if asset == "RUB":
            if amount < 500 or amount % 10 != 0:
                raise ValueError
        elif asset in FIAT_CURRENCIES:
            if amount < 10 or amount % 10 != 0:
                raise ValueError
        else:
            if amount <= 0:
                raise ValueError
        context.user_data["p2p_data"]["give_amount"] = amount
        context.user_data["p2p_step"] = "receive_asset"
        kb = [[InlineKeyboardButton(a, callback_data=f"p2p_recv_{a}") for a in ALL_ASSETS[i:i+3]] for i in range(0, len(ALL_ASSETS), 3)]
        await update.message.reply_text("💱 Получу:", reply_markup=InlineKeyboardMarkup(kb))
    except:
        await update.message.reply_text(f"Некорректная сумма. Минимум: {get_min_amount(asset)}")

async def p2p_select_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    asset = query.data.replace("p2p_recv_", "")
    data = context.user_data["p2p_data"]
    data["receive"] = asset
    waiting = await send_waiting(query.message.chat_id, context.bot)
    try:
        rate = await get_exchange_rate(data["give"], asset)
        give_amt = data["give_amount"]
        receive_amt = give_amt * rate
        _, fee = calculate_receive_amount(data["give"], give_amt, asset)
        final_amt = receive_amt - fee
        data["receive_amount"] = final_amt
        data["fee"] = fee
        similar = get_similar_offers(data["give"], asset)
        msg = f"✅ Расчёт:\nОтдам: {give_amt} {data['give']}\nПолучу: {final_amt:.6f} {asset}\n"
        if fee > 0:
            msg += f"Комиссия: {fee:.6f} {asset} (0.01%)\n"
        else:
            msg += "Комиссия: отсутствует\n\n"
        if similar:
            msg += "📋 Аналогичные:\n"
            for o in similar:
                msg += f"• {o['give_amt']} {o['give']} → {o['receive_amt']:.2f} {o['receive']} ({o['contact']})\n"
        msg += "\n📤 Отправить?"
        kb = [[InlineKeyboardButton("📤 Отправить", callback_data="p2p_publish")]]
        await query.message.edit_text(msg, reply_markup=InlineKeyboardMarkup(kb))
    finally:
        await waiting.delete()

# ✅ КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ: правильная проверка
async def p2p_publish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    p2p_data = context.user_data.get("p2p_data")
    if p2p_data is None:  # ← ЭТО ПРАВИЛЬНО
        await query.message.edit_text("❌ Данные формы утеряны.")
        return

    give = p2p_data.get("give")
    give_amount = p2p_data.get("give_amount")
    receive = p2p_data.get("receive")
    receive_amount = p2p_data.get("receive_amount", 0.0)
    fee = p2p_data.get("fee", 0.0)

    if not give or not receive or give_amount is None:
        await query.message.edit_text("❌ Неполные данные.")
        return

    p2p_usage[user_id] = p2p_usage.get(user_id, 0) + 1
    order = {
        "id": p2p_usage[user_id],
        "from_coin": give,
        "to_coin": receive,
        "amount": give_amount,
        "final_amount": receive_amount,
        "fee": fee,
        "contact": f"@{update.effective_user.username}" if update.effective_user.username else f"user{user_id}",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    if user_id not in exchange_orders:
        exchange_orders[user_id] = []
    exchange_orders[user_id].append(order)
    if p2p_sheet:
        try:
            p2p_sheet.append_row([
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                str(user_id),
                order["from_coin"],
                order["to_coin"],
                str(order["amount"]),
                order["contact"],
                str(order["fee"]),
                f"Nicegram ID: {NICEGRAM_ID}"
            ])
        except: pass
    await query.message.edit_text("✅ Заявка опубликована!")
    context.user_data.pop("p2p_data", None)

async def help_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.message.edit_text("📄 Помощь:\n• /pay\n• /p2p\n• /my_offers")

async def my_offers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    orders = exchange_orders.get(uid, [])
    if not orders:
        await update.message.reply_text("Нет заявок.")
        return
    msg = "📋 Ваши заявки:\n\n"
    for o in orders[-3:]:
        msg += f"#{o['id']} | {o['from_coin']} → {o['to_coin']}\n"
    await update.message.reply_text(msg)

# === ЗАПУСК ===
async def main():
    logging.basicConfig(level=logging.INFO)
    load_db()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pay", lambda u, c: pay_main(u, c)))
    app.add_handler(CommandHandler("p2p", lambda u, c: p2p_main(u, c)))
    app.add_handler(CommandHandler("my_offers", my_offers))
    app.add_handler(CallbackQueryHandler(pay_main, pattern="^pay_main$"))
    app.add_handler(CallbackQueryHandler(p2p_main, pattern="^p2p_main$"))
    app.add_handler(CallbackQueryHandler(help_main, pattern="^help_main$"))
    app.add_handler(CallbackQueryHandler(handle_paytype, pattern="^paytype_"))
    app.add_handler(CallbackQueryHandler(handle_plan, pattern="^plan_"))
    app.add_handler(CallbackQueryHandler(handle_paymethod, pattern="^paymethod_"))
    app.add_handler(CallbackQueryHandler(select_pay_asset, pattern="^payasset_"))
    app.add_handler(CallbackQueryHandler(p2p_select_give, pattern="^p2p_give_"))
    app.add_handler(CallbackQueryHandler(p2p_select_receive, pattern="^p2p_recv_"))
    app.add_handler(CallbackQueryHandler(p2p_publish, pattern="^p2p_publish$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, p2p_enter_give_amount))
    app.add_handler(CallbackQueryHandler(start, pattern="^back_to_start$"))
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logging.info("✅ Бот запущен!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
