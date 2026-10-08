# 塔羅占卜機器人

Telegram 塔羅占卜機器人，使用萊德偉特牌組，透過 Gemini AI 提供深度解析。

## 功能

- **三種牌陣**：單張、四牌陣（心態/過去/現在/未來）、六芒星（7張深度分析）
- **正逆位**：隨機決定正/逆位，自動旋轉圖片
- **AI 解析**：Gemini 3.1 Flash lite 根據問題與牌面給出客製化詮釋
- **後續追問**：占卜完成後可繼續追問，保留最近的對話脈絡
- **使用限制**：每日免費 7 次；VIP 密碼解鎖無限制
- **狀態查詢**：`/status` 顯示剩餘抽牌額度與 VIP 狀態；額度於台灣時間每日 00:00 重置
- **自行解析**：內建 AI 失敗時仍可複製完整 Prompt，交給自己的 LLM 解析

## 專案結構

```
tarot-tg-bot/
├── cards/               # 78 public-domain card images
├── app.py               # Bot entry point
├── download_cards.py    # Download missing images from Wikimedia
├── test_app.py          # Offline regression checks
├── tarot_data.json      # Card names and image URLs
├── pyproject.toml       # Project metadata and dependencies
├── uv.lock              # Locked dependency versions
├── .python-version      # Python version for uv
├── .env.example         # Environment configuration template
└── requirements.txt     # Generated dependencies for pip-based deployments
```

## 環境變數

| 變數名稱 | 說明 |
|---|---|
| `TELEGRAM_TOKEN` | Telegram Bot Token |
| `GEMINI_API_KEY` | Google Gemini API Key |
| `VIP_PASSWORD` | VIP 解鎖密碼；留空時停用 VIP 解鎖 |
| `PORT` | 伺服器監聽 Port（預設 10000） |

## 本地開發

先安裝 [uv](https://docs.astral.sh/uv/getting-started/installation/)，並在專案根目錄執行：

```powershell
# Create the virtual environment and install locked dependencies.
uv sync --locked

# Copy the template, then fill in your keys and a private VIP password.
Copy-Item .env.example .env

# Start the bot after editing .env.
uv run --env-file .env python app.py
```

卡牌圖片已包含在專案內；需要補下載時執行：

```powershell
uv run python download_cards.py
```

`uv run` 會自動使用專案的 `.venv`，不需要手動啟用虛擬環境。請勿提交 `.env`。

啟動時會檢查 Token、API Key 與 Port；範例中的佔位文字必須替換。使用者狀態保存在記憶體內，重啟後重置；追問不另外扣除抽牌額度。

離線檢查（不呼叫 Telegram 或 Gemini API）：

```powershell
uv run python test_app.py
```

新增或更新依賴時，以 `pyproject.toml` 與 `uv.lock` 為準；仍使用 pip 的部署環境可安裝 `requirements.txt`。修改依賴後重新匯出：

```powershell
uv export --locked --no-hashes --no-dev --no-emit-project --output-file requirements.txt
```

## 圖片版權

使用 **萊德偉特塔羅牌（Rider-Waite Tarot）** 原版圖片，出版於 1909 年，現已進入公共領域（Public Domain）。圖片來源為 Wikimedia Commons。
