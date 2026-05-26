import discord
import re
import os
import time
import math
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import matplotlib.dates as mdates
from flask import Flask
from threading import Thread

# Flaskサーバー用 (Renderスリープ防止)
app = Flask('')
@app.route('/')
def home(): return "I am alive!"
t = Thread(target=lambda: app.run(host='0.0.0.0', port=10000)); t.start()

# 日本語フォント設定
def setup_font():
    font_url = "https://github.com/googlefonts/noto-cjk/raw/main/Sans/OTC/NotoSansCJK-Regular.ttc"
    font_path = "NotoSansCJK.ttc"
    try:
        if not os.path.exists(font_path):
            import urllib.request
            urllib.request.urlretrieve(font_url, font_path)
        fm.fontManager.addfont(font_path)
        plt.rcParams['font.family'] = 'Noto Sans CJK JP'
    except Exception as e:
        print(f"Font setup warning: {e}")
        plt.rcParams['font.family'] = 'sans-serif'

setup_font()

# ==================== 【設定部分】 ====================
TOKEN = os.environ.get('DISCORD_TOKEN')
TARGET_CHANNEL_ID = 1474973509217423401  # ★XP報告するチャンネルID
LOG_CHANNEL_ID = 1508739566138294522     # ★バックアップ用チャンネルID
# ====================================================

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

def parse_args(content, current_year, current_season_type):
    if "全期間" in content: return str(current_year), current_season_type, True, "全期間"
    target_year = str(current_year)
    year_match = re.search(r'([0-9]{4})年', content)
    if year_match: target_year = year_match.group(1)
    target_season = current_season_type
    for s in ["春シーズン", "夏シーズン", "秋シーズン", "冬シーズン", "春", "夏", "秋", "冬"]:
        if s in content:
            target_season = s if "シーズン" in s else f"{s}シーズン"
            break
    return target_year, target_season, False, f"{target_year}年 {target_season}"

async def get_all_records():
    channel = client.get_channel(LOG_CHANNEL_ID)
    data = {} 
    if not channel: return data
    async for message in channel.history(limit=5000, oldest_first=True):
        if message.author == client.user:
            p = message.content.split('|')
            if len(p) >= 6:
                uid, uname, xp, t_str, season, msg_id = int(p[0]), p[1], int(p[2]), p[3], p[4], int(p[5])
                try: dt = datetime.strptime(t_str, '%Y/%m/%d %H:%M')
                except: dt = datetime.now()
                if uid not in data: data[uid] = {'name': uname, 'records': []}
                data[uid]['name'] = uname
                data[uid]['records'].append({'xp': xp, 'time': dt, 'season': season, 'msg_id': msg_id})
    
    for uid in data:
        data[uid]['records'].sort(key=lambda x: x['time'])
        
    return data

@client.event
async def on_ready():
    print(f'{client.user} が起動しました！')

