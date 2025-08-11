import os
import re
import json
import logging
from datetime import datetime, timedelta

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
    raise RuntimeError("環境変数 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN が未設定です。")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

app = Flask(__name__)

# ====== 場コード（日和の place_no） ======
PLACE_MAP = {
    "桐生": 1, "戸田": 2, "江戸川": 3, "平和島": 4, "多摩川": 5, "浜名湖": 6,
    "蒲郡": 7, "常滑": 8, "津": 9, "三国": 10, "びわこ": 11, "住之江": 12,
    "尼崎": 13, "鳴門": 14, "丸亀": 15, "児島": 16, "宮島": 17, "徳山": 18,
    "下関": 19, "若松": 20, "芦屋": 21, "福岡": 22, "唐津": 23, "大村": 24,
}

# ====== ルーティング（ヘルスチェック） ======
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
            "・『丸亀 8』のように「場名 レース番号」（日付省略可→今日）\n"
            "・『丸亀 8 20250811』のように日付(YYYYMMDD)付きでもOK\n"
            "※データは“ボートレース日和”を優先して取得します。"
        )
        reply(event, usage)
        return

    # 解析：『場名 レース番号 [日付8桁]』
    m = re.match(r"^\s*([^\s\d]+)\s+(\d{1,2})(?:\s+(\d{8}))?\s*$", text)
    if not m:
        reply(event, "入力例：『丸亀 8』 / 『丸亀 8 20250811』 / 『help』")
        return

    place_name = m.group(1)
    race_no = int(m.group(2))
    date_yyyymmdd = m.group(3) or datetime.now().strftime("%Y%m%d")

    if place_name not in PLACE_MAP:
        reply(event, f"場名が分かりません：{place_name}\n対応例：丸亀, 桐生, 唐津 など")
        return

    place_no = PLACE_MAP[place_name]

    header = f"📍 {place_name} {race_no}R ({format_date(date_yyyymmdd)})\n" + "─" * 22
    try:
        # 1) 日和（slider=4 直前情報）を優先
        biyori_url = build_biyori_url(place_no, race_no, date_yyyymmdd, slider=4)
        rows = fetch_biyori_table(biyori_url)

        # 直前情報の時短：主要指標だけ抜粋
        metrics = pick_metrics(rows)  # {'展示', '周回', '周り足', '直線', 'ST'} などが入れば使う

        # 2) 足りなければ MyData（slider=9）も併用して拡充
        if len(metrics.keys()) < 2:
            biyori_url2 = build_biyori_url(place_no, race_no, date_yyyymmdd, slider=9)
            rows2 = fetch_biyori_table(biyori_url2)
            metrics2 = pick_metrics(rows2)
            metrics.update({k: v for k, v in metrics2.items() if k not in metrics})

        # 予想生成（超簡易ロジック）
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
            f"\n(src: 日和 / {biyori_url})"
        )
        reply(event, msg)

    except TableNotFound as e:
        # 日和で取れなかった時は、理由とURLだけ返す
        logger.warning("[biyori] fetch failed: %s", e.url)
        fallback = (
            f"{header}\n直前情報の取得に失敗しました。少し待ってから再度お試しください。\n"
            f"(src: 日和 / {e.url})"
        )
        reply(event, fallback)

    except Exception as e:
        logger.exception("unhandled")
        reply(event, f"{header}\nエラーが発生しました：{e}")

# ====== ここからロジック ======
def format_date(yyyymmdd: str) -> str:
    try:
        dt = datetime.strptime(yyyymmdd, "%Y%m%d")
        return dt.strftime("%Y/%m/%d")
    except Exception:
        return yyyymmdd

def build_biyori_url(place_no: int, race_no: int, yyyymmdd: str, slider: int = 4) -> str:
    return (
        "https://kyoteibiyori.com/race_shusso.php"
        f"?place_no={place_no}&race_no={race_no}&hiduke={yyyymmdd}&slider={slider}"
    )

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

class TableNotFound(Exception):
    def __init__(self, url: str):
        super().__init__("table-not-found")
        self.url = url

