import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image

TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY")
SECRET_PASSWORD = os.environ.get("VIP_PASSWORD", "").strip()
if SECRET_PASSWORD.startswith("replace-with-"):
    SECRET_PASSWORD = ""
GEMINI_MODEL_PRIMARY  = "gemini-3.1-flash-lite"
GEMINI_MODEL_FALLBACK = "gemini-2.5-flash"
DAILY_LIMIT     = 7
CONTEXT_MAX_CHARS = 2000  # Follow-up context limit; discard the oldest text when exceeded.
BASE_DIR = Path(__file__).resolve().parent.parent
TAIWAN_TIME = timezone(timedelta(hours=8))

client = None

with (BASE_DIR / "assets" / "tarot_data.json").open(encoding="utf-8") as f:
    TAROT_DATA = json.load(f)

LAYOUTS = {
    "draw_1":    (1, "單張",   ["核心指引"]),
    "draw_4":    (4, "四牌陣", ["現在心態", "過去事件", "現在事件", "未來事件"]),
    "draw_hexa": (7, "六芒星", ["過去狀況", "現在狀況", "未來發展", "對應策略", "周遭狀況", "問者態度", "最後結果"]),
}


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
    local_path = BASE_DIR / "assets" / "cards" / filename

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
                break


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
