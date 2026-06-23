import os
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY", "").strip()
MASSIVE_API_BASE = "https://api.massive.com"
BINANCE_API_BASE = "https://data-api.binance.vision"

HOURLY_POINTS = 23
FINAL_POINTS = 24


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET"
    return response


def manual():
    return "Data requires manual verification."


def format_number(value, decimals=2):
    try:
        number = float(value)
        if decimals == 0:
            return str(int(round(number)))
        return f"{number:.{decimals}f}"
    except Exception:
        return manual()


def format_sequence(values, decimals=2, target_points=24):
    if not isinstance(values, list) or len(values) < target_points:
        return manual()

    selected = values[-target_points:]
    result = []

    for value in selected:
        formatted = format_number(value, decimals)
        if formatted == manual():
            return manual()
        result.append(formatted)

    return ",".join(result)


def is_flat_or_invalid_sequence(values, min_unique=3):
    """
    防止指数返回一整串完全相同的价格。
    如果最近价格点里唯一值太少，则判定为无效走势序列。
    """
    if not isinstance(values, list) or len(values) < 10:
        return True

    rounded_values = []

    for value in values:
        try:
            rounded_values.append(round(float(value), 2))
        except Exception:
            return True

    return len(set(rounded_values)) < min_unique


def utc_time_from_ms(timestamp_ms):
    try:
        return datetime.fromtimestamp(
            int(timestamp_ms) / 1000,
            timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return manual()


def get_reference_tickers(search_text, market="indices", limit=50):
    """
    从 Massive reference tickers 搜索指数 ticker。
    用于尽量找到 Nasdaq Composite，而不是 Nasdaq futures 或 Nasdaq-100。
    """
    if not MASSIVE_API_KEY:
        return []

    url = f"{MASSIVE_API_BASE}/v3/reference/tickers"

    params = {
        "market": market,
        "search": search_text,
        "active": "true",
        "limit": limit,
        "apiKey": MASSIVE_API_KEY
    }

    try:
        response = requests.get(url, params=params, timeout=15)
        data = response.json()
        return data.get("results", []) or []
    except Exception:
        return []


def get_nasdaq_composite_candidates():
    """
    目标：只使用 Nasdaq Composite 综合指数，不使用期指，不使用 Nasdaq-100。
    """
    candidates = []

    results = get_reference_tickers("Nasdaq Composite", market="indices", limit=50)

    for item in results:
        ticker = item.get("ticker", "")
        name = item.get("name", "")

        ticker_upper = ticker.upper()
        name_upper = name.upper()

        if not ticker_upper.startswith("I:"):
            continue

        if "NDX" in ticker_upper:
            continue

        if "NASDAQ-100" in name_upper or "NASDAQ 100" in name_upper:
            continue

        if "NASDAQ" in name_upper and "COMPOSITE" in name_upper:
            candidates.append(ticker)

    fallback = [
        "I:IXIC",
        "I:COMPQ",
        "I:NASCOMP",
        "I:NASDAQCOMPOSITE",
        "I:COMP"
    ]

    for ticker in fallback:
        if ticker not in candidates:
            candidates.append(ticker)

    return candidates


def get_massive_aggs(ticker, multiplier=1, timespan="hour", days_back=14):
    """
    从 Massive 获取聚合K线。
    返回 close 数组和时间数组。
    """
    if not MASSIVE_API_KEY:
        return {
            "status": "error",
            "error": "Missing MASSIVE_API_KEY",
            "closes": [],
            "times": []
        }

    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=days_back)

    url = f"{MASSIVE_API_BASE}/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{start_date}/{today}"

    params = {
        "adjusted": "true",
        "sort": "asc",
        "limit": 50000,
        "apiKey": MASSIVE_API_KEY
    }

    try:
        response = requests.get(url, params=params, timeout=20)
        data = response.json()

        results = data.get("results")

        if not results:
            return {
                "status": "error",
                "error": "No results",
                "raw": data,
                "closes": [],
                "times": []
            }

        closes = []
        times = []

        for item in results:
            if "c" in item and item["c"] is not None:
                closes.append(item["c"])
                times.append(item.get("t"))

        if not closes:
            return {
                "status": "error",
                "error": "No closes",
                "raw": data,
                "closes": [],
                "times": []
            }

        return {
            "status": "ok",
            "closes": closes,
            "times": times
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "closes": [],
            "times": []
        }


