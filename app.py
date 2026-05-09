import asyncio
import html
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
GEMINI_MODEL_PRIMARY  = "gemini-3.1-flash-lite"
GEMINI_MODEL_FALLBACK = "gemini-2.5-flash"
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

# ── Prompts ───────────────────────────────────────────────────────────────────

# === FULL 版：給使用者複製到自己的 LLM (ChatGPT/Claude/Gemini Pro 等) ===
# 不限制 Markdown，讓對方 LLM 自然格式化；指令更完整。
READING_PROMPT_FULL = """\
這是一份塔羅占卜解讀請求。請扮演專業的塔羅諮詢師，使用萊德偉特體系（Rider-Waite-Smith）。你的任務不是預測未來，而是透過牌面協助問者看清當下處境、盲點與選擇空間。

【內部思考與分析（絕對不要輸出到回覆中）】
在產生正式回覆前，請先在內部完成以下分析（不要將任何「步驟一」、「逐牌定位」或條列式的分析過程印在給問者的回覆中）：
1. 逐牌定位：分析各牌面在該牌位上的核心象徵、正逆位差異與圖像細節。
2. 跨牌觀察：觀察大阿爾克那比例、花色集中度、逆位比例及宮廷牌暗示的狀態。

【占卜基本資訊】
問者問題：{question}
使用牌陣：{spread_name}
*牌陣位置意義參考：
- 單張占卜：針對問題的核心指引或整體狀態。
- 四牌陣：1. 現在心態 / 2. 過去事件 / 3. 現在事件 / 4. 未來發展（順著目前軌跡發展的可能走向）。
- 六芒星牌陣：1. 過去 / 2. 現在 / 3. 未來 / 4. 具體建議 / 5. 周遭環境與他人影響 / 6. 問者的潛意識態度 / 7. 最終可能結果。

抽牌結果：
{cards_drawn}

【給問者的正式回覆要求】
1. 自然對話：請以有經驗、具備同理心但不失理性的諮詢師口吻，寫出一段自然流暢的解讀文章，就像面對面跟朋友對話一樣。
2. 融入牌意：直接綜合你的內部分析來回應問者的問題。請順暢地將牌名、正逆位與圖像細節化為具體的敘述帶入文中。（例如：「你抽到的寶劍九正位，那個在深夜驚醒的畫面，很符合你現在對面試的焦慮...」）
3. 具體建議：在文末給予 2-3 點問者能實際執行或反思的具體建議，建議必須有前文的牌面支撐，具備可操作性。
4. 禁用語彙與限制：不對醫療、法律、財務做具體建議；不做具體的鐵口直斷預言（如「你下個月一定會...」）；避免使用「能量」、「宇宙」等空泛詞彙與心靈雞湯。

現在，請直接輸出你給問者的解讀回覆：\
"""

# === LITE 版：給內建 Gemini Flash Lite 用 ===
# 精簡指令；嚴格要求 Telegram HTML，禁止 Markdown。
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

# === 追問版：內建模式才會用到 ===
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
每日免費占卜 7 次，隔天自動重置（每次抽牌計 1 次，無論選擇哪種解讀模式）。

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
    """先嘗試 primary 模型，若持續擁塞 (503/429) 則切換到 fallback 模型。

    流程：
    1. 用 primary 模型呼叫，若拿到 503/429 等候 2 秒重試一次。
    2. primary 仍然擁塞 → 切換到 fallback 模型，同樣最多重試 2 次。
    3. 非擁塞類錯誤（auth、網路、模型名稱錯誤等）直接拋出，不做 fallback。
    """
    models = [GEMINI_MODEL_PRIMARY, GEMINI_MODEL_FALLBACK]
    last_error: Exception | None = None

    for model_idx, model in enumerate(models):
        is_last_model = (model_idx == len(models) - 1)

        for attempt in range(2):
            try:
                return client.models.generate_content(model=model, contents=prompt).text
            except Exception as e:
                last_error = e
                err_str = str(e)
                is_throttle = "503" in err_str or "429" in err_str

                # 非擁塞類錯誤直接拋出，不重試也不 fallback
                if not is_throttle:
                    raise

                is_last_attempt = (attempt == 1)

                if not is_last_attempt:
                    # 同模型再試一次
                    print(f"⚠️ {model} 擁塞，等待 2 秒後重試...")
                    await asyncio.sleep(2)
                    continue

                # 此模型最後一次嘗試也失敗
                if is_last_model:
                    raise
                print(f"⚠️ 主模型 {model} 持續擁塞，切換到備用模型 {models[model_idx + 1]}...")
                break  # 跳出內圈，換下一個模型

    if last_error:
        raise last_error
    raise RuntimeError("get_gemini_response 邏輯異常結束")


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
    """使用獨立的 FOLLOW_UP_PROMPT 處理追問，避免重新解讀整副牌。"""
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

        reset_kb = [[InlineKeyboardButton("🔄 結束追問，開啟新占卜", callback_data="new_reading")]]
        await safe_reply_with_html(update.message, response_text, InlineKeyboardMarkup(reset_kb))
    except Exception as e:
        await update.message.reply_text(f"❌ 靈力中斷：{e}")


