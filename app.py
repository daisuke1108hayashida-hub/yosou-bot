import os
import re
import logging
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# ====== 基本設定 ======
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("yosou-bot")

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN が未設定です。")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

app = Flask(__name__)

# ====== 出す点数上限 ======
MAX_MAIN = 6
MAX_COVER = 4
MAX_ATTACK = 4

# ====== 場コード（日和 place_no） ======
PLACE_MAP = {
    "桐生": 1, "戸田": 2, "江戸川": 3, "平和島": 4, "多摩川": 5, "浜名湖": 6,
    "蒲郡": 7, "常滑": 8, "津": 9, "三国": 10, "びわこ": 11, "住之江": 12,
    "尼崎": 13, "鳴門": 14, "丸亀": 15, "児島": 16, "宮島": 17, "徳山": 18,
    "下関": 19, "若松": 20, "芦屋": 21, "福岡": 22, "唐津": 23, "大村": 24,
}

# ====== ルーティング ======
@app.route("/")
def root():
    return "ok", 200

@app.route("/health")
def health():
    return "ok", 200

# ====== LINE コールバック ======
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.exception("Invalid signature")
        abort(400)
    return "OK"

# ====== メッセージ処理 ======
@handler.add(MessageEvent, message=TextMessage)
def handle_text(event: MessageEvent):
    text = event.message.text.strip()

    if text.lower() in ("help", "使い方", "？"):
        usage = (
            "使い方：\n"
            "・『丸亀 8』のように “場名 レース番号” （日付省略可→今日）\n"
            "・『丸亀 8 20250811』のように日付(YYYYMMDD)付きでもOK\n"
            "※データは“ボートレース日和”優先（直前→MyDataの順に取得）"
        )
        reply(event, usage)
        return

    m = re.match(r"^\s*([^\s\d]+)\s+(\d{1,2})(?:\s+(\d{8}))?\s*$", text)
    if not m:
        reply(event, "入力例：『丸亀 8』 / 『丸亀 8 20250811』 / 『help』")
        return

    place_name = m.group(1)
    race_no = int(m.group(2))
    date_yyyymmdd = m.group(3) or datetime.now().strftime("%Y%m%d")

    if place_name not in PLACE_MAP:
        reply(event, f"場名が分かりません：{place_name}")
        return

    place_no = PLACE_MAP[place_name]
    header = f"📍 {place_name} {race_no}R ({format_date(date_yyyymmdd)})\n" + "─" * 22

    url_jikzen = build_biyori_url(place_no, race_no, date_yyyymmdd, slider=4)   # 直前
    url_mydata = build_biyori_url(place_no, race_no, date_yyyymmdd, slider=9)   # MyData

    try:
        # 直前 → ダメなら MyData に即フォールバック
        rows = None
        tried = []

        try:
            rows = fetch_biyori_table(url_jikzen)
            tried.append(url_jikzen)
        except TableNotFound:
            logger.warning("yosou-bot:[biyori] fetch failed (直前): %s", url_jikzen)
            tried.append(url_jikzen)

        if rows is None:
            try:
                rows = fetch_biyori_table(url_mydata)
                tried.append(url_mydata)
            except TableNotFound:
                logger.warning("yosou-bot:[biyori] fetch failed (MyData): %s", url_mydata)
                tried.append(url_mydata)

        if rows is None:
            reply(event, f"{header}\n直前情報の取得に失敗しました。\n試行URL:\n- {url_jikzen}\n- {url_mydata}")
            return

        metrics = pick_metrics(rows)
        analysis = build_analysis(metrics)
        bets = build_bets(analysis)

        msg = (
            f"{header}\n"
            f"🧭 展開予想：{analysis['scenario']}\n"
            f"🧩 根拠：{analysis['reason']}\n"
            "─" * 22 + "\n\n"
            f"🎯 本線：{', '.join(bets['main'])}\n"
            f"🛡️ 抑え：{', '.join(bets['cover'])}\n"
            f"💥 狙い：{', '.join(bets['attack'])}\n"
            f"\n(src: 日和 / {tried[-1]})"
        )
        reply(event, msg)

    except Exception as e:
        logger.exception("unhandled")
        reply(event, f"{header}\nエラー：{e}")

# ====== 共通関数 ======
def format_date(yyyymmdd: str) -> str:
    try:
        return datetime.strptime(yyyymmdd, "%Y%m%d").strftime("%Y/%m/%d")
    except Exception:
        return yyyymmdd

def build_biyori_url(place_no: int, race_no: int, yyyymmdd: str, slider: int = 4) -> str:
    return (
        "https://kyoteibiyori.com/race_shusso.php"
        f"?place_no={place_no}&race_no={race_no}&hiduke={yyyymmdd}&slider={slider}"
    )

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile Safari/604.1"
)

class TableNotFound(Exception):
    def __init__(self, url: str):
        super().__init__("table-not-found")
        self.url = url

