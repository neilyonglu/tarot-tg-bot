# Development and deployment

## 專案結構

```
tarot-tg-bot/
├── cards/               # 78 public-domain card images
├── app.py               # Bot entry point
├── tarot.py             # Card data, quotas, images, Gemini, and configuration
├── handlers.py          # Telegram messages and reading interactions
├── prompts.py           # Reading prompts and in-bot manual
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

## Server 部署與自動更新

部署路徑為 `/mnt/ssd/self-proj/tarot-tg-bot`。使用 Ubuntu x86_64 主機上的 GitHub Actions self-hosted runner；不需要新增入站 SSH 白名單。

首次切換前，先停掉原本執行中的 bot，避免同一個 Telegram Token 同時被兩個程序輪詢。既有 `.env` 會沿用，Docker 建置不會包含它。

```bash
cd /mnt/ssd/self-proj/tarot-tg-bot
git pull --ff-only origin main
test -f .env || cp .env.example .env
chmod 600 .env
nano .env
docker info >/dev/null
docker compose up --build --wait --wait-timeout 90 bot
```

容器使用 `uv.lock` 安裝依賴；每次建置都先執行離線測試，測試失敗不會替換正在運行的容器。Telegram 初始化與指令註冊成功後才開啟 HTTP 健康檢查服務，Compose 等待健康檢查通過後才視為完成；這項檢查不代表 Gemini API 可用。Bot 使用 polling，Compose 不公開任何入站 port。

Runner 安裝一次即可：

1. 打開 GitHub repo 的 **Settings → Actions → Runners → New self-hosted runner**，選擇 **Linux / x64**。
2. 在主機另建 runner 目錄，與部署目錄分開：

   ```bash
   mkdir -p /mnt/ssd/self-proj/actions-runner-tarot
   cd /mnt/ssd/self-proj/actions-runner-tarot
   ```

3. 執行 GitHub 頁面產生的下載與解壓指令；設定 runner 時，加上 `--name genton-tarot --labels tarot`。註冊 Token 只在主機上使用，不要提交或貼到聊天。
4. 確認執行 runner 的 `genton` 帳號能直接使用 Docker。若 `docker info` 顯示權限不足：

   ```bash
   sudo usermod -aG docker genton
   ```

   登出再登入，重新確認 `docker info`。Docker 群組具有接近 root 的權限，只加入可信任帳號。

5. 在 runner 目錄安裝並啟動 systemd 服務：

   ```bash
   sudo ./svc.sh install genton
   sudo ./svc.sh start
   sudo ./svc.sh status
   ```

GitHub 顯示 runner 為 **Idle** 後，可在 **Actions → Test and deploy tarot bot → Run workflow** 手動測試。PR 與 `main` 更新會先在 GitHub 雲端 runner 執行 Docker 建置與離線測試；只有 `main` 的 CI 通過後，才交給主機上的 runner 部署，不會在這台主機執行 PR。請保護 `main` 並限制可推送的人員，因為部署 workflow 能存取主機的 Docker 與 bot 設定。

流程是 GitHub 雲端 CI 通過 → server fetch 並 fast-forward 到 CI 已驗證的 commit → Docker 建置與離線測試 → 更新單一 bot 容器並等待健康檢查 → 傳送 Telegram 通知至 `6501701404`。部署目錄必須在 `main`，且沒有未提交的已追蹤檔案修改；runner 帳號也必須能讀取 origin repo。部署失敗時查看 Actions 記錄；通知失敗會標示該步驟失敗，不代表容器已回復舊版。

```bash
# Inspect the running bot.
cd /mnt/ssd/self-proj/tarot-tg-bot
docker compose ps
docker compose logs --tail 100 bot
```

## 圖片版權

使用 **萊德偉特塔羅牌（Rider-Waite Tarot）** 原版圖片，出版於 1909 年，現已進入公共領域（Public Domain）。圖片來源為 Wikimedia Commons。
