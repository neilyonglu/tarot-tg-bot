import os
import json
import time
import requests
from urllib.parse import urlparse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
(BASE_DIR / 'cards').mkdir(exist_ok=True)

with (BASE_DIR / 'tarot_data.json').open(encoding='utf-8') as f:
    data = json.load(f)

headers = {'User-Agent': 'TelegramTarotBot/1.0 (https://github.com/neilyonglu/tarot-tg-bot)'}

success, failed = 0, []

for name, url in data.items():
    filename = os.path.basename(urlparse(url).path)
    dest = BASE_DIR / 'cards' / filename
    if os.path.exists(dest):
        print(f"[skip] {name}")
        success += 1
        continue
    try:
        r = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
        r.raise_for_status()
        with open(dest, 'wb') as f:
            f.write(r.content)
        print(f"[ok]   {name} -> {filename}")
        success += 1
        time.sleep(0.3)
    except Exception as e:
        print(f"[fail] {name}: {e}")
        failed.append(name)

print(f"\n完成：{success} 成功，{len(failed)} 失敗")
if failed:
    print("失敗清單：", failed)
