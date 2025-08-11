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
    raise RuntimeError("環境変数が不足しています: LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN")

# ====== Flask / LINE SDK ======
app = Flask(__name__)
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ====== 競艇場名 → place_no（ボートレース日和） ======
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
    """
    例:
      丸亀 8
      丸亀 8 20250811
      唐津 12 20250811
    をパースして (place_no, race_no, yyyymmdd) を返す
    """
    t = normalize_text(text)
    m = re.match(r"^\s*(\S+)\s+(\d{1,2})(?:\s+(\d{8}))?\s*$", t)
    if not m:
        return None

    place_name, race_no, yyyymmdd = m.group(1), int(m.group(2)), m.group(3)

    # 日付省略時は「今日」
    if not yyyymmdd:
        yyyymmdd = datetime.utcnow() + timedelta(hours=9)  # JST
        yyyymmdd = yyyymmdd.strftime("%Y%m%d")

    # 場コード
    place_no = PLACE_NO.get(place_name)
    if not place_no:
        return None
    return place_no, race_no, yyyymmdd, place_name

# ====== 日和 直前情報スクレイパ ======
def fetch_biyori_beforeinfo(place_no: int, race_no: int, yyyymmdd: str):
    """
    直前情報テーブル（展示/周回/周り足/直線/ST など）を取得して配列で返す。
    返り値: list[dict] (1～6号艇の順) / 取得失敗時は None
    """
    url = (
        f"https://kyoteibiyori.com/race_shusso.php"
        f"?place_no={place_no}&race_no={race_no}&hiduke={yyyymmdd}&slider=4"
    )
    headers = {
        # Botブロックを避けるためブラウザっぽいUAとRefererを付与
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Referer": f"https://kyoteibiyori.com/",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }

    last_err = None
    for i in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=12)
            if resp.status_code != 200:
                last_err = f"status={resp.status_code}"
                time.sleep(1.2 * (i + 1))
                continue

            soup = BeautifulSoup(resp.text, "lxml")

            # テーブルを特定：ヘッダに「展示」「周回」「周り足」「直線」「ST」などが並ぶものを探す
            target_tbl = None
            for tbl in soup.find_all("table"):
                ths = [th.get_text(strip=True) for th in tbl.find_all("th")]
                header = "".join(ths)
                if ("展示" in header or "展示タイム" in header) and "周回" in header and "直線" in header and "ST" in header:
                    target_tbl = tbl
                    break

            if not target_tbl:
                last_err = "table-not-found"
                time.sleep(1.2 * (i + 1))
                continue

            rows = target_tbl.find_all("tr")
            data = []
            # 1〜6号艇ぶん抽出（ヘッダ行をスキップ）
            for tr in rows[1:7]:
                tds = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
                if not tds:
                    continue
                # ページ構造に左右されないよう、数値だけを柔軟に取得
                # だいたい [選手名/級, 展示, 周回, 周り足, 直線, ST, …] の順に来る想定
                # 数値カラムっぽいものだけ拾う
                nums = [x for x in tds if re.search(r"\d", x)]
                # 保険として長さチェック
                info = {
                    "tenji": None,
                    "shukai": None,
                    "mawari": None,
                    "chokusen": None,
                    "st": None,
                    "raw": tds
                }
                # 見つかった順に当てはめ（表示順が違っても最低限値は拾える）
                # ここはサイト変更に強めのゆるい割り当て
                def pick(pattern):
                    for x in nums:
                        if re.search(pattern, x):
                            return x
                    return None

                info["tenji"] = pick(r"^\d+\.\d+$")
                info["shukai"] = pick(r"^\d+\.\d+$")
                info["mawari"] = pick(r"^\d+\.\d+$")
                info["chokusen"] = pick(r"^\d+\.\d+$")
                info["st"] = pick(r"^F?\.?\d+$|^F\d+$")

                data.append(info)

            if len(data) >= 6:
                return data

            last_err = "rows-short"
            time.sleep(1.2 * (i + 1))

        except Exception as e:
            last_err = str(e)
            time.sleep(1.2 * (i + 1))

    print(f"[biyori] fetch failed: url={url} err={last_err}")
    return None

def build_prediction_from_biyori(binfo):
    """
    超シンプルな仮ロジック：
    - 展示/直線が良い（値が速い＝小さい）艇を上位
    - STが良い（数値小さい/Fは悪い）艇を加点
    返り値: 展開テキスト, 本線/抑え/狙い（各3連単候補の簡易リスト）
    """
    def to_float(x):
        try:
            return float(x)
        except:
            return None

    scores = []
    for i, r in enumerate(binfo, start=1):
        tenji = to_float(r["tenji"])
        choku = to_float(r["chokusen"])
        st = r["st"]
        st_val = None
        if st:
            if st.startswith("F"):
                st_val = 9.99  # 大減点
            else:
                st_val = to_float(st.replace("F", "")) or 9.99
        s = 0.0
        if tenji: s += (7.00 - min(7.00, tenji)) * 10   # 例: 6.70で +3pt
        if choku: s += (8.00 - min(8.00, choku)) * 5    # 例: 7.70で +1.5pt
        if st_val is not None: s += (0.30 - min(0.30, st_val)) * 20  # 0.12で +3.6pt
        scores.append((i, s, tenji, choku, st))

    scores.sort(key=lambda x: x[1], reverse=True)
    # ざっくり展開文
    head = scores[0][0]
    text = f"展開予想：①{head}の機力優位。本命は{head}中心。"

    # テンプレ買い目（超簡易）
    order = [x[0] for x in scores[:4]]  # 上位4艇
    if len(order) < 4:
        # データ取れないときの保険
        order = [1,2,3,4]

    # 本線/抑え/狙い（例）
    hon = [f"{order[0]}-{order[1]}-{order[2]}", f"{order[0]}-{order[2]}-{order[1]}"]
    osa = [f"{order[1]}-{order[0]}-{order[2]}", f"{order[0]}-{order[1]}-{order[3]}"]
    nerai = [f"{order[0]}-{order[3]}-{order[1]}", f"{order[3]}-{order[0]}-{order[1]}"]

    return text, hon, osa, nerai

def build_reply(place_name, race_no, yyyymmdd):
    # 日和優先
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

    # 失敗したらエラーメッセージ
    return "直前情報の取得に失敗しました。少し時間を空けてからもう一度お試しください。"

# ====== ルーティング ======
@app.route("/health")
def health():
    return "ok", 200

@app.route("/")
def index():
    return "ok", 200

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
        msg = "入力例：『丸亀 8』 / 『唐津 12 20250811』\n日和の直前情報を使って簡易展開と買い目を返します。"
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
    # 開発ローカル用（RenderはProcfileでgunicorn起動）
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
