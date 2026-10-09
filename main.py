from flask import Flask
from threading import Thread
from bot import bot
from config import TOKEN, PORT

app = Flask(__name__)

@app.route("/")
def home():
    return "HybridBOT is online", 200

@app.route("/health")
def health():
    return {
        "status": "ok",
        "bot": str(bot.user) if bot.user else "starting"
    }, 200

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    bot.run(TOKEN)
