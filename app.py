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


def format_sequence(values, decimals=2):
    if not isinstance(values, list) or len(values) < 10:
        return manual()

    result = []

    for value in values[-15:]:
        try:
            number = float(value)

            if decimals == 0:
                result.append(str(int(round(number))))
            else:
                result.append(f"{number:.{decimals}f}")

        except Exception:
            return manual()

    return ",".join(result)


def format_latest(value, decimals=2):
    try:
        number = float(value)

        if decimals == 0:
            return str(int(round(number)))
        else:
            return f"{number:.{decimals}f}"

    except Exception:
        return manual()


def get_aggs(ticker, decimals=2, multiplier=5, timespan="minute", days_back=7):
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

        for item in results:
            if "c" in item and item["c"] is not None:
                closes.append(item["c"])

        if len(closes) < 10:
            return {
                "ticker": ticker,
                "source": "Massive",
                "status": "error",
                "latest": manual(),
                "sequence": manual(),
                "raw": data
            }

        latest_raw = closes[-1]
        latest_time = datetime.fromtimestamp(
            results[-1]["t"] / 1000,
            timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")

        return {
            "ticker": ticker,
            "source": "Massive",
            "status": "ok",
            "latest": format_latest(latest_raw, decimals),
            "sequence": format_sequence(closes, decimals),
            "time_utc": latest_time
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


def get_first_valid(name, tickers, decimals=2):
    tested = []

    for ticker in tickers:
        result = get_aggs(ticker, decimals=decimals)

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
        decimals=2
    )

    stocks_focus = get_first_valid(
        name="Stocks Focus",
        tickers=[
            "I:NDX",
            "I:SPX",
            "I:DJI"
        ],
        decimals=2
    )

    btc = get_first_valid(
        name="Bitcoin",
        tickers=[
            "X:BTCUSD",
            "X:BTC-USD"
        ],
        decimals=0
    )

    eth = get_first_valid(
        name="Ethereum",
        tickers=[
            "X:ETHUSD",
            "X:ETH-USD"
        ],
        decimals=0
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
            "sequence": gold.get("sequence", manual())
        },

        "stocks_focus": {
            "name": stocks_focus.get("name", "Stocks Focus"),
            "symbol": stocks_focus.get("ticker", manual()),
            "unit": "Index Points",
            "source": stocks_focus.get("source", "Massive"),
            "status": stocks_focus.get("status", "error"),
            "latest": stocks_focus.get("latest", manual()),
            "sequence": stocks_focus.get("sequence", manual())
        },

        "btc": {
            "name": "Bitcoin",
            "symbol": btc.get("ticker", manual()),
            "unit": "USD",
            "source": btc.get("source", "Massive"),
            "status": btc.get("status", "error"),
            "latest": btc.get("latest", manual()),
            "sequence": btc.get("sequence", manual())
        },

        "eth": {
            "name": "Ethereum",
            "symbol": eth.get("ticker", manual()),
            "unit": "USD",
            "source": eth.get("source", "Massive"),
            "status": eth.get("status", "error"),
            "latest": eth.get("latest", manual()),
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
            "market_time_checked": checked_at_utc
        }
    }

    return jsonify(response_data)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
