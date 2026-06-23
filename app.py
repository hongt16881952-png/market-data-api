import os
import copy
import requests
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from flask import Flask, jsonify, request
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY", "").strip()
COINMARKETCAP_API_KEY = os.getenv("COINMARKETCAP_API_KEY", "").strip()

MASSIVE_API_BASE = "https://api.massive.com"
BINANCE_API_BASE = "https://data-api.binance.vision"
COINBASE_API_BASE = "https://api.exchange.coinbase.com"
COINGECKO_API_BASE = "https://api.coingecko.com/api/v3"
COINMARKETCAP_API_BASE = "https://pro-api.coinmarketcap.com/v1"
YAHOO_API_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"

HOURLY_POINTS = 23
FINAL_POINTS = 24

CACHE_SECONDS = 300
CACHE = {
    "data": None,
    "time": None
}

GLOBAL_INDICES_CACHE = {
    "data": None,
    "time": None
}


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET"
    return response


@app.route("/")
@app.route("/health")
def home():
    return jsonify({
        "status": "ok",
        "message": "Market data API is running.",
        "endpoints": [
            "/market-data",
            "/api/market-data",
            "/global-indices",
            "/api/global-indices",
            "/health"
        ],
        "cache_seconds": CACHE_SECONDS
    })


def manual():
    return "Data requires manual verification."


def now_utc_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


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


