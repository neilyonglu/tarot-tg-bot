import asyncio
import html
import json
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from pathlib import Path
from secrets import token_hex
from urllib.parse import urlparse

import requests
from google import genai
from PIL import Image
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
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
SECRET_PASSWORD = os.environ.get("VIP_PASSWORD", "").strip()
if SECRET_PASSWORD.startswith("replace-with-"):
    SECRET_PASSWORD = ""
GEMINI_MODEL_PRIMARY  = "gemini-3.1-flash-lite"
GEMINI_MODEL_FALLBACK = "gemini-2.5-flash"
DAILY_LIMIT     = 7
CONTEXT_MAX_CHARS = 2000  # Follow-up context limit; discard the oldest text when exceeded.
BASE_DIR = Path(__file__).resolve().parent
TAIWAN_TIME = timezone(timedelta(hours=8))

client = None

with (BASE_DIR / "tarot_data.json").open(encoding="utf-8") as f:
    TAROT_DATA = json.load(f)

LAYOUTS = {
    "draw_1":    (1, "單張",   ["核心指引"]),
    "draw_4":    (4, "四牌陣", ["現在心態", "過去事件", "現在事件", "未來事件"]),
    "draw_hexa": (7, "六芒星", ["過去狀況", "現在狀況", "未來發展", "對應策略", "周遭狀況", "問者態度", "最後結果"]),
}

# ── Prompts ───────────────────────────────────────────────────────────────────

# === FULL: for users to copy into their own LLM (ChatGPT/Claude/Gemini Pro, etc.) ===
# Detailed instructions with unrestricted Markdown formatting.
READING_PROMPT_FULL = """\
這是一份塔羅占卜解讀請求。請扮演專業的塔羅諮詢師，使用萊德偉特體系（Rider-Waite-Smith）。你的任務不是預測未來，而是透過牌面協助問者看清當下處境、盲點與選擇空間。

【內部思考與分析（絕對不要輸出到回覆中）】
在產生正式回覆前，請先在內部完成以下分析（不要將任何「步驟一」、「逐牌定位」或條列式的分析過程印在給問者的回覆中）：
1. 逐牌定位：分析各牌面在該牌位上的核心象徵、正逆位差異與圖像細節。
2. 跨牌觀察：觀察大阿爾克那比例、花色集中度、逆位比例及宮廷牌暗示的狀態。

【占卜基本資訊】
問者問題：{question}
使用牌陣：{layout}
*牌陣位置意義參考：
- 單張占卜：針對問題的核心指引或整體狀態。
- 四牌陣：1. 現在心態 / 2. 過去事件 / 3. 現在事件 / 4. 未來發展（順著目前軌跡發展的可能走向）。
- 六芒星牌陣：1. 過去 / 2. 現在 / 3. 未來 / 4. 具體建議 / 5. 周遭環境與他人影響 / 6. 問者的潛意識態度 / 7. 最終可能結果。

抽牌結果：
{cards}

【給問者的正式回覆要求】
1. 自然對話：請以有經驗、具備同理心但不失理性的諮詢師口吻，寫出一段自然流暢的解讀文章，就像面對面跟朋友對話一樣。
2. 融入牌意：直接綜合你的內部分析來回應問者的問題。請順暢地將牌名、正逆位與圖像細節化為具體的敘述帶入文中。（例如：「你抽到的寶劍九正位，那個在深夜驚醒的畫面，很符合你現在對面試的焦慮...」）
3. 具體建議：在文末給予 2-3 點問者能實際執行或反思的具體建議，建議必須有前文的牌面支撐，具備可操作性。
4. 禁用語彙與限制：不對醫療、法律、財務做具體建議；不做具體的鐵口直斷預言（如「你下個月一定會...」）；避免使用「能量」、「宇宙」等空泛詞彙與心靈雞湯。

現在，請直接輸出你給問者的解讀回覆：\
"""

