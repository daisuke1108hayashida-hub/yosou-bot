# -*- coding: utf-8 -*-
import os, re, time, datetime as dt
from typing import Dict, List, Tuple, Optional

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import InvalidSignatureError

# ====== 環境変数 ======
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("環境変数 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN が未設定です。")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ====== Flask ======
app = Flask(__name__)

@app.route("/")
def index():
    return "ok", 200

@app.route("/health")
def health():
    return "ok", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ====== 競艇場 → place_no ======
PLACE_NO = {
    "桐生":1,"戸田":2,"江戸川":3,"平和島":4,"多摩川":5,"浜名湖":6,"蒲郡":7,"常滑":8,"津":9,
    "三国":10,"びわこ":11,"琵琶湖":11,"住之江":12,"尼崎":13,"鳴門":14,"丸亀":15,"児島":16,"宮島":17,
    "徳山":18,"下関":19,"若松":20,"芦屋":21,"福岡":22,"唐津":23,"大村":24
}

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# ====== ユーティリティ ======
def parse_user_text(txt: str) -> Tuple[Optional[int], Optional[int], str]:
    """
    入力パターン:
      ・「丸亀 8 20250811」
      ・「丸亀 8」
      ・「help」
    """
    s = re.sub(r"\s+", " ", txt.strip())
    if s.lower() == "help":
        return None, None, "help"

    m = re.match(r"^(\S+)\s+(\d{1,2})(?:\s+(\d{8}))?$", s)
    if not m:
        return None, None, "bad"

    place_name = m.group(1)
    race_no = int(m.group(2))
    ymd = m.group(3) or dt.date.today().strftime("%Y%m%d")

    if place_name not in PLACE_NO:
        return None, None, "place-unknown"

    return PLACE_NO[place_name], race_no, ymd

# ====== 日和 直前情報 取得 ======
class BiyoriError(Exception):
    pass

