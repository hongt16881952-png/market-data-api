import os
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY", "").strip()
API_BASE = "https://api.massive.com"


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


def format_sequence(values, decimals=2, max_points=15):
    if not isinstance(values, list) or len(values) < 10:
        return manual()

    selected = values[-max_points:]

    result = []
    for value in selected:
        formatted = format_number(value, decimals)
        if formatted == manual():
            return manual()
        result.append(formatted)

    return ",".join(result)


def get_reference_tickers(search_text, market="indices", limit=20):
    if not MASSIVE_API_KEY:
        return []

    url = f"{API_BASE}/v3/reference/tickers"
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
    目标：只找 Nasdaq Composite 综合指数，不使用期指。
    规则：
    1. 优先通过 Massive reference tickers 搜索 Nasdaq Composite。
    2. 只接受 I: 开头的指数 ticker。
    3. 排除 NDX，因为 NDX 是 Nasdaq-100，不是 Composite。
    4. 如果搜索不到，再尝试常见 Nasdaq Composite ticker 写法。
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

        if "NDX" in ticker_upper or "NASDAQ-100" in name_upper or "NASDAQ 100" in name_upper:
            continue

        if "NASDAQ" in name_upper and "COMPOSITE" in name_upper:
            candidates.append(ticker)

    fallback = [
        "I:COMP",
        "I:IXIC",
        "I:COMPQ",
        "I:NASCOMP",
        "I:NASDAQCOMPOSITE"
    ]

    for ticker in fallback:
        if ticker not in candidates:
            candidates.append(ticker)

    return candidates