def get_massive_mixed_sequence(name, tickers, decimals=2):
    """
    Massive 混合序列：
    - 前23个点：最近可用1小时K线 close
    - 最后1个点：最新可用5分钟K线 close
    - 如果5分钟不可用，则使用最近可用1小时 close
    """
    tested = []

    for ticker in tickers:
        test_info = {
            "ticker": ticker,
            "status": "testing"
        }
        tested.append(test_info)

        hourly = get_massive_aggs(
            ticker=ticker,
            multiplier=1,
            timespan="hour",
            days_back=14
        )

        if hourly.get("status") != "ok":
            test_info["status"] = "error"
            test_info["reason"] = hourly.get("error", "hourly data error")
            continue

        hourly_closes = hourly.get("closes", [])
        hourly_times = hourly.get("times", [])

        if len(hourly_closes) < HOURLY_POINTS:
            test_info["status"] = "error"
            test_info["reason"] = "not enough hourly bars"
            continue

        selected_hourly_closes = hourly_closes[-HOURLY_POINTS:]
        selected_hourly_times = hourly_times[-HOURLY_POINTS:]

        if is_flat_or_invalid_sequence(selected_hourly_closes):
            test_info["status"] = "error"
            test_info["reason"] = "flat or invalid hourly sequence"
            test_info["raw_last_values"] = selected_hourly_closes
            continue

        five_minute = get_massive_aggs(
            ticker=ticker,
            multiplier=5,
            timespan="minute",
            days_back=14
        )

        final_close = selected_hourly_closes[-1]
        final_time = selected_hourly_times[-1]
        final_price_source = "hourly fallback close"

        if five_minute.get("status") == "ok":
            five_closes = five_minute.get("closes", [])
            five_times = five_minute.get("times", [])

            if five_closes and five_times:
                candidate_close = five_closes[-1]
                candidate_time = five_times[-1]

                try:
                    if candidate_time is not None and final_time is not None:
                        if int(candidate_time) >= int(final_time):
                            final_close = candidate_close
                            final_time = candidate_time
                            final_price_source = "latest available 5-minute close"
                    else:
                        final_close = candidate_close
                        final_time = candidate_time
                        final_price_source = "latest available 5-minute close"
                except Exception:
                    final_close = candidate_close
                    final_time = candidate_time
                    final_price_source = "latest available 5-minute close"

        mixed_values = selected_hourly_closes + [final_close]

        sequence = format_sequence(
            mixed_values,
            decimals=decimals,
            target_points=FINAL_POINTS
        )

        if sequence == manual():
            test_info["status"] = "error"
            test_info["reason"] = "sequence format failed"
            continue

        test_info["status"] = "ok"

        return {
            "name": name,
            "ticker": ticker,
            "source": "Massive",
            "status": "ok",
            "latest": format_number(final_close, decimals),
            "first_price": format_number(mixed_values[0], decimals),
            "last_price": format_number(final_close, decimals),
            "first_price_role": "sequence start price",
            "last_price_role": "latest available 5-minute close or market close",
            "bar_interval": "mixed",
            "sequence_point_count": FINAL_POINTS,
            "sequence_rule": "first 23 points are latest available 1-hour closes; last point is latest available 5-minute close or market close",
            "final_price_source": final_price_source,
            "sequence_start_time_utc": utc_time_from_ms(selected_hourly_times[0]),
            "sequence_hourly_last_time_utc": utc_time_from_ms(selected_hourly_times[-1]),
            "sequence_last_time_utc": utc_time_from_ms(final_time),
            "sequence": sequence,
            "tested_tickers": tested
        }

    return {
        "name": name,
        "ticker": manual(),
        "source": "Massive",
        "status": "error",
        "latest": manual(),
        "first_price": manual(),
        "last_price": manual(),
        "first_price_role": "sequence start price",
        "last_price_role": "latest available 5-minute close or market close",
        "bar_interval": "mixed",
        "sequence_point_count": FINAL_POINTS,
        "sequence_rule": "first 23 points are latest available 1-hour closes; last point is latest available 5-minute close or market close",
        "final_price_source": manual(),
        "sequence": manual(),
        "tested_tickers": tested
    }


