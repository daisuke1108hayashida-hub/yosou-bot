import os
import re
import json
import math
import logging
from datetime import datetime
from typing import Dict, List, Optional

import httpx
from bs4 import BeautifulSoup
from flask import Flask, request, abort, jsonify

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# -----------------------------
# 基本設定
# -----------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("yosou-bot")

LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    logger.warning("LINE 環境変数が未設定です。LINE 連携は動かないかもしれません。")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

app = Flask(__name__)

# 競艇場 -> place_no
PLACE_NO = {
    "桐生": 1, "戸田": 2, "江戸川": 3, "平和島": 4, "多摩川": 5, "浜名湖": 6,
    "蒲郡": 7, "常滑": 8, "津": 9, "三国": 10, "琵琶湖": 11, "住之江": 12,
    "尼崎": 13, "鳴門": 14, "丸亀": 15, "児島": 16, "宮島": 17, "徳山": 18,
    "下関": 19, "若松": 20, "芦屋": 21, "福岡": 22, "唐津": 23, "大村": 24,
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# -----------------------------
# HTML 取得＆パース（日和）
# -----------------------------
BIYORI_URL = "https://kyoteibiyori.com/race_shusso.php"

def fetch_biyori(place_no: int, race_no: int, hiduke: str, slider: int = 4) -> str:
    """ページHTMLを取得"""
    params = {"place_no": place_no, "race_no": race_no, "hiduke": hiduke, "slider": slider}
    headers = {"User-Agent": USER_AGENT, "Referer": "https://kyoteibiyori.com/"}
    timeout = httpx.Timeout(20.0, connect=10.0)
    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
        r = client.get(BIYORI_URL, params=params)
        r.raise_for_status()
        return r.text

def parse_biyori_metrics(html: str) -> Dict[int, Dict[str, Optional[float]]]:
    """
    直前情報のテーブルから 指数を抜く。
    返り値: {lane: {"展示":sec, "周回":sec, "周り足":pt, "直線":pt, "ST":sec}}
    """
    soup = BeautifulSoup(html, "lxml")

    # テーブル総当たりで、行頭が「展示/周回/周り足/直線/ST」のブロックを探す
    wanted = ["展示", "周回", "周り足", "直線", "ST"]
    metrics: Dict[str, List[Optional[float]]] = {k: [None]*6 for k in wanted}

    tables = soup.find_all("table")
    if not tables:
        raise ValueError("table-not-found")

    found_any = False
    for tbl in tables:
        rows = tbl.find_all("tr")
        for tr in rows:
            cells = tr.find_all(["th", "td"])
            if not cells:
                continue
            head = cells[0].get_text(strip=True)
            if head not in wanted:
                continue

            # 右6列が 1～6号艇
            vals = []
            for td in cells[1:7]:
                t = td.get_text(strip=True)
                v = _to_float_safe(t)
                vals.append(v)

            # 足りない列は詰める
            while len(vals) < 6:
                vals.append(None)

            metrics[head] = vals[:6]
            found_any = True

    if not found_any:
        raise ValueError("table-not-found")

    # lane dict に寄せ替え
    lane_metrics: Dict[int, Dict[str, Optional[float]]] = {}
    for lane in range(1, 7):
        lane_metrics[lane] = {k: metrics[k][lane-1] for k in wanted}

    return lane_metrics

def _to_float_safe(s: str) -> Optional[float]:
    """ '6.76', 'F.05', 'F05', '-' などを float に寄せる """
    if not s or s == "-" or s is None:
        return None
    s = s.replace("F.", ".").replace("F", "")
    try:
        return float(s)
    except Exception:
        # 直線/周り足 で 7.88 などのフォーマットはそのまま
        # 万一全角が混じる場合も置換
        try:
            return float(s.replace("．", "."))
        except Exception:
            return None

# -----------------------------
# 予想ロジック（簡易）
# -----------------------------
def score_lanes(lane_data: Dict[int, Dict[str, Optional[float]]]) -> Dict[int, float]:
    """
    展示/周回(小さいほど良)・周り足/直線(大きいほど良)・ST(小さいほど良)を総合スコア化
    ＋内枠バイアス
    """
    lanes = list(lane_data.keys())

    def rank(values: List[Optional[float]], higher_is_better: bool) -> Dict[int, float]:
        arr = []
        for i, v in enumerate(values, start=1):
            if v is None or math.isnan(v):
                continue
            arr.append((i, v))
        if not arr:
            return {i: 0.0 for i in lanes}

        # ソート方向
        arr.sort(key=lambda x: x[1], reverse=higher_is_better)
        # スコア 6,5,4... を割り当て
        base = {i: 0.0 for i in lanes}
        score = 6.0
        for i, _v in arr:
            base[i] = score
            score -= 1.0
        return base

    # 各指標の順位スコア
    r_tenji   = rank([lane_data[i]["展示"]   for i in lanes], higher_is_better=False)
    r_shukai  = rank([lane_data[i]["周回"]   for i in lanes], higher_is_better=False)
    r_mawari  = rank([lane_data[i]["周り足"] for i in lanes], higher_is_better=True)
    r_chokus  = rank([lane_data[i]["直線"]   for i in lanes], higher_is_better=True)
    r_st      = rank([lane_data[i]["ST"]     for i in lanes], higher_is_better=False)

    # 内枠バイアス（超控えめ）
    lane_bias = {1: 1.4, 2: 0.8, 3: 0.4, 4: 0.2, 5: -0.2, 6: -0.6}

    total = {}
    for i in lanes:
        total[i] = (
            0.30 * r_tenji[i] +
            0.15 * r_shukai[i] +
            0.25 * r_mawari[i] +
            0.20 * r_chokus[i] +
            0.10 * r_st[i] +
            lane_bias.get(i, 0.0)
        )
    return total

def make_picks(scores: Dict[int, float]) -> Dict[str, List[str]]:
    """
    スコアから買い目を作る。
    - 本線: 6点
    - 抑え: 6点
    - 狙い: 6点（外枠/捲り目を少し混ぜる）
    """
    order = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    heads = [i for i, _ in order]  # スコア順 例: [1,2,3,4,5,6]

    top1, top2, top3, top4, top5, top6 = heads

    def tri(a, b, c): return f"{a}-{b}-{c}"

    main = [
        tri(top1, top2, top3),
        tri(top1, top3, top2),
        tri(top1, top2, top4),
        tri(top1, top3, top4),
        tri(top2, top1, top3),
        tri(top1, top4, top2),
    ]

    hold = [
        tri(top2, top3, top1),
        tri(top3, top1, top2),
        tri(top1, top5, top3),
        tri(top1, top2, top5),
        tri(top2, top4, top1),
        tri(top3, top2, top4),
    ]

    aim = [
        tri(top4, top1, top2),
        tri(top5, top1, top2),
        tri(top2, top5, top1),
        tri(top3, top5, top1),
        tri(top4, top2, top1),
        tri(top6, top1, top2),
    ]

    # 重複削除＆上位から
    def uniq(xs):
        seen, out = set(), []
        for x in xs:
            if x not in seen:
                seen.add(x); out.append(x)
        return out[:12]
    return {"main": uniq(main), "hold": uniq(hold), "sniper": uniq(aim)}

def format_pick_lines(picks: Dict[str, List[str]]) -> List[str]:
    def make(label, icon, key):
        arr = picks.get(key) or []
        return f"{icon} {label}（{len(arr)}点）: {', '.join(arr) if arr else 'なし'}"
    return [
        make("本線", "🎯", "main"),
        make("抑え", "🛡️", "hold"),
        make("狙い", "💥", "sniper"),
    ]

def build_scenario(scores: Dict[int, float], lane_data: Dict[int, Dict[str, Optional[float]]]) -> str:
    """
    展開予想テキスト（短文）
    """
    order = [i for i, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)]
    head = order[0]
    tail  = order[-1]

    # 直線・周り足が良い艇
    def top_of(key, higher=True):
        arr = [(i, lane_data[i][key]) for i in range(1, 7) if lane_data[i][key] is not None]
        if not arr:
            return None
        arr.sort(key=lambda x: x[1], reverse=higher)
        return arr[0][0]

    fast_st = top_of("ST", higher=False)
    good_str = top_of("直線", True)
    good_turn = top_of("周り足", True)

    msgs = []
    msgs.append(f"①{head}頭が本線。")
    if fast_st and fast_st == head:
        msgs.append("STも速く先制濃厚。")
    elif fast_st:
        msgs.append(f"STは{fast_st}が速く差し/捲りの警戒。")

    if good_turn:
        msgs.append(f"周り足は{good_turn}が良く内差し有力。")
    if good_str and good_str != good_turn:
        msgs.append(f"直線は{good_str}が伸び目。")

    msgs.append(f"穴は外の{tail}連動。")
    return " ".join(msgs)