def fetch_biyori_table(url: str):
    """日和のページから、直前/MyDataのどちらでも使える“表”を抽出して返す"""
    headers = {
        "User-Agent": UA,
        "Accept-Language": "ja,en;q=0.8",
        "Referer": "https://kyoteibiyori.com/",
        "Cache-Control": "no-cache",
    }
    r = requests.get(url, headers=headers, timeout=12)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    # 候補テーブルを広めに取得
    tables = soup.find_all("table")
    if not tables:
        raise TableNotFound(url)

    KEYWORDS = [
        "選手情報", "直前情報", "MyData", "枠別情報",
        "展示", "周回", "周り足", "直線", "ST", "平均ST"
    ]

    def looks_like(tbl):
        text = tbl.get_text(" ", strip=True)
        # キーワードのどれかを含む & 列が多い（6艇以上を期待）
        has_key = any(k in text for k in KEYWORDS)
        many_cols = max([len(tr.find_all(["th", "td"])) for tr in tbl.find_all("tr")] or [0]) >= 7
        return has_key and many_cols

    # もっとも“ありえそう”なテーブルを優先
    candidates = [t for t in tables if looks_like(t)]
    target = candidates[0] if candidates else None
    if target is None:
        raise TableNotFound(url)

    rows = []
    for tr in target.find_all("tr"):
        cols = [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
        if cols:
            rows.append(cols)
    return rows

def pick_metrics(rows):
    metrics = {}
    labels = {
        "展示": ["展示", "展示タイム", "展示ﾀｲﾑ"],
        "周回": ["周回"],
        "周り足": ["周り足", "ﾏﾜﾘ足", "回り足"],
        "直線": ["直線"],
        "ST": ["ST", "平均ST", "平均ＳＴ"],
    }
    for row in rows:
        label = row[0] if row else ""
        for key, alts in labels.items():
            if any(a in label for a in alts):
                values = row[1:7]
                values = [parse_float_safe(v) for v in values]
                while len(values) < 6:
                    values.append(None)
                metrics[key] = values[:6]
                break
    return metrics

def parse_float_safe(s):
    try:
        s = str(s).replace("F", ".").replace("L", ".")
        m = re.findall(r"-?\d+(?:\.\d+)?", s)
        return float(m[0]) if m else None
    except Exception:
        return None

def build_analysis(metrics):
    weights = {"展示": 0.35, "周回": 0.30, "直線": 0.25, "ST": 0.10}

    def rank_for(label, reverse=False):
        vals = metrics.get(label)
        if not vals:
            return [None]*6
        pairs = []
        for i, v in enumerate(vals):
            if v is None:
                pairs.append((9999 if not reverse else -9999, i))
            else:
                pairs.append((v, i))
        pairs_sorted = sorted(pairs, key=lambda x: x[0], reverse=reverse)
        ranks = [0]*6
        for r, (_, idx) in enumerate(pairs_sorted, start=1):
            ranks[idx] = r
        return ranks

    rk_ex = rank_for("展示", False)
    rk_lap = rank_for("周回", False)
    rk_lin = rank_for("直線", True)
    rk_st = rank_for("ST", False)

    score = [0]*6
    for i in range(6):
        for label, rk in [("展示", rk_ex), ("周回", rk_lap), ("直線", rk_lin), ("ST", rk_st)]:
            if rk[i]:
                score[i] += (7 - rk[i]) * weights[label]

    order = sorted(range(6), key=lambda i: score[i], reverse=True)
    axis = order[0] + 1
    scenario = "①先制の逃げ本線" if axis == 1 else f"{axis}コース軸の攻め"
    reason = f"展示/周回/直線/ST の総合評価で {axis}号艇が最上位"
    return {"axis": axis, "order": [i+1 for i in order], "scenario": scenario, "reason": reason}

def build_bets(analysis):
    axis = analysis["axis"]
    order = [x for x in analysis["order"] if x != axis]
    top3 = order[:3]
    top4 = order[:4]

    def tri(a, b, c): return f"{a}-{b}-{c}"

    main = []
    if len(top3) >= 2:
        for i, b in enumerate(top3):
            for j, c in enumerate(top3):
                if i == j: continue
                main.append(tri(axis, b, c))
    elif len(top3) == 1:
        main.append(tri(axis, top3[0], order[1] if len(order) > 1 else (1 if axis != 1 else 2)))
    main = dedup(main)[:MAX_MAIN]

    cover = []
    if len(top3) >= 2:
        for i, b in enumerate(top3):
            for j, c in enumerate(top3):
                if i == j: continue
                cover.append(tri(b, axis, c))
    cover = [x for x in dedup(cover) if x not in main][:MAX_COVER]

    attack = []
    if len(top4) >= 4:
        attack += [tri(axis, top4[3], top3[0]), tri(axis, top4[3], top3[1])]
    if len(top3) >= 3:
        attack += [tri(top3[0], top3[2], axis), tri(top3[1], top3[2], axis)]
    attack = [x for x in dedup(attack) if x not in main + cover][:MAX_ATTACK]

    return {"main": main, "cover": cover, "attack": attack}

def dedup(lst):
    out = []
    for x in lst:
        if x not in out:
            out.append(x)
    return out

def reply(event, text):
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))
