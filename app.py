import os
import re
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("環境変数 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN が未設定です。")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 会場 → jcd
PLACE2JCD = {
    "桐生":"01","戸田":"02","江戸川":"03","平和島":"04","多摩川":"05",
    "浜名湖":"06","浜名":"06","蒲郡":"07","常滑":"08","津":"09",
    "三国":"10","びわこ":"11","琵琶湖":"11","住之江":"12","尼崎":"13",
    "鳴門":"14","丸亀":"15","児島":"16","宮島":"17","徳山":"18",
    "下関":"19","若松":"20","芦屋":"21","福岡":"22","唐津":"23","大村":"24",
}
JST = timezone(timedelta(hours=9))


def help_text():
    return ("使い方：\n"
            "『丸亀 8 20250811』/『丸亀 8』（日付省略=本日）\n"
            "直前情報を要約して “展開予想” と “本線/抑え/狙い” を返します。")

def parse_user_input(text: str):
    text = text.strip().replace("　", " ")
    if text.lower() in ("help","ヘルプ","使い方"):
        return {"cmd":"help"}
    m = re.match(r"^(?P<place>\S+)\s+(?P<race>\d{1,2})(?:\s+(?P<date>\d{8}))?$", text)
    if not m:
        return None
    place = m.group("place")
    race = int(m.group("race"))
    date_str = m.group("date")
    if place not in PLACE2JCD:
        return {"error": f"場名が不明です：{place}（例：丸亀 8）"}
    if not (1 <= race <= 12):
        return {"error": f"レース番号が不正です：{race}"}
    if date_str:
        try:
            d = datetime.strptime(date_str, "%Y%m%d").date()
        except ValueError:
            return {"error": f"日付形式が不正です：{date_str}（YYYYMMDD）"}
    else:
        d = datetime.now(JST).date()
    return {"cmd":"race","place":place,"race":race,"date":d}

def safe_float(x):
    try:
        s = str(x).replace("F","").replace("－","").replace("-","").strip()
        return float(s) if s else None
    except Exception:
        return None

# ---------- 直前情報（boatrace公式） ----------
@lru_cache(maxsize=128)
def fetch_beforeinfo(jcd: str, rno: int, yyyymmdd: str):
    url = f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={jcd}&hd={yyyymmdd}"
    ua = {"User-Agent":"Mozilla/5.0 (learning-bot)"}
    res = requests.get(url, headers=ua, timeout=15)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html5lib")

    # 直前情報に相当するテーブルを探索
    target = None
    for tb in soup.find_all("table"):
        ths = [th.get_text(strip=True) for th in tb.find_all("th")]
        if not ths: 
            continue
        joined = " ".join(ths)
        if any(k in joined for k in ("展示","展示タイム","周回","直線","ST","スタート")):
            target = tb
            break
    if not target:
        return None

    lanes = [None]*6
    rows = target.find_all("tr")
    header = [c.get_text(strip=True) for c in rows[0].find_all(["th","td"])]

    def pick(txts, keys):
        for k in keys:
            for i,h in enumerate(header):
                if k in h and i < len(txts):
                    v = txts[i].strip()
                    if v not in ("","–","－"): 
                        return v
        return ""

    for tr in rows[1:]:
        tds = tr.find_all("td")
        if not tds: 
            continue
        txts = [td.get_text(strip=True) for td in tds]

        lane = None
        for t in txts[:2]:
            if re.fullmatch(r"[1-6]", t):
                lane = int(t); break
        if not lane: 
            m = re.search(r"^(\d)号?艇", " ".join(txts))
            if m: lane = int(m.group(1))
        if not lane: 
            continue

        name = None
        for t in txts:
            if len(t)>=2 and not re.search(r"[0-9.－-]", t):
                name = t; break

        tenji = safe_float(pick(txts, ("展示タイム","展示")))
        lap = safe_float(pick(txts, ("周回","一周")))
        mawari = safe_float(pick(txts, ("周り足","回り足")))
        straight = safe_float(pick(txts, ("直線",)))
        st = safe_float(pick(txts, ("ST","スタート")))

        lanes[lane-1] = {
            "lane": lane, "name": name or f"{lane}号艇",
            "tenji": tenji, "lap": lap, "mawari": mawari, 
            "straight": straight, "st": st
        }

    lanes = [x or {"lane":i+1,"name":f"{i+1}号艇",
                   "tenji":None,"lap":None,"mawari":None,"straight":None,"st":None}
             for i,x in enumerate(lanes)]
    return {"url": url, "lanes": lanes}

# ---------- 予想ロジック ----------
def _rank(values, smaller_is_better=True):
    vals = []
    for v in values:
        if v is None:
            vals.append(float("inf") if smaller_is_better else float("-inf"))
        else:
            vals.append(v)
    idx = list(range(len(vals)))
    idx.sort(key=lambda i: vals[i] if smaller_is_better else -vals[i])
    ranks = [0]*len(vals)
    for r,i in enumerate(idx, start=1): 
        ranks[i] = r
    return ranks

