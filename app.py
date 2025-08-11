import os
import re
import time
import unicodedata
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# ====== 環境変数 ======
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINE envs missing")

# ====== Flask / LINE ======
app = Flask(__name__)
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ====== 場コード（ボートレース日和） ======
PLACE_NO = {
    "桐生": 1, "戸田": 2, "江戸川": 3, "平和島": 4, "多摩川": 5,
    "浜名湖": 6, "蒲郡": 7, "常滑": 8, "津": 9, "三国": 10,
    "びわこ": 11, "住之江": 12, "尼崎": 13, "鳴門": 14, "丸亀": 15,
    "児島": 16, "宮島": 17, "徳山": 18, "下関": 19, "若松": 20,
    "芦屋": 21, "福岡": 22, "唐津": 23, "大村": 24,
}

# ====== ユーティリティ ======
FW_TO_HW = str.maketrans("０１２３４５６７８９", "0123456789")
def normalize_text(s: str) -> str:
    return unicodedata.normalize("NFKC", s).translate(FW_TO_HW).strip()

def parse_user_input(text: str):
    t = normalize_text(text)
    m = re.match(r"^\s*(\S+)\s+(\d{1,2})(?:\s+(\d{8}))?\s*$", t)
    if not m: return None
    place_name, race_no, yyyymmdd = m.group(1), int(m.group(2)), m.group(3)
    if not yyyymmdd:
        yyyymmdd = (datetime.utcnow() + timedelta(hours=9)).strftime("%Y%m%d")
    if place_name not in PLACE_NO: return None
    return PLACE_NO[place_name], race_no, yyyymmdd, place_name

# ====== 日和 直前情報 取得 ======

# テーブル方式（従来）
def _parse_table_style(soup: BeautifulSoup):
    KEYWORDS = ["展示", "展示タイム", "周回", "周り足", "直線", "ST", "スタート"]
    best_tbl, best_score = None, -1
    for tbl in soup.find_all("table"):
        txt = tbl.get_text(" ", strip=True)
        score = sum(1 for k in KEYWORDS if k in txt)
        rows = tbl.find_all("tr")
        if 6 <= len(rows) <= 12: score += 1
        if score > best_score: best_tbl, best_score = tbl, score
    if not best_tbl or best_score < 3:
        return None

    header_map = {
        "展示": "tenji", "展示ﾀｲﾑ": "tenji", "展示タイム": "tenji",
        "周回": "shukai", "周回ﾀｲﾑ": "shukai",
        "周り足": "mawari", "回り足": "mawari",
        "直線": "chokusen",
        "ST": "st", "ＳＴ": "st", "スタート": "st"
    }
    rows = best_tbl.find_all("tr")
    head_i = 0
    for i, tr in enumerate(rows[:5]):
        if tr.find("th"): head_i = i
    data_rows = rows[head_i+1:head_i+7]
    if len(data_rows) < 6: return None

    def get_by_col(tds, ths_idx, name):
        idx = ths_idx.get(name)
        if idx is None or idx >= len(tds): return None
        return tds[idx].get_text(strip=True)

    # thインデックス
    ths = [th.get_text(strip=True) for th in rows[head_i].find_all("th")]
    ths_idx = {}
    for i, h in enumerate(ths):
        for k, v in header_map.items():
            if k in h and v not in ths_idx: ths_idx[v] = i

    out = []
    for tr in data_rows:
        cells = tr.find_all(["td", "th"])
        tds = [c.get_text(strip=True) for c in cells]
        rec = {
            "tenji": get_by_col(cells, ths_idx, "tenji"),
            "shukai": get_by_col(cells, ths_idx, "shukai"),
            "mawari": get_by_col(cells, ths_idx, "mawari"),
            "chokusen": get_by_col(cells, ths_idx, "chokusen"),
            "st": get_by_col(cells, ths_idx, "st"),
            "raw": tds,
        }
        out.append(rec)
    return out if len(out) >= 6 else None

# テキスト走査方式（divグリッドでも拾う）
def _parse_text_style(soup: BeautifulSoup):
    text = soup.get_text(" ", strip=True)
    # 行ブロック抽出
    def grab(label, nxt_labels):
        pattern = rf"{label}\s*(.+?)\s*(?:{'|'.join(map(re.escape,nxt_labels))}|$)"
        m = re.search(pattern, text)
        return m.group(1) if m else ""

    labels = ["展示", "周回", "周り足", "直線", "ST"]
    blocks = {}
    for i, lb in enumerate(labels):
        nxt = labels[i+1:] if i+1 < len(labels) else ["選手", "体重", "プロペラ", "チルト", "詳細"]
        blocks[lb] = grab(lb, nxt)

    # 数値パース
    num_re = re.compile(r"(?:F\.?\d+|F\d+|(?:\d+)?\.\d+)")
    rows = {}
    for lb in labels:
        vals = num_re.findall(blocks.get(lb, ""))
        # 先頭6つだけ採用
        rows[lb] = (vals + [None]*6)[:6]

    # 6艇分組み立て
    b = []
    for i in range(6):
        b.append({
            "tenji": rows["展示"][i],
            "shukai": rows["周回"][i],
            "mawari": rows["周り足"][i],
            "chokusen": rows["直線"][i],
            "st": rows["ST"][i],
            "raw": []
        })
    # 少なくとも展示が3つ以上見つかっていれば採用
    if sum(1 for x in rows["展示"] if x) >= 3:
        return b
    return None