def get_aggs(ticker, decimals=2, multiplier=5, timespan="minute", days_back=7):
    """
    sequence 逻辑：
    - 第一个价格：该序列的起始价，或在数据源可确认时代表前收盘价附近的起点。
    - 最后一个价格：最新可确认的聚合K线收盘价；市场休市时代表最近可用收盘价。
    - 中间价格：全部来自 Massive aggregate bars，不自行生成。
    """

    if not MASSIVE_API_KEY:
        return {
            "ticker": ticker,
            "source": "Massive",
            "status": "error",
            "latest": manual(),
            "sequence": manual(),
            "error": "Missing MASSIVE_API_KEY"
        }

    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=days_back)

    url = f"{API_BASE}/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{start_date}/{today}"

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

        if not results or len(results) < 10:
            return {
                "ticker": ticker,
                "source": "Massive",
                "status": "error",
                "latest": manual(),
                "sequence": manual(),
                "raw": data
            }

        closes = []
        times = []

        for item in results:
            if "c" in item and item["c"] is not None:
                closes.append(item["c"])
                times.append(item.get("t"))

        if len(closes) < 10:
            return {
                "ticker": ticker,
                "source": "Massive",
                "status": "error",
                "latest": manual(),
                "sequence": manual(),
                "raw": data
            }

        selected_closes = closes[-15:]
        selected_times = times[-15:]

        sequence = format_sequence(selected_closes, decimals)
        latest_raw = selected_closes[-1]
        first_raw = selected_closes[0]

        latest_time = manual()
        first_time = manual()

        if selected_times[-1]:
            latest_time = datetime.fromtimestamp(
                selected_times[-1] / 1000,
                timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")

        if selected_times[0]:
            first_time = datetime.fromtimestamp(
                selected_times[0] / 1000,
                timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")

        return {
            "ticker": ticker,
            "source": "Massive",
            "status": "ok",
            "latest": format_number(latest_raw, decimals),
            "first_price": format_number(first_raw, decimals),
            "last_price": format_number(latest_raw, decimals),
            "first_price_role": "sequence start price or previous close when available",
            "last_price_role": "latest available close or market close",
            "bar_interval": f"{multiplier} {timespan}",
            "sequence": sequence,
            "sequence_start_time_utc": first_time,
            "sequence_last_time_utc": latest_time
        }

    except Exception as e:
        return {
            "ticker": ticker,
            "source": "Massive",
            "status": "error",
            "latest": manual(),
            "sequence": manual(),
            "error": str(e)
        }


def get_first_valid(name, tickers, decimals=2, multiplier=5, timespan="minute", days_back=7):
    tested = []

    for ticker in tickers:
        result = get_aggs(
            ticker=ticker,
            decimals=decimals,
            multiplier=multiplier,
            timespan=timespan,
            days_back=days_back
        )

        tested.append({
            "ticker": ticker,
            "status": result.get("status", "error")
        })

        if result.get("status") == "ok":
            result["name"] = name
            result["tested_tickers"] = tested
            return result

    return {
        "name": name,
        "ticker": manual(),
        "source": "Massive",
        "status": "error",
        "latest": manual(),
        "first_price": manual(),
        "last_price": manual(),
        "first_price_role": "sequence start price or previous close when available",
        "last_price_role": "latest available close or market close",
        "bar_interval": f"{multiplier} {timespan}",
        "sequence": manual(),
        "tested_tickers": tested
    }


@app.route("/")
@app.route("/api/market-data")
@app.route("/market-data")
def market_data():
    checked_at_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    today = datetime.now().strftime("%B %d")

    gold = get_first_valid(
        name="Spot Gold",
        tickers=[
            "C:XAUUSD",
            "XAUUSD",
            "F:GC"
        ],
        decimals=2,
        multiplier=5,
        timespan="minute",
        days_back=7
    )

    nasdaq_composite_candidates = get_nasdaq_composite_candidates()

    stocks_focus = get_first_valid(
        name="Nasdaq Composite",
        tickers=nasdaq_composite_candidates,
        decimals=2,
        multiplier=5,
        timespan="minute",
        days_back=7
    )

    btc = get_first_valid(
        name="Bitcoin",
        tickers=[
            "X:BTCUSD",
            "X:BTC-USD"
        ],
        decimals=0,
        multiplier=5,
        timespan="minute",
        days_back=7
    )

    eth = get_first_valid(
        name="Ethereum",
        tickers=[
            "X:ETHUSD",
            "X:ETH-USD"
        ],
        decimals=0,
        multiplier=5,
        timespan="minute",
        days_back=7
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
            "first_price_role": gold.get("first_price_role", "sequence start price or previous close when available"),
            "last_price_role": gold.get("last_price_role", "latest available close or market close"),
            "bar_interval": gold.get("bar_interval", "5 minute"),
            "sequence": gold.get("sequence", manual())
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
            "first_price_role": stocks_focus.get("first_price_role", "sequence start price or previous close when available"),
            "last_price_role": stocks_focus.get("last_price_role", "latest available close or market close"),
            "bar_interval": stocks_focus.get("bar_interval", "5 minute"),
            "sequence": stocks_focus.get("sequence", manual()),
            "tested_tickers": stocks_focus.get("tested_tickers", [])
        },

        "btc": {
            "name": "Bitcoin",
            "symbol": btc.get("ticker", manual()),
            "unit": "USD",
            "source": btc.get("source", "Massive"),
            "status": btc.get("status", "error"),
            "latest": btc.get("latest", manual()),
            "first_price": btc.get("first_price", manual()),
            "last_price": btc.get("last_price", manual()),
            "first_price_role": btc.get("first_price_role", "sequence start price or previous close when available"),
            "last_price_role": btc.get("last_price_role", "latest available close or market close"),
            "bar_interval": btc.get("bar_interval", "5 minute"),
            "sequence": btc.get("sequence", manual())
        },

        "eth": {
            "name": "Ethereum",
            "symbol": eth.get("ticker", manual()),
            "unit": "USD",
            "source": eth.get("source", "Massive"),
            "status": eth.get("status", "error"),
            "latest": eth.get("latest", manual()),
            "first_price": eth.get("first_price", manual()),
            "last_price": eth.get("last_price", manual()),
            "first_price_role": eth.get("first_price_role", "sequence start price or previous close when available"),
            "last_price_role": eth.get("last_price_role", "latest available close or market close"),
            "bar_interval": eth.get("bar_interval", "5 minute"),
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
            "btc_source": btc.get("source", "Massive"),
            "eth_source": eth.get("source", "Massive"),
            "market_time_checked": checked_at_utc,
            "stocks_focus_note": "Stocks focus uses Nasdaq Composite index candidates only, not futures."
        }
    }

    return jsonify(response_data)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