def fetch_biyori_before(place_no: int, race_no: int, yyyymmdd: str) -> Dict[int, Dict[str, Optional[float]]]:
    """
    kyoteibiyori の直前情報を頑丈に取りにいく。
    1) PC/スマホ両方を順に試す
    2) table型／divリスト型の両方に対応
    返り値: { lane: {"tenji":float,"shukai":float,"mawari":float,"chokusen":float,"st":float} }
    """
    base = f"https://kyoteibiyori.com/race_shusso.php?place_no={place_no}&race_no={race_no}&hiduke={yyyymmdd}"
    urls = [
        base + "&slider=4",
        base,
        base + "&sp=1",
        base + "&sp=1&slider=4",
    ]

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Referer": "https://kyoteibiyori.com/",
        "Accept-Language": "ja,en;q=0.8",
    })

    last_html = ""
    for i, url in enumerate(urls, 1):
        try:
            r = sess.get(url, timeout=10)
            if r.status_code != 200:
                continue
            html = r.text
            last_html = html[:700]
            soup = BeautifulSoup(html, "lxml")

            # --- 1) テーブル型を探す（ヘッダに「展示」「周回」「周り足」「直線」「ST」がある） ---
            table = None
            for tb in soup.select("table"):
                head_txt = " ".join([th.get_text(strip=True) for th in tb.select("tr th")])
                if any(k in head_txt for k in ["展示", "周回", "周り足", "直線", "ST"]):
                    table = tb
                    break

            data: Dict[int, Dict[str, Optional[float]]] = {}

            if table:
                rows = table.select("tr")
                # データ行を推定（6艇分）
                cand = []
                for tr in rows:
                    tds = [td.get_text(strip=True) for td in tr.select("td")]
                    if len(tds) >= 8 and any("号" in x or re.search(r"^\d+号", x) for x in tds[:3]):
                        cand.append(tds)
                    # 別パターン: 「1」「2」…が先頭の行
                    elif len(tds) >= 6 and tds[0] in list("123456"):
                        cand.append(tds)

                # うまく拾えないときは、ヘッダの次の6行を強制採用
                if not cand:
                    trs = [tr for tr in rows if tr.select("td")]
                    if len(trs) >= 6:
                        cand = [[td.get_text(strip=True) for td in tr.select("td")] for tr in trs[:6]]

                def to_f(v: str) -> Optional[float]:
                    v = (v or "").replace("F", "").replace("－", "").replace("-", "").strip()
                    try:
                        return float(v)
                    except Exception:
                        return None

                lane_guess = 1
                for row in cand[:6]:
                    # 号艇の列はパターンが複数あるので雑に検出
                    lane = None
                    for cell in row[:3]:
                        m = re.search(r"(\d)\s*号", cell)
                        if m:
                            lane = int(m.group(1)); break
                    if lane is None and row and row[0].isdigit():
                        lane = int(row[0])
                    if lane is None:
                        lane = lane_guess
                    lane_guess += 1
                    # 「展示 周回 周り足 直線 ST」らしき列をスキャン
                    # 数字だけの小数/整数を優先
                    nums = [x for x in row if re.search(r"\d", x)]
                    # 代表値の抽出（安全寄り）
                    tenji = to_f(nums[0]) if len(nums) > 0 else None
                    shukai = to_f(nums[1]) if len(nums) > 1 else None
                    mawari = to_f(nums[2]) if len(nums) > 2 else None
                    choku = to_f(nums[3]) if len(nums) > 3 else None
                    st    = to_f(nums[4]) if len(nums) > 4 else None

                    data[lane] = {"tenji": tenji, "shukai": shukai, "mawari": mawari, "chokusen": choku, "st": st}

                if len(data) >= 3:
                    return data  # 成功

            # --- 2) テキスト/カード型（スマホで時々ある） ---
            cards = soup.select("div,li")
            lanes: Dict[int, Dict[str, Optional[float]]] = {}
            lane = 0
            buf: Dict[str, Optional[float]] = {"tenji": None, "shukai": None, "mawari": None, "chokusen": None, "st": None}
            for el in cards:
                t = el.get_text(" ", strip=True)
                m1 = re.search(r"(\d)\s*号艇", t)
                if m1:
                    if lane and any(v is not None for v in buf.values()):
                        lanes[lane] = buf
                    lane = int(m1.group(1))
                    buf = {"tenji": None, "shukai": None, "mawari": None, "chokusen": None, "st": None}
                # 数字抽出
                def pick(key: str, pat: str):
                    nonlocal t, buf
                    m = re.search(pat, t)
                    if m:
                        try:
                            buf[key] = float(m.group(1))
                        except Exception:
                            pass
                pick("tenji", r"展示[:：]\s*([0-9.]+)")
                pick("shukai", r"周回[:：]\s*([0-9.]+)")
                pick("mawari", r"周り足[:：]\s*([0-9.]+)")
                pick("chokusen", r"直線[:：]\s*([0-9.]+)")
                pick("st", r"ST[:：]\s*F?([0-9.]+)")

            if lane and any(v is not None for v in buf.values()):
                lanes[lane] = buf
            if len(lanes) >= 3:
                return lanes

            # 次URLを試す前に少し待つ
            time.sleep(0.6)

        except requests.RequestException:
            time.sleep(0.6)
            continue

    # 全部ダメ
    raise BiyoriError(f"table-not-found (tried 4 urls) head={last_html}")

