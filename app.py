# -*- coding: utf-8 -*-
import os
import re
import json
import datetime as dt
from itertools import product, permutations

import requests
from bs4 import BeautifulSoup

from flask import Flask, request, Response, jsonify

# ===== LINE Bot =====
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# ----- 環境変数 -----
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

# ===== 競艇場 -> place_no（ボートレース日和と同じ番号） =====
PLACE_MAP = {
    "桐生": 1, "戸田": 2, "江戸川": 3, "平和島": 4, "多摩川": 5,
    "浜名湖": 6, "蒲郡": 7, "常滑": 8, "津": 9, "三国": 10,
    "びわこ": 11, "住之江": 12, "尼崎": 13, "鳴門": 14, "丸亀": 15,
    "児島": 16, "宮島": 17, "徳山": 18, "下関": 19, "若松": 20,
    "芦屋": 21, "福岡": 22, "唐津": 23, "大村": 24,
}

# ====== ヘルパ ======
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"

def today_jst():
    return dt.datetime.utcnow() + dt.timedelta(hours=9)

def parse_user_text(text: str):
    """
    ユーザー入力例:
      丸亀 8
      丸亀 8 20250811
      丸亀 8R
      help
    戻り: (place_no, race_no, yyyymmdd or None)
    """
    text = text.strip()
    if text.lower() == "help":
        return ("HELP", None, None)

    # 「場 所 レース 日付?」の形式をざっくり
    m = re.match(r"^\s*([^\s0-9]+)\s*([0-9]{1,2})[Rr]?\s*(\d{8})?\s*$", text)
    if not m:
        return (None, None, None)
    place_name = m.group(1)
    race_no = int(m.group(2))
    ymd = m.group(3)

    place_no = PLACE_MAP.get(place_name)
    if not place_no:
        return (None, None, None)

    if not ymd:
        ymd = today_jst().strftime("%Y%m%d")

    return (place_no, race_no, ymd)

# ====== 日和スクレイピング ======

def fetch_biyori(place_no: int, race_no: int, ymd: str, slider: int):
    """ボートレース日和の出走ページを取得して、主要テーブルをdictに整形"""
    url = (
        "https://kyoteibiyori.com/race_shusso.php"
        f"?place_no={place_no}&race_no={race_no}&hiduke={ymd}&slider={slider}"
    )
    res = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    res.raise_for_status()
    html = res.text
    soup = BeautifulSoup(html, "lxml")

    # 画面には複数テーブルがある。ヘッダ行に「選手情報」等が並ぶ大きいテーブルを拾う
    tables = soup.find_all("table")
    target = None
    for t in tables:
        ths = [th.get_text(strip=True) for th in t.find_all("th")]
        if ("選手情報" in "".join(ths)) or ("展示" in ths and "周回" in ths):
            target = t
            break

    if not target:
        # 見つからないときはエラー
        raise ValueError(f"[biyori] table not found url={url}")

    # 1～6号艇の列構造を拾いやすいように抽出
    # 行の先頭セル(項目名) + 6艇分の値、という横長構造を辞書化する
    data = {lane: {} for lane in range(1, 7)}
    rows = target.find_all("tr")
    for tr in rows:
        cells = tr.find_all(["th", "td"])
        if len(cells) < 7:
            continue
        label = cells[0].get_text(strip=True)
        # 2～7列目が1～6号艇
        for lane in range(1, 7):
            val = cells[lane].get_text(" ", strip=True)
            data[lane][label] = val

    return {
        "url": url,
        "table": data
    }

def pick_numbers(b4: dict, my: dict):
    """
    直前(=slider4)とMyData(=slider9)を合わせて簡易評価→買い目生成
    スコアの素朴ルール:
      ・展示/周回/周り足/直線 は数字が小さい(速い)ほど加点
      ・平均STは小さいほど加点
      ・ST順位(総合)は小さいほど加点
    """
    def to_float(x):
        try:
            return float(x)
        except:
            return None

    score = {}
    for lane in range(1, 7):
        s = 0.0
        bj = b4["table"].get(lane, {})
        md = my["table"].get(lane, {})

        # 直前側
        for key in ["展示", "周回", "周り足", "直線"]:
            v = to_float(bj.get(key))
            if v is not None:
                s += max(0, 10.0 - v)  # 速いほど＋

        # MyData 側
        st = to_float(md.get("平均ST(総合)")) or to_float(md.get("平均ST（総合）")) or to_float(md.get("平均ST"))
        if st is not None:
            s += max(0, 3.0 - st) * 5  # STは重みを高めに

        st_rank = to_float(md.get("ST順位(総合)")) or to_float(md.get("ST順位（総合）"))
        if st_rank is not None:
            s += max(0, 7 - st_rank) * 1.5

        score[lane] = round(s, 3)

    # 上位を並べる
    order = sorted(score.items(), key=lambda x: x[1], reverse=True)
    lanes_sorted = [k for k, v in order]

    # 1号艇を軸にするか、差し中心にするかを簡易判断
    axis = 1 if lanes_sorted[0] == 1 or lanes_sorted[1] == 1 else lanes_sorted[0]

    def trio(a, b, c):
        return f"{a}-{b}-{c}"

    # 本線（4点）
    main = []
    if axis == 1:
        cands = [x for x in lanes_sorted if x != 1][:4]
        for b in cands[:2]:
            for c in cands:
                if b == c: 
                    continue
                if len(main) >= 4: 
                    break
                main.append(trio(1, b, c))
    else:
        # まくり or 差し
        second = lanes_sorted[1]
        third = lanes_sorted[2] if len(lanes_sorted) > 2 else (1 if second != 1 else lanes_sorted[3])
        main = [trio(axis, second, third), trio(axis, 1, second), trio(1, axis, second), trio(axis, third, second)]

    # 抑え（3点）
    sub = []
    for a, b in permutations(lanes_sorted[:3], 2):
        if a == b: 
            continue
        for c in lanes_sorted[:4]:
            if c in (a, b):
                continue
            if trio(a, b, c) not in main and trio(a, b, c) not in sub:
                sub.append(trio(a, b, c))
            if len(sub) >= 3:
                break
        if len(sub) >= 3:
            break

    # 狙い（2点）
    aim = []
    tail = lanes_sorted[-2:]
    for c in tail:
        a, b = lanes_sorted[0], lanes_sorted[1]
        pick = trio(c, a, b)
        if pick not in main and pick not in sub and pick not in aim:
            aim.append(pick)
        if len(aim) >= 2:
            break

    return {
        "score": score,
        "order": lanes_sorted,
        "axis": axis,
        "main": main[:4],
        "sub": sub[:3],
        "aim": aim[:2],
    }

