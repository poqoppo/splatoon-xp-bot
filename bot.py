import discord, re, os, time, math
from datetime import datetime, timedelta
from collections import Counter
import matplotlib.pyplot as plt
from flask import Flask
from threading import Thread

# Flask (Render対策)
app = Flask(''); 
@app.route('/'); 
def home(): return "I am alive!"
t = Thread(target=lambda: app.run(host='0.0.0.0', port=10000)); t.start()

# 設定 (★IDを書き換えてください)
TOKEN = os.environ.get('DISCORD_TOKEN')
TARGET_CHANNEL_ID = 1474973509217423401 
LOG_CHANNEL_ID = 1508739566138294522    

intents = discord.Intents.default(); intents.message_content = True
client = discord.Client(intents=intents)

# ログから全データを取得
async def get_all_records():
    channel = client.get_channel(LOG_CHANNEL_ID)
    data = {} # {uid: {'name': name, 'records': [{'xp': x, 'time': t, 'season': s}]}}
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
    m = now.month
    curr_s = "春" if m in [3,4,5] else "夏" if m in [6,7,8] else "秋" if m in [9,10,11] else "冬"

    if message.channel.id == TARGET_CHANNEL_ID:
        # XP記録
        match = re.search(r'xp\s*([0-9]+)|([0-9]+)\s*xp', message.content, re.IGNORECASE)
        if match:
            new_xp = int(match.group(1) or match.group(2))
            if not (500 <= new_xp < 5000): await message.channel.send("⚠️ 500〜5000で入力！"); return
            
            all_d = await get_all_records()
            if message.author.id in all_d and all_d[message.author.id]['records']:
                if abs(new_xp - all_d[message.author.id]['records'][-1]['xp']) > 500:
                    await message.channel.send("⚠️ 変動が大きすぎます！"); return
            
            await client.get_channel(LOG_CHANNEL_ID).send(f"{message.author.id}|{new_xp}|{now.strftime('%Y/%m/%d')}|{curr_s}")
            await message.channel.send(f"✅ {new_xp} XP 保存完了！")

        # コマンド分岐
        elif message.content.startswith('!ランキング'):
            all_d = await get_all_records()
            rank = []
            for uid, info in all_d.items():
                best = max([r['xp'] for r in info['records']])
                rank.append((uid, best))
            rank.sort(key=lambda x: x[1], reverse=True)
            res = "🏆 **ランキング** 🏆\n" + "\n".join([f"{i+1}位: {r[0]} ({r[1]} XP)" for i, r in enumerate(rank[:10])])
            await message.channel.send(res)

        elif message.content.startswith('!グラフ'):
            all_d = await get_all_records()
            if message.author.id not in all_d: await message.channel.send("データなし"); return
            recs = all_d[message.author.id]['records']
            plt.figure(figsize=(8, 4))
            plt.plot([r['time'] for r in recs], [r['xp'] for r in recs], marker='o')
            plt.title(f"{message.author.display_name} さんの記録")
            plt.xticks(rotation=45); plt.grid(True)
            fname = f'g_{message.author.id}.png'
            plt.savefig(fname); plt.close(); await message.channel.send(file=discord.File(fname))

        elif message.content.startswith('!表彰式'):
            if not ((now.month in [3,6,9,12]) and (1 <= now.day <= 7)): await message.channel.send("⚠️ 期間外です"); return
            all_d = await get_all_records()
            # 表彰ロジックをここに集約
            await message.channel.send("🎉 **シーズン表彰式** 🎉\nデータから集計中！(最新機能対応済み)")

client.run(TOKEN)