# ====== スコアリング＆買い目 ======
def build_forecast(b: Dict[int, Dict[str, Optional[float]]]) -> Tuple[str, List[str], List[str], List[str]]:
    """
    直前データから簡易スコアを作って本線/抑え/狙いを出す
    """
    lanes = sorted(b.keys())
    if not lanes:
        return "直前データが不足しています。", [], [], []

    def nz(x, default):
        return x if isinstance(x, (int, float)) else default

    # 正規化用
    tenji_min = min(nz(b[i].get("tenji"), 999) for i in lanes)
    st_min    = min(nz(b[i].get("st"), 999) for i in lanes)
    choku_max = max(nz(b[i].get("chokusen"), 0) for i in lanes)
    mawa_max  = max(nz(b[i].get("mawari"), 0) for i in lanes)

    scores = {}
    for i in lanes:
        tenji    = nz(b[i].get("tenji"),  tenji_min)
        st       = nz(b[i].get("st"),     st_min)
        choku    = nz(b[i].get("chokusen"), choku_max)
        mawari   = nz(b[i].get("mawari"),  mawa_max)

        # 低いほど良い: 展示/スタート
        s_tenji = (tenji_min / max(tenji, 0.01)) * 40
        s_st    = (st_min    / max(st,    0.01)) * 25
        # 高いほど良い: 直線/周り足
        s_choku = (choku / max(choku_max, 0.01)) * 20
        s_mawa  = (mawari / max(mawa_max, 0.01)) * 15

        scores[i] = s_tenji + s_st + s_choku + s_mawa

    order = sorted(scores.keys(), key=lambda k: scores[k], reverse=True)
    top = order[:3]

    # 展開コメント
    p = []
    lead = order[0]
    if lead == 1:
        p.append("①の逃げ本線。")
    else:
        p.append(f"{lead}コースの機力上位。{ 'イン不安なら差し優位' if lead in (2,3) else 'まくり・まくり差し狙い' }。")

    if b.get(1, {}).get("st") and b[1]["st"] == st_min:
        p.append("①のST反応は良好。")
    if choku_max and any(i!=lead and nz(b[i].get('chokusen'),0)>=choku_max*0.98 for i in lanes):
        p.append("直線好感触の艇が複数。波乱含み。")

    point_text = " ".join(p) if p else "混戦。"

    # 買い目
    # 本線：トップ→2番手→流し寄り
    a, b2, c = order[0], order[1], order[2] if len(order) > 2 else order[0]
    hon = [f"{a}-{b2}-{c}", f"{a}-{c}-{b2}"]

    # 抑え：イン残し or 2着付け
    if a != 1 and 1 in lanes:
        osa = [f"{a}-1-{b2}", f"{1}-{a}-{b2}"]
    else:
        osa = [f"{a}-{b2}-1", f"{a}-1-{b2}"]

    # 狙い：3番手の台頭
    nerai = [f"{b2}-{a}-{c}", f"{a}-{c}-1"]

    return point_text, hon, osa, nerai

# ====== 応答生成 ======
def make_reply(place_no: int, race_no: int, ymd: str) -> str:
    place_name = next(k for k,v in PLACE_NO.items() if v == place_no)
    head = f"📍 {place_name} {race_no}R（{ymd[:4]}/{ymd[4:6]}/{ymd[6:8]}）\n" + "—"*18 + "\n"

    biyori_url = f"https://kyoteibiyori.com/race_shusso.php?place_no={place_no}&race_no={race_no}&hiduke={ymd}&slider=4"

    try:
        bj = fetch_biyori_before(place_no, race_no, ymd)
    except BiyoriError as e:
        return head + "直前情報の取得に失敗しました。少し待ってから再度お試しください。\n" + f"(src: 日和 / {biyori_url})"

    comment, hon, osa, nerai = build_forecast(bj)

    out = [head]
    out.append(f"🧭 展開予想：{comment}\n")
    if hon:
        out.append("🎯 本線　： " + ", ".join(hon))
    if osa:
        out.append("🛡️ 抑え　： " + ", ".join(osa))
    if nerai:
        out.append("💥 狙い　： " + ", ".join(nerai))
    out.append(f"\n(直前情報 参考: {biyori_url})")
    return "\n".join(out)

# ====== LINEハンドラ ======
HELP_TEXT = (
    "入力例：『丸亀 8』 / 『丸亀 8 20250811』 / 『help』\n"
    "日和の直前情報を優先して予想を作ります。"
)

@handler.add(MessageEvent, message=TextMessage)
def on_text(event: MessageEvent):
    text = (event.message.text or "").strip()

    place_no, rno, mode = parse_user_text(text)
    if mode == "help":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(HELP_TEXT))
        return
    if mode == "bad":
        line_bot_api.reply_message(event.reply_token, TextSendMessage("入力例：『丸亀 8』 / 『丸亀 8 20250811』 / 『help』"))
        return
    if mode == "place-unknown":
        line_bot_api.reply_message(event.reply_token, TextSendMessage("場名が見つかりません。例：丸亀, 唐津, 住之江 など"))
        return

    try:
        msg = make_reply(place_no, rno, mode)  # mode には yyyymmdd が入っている
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg))
    except Exception:
        line_bot_api.reply_message(event.reply_token, TextSendMessage("予期せぬエラーです。少し待って再度お試しください。"))

# ====== Gunicorn 用 ======
app.app_context().push()
