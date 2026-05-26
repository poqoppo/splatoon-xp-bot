import discord, re, os, time, math
from datetime import datetime, timedelta
from collections import Counter
import matplotlib.pyplot as plt
from flask import Flask
from threading import Thread

# Flask (Render対策)
app = Flask('')

@app.route('/')
def home():
    return "I am alive!"

def run_flask():
    app.run(host='0.0.0.0', port=10000)

t = Thread(target=run_flask)
t.start()

# 設定 (★IDを書き換えてください)
TOKEN = os.environ.get('DISCORD_TOKEN')
TARGET_CHANNEL_ID = 1474973509217423401 # 報告用IDを入れてね
LOG_CHANNEL_ID = 1508739566138294522    # バックアップ用IDを入れてね

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# ログから全データを取得
async def get_all_records():
    channel = client.get_channel(LOG_CHANNEL_ID)
    data = {} # {uid: {'records': [{'xp': x, 'time': t, 'season': s}]}}
    if not channel: return data
    async for message in channel.history(limit=5000, oldest_first=True):
        if message.author == client.user:
            p = message.content.split('|')
            if len(p) >= 4:
                uid, xp, t, s = int(p[0]), int(p[1]), p[2], p[3]
                if uid not in data: data[uid] = {'records': []}
                data[uid]['records'].append({'xp': xp, 'time': t, 'season': s})
    return data

@client.event
async def on_message(message):
    if message.author == client.user: return
    now = datetime.now()
    
    if message.channel.id == TARGET_CHANNEL_ID:
        # XP記録処理
        match = re.search(r'xp\s*([0-9]+)|([0-9]+)\s*xp', message.content, re.IGNORECASE)
        if match:
            new_xp = int(match.group(1) or match.group(2))
            if not (500 <= new_xp < 5000):
                await message.channel.send("⚠️ パワーは500〜5000で入力してください！"); return
            
            all_d = await get_all_records()
            if message.author.id in all_d and all_d[message.author.id]['records']:
                last_xp = all_d[message.author.id]['records'][-1]['xp']
                if abs(new_xp - last_xp) > 500:
                    await message.channel.send(f"⚠️ 前回({last_xp} XP)から変動が大きすぎます！"); return
            
            curr_s = "春" # 季節判定は後で自動化可
            await client.get_channel(LOG_CHANNEL_ID).send(f"{message.author.id}|{new_xp}|{now.strftime('%Y/%m/%d')}|{curr_s}")
            await message.channel.send(f"✅ {new_xp} XP を保存しました！")

        # グラフコマンド
        elif message.content.startswith('!グラフ'):
            all_d = await get_all_records()
            if message.author.id not in all_d: await message.channel.send("データなし"); return
            recs = all_d[message.author.id]['records']
            plt.figure(figsize=(8, 4))
            plt.plot([r['time'] for r in recs], [r['xp'] for r in recs], marker='o')
            plt.title(f"{message.author.display_name} さんの成長記録")
            plt.xticks(rotation=45); plt.grid(True)
            fname = f'g_{message.author.id}.png'
            plt.savefig(fname); plt.close(); await message.channel.send(file=discord.File(fname))

client.run(TOKEN)
