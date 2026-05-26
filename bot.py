import discord
import sqlite3
import re
import os  # ← 安全のため追加
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
import japanize_matplotlib
import math
from collections import Counter

# 安全のため、TOKENは環境変数から読み込むように変更しました
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
    target_year = str(current_year)
    target_season = current_season
    is_all_time = False

    if len(args) > 1:
        if "全期間" in args:
            is_all_time = True
        else:
            for arg in args[1:]:
                if "年" in arg:
                    target_year = re.sub(r'\D', '', arg)
                elif arg.isdigit() and len(arg) == 4:
                    target_year = arg
                elif "シーズン" in arg:
                    target_season = arg
    
    display_title = "全期間" if is_all_time else f"{target_year}年 {target_season}"
    return target_year, target_season, is_all_time, display_title

@client.event
async def on_message(message):
    if message.author == client.user:
        return
    if message.channel.id != TARGET_CHANNEL_ID:
        return

    now = datetime.now()
    month = now.month
    if month in [3, 4, 5]: current_season = "春シーズン"
    elif month in [6, 7, 8]: current_season = "夏シーズン"
    elif month in [9, 10, 11]: current_season = "秋シーズン"
    else: current_season = "冬シーズン"
    current_year = now.year

    if message.content.startswith('!全員のグラフ'):
        target_year, target_season, is_all_time, display_title = parse_args(message.content, current_year, current_season)
        conn = sqlite3.connect('xp_data.db')
        cursor = conn.cursor()
        if is_all_time:
            cursor.execute('SELECT DISTINCT record_time FROM xp_records ORDER BY record_time ASC')
        else:
            cursor.execute('SELECT DISTINCT record_time FROM xp_records WHERE record_time LIKE ? AND season = ? ORDER BY record_time ASC', (f"{target_year}/%", target_season))
        all_times_raw = [row[0] for row in cursor.fetchall()]
        if not all_times_raw:
            await message.channel.send(f'⚠️ まだ誰も {display_title} の記録をしていないみたいです！')
            conn.close()
            return
        dates = [t[:10] for t in all_times_raw]
        date_counts = Counter(dates)
        display_times = []
        for t in all_times_raw:
            y, m, d, h = t[2:4], int(t[5:7]), int(t[8:10]), int(t[11:13])
            date_str = f"{y}年{m}月{d}日" if is_all_time else f"{m}月{d}日"
            display_times.append(f"{date_str} {h}時" if date_counts[t[:10]] > 1 else date_str)
        cursor.execute('SELECT DISTINCT user_id, user_name FROM xp_records' if is_all_time else 'SELECT DISTINCT user_id, user_name FROM xp_records WHERE record_time LIKE ? AND season = ?', (f"{target_year}/%", target_season))
        users = cursor.fetchall()
        plt.figure(figsize=(10, 5))
        for user_id, user_name in users:
            cursor.execute('SELECT record_time, xp_value FROM xp_records WHERE user_id = ? ORDER BY record_time ASC' if is_all_time else 'SELECT record_time, xp_value FROM xp_records WHERE user_id = ? AND record_time LIKE ? AND season = ? ORDER BY record_time ASC', (user_id,) if is_all_time else (user_id, f"{target_year}/%", target_season))
            user_records = dict(cursor.fetchall())
            user_xps, current_xp = [], math.nan
            for t in all_times_raw:
                if t in user_records: current_xp = user_records[t]
                user_xps.append(current_xp)
            plt.plot(display_times, user_xps, marker='o', linestyle='-', linewidth=2, markersize=8, label=user_name)
        conn.close()
        plt.title(f'みんなの成長記録（{display_title}）', fontsize=16)
        plt.xlabel('記録日時', fontsize=12); plt.ylabel('XP（パワー）', fontsize=12)
        plt.xticks(rotation=90); plt.grid(True, linestyle='--', alpha=0.7); plt.legend(loc='center left', bbox_to_anchor=(1, 0.5)); plt.tight_layout()
        plt.savefig('all_graph.png'); plt.close(); await message.channel.send(file=discord.File('all_graph.png'))
        return

    elif message.content.startswith('!グラフ'):
        target_year, target_season, is_all_time, display_title = parse_args(message.content, current_year, current_season)
        conn = sqlite3.connect('xp_data.db')
        cursor = conn.cursor()
        cursor.execute('SELECT record_time, xp_value FROM xp_records WHERE user_id = ? ORDER BY record_time ASC' if is_all_time else 'SELECT record_time, xp_value FROM xp_records WHERE user_id = ? AND record_time LIKE ? AND season = ? ORDER BY record_time ASC', (message.author.id,) if is_all_time else (message.author.id, f"{target_year}/%", target_season))
        records = cursor.fetchall(); conn.close()
        if not records:
            await message.channel.send(f'⚠️ {message.author.display_name}さんの {display_title} のデータがありません！')
            return
        times_raw = [row[0] for row in records]
        dates = [t[:10] for t in times_raw]
        date_counts = Counter(dates)
        times = []
        for t in times_raw:
            y, m, d, h = t[2:4], int(t[5:7]), int(t[8:10]), int(t[11:13])
            date_str = f"{y}年{m}月{d}日" if is_all_time else f"{m}月{d}日"
            times.append(f"{date_str} {h}時" if date_counts[t[:10]] > 1 else date_str)
        xps = [row[1] for row in records]
        plt.figure(figsize=(10, 5)); plt.plot(times, xps, marker='o', color='b', linestyle='-', linewidth=2, markersize=8)
        plt.title(f'{message.author.display_name} さんの成長記録（{display_title}）', fontsize=16)
        plt.xticks(rotation=90); plt.grid(True, linestyle='--', alpha=0.7); plt.tight_layout()
        plt.savefig('graph.png'); plt.close(); await message.channel.send(file=discord.File('graph.png'))
        return

    elif message.content.startswith('!ランキング'):
        target_year, target_season, is_all_time, display_title = parse_args(message.content, current_year, current_season)
        conn = sqlite3.connect('xp_data.db'); cursor = conn.cursor()
        if is_all_time:
            cursor.execute('SELECT user_name, MAX(xp_value) FROM xp_records GROUP BY user_id ORDER BY MAX(xp_value) DESC LIMIT 10')
            display_title = "全期間（自己ベスト）"
        else:
            cursor.execute('SELECT user_name, xp_value FROM xp_records WHERE id IN (SELECT MAX(id) FROM xp_records WHERE record_time LIKE ? AND season = ? GROUP BY user_id) ORDER BY xp_value DESC LIMIT 10', (f"{target_year}/%", target_season))
        ranking = cursor.fetchall(); conn.close()
        if not ranking:
            await message.channel.send(f'⚠️ まだ誰も {display_title} の記録をしていません！')
            return
        reply = f"🏆 **{display_title} のトップ10** 🏆\n\n"
        for i, row in enumerate(ranking):
            reply += f"{['🥇','🥈','🥉'][i] if i<3 else f'**{i+1}位**'}：{row[0]} さん ({row[1]} XP)\n"
        await message.channel.send(reply)
        return

    elif message.content.startswith('!表彰式'):
        # ⚠️ テスト時は True に変更
        is_award_week = (now.month in [3, 6, 9, 12]) and (1 <= now.day <= 7)
        if not is_award_week:
            await message.channel.send('⚠️ 表彰式はシーズン終了直後の1週間（3, 6, 9, 12月の1日〜7日）限定コマンドです！')
            return
        
        target_season = {"3":"冬シーズン", "6":"春シーズン", "9":"夏シーズン", "12":"秋シーズン"}.get(str(now.month), current_season)
        conn = sqlite3.connect('xp_data.db'); cursor = conn.cursor()
        cursor.execute('SELECT user_name, user_id, xp_value, record_time FROM xp_records WHERE season = ? ORDER BY record_time ASC', (target_season,))
        rows = cursor.fetchall(); conn.close()
        if not rows: return await message.channel.send('データがありません。')
        
        user_data_map = {}
        for u_n, u_i, xp, r_t in rows:
            if u_i not in user_data_map: user_data_map[u_i] = {'name': u_n, 'records': []}
            user_data_map[u_i]['records'].append({'xp': xp, 'time': r_t})
        
        most_played, last_spurt, fmt = [], [], '%Y/%m/%d %H:%M'
        for u_i, u_info in user_data_map.items():
            records = u_info['records']
            most_played.append((u_info['name'], len(records)))
            end_t = datetime.strptime(records[-1]['time'], fmt)
            spurt_s_str = (end_t - timedelta(days=7)).strftime(fmt)
            base_xp = next((r['xp'] for r in reversed(records) if r['time'] <= spurt_s_str), records[0]['xp'])
            last_spurt.append((u_info['name'], records[-1]['xp'] - base_xp))
        
        most_played.sort(key=lambda x: x[1], reverse=True)
        last_spurt.sort(key=lambda x: x[1], reverse=True)
        await message.channel.send(f"🎉 **{target_season} 振り返り表彰式** 🎉\n\n🦑 **一番潜ったで賞**：{most_played[0][0]} ({most_played[0][1]}回)\n🔥 **ラストスパート賞**：{last_spurt[0][0]} (+{last_spurt[0][1]} XP)")
        return

    elif message.content == '!リセット':
        conn = sqlite3.connect('xp_data.db'); cursor = conn.cursor()
        cursor.execute('DELETE FROM xp_records WHERE user_id = ? AND season = ? AND record_time LIKE ?', (message.author.id, current_season, f"{current_year}/%"))
        count = cursor.rowcount; conn.commit(); conn.close()
        await message.channel.send(f'🗑️ {count}件リセットしました！' if count > 0 else 'データがありません。')
        return

    match = re.search(r'xp\s*([0-9]+)|([0-9]+)\s*xp', message.content, re.IGNORECASE)
    if match:
        xp = int(match.group(1) or match.group(2))
        conn = sqlite3.connect('xp_data.db'); cursor = conn.cursor()
        cursor.execute('INSERT INTO xp_records (user_id, user_name, xp_value, record_time, season) VALUES (?, ?, ?, ?, ?)', 
                       (message.author.id, message.author.display_name, xp, now.strftime('%Y/%m/%d %H:%M'), current_season))
        conn.commit(); conn.close()
        await message.channel.send(f'✅ {xp} XP を保存しました！')

client.run(TOKEN)