# triangle_bybit_testnet_bot.py — торговый бот для тестовой сети Bybit (исправленная версия)
import ccxt.async_support as ccxt
import asyncio
import os
import hashlib
import time
from datetime import datetime
from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import Application

# === Конфигурация тестовой сети ===
TESTNET_MODE = True
DEBUG_MODE = True
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
telegram_app = Application.builder().token(TELEGRAM_TOKEN).build()

# === Параметры для тестовой сети ===
COMMISSION_RATE = 0.001  # 0.1%
MIN_PROFIT = 0.01  # Низкий порог для тестирования
MAX_PROFIT = 5.0  # %
START_COINS = ['USDT', 'BTC', 'ETH']
TARGET_VOLUME_USDT = 10  # Меньший объем для тестов
LOG_FILE = "testnet_trades.csv"
TRIANGLE_CACHE = {}
TRIANGLE_HOLD_TIME = 5  # seconds
MAX_SLIPPAGE = 0.01  # 1% для тестов
MAX_RETRIES = 2
RETRY_DELAY = 1

# === Инициализация тестовой сети Bybit ===
exchange = ccxt.bybit({
    "enableRateLimit": True,
    "apiKey": os.getenv("BYBIT_TESTNET_API_KEY"),
    "secret": os.getenv("BYBIT_TESTNET_API_SECRET"),
    "options": {
        "defaultType": "spot",
    },
    "urls": {
        "api": {
            "public": "https://api-testnet.bybit.com",
            "private": "https://api-testnet.bybit.com",
        }
    }
})

# Инициализация файла лога
if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, "w") as f:
        f.write("timestamp,route,profit_percent,volume_usdt,status,details\n")

async def load_symbols():
    markets = await exchange.load_markets()
    return list(markets.keys()), markets

async def find_triangles(symbols):
    triangles = []
    for base in START_COINS:
        for sym1 in symbols:
            if not sym1.endswith('/' + base): continue
            mid1 = sym1.split('/')[0]
            for sym2 in symbols:
                if not sym2.startswith(mid1 + '/'): continue
                mid2 = sym2.split('/')[1]
                third = f"{mid2}/{base}"
                if third in symbols or f"{base}/{mid2}" in symbols:
                    triangles.append((base, mid1, mid2))
    return triangles

async def get_avg_price(orderbook_side, target_usdt):
    total_base = 0
    total_usd = 0
    max_liquidity = 0
    for price, volume in orderbook_side:
        price = float(price)
        volume = float(volume)
        usd = price * volume
        max_liquidity += usd
        if total_usd + usd >= target_usdt:
            remain_usd = target_usdt - total_usd
            total_base += remain_usd / price
            total_usd += remain_usd
            break
        else:
            total_base += volume
            total_usd += usd
    if total_usd < target_usdt:
        return None, 0, max_liquidity
    avg_price = total_usd / total_base
    return avg_price, total_usd, max_liquidity

async def get_execution_price(symbol, side, target_usdt):
    try:
        orderbook = await exchange.fetch_order_book(symbol)
        if side == "buy":
            return await get_avg_price(orderbook['asks'], target_usdt)
        else:
            return await get_avg_price(orderbook['bids'], target_usdt)
    except Exception as e:
        if DEBUG_MODE:
            print(f"[Orderbook Error {symbol}]: {e}")
        return None, 0, 0