def fetch_biyori_table(url: str):
    """日和のレース出走ページから、表データを二次元配列にして返す。
       ヘッダ名や構造の揺れに耐えるよう、候補テーブルを総当たりで探索。"""
    headers = {"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"}
    r = requests.get(url, headers=headers, timeout=12)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    candidates = soup.find_all("table")
    if not candidates:
        raise TableNotFound(url)

    def looks_like_target(tbl):
        # 直前情報 or MyData らしい行ラベルが含まれるかで判定
        text = tbl.get_text(" ", strip=True)
        keys = ["展示", "周回", "周り足", "直線", "ST", "平均ST", "枠別情報"]
        return any(k in text for k in keys)

    for tbl in candidates:
        if not looks_like_target(tbl):
            continue
        rows = []
        for tr in tbl.find_all("tr"):
            cols = [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
            if cols and any(cols):
                rows.append(cols)
        # 6艇×複数指標が載ったテーブルが候補
        if rows and any("1号" in " ".join(r) for r in rows) or len(rows) >= 6:
            return rows

    raise TableNotFound(url)

def pick_metrics(rows):
    """テーブル行から必要指標を拾って {label: [6艇分]} に整形。
       取れた分だけ返す（無ければ空辞書）。"""
    metrics = {}
    labels = {
        "展示": ["展示", "展示タイム", "展示ﾀｲﾑ"],
        "周回": ["周回"],
        "周り足": ["周り足", "ﾏﾜﾘ足", "回り足"],
        "直線": ["直線"],
        "ST": ["ST", "平均ST", "平均ＳＴ"],
    }

    # 各行ラベルを見つけて6コース分を抽出
    for row in rows:
        label = row[0] if row else ""
        for key, alts in labels.items():
            if any(a in label for a in alts):
                # 数値化（6艇分が並ぶことを想定。足りなければ埋める）
                values = row[1:7]
                values = [parse_float_safe(v) for v in values]
                while len(values) < 6:
                    values.append(None)
                metrics[key] = values[:6]
                break

    return metrics

def parse_float_safe(s):
    try:
        s = s.replace("F", ".").replace("L", ".")  # まれに ST で F表記など混ざる対策
    except Exception:
        pass
    try:
        return float(re.findall(r"-?\d+(?:\.\d+)?", str(s))[0])
    except Exception:
        return None

def build_analysis(metrics):
    """超簡易：展示/周回/直線/ST をスコア化して上位を出す"""
    # 小さいほど良い系：展示, 周回, ST / 大きいほど良い：直線
    # それぞれ重み付け
    weights = {"展示": 0.35, "周回": 0.30, "直線": 0.25, "ST": 0.10}

    # 正規化用に順位化する（Noneはビリ扱い）
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
        # reverse=False: 昇順（小さい方が良い） / reverse=True: 降順（大きい方が良い）
        pairs_sorted = sorted(pairs, key=lambda x: x[0], reverse=reverse)
        ranks = [0]*6
        for r, (_, idx) in enumerate(pairs_sorted, start=1):
            ranks[idx] = r
        return ranks

    rank_ex = rank_for("展示", reverse=False)
    rank_lap = rank_for("周回", reverse=False)
    rank_lin = rank_for("直線", reverse=True)
    rank_st = rank_for("ST", reverse=False)

    score = [0]*6
    for i in range(6):
        for label, rk in [("展示", rank_ex), ("周回", rank_lap), ("直線", rank_lin), ("ST", rank_st)]:
            if rk[i]:
                score[i] += (7 - rk[i]) * weights[label]  # 1位=6点, 6位=1点 的なスコア

    top = sorted(range(6), key=lambda i: score[i], reverse=True)
    axis = top[0] + 1  # 軸（1〜6）

    # ざっくりシナリオ文言
    scenario = "①先制の逃げ本線" if axis == 1 else f"{axis}コース軸の攻め"
    reason = f"展示/周回/直線/ST の総合評価で {axis}号艇が最上位"

    return {"axis": axis, "order": [i+1 for i in top], "scenario": scenario, "reason": reason}

def build_bets(analysis):
    """軸＋相手上位から 3連単フォーマットの買い目を作る"""
    axis = analysis["axis"]
    order = [x for x in analysis["order"] if x != axis]
    # 相手上位3艇
    opp = order[:3] if len(order) >= 3 else order

    def tri(a, b, c):
        return f"{a}-{b}-{c}"

    main = []
    cover = []
    attack = []

    # 本線：軸-相手上位2-相手上位2（順序違い）
    if len(opp) >= 2:
        main.append(tri(axis, opp[0], opp[1]))
        main.append(tri(axis, opp[1], opp[0]))
    elif len(opp) == 1:
        main.append(tri(axis, opp[0], order[2] if len(order) > 2 else 1 if axis != 1 else 2))

    # 抑え：相手頭→軸→相手
    if len(opp) >= 2:
        cover.append(tri(opp[0], axis, opp[1]))
        cover.append(tri(opp[1], axis, opp[0]))

    # 狙い：3番手絡み or まくり差し想定
    if len(opp) >= 3:
        attack.append(tri(axis, opp[2], opp[0]))
        attack.append(tri(opp[0], opp[1], axis))

    # 重複除去
    main = dedup(main)
    cover = dedup([x for x in cover if x not in main])
    attack = dedup([x for x in attack if x not in main + cover])

    return {"main": main, "cover": cover, "attack": attack}

def dedup(lst):
    out = []
    for x in lst:
        if x not in out:
            out.append(x)
    return out

def reply(event, text):
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))
