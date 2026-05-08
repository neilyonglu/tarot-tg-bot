# 塔羅占卜機器人

Telegram 塔羅占卜機器人，使用萊德偉特牌組，透過 Gemini AI 提供深度解析。

## 功能

- **三種牌陣**：單張、四牌陣（心態/過去/現在/未來）、六芒星（7張深度分析）
- **正逆位**：隨機決定正/逆位，自動旋轉圖片
- **AI 解析**：Gemini 2.5 Flash 根據問題與牌面給出客製化詮釋
- **後續追問**：占卜完成後可繼續追問，保留完整上下文
- **使用限制**：每日免費 5 次；VIP 密碼解鎖無限制

## 專案結構

```
tarot-tg-bot/
├── cards/               # 78 張塔羅牌圖片（公共領域）
├── app.py               # 主程式
├── download_cards.py    # 工具：從 Wikimedia 下載卡牌圖片
├── tarot_data.json      # 卡牌名稱 → Wikimedia 圖片 URL
└── requirements.txt
```

## 環境變數

| 變數名稱 | 說明 |
|---|---|
| `TELEGRAM_TOKEN` | Telegram Bot Token |
| `GEMINI_API_KEY` | Google Gemini API Key |
| `VIP_PASSWORD` | VIP 解鎖密碼 |
| `PORT` | 伺服器監聽 Port（預設 10000） |

## 本地開發

```bash
# 安裝依賴
pip install -r requirements.txt

# 下載卡牌圖片（首次執行需要）
python download_cards.py

# 設定環境變數後啟動
python app.py
```

## 圖片版權

使用 **萊德偉特塔羅牌（Rider-Waite Tarot）** 原版圖片，出版於 1909 年，現已進入公共領域（Public Domain）。圖片來源為 Wikimedia Commons。
