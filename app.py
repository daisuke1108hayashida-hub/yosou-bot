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
    raise RuntimeError("環境変数が不足: LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN")

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
    if not m:
        return None
    place_name, race_no, yyyymmdd = m.group(1), int(m.group(2)), m.group(3)
    if not yyyymmdd:
        yyyymmdd = (datetime.utcnow() + timedelta(hours=9)).strftime("%Y%m%d")
    place_no = PLACE_NO.get(place_name)
    if not place_no:
        return None
    return place_no, race_no, yyyymmdd, place_name

# ====== 日和 直前情報取得（ロバスト版） ======
def fetch_biyori_beforeinfo(place_no: int, race_no: int, yyyymmdd: str):
    url = (
        f"https://kyoteibiyori.com/race_shusso.php"
        f"?place_no={place_no}&race_no={race_no}&hiduke={yyyymmdd}&slider=4"
    )
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Referer": "https://kyoteibiyori.com/",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }

    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code != 200:
                last_err = f"status={r.status_code}"
                time.sleep(1.0 * (attempt + 1))
                continue

            soup = BeautifulSoup(r.text, "lxml")

            # ---- 1) 一番「それっぽい」テーブルを採点して選ぶ
            KEYWORDS = ["展示", "展示タイム", "周回", "周り足", "直線", "ST", "スタート"]
            best_tbl, best_score = None, -1
            for tbl in soup.find_all("table"):
                txt = tbl.get_text(" ", strip=True)
                score = sum(1 for k in KEYWORDS if k in txt)
                # 行数・列数で少し加点（データテーブルっぽさ）
                rows = tbl.find_all("tr")
                if 6 <= len(rows) <= 12:
                    score += 1
                if score > best_score:
                    best_score = score
                    best_tbl = tbl

            if not best_tbl or best_score < 3:
                last_err = "table-not-found"
                time.sleep(1.0 * (attempt + 1))
                continue

            # ---- 2) 見出し（th）から列インデックスを特定（表記ゆれ吸収）
            header_map = {
                "展示": "tenji", "展示ﾀｲﾑ": "tenji", "展示タイム": "tenji",
                "周回": "shukai", "周回ﾀｲﾑ": "shukai",
                "周り足": "mawari", "回り足": "mawari",
                "直線": "chokusen",
                "ST": "st", "ＳＴ": "st", "スタート": "st"
            }
            ths = [th.get_text(strip=True) for th in best_tbl.find_all("th")]
            col_idx = {}
            for idx, h in enumerate(ths):
                for k, v in header_map.items():
                    if k in h and v not in col_idx:
                        col_idx[v] = idx

            # ---- 3) 1～6号艇の行を読む（ヘッダ行の次を想定だが柔軟に）
            rows = best_tbl.find_all("tr")
            # ヘッダ行の位置（thが多い行）を推定
            head_i = 0
            for i, tr in enumerate(rows[:5]):
                if tr.find("th"):
                    head_i = i
            data_rows = rows[head_i+1:head_i+7]

            out = []
            for tr in data_rows:
                tds = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if not tds:
                    continue

                def get_by_col(name):
                    if name in col_idx and col_idx[name] < len(tds):
                        return tds[col_idx[name]]
                    return None

                rec = {
                    "tenji": get_by_col("tenji"),
                    "shukai": get_by_col("shukai"),
                    "mawari": get_by_col("mawari"),
                    "chokusen": get_by_col("chokusen"),
                    "st": get_by_col("st"),
                    "raw": tds,
                }

                # フォールバック：数値らしいものを補完
                if not rec["tenji"]:
                    m = re.search(r"\d+\.\d+", " ".join(tds))
                    rec["tenji"] = m.group(0) if m else None
                if not rec["st"]:
                    m = re.search(r"(?:F)?\d?\.\d+|F\d+", " ".join(tds))
                    rec["st"] = m.group(0) if m else None

                out.append(rec)

            if len(out) >= 6:
                return out

            last_err = "rows-short"
            time.sleep(1.0 * (attempt + 1))

        except Exception as e:
            last_err = str(e)
            time.sleep(1.0 * (attempt + 1))

    print(f"[biyori] fetch failed: url={url} err={last_err}")
    return None

def build_prediction_from_biyori(binfo):
    def to_float(x):
        try: return float(x)
        except: return None

    scores = []
    for i, r in enumerate(binfo, start=1):
        tenji = to_float(r["tenji"])
        choku = to_float(r["chokusen"])
        st_raw = r["st"]
        st_val = None
        if st_raw:
            if st_raw.startswith("F"):
                st_val = 9.99
            else:
                try:
                    st_val = float(st_raw.replace("F", ""))
                except:
                    st_val = 9.99
        s = 0.0
        if tenji: s += (7.00 - min(7.00, tenji)) * 10
        if choku: s += (8.00 - min(8.00, choku)) * 5
        if st_val is not None: s += (0.30 - min(0.30, st_val)) * 20
        scores.append((i, s))

    scores.sort(key=lambda x: x[1], reverse=True)
    order = [x[0] for x in scores[:4]] or [1,2,3,4]
    head = order[0]

    expo = f"展開予想：①{head}の機力優位。本命は{head}中心。"
    hon  = [f"{order[0]}-{order[1]}-{order[2]}", f"{order[0]}-{order[2]}-{order[1]}"]
    osa  = [f"{order[1]}-{order[0]}-{order[2]}", f"{order[0]}-{order[1]}-{order[3]}"]
    nerai= [f"{order[0]}-{order[3]}-{order[1]}", f"{order[3]}-{order[0]}-{order[1]}"]
    return expo, hon, osa, nerai

def build_reply(place_name, race_no, yyyymmdd):
    binfo = fetch_biyori_beforeinfo(PLACE_NO[place_name], race_no, yyyymmdd)
    if binfo:
        expo, hon, osa, nerai = build_prediction_from_biyori(binfo)
        url = (f"https://kyoteibiyori.com/race_shusso.php"
               f"?place_no={PLACE_NO[place_name]}&race_no={race_no}&hiduke={yyyymmdd}&slider=4")
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

    return "直前情報の取得に失敗しました。少し待ってから再度お試しください。"

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
        msg = "入力例：『丸亀 8』 / 『唐津 12 20250811』\n日和の直前情報で簡易展開と買い目を返します。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg))
        return

    parsed = parse_user_input(text)
    if not parsed:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("入力例：『丸亀 8』 / 『丸亀 8 20250811』\n'help' で使い方を表示します。")
        )
        return

    place_no, race_no, yyyymmdd, place_name = parsed
    reply = build_reply(place_name, race_no, yyyymmdd)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(reply))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
