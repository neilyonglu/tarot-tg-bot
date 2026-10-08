import asyncio
import html
import random
import re
from secrets import token_hex

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

import tarot
from prompts import FOLLOW_UP_PROMPT, MANUAL_TEXT, READING_PROMPT_FULL, READING_PROMPT_LITE

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


async def send_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _reset_reading(context.user_data)
    await update.message.reply_text(
        "🌙 歡迎！請深呼吸，然後直接在此輸入你的問題，愈詳細愈好，並在心中默念3遍，我將為你開啟占卜。"
    )


async def handle_manual(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(MANUAL_TEXT, parse_mode="HTML")


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remaining = tarot.get_remaining_uses(context.user_data)
    vip = "已解鎖" if remaining is None else "未解鎖"
    quota = "無限制" if remaining is None else f"{remaining} / {tarot.DAILY_LIMIT} 次"
    await update.message.reply_text(
        f"📊 今日剩餘抽牌額度：{quota}\nVIP：{vip}\n額度於台灣時間每日 00:00 重置。"
    )


async def handle_pwd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not tarot.SECRET_PASSWORD:
        await update.message.reply_text("VIP 解鎖目前未開放。")
        return
    if not context.args:
        await update.message.reply_text(
            "💡 請使用格式：\n<code>/pwd 你的密碼</code>\n來解鎖大師的無限靈力。",
            parse_mode="HTML",
        )
        return

    if " ".join(context.args) == tarot.SECRET_PASSWORD:
        await _activate_vip(update, context)
    else:
        await update.message.reply_text("❌ 密碼錯誤，靈力封印未解除。")


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_text = update.message.text

    if tarot.SECRET_PASSWORD and user_text == tarot.SECRET_PASSWORD:
        await _activate_vip(update, context)
        return

    if context.user_data.get("is_follow_up_mode"):
        await handle_follow_up(update, context, user_text)
        return

    remaining = tarot.get_remaining_uses(context.user_data)
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
        response_text = await tarot.get_gemini_response(prompt)

        new_entry = f"\n\n使用者追問：「{user_text}」\n大師回答：{response_text}"
        full_context = context.user_data.get("reading_context", "") + new_entry
        if len(full_context) > tarot.CONTEXT_MAX_CHARS:
            full_context = "（前段對話已省略）\n" + full_context[-tarot.CONTEXT_MAX_CHARS:]
        context.user_data["reading_context"] = full_context

        reset_kb = [[InlineKeyboardButton("🔄 結束追問，開啟新占卜", callback_data=_reading_callback(context, "new_reading"))]]
        await safe_reply_with_html(update.message, response_text, InlineKeyboardMarkup(reset_kb))
    except Exception as e:
        print(f"Follow-up failed: {e}")
        await update.message.reply_text("❌ 內建解析暫時無法使用，可複製 Prompt 到自己的 LLM 繼續解析。")
        await _present_mode_selection(update.message, context)


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
        response_text = await tarot.get_gemini_response(prompt)

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


async def _handle_layout_selection(query, context: ContextTypes.DEFAULT_TYPE, layout_key: str) -> None:
    """Draw cards, send their images, and ask the user to choose a reading mode."""
    count, layout_name, positions = tarot.LAYOUTS[layout_key]

    if not tarot.consume_usage(context.user_data):
        await query.edit_message_text("⏳ 大師今天的靈力已經耗盡囉！請輸入「/pwd 你的密碼」解鎖。")
        return

    await query.edit_message_text(f"🔮 佈下【{layout_name}】中，請稍候...")

    drawn_cards = random.sample(list(tarot.TAROT_DATA), count)
    card_results = []

    try:
        for card_name, pos_label in zip(drawn_cards, positions):
            is_reversed = random.choice([True, False])
            state = "逆位" if is_reversed else "正位"

            card_results.append(f"📍 {pos_label}: {card_name} ({state})")
            photo = await asyncio.to_thread(tarot.get_card_image, tarot.TAROT_DATA[card_name], is_reversed)
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


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, _, reading_id = (query.data or "").partition(":")
    if not reading_id or reading_id != context.user_data.get("reading_id"):
        await query.answer("這個按鈕已失效，請使用目前的占卜按鈕。", show_alert=True)
        return
    if action in tarot.LAYOUTS:
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

    if action in tarot.LAYOUTS:
        await _handle_layout_selection(query, context, action)
        return
