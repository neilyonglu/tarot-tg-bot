import asyncio
import json
import os
import random
import re
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from urllib.parse import urlparse

import requests
from google import genai
from PIL import Image
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ── Config ────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY")
SECRET_PASSWORD = os.environ.get("VIP_PASSWORD", "未設定密碼")
DAILY_LIMIT     = 7
CONTEXT_MAX_CHARS = 2000  # 追問記憶上限，超過則截掉最舊的部分

client = genai.Client(api_key=GEMINI_API_KEY)

with open("tarot_data.json", "r", encoding="utf-8") as f:
    TAROT_DATA = json.load(f)

LAYOUTS = {
    "draw_1":    (1, "單張",   ["核心指引"]),
    "draw_4":    (4, "四牌陣", ["現在心態", "過去事件", "現在事件", "未來事件"]),
    "draw_hexa": (7, "六芒星", ["過去狀況", "現在狀況", "未來發展", "對應策略", "周遭狀況", "問者態度", "最後結果"]),
}

READING_PROMPT = """\
你是一位精通萊德偉特體系的塔羅大師。
問題：「{question}」
牌陣：【{layout}】
結果：
{cards}

請結合牌面深度解析並給予建議。

【⚠️排版嚴格要求】：
請務必使用 Telegram 支援的 HTML 標籤進行排版：
- 粗體請使用 <b>你的文字</b>
- 斜體請使用 <i>你的文字</i>
- 底線請使用 <u>你的文字</u>
絕對不要使用任何 Markdown 語法（例如 **粗體**、*斜體* 或 # 標題）。
段落之間請直接換行即可，不需要使用 <br>。\
"""

MANUAL_TEXT = """\
<b>🔮 塔羅占卜大師 使用手冊</b>

<b>【基本使用】</b>
直接輸入你的問題，愈詳細愈好，大師將為你開啟占卜。

<b>【三種牌陣】</b>
🔮 <b>單張</b> — 快速解惑，抽 1 張核心指引牌
🎴 <b>四牌陣</b> — 深入分析，抽 4 張（現在心態／過去／現在／未來）
✡️ <b>六芒星</b> — 全面解析，抽 7 張（過去、現在、未來、對策、周遭、問者態度、最終結果）

<b>【追問功能】</b>
占卜完成後可直接輸入文字繼續追問，大師將結合牌面持續解析。
點擊「🔄 結束追問，開啟新占卜」重置記憶，開始全新問題。

<b>【使用限制】</b>
每日免費占卜 7 次，隔天自動重置。

<b>【VIP 模式】</b>
輸入 <code>/pwd 你的密碼</code> 解鎖無限次數占卜。

<b>【指令列表】</b>
/start — 重新開始，重置追問記憶
/manual — 查看此使用手冊
/pwd — 解鎖 VIP 無限模式\
"""

# ── Utilities ─────────────────────────────────────────────────────────────────

def _reset_daily_if_needed(user_data: dict) -> None:
    today = date.today().isoformat()
    if user_data.get("last_usage_date") != today:
        user_data["last_usage_date"] = today
        user_data["usage_count"] = 0


def get_remaining_uses(user_data: dict) -> int | None:
    """Returns remaining uses today, or None if unlimited."""
    if user_data.get("is_unlocked"):
        return None
    _reset_daily_if_needed(user_data)
    return DAILY_LIMIT - user_data.get("usage_count", 0)


def consume_usage(user_data: dict) -> bool:
    """Consumes one use. Returns False if daily limit reached."""
    if user_data.get("is_unlocked"):
        return True
    _reset_daily_if_needed(user_data)
    if user_data.get("usage_count", 0) >= DAILY_LIMIT:
        return False
    user_data["usage_count"] = user_data.get("usage_count", 0) + 1
    return True