def format_line(index, pair, price, side, volume_usd, color, liquidity):
    emoji = {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(color, "")
    return f"{emoji} {index}. {pair} - {price:.6f} ({side}), исполнено ${volume_usd:.2f}, доступно ${liquidity:.2f}"

async def send_telegram_message(text):
    try:
        await telegram_app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID, 
            text=text, 
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
    except Exception as e:
        if DEBUG_MODE:
            print(f"[Telegram Error]: {e}")

def log_trade(base, mid1, mid2, profit, volume, status, details=""):
    with open(LOG_FILE, "a") as f:
        route = f"{base}->{mid1}->{mid2}->{base}"
        f.write(f"{datetime.utcnow()},{route},{profit:.4f},{volume},{status},{details}\n")

async def fetch_balances():
    try:
        balances = await exchange.fetch_balance()
        return {k: float(v) for k, v in balances["total"].items() if float(v) > 0}
    except Exception as e:
        if DEBUG_MODE:
            print(f"[Balance Error]: {e}")
        return {}

async def execute_real_trade(route_id, steps):
    """Выполняет реальные торговые операции в тестовой сети"""
    if TESTNET_MODE:
        # В тестовом режиме не исполняем реальные сделки
        test_msg = [
            "🧪 <b>ТЕСТОВАЯ СДЕЛКА</b>",
            f"Маршрут: {route_id}",
            "Действия:"
        ]
        
        for i, (symbol, side, amount) in enumerate(steps):
            test_msg.append(f"{i+1}. {symbol} {side.upper()} {amount:.6f}")
            
        test_msg.append("\n⚠️ <i>В тестовом режиме сделка не исполняется</i>")
        await send_telegram_message("\n".join(test_msg))
        return True, "Test trade simulated"
    
    try:
        results = []
        for i, (symbol, side, amount) in enumerate(steps):
            # Создаем рыночный ордер
            order = await exchange.create_order(
                symbol=symbol,
                type='market',
                side=side,
                amount=amount
            )
            results.append(order)
            
            # Задержка между ордерами
            if i < len(steps) - 1:
                await asyncio.sleep(0.5)
                
        return True, results
    except Exception as e:
        return False, str(e)

async def check_triangle(base, mid1, mid2, symbols, markets):
    try:
        s1 = f"{mid1}/{base}" if f"{mid1}/{base}" in symbols else f"{base}/{mid1}"
        s2 = f"{mid2}/{mid1}" if f"{mid2}/{mid1}" in symbols else f"{mid1}/{mid2}"
        s3 = f"{mid2}/{base}" if f"{mid2}/{base}" in symbols else f"{base}/{mid2}"

        price1, vol1, liq1 = await get_execution_price(s1, "buy" if f"{mid1}/{base}" in symbols else "sell", TARGET_VOLUME_USDT)
        if not price1: return
        step1 = (1 / price1 if f"{mid1}/{base}" in symbols else price1) * (1 - COMMISSION_RATE)
        side1 = "ASK" if f"{mid1}/{base}" in symbols else "BID"

        price2, vol2, liq2 = await get_execution_price(s2, "buy" if f"{mid2}/{mid1}" in symbols else "sell", TARGET_VOLUME_USDT)
        if not price2: return
        step2 = (1 / price2 if f"{mid2}/{mid1}" in symbols else price2) * (1 - COMMISSION_RATE)
        side2 = "ASK" if f"{mid2}/{mid1}" in symbols else "BID"

        price3, vol3, liq3 = await get_execution_price(s3, "sell" if f"{mid2}/{base}" in symbols else "buy", TARGET_VOLUME_USDT)
        if not price3: return
        step3 = (price3 if f"{mid2}/{base}" in symbols else 1 / price3) * (1 - COMMISSION_RATE)
        side3 = "BID" if f"{mid2}/{base}" in symbols else "ASK"

        result = step1 * step2 * step3
        profit_percent = (result - 1) * 100
        if not (MIN_PROFIT <= profit_percent <= MAX_PROFIT): 
            return

        route_id = f"{base}->{mid1}->{mid2}->{base}"
        route_hash = hashlib.md5(route_id.encode()).hexdigest()
        now = datetime.utcnow()
        prev_time = TRIANGLE_CACHE.get(route_hash)
        
        if prev_time and (now - prev_time).total_seconds() >= TRIANGLE_HOLD_TIME:
            execute = True
        else:
            TRIANGLE_CACHE[route_hash] = now
            execute = False

        min_liquidity = round(min(liq1, liq2, liq3), 2)
        pure_profit_usdt = round((result - 1) * TARGET_VOLUME_USDT, 2)

        message_lines = [
            f"🔁 <b>ТЕСТ: Арбитражная возможность</b>",
            format_line(1, s1, price1, side1, vol1, "green", liq1),
            format_line(2, s2, price2, side2, vol2, "yellow", liq2),
            format_line(3, s3, price3, side3, vol3, "red", liq3),
            "",
            f"💰 <b>Чистая прибыль:</b> {pure_profit_usdt:.2f} USDT",
            f"📈 <b>Спред:</b> {profit_percent:.2f}%",
            f"💧 <b>Мин. ликвидность:</b> ${min_liquidity:.2f}",
            f"⚙️ <b>Готов к сделке:</b> {'ДА' if execute else 'НЕТ'}"
        ]

        if DEBUG_MODE:
            print("\n".join(message_lines))

        await send_telegram_message("\n".join(message_lines))
        log_trade(base, mid1, mid2, profit_percent, min_liquidity, "detected")

        if execute:
            # В тестовом режиме пропускаем проверку баланса
            steps = []
            expected_prices = []
            
            # Шаг 1: base -> mid1
            if f"{mid1}/{base}" in symbols:
                steps.append((s1, "buy", TARGET_VOLUME_USDT))
            else:
                steps.append((s1, "sell", TARGET_VOLUME_USDT))
            expected_prices.append(price1)
            
            # Шаг 2: mid1 -> mid2
            amount_mid1 = TARGET_VOLUME_USDT / price1 * (1 - COMMISSION_RATE) if f"{mid1}/{base}" in symbols else TARGET_VOLUME_USDT * price1 * (1 - COMMISSION_RATE)
            if f"{mid2}/{mid1}" in symbols:
                steps.append((s2, "buy", amount_mid1))
            else:
                steps.append((s2, "sell", amount_mid1))
            expected_prices.append(price2)
            
            # Шаг 3: mid2 -> base
            amount_mid2 = amount_mid1 / price2 * (1 - COMMISSION_RATE) if f"{mid2}/{mid1}" in symbols else amount_mid1 * price2 * (1 - COMMISSION_RATE)
            if f"{mid2}/{base}" in symbols:
                steps.append((s3, "sell", amount_mid2))
            else:
                steps.append((s3, "buy", amount_mid2))
            expected_prices.append(price3)
            
            trade_success, trade_result = await execute_real_trade(route_id, steps)
            
            if trade_success:
                msg = [
                    f"✅ <b>ТЕСТ: Сделка симулирована</b>",
                    f"Маршрут: {route_id}",
                    f"Ожидаемая прибыль: {profit_percent:.2f}%",
                    f"Сумма прибыли: {pure_profit_usdt:.2f} USDT",
                    f"<i>В тестовом режиме реальные ордера не создаются</i>"
                ]
                await send_telegram_message("\n".join(msg))
                log_trade(base, mid1, mid2, profit_percent, TARGET_VOLUME_USDT, "simulated")
    except Exception as e:
        error_msg = f"⚠️ <b>Ошибка при обработке треугольника</b>\n{str(e)}"
        await send_telegram_message(error_msg)
        if DEBUG_MODE:
            import traceback
            traceback.print_exc()

async def send_balance_update():
    """Отправляет текущий баланс в Telegram"""
    try:
        balances = await fetch_balances()
        if not balances:
            return
            
        msg = ["💰 <b>ТЕСТОВЫЙ БАЛАНС:</b>"]
        for coin, amount in balances.items():
            if amount > 0.0001:  # Фильтр малых сумм
                msg.append(f"{coin}: {amount:.6f}")
        
        msg.append("\n⚙️ Для пополнения используйте Bybit Testnet Faucet")
        await send_telegram_message("\n".join(msg))
    except Exception as e:
        if DEBUG_MODE:
            print(f"[Balance Update Error]: {e}")

async def check_exchange_connection():
    """Проверяет подключение к бирже"""
    try:
        # Проверяем время сервера как индикатор доступности
        server_time = await exchange.fetch_time()
        if DEBUG_MODE:
            print(f"Серверное время Bybit: {server_time}")
        return True
    except Exception as e:
        error_msg = f"❌ <b>Ошибка подключения к Bybit</b>\n{str(e)}"
        await send_telegram_message(error_msg)
        if DEBUG_MODE:
            import traceback
            traceback.print_exc()
        return False

async def main():
    try:
        # Проверка подключения к бирже
        connected = await check_exchange_connection()
        if not connected:
            return
            
        await send_telegram_message("🤖 <b>Бот для тестовой сети Bybit запущен</b>")
        
        symbols, markets = await load_symbols()
        triangles = await find_triangles(symbols)
        
        if DEBUG_MODE:
            print(f"🔁 Найдено маршрутов: {len(triangles)}")
            await send_telegram_message(f"🔍 Найдено треугольников: {len(triangles)}")

        await telegram_app.initialize()
        await telegram_app.start()

        last_balance_update = time.time()
        
        while True:
            tasks = [check_triangle(base, mid1, mid2, symbols, markets) 
                     for base, mid1, mid2 in triangles]
            await asyncio.gather(*tasks)
            
            # Отправляем баланс каждый час
            current_time = time.time()
            if current_time - last_balance_update > 3600:
                await send_balance_update()
                last_balance_update = current_time
                
            await asyncio.sleep(10)
            
    except KeyboardInterrupt:
        print("Остановка...")
    except Exception as e:
        error_msg = f"🚨 <b>Критическая ошибка</b>\n{str(e)}"
        await send_telegram_message(error_msg)
        if DEBUG_MODE:
            import traceback
            traceback.print_exc()
    finally:
        try:
            await exchange.close()
            await telegram_app.stop()
            await telegram_app.shutdown()
        except:
            pass

if __name__ == '__main__':
    asyncio.run(main())