@client.event
async def on_message(message):
    if message.author == client.user: return
    now = datetime.now()
    current_season_type = "春シーズン" if now.month in [3,4,5] else "夏シーズン" if now.month in [6,7,8] else "秋シーズン" if now.month in [9,10,11] else "冬シーズン"
    curr_season_full_str = f"{now.year}年 {current_season_type}"

    if message.channel.id == TARGET_CHANNEL_ID:
        # XP入力
        match = re.search(r'xp\s*([0-9]+)|([0-9]+)\s*xp', message.content, re.IGNORECASE)
        if match:
            new_xp = int(match.group(1) or match.group(2))
            if not (500 <= new_xp < 5000):
                await message.channel.send("⚠️ パワーは500〜5000で入力してください！"); return
            
            all_d = await get_all_records()
            if message.author.id in all_d and all_d[message.author.id]['records']:
                last_xp = all_d[message.author.id]['records'][-1]['xp']
                if abs(new_xp - last_xp) > 500:
                    await message.channel.send(f"⚠️ 前回記録({last_xp} XP)から±500以上の急激な増減があるため保存できません！"); return
            
            log_channel = client.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                await log_channel.send(f"{message.author.id}|{message.author.display_name}|{new_xp}|{now.strftime('%Y/%m/%d %H:%M')}|{curr_season_full_str}|{message.id}")
                await message.channel.send(f"✅ {new_xp} XP を保存しました！")

        # グラフ
        elif message.content.startswith('!グラフ'):
            ty, ts, ia, title = parse_args(message.content, now.year, current_season_type)
            all_d = await get_all_records()
            if message.author.id not in all_d:
                await message.channel.send("⚠️ データがありません。"); return
            
            recs = all_d[message.author.id]['records']
            if not ia: recs = [r for r in recs if r['season'] == f"{ty}年 {ts}"]
            if not recs:
                await message.channel.send(f"⚠️ {title} のデータがありません。"); return
            
            fig, ax = plt.subplots(figsize=(10, 5))
            ax.plot([r['time'] for r in recs], [r['xp'] for r in recs], marker='o', color='#1f77b4', linewidth=2, markersize=6)
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))
            plt.xticks(rotation=90, fontsize=10) 
            ax.set_title(f"{message.author.display_name} さんの成長記録 ({title})", fontsize=15)
            ax.grid(True, linestyle='--', alpha=0.6)
            plt.tight_layout()
            
            fname = f'g_{message.author.id}_{int(time.time())}.png'
            plt.savefig(fname); plt.close(); await message.channel.send(file=discord.File(fname))

        # 全員のグラフ
        elif message.content.startswith('!全員のグラフ'):
            ty, ts, ia, title = parse_args(message.content, now.year, current_season_type)
            all_d = await get_all_records()
            fig, ax = plt.subplots(figsize=(10, 5))
            has_data = False
            
            for uid, info in all_d.items():
                recs = info['records']
                if not ia: recs = [r for r in recs if r['season'] == f"{ty}年 {ts}"]
                if recs:
                    has_data = True
                    ax.plot([r['time'] for r in recs], [r['xp'] for r in recs], marker='o', markersize=4, label=info['name'])
            
            if not has_data:
                await message.channel.send(f"⚠️ {title} のデータがありません。"); return
            
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))
            plt.xticks(rotation=90, fontsize=10) 
            ax.set_title(f"みんなのXP比較グラフ ({title})", fontsize=15)
            ax.grid(True, linestyle='--', alpha=0.6)
            ax.legend(loc='upper left', bbox_to_anchor=(1, 1))
            plt.tight_layout()
            
            fname = f'all_{int(time.time())}.png'
            plt.savefig(fname); plt.close(); await message.channel.send(file=discord.File(fname))

        # ランキング
        elif message.content.startswith('!ランキング'):
            ty, ts, ia, title = parse_args(message.content, now.year, current_season_type)
            all_d = await get_all_records()
            ranking = []
            for uid, info in all_d.items():
                recs = info['records']
                if not ia: recs = [r for r in recs if r['season'] == f"{ty}年 {ts}"]
                if recs: ranking.append((info['name'], recs[-1]['xp']))
            if not ranking:
                await message.channel.send(f"⚠️ {title} のデータがありません。"); return
            ranking.sort(key=lambda x: x[1], reverse=True)
            res = f"🏆 **{title} ランキング** 🏆\n\n"
            for i, (name, xp) in enumerate(ranking[:10]): 
                medal = ['🥇','🥈','🥉'][i] if i<3 else f"**{i+1}位**"
                res += f"{medal}：{name} ({xp} XP)\n"
            await message.channel.send(res)

        # 表彰式
        elif message.content.startswith('!表彰式'):
            if not ((now.month in [3, 6, 9, 12]) and (1 <= now.day <= 7)):
                await message.channel.send('⚠️ 表彰式はシーズン終了直後の1週間限定です！'); return
            target_s_type = {"3": "冬シーズン", "6": "春シーズン", "9": "夏シーズン", "12": "秋シーズン"}.get(str(now.month))
            target_season_str = f"{now.year}年 {target_s_type}"
            all_d = await get_all_records(); most_played, last_spurt = [], []
            for uid, info in all_d.items():
                s_recs = [r for r in info['records'] if r['season'] == target_season_str]
                if not s_recs: continue
                most_played.append((info['name'], len(s_recs)))
                if len(s_recs) >= 2:
                    base = s_recs[0]
                    for r in reversed(s_recs):
                        if r['time'] <= s_recs[-1]['time'] - timedelta(days=7): base = r; break
                    last_spurt.append((info['name'], s_recs[-1]['xp'] - base['xp']))
                else: last_spurt.append((info['name'], 0))
            if not most_played:
                await message.channel.send("⚠️ 表彰データがありません。"); return
            most_played.sort(key=lambda x: x[1], reverse=True); last_spurt.sort(key=lambda x: x[1], reverse=True)
            await message.channel.send(f"🎉 **{target_season_str} 表彰式** 🎉\n\n🦑 **一番潜ったで賞**: {most_played[0][0]}さん ({most_played[0][1]}回)\n🔥 **ラストスパート賞**: {last_spurt[0][0]}さん (+{last_spurt[0][1]} XP)")

        # 直近1件削除
        elif message.content == '!リセット':
            log_channel = client.get_channel(LOG_CHANNEL_ID)
            deleted = False
            async for m_log in log_channel.history(limit=100):
                if m_log.author == client.user and m_log.content.startswith(f"{message.author.id}|"):
                    await m_log.delete(); deleted = True; break
            await message.channel.send("🗑️ 直近の記録を1件リセットしました！" if deleted else "⚠️ 削除対象がありません。")

        # 【NEW】自分の全データ削除
        elif message.content == '!マイデータ全削除':
            log_channel = client.get_channel(LOG_CHANNEL_ID)
            if not log_channel: return
            await message.channel.send("🗑️ あなたの全データを削除しています...（少し時間がかかります）")
            deleted_count = 0
            async for m_log in log_channel.history(limit=5000):
                if m_log.author == client.user and m_log.content.startswith(f"{message.author.id}|"):
                    await m_log.delete()
                    deleted_count += 1
            await message.channel.send(f"✅ あなたの過去データ {deleted_count} 件をすべて完全に消去しました！")

        # 【NEW】サーバーの全データ強制リセット
        elif message.content == '!全員のデータ強制リセット':
            log_channel = client.get_channel(LOG_CHANNEL_ID)
            if not log_channel: return
            await message.channel.send("⚠️ サーバーの全データを初期化しています...（少し時間がかかります）")
            deleted_count = 0
            async for m_log in log_channel.history(limit=5000):
                if m_log.author == client.user:
                    await m_log.delete()
                    deleted_count += 1
            await message.channel.send(f"🚨 システムの全XPデータ（計 {deleted_count} 件）を完全に初期化しました！")

# 削除・編集の自動連動
@client.event
async def on_raw_message_delete(payload):
    if payload.channel_id != TARGET_CHANNEL_ID: return
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if not log_channel: return
    async for m_log in log_channel.history(limit=100):
        if m_log.author == client.user and m_log.content.endswith(f"|{payload.message_id}"):
            await m_log.delete(); break

@client.event
async def on_raw_message_edit(payload):
    if payload.channel_id != TARGET_CHANNEL_ID: return
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    content = payload.data.get('content')
    if not log_channel or not content: return
    match = re.search(r'xp\s*([0-9]+)|([0-9]+)\s*xp', content, re.IGNORECASE)
    async for m_log in log_channel.history(limit=100):
        if m_log.author == client.user and m_log.content.endswith(f"|{payload.message_id}"):
            if match:
                new_xp = int(match.group(1) or match.group(2))
                if not (500 <= new_xp < 5000): return
                
                parts = m_log.content.split('|')
                parts[2] = str(new_xp)
                await m_log.edit(content="|".join(parts))
            else: 
                await m_log.delete()
            break

client.run(TOKEN)