# -----------------------------
# LINE Webhook
# -----------------------------
@app.route("/callback", methods=["POST"])
def callback():
    if not handler:
        return "LINE未設定", 200
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    text = (event.message.text or "").strip()
    if text.lower() in ("help", "ヘルプ"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(_usage()))
        return

    m = re.match(r"(\S+)\s+(\d{1,2})\s+(\d{8})", text)
    if not m:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(_usage()))
        return

    place_name, race_no_s, hiduke = m.group(1), m.group(2), m.group(3)
    place_no = PLACE_NO.get(place_name)
    if not place_no:
        line_bot_api.reply_message(event.reply_token, TextSendMessage("場名が分かりません。例) 丸亀 8 20250811"))
        return

    race_no = int(race_no_s)
    # まず slider=4 → ダメなら 9
    for slider in (4, 9):
        try:
            html = fetch_biyori(place_no, race_no, hiduke, slider=slider)
            lanes = parse_biyori_metrics(html)
            scores = score_lanes(lanes)
            picks = make_picks(scores)

            title = f"📍 {place_name} {race_no}R ({datetime.strptime(hiduke, '%Y%m%d').strftime('%Y/%m/%d')})"
            scenario = build_scenario(scores, lanes)
            lines = [
                title,
                "――――――――――――――",
                f"🔎 展開予想：{scenario}",
                "――――――――――――――",
            ]
            lines.extend(format_pick_lines(picks))
            lines.append(f"(src: 日和 / {BIYORI_URL}?place_no={place_no}&race_no={race_no}&hiduke={hiduke}&slider={slider})")

            line_bot_api.reply_message(event.reply_token, TextSendMessage("\n".join(lines)))
            return
        except Exception as e:
            logger.warning(f"[biyori] fetch/parse failed slider={slider} : {e}")

    # フォールバック（公式リンクだけ提示）
    url_official = (
        f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?"
        f"rno={race_no}&jcd={place_no:02d}&hd={hiduke}"
    )
    msg = (
        f"📍 {place_name} {race_no}R ({datetime.strptime(hiduke, '%Y%m%d').strftime('%Y/%m/%d')})\n"
        "直前情報の取得に失敗しました。少し待ってから再度お試しください。\n"
        f"(src: 公式 / {url_official})"
    )
    line_bot_api.reply_message(event.reply_token, TextSendMessage(msg))

def _usage() -> str:
    return (
        "入力例：『丸亀 8 20250811』 / 『help』\n"
        "・場名 半角スペース レース番号 日付(YYYYMMDD)\n"
        "・直前情報はボートレース日和を優先し、取得失敗時は公式リンクを案内します。"
    )

# -----------------------------
# Debug 用 (ブラウザで動作確認)
# -----------------------------
@app.get("/")
def root():
    return "yosou-bot alive", 200

@app.get("/_debug/biyori")
def debug_biyori():
    try:
        place_no = int(request.args.get("place_no", "15"))
        race_no  = int(request.args.get("race_no", "12"))
        hiduke   = request.args.get("hiduke", datetime.now().strftime("%Y%m%d"))
        slider   = int(request.args.get("slider", "4"))
        html = fetch_biyori(place_no, race_no, hiduke, slider=slider)
        lanes = parse_biyori_metrics(html)
        scores = score_lanes(lanes)
        picks  = make_picks(scores)
        return jsonify({"lanes": lanes, "scores": scores, "picks": picks})
    except Exception as e:
        return f"[biyori] {e}", 200