def get_card_image(url: str, is_reversed: bool) -> BytesIO:
    filename = os.path.basename(urlparse(url).path)
    local_path = os.path.join("cards", filename)

    if os.path.exists(local_path):
        img = Image.open(local_path)
    else:
        # Fallback：本地圖檔遺失時從 Wikimedia 抓取
        headers = {"User-Agent": "TelegramTarotBot/1.0 (https://github.com/neilyonglu/tarot-tg-bot)"}
        for attempt in range(3):
            try:
                r = requests.get(url, headers=headers, timeout=15)
                r.raise_for_status()
                img = Image.open(BytesIO(r.content))
                break
            except requests.exceptions.RequestException as e:
                if attempt < 2:
                    print(f"抓圖失敗，重試中... ({e})")
                    time.sleep(1)
        else:
            raise Exception("無法連線到圖庫，請稍後再試。")

    if is_reversed:
        img = img.rotate(180)
    img.thumbnail((600, 800))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    bio = BytesIO()
    bio.name = "card.jpg"
    img.save(bio, "JPEG", quality=85)
    bio.seek(0)
    return bio


async def get_gemini_response(prompt: str) -> str:
    for attempt in range(3):
        try:
            return client.models.generate_content(model="gemini-2.5-flash", contents=prompt).text
        except Exception as e:
            if ("503" in str(e) or "429" in str(e)) and attempt < 2:
                print(f"⚠️ API 擁塞，等待後重試（第 {attempt + 2} 次）...")
                await asyncio.sleep(2)
                continue
            raise


async def safe_reply_with_html(message_obj, text: str, reply_markup=None) -> None:
    try:
        await message_obj.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
    except Exception:
        clean = re.sub(r"<[^>]+>", "", text)
        await message_obj.reply_text(clean, reply_markup=reply_markup)


async def _activate_vip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """解鎖 VIP：刪除含密碼的原訊息，再發送確認通知。"""
    context.user_data["is_unlocked"] = True
    try:
        await update.message.delete()
    except Exception:
        pass  # 私聊以外的場景可能沒有刪除權限
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🔓 密碼正確！\n大師為你開啟了「無限靈力模式」✨，現在可無限制占卜！請直接輸入你想問的問題。",
    )

# ── Handlers ──────────────────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("start",  "🌙 重新開始占卜"),
        BotCommand("manual", "📖 使用手冊"),
        BotCommand("pwd",    "🔓 解鎖 VIP 模式"),
    ])


async def send_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["is_follow_up_mode"] = False
    context.user_data["reading_context"] = ""
    await update.message.reply_text(
        "🌙 歡迎！請深呼吸，然後直接在此輸入你的問題，愈詳細愈好，並在心中默念3遍，我將為你開啟占卜。"
    )


