import os
import asyncio
import threading
import random
import requests
import json
from io import BytesIO
from PIL import Image
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer  # 🌟 新增：極輕量伺服器套件
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from google import genai

# --- 1. 讀取金鑰與設定 ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY)

# 👑 【VIP 系統設定】 👑
SECRET_PASSWORD = os.environ.get("VIP_PASSWORD", "未設定密碼")
DAILY_LIMIT = 5

with open('tarot_data.json', 'r', encoding='utf-8') as file:
    TAROT_DATA = json.load(file)

# --- 2. 共用小工具 ---

def get_rotated_card(url, is_reversed):
    headers = {'User-Agent': 'Mozilla/5.0'}
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    img = Image.open(BytesIO(response.content))
    if is_reversed: img = img.rotate(180)
    img.thumbnail((600, 800))
    bio = BytesIO()
    bio.name = 'card.jpg'
    if img.mode in ("RGBA", "P"): img = img.convert("RGB")
    img.save(bio, 'JPEG', quality=85)
    bio.seek(0)
    return bio

async def get_gemini_response(prompt):
    try:
        return client.models.generate_content(model='gemini-3.1-flash-lite-preview', contents=prompt).text
    except Exception as api_err:
        if "503" in str(api_err) or "429" in str(api_err):
            print("3.1 通道塞車，自動切換至 2.0 備用通道...")
            return client.models.generate_content(model='gemini-2.5-flash', contents=prompt).text
        raise api_err

async def safe_reply_with_html(message_obj, text, reply_markup=None):
    try:
        await message_obj.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
    except Exception as parse_err:
        clean_text = text.replace('<b>', '').replace('</b>', '').replace('<i>', '').replace('</i>', '').replace('<u>', '').replace('</u>', '')
        await message_obj.reply_text(clean_text, reply_markup=reply_markup)

# --- 3. 機器人邏輯 ---

async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "🌙 重新開始占卜"),
        BotCommand("pwd", "🔓 解鎖 VIP 模式")
    ])

async def send_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['is_follow_up_mode'] = False
    context.user_data['reading_context'] = ""
    await update.message.reply_text("🌙 歡迎！請深呼吸，然後直接在此輸入你的問題，愈詳細愈好，並在心中默念3遍，我將為你開啟占卜。")

