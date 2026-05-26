import discord
import sqlite3
import re
import os
import urllib.request
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import math
from collections import Counter
from flask import Flask
from threading import Thread

# Flaskサーバー用 (Render対策)
app = Flask('')
@app.route('/')
def home():
    return "I am alive!"
def run():
    app.run(host='0.0.0.0', port=10000)
t = Thread(target=run)
t.start()

# 日本語フォントの設定
def setup_font():
    font_url = "https://github.com/googlefonts/noto-cjk/raw/main/Sans/OTC/NotoSansCJK-Regular.ttc"
    font_path = "NotoSansCJK.ttc"
    if not os.path.exists(font_path):
        urllib.request.urlretrieve(font_url, font_path)
    fm.fontManager.addfont(font_path)
    plt.rcParams['font.family'] = 'Noto Sans CJK JP'

setup_font()

# 設定
TOKEN = os.environ.get('DISCORD_TOKEN')
TARGET_CHANNEL_ID = 1474973509217423401

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

def setup_database():
    conn = sqlite3.connect('xp_data.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS xp_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            user_name TEXT,
            xp_value INTEGER,
            record_time TIMESTAMP,
            season TEXT
        )
    ''')
    conn.commit()
    conn.close()

@client.event
async def on_ready():
    setup_database()
    print(f'{client.user} が起動しました！')

def parse_args(content, current_year, current_season):
    args = content.split()
    target_year, target_season, is_all_time = str(current_year), current_season, False
    if len(args) > 1:
        if "全期間" in args: is_all_time = True
        else:
            for arg in args[1:]:
                if "年" in arg: target_year = re.sub(r'\D', '', arg)
                elif arg.isdigit() and len(arg) == 4: target_year = arg
                elif "シーズン" in arg: target_season = arg
    return target_year, target_season, is_all_time, ("全期間" if is_all_time else f"{target_year}年 {target_season}")

@client.event
async def on_message(message):
    if message.author == client.user or message.channel.id != TARGET_CHANNEL_ID: return
    now = datetime.now()
    m = now.month
    current_season = "春シーズン" if m in [3,4,5] else "夏シーズン" if m in [6,7,8] else "秋シーズン" if m in [9,10,11] else "冬シーズン"
    current_year = now.year

    if message.content.startswith('!全員のグラフ'):
        ty, ts, ia, title = parse_args(message.content, current_year, current_season)
        conn = sqlite3.connect('xp_data.db'); cursor = conn.cursor()
        if ia: cursor.execute('SELECT DISTINCT record_time FROM xp_records ORDER BY record_time ASC')
        else: cursor.execute('SELECT DISTINCT record_time FROM xp_records WHERE record_time LIKE ? AND season = ? ORDER BY record_time ASC', (f"{ty}/%", ts))
        times_raw = [r[0] for r in cursor.fetchall()]
        if not times_raw: await message.channel.send('⚠️ まだ記録がありません'); conn.close(); return
        
        dates = [t[:10] for t in times_raw]; dc = Counter(dates)
        d_times = [f"{t[2:4]}年{int(t[5:7])}月{int(t[8:10])}日" + (f" {int(t[11:13])}時" if dc[t[:10]]>1 else "") for t in times_raw]
        
        cursor.execute('SELECT DISTINCT user_id, user_name FROM xp_records' if ia else 'SELECT DISTINCT user_id, user_name FROM xp_records WHERE record_time LIKE ? AND season = ?', (f"{ty}/%", ts))
        users = cursor.fetchall()
        plt.figure(figsize=(10, 5))
        for uid, uname in users:
            cursor.execute('SELECT record_time, xp_value FROM xp_records WHERE user_id = ? ORDER BY record_time ASC' if ia else 'SELECT record_time, xp_value FROM xp_records WHERE user_id = ? AND record_time LIKE ? AND season = ? ORDER BY record_time ASC', (uid,) if ia else (uid, f"{ty}/%", ts))
            rec = dict(cursor.fetchall()); xps, cxp = [], math.nan
            for t in times_raw:
                if t in rec: cxp = rec[t]
                xps.append(cxp)
            plt.plot(d_times, xps, marker='o', label=uname)
        conn.close()
        plt.title(f'みんなの成長記録（{title}）'); plt.grid(True, linestyle='--', alpha=0.7); plt.xticks(rotation=90); plt.legend(); plt.tight_layout()
        plt.savefig('all.png'); plt.close(); import time; time.sleep(1); await message.channel.send(file=discord.File('all.png'))

    elif message.content.startswith('!グラフ'):
        ty, ts, ia, title = parse_args(message.content, current_year, current_season)
        conn = sqlite3.connect('xp_data.db'); cursor = conn.cursor()
        cursor.execute('SELECT record_time, xp_value FROM xp_records WHERE user_id = ? ORDER BY record_time ASC' if ia else 'SELECT record_time, xp_value FROM xp_records WHERE user_id = ? AND record_time LIKE ? AND season = ? ORDER BY record_time ASC', (message.author.id,) if ia else (message.author.id, f"{ty}/%", ts))
        rec = cursor.fetchall(); conn.close()
        if not rec: await message.channel.send('データがありません'); return
        times = [f"{t[0][2:4]}年{int(t[0][5:7])}月{int(t[0][8:10])}日" for t in rec]
        plt.figure(figsize=(10, 5)); plt.plot(times, [r[1] for r in rec], marker='o')
        plt.title(f'{message.author.display_name} さんの成長記録 ({title})', fontsize=16)
        plt.grid(True, linestyle='--', alpha=0.7); plt.xticks(rotation=90); plt.tight_layout()
        plt.savefig('g.png'); plt.close(); import time; time.sleep(1); await message.channel.send(file=discord.File('g.png'))

    elif message.content.startswith('!ランキング'):
        ty, ts, ia, title = parse_args(message.content, current_year, current_season)
        conn = sqlite3.connect('xp_data.db'); cursor = conn.cursor()
        if ia: cursor.execute('SELECT user_name, MAX(xp_value) FROM xp_records GROUP BY user_id ORDER BY MAX(xp_value) DESC LIMIT 10')
        else: cursor.execute('SELECT user_name, xp_value FROM xp_records WHERE id IN (SELECT MAX(id) FROM xp_records WHERE record_time LIKE ? AND season = ? GROUP BY user_id) ORDER BY xp_value DESC LIMIT 10', (f"{ty}/%", ts))
        rank = cursor.fetchall(); conn.close()
        reply = f"🏆 **{title} ランキング** 🏆\n\n" + "\n".join([f"{['🥇','🥈','🥉'][i] if i<3 else f'**{i+1}位**'}：{r[0]} ({r[1]} XP)" for i, r in enumerate(rank)])
        await message.channel.send(reply)

    elif message.content.startswith('!表彰式'):
        if not ((now.month in [3, 6, 9, 12]) and (1 <= now.day <= 7)):
            await message.channel.send('⚠️ 表彰式はシーズン終了直後の1週間限定です！'); return
        target_s = {"3":"冬シーズン", "6":"春シーズン", "9":"夏シーズン", "12":"秋シーズン"}.get(str(now.month), current_season)
        conn = sqlite3.connect('xp_data.db'); cursor = conn.cursor()
        cursor.execute('SELECT user_name, user_id, xp_value, record_time FROM xp_records WHERE season = ? ORDER BY record_time ASC', (target_s,))
        rows = cursor.fetchall(); conn.close()
        if not rows: await message.channel.send('データなし'); return
        umap = {}
        for un, ui, xp, rt in rows:
            if ui not in umap: umap[ui] = {'n': un, 'r': []}
            umap[ui]['r'].append({'x': xp, 't': rt})
        mp, ls = [], []
        for ui, info in umap.items():
            recs = info['r']
            mp.append((info['n'], len(recs)))
            et = datetime.strptime(recs[-1]['t'], '%Y/%m/%d %H:%M')
            st = (et - timedelta(days=7)).strftime('%Y/%m/%d %H:%M')
            bx = next((r['x'] for r in reversed(recs) if r['t'] <= st), recs[0]['x'])
            ls.append((info['n'], recs[-1]['x'] - bx))
        mp.sort(key=lambda x: x[1], reverse=True); ls.sort(key=lambda x: x[1], reverse=True)
        await message.channel.send(f"🎉 **{target_s} 表彰式** 🎉\n🦑 **潜ったで賞**: {mp[0][0]}({mp[0][1]}回)\n🔥 **ラストスパート**: {ls[0][0]}(+{ls[0][1]} XP)")

    elif message.content == '!リセット':
        conn = sqlite3.connect('xp_data.db'); cursor = conn.cursor()
        cursor.execute('DELETE FROM xp_records WHERE user_id = ? AND season = ? AND record_time LIKE ?', (message.author.id, current_season, f"{current_year}/%"))
        c = cursor.rowcount; conn.commit(); conn.close()
        await message.channel.send(f'🗑️ {c}件リセットしました！' if c > 0 else 'データなし')

    match = re.search(r'xp\s*([0-9]+)|([0-9]+)\s*xp', message.content, re.IGNORECASE)
    if match:
        xp = int(match.group(1) or match.group(2))
        conn = sqlite3.connect('xp_data.db'); cursor = conn.cursor()
        cursor.execute('INSERT INTO xp_records (user_id, user_name, xp_value, record_time, season) VALUES (?, ?, ?, ?, ?)', (message.author.id, message.author.display_name, xp, now.strftime('%Y/%m/%d %H:%M'), current_season))
        conn.commit(); conn.close()
        await message.channel.send(f'✅ {xp} XP を保存！')

client.run(TOKEN)
