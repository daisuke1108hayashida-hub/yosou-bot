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

# ---------------------------
# Flask & LINE setup
# ---------------------------
app = Flask(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("環境変数 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を設定してください。")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ---------------------------
# 会場コード（boatrace.jp jcd）
# ---------------------------
PLACE2JCD = {
    "桐生": "01", "戸田": "02", "江戸川": "03", "平和島": "04", "多摩川": "05",
    "浜名湖": "06", "浜名": "06", "蒲郡": "07", "常滑": "08", "津": "09",
    "三国": "10", "びわこ": "11", "琵琶湖": "11", "住之江": "12", "尼崎": "13",
    "鳴門": "14", "丸亀": "15", "児島": "16", "宮島": "17", "徳山": "18",
    "下関": "19", "若松": "20", "芦屋": "21", "福岡": "22", "唐津": "23", "大村": "24",
}

JST = timezone(timedelta(hours=9))

# ---------------------------
# Helpers
# ---------------------------
def help_text() -> str:
    return (
        "使い方：\n"
        "・直前情報＆簡易予想 →『丸亀 8 20250811』のように送信\n"
        "　（日付省略可：例『丸亀 8』は今日）\n\n"
        "返却内容：直前タイム/周回/直線/ST など → 本線/抑え/狙い & 展開予想\n"
        "※データ取得元: boatrace.jp（直前情報）"
    )

def parse_user_input(text: str):
    """
    『{場} {R} [YYYYMMDD]』をパース
    """
    text = text.strip().replace("　", " ")
    if text.lower() in ("help", "ヘルプ", "使い方"):
        return {"cmd": "help"}

    m = re.match(r"^(?P<place>\S+)\s+(?P<race>\d{1,2})(?:\s+(?P<date>\d{8}))?$", text)
    if not m:
        return None

    place = m.group("place")
    race = int(m.group("race"))
    date_str = m.group("date")

    if place not in PLACE2JCD:
        return {"error": f"場名が分かりません：{place}（例：丸亀 8）"}

    if not (1 <= race <= 12):
        return {"error": f"レース番号が不正です：{race}"}

    if date_str:
        try:
            dt = datetime.strptime(date_str, "%Y%m%d").date()
        except ValueError:
            return {"error": f"日付の形式が不正です：{date_str}（YYYYMMDD）"}
    else:
        dt = datetime.now(JST).date()

    return {"cmd": "race", "place": place, "race": race, "date": dt}

def safe_float(x):
    try:
        return float(str(x).replace("F", "").replace("－", "").replace("-", "").strip())
    except Exception:
        return None

def rank_indices(values, reverse=False):
    """
    values: list[float|None]
    reverse=False（昇順=小さいほど上）/True（降順=大きいほど上）
    Noneは最後尾扱い
    return: list of rank (1..n)
    """
    pairs = []
    for i, v in enumerate(values):
        key = (1, 0) if v is None else (0, (-v if reverse else v))
        pairs.append((key, i))
    pairs.sort()
    ranks = [0]*len(values)
    r = 1
    for _, idx in pairs:
        ranks[idx] = r
        r += 1
    return ranks

# ---------------------------
# スクレイプ（直前情報）
# ---------------------------
@lru_cache(maxsize=128)
def fetch_beforeinfo(jcd: str, rno: int, yyyymmdd: str):
    """
    boatrace.jp 直前情報ページを取得して6艇分を辞書で返す
    ※簡易パーサ（HTMLの変更に弱いので例外時はNone返し）
    """
    url = f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={jcd}&hd={yyyymmdd}"
    t0 = time.time()
    ua = {"User-Agent": "Mozilla/5.0 (bot for learning)"}
    res = requests.get(url, headers=ua, timeout=15)
    res.raise_for_status()

    soup = BeautifulSoup(res.text, "html5lib")

    # 「直前情報」テーブルを総当りで探す（見出しに特定ヘッダがあるもの）
    tables = soup.find_all("table")
    target = None
    want_headers = {"展示", "展示タイム", "周回", "一周", "直線", "ST"}
    for tb in tables:
        ths = [th.get_text(strip=True) for th in tb.find_all("th")]
        if not ths:
            continue
        if any(h in "".join(ths) for h in want_headers):
            target = tb
            break

    if not target:
        return None

    # 行を走査して「枠番/選手名/展示/周回/直線/ST/体重/調整重量/チルト等」を拾う
    lanes = [None]*6
    rows = target.find_all("tr")
    # 構造が一定でないことが多いので各セルのテキストを見出し名から推定
    header = [c.get_text(strip=True) for c in rows[0].find_all(["th","td"])]
    # 予備：列名インデックス辞書
    col_idx = {}
    for idx, name in enumerate(header):
        col_idx[name] = idx

    for tr in rows[1:]:
        tds = tr.find_all("td")
        if not tds:
            continue
        txts = [td.get_text(strip=True) for td in tds]
        # 先頭に枠番/選手名が入っているケースが多い
        s = " ".join(txts)
        # 枠番推定
        lane = None
        m = re.search(r"^(\d)号?艇", s)
        if m:
            lane = int(m.group(1))
        else:
            # 1〜6のいずれかのセルに数字だけの列があれば使う
            for t in txts[:2]:
                if re.fullmatch(r"[1-6]", t):
                    lane = int(t)
                    break
        if not lane or not (1 <= lane <= 6):
            continue

        # 選手名
        name = None
        for t in txts:
            if len(t) >= 2 and all(ch not in t for ch in "0123456789."):
                # 数字を含まず、短すぎない → 人名っぽい
                name = t
                break

        def pick(*keys):
            for k in keys:
                for i, h in enumerate(header):
                    if k in h:
                        v = txts[i] if i < len(txts) else ""
                        if v in ("", "–", "－"):
                            continue
                        return v
            # 予備：テキスト全体から正規表現で拾う
            if "ST" in keys:
                m = re.search(r"ST\s*([-.0-9F]+)", s)
                return m.group(1) if m else ""
            return ""

        tenji = pick("展示タイム", "展示")
        lap = pick("周回", "一周")
        straight = pick("直線")
        st = pick("ST")

        lanes[lane-1] = {
            "lane": lane,
            "name": name or f"{lane}号艇",
            "tenji": safe_float(tenji),
            "lap": safe_float(lap),
            "straight": safe_float(straight),
            "st": safe_float(st),
        }

    # 取得できなかった枠がある場合はNone
    if any(v is None for v in lanes):
        # それでも何かしら返す
        lanes = [x or {"lane": i+1, "name": f"{i+1}号艇", "tenji": None, "lap": None, "straight": None, "st": None}
                 for i, x in enumerate(lanes)]

    elapsed = int((time.time() - t0)*1000)
    return {"url": url, "elapsed_ms": elapsed, "lanes": lanes}

def build_prediction(lanes):
    """
    6艇の dict を受け取り、簡易スコア→本線/抑え/狙い と 展開コメントを作成
    """
    # スコア：小さいほど良い指標(tenji, lap, straight, st)を合算（欠損は平均扱い）
    vs_tenji = [x["tenji"] for x in lanes]
    vs_lap = [x["lap"] for x in lanes]
    vs_str = [x["straight"] for x in lanes]
    vs_st = [x["st"] for x in lanes]

    # 欠損は平均で埋める
    def fill_avg(arr):
        vals = [v for v in arr if v is not None]
        avg = sum(vals)/len(vals) if vals else None
        return [avg if v is None else v for v in arr]

    vs_tenji = fill_avg(vs_tenji)
    vs_lap = fill_avg(vs_lap)
    vs_str = fill_avg(vs_str)
    vs_st = fill_avg(vs_st)

    r_tenji = rank_indices(vs_tenji, reverse=False)
    r_lap = rank_indices(vs_lap, reverse=False)
    r_str = rank_indices(vs_str, reverse=False)
    r_st = rank_indices(vs_st, reverse=False)

    for i, ln in enumerate(lanes):
        # 重み（お好みで調整）
        score = (
            0.35 * r_tenji[i] +
            0.30 * r_lap[i] +
            0.20 * r_str[i] +
            0.15 * r_st[i]
        )
        ln["score"] = score

    # 強さ順
    order = sorted(lanes, key=lambda x: x["score"])
    top = order[:3]  # 上位3艇

    # 展開のざっくり推定
    st_ranks = rank_indices(vs_st, reverse=False)
    st_best_lane = st_ranks.index(1) + 1
    scenario = ""
    if st_best_lane == 1 and order[0]["lane"] == 1:
        scenario = "①ST先制→逃げ本線。相手は②③。"
    elif st_best_lane in (2,3) and order[0]["lane"] in (2,3):
        scenario = f"{st_best_lane}コースのスタート良化→差し・まくり差し本線。"
    elif st_best_lane in (4,5,6):
        scenario = f"外の{st_best_lane}コースが気配↑ → 強攻のまくり差しまで。"
    else:
        scenario = "混戦。直前タイム上位を素直に評価。"

    # 買い目（例）：本線は上位2艇軸、抑えは1を相手に、狙いは外の一発
    fav1, fav2, alt = top[0]["lane"], top[1]["lane"], top[2]["lane"]
    head = 1 if any(x["lane"] == 1 for x in top[:2]) else fav1

    main = [f"{head}-{fav2}-{alt}", f"{head}-{alt}-{fav2}"]
    cover = [f"{fav2}-{head}-{alt}"]
    # 外枠でSTや直線が良い艇を狙いに
    value_cands = sorted([ln for ln in lanes if ln["lane"] >= 4],
                         key=lambda x: (rank_indices(vs_st, False)[x["lane"]-1],
                                        rank_indices(vs_str, False)[x["lane"]-1]))
    if value_cands:
        v = value_cands[0]["lane"]
        value = [f"{v}-{head}-{fav2}"]
    else:
        value = [f"{alt}-{head}-{fav2}"]

    return scenario, main, cover, value

def format_reply(place, rno, date, data):
    lanes = data["lanes"]
    scenario, main, cover, value = build_prediction(lanes)

    lines = [f"📍 {place} {rno}R 直前情報（{date.strftime('%Y/%m/%d')}）",
             f"src: {data['url']}  ⏱{data['elapsed_ms']}ms",
             "――――――――――"]
    for ln in lanes:
        t = f"{ln['lane']}号艇 {ln['name']}  展示:{ln['tenji']}  周回:{ln['lap']}  直線:{ln['straight']}  ST:{ln['st']}  [S:{ln['score']:.1f}]"
        lines.append(t)

    lines += [
        "――――――――――",
        f"🧭 展開予想：{scenario}",
        f"🎯 本線：{', '.join(main)}",
        f"🛡️ 抑え：{', '.join(cover)}",
        f"💥 狙い：{', '.join(value)}",
    ]
    return "\n".join(lines)

# ---------------------------
# Routes
# ---------------------------
@app.route("/health")
def health():
    return "ok", 200

@app.route("/")
def index():
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

# ---------------------------
# LINE Handlers
# ---------------------------
@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    text = event.message.text.strip()

    parsed = parse_user_input(text)
    if not parsed:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="入力例：『丸亀 8 20250811』/『丸亀 8』/『help』")
        )
        return

    if parsed.get("cmd") == "help":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text()))
        return

    if "error" in parsed:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=parsed["error"]))
        return

    place = parsed["place"]
    rno = parsed["race"]
    d = parsed["date"]
    jcd = PLACE2JCD[place]
    yyyymmdd = d.strftime("%Y%m%d")

    # キャッシュ付き取得
    try:
        data = fetch_beforeinfo(jcd, rno, yyyymmdd)
    except Exception as e:
        data = None

    if not data:
        fallback = f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={jcd}&hd={yyyymmdd}"
        msg = f"直前情報の取得に失敗しました。\n→ 公式直前情報: {fallback}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    reply = format_reply(place, rno, d, data)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