# === LITE: for the built-in Gemini Flash Lite model ===
# Concise instructions requiring Telegram HTML and prohibiting Markdown.
READING_PROMPT_LITE = """\
你是專業的塔羅諮詢師，使用萊德偉特體系（RWS）。
你的任務不是預測未來，而是透過牌面協助問者釐清當下處境與選擇空間。

【內部思考與分析（絕對不要輸出到回覆中）】
在產生回覆前，請先在內部評估以下資訊，不要將「步驟」或「逐牌分析」條列印出來：
1. 將每一張牌嚴格對應到該「牌陣」與「牌位」的意義。
2. 結合該牌的核心象徵、正逆位與圖像細節。
3. 觀察大阿爾克那比例、花色集中、逆位暗示等跨牌張力。

【占卜基本資訊】
問題：{question}
牌陣：{layout}
*牌陣位置意義參考：
- 單張：核心指引。
- 四牌陣：1. 現在心態 / 2. 過去事件 / 3. 現在事件 / 4. 未來發展。
- 六芒星：1. 過去 / 2. 現在 / 3. 未來 / 4. 具體建議 / 5. 周遭環境 / 6. 問者態度 / 7. 最終結果。

抽牌結果：
{cards}

【給問者的正式回覆要求】
1. 自然對話：請以溫和、專業的諮詢師口吻，寫出一段自然流暢的解讀文章。
2. 融入牌意：直接回應問題。順暢地將牌名、正逆位與細節化為敘述帶入文中（例如：「你抽到的寶劍九正位，畫面中的焦慮呼應了...」）。
3. 具體建議：文末給 2-3 點有牌面支撐的具體可操作建議。

【排版要求（非常嚴格）】
請務必使用 Telegram HTML 標籤：
- 粗體 <b>文字</b>
- 斜體 <i>文字</i>
- 底線 <u>文字</u>
絕對不要使用任何 Markdown 語法（禁止使用 **粗體**、*斜體*、# 標題、- 條列符號）。
段落之間直接換行即可，不要使用 <br>。
若需條列，請使用全形「・」或數字「1. 2. 3.」。

【禁止事項】
不做具體時間預言；不對特定他人人格評斷；不給醫療/法律/財務具體建議。
若問者透露自殘或嚴重憂鬱訊號，先溫和提醒尋求專業協助再解讀。
禁用「能量」、「宇宙」等空泛詞彙與心靈雞湯。

現在，請直接輸出你給問者的解讀回覆：\
"""

# === Follow-up: responses use the built-in model ===
FOLLOW_UP_PROMPT = """\
你是專業的塔羅諮詢師，正在以自然、對話的口吻回應問者對先前牌陣解讀的追問。

【占卜上下文】
最初的問題：{question}
當時抽到的牌（{layout}）：
{cards}

先前的解讀脈絡：
{reading_context}

問者最新的追問：
{follow_up}

【給問者的正式回覆要求】
1. 聚焦解答：針對追問本身回答，不要重新把整副牌解讀一遍。
2. 牌面佐證：回答必須自然地引用具體某張牌或某個牌面元素作為依據（例如：「就如同剛才那張星幣二逆位提醒的...」），這是占卜與一般聊天的差別。
3. 誠實界線：若追問已偏離牌面能回答的範圍，誠實指出並建議是否需要重新抽牌；若追問是補充新資訊，請重新評估牌面在新脈絡下的意義。
4. 自然對話：維持有經驗諮詢師的語氣，不要列出「步驟」或「分析過程」。

【排版要求（非常嚴格）】
請務必使用 Telegram HTML 標籤：
- 粗體 <b>文字</b>
- 斜體 <i>文字</i>
- 底線 <u>文字</u>
絕對不要使用任何 Markdown 語法（禁止使用 **粗體**、*斜體*、# 標題、- 條列符號）。
段落之間直接換行即可，不要使用 <br>。

【禁止事項】
不做具體時間預言；不對特定他人人格評斷；不給醫療/法律/財務具體建議。
禁用「能量」、「宇宙」等空泛詞彙與心靈雞湯。

現在，請直接輸出你的回覆：\
"""

