from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/")
@app.route("/api/market-data")
@app.route("/market-data")
def market_data():
    return jsonify({
        "status": "ok",
        "message": "Render route test successful"
    })