def fetch_biyori_beforeinfo(place_no: int, race_no: int, yyyymmdd: str):
    url = (f"https://kyoteibiyori.com/race_shusso.php"
           f"?place_no={place_no}&race_no={race_no}&hiduke={yyyymmdd}&slider=4")
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120 Safari/537.36"),
        "Referer": "https://kyoteibiyori.com/",
        "Accept-Language": "ja,en;q=0.8",
    }
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code != 200:
                last_err = f"status={r.status_code}"
                time.sleep(0.7*(attempt+1)); continue
            soup = BeautifulSoup(r.text, "lxml")

            data = _parse_table_style(soup)
            if not data:
                data = _parse_text_style(soup)
            if data and len(data) >= 6:
                return data

            last_err = "table-not-found"
            time.sleep(0.7*(attempt+1))
        except Exception as e:
            last_err = str(e)
            time.sleep(0.7*(attempt+1))
    print(f"[biyori] fetch failed: url={url} err={last_err}")
    return None

# ====== 予想生成（簡易） ======
def build_prediction_from_biyori(binfo):
    def f(x):
        if x is None: return None
        try:
            return float(x if x.startswith("0") or not x.startswith(".") else "0"+x)
        except: return None

    scores = []
    for lane, r in enumerate(binfo, start=1):
        tenji = f(r["tenji"])
        choku = f(r["chokusen"])
        st = r["st"]
        st_v = None
        if st:
            if str(st).startswith("F"):
                st_v = 9.99
            else:
                try:
                    s = str(st)
                    if s.startswith("."): s = "0"+s
                    st_v = float(s)
                except:
                    st_v = 9.99
        s = 0.0
        if tenji is not None: s += (7.00 - min(7.00, tenji)) * 10
        if choku is not None: s += (8.00 - min(8.00, choku)) * 5
        if st_v is not None:  s += (0.30 - min(0.30, st_v)) * 20
        scores.append((lane, s))
    scores.sort(key=lambda x: x[1], reverse=True)
    order = [x[0] for x in scores[:4]] or [1,2,3,4]
    head = order[0]

    expo = f"展開予想：①{head}の機力優位。相手筆頭は内有利。"
    hon   = [f"{order[0]}-{order[1]}-{order[2]}", f"{order[0]}-{order[2]}-{order[1]}"]
    osa   = [f"{order[1]}-{order[0]}-{order[2]}", f"{order[0]}-{order[1]}-{order[3]}"]
    nerai = [f"{order[0]}-{order[3]}-{order[1]}", f"{order[3]}-{order[0]}-{order[1]}"]
    return expo, hon, osa, nerai

def build_reply(place_name, race_no, yyyymmdd):
    place_no = PLACE_NO[place_name]
    binfo = fetch_biyori_beforeinfo(place_no, race_no, yyyymmdd)
    if not binfo:
        return "直前情報の取得に失敗しました。少し待ってから再度お試しください。"

    expo, hon, osa, nerai = build_prediction_from_biyori(binfo)
    url = (f"https://kyoteibiyori.com/race_shusso.php"
           f"?place_no={place_no}&race_no={race_no}&hiduke={yyyymmdd}&slider=4")
    lines = []
    lines.append(f"📍 {place_name} {race_no}R（{datetime.strptime(yyyymmdd,'%Y%m%d').strftime('%Y/%m/%d')}）")
    lines.append("――――――――――――――――")
    lines.append(f"🧭 {expo}")
    lines.append("")
    lines.append(f"🎯 本線：{', '.join(hon)}")
    lines.append(f"🛡️ 抑え：{', '.join(osa)}")
    lines.append(f"💥 狙い：{', '.join(nerai)}")
    lines.append("")
    lines.append(f"(直前情報: 日和) {url}")
    return "\n".join(lines)

# ====== ルーティング ======
@app.route("/health")
def health(): return "ok", 200

@app.route("/")
def index(): return "ok", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    text = event.message.text.strip()
    if text.lower() in {"help", "ヘルプ"}:
        msg = "入力例：『丸亀 8』 / 『唐津 12 20250811』\n日和の直前情報から展開と買い目を返します。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg))
        return
    parsed = parse_user_input(text)
    if not parsed:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("入力例：『丸亀 8』 / 『丸亀 8 20250811』\n'help' で使い方を表示します。")
        ); return
    place_no, race_no, yyyymmdd, place_name = parsed
    reply = build_reply(place_name, race_no, yyyymmdd)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(reply))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