MANUAL_TEXT = """\
<b>🔮 塔羅占卜大師 使用手冊</b>

<b>【基本使用】</b>
直接輸入你的問題，愈詳細愈好，大師將為你開啟占卜。

<b>【三種牌陣】</b>
🔮 <b>單張</b> — 快速解惑，抽 1 張核心指引牌
🎴 <b>四牌陣</b> — 深入分析，抽 4 張(現在心態／過去／現在／未來)
✡️ <b>六芒星</b> — 全面解析，抽 7 張(過去、現在、未來、對策、周遭、問者態度、最終結果)

<b>【兩種解讀模式】</b>
抽完牌後可選擇：
📋 <b>複製完整 Prompt</b> — 適合有自己 LLM 額度（ChatGPT、Claude 等）的使用者，可獲得最完整深度的解讀
🔮 <b>內建大師解析</b> — 直接由本機器人解讀，方便快速

<b>【追問功能】</b>
解讀完成後可直接輸入文字繼續追問，由內建大師結合牌面持續解析。
點擊「🔄 結束追問，開啟新占卜」重置記憶，開始全新問題。

<b>【使用限制】</b>
每日免費占卜 7 次，台灣時間午夜重置（每次抽牌計 1 次，無論選擇哪種解讀模式）。

<b>【VIP 模式】</b>
輸入 <code>/pwd 你的密碼</code> 解鎖無限次數占卜。

<b>【指令列表】</b>
/start — 重新開始，重置追問記憶
/manual — 查看此使用手冊
/status — 查看剩餘抽牌額度與 VIP 狀態
/pwd — 解鎖 VIP 無限模式\
"""

# ── Utilities ─────────────────────────────────────────────────────────────────

def _reset_daily_if_needed(user_data: dict) -> None:
    today = datetime.now(TAIWAN_TIME).date().isoformat()
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
    local_path = BASE_DIR / "cards" / filename

    if os.path.exists(local_path):
        img = Image.open(local_path)
    else:
        # Fetch from Wikimedia if the local image is missing.
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
    """Try the primary model, then fall back on persistent 503/429 errors.

    1. Retry the primary model once after a two-second delay on 503/429.
    2. If both attempts fail, try the fallback model up to twice.
    3. Raise other errors immediately without retrying or falling back.
    """
    models = [GEMINI_MODEL_PRIMARY, GEMINI_MODEL_FALLBACK]

    for model_idx, model in enumerate(models):
        is_last_model = (model_idx == len(models) - 1)

        for attempt in range(2):
            try:
                response = await client.aio.models.generate_content(model=model, contents=prompt)
                if not response.text:
                    raise RuntimeError("AI 沒有回傳解讀，請複製 Prompt 自行解析。")
                return response.text
            except Exception as e:
                err_str = str(e)
                is_throttle = "503" in err_str or "429" in err_str

                # Raise other errors without retrying or falling back.
                if not is_throttle:
                    raise

                is_last_attempt = (attempt == 1)

                if not is_last_attempt:
                    # Retry the same model once.
                    print(f"⚠️ {model} 擁塞，等待 2 秒後重試...")
                    await asyncio.sleep(2)
                    continue

                # The final attempt for this model also failed.
                if is_last_model:
                    raise
                print(f"⚠️ 主模型 {model} 持續擁塞，切換到備用模型 {models[model_idx + 1]}...")
                break  # Leave the inner loop and try the next model.


async def safe_reply_with_html(message_obj, text: str, reply_markup=None) -> None:
    if len(text.encode("utf-16-le")) <= 8000:
        try:
            await message_obj.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
            return
        except BadRequest as error:
            if "parse entities" not in str(error).lower():
                raise
    # ponytail: long replies use plain text; use Telegram entities if rich formatting is needed.
    clean = html.unescape(re.sub(r"<[^>]+>", "", text))
    for i in range(0, len(clean), 2000):
        await message_obj.reply_text(
            clean[i:i + 2000],
            reply_markup=reply_markup if i + 2000 >= len(clean) else None,
        )


