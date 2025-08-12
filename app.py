# app.py
import os
import re
import logging
from datetime import datetime
from typing import Dict, List, Tuple

from flask import Flask, request, abort, jsonify

import httpx
from bs4 import BeautifulSoup

# ===== LINE SDK v3 =====
from linebot.v3 import WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)

# 自作のメッセージ整形ユーティリティ
from formatter import build_message

# --------------------------
# 基本設定
# --------------------------
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_TOKEN  = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")

if not CHANNEL_SECRET or not CHANNEL_TOKEN:
    raise RuntimeError("環境変数 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を設定してください。")

handler = WebhookHandler(CHANNEL_SECRET)
config  = Configuration(access_token=CHANNEL_TOKEN)

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
log = logging.getLogger("yosou-bot")

# 場コード（jcd） 公式サイト用
JCD = {
    "桐生":"01","戸田":"02","江戸川":"03","平和島":"04","多摩川":"05","浜名湖":"06","蒲郡":"07","常滑":"08","津":"09","三国":"10",
    "びわこ":"11","住之江":"12","尼崎":"13","鳴門":"14","丸亀":"15","児島":"16","宮島":"17","徳山":"18","下関":"19","若松":"20",
    "芦屋":"21","福岡":"22","唐津":"23","大村":"24"
}

# --------------------------
# ルート/ヘルスチェック
# --------------------------
@app.get("/")
def index():
    return "yosou-bot is alive ✨"

@app.get("/_health")
def health():
    return jsonify(ok=True)

# --------------------------
# 公式 beforeinfo 取得（フォールバック元）
# --------------------------
def fetch_beforeinfo(jcd: str, rno: int, yyyymmdd: str) -> Dict:
    """
    公式 beforeinfo を軽く取得。パースが難しい箇所はスキップしてもOK。
    返り値は meta(dict)。取れた分だけ入れる設計。
    """
    url = f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={jcd}&hd={yyyymmdd}"
    meta: Dict = {"参考": url}

    try:
        with httpx.Client(timeout=15) as client:
            res = client.get(url, headers={"User-Agent":"yosou-bot/1.0"})
            res.raise_for_status()
            soup = BeautifulSoup(res.text, "lxml")

        # タイトルから場名や日付を推定
        title = soup.select_one("title").get_text(strip=True) if soup.select_one("title") else ""
        for name, code in JCD.items():
            if name in title:
                meta["場名"] = name
                break

        meta["日付"] = f"{yyyymmdd[:4]}/{yyyymmdd[4:6]}/{yyyymmdd[6:]}"
        meta["レース"] = rno

        # （任意）展示気配・風のところがあれば軽く拾う
        w = soup.find(string=re.compile("風"))
        if w and isinstance(w, str):
            # ざっくり抽出（正規表現で m/s を拾う）
            m = re.search(r"(\d+(?:\.\d+)?)\s*m/s", w)
            if m:
                meta["風速"] = float(m.group(1))

        # スタート展示テーブルがあれば ST をざっくり
        players: Dict[int, Dict] = {}
        st_tbl = soup.select_one(".table1.is-w495") or soup.select_one(".table1")
        if st_tbl:
            # 行内に「1」「2」…が含まれていれば簡易に読む（ページ改修に弱いが落ちない実装）
            for lane in range(1, 7):
                players[lane] = players.get(lane, {})
        meta["選手"] = players

    except Exception as e:
        log.warning("fetch_beforeinfo failed: %s url=%s", e, url)

    return meta

@app.get("/_debug/beforeinfo")
def debug_beforeinfo():
    jcd = request.args.get("jcd", "15")
    rno = int(request.args.get("rno", "8"))
    hd  = request.args.get("hd", datetime.now().strftime("%Y%m%d"))
    meta = fetch_beforeinfo(jcd, rno, hd)
    return jsonify(meta)

# --------------------------
# 予想ロジック（簡易）
# --------------------------
Triple = Tuple[int, int, int]

def _perm_head(head: int, seconds: List[int], thirds: List[int]) -> List[Triple]:
    out: List[Triple] = []
    for s in seconds:
        for t in thirds:
            if t == s: 
                continue
            out.append((head, s, t))
    return out

def build_picks(meta: Dict) -> Dict[str, List[Triple]]:
    """
    とりあえず読みやすい圧縮表記になるよう配列を用意。
    - 本線: ①頭で2or3相手
    - 押え: ②頭の差し／相手①3,4,5
    - 穴目: カドや外の一撃（4 or 5）
    """
    main: List[Triple] = []
    sub:  List[Triple] = []
    ana:  List[Triple] = []

    # 本線：1-2-3456 / 1-3-2456
    main += _perm_head(1, [2], [3,4,5,6])
    main += _perm_head(1, [3], [2,4,5,6])

    # 押え：2-1-345 / 2-3-145
    sub  += _perm_head(2, [1], [3,4,5])
    sub  += _perm_head(2, [3], [1,4,5])

    # 穴目：4頭・5頭の筋
    ana  += _perm_head(4, [1], [2,3,5])
    ana  += _perm_head(5, [1], [2,3,4])

    return {"main": main, "sub": sub, "ana": ana}

# --------------------------
# コマンド解析
# --------------------------
HELP = (
    "使い方例：\n"
    "  丸亀 8 20250812\n"
    "  常滑 6 20250812\n"
    "（場名 半角スペース レース番号 日付YYYYMMDD）"
)

def parse_command(text: str):
    """
    戻り値: (場名, jcd, rno, yyyymmdd) or None
    """
    t = text.strip()
    parts = re.split(r"\s+", t)
    if len(parts) != 3:
        return None
    place, r_str, d_str = parts
    if place not in JCD:
        return None
    if not r_str.isdigit() or not d_str.isdigit() or len(d_str) != 8:
        return None
    return place, JCD[place], int(r_str), d_str

# --------------------------
# LINE Webhook
# --------------------------
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        log.exception("handle error: %s", e)
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    user_text = event.message.text.strip()
    parsed = parse_command(user_text)

    if not parsed:
        reply_text(event.reply_token, HELP)
        return

    place_name, jcd, rno, ymd = parsed

    # データ取得（公式フォールバック）
    meta = fetch_beforeinfo(jcd, rno, ymd)
    # 最低限の項目を補完
    meta.setdefault("場名", place_name)
    meta.setdefault("レース", rno)
    meta.setdefault("日付", f"{ymd[:4]}/{ymd[4:6]}/{ymd[6:]}")

    # 買い目生成 → 整形
    buckets = build_picks(meta)
    title   = f"{place_name} {rno}R（{meta['日付']}）"
    text    = build_message(title, meta, buckets)

    reply_text(event.reply_token, text)

def reply_text(reply_token: str, text: str):
    with ApiClient(config) as api_client:
        api = MessagingApi(api_client)
        api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text[:4900])]  # LINE上限に安全配慮
            )
        )

# --------------------------
# ローカル実行
# --------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