def _unique_trifectas(head, seconds, thirds, max_n=3):
    """重複のない3連単の並びを生成"""
    tickets = []
    for s in seconds:
        if s == head: 
            continue
        for t in thirds:
            if t == head or t == s: 
                continue
            tickets.append(f"{head}-{s}-{t}")
            if len(tickets) >= max_n: 
                return tickets
    return tickets

def build_narrative_and_tickets(lanes):
    # ランク（小さいほど良い前提）
    r_ten = _rank([x["tenji"] for x in lanes])
    r_lap = _rank([x["lap"] for x in lanes])
    r_maw = _rank([x["mawari"] for x in lanes])
    r_str = _rank([x["straight"] for x in lanes])
    r_st  = _rank([x["st"] for x in lanes])

    # 総合スコア（欠損には平均順位を付与）
    def nz(r): 
        avg = sum(r)/len(r)
        return [avg if x==0 else x for x in r]

    r_ten,r_lap,r_maw,r_str,r_st = map(nz,(r_ten,r_lap,r_maw,r_str,r_st))
    scores = []
    for i in range(6):
        score = (0.30*r_ten[i] + 0.30*r_lap[i] + 0.15*r_maw[i] +
                 0.15*r_str[i] + 0.10*r_st[i])
        scores.append((score, i+1))
    order = [ln for _,ln in sorted(scores)]  # 良い順の枠番

    # 展開推定
    st_best = r_st.index(1)+1 if 1 in r_st else None
    str_best = r_str.index(1)+1 if 1 in r_str else None
    head = 1 if (1 in order[:2] and (st_best in (1,2) or r_ten[0] <= 2)) else order[0]

    if head == 1 and (st_best in (1,2)):
        scenario = "①先制の逃げ本線。相手筆頭は内差し勢。"
    elif head in (2,3) and (st_best in (2,3)):
        scenario = f"{head}コースの差し・まくり差し本線。①は残しまで。"
    elif head >= 4 and (str_best in (4,5,6)):
        scenario = f"外勢の出足直線が目立つ。{head}頭の強攻まで。"
    else:
        scenario = "混戦。直前の周回・展示上位を素直に評価。"

    # 買い目：本線=頭→相手2→相手3、抑え=相手→頭→相手、狙い=外の一発
    others = [x for x in order if x != head]
    sec = others[:2]                     # 2列目候補
    third = [x for x in order if x not in (head,)+tuple(sec)]

    main = _unique_trifectas(head, sec, third, max_n=3)
    cover = _unique_trifectas(sec[0], [head], [sec[1]]+third, max_n=2) if len(sec)>=2 else []
    # 狙い：外枠から一番良いのをチョイス
    outside = [x for x in order if x >= 4]
    if outside:
        v = outside[0]
        value = _unique_trifectas(v, [head]+sec, [n for n in range(1,7)], max_n=2)
    else:
        value = _unique_trifectas(others[0], [head], third, max_n=1)

    # 根拠（上位3艇の“得意項目”）
    strengths = []
    for ln in order[:3]:
        i = ln-1
        tags = []
        if r_ten[i] == 1: tags.append("展示◎")
        if r_lap[i] == 1: tags.append("周回◎")
        if r_maw[i] == 1: tags.append("周り足◎")
        if r_str[i] <= 2: tags.append("直線○")
        if r_st[i]  <= 2: tags.append("ST○")
        strengths.append(f"{ln}：{'・'.join(tags) if tags else 'バランス'}")

    return scenario, main, cover, value, strengths

def format_reply(place, rno, date, data):
    lanes = data["lanes"]
    scenario, main, cover, value, strengths = build_narrative_and_tickets(lanes)
    lines = [
        f"📍 {place} {rno}R（{date.strftime('%Y/%m/%d')}）",
        "――――――――――",
        f"🧭 展開予想：{scenario}",
        "🧩 根拠：" + " / ".join(strengths),
        "――――――――――",
        f"🎯 本線：{', '.join(main) if main else '—'}",
        f"🛡️ 抑え：{', '.join(cover) if cover else '—'}",
        f"💥 狙い：{', '.join(value) if value else '—'}",
        f"（直前情報 元: {data['url']}）"
    ]
    return "\n".join(lines)

# ---------- Routes ----------
@app.route("/health")
def health(): return "ok", 200

@app.route("/")
def index(): return "ok", 200

@app.route("/callback", methods=["POST"])
def callback():
    sig = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, sig)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    text = event.message.text.strip()
    p = parse_user_input(text)
    if not p:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="例）『丸亀 8』 / 『丸亀 8 20250811』 / 『help』"))
        return
    if p.get("cmd") == "help":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text()))
        return
    if "error" in p:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=p["error"]))
        return

    place, rno, d = p["place"], p["race"], p["date"]
    jcd = PLACE2JCD[place]
    ymd = d.strftime("%Y%m%d")

    try:
        data = fetch_beforeinfo(jcd, rno, ymd)
    except Exception:
        data = None

    if not data:
        fallback = f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={jcd}&hd={ymd}"
        msg = f"直前情報の取得に失敗しました。公式直前情報：{fallback}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    reply = format_reply(place, rno, d, data)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