def _reset_reading(user_data: dict) -> None:
    user_data.update(
        reading_id=token_hex(8), is_follow_up_mode=False, reading_context="",
        selection_pending=False, mode_pending=False,
        question="", layout_name="", card_results=[],
    )


def _reading_callback(context, action: str) -> str:
    return f"{action}:{context.user_data['reading_id']}"


async def _activate_vip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unlock VIP, delete the password message, and send confirmation."""
    context.user_data["is_unlocked"] = True
    try:
        await update.message.delete()
    except Exception:
        pass  # Deletion permission may be unavailable outside private chats.
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="🔓 密碼正確！\n大師為你開啟了「無限靈力模式」✨，現在可無限制占卜！請直接輸入你想問的問題。",
    )

# ── Handlers ──────────────────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("start",  "🌙 重新開始占卜"),
        BotCommand("manual", "📖 使用手冊"),
        BotCommand("status", "📊 剩餘額度與 VIP 狀態"),
        BotCommand("pwd",    "🔓 解鎖 VIP 模式"),
    ])


async def send_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _reset_reading(context.user_data)
    await update.message.reply_text(
        "🌙 歡迎！請深呼吸，然後直接在此輸入你的問題，愈詳細愈好，並在心中默念3遍，我將為你開啟占卜。"
    )


async def handle_manual(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(MANUAL_TEXT, parse_mode="HTML")


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remaining = get_remaining_uses(context.user_data)
    vip = "已解鎖" if remaining is None else "未解鎖"
    quota = "無限制" if remaining is None else f"{remaining} / {DAILY_LIMIT} 次"
    await update.message.reply_text(
        f"📊 今日剩餘抽牌額度：{quota}\nVIP：{vip}\n額度於台灣時間每日 00:00 重置。"
    )


async def handle_pwd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not SECRET_PASSWORD:
        await update.message.reply_text("VIP 解鎖目前未開放。")
        return
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

    if SECRET_PASSWORD and user_text == SECRET_PASSWORD:
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
    _reset_reading(context.user_data)
    context.user_data["question"] = user_text
    context.user_data["selection_pending"] = True

    keyboard = [
        [InlineKeyboardButton("🔮 單張 (快速解惑)",              callback_data=_reading_callback(context, "draw_1"))],
        [InlineKeyboardButton("🎴 四牌陣 (心態/過去/現在/未來)", callback_data=_reading_callback(context, "draw_4"))],
        [InlineKeyboardButton("✡️ 六芒星 (深入分析與對策)",      callback_data=_reading_callback(context, "draw_hexa"))],
    ]
    await safe_reply_with_html(
        update.message,
        f"✅ 已感應問題：「{html.escape(user_text)}」{limit_hint}\n請選擇牌陣：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_follow_up(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str
) -> None:
    """Use FOLLOW_UP_PROMPT to answer follow-ups without rereading all cards."""
    await update.message.reply_text("✨ 大師正在傾聽你的疑惑...")

    prompt = FOLLOW_UP_PROMPT.format(
        question=context.user_data.get("question", "未知問題"),
        layout=context.user_data.get("layout_name", "塔羅牌陣"),
        cards="\n".join(context.user_data.get("card_results", ["無抽牌紀錄"])),
        reading_context=context.user_data.get(
            "reading_context", "（無先前脈絡，使用者可能在自己的 LLM 進行了解讀）"
        ) or "（無先前脈絡）",
        follow_up=user_text,
    )

    try:
        response_text = await get_gemini_response(prompt)

        new_entry = f"\n\n使用者追問：「{user_text}」\n大師回答：{response_text}"
        full_context = context.user_data.get("reading_context", "") + new_entry
        if len(full_context) > CONTEXT_MAX_CHARS:
            full_context = "（前段對話已省略）\n" + full_context[-CONTEXT_MAX_CHARS:]
        context.user_data["reading_context"] = full_context

        reset_kb = [[InlineKeyboardButton("🔄 結束追問，開啟新占卜", callback_data=_reading_callback(context, "new_reading"))]]
        await safe_reply_with_html(update.message, response_text, InlineKeyboardMarkup(reset_kb))
    except Exception as e:
        print(f"Follow-up failed: {e}")
        await update.message.reply_text("❌ 內建解析暫時無法使用，可複製 Prompt 到自己的 LLM 繼續解析。")
        await _present_mode_selection(update.message, context)


# ── Mode handlers (after cards drawn) ─────────────────────────────────────────

async def _present_mode_selection(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the copy-prompt and built-in reading buttons after drawing cards."""
    mode_kb = [
        [InlineKeyboardButton("📋 複製完整 Prompt 自行解析", callback_data=_reading_callback(context, "mode_copy"))],
        [InlineKeyboardButton("🔮 用內建大師解析",            callback_data=_reading_callback(context, "mode_builtin"))],
    ]
    sent = await message.reply_text(
        "✨ 牌已揭曉，請選擇解讀方式：\n\n"
        "📋 <b>複製 Prompt</b>\n"
        "取得高品質提示詞，貼到你自己的 LLM(ChatGPT、Claude、Gemini Pro 等)。"
        "適合想要更深入解讀、或希望節省機器人額度的使用者。\n\n"
        "🔮 <b>內建大師</b>\n"
        "由本機器人內建模型直接解析，方便快速。",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(mode_kb),
    )
    context.user_data["mode_pending"] = (sent.chat_id, sent.message_id)