def utc_time_from_seconds(timestamp_seconds):
    try:
        return datetime.fromtimestamp(
            int(timestamp_seconds),
            timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return manual()


def get_reference_tickers(search_text, market="indices", limit=50):
    """
    从 Massive reference tickers 搜索指数 ticker。
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
        response = requests.get(url, params=params, timeout=6)
        data = response.json()
        return data.get("results", []) or []
    except Exception:
        return []


def get_nasdaq_composite_candidates():
    """
    Nasdaq Composite 候选 ticker。
    固定候选，避免每次请求都搜索 reference tickers，提高速度。
    """
    return [
        "I:IXIC",
        "I:COMPQ",
        "I:NASCOMP",
        "I:NASDAQCOMPOSITE",
        "I:COMP"
    ]


def get_index_candidates(search_text, fallback_tickers):
    """
    通用指数候选 ticker 搜索函数。
    每个指数独立取数，互不替代。
    只接受 I: 开头的指数 ticker，避免期指。
    """
    candidates = []

    results = get_reference_tickers(search_text, market="indices", limit=50)

    normalized_search = (
        search_text.upper()
        .replace("&", " ")
        .replace(".", " ")
        .replace("-", " ")
        .replace("  ", " ")
    )
    search_words = [word for word in normalized_search.split() if len(word) > 1 or word.isdigit()]

    for item in results:
        ticker = item.get("ticker", "")
        name = item.get("name", "")

        ticker_upper = ticker.upper()
        name_upper = name.upper()

        if not ticker_upper.startswith("I:"):
            continue

        if "FUTURE" in name_upper or "FUTURES" in name_upper:
            continue

        matched_count = 0

        for word in search_words:
            if word in name_upper or word in ticker_upper:
                matched_count += 1

        if matched_count >= max(1, min(2, len(search_words))):
            candidates.append(ticker)

    for ticker in fallback_tickers:
        if ticker not in candidates:
            candidates.append(ticker)

    return candidates


def get_global_index_candidates():
    return {
        "sp500": {
            "name": "S&P 500",
            "yahoo_symbol": "^GSPC",
            "tickers": [
                "I:SPX",
                "I:INX",
                "I:GSPC",
                "I:SP500"
            ]
        },
        "dow_jones": {
            "name": "Dow Jones Industrial Average",
            "yahoo_symbol": "^DJI",
            "tickers": [
                "I:DJI",
                "I:DJIA",
                "I:DJI30"
            ]
        },
        "ftse_100": {
            "name": "FTSE 100",
            "yahoo_symbol": "^FTSE",
            "tickers": [
                "I:UKX",
                "I:FTSE",
                "I:FTSE100"
            ]
        },
        "nikkei_225": {
            "name": "Nikkei 225",
            "yahoo_symbol": "^N225",
            "tickers": [
                "I:N225",
                "I:NI225",
                "I:NIKKEI225"
            ]
        },
        "dax_40": {
            "name": "DAX 40",
            "yahoo_symbol": "^GDAXI",
            "tickers": [
                "I:DAX",
                "I:GDAXI",
                "I:DAX40"
            ]
        },
        "kospi": {
            "name": "KOSPI",
            "yahoo_symbol": "^KS11",
            "tickers": [
                "I:KOSPI",
                "I:KS11"
            ]
        }
    }


def get_yahoo_chart_closes(symbol, interval="1h", range_period="14d", timeout=6):
    """
    Yahoo Finance chart 兜底源。
    symbol 示例：
    ^GSPC, ^DJI, ^FTSE, ^N225, ^GDAXI, ^KS11
    """
    encoded_symbol = quote(symbol, safe="")
    url = f"{YAHOO_API_BASE}/{encoded_symbol}"

    params = {
        "interval": interval,
        "range": range_period
    }

    headers = {
        "User-Agent": "Mozilla/5.0 market-data-api/1.0"
    }

    try:
        response = requests.get(url, params=params, headers=headers, timeout=timeout)
        data = response.json()

        result = data.get("chart", {}).get("result", [])

        if not result:
            return {
                "status": "error",
                "error": "No Yahoo chart result",
                "raw": data,
                "closes": [],
                "times": []
            }

        item = result[0]
        timestamps = item.get("timestamp", [])
        quote_data = item.get("indicators", {}).get("quote", [])

        if not quote_data:
            return {
                "status": "error",
                "error": "No Yahoo quote data",
                "raw": data,
                "closes": [],
                "times": []
            }

        closes_raw = quote_data[0].get("close", [])

        closes = []
        times = []

        for timestamp, close_value in zip(timestamps, closes_raw):
            if close_value is None:
                continue

            closes.append(close_value)
            times.append(timestamp)

        if not closes:
            return {
                "status": "error",
                "error": "No valid Yahoo closes",
                "raw": data,
                "closes": [],
                "times": []
            }

        return {
            "status": "ok",
            "closes": closes,
            "times": times,
            "source": "Yahoo Finance chart",
            "symbol": symbol
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "closes": [],
            "times": []
        }


def get_yahoo_mixed_index_sequence(name, yahoo_symbol, decimals=2):
    """
    Yahoo global index 兜底序列：
    - 前23个点：最近可用1小时 close
    - 最后1个点：最新可用5分钟 close
    - 如果5分钟不可用，则使用最近可用1小时 close
    """
    hourly = get_yahoo_chart_closes(
        symbol=yahoo_symbol,
        interval="1h",
        range_period="14d",
        timeout=6
    )

    tested = [
        {
            "source": "Yahoo Finance 1-hour chart",
            "symbol": yahoo_symbol,
            "status": hourly.get("status", "error")
        }
    ]

    if hourly.get("status") != "ok":
        return {
            "name": name,
            "ticker": yahoo_symbol,
            "source": "Yahoo Finance chart",
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
            "tested_tickers": tested,
            "error": hourly.get("error", "Yahoo hourly data error")
        }

    hourly_closes = hourly.get("closes", [])
    hourly_times = hourly.get("times", [])

    if len(hourly_closes) < HOURLY_POINTS:
        return {
            "name": name,
            "ticker": yahoo_symbol,
            "source": "Yahoo Finance chart",
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
            "tested_tickers": tested,
            "error": "not enough Yahoo hourly closes"
        }

    selected_hourly_closes = hourly_closes[-HOURLY_POINTS:]
    selected_hourly_times = hourly_times[-HOURLY_POINTS:]

    if is_flat_or_invalid_sequence(selected_hourly_closes):
        return {
            "name": name,
            "ticker": yahoo_symbol,
            "source": "Yahoo Finance chart",
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
            "tested_tickers": tested,
            "error": "flat or invalid Yahoo hourly sequence"
        }

    five_minute = get_yahoo_chart_closes(
        symbol=yahoo_symbol,
        interval="5m",
        range_period="5d",
        timeout=6
    )

    tested.append({
        "source": "Yahoo Finance 5-minute chart",
        "symbol": yahoo_symbol,
        "status": five_minute.get("status", "error")
    })

    final_close = selected_hourly_closes[-1]
    final_time = selected_hourly_times[-1]
    final_price_source = "Yahoo hourly fallback close"

    if five_minute.get("status") == "ok":
        five_closes = five_minute.get("closes", [])
        five_times = five_minute.get("times", [])

        if five_closes and five_times:
            candidate_close = five_closes[-1]
            candidate_time = five_times[-1]

            try:
                if int(candidate_time) >= int(final_time):
                    final_close = candidate_close
                    final_time = candidate_time
                    final_price_source = "Yahoo latest available 5-minute close"
            except Exception:
                final_close = candidate_close
                final_time = candidate_time
                final_price_source = "Yahoo latest available 5-minute close"

    mixed_values = selected_hourly_closes + [final_close]

    sequence = format_sequence(
        mixed_values,
        decimals=decimals,
        target_points=FINAL_POINTS
    )

    if sequence == manual():
        return {
            "name": name,
            "ticker": yahoo_symbol,
            "source": "Yahoo Finance chart",
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
            "tested_tickers": tested,
            "error": "Yahoo sequence format failed"
        }

    return {
        "name": name,
        "ticker": yahoo_symbol,
        "source": "Yahoo Finance chart",
        "status": "ok",
        "latest": format_number(final_close, decimals),
        "first_price": format_number(mixed_values[0], decimals),
        "last_price": format_number(final_close, decimals),
        "first_price_role": "sequence start price",
        "last_price_role": "latest available 5-minute close or market close",
        "bar_interval": "mixed",
        "sequence_point_count": FINAL_POINTS,
        "sequence_rule": "first 23 points are Yahoo Finance 1-hour closes; last point is Yahoo Finance latest available 5-minute close or market close",
        "final_price_source": final_price_source,
        "sequence_start_time_utc": utc_time_from_seconds(selected_hourly_times[0]),
        "sequence_hourly_last_time_utc": utc_time_from_seconds(selected_hourly_times[-1]),
        "sequence_last_time_utc": utc_time_from_seconds(final_time),
        "sequence": sequence,
        "tested_tickers": tested
    }


def get_global_indices_data():
    candidates_map = get_global_index_candidates()
    output = {}

    def fetch_one(key, item):
        massive_result = get_massive_mixed_sequence(
            name=item.get("name", key),
            tickers=item.get("tickers", []),
            decimals=2
        )

        if massive_result.get("status") == "ok":
            return key, wrap_index_result(item.get("name", key), massive_result)

        yahoo_symbol = item.get("yahoo_symbol")

        if yahoo_symbol:
            yahoo_result = get_yahoo_mixed_index_sequence(
                name=item.get("name", key),
                yahoo_symbol=yahoo_symbol,
                decimals=2
            )

            if yahoo_result.get("status") == "ok":
                wrapped = wrap_index_result(item.get("name", key), yahoo_result)
                wrapped["fallback_used"] = "Yahoo Finance chart"
                wrapped["massive_attempt"] = massive_result.get("tested_tickers", [])
                return key, wrapped

            fallback_error = {
                "massive_attempt": massive_result.get("tested_tickers", []),
                "yahoo_attempt": yahoo_result.get("tested_tickers", []),
                "yahoo_error": yahoo_result.get("error", manual())
            }
        else:
            fallback_error = {
                "massive_attempt": massive_result.get("tested_tickers", []),
                "yahoo_error": "No Yahoo symbol configured"
            }

        wrapped_error = wrap_index_result(item.get("name", key), massive_result)
        wrapped_error["fallback_used"] = manual()
        wrapped_error["fallback_error"] = fallback_error
        return key, wrapped_error

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(fetch_one, key, item)
            for key, item in candidates_map.items()
        ]

        for future in futures:
            try:
                key, value = future.result(timeout=22)
                output[key] = value
            except Exception as e:
                pass

    ordered_output = {}

    for key, item in candidates_map.items():
        ordered_output[key] = output.get(key, {
            "name": item.get("name", key),
            "symbol": manual(),
            "unit": "Index Points",
            "source": "Massive / Yahoo Finance chart",
            "status": "error",
            "latest": manual(),
            "first_price": manual(),
            "last_price": manual(),
            "sequence": manual(),
            "error": "Global index request failed or timed out"
        })

    return ordered_output


def get_coinbase_hourly_closes(product_id, target_points=23):
    """
    Coinbase 1小时K线：
    - product_id 示例：BTC-USD、ETH-USD
    - candles 返回格式：[time, low, high, open, close, volume]
    - 只使用已经完成的1小时 candle
    """
    url = f"{COINBASE_API_BASE}/products/{product_id}/candles"

    params = {
        "granularity": 3600
    }

    headers = {
        "User-Agent": "market-data-api/1.0"
    }

    try:
        response = requests.get(url, params=params, headers=headers, timeout=6)
        data = response.json()

        if not isinstance(data, list):
            return {
                "status": "error",
                "error": "Invalid Coinbase candles response",
                "raw": data,
                "closes": [],
                "times": []
            }

        now_seconds = int(datetime.now(timezone.utc).timestamp())
        completed = []

        for item in data:
            try:
                candle_start = int(item[0])
                candle_close_time = candle_start + 3600

                if candle_close_time <= now_seconds:
                    completed.append(item)
            except Exception:
                continue

        completed.sort(key=lambda item: int(item[0]))

        if len(completed) < target_points:
            return {
                "status": "error",
                "error": "not enough completed Coinbase hourly candles",
                "closes": [],
                "times": []
            }

        selected = completed[-target_points:]
        closes = [item[4] for item in selected]
        times = [item[0] for item in selected]

        return {
            "status": "ok",
            "closes": closes,
            "times": times,
            "source": "Coinbase",
            "product_id": product_id
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "closes": [],
            "times": []
        }


def get_coinbase_latest_price(product_id):
    """
    Coinbase 最新 ticker 价格：
    - product_id 示例：BTC-USD、ETH-USD
    - 返回 Coinbase 真 USD 交易对最新价格
    """
    url = f"{COINBASE_API_BASE}/products/{product_id}/ticker"

    headers = {
        "User-Agent": "market-data-api/1.0"
    }

    try:
        response = requests.get(url, headers=headers, timeout=5)
        data = response.json()

        price = data.get("price")

        if not price:
            return {
                "status": "error",
                "error": "No Coinbase ticker price",
                "raw": data
            }

        return {
            "status": "ok",
            "price": price,
            "source": "Coinbase",
            "product_id": product_id,
            "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }


def get_binance_klines(symbol, interval, limit=100):
    """
    Binance K线数据：
    - symbol 示例：BTCUSDT、ETHUSDT
    - 使用 data-api.binance.vision，不需要 API Key
    """
    url = f"{BINANCE_API_BASE}/api/v3/klines"

    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }

    try:
        response = requests.get(url, params=params, timeout=6)
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


def get_binance_hourly_closes(symbol, target_points=23):
    """
    Binance 1小时K线兜底。
    Coinbase 前23个点不足时，使用 Binance 1小时 close。
    """
    hourly = get_binance_klines(
        symbol=symbol,
        interval="1h",
        limit=target_points + 20
    )

    if hourly.get("status") != "ok":
        return {
            "status": "error",
            "error": hourly.get("error", "Binance hourly data error"),
            "closes": [],
            "times": []
        }

    klines = hourly.get("klines", [])

    if len(klines) < target_points:
        return {
            "status": "error",
            "error": "not enough completed Binance hourly bars",
            "closes": [],
            "times": []
        }

    selected = klines[-target_points:]
    closes = [item[4] for item in selected]
    times = [item[0] for item in selected]

    return {
        "status": "ok",
        "closes": closes,
        "times": times,
        "source": "Binance",
        "symbol": symbol
    }


def get_binance_latest_price(symbol):
    """
    Binance ticker 最新价格兜底。
    """
    url = f"{BINANCE_API_BASE}/api/v3/ticker/price"

    params = {
        "symbol": symbol
    }

    try:
        response = requests.get(url, params=params, timeout=5)
        data = response.json()

        price = data.get("price")

        if not price:
            return {
                "status": "error",
                "error": "No Binance ticker price",
                "raw": data
            }

        return {
            "status": "ok",
            "price": price,
            "source": "Binance",
            "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }


def get_binance_latest_5m_close(symbol):
    """
    Binance 最新完成5分钟K线 close，作为最后兜底。
    """
    five_minute = get_binance_klines(
        symbol=symbol,
        interval="5m",
        limit=10
    )

    if five_minute.get("status") != "ok":
        return {
            "status": "error",
            "error": five_minute.get("error", "Binance 5m data error")
        }

    klines = five_minute.get("klines", [])

    if not klines:
        return {
            "status": "error",
            "error": "No completed Binance 5m candles"
        }

    latest = klines[-1]

    return {
        "status": "ok",
        "price": latest[4],
        "source": "Binance 5-minute close",
        "time_ms": latest[6]
    }


def get_coingecko_usd_price(coin_id):
    """
    CoinGecko USD 聚合参考价。
    coin_id 示例：bitcoin、ethereum
    """
    url = f"{COINGECKO_API_BASE}/simple/price"

    params = {
        "ids": coin_id,
        "vs_currencies": "usd"
    }

    try:
        response = requests.get(url, params=params, timeout=5)
        data = response.json()

        price = data.get(coin_id, {}).get("usd")

        if price is None:
            return {
                "status": "error",
                "error": "No CoinGecko USD price",
                "raw": data
            }

        return {
            "status": "ok",
            "price": price,
            "source": "CoinGecko",
            "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }


def get_coinmarketcap_usd_price(symbol):
    """
    CoinMarketCap USD 最新报价。
    需要在 Render Environment Variables 里设置：
    COINMARKETCAP_API_KEY
    如果没有设置，会自动跳过，不影响程序运行。
    """
    if not COINMARKETCAP_API_KEY:
        return {
            "status": "error",
            "error": "Missing COINMARKETCAP_API_KEY"
        }

    url = f"{COINMARKETCAP_API_BASE}/cryptocurrency/quotes/latest"

    params = {
        "symbol": symbol,
        "convert": "USD"
    }

    headers = {
        "X-CMC_PRO_API_KEY": COINMARKETCAP_API_KEY,
        "Accept": "application/json"
    }

    try:
        response = requests.get(url, params=params, headers=headers, timeout=5)
        data = response.json()

        price = data.get("data", {}).get(symbol, {}).get("quote", {}).get("USD", {}).get("price")

        if price is None:
            return {
                "status": "error",
                "error": "No CoinMarketCap USD price",
                "raw": data
            }

        return {
            "status": "ok",
            "price": price,
            "source": "CoinMarketCap",
            "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }


def get_crypto_final_price(product_id, binance_symbol, coingecko_id, cmc_symbol):
    """
    第24个最新价格点优先级：
    1. Coinbase ticker 真 USD 交易对
    2. CoinGecko USD 聚合参考价
    3. CoinMarketCap USD 最新价，如果已设置 API Key
    4. Binance ticker
    5. Binance 最新完成5分钟K线 close
    """
    attempts = []

    coinbase_price = get_coinbase_latest_price(product_id)
    attempts.append({
        "source": "Coinbase",
        "status": coinbase_price.get("status", "error")
    })

    if coinbase_price.get("status") == "ok":
        return {
            "status": "ok",
            "price": coinbase_price.get("price"),
            "source": f"Coinbase {product_id} ticker price",
            "time_utc": coinbase_price.get("time_utc", manual()),
            "attempts": attempts
        }

    coingecko_price = get_coingecko_usd_price(coingecko_id)
    attempts.append({
        "source": "CoinGecko",
        "status": coingecko_price.get("status", "error")
    })

    if coingecko_price.get("status") == "ok":
        return {
            "status": "ok",
            "price": coingecko_price.get("price"),
            "source": "CoinGecko USD reference price",
            "time_utc": coingecko_price.get("time_utc", manual()),
            "attempts": attempts
        }

    cmc_price = get_coinmarketcap_usd_price(cmc_symbol)
    attempts.append({
        "source": "CoinMarketCap",
        "status": cmc_price.get("status", "error")
    })

    if cmc_price.get("status") == "ok":
        return {
            "status": "ok",
            "price": cmc_price.get("price"),
            "source": "CoinMarketCap USD quote",
            "time_utc": cmc_price.get("time_utc", manual()),
            "attempts": attempts
        }

    binance_price = get_binance_latest_price(binance_symbol)
    attempts.append({
        "source": "Binance ticker",
        "status": binance_price.get("status", "error")
    })

    if binance_price.get("status") == "ok":
        return {
            "status": "ok",
            "price": binance_price.get("price"),
            "source": f"Binance {binance_symbol} ticker price",
            "time_utc": binance_price.get("time_utc", manual()),
            "attempts": attempts
        }

    binance_5m = get_binance_latest_5m_close(binance_symbol)
    attempts.append({
        "source": "Binance 5-minute close",
        "status": binance_5m.get("status", "error")
    })

    if binance_5m.get("status") == "ok":
        return {
            "status": "ok",
            "price": binance_5m.get("price"),
            "source": f"Binance {binance_symbol} latest completed 5-minute close",
            "time_utc": utc_time_from_ms(binance_5m.get("time_ms")),
            "attempts": attempts
        }

    return {
        "status": "error",
        "price": manual(),
        "source": manual(),
        "time_utc": manual(),
        "attempts": attempts
    }


def get_crypto_mixed_sequence(name, product_id, binance_symbol, coingecko_id, cmc_symbol, decimals=2):
    """
    Crypto 混合序列：
    - 前23个点：优先 Coinbase 1小时 USD candles
    - 如果 Coinbase 前23个点不可用，使用 Binance 1小时 candles 兜底
    - 第24个点：Coinbase ticker -> CoinGecko -> CoinMarketCap -> Binance ticker -> Binance 5m close
    """
    tested_sources = []

    hourly = get_coinbase_hourly_closes(
        product_id=product_id,
        target_points=HOURLY_POINTS
    )

    tested_sources.append({
        "source": "Coinbase hourly candles",
        "status": hourly.get("status", "error")
    })

    hourly_source = f"Coinbase {product_id} hourly candles"
    hourly_time_type = "seconds"

    if hourly.get("status") != "ok":
        hourly = get_binance_hourly_closes(
            symbol=binance_symbol,
            target_points=HOURLY_POINTS
        )

        tested_sources.append({
            "source": "Binance hourly candles",
            "status": hourly.get("status", "error")
        })

        hourly_source = f"Binance {binance_symbol} hourly candles"
        hourly_time_type = "milliseconds"

    if hourly.get("status") != "ok":
        return {
            "name": name,
            "status": "error",
            "source": "Multi-source",
            "latest": manual(),
            "first_price": manual(),
            "last_price": manual(),
            "sequence": manual(),
            "tested_sources": tested_sources,
            "error": hourly.get("error", "No valid hourly sequence")
        }

    hourly_closes = hourly.get("closes", [])
    hourly_times = hourly.get("times", [])

    if len(hourly_closes) < HOURLY_POINTS:
        return {
            "name": name,
            "status": "error",
            "source": "Multi-source",
            "latest": manual(),
            "first_price": manual(),
            "last_price": manual(),
            "sequence": manual(),
            "tested_sources": tested_sources,
            "error": "not enough hourly closes"
        }

    final_price = get_crypto_final_price(
        product_id=product_id,
        binance_symbol=binance_symbol,
        coingecko_id=coingecko_id,
        cmc_symbol=cmc_symbol
    )

    tested_sources.extend(final_price.get("attempts", []))

    if final_price.get("status") != "ok":
        return {
            "name": name,
            "status": "error",
            "source": "Multi-source",
            "latest": manual(),
            "first_price": manual(),
            "last_price": manual(),
            "sequence": manual(),
            "tested_sources": tested_sources,
            "error": "No valid final price"
        }

    final_close = final_price.get("price")
    mixed_values = hourly_closes + [final_close]

    sequence = format_sequence(
        mixed_values,
        decimals=decimals,
        target_points=FINAL_POINTS
    )

    if sequence == manual():
        return {
            "name": name,
            "status": "error",
            "source": "Multi-source",
            "latest": manual(),
            "first_price": manual(),
            "last_price": manual(),
            "sequence": manual(),
            "tested_sources": tested_sources,
            "error": "sequence format failed"
        }

    if hourly_time_type == "seconds":
        sequence_start_time = utc_time_from_seconds(hourly_times[0])
        sequence_hourly_last_time = utc_time_from_seconds(hourly_times[-1])
    else:
        sequence_start_time = utc_time_from_ms(hourly_times[0])
        sequence_hourly_last_time = utc_time_from_ms(hourly_times[-1])

    return {
        "name": name,
        "symbol": product_id,
        "source_symbol": {
            "coinbase": product_id,
            "binance": binance_symbol,
            "coingecko": coingecko_id,
            "coinmarketcap": cmc_symbol
        },
        "source": "Multi-source",
        "status": "ok",
        "latest": format_number(final_close, decimals),
        "first_price": format_number(mixed_values[0], decimals),
        "last_price": format_number(final_close, decimals),
        "first_price_role": "sequence start price",
        "last_price_role": "latest USD reference price from Coinbase, CoinGecko, CoinMarketCap, or Binance fallback",
        "bar_interval": "mixed",
        "sequence_point_count": FINAL_POINTS,
        "sequence_rule": "first 23 points use Coinbase 1-hour USD candles when available, otherwise Binance 1-hour closes; last point uses Coinbase ticker price, then CoinGecko/CoinMarketCap USD reference price, then Binance fallback",
        "hourly_sequence_source": hourly_source,
        "final_price_source": final_price.get("source", manual()),
        "sequence_start_time_utc": sequence_start_time,
        "sequence_hourly_last_time_utc": sequence_hourly_last_time,
        "sequence_last_time_utc": final_price.get("time_utc", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        "sequence": sequence,
        "tested_sources": tested_sources
    }


def build_market_data():
    checked_at_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    today = datetime.now().strftime("%B %d")

    with ThreadPoolExecutor(max_workers=4) as executor:
        future_gold = executor.submit(
            get_massive_mixed_sequence,
            "Spot Gold",
            [
                "C:XAUUSD",
                "XAUUSD",
                "F:GC"
            ],
            2
        )

        future_stocks_focus = executor.submit(
            get_massive_mixed_sequence,
            "Nasdaq Composite",
            get_nasdaq_composite_candidates(),
            2
        )

        future_btc = executor.submit(
            get_crypto_mixed_sequence,
            "Bitcoin",
            "BTC-USD",
            "BTCUSDT",
            "bitcoin",
            "BTC",
            2
        )

        future_eth = executor.submit(
            get_crypto_mixed_sequence,
            "Ethereum",
            "ETH-USD",
            "ETHUSDT",
            "ethereum",
            "ETH",
            2
        )

        gold = future_gold.result(timeout=18)
        stocks_focus = future_stocks_focus.result(timeout=18)
        btc = future_btc.result(timeout=18)
        eth = future_eth.result(timeout=18)

    # 为了避免 Render 免费版首次请求超时，主 /market-data 不再直接抓取 global_indices。
    # global_indices 已拆分到 /global-indices 单独请求。
    global_indices = {}
    global_indices_price_sequences = {}

    for key, value in global_indices.items():
        global_indices_price_sequences[key] = value.get("sequence", manual())

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

        "global_indices": global_indices,

        "btc": {
            "name": "Bitcoin",
            "symbol": "BTC/USD",
            "source_symbol": btc.get("source_symbol", {}),
            "unit": "USD",
            "source": btc.get("source", "Multi-source"),
            "status": btc.get("status", "error"),
            "latest": btc.get("latest", manual()),
            "first_price": btc.get("first_price", manual()),
            "last_price": btc.get("last_price", manual()),
            "first_price_role": btc.get("first_price_role", "sequence start price"),
            "last_price_role": btc.get("last_price_role", "latest USD reference price from Coinbase, CoinGecko, CoinMarketCap, or Binance fallback"),
            "bar_interval": btc.get("bar_interval", "mixed"),
            "sequence_point_count": btc.get("sequence_point_count", FINAL_POINTS),
            "sequence_rule": btc.get("sequence_rule", "first 23 points use Coinbase 1-hour USD candles when available, otherwise Binance 1-hour closes; last point uses Coinbase ticker price, then CoinGecko/CoinMarketCap USD reference price, then Binance fallback"),
            "hourly_sequence_source": btc.get("hourly_sequence_source", manual()),
            "final_price_source": btc.get("final_price_source", manual()),
            "sequence_start_time_utc": btc.get("sequence_start_time_utc", manual()),
            "sequence_hourly_last_time_utc": btc.get("sequence_hourly_last_time_utc", manual()),
            "sequence_last_time_utc": btc.get("sequence_last_time_utc", manual()),
            "sequence": btc.get("sequence", manual()),
            "tested_sources": btc.get("tested_sources", [])
        },

        "eth": {
            "name": "Ethereum",
            "symbol": "ETH/USD",
            "source_symbol": eth.get("source_symbol", {}),
            "unit": "USD",
            "source": eth.get("source", "Multi-source"),
            "status": eth.get("status", "error"),
            "latest": eth.get("latest", manual()),
            "first_price": eth.get("first_price", manual()),
            "last_price": eth.get("last_price", manual()),
            "first_price_role": eth.get("first_price_role", "sequence start price"),
            "last_price_role": eth.get("last_price_role", "latest USD reference price from Coinbase, CoinGecko, CoinMarketCap, or Binance fallback"),
            "bar_interval": eth.get("bar_interval", "mixed"),
            "sequence_point_count": eth.get("sequence_point_count", FINAL_POINTS),
            "sequence_rule": eth.get("sequence_rule", "first 23 points use Coinbase 1-hour USD candles when available, otherwise Binance 1-hour closes; last point uses Coinbase ticker price, then CoinGecko/CoinMarketCap USD reference price, then Binance fallback"),
            "hourly_sequence_source": eth.get("hourly_sequence_source", manual()),
            "final_price_source": eth.get("final_price_source", manual()),
            "sequence_start_time_utc": eth.get("sequence_start_time_utc", manual()),
            "sequence_hourly_last_time_utc": eth.get("sequence_hourly_last_time_utc", manual()),
            "sequence_last_time_utc": eth.get("sequence_last_time_utc", manual()),
            "sequence": eth.get("sequence", manual()),
            "tested_sources": eth.get("tested_sources", [])
        },

        "template": {
            "date": today,
            "gold_price_sequence": gold.get("sequence", manual()),
            "stocks_price_sequence": stocks_focus.get("sequence", manual()),
            "btc_price_sequence": btc.get("sequence", manual()),
            "eth_price_sequence": eth.get("sequence", manual()),
            "global_indices_price_sequences": global_indices_price_sequences
        },

        "data_check": {
            "gold_source": gold.get("source", "Massive"),
            "stocks_source": stocks_focus.get("source", "Massive"),
            "global_indices_source": "Massive first, Yahoo Finance chart fallback",
            "btc_source": btc.get("source", "Multi-source"),
            "eth_source": eth.get("source", "Multi-source"),
            "market_time_checked": checked_at_utc,
            "sequence_rule": "mixed sequence: first 23 points are hourly closes; crypto uses Coinbase hourly USD candles first and Binance hourly candles as fallback; crypto final point uses Coinbase ticker, CoinGecko, CoinMarketCap, Binance ticker, then Binance 5-minute close fallback.",
            "stocks_focus_note": "Stocks focus uses Nasdaq Composite index candidates only, not futures and not Nasdaq-100.",
            "global_indices_note": "Global indices are available from /global-indices. They use Massive first and Yahoo Finance chart as fallback. They are not fetched inside /market-data to avoid Render timeout.",
            "crypto_note": "BTC/USD and ETH/USD use Coinbase USD pairs first. Binance is used as a fallback for hourly sequence stability. CoinGecko and CoinMarketCap are used as latest USD reference fallbacks."
        }
    }

    return response_data


@app.route("/api/market-data")
@app.route("/market-data")
def market_data():
    refresh = request.args.get("refresh", "").lower() in ["1", "true", "yes"]

    if CACHE["data"] is not None and CACHE["time"] is not None and not refresh:
        age = (datetime.now(timezone.utc) - CACHE["time"]).total_seconds()

        if age < CACHE_SECONDS:
            cached_data = copy.deepcopy(CACHE["data"])
            cached_data["cache"] = {
                "status": "hit",
                "age_seconds": int(age),
                "cache_seconds": CACHE_SECONDS
            }
            return jsonify(cached_data)

    try:
        response_data = build_market_data()
        response_data["cache"] = {
            "status": "miss",
            "age_seconds": 0,
            "cache_seconds": CACHE_SECONDS
        }

        CACHE["data"] = copy.deepcopy(response_data)
        CACHE["time"] = datetime.now(timezone.utc)

        return jsonify(response_data)

    except Exception as e:
        if CACHE["data"] is not None:
            cached_data = copy.deepcopy(CACHE["data"])
            cached_data["cache"] = {
                "status": "stale_fallback",
                "error": str(e),
                "cache_seconds": CACHE_SECONDS
            }
            return jsonify(cached_data)

        return jsonify({
            "status": "error",
            "error": str(e),
            "message": "Market data request failed and no cache is available."
        }), 500


@app.route("/api/global-indices")
@app.route("/global-indices")
def global_indices_endpoint():
    refresh = request.args.get("refresh", "").lower() in ["1", "true", "yes"]

    if GLOBAL_INDICES_CACHE["data"] is not None and GLOBAL_INDICES_CACHE["time"] is not None and not refresh:
        age = (datetime.now(timezone.utc) - GLOBAL_INDICES_CACHE["time"]).total_seconds()

        if age < CACHE_SECONDS:
            cached_data = copy.deepcopy(GLOBAL_INDICES_CACHE["data"])
            cached_data["cache"] = {
                "status": "hit",
                "age_seconds": int(age),
                "cache_seconds": CACHE_SECONDS
            }
            return jsonify(cached_data)

    try:
        checked_at_utc = now_utc_text()
        global_indices = get_global_indices_data()
        global_indices_price_sequences = {}

        for key, value in global_indices.items():
            global_indices_price_sequences[key] = value.get("sequence", manual())

        response_data = {
            "date": datetime.now().strftime("%B %d"),
            "checked_at_utc": checked_at_utc,
            "global_indices": global_indices,
            "template": {
                "global_indices_price_sequences": global_indices_price_sequences
            },
            "data_check": {
                "global_indices_source": "Massive first, Yahoo Finance chart fallback",
                "market_time_checked": checked_at_utc,
                "global_indices_note": "Global indices are available from /global-indices. They use Massive first and Yahoo Finance chart as fallback. They are not fetched inside /market-data to avoid Render timeout.",
                "cache_seconds": CACHE_SECONDS
            },
            "cache": {
                "status": "miss",
                "age_seconds": 0,
                "cache_seconds": CACHE_SECONDS
            }
        }

        GLOBAL_INDICES_CACHE["data"] = copy.deepcopy(response_data)
        GLOBAL_INDICES_CACHE["time"] = datetime.now(timezone.utc)

        return jsonify(response_data)

    except Exception as e:
        if GLOBAL_INDICES_CACHE["data"] is not None:
            cached_data = copy.deepcopy(GLOBAL_INDICES_CACHE["data"])
            cached_data["cache"] = {
                "status": "stale_fallback",
                "error": str(e),
                "cache_seconds": CACHE_SECONDS
            }
            return jsonify(cached_data)

        return jsonify({
            "status": "error",
            "error": str(e),
            "message": "Global indices request failed and no cache is available."
        }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
