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
GEMINI_MODEL    = "gemini-3.1-flash-lite"
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
這是一份塔羅占卜解讀請求。請扮演專業的塔羅諮詢師，使用萊德偉特體系（Rider-Waite-Smith）的傳統象徵。
你的任務不是預測未來，而是透過牌面協助問者看清當下處境、盲點與選擇空間。

# 問者問題
{question}

# 使用牌陣
{layout}

# 抽牌結果
{cards}

# 解讀任務（請依序執行）

## 步驟一：逐牌定位
針對每一張牌，依序說明：
1. 該牌在 RWS 體系的核心象徵（1–2 句即可，避免空泛）
2. 正位／逆位的具體差異
3. 牌面圖像中（人物、姿態、場景、符號）最能呼應此次問題與牌位的細節
4. 此牌在這個牌位上對問者的具體訊息

要求：
- 每一點都要錨定在牌的具體象徵或圖像細節，不要使用「能量」「氛圍」「振動」這類含糊詞
- 若牌與牌位語意有張力（例如「未來」位出現停滯型牌），請把張力本身當成訊息點出，不要硬解

## 步驟二：跨牌觀察（單張牌請跳過此步）
只在實際出現時寫，沒有就略過，不要硬湊：
- 大阿爾克那比例（多 = 命運層面議題；少 = 日常選擇層面）
- 花色集中（聖杯=情感／寶劍=思緒／權杖=行動／錢幣=現實）
- 數字重複或遞進（同數字 = 主題重複；遞進 = 階段性發展）
- 宮廷牌（指向具體人物或問者自身扮演的角色）
- 逆位比例偏高（暗示卡點在內在尚未顯化）

## 步驟三：回應問題
綜合前兩步，直接回應問者的問題：
- 不要重述步驟一已寫過的內容
- 不要做具體時間預言（不要寫「下個月」「三週內」這類話）
- 若牌面指向多種可能，誠實列出並標明各自牌面依據
- 若牌面與問題不直接相關，誠實說出，並指出牌面真正在回應的是什麼

## 步驟四：可行的反思與行動
給 2–3 點具體建議：
- 每點都要標明來自前面分析中的哪張牌或哪個模式
- 是「問者可以做的事」或「值得問自己的問題」，不是「將會發生什麼」
- 避免心靈雞湯，避免「相信宇宙」這類話

# 禁止事項
1. 不對醫療、法律、財務做具體建議；若問題涉及這些，請建議問者尋求對應專業協助
2. 不對特定他人做人格評斷或診斷
3. 不做高確定性的未來預言
4. 若問者透露自我傷害、嚴重憂鬱等訊號，請優先溫和提醒尋求專業心理支持，再給牌面解讀

# 語氣與排版
誠懇、專業，像有經驗的諮詢師朋友，不要使用神祕學腔調。
篇幅以資訊密度為準，講清楚就停，不要為了長度填字。
請使用清楚易讀的排版（粗體、條列、標題等），便於在你目前的 LLM 介面閱讀。\
"""

# === LITE 版：給內建 Gemini Flash Lite 用 ===
# 精簡指令；嚴格要求 Telegram HTML，禁止 Markdown。
READING_PROMPT_LITE = """\
你是專業的塔羅諮詢師，使用萊德偉特體系（RWS）。
你的任務不是預測未來，而是協助問者看清當下處境與選擇空間。

# 問題
{question}

# 牌陣
{layout}

# 抽牌結果
{cards}

# 解讀步驟（依序執行）

## 步驟一：逐牌定位
每張牌依序說明：
1. RWS 核心象徵（1-2 句）
2. 正位／逆位的具體差異
3. 牌面圖像中（人物、姿態、場景、符號）呼應此次問題與牌位的細節
4. 此牌在這個牌位上對問者的訊息

要求：每點都要錨定具體象徵或圖像，不要用「能量」「氛圍」「振動」這類含糊詞。

## 步驟二：跨牌觀察（單張牌跳過）
只在實際出現時寫：大阿爾克那比例、花色集中、數字重複、宮廷牌、逆位比例偏高。

## 步驟三：回應問題
綜合前兩步直接回答問者，不要重述步驟一。
不做時間預言（如「下個月」「三週內」）。
多解時誠實列出各自牌面依據。

## 步驟四：建議
2-3 點，每點標明來自哪張牌；是「可做的事」或「值得問自己的問題」，不是預言。

# 排版要求（嚴格）
請務必使用 Telegram HTML 標籤：
- 粗體 <b>文字</b>
- 斜體 <i>文字</i>
- 底線 <u>文字</u>

絕對不要使用 Markdown（不要 **粗體**、*斜體*、# 標題、- 條列符號）。
段落之間直接換行，不要 <br>。
條列用「・」或「1. 2. 3.」開頭。

# 禁止事項
不做時間預言；不對特定他人人格評斷；不給醫療／法律／財務具體建議。
若問者透露自殘或嚴重憂鬱訊號，先溫和提醒尋求專業協助再解讀。

# 語氣
誠懇、專業，不要神祕學腔調，不要心靈雞湯。
講清楚就停，不要為長度填字。\
"""

# === 追問版：內建模式才會用到 ===
FOLLOW_UP_PROMPT = """\
你是專業的塔羅諮詢師，正在回應問者對先前牌陣解讀的追問。

# 最初的問題
{question}

# 當時抽到的牌（{layout}）
{cards}

# 先前的解讀脈絡
{reading_context}

# 問者最新的追問
{follow_up}

# 任務原則
1. 聚焦在追問本身，不要重新解讀整副牌
2. 必須引用具體某張牌或某個牌面元素作為依據——這是和一般聊天的差別
3. 若追問已經偏離牌面能回答的範圍（例如問了完全無關的新問題），誠實指出並建議是否需要重新抽牌
4. 若追問是對先前解讀的反駁或補充資訊，重新評估牌面在新脈絡下的意義

# 禁止
不做時間預言；不對他人做人格評斷；不給醫療／法律／財務具體建議；
不要使用「能量」「振動」「宇宙安排」這類含糊詞。

# 排版要求（嚴格）
僅用 Telegram HTML：<b>粗體</b>、<i>斜體</i>、<u>底線</u>。
絕對不要 Markdown（不要 **粗體**、*斜體*、# 標題）。
段落直接換行，不要 <br>。條列用「・」或數字。

# 篇幅
比初次解讀短，聚焦回答即可，不要硬湊長度。\
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
    for attempt in range(3):
        try:
            return client.models.generate_content(model=GEMINI_MODEL, contents=prompt).text
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