async def _handle_mode_copy(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the full prompt in a <pre> block for easy copying."""
    try:
        full_prompt = READING_PROMPT_FULL.format(
            question=context.user_data.get("question", "未指定問題"),
            layout=context.user_data.get("layout_name", ""),
            cards="\n".join(context.user_data.get("card_results", [])),
        )

        await query.message.reply_text("📋 完整 Prompt 如下；若分成多則訊息，請依序複製到自己的 LLM：")
        await safe_reply_with_html(query.message, f"<pre>{html.escape(full_prompt)}</pre>")

        reset_kb = [[InlineKeyboardButton("🔄 結束，開啟新占卜", callback_data=_reading_callback(context, "new_reading"))]]
        await query.message.reply_text(
            "✅ Prompt 已生成。\n\n"
            "💡 將上方內容貼到 ChatGPT、Claude 或 Gemini 等任何 LLM，即可獲得完整解讀。\n"
            "若想針對這次牌組做進一步追問，<b>可直接在這邊輸入文字</b>，將由內建大師回應。",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(reset_kb),
        )

        context.user_data["is_follow_up_mode"] = True
        context.user_data["reading_context"] = ""

    except Exception as e:
        await query.message.reply_text(f"❌ 產生 Prompt 時發生錯誤：{e}")
        await _present_mode_selection(query.message, context)

async def _handle_mode_builtin(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the LITE prompt with the built-in Gemini Flash Lite model."""
    await query.message.reply_text("✨ 大師正在感應牌面連結，深度解析中...")

    prompt = READING_PROMPT_LITE.format(
        question=context.user_data.get("question", "未指定問題"),
        layout=context.user_data.get("layout_name", ""),
        cards="\n".join(context.user_data.get("card_results", [])),
    )

    try:
        response_text = await get_gemini_response(prompt)

        context.user_data["is_follow_up_mode"] = True
        context.user_data["reading_context"]   = f"初次解析：\n{response_text}"

        await safe_reply_with_html(query.message, response_text)

        reset_kb = [[InlineKeyboardButton("🔄 結束追問，開啟新占卜", callback_data=_reading_callback(context, "new_reading"))]]
        await safe_reply_with_html(
            query.message,
            "💡 <b>占卜完成。</b>\n如果你對某張牌有疑問，或想更深入了解，"
            "<b>請直接在此輸入文字追問</b>。\n\n或者點擊下方按鈕問全新的問題：",
            InlineKeyboardMarkup(reset_kb),
        )
    except Exception as e:
        print(f"Built-in reading failed: {e}")
        await query.message.reply_text("❌ 內建解析暫時無法使用，可複製 Prompt 到自己的 LLM 自行解析。")
        await _present_mode_selection(query.message, context)


# ── Layout selection (cards drawing) ─────────────────────────────────────────

async def _handle_layout_selection(query, context: ContextTypes.DEFAULT_TYPE, layout_key: str) -> None:
    """Draw cards, send their images, and ask the user to choose a reading mode."""
    count, layout_name, positions = LAYOUTS[layout_key]

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
            photo = await asyncio.to_thread(get_card_image, TAROT_DATA[card_name], is_reversed)
            try:
                await query.message.reply_photo(
                    photo=photo, caption=f"📍 【{pos_label}】: {card_name} ({state})"
                )
            finally:
                photo.close()
            await asyncio.sleep(1.5)

        # Store the drawn cards for the selected reading mode.
        context.user_data["layout_name"]  = layout_name
        context.user_data["card_results"] = card_results

        # Show the reading mode buttons.
        await _present_mode_selection(query.message, context)

    except Exception as e:
        await query.message.reply_text(f"❌ 靈力中斷：{e}")


# ── Main button dispatcher ────────────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, _, reading_id = (query.data or "").partition(":")
    if not reading_id or reading_id != context.user_data.get("reading_id"):
        await query.answer("這個按鈕已失效，請使用目前的占卜按鈕。", show_alert=True)
        return
    if action in LAYOUTS:
        if not context.user_data.pop("selection_pending", False):
            await query.answer("這次占卜已經抽過牌了。", show_alert=True)
            return
    elif action in ("mode_copy", "mode_builtin"):
        if context.user_data.get("mode_pending") != (query.message.chat_id, query.message.message_id):
            await query.answer("這個選項已處理，請使用最新的按鈕。", show_alert=True)
            return
        context.user_data["mode_pending"] = None
    elif action != "new_reading":
        await query.answer("不支援的選項。", show_alert=True)
        return
    await query.answer()

    if action == "new_reading":
        _reset_reading(context.user_data)
        await query.message.reply_text(
            "🌙 記憶已重置。\n請直接輸入你【新的問題】，我將為你開啟全新的占卜。"
        )
        return

    if action == "mode_copy":
        await _handle_mode_copy(query, context)
        return

    if action == "mode_builtin":
        await _handle_mode_builtin(query, context)
        return

    if action in LAYOUTS:
        await _handle_layout_selection(query, context, action)
        return

# ── Server & Entry ────────────────────────────────────────────────────────────

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Tarot Master is awake!")

    def log_message(self, format, *args):
        pass


def validate_config() -> None:
    missing = [
        name for name, value in (("TELEGRAM_TOKEN", TELEGRAM_TOKEN), ("GEMINI_API_KEY", GEMINI_API_KEY))
        if not value or not value.strip() or value.startswith("replace-with-")
    ]
    if missing:
        raise RuntimeError(f"請設定有效的 {', '.join(missing)}，再執行 uv run --env-file .env python app.py")
    try:
        port = int(os.environ.get("PORT", 10000))
    except ValueError:
        raise RuntimeError("PORT 必須是 1 到 65535 的整數。") from None
    if not 1 <= port <= 65535:
        raise RuntimeError("PORT 必須是 1 到 65535 的整數。")


def run_bot() -> None:
    global client
    validate_config()
    client = genai.Client(api_key=GEMINI_API_KEY)
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
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("pwd",    handle_pwd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    app.add_handler(CallbackQueryHandler(button_handler))

    threading.Thread(target=run_dummy_server, daemon=True).start()
    print("--- 機器人啟動中 ---")
    app.run_polling(stop_signals=None, drop_pending_updates=True)


def run_dummy_server() -> None:
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), PingHandler)
    print(f"--- 輕量喚醒伺服器已在 Port {port} 對外開放 ---")
    server.serve_forever()


if __name__ == "__main__":
    run_bot()
