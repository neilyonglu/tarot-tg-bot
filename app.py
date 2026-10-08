import asyncio
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from google import genai
from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

import tarot
from handlers import button_handler, handle_manual, handle_pwd, handle_status, handle_text_input, send_welcome

async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("start",  "🌙 重新開始占卜"),
        BotCommand("manual", "📖 使用手冊"),
        BotCommand("status", "📊 剩餘額度與 VIP 狀態"),
        BotCommand("pwd",    "🔓 解鎖 VIP 模式"),
    ])
    threading.Thread(target=run_dummy_server, daemon=True).start()


class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Tarot Master is awake!")

    def log_message(self, format, *args):
        pass


def run_bot() -> None:
    tarot.validate_config()
    tarot.client = genai.Client(api_key=tarot.GEMINI_API_KEY)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = (
        Application.builder()
        .token(tarot.TELEGRAM_TOKEN)
        .post_init(post_init)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start",  send_welcome))
    app.add_handler(CommandHandler("manual", handle_manual))
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("pwd",    handle_pwd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("--- 機器人啟動中 ---")
    app.run_polling(stop_signals=None, drop_pending_updates=True)


def run_dummy_server() -> None:
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    print(f"--- 輕量喚醒伺服器已在 Port {port} 對外開放 ---")
    server.serve_forever()


if __name__ == "__main__":
    run_bot()