async def handle_manual(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(MANUAL_TEXT, parse_mode="HTML")


async def handle_pwd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "💡 請使用格式：\n<code>/pwd 你的密碼</code>\n來解鎖大師的無限靈力。",
            parse_mode="HTML",
        )
        return

    if " ".join(context.args) == SECRET_PASSWORD:
        await _activate_vip(update, context)
    else:
        await update.message.reply_text("❌ 密碼錯誤，靈力封印未解除。")


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_text = update.message.text

    if user_text == SECRET_PASSWORD:
        await _activate_vip(update, context)
        return

    if context.user_data.get("is_follow_up_mode"):
        await handle_follow_up(update, context, user_text)
        return

    remaining = get_remaining_uses(context.user_data)
    if remaining is not None and remaining <= 0:
        await update.message.reply_text(
            "⏳ 每日免費 7 次已用完。\n💡 若是 VIP 請輸入「/pwd 你的密碼」解鎖無限模式！"
        )
        return

    limit_hint = f"\n(💡 今日抽牌額度剩餘：{remaining} 次)" if remaining is not None else ""
    context.user_data["question"] = user_text

    keyboard = [
        [InlineKeyboardButton("🔮 單張 (快速解惑)",              callback_data="draw_1")],
        [InlineKeyboardButton("🎴 四牌陣 (心態/過去/現在/未來)", callback_data="draw_4")],
        [InlineKeyboardButton("✡️ 六芒星 (深入分析與對策)",      callback_data="draw_hexa")],
    ]
    await update.message.reply_text(
        f"✅ 已感應問題：「{user_text}」{limit_hint}\n請選擇牌陣：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_follow_up(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str
) -> None:
    await update.message.reply_text("✨ 大師正在傾聽你的疑惑...")

    combined_question = (
        f"{context.user_data.get('question', '未知問題')}\n"
        f"(💡本次為後續追問，前情提要：{context.user_data.get('reading_context', '')}。"
        f"使用者最新追問：「{user_text}」)"
    )
    prompt = READING_PROMPT.format(
        question=combined_question,
        layout=context.user_data.get("layout_name", "塔羅牌陣"),
        cards="\n".join(context.user_data.get("card_results", ["無抽牌紀錄"])),
    )

    try:
        response_text = await get_gemini_response(prompt)

        new_entry = f"\n\n使用者追問：「{user_text}」\n大師回答：{response_text}"
        full_context = context.user_data["reading_context"] + new_entry
        if len(full_context) > CONTEXT_MAX_CHARS:
            full_context = "（前段對話已省略）\n" + full_context[-CONTEXT_MAX_CHARS:]
        context.user_data["reading_context"] = full_context

        reset_kb = [[InlineKeyboardButton("🔄 結束追問，開啟新占卜", callback_data="new_reading")]]
        await safe_reply_with_html(update.message, response_text, InlineKeyboardMarkup(reset_kb))
    except Exception as e:
        await update.message.reply_text(f"❌ 靈力中斷：{e}")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "new_reading":
        context.user_data["is_follow_up_mode"] = False
        context.user_data["reading_context"] = ""
        await query.message.reply_text(
            "🌙 記憶已重置。\n請直接輸入你【新的問題】，我將為你開啟全新的占卜。"
        )
        return

    if query.data not in LAYOUTS:
        return

    count, layout_name, positions = LAYOUTS[query.data]

    if not consume_usage(context.user_data):
        await query.edit_message_text("⏳ 大師今天的靈力已經耗盡囉！請輸入「/pwd 你的密碼」解鎖。")
        return

    await query.edit_message_text(f"🔮 佈下【{layout_name}】中，請稍候...")

    drawn_cards = random.sample(list(TAROT_DATA), count)
    card_results = []

    try:
        for card_name, pos_label in zip(drawn_cards, positions):
            is_reversed = random.choice([True, False])
            state = "逆位" if is_reversed else "正位"

            card_results.append(f"📍 {pos_label}: {card_name} ({state})")
            photo = get_card_image(TAROT_DATA[card_name], is_reversed)
            await query.message.reply_photo(
                photo=photo, caption=f"📍 【{pos_label}】: {card_name} ({state})"
            )
            await asyncio.sleep(1.5)

        await query.message.reply_text("✨ 所有牌面已揭曉。大師正在感應牌面連結，深度解析中...")

        context.user_data["layout_name"]  = layout_name
        context.user_data["card_results"] = card_results

        prompt = READING_PROMPT.format(
            question=context.user_data.get("question", "未指定問題"),
            layout=layout_name,
            cards="\n".join(card_results),
        )
        response_text = await get_gemini_response(prompt)

        context.user_data["is_follow_up_mode"] = True
        context.user_data["reading_context"]   = f"初次解析：\n{response_text}"

        await safe_reply_with_html(query.message, response_text)

        reset_kb = [[InlineKeyboardButton("🔄 結束追問，開啟新占卜", callback_data="new_reading")]]
        await safe_reply_with_html(
            query.message,
            "💡 <b>占卜完成。</b>\n如果你對某張牌有疑問，或想更深入了解，"
            "<b>請直接在此輸入文字追問</b>。\n\n或者點擊下方按鈕問全新的問題：",
            InlineKeyboardMarkup(reset_kb),
        )

    except Exception as e:
        await query.message.reply_text(f"❌ 靈力中斷：{e}")

# ── Server & Entry ────────────────────────────────────────────────────────────

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Tarot Master is awake!")

    def log_message(self, format, *args):
        pass


def run_bot() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start",  send_welcome))
    app.add_handler(CommandHandler("manual", handle_manual))
    app.add_handler(CommandHandler("pwd",    handle_pwd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("--- 機器人已在背景啟動 ---")
    app.run_polling(stop_signals=None, drop_pending_updates=True)


def run_dummy_server() -> None:
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    print(f"--- 輕量喚醒伺服器已在 Port {port} 對外開放 ---")
    server.serve_forever()


if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    run_dummy_server()