# ── Mode handlers (after cards drawn) ─────────────────────────────────────────

async def _present_mode_selection(query) -> None:
    """抽完牌後顯示「複製 Prompt / 內建解析」兩個按鈕。"""
    mode_kb = [
        [InlineKeyboardButton("📋 複製完整 Prompt 自行解析", callback_data="mode_copy")],
        [InlineKeyboardButton("🔮 用內建大師解析",            callback_data="mode_builtin")],
    ]
    await query.message.reply_text(
        "✨ 牌已揭曉，請選擇解讀方式：\n\n"
        "📋 <b>複製 Prompt</b>\n"
        "取得高品質提示詞，貼到你自己的 LLM(ChatGPT、Claude、Gemini Pro 等)。"
        "適合想要更深入解讀、或希望節省機器人額度的使用者。\n\n"
        "🔮 <b>內建大師</b>\n"
        "由本機器人內建模型直接解析，方便快速。",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(mode_kb),
    )


async def _handle_mode_copy(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """產生完整 prompt 並以 <pre> 格式發送，方便使用者一鍵複製。"""
    full_prompt = READING_PROMPT_FULL.format(
        question=context.user_data.get("question", "未指定問題"),
        layout=context.user_data.get("layout_name", ""),
        cards="\n".join(context.user_data.get("card_results", [])),
    )

    # <pre> 內容須做 HTML escape，避免特殊字元被 Telegram 誤判為 tag
    escaped = html.escape(full_prompt)
    pre_message = f"<pre>{escaped}</pre>"

    # Telegram 單則訊息上限 4096 字元，留一點 buffer
    if len(pre_message) > 4000:
        # 極端情況：問題或牌組過長。分段發送
        await query.message.reply_text(
            "📋 <b>完整 Prompt（內容較長，分段呈現，請依序複製貼上）：</b>",
            parse_mode="HTML",
        )
        chunk_size = 3500
        for i in range(0, len(full_prompt), chunk_size):
            chunk = full_prompt[i:i + chunk_size]
            await query.message.reply_text(
                f"<pre>{html.escape(chunk)}</pre>",
                parse_mode="HTML",
            )
    else:
        await query.message.reply_text(
            f"📋 <b>請長按下方文字框複製，貼到你的 LLM：</b>\n\n{pre_message}",
            parse_mode="HTML",
        )

    reset_kb = [[InlineKeyboardButton("🔄 結束，開啟新占卜", callback_data="new_reading")]]
    await query.message.reply_text(
        "✅ Prompt 已生成。\n\n"
        "💡 將上方內容貼到 ChatGPT、Claude 或 Gemini 等任何 LLM，即可獲得完整解讀。\n"
        "若想針對這次牌組做進一步追問，<b>可直接在這邊輸入文字</b>，將由內建大師回應。",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(reset_kb),
    )

    # 進入追問模式（即使沒用內建解析也允許追問；reading_context 為空時 prompt 有 fallback）
    context.user_data["is_follow_up_mode"] = True
    context.user_data["reading_context"] = ""


async def _handle_mode_builtin(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """用內建 Gemini Flash Lite 跑 LITE 版 prompt。"""
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

        reset_kb = [[InlineKeyboardButton("🔄 結束追問，開啟新占卜", callback_data="new_reading")]]
        await safe_reply_with_html(
            query.message,
            "💡 <b>占卜完成。</b>\n如果你對某張牌有疑問，或想更深入了解，"
            "<b>請直接在此輸入文字追問</b>。\n\n或者點擊下方按鈕問全新的問題：",
            InlineKeyboardMarkup(reset_kb),
        )
    except Exception as e:
        await query.message.reply_text(f"❌ 靈力中斷：{e}")


# ── Layout selection (cards drawing) ─────────────────────────────────────────

async def _handle_layout_selection(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """抽牌、發送牌面圖、然後請使用者選擇解讀模式。"""
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

        # 儲存抽牌結果，供後續模式選擇使用
        context.user_data["layout_name"]  = layout_name
        context.user_data["card_results"] = card_results

        # 顯示解讀模式按鈕
        await _present_mode_selection(query)

    except Exception as e:
        await query.message.reply_text(f"❌ 靈力中斷：{e}")


# ── Main button dispatcher ────────────────────────────────────────────────────

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

    if query.data == "mode_copy":
        await _handle_mode_copy(query, context)
        return

    if query.data == "mode_builtin":
        await _handle_mode_builtin(query, context)
        return

    if query.data in LAYOUTS:
        await _handle_layout_selection(query, context)
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