def get_binance_klines(symbol, interval, limit=100):
    """
    获取 Binance K线数据。
    使用 data-api.binance.vision，不需要 API Key。
    """
    url = f"{BINANCE_API_BASE}/api/v3/klines"

    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }

    try:
        response = requests.get(url, params=params, timeout=15)
        data = response.json()

        if not isinstance(data, list):
            return {
                "status": "error",
                "error": "Invalid Binance response",
                "raw": data,
                "klines": []
            }

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        completed_klines = []

        for item in data:
            try:
                close_time = int(item[6])
                if close_time <= now_ms:
                    completed_klines.append(item)
            except Exception:
                continue

        return {
            "status": "ok",
            "klines": completed_klines
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "klines": []
        }


def get_binance_latest_price(symbol):
    """
    获取 Binance 当前 ticker 最新价格。
    这个价格比最新完成的5分钟K线更接近实时行情。
    """
    url = f"{BINANCE_API_BASE}/api/v3/ticker/price"

    params = {
        "symbol": symbol
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()

        price = data.get("price")

        if not price:
            return {
                "status": "error",
                "error": "No ticker price",
                "raw": data
            }

        return {
            "status": "ok",
            "price": price,
            "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }


def get_binance_mixed_sequence(symbol, name, decimals=2):
    """
    Binance 混合序列：
    - 前23个点：最近可用1小时K线 close
    - 最后1个点：优先使用 Binance ticker 最新价格
    - 如果 ticker 最新价格不可用，则使用最新已完成5分钟K线 close
    - 如果5分钟K线不可用，则使用最近1小时K线 close
    """
    hourly = get_binance_klines(
        symbol=symbol,
        interval="1h",
        limit=HOURLY_POINTS + 10
    )

    if hourly.get("status") != "ok":
        return {
            "name": name,
            "symbol": symbol,
            "source": "Binance",
            "status": "error",
            "latest": manual(),
            "first_price": manual(),
            "last_price": manual(),
            "sequence": manual(),
            "error": hourly.get("error", "hourly data error")
        }

    hourly_klines = hourly.get("klines", [])

    if len(hourly_klines) < HOURLY_POINTS:
        return {
            "name": name,
            "symbol": symbol,
            "source": "Binance",
            "status": "error",
            "latest": manual(),
            "first_price": manual(),
            "last_price": manual(),
            "sequence": manual(),
            "error": "not enough completed hourly bars"
        }

    selected_hourly = hourly_klines[-HOURLY_POINTS:]
    hourly_closes = [item[4] for item in selected_hourly]
    hourly_open_times = [item[0] for item in selected_hourly]
    hourly_close_times = [item[6] for item in selected_hourly]

    final_close = hourly_closes[-1]
    final_time = hourly_close_times[-1]
    final_price_source = "hourly fallback close"

    latest_price = get_binance_latest_price(symbol)

    if latest_price.get("status") == "ok":
        final_close = latest_price.get("price")
        final_time = int(datetime.now(timezone.utc).timestamp() * 1000)
        final_price_source = "latest Binance ticker price"
    else:
        five_minute = get_binance_klines(
            symbol=symbol,
            interval="5m",
            limit=10
        )

        if five_minute.get("status") == "ok":
            five_klines = five_minute.get("klines", [])

            if five_klines:
                latest_five = five_klines[-1]
                final_close = latest_five[4]
                final_time = latest_five[6]
                final_price_source = "latest completed 5-minute close"

    mixed_values = hourly_closes + [final_close]

    sequence = format_sequence(
        mixed_values,
        decimals=decimals,
        target_points=FINAL_POINTS
    )

    if sequence == manual():
        return {
            "name": name,
            "symbol": symbol,
            "source": "Binance",
            "status": "error",
            "latest": manual(),
            "first_price": manual(),
            "last_price": manual(),
            "sequence": manual(),
            "error": "sequence format failed"
        }

    return {
        "name": name,
        "symbol": symbol,
        "source": "Binance",
        "status": "ok",
        "latest": format_number(final_close, decimals),
        "first_price": format_number(mixed_values[0], decimals),
        "last_price": format_number(final_close, decimals),
        "first_price_role": "sequence start price",
        "last_price_role": "latest Binance ticker price, or latest completed 5-minute close when ticker price is unavailable",
        "bar_interval": "mixed",
        "sequence_point_count": FINAL_POINTS,
        "sequence_rule": "first 23 points are latest available 1-hour closes; last point is latest Binance ticker price with 5-minute close fallback",
        "final_price_source": final_price_source,
        "sequence_start_time_utc": utc_time_from_ms(hourly_open_times[0]),
        "sequence_hourly_last_time_utc": utc_time_from_ms(hourly_close_times[-1]),
        "sequence_last_time_utc": utc_time_from_ms(final_time),
        "sequence": sequence
    }


@app.route("/")
@app.route("/api/market-data")
@app.route("/market-data")
def market_data():
    checked_at_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    today = datetime.now().strftime("%B %d")

    gold = get_massive_mixed_sequence(
        name="Spot Gold",
        tickers=[
            "C:XAUUSD",
            "XAUUSD",
            "F:GC"
        ],
        decimals=2
    )

    nasdaq_composite_candidates = get_nasdaq_composite_candidates()

    stocks_focus = get_massive_mixed_sequence(
        name="Nasdaq Composite",
        tickers=nasdaq_composite_candidates,
        decimals=2
    )

    btc = get_binance_mixed_sequence(
        symbol="BTCUSDT",
        name="Bitcoin",
        decimals=2
    )

    eth = get_binance_mixed_sequence(
        symbol="ETHUSDT",
        name="Ethereum",
        decimals=2
    )

    response_data = {
        "date": today,
        "checked_at_utc": checked_at_utc,

        "gold": {
            "name": "Spot Gold",
            "symbol": gold.get("ticker", manual()),
            "unit": "USD per troy ounce",
            "source": gold.get("source", "Massive"),
            "status": gold.get("status", "error"),
            "latest": gold.get("latest", manual()),
            "first_price": gold.get("first_price", manual()),
            "last_price": gold.get("last_price", manual()),
            "first_price_role": gold.get("first_price_role", "sequence start price"),
            "last_price_role": gold.get("last_price_role", "latest available 5-minute close or market close"),
            "bar_interval": gold.get("bar_interval", "mixed"),
            "sequence_point_count": gold.get("sequence_point_count", FINAL_POINTS),
            "sequence_rule": gold.get("sequence_rule", "first 23 points are latest available 1-hour closes; last point is latest available 5-minute close or market close"),
            "final_price_source": gold.get("final_price_source", manual()),
            "sequence_start_time_utc": gold.get("sequence_start_time_utc", manual()),
            "sequence_hourly_last_time_utc": gold.get("sequence_hourly_last_time_utc", manual()),
            "sequence_last_time_utc": gold.get("sequence_last_time_utc", manual()),
            "sequence": gold.get("sequence", manual()),
            "tested_tickers": gold.get("tested_tickers", [])
        },

        "stocks_focus": {
            "name": "Nasdaq Composite",
            "symbol": stocks_focus.get("ticker", manual()),
            "unit": "Index Points",
            "source": stocks_focus.get("source", "Massive"),
            "status": stocks_focus.get("status", "error"),
            "latest": stocks_focus.get("latest", manual()),
            "first_price": stocks_focus.get("first_price", manual()),
            "last_price": stocks_focus.get("last_price", manual()),
            "first_price_role": stocks_focus.get("first_price_role", "sequence start price"),
            "last_price_role": stocks_focus.get("last_price_role", "latest available 5-minute close or market close"),
            "bar_interval": stocks_focus.get("bar_interval", "mixed"),
            "sequence_point_count": stocks_focus.get("sequence_point_count", FINAL_POINTS),
            "sequence_rule": stocks_focus.get("sequence_rule", "first 23 points are latest available 1-hour closes; last point is latest available 5-minute close or market close"),
            "final_price_source": stocks_focus.get("final_price_source", manual()),
            "sequence_start_time_utc": stocks_focus.get("sequence_start_time_utc", manual()),
            "sequence_hourly_last_time_utc": stocks_focus.get("sequence_hourly_last_time_utc", manual()),
            "sequence_last_time_utc": stocks_focus.get("sequence_last_time_utc", manual()),
            "sequence": stocks_focus.get("sequence", manual()),
            "tested_tickers": stocks_focus.get("tested_tickers", [])
        },

        "btc": {
            "name": "Bitcoin",
            "symbol": "BTC/USD",
            "source_symbol": btc.get("symbol", "BTCUSDT"),
            "unit": "USD",
            "source": btc.get("source", "Binance"),
            "status": btc.get("status", "error"),
            "latest": btc.get("latest", manual()),
            "first_price": btc.get("first_price", manual()),
            "last_price": btc.get("last_price", manual()),
            "first_price_role": btc.get("first_price_role", "sequence start price"),
            "last_price_role": btc.get("last_price_role", "latest Binance ticker price, or latest completed 5-minute close when ticker price is unavailable"),
            "bar_interval": btc.get("bar_interval", "mixed"),
            "sequence_point_count": btc.get("sequence_point_count", FINAL_POINTS),
            "sequence_rule": btc.get("sequence_rule", "first 23 points are latest available 1-hour closes; last point is latest Binance ticker price with 5-minute close fallback"),
            "final_price_source": btc.get("final_price_source", manual()),
            "sequence_start_time_utc": btc.get("sequence_start_time_utc", manual()),
            "sequence_hourly_last_time_utc": btc.get("sequence_hourly_last_time_utc", manual()),
            "sequence_last_time_utc": btc.get("sequence_last_time_utc", manual()),
            "sequence": btc.get("sequence", manual())
        },

        "eth": {
            "name": "Ethereum",
            "symbol": "ETH/USD",
            "source_symbol": eth.get("symbol", "ETHUSDT"),
            "unit": "USD",
            "source": eth.get("source", "Binance"),
            "status": eth.get("status", "error"),
            "latest": eth.get("latest", manual()),
            "first_price": eth.get("first_price", manual()),
            "last_price": eth.get("last_price", manual()),
            "first_price_role": eth.get("first_price_role", "sequence start price"),
            "last_price_role": eth.get("last_price_role", "latest Binance ticker price, or latest completed 5-minute close when ticker price is unavailable"),
            "bar_interval": eth.get("bar_interval", "mixed"),
            "sequence_point_count": eth.get("sequence_point_count", FINAL_POINTS),
            "sequence_rule": eth.get("sequence_rule", "first 23 points are latest available 1-hour closes; last point is latest Binance ticker price with 5-minute close fallback"),
            "final_price_source": eth.get("final_price_source", manual()),
            "sequence_start_time_utc": eth.get("sequence_start_time_utc", manual()),
            "sequence_hourly_last_time_utc": eth.get("sequence_hourly_last_time_utc", manual()),
            "sequence_last_time_utc": eth.get("sequence_last_time_utc", manual()),
            "sequence": eth.get("sequence", manual())
        },

        "template": {
            "date": today,
            "gold_price_sequence": gold.get("sequence", manual()),
            "stocks_price_sequence": stocks_focus.get("sequence", manual()),
            "btc_price_sequence": btc.get("sequence", manual()),
            "eth_price_sequence": eth.get("sequence", manual())
        },

        "data_check": {
            "gold_source": gold.get("source", "Massive"),
            "stocks_source": stocks_focus.get("source", "Massive"),
            "btc_source": btc.get("source", "Binance"),
            "eth_source": eth.get("source", "Binance"),
            "market_time_checked": checked_at_utc,
            "sequence_rule": "mixed sequence: first 23 points are latest available 1-hour closes; crypto last point uses Binance ticker price; gold/stocks last point uses latest available 5-minute close or market close",
            "stocks_focus_note": "Stocks focus uses Nasdaq Composite index candidates only, not futures and not Nasdaq-100.",
            "crypto_note": "BTC/USD and ETH/USD are sourced from Binance BTCUSDT and ETHUSDT. The last point uses Binance ticker price, with latest completed 5-minute kline close as fallback."
        }
    }

    return jsonify(response_data)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