def make_preview_text(place_no, race_no, ymd, b4, my, picks):
    d = dt.datetime.strptime(ymd, "%Y%m%d").strftime("%Y/%m/%d")
    head = f"📍 {name_by_place(place_no)} {race_no}R ({d})\n" + "―"*24 + "\n\n"

    # 軽い展開コメント
    axis = picks["axis"]
    axis_note = "①イン逃げ本線" if axis == 1 else f"{axis}の攻め台"
    exp = f"🧭 展開予想：{axis_note}。直前×MyDataの合算評価で上位を素直に。\n"

    # 買い目
    main = "🎯 本線 ： " + ", ".join(picks["main"])
    sub  = "🛡 抑え ： " + ", ".join(picks["sub"])
    aim  = "💥 狙い ： " + ", ".join(picks["aim"])

    src = f"\n\n(直前:日和 slider=4 / MyData:日和 slider=9)\n{b4['url']}\n{my['url']}"
    return head + exp + "\n".join([main, sub, aim]) + src

def name_by_place(place_no: int) -> str:
    inv = {v: k for k, v in PLACE_MAP.items()}
    return inv.get(place_no, f"場No.{place_no}")

# ====== Flask ルート ======

@app.route("/", methods=["GET"])
def root():
    return "ok", 200

@app.route("/_debug/health", methods=["GET"])
def debug_health():
    return "ok", 200

@app.route("/_debug/biyori", methods=["GET"])
def debug_biyori():
    try:
        place_no = int(request.args.get("place_no"))
        race_no  = int(request.args.get("race_no"))
        hiduke   = request.args.get("hiduke")
        slider   = int(request.args.get("slider", 4))
        data = fetch_biyori(place_no, race_no, hiduke, slider)
        return jsonify({"ok": True, "url": data["url"], "table": data["table"]})
    except Exception as e:
        return Response(str(e), status=500, mimetype="text/plain")

@app.route("/_debug/biyori_html", methods=["GET"])
def debug_biyori_html():
    try:
        place_no = int(request.args.get("place_no"))
        race_no  = int(request.args.get("race_no"))
        hiduke   = request.args.get("hiduke")
        slider   = int(request.args.get("slider", 4))
        url = (
            "https://kyoteibiyori.com/race_shusso.php"
            f"?place_no={place_no}&race_no={race_no}&hiduke={hiduke}&slider={slider}"
        )
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        return Response(r.text, mimetype="text/html")
    except Exception as e:
        return Response(str(e), status=500, mimetype="text/plain")

# ====== LINE Webhook ======

@app.route("/callback", methods=["POST"])
def callback():
    if not handler:
        return "LINE handler not set", 500

    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        return "Invalid signature", 400
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event: MessageEvent):
    text = (event.message.text or "").strip()

    # help
    place_no, race_no, ymd = parse_user_text(text)
    if place_no == "HELP":
        reply = (
            "使い方：\n"
            "・『丸亀 8』／『丸亀 8 20250811』のように送信\n"
            "・直前＆MyDataを日和から取得 → 展開と買い目を返します。\n"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    if not place_no or not race_no or not ymd:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="入力例：『丸亀 8』 / 『丸亀 8 20250811』 / 『help』")
        )
        return

    # スクレイピング（直前=slider4, MyData=slider9）
    try:
        b4 = fetch_biyori(place_no, race_no, ymd, slider=4)
        my = fetch_biyori(place_no, race_no, ymd, slider=9)
    except Exception as e:
        msg = (
            "直前情報の取得に失敗しました。少し待ってから再度お試しください。\n"
            f"(src: 日和 / {str(e)})"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    # 予想生成
    try:
        picks = pick_numbers(b4, my)
        reply = make_preview_text(place_no, race_no, ymd, b4, my, picks)
    except Exception as e:
        reply = "予想の生成に失敗しました。別レースでお試しください。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))


# ====== エントリーポイント ======
if __name__ == "__main__":
    # 開発ローカル用
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
