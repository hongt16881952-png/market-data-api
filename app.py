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
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()

MASSIVE_API_BASE = "https://api.massive.com"
BINANCE_API_BASE = "https://data-api.binance.vision"
COINBASE_API_BASE = "https://api.exchange.coinbase.com"
COINGECKO_API_BASE = "https://api.coingecko.com/api/v3"
COINMARKETCAP_API_BASE = "https://pro-api.coinmarketcap.com/v1"
YAHOO_API_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
YAHOO_QUOTE_API_BASE = "https://query1.finance.yahoo.com/v7/finance/quote"
TWELVEDATA_API_BASE = "https://api.twelvedata.com"

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
    """
    全球主要股票指数候选 ticker。
    Massive 优先，Yahoo Finance chart 兜底。
    每个指数独立取数，互不替代。
    """
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
        response = requests.get(url, params=params, timeout=6)
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
            except Exception:
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
    1. CoinGecko USD 聚合参考价
    2. CoinMarketCap USD 最新价，如果已设置 API Key
    3. Coinbase ticker 真 USD 交易对
    4. Binance ticker
    5. Binance 最新完成5分钟K线 close

    目的：
    - 更接近 Google / CoinGecko / CoinMarketCap 这类综合美元参考报价
    - Coinbase / Binance 作为备用，保证稳定性
    """
    attempts = []

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
        "last_price_role": "latest USD reference price from CoinGecko, CoinMarketCap, Coinbase, or Binance fallback",
        "bar_interval": "mixed",
        "sequence_point_count": FINAL_POINTS,
        "sequence_rule": "first 23 points use Coinbase 1-hour USD candles when available, otherwise Binance 1-hour closes; last point uses CoinGecko USD reference price, then CoinMarketCap, Coinbase ticker, and Binance fallback",
        "hourly_sequence_source": hourly_source,
        "final_price_source": final_price.get("source", manual()),
        "sequence_start_time_utc": sequence_start_time,
        "sequence_hourly_last_time_utc": sequence_hourly_last_time,
        "sequence_last_time_utc": final_price.get("time_utc", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        "sequence": sequence,
        "tested_sources": tested_sources
    }



def get_twelvedata_time_series(symbol, interval="1h", outputsize=60, timeout=7):
    """
    Twelve Data time_series 通用函数。
    用于 Gold / XAUUSD 的小时序列和5分钟兜底。
    """
    if not TWELVEDATA_API_KEY:
        return {
            "status": "error",
            "error": "Missing TWELVEDATA_API_KEY",
            "symbol": symbol,
            "closes": [],
            "times": []
        }

    url = f"{TWELVEDATA_API_BASE}/time_series"

    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVEDATA_API_KEY
    }

    try:
        response = requests.get(url, params=params, timeout=timeout)
        data = response.json()

        if data.get("status") == "error":
            return {
                "status": "error",
                "error": data.get("message") or data.get("code") or "Twelve Data error",
                "raw": data,
                "symbol": symbol,
                "closes": [],
                "times": []
            }

        values = data.get("values", [])

        if not values:
            return {
                "status": "error",
                "error": "No Twelve Data values",
                "raw": data,
                "symbol": symbol,
                "closes": [],
                "times": []
            }

        rows = []

        for item in values:
            close_value = item.get("close")
            datetime_value = item.get("datetime")

            if close_value is None or datetime_value is None:
                continue

            try:
                close_number = float(close_value)
            except Exception:
                continue

            rows.append({
                "datetime": str(datetime_value),
                "close": close_number
            })

        if not rows:
            return {
                "status": "error",
                "error": "No valid Twelve Data closes",
                "raw": data,
                "symbol": symbol,
                "closes": [],
                "times": []
            }

        # Twelve Data 通常是最新在前；这里统一按时间升序排列，方便生成趋势序列。
        rows = sorted(rows, key=lambda x: x["datetime"])

        return {
            "status": "ok",
            "source": "Twelve Data time_series",
            "symbol": symbol,
            "interval": interval,
            "closes": [row["close"] for row in rows],
            "times": [row["datetime"] for row in rows]
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "symbol": symbol,
            "closes": [],
            "times": []
        }


def twelvedata_datetime_to_text(value):
    if value in [None, ""]:
        return manual()

    value_text = str(value)

    if "UTC" in value_text:
        return value_text

    return f"{value_text} UTC"


def get_twelvedata_mixed_sequence(name, symbol_candidates, decimals=2):
    """
    Twelve Data 混合序列：
    - 前23个点：Twelve Data 1小时 close
    - 最后1个点：优先 Twelve Data quote 最新价
    - 如果 quote 不可用，则使用 Twelve Data 5分钟 close
    """
    tested = []

    for symbol in symbol_candidates:
        hourly = get_twelvedata_time_series(
            symbol=symbol,
            interval="1h",
            outputsize=HOURLY_POINTS + 30,
            timeout=7
        )

        tested.append({
            "source": "Twelve Data 1-hour time_series",
            "symbol": symbol,
            "status": hourly.get("status", "error"),
            "reason": hourly.get("error", "")
        })

        if hourly.get("status") != "ok":
            continue

        hourly_closes = hourly.get("closes", [])
        hourly_times = hourly.get("times", [])

        if len(hourly_closes) < HOURLY_POINTS:
            tested.append({
                "source": "Twelve Data 1-hour time_series",
                "symbol": symbol,
                "status": "error",
                "reason": "not enough hourly bars"
            })
            continue

        selected_hourly_closes = hourly_closes[-HOURLY_POINTS:]
        selected_hourly_times = hourly_times[-HOURLY_POINTS:]

        if is_flat_or_invalid_sequence(selected_hourly_closes):
            tested.append({
                "source": "Twelve Data 1-hour time_series",
                "symbol": symbol,
                "status": "error",
                "reason": "flat or invalid hourly sequence"
            })
            continue

        final_close = selected_hourly_closes[-1]
        final_time = selected_hourly_times[-1]
        final_price_source = "Twelve Data hourly fallback close"

        quote = get_twelvedata_index_quote(
            name=name,
            symbol_candidates=[
                symbol
            ],
            decimals=decimals
        )

        tested.append({
            "source": "Twelve Data quote",
            "symbol": symbol,
            "status": quote.get("status", "error"),
            "reason": quote.get("error", "")
        })

        if quote.get("status") == "ok":
            try:
                final_close = float(quote.get("latest"))
                final_time = quote.get("market_time_utc", final_time)
                final_price_source = "Twelve Data quote latest price"
            except Exception:
                pass
        else:
            five_minute = get_twelvedata_time_series(
                symbol=symbol,
                interval="5min",
                outputsize=30,
                timeout=7
            )

            tested.append({
                "source": "Twelve Data 5-minute time_series",
                "symbol": symbol,
                "status": five_minute.get("status", "error"),
                "reason": five_minute.get("error", "")
            })

            if five_minute.get("status") == "ok":
                five_closes = five_minute.get("closes", [])
                five_times = five_minute.get("times", [])

                if five_closes and five_times:
                    final_close = five_closes[-1]
                    final_time = five_times[-1]
                    final_price_source = "Twelve Data latest available 5-minute close"

        mixed_values = selected_hourly_closes + [final_close]

        sequence = format_sequence(
            mixed_values,
            decimals=decimals,
            target_points=FINAL_POINTS
        )

        if sequence == manual():
            tested.append({
                "source": "Twelve Data sequence formatter",
                "symbol": symbol,
                "status": "error",
                "reason": "sequence format failed"
            })
            continue

        return {
            "name": name,
            "ticker": symbol,
            "source": "Twelve Data",
            "status": "ok",
            "latest": format_number(final_close, decimals),
            "first_price": format_number(mixed_values[0], decimals),
            "last_price": format_number(final_close, decimals),
            "first_price_role": "sequence start price",
            "last_price_role": "latest available quote price or 5-minute close",
            "bar_interval": "mixed",
            "sequence_point_count": FINAL_POINTS,
            "sequence_rule": "first 23 points are Twelve Data 1-hour closes; last point is Twelve Data quote latest price or 5-minute close",
            "final_price_source": final_price_source,
            "sequence_start_time_utc": twelvedata_datetime_to_text(selected_hourly_times[0]),
            "sequence_hourly_last_time_utc": twelvedata_datetime_to_text(selected_hourly_times[-1]),
            "sequence_last_time_utc": twelvedata_datetime_to_text(final_time),
            "sequence": sequence,
            "tested_tickers": tested
        }

    return {
        "name": name,
        "ticker": manual(),
        "source": "Twelve Data",
        "status": "error",
        "latest": manual(),
        "first_price": manual(),
        "last_price": manual(),
        "first_price_role": "sequence start price",
        "last_price_role": "latest available quote price or 5-minute close",
        "bar_interval": "mixed",
        "sequence_point_count": FINAL_POINTS,
        "sequence_rule": "first 23 points are Twelve Data 1-hour closes; last point is Twelve Data quote latest price or 5-minute close",
        "final_price_source": manual(),
        "sequence": manual(),
        "tested_tickers": tested,
        "error": "No valid Twelve Data time_series for selected symbols"
    }


def get_gold_focus_sequence():
    """
    Gold / XAUUSD 数据源优先级：
    1. Twelve Data XAU/USD
    2. Twelve Data XAUUSD
    3. Massive C:XAUUSD / XAUUSD / F:GC 备用

    目的：让黄金最新价更接近 XAU/USD 实时综合报价。
    """
    twelvedata_result = get_twelvedata_mixed_sequence(
        name="Spot Gold",
        symbol_candidates=[
            "XAU/USD",
            "XAUUSD"
        ],
        decimals=2
    )

    if twelvedata_result.get("status") == "ok":
        return twelvedata_result

    massive_result = get_massive_mixed_sequence(
        name="Spot Gold",
        tickers=[
            "C:XAUUSD",
            "XAUUSD",
            "F:GC"
        ],
        decimals=2
    )

    massive_tested = massive_result.get("tested_tickers", [])
    massive_result["tested_tickers"] = [
        {
            "source": "Twelve Data gold priority source",
            "status": twelvedata_result.get("status", "error"),
            "reason": twelvedata_result.get("error", ""),
            "tested_tickers": twelvedata_result.get("tested_tickers", [])
        }
    ] + massive_tested

    if massive_result.get("status") == "ok":
        massive_result["source"] = "Massive fallback after Twelve Data"
        massive_result["sequence_rule"] = "Twelve Data was unavailable; fallback uses Massive hourly closes plus latest available 5-minute close"
        return massive_result

    return {
        "name": "Spot Gold",
        "ticker": manual(),
        "source": "Twelve Data first, Massive fallback",
        "status": "error",
        "latest": manual(),
        "first_price": manual(),
        "last_price": manual(),
        "first_price_role": "sequence start price",
        "last_price_role": "latest available quote price or 5-minute close",
        "bar_interval": "mixed",
        "sequence_point_count": FINAL_POINTS,
        "sequence_rule": "Twelve Data first; Massive fallback",
        "final_price_source": manual(),
        "sequence": manual(),
        "tested_tickers": [
            {
                "source": "Twelve Data",
                "status": twelvedata_result.get("status", "error"),
                "reason": twelvedata_result.get("error", ""),
                "tested_tickers": twelvedata_result.get("tested_tickers", [])
            },
            {
                "source": "Massive",
                "status": massive_result.get("status", "error"),
                "reason": massive_result.get("error", ""),
                "tested_tickers": massive_result.get("tested_tickers", [])
            }
        ],
        "error": "Gold data unavailable from Twelve Data and Massive"
    }


def get_nasdaq_composite_focus_sequence():
    """
    Nasdaq Composite 数据源优先级：
    1. Yahoo Finance chart ^IXIC
    2. Massive Nasdaq Composite 候选 ticker 备用

    目的：避免 Massive 免费指数数据只返回昨日收盘，导致与 Google 当前 Nasdaq Composite 明显偏离。
    """
    yahoo_result = get_yahoo_mixed_index_sequence(
        name="Nasdaq Composite",
        yahoo_symbol="^IXIC",
        decimals=2
    )

    if yahoo_result.get("status") == "ok":
        yahoo_result["source"] = "Yahoo Finance chart"
        yahoo_result["sequence_rule"] = "first 23 points are Yahoo Finance ^IXIC 1-hour closes; last point is Yahoo Finance ^IXIC latest available 5-minute close or market close"
        return yahoo_result

    massive_result = get_massive_mixed_sequence(
        name="Nasdaq Composite",
        tickers=get_nasdaq_composite_candidates(),
        decimals=2
    )

    massive_tested = massive_result.get("tested_tickers", [])
    massive_result["tested_tickers"] = [
        {
            "source": "Yahoo Finance chart ^IXIC priority source",
            "status": yahoo_result.get("status", "error"),
            "reason": yahoo_result.get("error", ""),
            "tested_tickers": yahoo_result.get("tested_tickers", [])
        }
    ] + massive_tested

    if massive_result.get("status") == "ok":
        massive_result["source"] = "Massive fallback after Yahoo Finance chart"
        massive_result["sequence_rule"] = "Yahoo Finance ^IXIC was unavailable; fallback uses Massive Nasdaq Composite hourly closes plus latest available 5-minute close"
        return massive_result

    return {
        "name": "Nasdaq Composite",
        "ticker": manual(),
        "source": "Yahoo Finance chart first, Massive fallback",
        "status": "error",
        "latest": manual(),
        "first_price": manual(),
        "last_price": manual(),
        "first_price_role": "sequence start price",
        "last_price_role": "latest available 5-minute close or market close",
        "bar_interval": "mixed",
        "sequence_point_count": FINAL_POINTS,
        "sequence_rule": "Yahoo Finance ^IXIC first; Massive Nasdaq Composite fallback",
        "final_price_source": manual(),
        "sequence": manual(),
        "tested_tickers": [
            {
                "source": "Yahoo Finance chart ^IXIC",
                "status": yahoo_result.get("status", "error"),
                "reason": yahoo_result.get("error", ""),
                "tested_tickers": yahoo_result.get("tested_tickers", [])
            },
            {
                "source": "Massive",
                "status": massive_result.get("status", "error"),
                "reason": massive_result.get("error", ""),
                "tested_tickers": massive_result.get("tested_tickers", [])
            }
        ],
        "error": "Nasdaq Composite data unavailable from Yahoo Finance chart and Massive"
    }


def build_market_data():
    checked_at_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    today = datetime.now().strftime("%B %d")

    with ThreadPoolExecutor(max_workers=4) as executor:
        future_gold = executor.submit(
            get_gold_focus_sequence
        )

        future_stocks_focus = executor.submit(
            get_nasdaq_composite_focus_sequence
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
            "last_price_role": btc.get("last_price_role", "latest USD reference price from CoinGecko, CoinMarketCap, Coinbase, or Binance fallback"),
            "bar_interval": btc.get("bar_interval", "mixed"),
            "sequence_point_count": btc.get("sequence_point_count", FINAL_POINTS),
            "sequence_rule": btc.get("sequence_rule", "first 23 points use Coinbase 1-hour USD candles when available, otherwise Binance 1-hour closes; last point uses CoinGecko USD reference price, then CoinMarketCap, Coinbase ticker, and Binance fallback"),
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
            "last_price_role": eth.get("last_price_role", "latest USD reference price from CoinGecko, CoinMarketCap, Coinbase, or Binance fallback"),
            "bar_interval": eth.get("bar_interval", "mixed"),
            "sequence_point_count": eth.get("sequence_point_count", FINAL_POINTS),
            "sequence_rule": eth.get("sequence_rule", "first 23 points use Coinbase 1-hour USD candles when available, otherwise Binance 1-hour closes; last point uses CoinGecko USD reference price, then CoinMarketCap, Coinbase ticker, and Binance fallback"),
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
            "gold_source": gold.get("source", "Twelve Data first, Massive fallback"),
            "stocks_source": stocks_focus.get("source", "Yahoo Finance chart first, Massive fallback"),
            "global_indices_source": "Massive first, Yahoo Finance chart fallback",
            "btc_source": btc.get("source", "Multi-source"),
            "eth_source": eth.get("source", "Multi-source"),
            "market_time_checked": checked_at_utc,
            "sequence_rule": "mixed sequence: Gold uses Twelve Data first with Massive fallback; Nasdaq Composite uses Yahoo Finance chart ^IXIC first with Massive fallback; crypto uses Coinbase hourly USD candles first and Binance hourly candles as fallback; crypto final point uses CoinGecko, CoinMarketCap, Coinbase ticker, Binance ticker, then Binance 5-minute close fallback.",
            "stocks_focus_note": "Stocks focus uses Nasdaq Composite ^IXIC via Yahoo Finance chart first; Massive Nasdaq Composite candidates are fallback only. It does not use futures and does not use Nasdaq-100.",
            "gold_note": "Gold uses Twelve Data XAU/USD first to better match live XAU/USD reference pricing; Massive C:XAUUSD is fallback only.",
            "global_indices_note": "Global indices are available from /global-indices as lightweight snapshots. They are not fetched inside /market-data to avoid Render timeout.",
            "crypto_note": "BTC/USD and ETH/USD use Coinbase USD hourly candles first for the first 23 points, with Binance as hourly fallback. The final latest point uses CoinGecko first, then CoinMarketCap, Coinbase, and Binance fallback."
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



def get_twelvedata_index_quote(name, symbol_candidates, decimals=2):
    """
    Twelve Data 全球指数轻量概览。
    需要在 Render Environment Variables 里设置：
    TWELVEDATA_API_KEY

    返回：
    latest / previous_close / change / change_percent
    """
    if not TWELVEDATA_API_KEY:
        return {
            "name": name,
            "symbol": manual(),
            "source": "Twelve Data",
            "status": "error",
            "latest": manual(),
            "previous_close": manual(),
            "change": manual(),
            "change_percent": manual(),
            "market_time_utc": manual(),
            "tested_symbols": [],
            "error": "Missing TWELVEDATA_API_KEY"
        }

    tested_symbols = []

    for symbol in symbol_candidates:
        url = f"{TWELVEDATA_API_BASE}/quote"

        params = {
            "symbol": symbol,
            "apikey": TWELVEDATA_API_KEY
        }

        try:
            response = requests.get(url, params=params, timeout=6)
            data = response.json()

            status = data.get("status")
            message = data.get("message") or data.get("code") or ""

            if status == "error":
                tested_symbols.append({
                    "symbol": symbol,
                    "status": "error",
                    "reason": message
                })
                continue

            latest = data.get("close") or data.get("price") or data.get("last")
            previous_close = data.get("previous_close")
            change = data.get("change")
            change_percent = data.get("percent_change") or data.get("change_percent")
            datetime_text = data.get("datetime")
            timestamp_value = data.get("timestamp")

            if latest is None:
                tested_symbols.append({
                    "symbol": symbol,
                    "status": "error",
                    "reason": "Missing latest close"
                })
                continue

            latest_number = float(latest)

            if previous_close is not None:
                previous_number = float(previous_close)
            elif change is not None:
                previous_number = latest_number - float(change)
            else:
                previous_number = None

            if change is not None:
                change_number = float(change)
            elif previous_number is not None:
                change_number = latest_number - previous_number
            else:
                change_number = None

            if change_percent is not None:
                change_percent_number = float(change_percent)
            elif previous_number not in [None, 0] and change_number is not None:
                change_percent_number = (change_number / previous_number) * 100
            else:
                change_percent_number = None

            if timestamp_value:
                market_time_utc = utc_time_from_seconds(timestamp_value)
            elif datetime_text:
                market_time_utc = str(datetime_text)
            else:
                market_time_utc = manual()

            tested_symbols.append({
                "symbol": symbol,
                "status": "ok"
            })

            return {
                "name": name,
                "symbol": symbol,
                "source": "Twelve Data",
                "status": "ok",
                "latest": format_number(latest_number, decimals),
                "previous_close": format_number(previous_number, decimals) if previous_number is not None else manual(),
                "change": format_number(change_number, decimals) if change_number is not None else manual(),
                "change_percent": format_number(change_percent_number, 2) if change_percent_number is not None else manual(),
                "market_time_utc": market_time_utc,
                "data_rule": "latest index snapshot from Twelve Data quote",
                "tested_symbols": tested_symbols
            }

        except Exception as e:
            tested_symbols.append({
                "symbol": symbol,
                "status": "error",
                "reason": str(e)
            })

    return {
        "name": name,
        "symbol": manual(),
        "source": "Twelve Data",
        "status": "error",
        "latest": manual(),
        "previous_close": manual(),
        "change": manual(),
        "change_percent": manual(),
        "market_time_utc": manual(),
        "tested_symbols": tested_symbols,
        "error": "No valid Twelve Data quote"
    }


def get_twelvedata_global_indices_overview():
    """
    Twelve Data 全球指数概览。
    每个指数独立尝试多个候选 symbol。
    """
    index_map = {
        "sp500": {
            "name": "S&P 500",
            "symbols": [
                "SPX",
                "INX",
                "GSPC"
            ]
        },
        "dow_jones": {
            "name": "Dow Jones Industrial Average",
            "symbols": [
                "DJI",
                "DJIA"
            ]
        },
        "ftse_100": {
            "name": "FTSE 100",
            "symbols": [
                "FTSE",
                "UKX"
            ]
        },
        "nikkei_225": {
            "name": "Nikkei 225",
            "symbols": [
                "N225",
                "NI225"
            ]
        },
        "dax_40": {
            "name": "DAX 40",
            "symbols": [
                "DAX",
                "GDAXI"
            ]
        },
        "kospi": {
            "name": "KOSPI",
            "symbols": [
                "KS11",
                "KOSPI"
            ]
        }
    }

    output = {}

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {}

        for key, item in index_map.items():
            future = executor.submit(
                get_twelvedata_index_quote,
                item["name"],
                item["symbols"],
                2
            )
            futures[future] = key

        for future, key in futures.items():
            try:
                output[key] = future.result(timeout=10)
            except Exception as e:
                output[key] = {
                    "name": index_map[key]["name"],
                    "symbol": manual(),
                    "source": "Twelve Data",
                    "status": "error",
                    "latest": manual(),
                    "previous_close": manual(),
                    "change": manual(),
                    "change_percent": manual(),
                    "market_time_utc": manual(),
                    "error": str(e)
                }

    return output

def get_yahoo_quote_batch(symbol_map, timeout=6):
    """
    Yahoo Finance quote 批量轻量取数。
    用于 /global-indices：只拿 latest / previous_close / change / change_percent。
    symbol_map 示例：
    {
        "sp500": {"name": "S&P 500", "symbol": "^GSPC"}
    }
    """
    symbols = ",".join([item["symbol"] for item in symbol_map.values()])

    params = {
        "symbols": symbols
    }

    headers = {
        "User-Agent": "Mozilla/5.0 market-data-api/1.0"
    }

    try:
        response = requests.get(YAHOO_QUOTE_API_BASE, params=params, headers=headers, timeout=timeout)
        data = response.json()

        results = data.get("quoteResponse", {}).get("result", [])

        by_symbol = {}

        for item in results:
            symbol = item.get("symbol")
            if symbol:
                by_symbol[symbol] = item

        output = {}

        for key, info in symbol_map.items():
            symbol = info["symbol"]
            name = info["name"]
            item = by_symbol.get(symbol, {})

            latest = item.get("regularMarketPrice")
            previous_close = item.get("regularMarketPreviousClose")
            change = item.get("regularMarketChange")
            change_percent = item.get("regularMarketChangePercent")
            market_time = item.get("regularMarketTime")

            if latest is None:
                output[key] = {
                    "name": name,
                    "symbol": symbol,
                    "source": "Yahoo Finance quote",
                    "status": "error",
                    "latest": manual(),
                    "previous_close": manual(),
                    "change": manual(),
                    "change_percent": manual(),
                    "market_time_utc": manual(),
                    "error": "Missing regularMarketPrice"
                }
                continue

            try:
                latest_number = float(latest)
            except Exception:
                output[key] = {
                    "name": name,
                    "symbol": symbol,
                    "source": "Yahoo Finance quote",
                    "status": "error",
                    "latest": manual(),
                    "previous_close": manual(),
                    "change": manual(),
                    "change_percent": manual(),
                    "market_time_utc": manual(),
                    "error": "Invalid regularMarketPrice"
                }
                continue

            if previous_close is None and change is not None:
                try:
                    previous_close = latest_number - float(change)
                except Exception:
                    previous_close = None

            if change is None and previous_close is not None:
                try:
                    change = latest_number - float(previous_close)
                except Exception:
                    change = None

            if change_percent is None and previous_close not in [None, 0]:
                try:
                    change_percent = (float(change) / float(previous_close)) * 100
                except Exception:
                    change_percent = None

            output[key] = {
                "name": name,
                "symbol": symbol,
                "source": "Yahoo Finance quote",
                "status": "ok",
                "latest": format_number(latest_number, 2),
                "previous_close": format_number(previous_close, 2) if previous_close is not None else manual(),
                "change": format_number(change, 2) if change is not None else manual(),
                "change_percent": format_number(change_percent, 2) if change_percent is not None else manual(),
                "market_time_utc": utc_time_from_seconds(market_time) if market_time is not None else manual(),
                "data_rule": "latest index snapshot from Yahoo Finance quote"
            }

        return output

    except Exception as e:
        output = {}

        for key, info in symbol_map.items():
            output[key] = {
                "name": info["name"],
                "symbol": info["symbol"],
                "source": "Yahoo Finance quote",
                "status": "error",
                "latest": manual(),
                "previous_close": manual(),
                "change": manual(),
                "change_percent": manual(),
                "market_time_utc": manual(),
                "error": str(e)
            }

        return output



def get_global_index_focus():
    """
    全球指数按优先顺序取一个可用指数，节省 Twelve Data 免费额度。
    优先顺序：
    1. S&P 500
    2. Dow Jones
    3. FTSE 100
    4. Nikkei 225
    5. DAX 40
    6. KOSPI

    逻辑：
    - 每个指数默认只请求 1 个 Twelve Data symbol
    - 成功一个就立即停止，不继续请求后面的指数
    - 正常情况只消耗 1 次 API 请求
    - 极端情况最多消耗 6 次 API 请求
    """
    priority_list = [
        {
            "key": "sp500",
            "name": "S&P 500",
            "symbol": "GSPC"
        },
        {
            "key": "dow_jones",
            "name": "Dow Jones Industrial Average",
            "symbol": "DJI"
        },
        {
            "key": "ftse_100",
            "name": "FTSE 100",
            "symbol": "FTSE"
        },
        {
            "key": "nikkei_225",
            "name": "Nikkei 225",
            "symbol": "N225"
        },
        {
            "key": "dax_40",
            "name": "DAX 40",
            "symbol": "DAX"
        },
        {
            "key": "kospi",
            "name": "KOSPI",
            "symbol": "KOSPI"
        }
    ]

    tested_indices = []

    for item in priority_list:
        result = get_twelvedata_index_quote(
            name=item["name"],
            symbol_candidates=[
                item["symbol"]
            ],
            decimals=2
        )

        tested_indices.append({
            "key": item["key"],
            "name": item["name"],
            "symbol": item["symbol"],
            "status": result.get("status", "error"),
            "source": result.get("source", "Twelve Data"),
            "reason": result.get("error", "")
        })

        if result.get("status") == "ok":
            result["key"] = item["key"]
            result["selection_rule"] = "selected by priority order: S&P 500, Dow Jones, FTSE 100, Nikkei 225, DAX 40, KOSPI"
            result["tested_indices"] = tested_indices
            return result, tested_indices

    return {
        "name": manual(),
        "symbol": manual(),
        "source": "Twelve Data",
        "status": "error",
        "latest": manual(),
        "previous_close": manual(),
        "change": manual(),
        "change_percent": manual(),
        "market_time_utc": manual(),
        "selection_rule": "no valid index found by priority order",
        "tested_indices": tested_indices,
        "error": "No valid global index focus"
    }, tested_indices

def get_global_indices_overview():
    """
    全球指数轻量概览。
    优先 Twelve Data。
    如果 TWELVEDATA_API_KEY 未设置或某个指数失败，则 Yahoo Finance quote 作为临时兜底。
    """
    yahoo_symbol_map = {
        "sp500": {
            "name": "S&P 500",
            "symbol": "^GSPC"
        },
        "dow_jones": {
            "name": "Dow Jones Industrial Average",
            "symbol": "^DJI"
        },
        "ftse_100": {
            "name": "FTSE 100",
            "symbol": "^FTSE"
        },
        "nikkei_225": {
            "name": "Nikkei 225",
            "symbol": "^N225"
        },
        "dax_40": {
            "name": "DAX 40",
            "symbol": "^GDAXI"
        },
        "kospi": {
            "name": "KOSPI",
            "symbol": "^KS11"
        }
    }

    twelvedata_result = get_twelvedata_global_indices_overview()
    yahoo_result = {}

    needs_yahoo_fallback = False

    for value in twelvedata_result.values():
        if value.get("status") != "ok":
            needs_yahoo_fallback = True
            break

    if needs_yahoo_fallback:
        yahoo_result = get_yahoo_quote_batch(yahoo_symbol_map, timeout=6)

    output = {}

    for key, td_item in twelvedata_result.items():
        if td_item.get("status") == "ok":
            output[key] = td_item
            continue

        yahoo_item = yahoo_result.get(key)

        if yahoo_item and yahoo_item.get("status") == "ok":
            yahoo_item["fallback_used"] = "Yahoo Finance quote"
            yahoo_item["twelvedata_attempt"] = td_item.get("tested_symbols", [])
            output[key] = yahoo_item
        else:
            td_item["fallback_used"] = manual()

            if yahoo_item:
                td_item["fallback_error"] = yahoo_item.get("error", manual())
            else:
                td_item["fallback_error"] = "Yahoo fallback not available"

            output[key] = td_item

    return output




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
        global_index_focus, tested_indices = get_global_index_focus()

        response_data = {
            "date": datetime.now().strftime("%B %d"),
            "checked_at_utc": checked_at_utc,
            "global_index_focus": global_index_focus,
            "tested_indices": tested_indices,
            "data_rule": "global index focus only; priority order; no 24-point price sequences",
            "data_check": {
                "global_indices_source": "Twelve Data",
                "market_time_checked": checked_at_utc,
                "selection_rule": "Try S&P 500 first. If unavailable, try Dow Jones, FTSE 100, Nikkei 225, DAX 40, then KOSPI. Stop after the first successful quote.",
                "global_indices_note": "This endpoint is optimized for Twelve Data free limits. It returns one priority global index focus for text market overview, not six full index sequences.",
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
            "message": "Global index focus request failed and no cache is available."
        }), 500



if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