async def handle_pwd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("💡 請使用格式：\n`/pwd 你的密碼`\n來解鎖大師的無限靈力。", parse_mode='Markdown')
        return

    user_pwd = " ".join(context.args)
    if user_pwd == SECRET_PASSWORD:
        context.user_data['is_unlocked'] = True
        await update.message.reply_text("🔓 密碼正確！\n大師為你開啟了「無限靈力模式」✨，現在可無限制占卜！請直接輸入你想問的問題。")
    else:
        await update.message.reply_text("❌ 密碼錯誤，靈力封印未解除。")

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    
    if user_text == SECRET_PASSWORD:
        context.user_data['is_unlocked'] = True
        await update.message.reply_text("🔓 密碼正確！\n大師為你開啟了「無限靈力模式」✨，現在可無限制占卜！請重新輸入你想問的問題。")
        return

    # 處理追問
    if context.user_data.get('is_follow_up_mode') == True:
        await update.message.reply_text("✨ 大師正在傾聽你的疑惑...")
        original_q = context.user_data.get('question', '未知問題')
        reading_context = context.user_data.get('reading_context', '')
        layout_name = context.user_data.get('layout_name', '塔羅牌陣')
        card_results = context.user_data.get('card_results', ['無抽牌紀錄'])
        
        user_question = f"{original_q}\n(💡本次為後續追問，前情提要：{reading_context}。使用者最新追問：「{user_text}」)"
        prompt = f"""
        你是一位精通萊德偉特體系的塔羅大師。
        問題：「{user_question}」
        牌陣：【{layout_name}】
        結果：
        {chr(10).join(card_results)}
        
        請結合牌面深度解析並給予建議。
        
        【⚠️排版嚴格要求】：
        請務必使用 Telegram 支援的 HTML 標籤進行排版：
        - 粗體請使用 <b>你的文字</b>
        - 斜體請使用 <i>你的文字</i>
        - 底線請使用 <u>你的文字</u>
        絕對不要使用任何 Markdown 語法（例如 **粗體**、*斜體* 或 # 標題）。
        段落之間請直接換行即可，不需要使用 <br>。
        """
        
        try:
            response_text = await get_gemini_response(prompt)
            context.user_data['reading_context'] += f"\n\n使用者追問：「{user_text}」\n大師回答：{response_text}"
            reset_keyboard = [[InlineKeyboardButton("🔄 結束追問，開啟新占卜", callback_data="new_reading")]]
            await safe_reply_with_html(update.message, response_text, InlineKeyboardMarkup(reset_keyboard))
        except Exception as e:
            await update.message.reply_text(f"❌ 靈力中斷：{str(e)}")
        return 
        
    is_unlocked = context.user_data.get('is_unlocked', False)
    limit_hint = ""
    
    if not is_unlocked: 
        today = date.today().isoformat() 
        if context.user_data.get('last_usage_date') != today:
            context.user_data['last_usage_date'] = today
            context.user_data['usage_count'] = 0
            
        if context.user_data.get('usage_count', 0) >= DAILY_LIMIT:
            await update.message.reply_text("⏳ 每日免費 5 次已用完。\n💡 若是 VIP 請輸入「/pwd 你的密碼」解鎖無限模式！")
            return
            
        remaining = DAILY_LIMIT - context.user_data.get('usage_count', 0)
        limit_hint = f"\n(💡 今日抽牌額度剩餘：{remaining} 次)"

    context.user_data['question'] = user_text
    keyboard = [
        [InlineKeyboardButton("🔮 單張 (快速解惑)", callback_data="draw_1")],
        [InlineKeyboardButton("🎴 四牌陣 (心態/過去/現在/未來)", callback_data="draw_4")],
        [InlineKeyboardButton("✡️ 六芒星 (深入分析與對策)", callback_data="draw_hexa")]
    ]
    await update.message.reply_text(f"✅ 已感應問題：「{user_text}」{limit_hint}\n請選擇牌陣：", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "new_reading":
        context.user_data['is_follow_up_mode'] = False
        context.user_data['reading_context'] = ""
        await query.message.reply_text("🌙 記憶已重置。\n請直接輸入你【新的問題】，我將為你開啟全新的占卜。")
        return
    
    if query.data == "draw_1": count, layout_name, positions = 1, "單張", ["核心指引"]
    elif query.data == "draw_4": count, layout_name, positions = 4, "四牌陣", ["現在心態", "過去事件", "現在事件", "未來事件"]
    elif query.data == "draw_hexa": count, layout_name, positions = 7, "六芒星", ["過去狀況", "現在狀況", "未來發展", "對應策略", "周遭狀況", "問者態度", "最後結果"]
    else: return

    is_unlocked = context.user_data.get('is_unlocked', False)
    if not is_unlocked:
        today = date.today().isoformat()
        if context.user_data.get('last_usage_date') != today:
            context.user_data['last_usage_date'] = today
            context.user_data['usage_count'] = 0
            
        if context.user_data.get('usage_count', 0) >= DAILY_LIMIT:
            await query.edit_message_text("⏳ 大師今天的靈力已經耗盡囉！請輸入「/pwd 你的密碼」解鎖。")
            return
            
        context.user_data['usage_count'] += 1

    await query.edit_message_text(f"🔮 佈下【{layout_name}】中，請稍候...")
    drawn_keys = random.sample(list(TAROT_DATA.keys()), count)
    card_results = []
    
    try:
        for i in range(count):
            card_name, pos_label = drawn_keys[i], positions[i]
            is_reversed = random.choice([True, False])
            state = "逆位" if is_reversed else "正位"
            
            card_results.append(f"📍 {pos_label}: {card_name} ({state})")
            photo_file = get_rotated_card(TAROT_DATA[card_name], is_reversed)
            await query.message.reply_photo(photo=photo_file, caption=f"📍 【{pos_label}】: {card_name} ({state})")
            await asyncio.sleep(1.5)
            
        await query.message.reply_text("✨ 所有牌面已揭曉。大師正在感應牌面連結，深度解析中...")
        
        user_question = context.user_data.get('question', '未指定問題')
        context.user_data['layout_name'] = layout_name
        context.user_data['card_results'] = card_results
        
        prompt = f"""
        你是一位精通萊德偉特體系的塔羅大師。
        問題：「{user_question}」
        牌陣：【{layout_name}】
        結果：
        {chr(10).join(card_results)}
        
        請結合牌面深度解析並給予建議。
        
        【⚠️排版嚴格要求】：
        請務必使用 Telegram 支援的 HTML 標籤進行排版：
        - 粗體請使用 <b>你的文字</b>
        - 斜體請使用 <i>你的文字</i>
        - 底線請使用 <u>你的文字</u>
        絕對不要使用任何 Markdown 語法（例如 **粗體**、*斜體* 或 # 標題）。
        段落之間請直接換行即可，不需要使用 <br>。
        """
        
        response_text = await get_gemini_response(prompt)
        
        context.user_data['is_follow_up_mode'] = True
        context.user_data['reading_context'] = f"初次解析：\n{response_text}"
        
        await safe_reply_with_html(query.message, response_text)
        
        reset_keyboard = [[InlineKeyboardButton("🔄 結束追問，開啟新占卜", callback_data="new_reading")]]
        await safe_reply_with_html(
            query.message, 
            "💡 <b>占卜完成。</b>\n如果你對某張牌有疑問，或想更深入了解，<b>請直接在此輸入文字追問</b>。\n\n或者點擊下方按鈕問全新的問題：", 
            InlineKeyboardMarkup(reset_keyboard)
        )

    except Exception as e:
        await query.message.reply_text(f"❌ 靈力中斷：{str(e)}")

# --- 4. 機器人啟動器 ---
def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).connect_timeout(30).read_timeout(60).write_timeout(60).pool_timeout(30).build()
    
    app.add_handler(CommandHandler("start", send_welcome))
    app.add_handler(CommandHandler("pwd", handle_pwd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    print("--- 機器人已在背景啟動 ---")
    app.run_polling(stop_signals=None, drop_pending_updates=True)

# --- 5. 🌟 極輕量網頁伺服器 (給 UptimeRobot 喚醒用) ---
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write(b"Tarot Master is awake!")
        
    def log_message(self, format, *args):
        pass  # 關閉日誌，讓伺服器更安靜省力

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), PingHandler)
    print(f"--- 輕量喚醒伺服器已在 Port {port} 對外開放 ---")
    server.serve_forever()

if __name__ == "__main__":
    # 讓 Telegram 機器人在背景獨立運作
    threading.Thread(target=run_bot, daemon=True).start()
    
    # 讓輕量伺服器在主程式運作，向外打開大門
    run_dummy_server()
