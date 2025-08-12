# app.py
# -*- coding: utf-8 -*-
import os
import re
import logging
import datetime as dt
from typing import Dict, List, Tuple, Optional

from flask import Flask, request, abort, jsonify

# --- LINE v3 SDK ---
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, MessagingApi, ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# --- HTTP & HTML ---
import httpx
from bs4 import BeautifulSoup

# =========================
# 基本設定
# =========================
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

handler = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
line_api = MessagingApi(configuration)

# 競艇場コード（jcd）
JCD = {
    "桐生":"01","戸田":"02","江戸川":"03","平和島":"04","多摩川":"05","浜名湖":"06","蒲郡":"07",
    "常滑":"08","津":"09","三国":"10","びわこ":"11","住之江":"12","尼崎":"13","鳴門":"14","丸亀":"15",
    "児島":"16","宮島":"17","徳山":"18","下関":"19","若松":"20","芦屋":"21","福岡":"22","唐津":"23","大村":"24"
}

Triplet = Tuple[int, int, int]

# =========================
# ユーティリティ
# =========================
def today_str_jst() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y%m%d")

def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())

def parse_user_query(text: str) -> Tuple[Optional[str], Optional[int], str]:
    text = normalize(text)
    parts = text.replace("R"," ").replace("ｒ"," ").split(" ")

    place = None
    rno = None
    hd = today_str_jst()

    # 場名
    for name in JCD.keys():
        if parts and (parts[0].startswith(name) or name in parts[0]):
            place = name
            parts = parts[1:]
            break

    # レース番号
    for p in list(parts):
        if re.fullmatch(r"\d{1,2}", p):
            rno = int(p)
            parts.remove(p)
            break

    # 日付
    for p in list(parts):
        m = re.sub(r"[^\d]", "", p)
        if re.fullmatch(r"\d{8}", m):
            hd = m
            break

    return place, rno, hd

def build_owpc_url(jcd: str, rno: int, hd: str) -> str:
    return f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={jcd}&hd={hd}"

def fetch_owpc_meta(url: str) -> Dict:
    """
    公式 beforeinfo を取得。
    HTML/XML を自動判別してパース。取れなくても空でOK。
    """
    meta: Dict = {}
    try:
        with httpx.Client(
            timeout=httpx.Timeout(10.0),
            headers={"User-Agent":"Mozilla/5.0"},
            follow_redirects=True,
        ) as cli:
            r = cli.get(url)
            r.raise_for_status()

        text = r.text
        head = text[:200].lstrip().lower()
        # 先頭に XML 宣言があれば XML とみなす
        parser = "lxml-xml" if head.startswith("<?xml") else "html.parser"
        soup = BeautifulSoup(text, parser)

        # タイトル
        meta["title"] = (soup.title.text.strip() if soup.title else "")

        # 風速（ゆるく抽出）
        txt = soup.get_text(" ", strip=True)
        m = re.search(r"風速[^0-9]*([0-9]+)", txt)
        if m:
            try:
                meta["風速"] = int(m.group(1))
            except Exception:
                pass

    except Exception as e:
        app.logger.exception("owpc fetch error: %s", e)
        meta["fetch_error"] = True
    return meta

# =========================
# 買い目ロジック
# =========================
def _perm_head(head: int, seconds: List[int], thirds: List[int]) -> List[Triplet]:
    out: List[Triplet] = []
    for s in seconds:
        for t in thirds:
            if len({head, s, t}) == 3:
                out.append((head, s, t))
    return out

def unique_trios(items: List[Triplet]) -> List[Triplet]:
    s = {(a, b, c) for (a, b, c) in items if len({a, b, c}) == 3}
    return sorted(s, key=lambda x: (x[0], x[1], x[2]))

def build_picks(meta: Dict) -> Dict[str, List[Triplet]]:
    main: List[Triplet] = []
    sub:  List[Triplet] = []
    ana:  List[Triplet] = []

    main += _perm_head(1, [2], [3, 4, 5, 6])
    main += _perm_head(1, [3], [2, 4, 5, 6])

    one_four = _perm_head(1, [4], [2, 3, 5, 6])

    wind = meta.get("風速") or meta.get("wind") or 0
    try:
        wind = float(wind)
    except Exception:
        wind = 0.0

    if wind >= 4:
        main += one_four
    else:
        sub += one_four

    sub += _perm_head(2, [1], [3, 4, 5])
    sub += _perm_head(2, [3], [1, 4, 5])

    ana += _perm_head(4, [1], [2, 3, 5])
    ana += _perm_head(5, [1], [2, 3, 4])

    main = unique_trios(main)
    sub  = unique_trios(sub)
    ana  = unique_trios(ana)
    return {"main": main, "sub": sub, "ana": ana}

# =========================
# 表示整形
# =========================
def fmt_triplet(t: Triplet) -> str:
    return f"{t[0]}-{t[1]}-{t[2]}"

def format_bucket(title: str, items: List[Triplet]) -> str:
    lines = [f"{title}（{len(items)}点）"]
    lines += [fmt_triplet(t) for t in items]
    return "\n".join(lines)

def build_comment(meta: Dict, url: str, place: str, rno: int, hd: str) -> str:
    wind = meta.get("風速")
    wind_note = f" 風速{wind}m。" if isinstance(wind, (int, float)) else ""
    lines = [
        "――――――――――――――――",
        "――――",
        f"{place}{rno}R の展望。内有利の傾向。{wind_note}".rstrip(),
        "①の信頼はやや割引。②の差し、③のまくり差しに注意。",
        "④が踏み込めば『1-4』筋が浮上。保険で1-4-流し。",
        f"(参考: {url})",
        ""
    ]
    return "\n".join(lines)

def build_message(place: str, rno: int, hd: str, url: str, meta: Dict) -> str:
    picks = build_picks(meta)
    comment = build_comment(meta, url, place, rno, hd)
    text = "\n".join([
        comment,
        format_bucket("本線", picks["main"]),
        "",
        format_bucket("押え", picks["sub"]),
        "",
        format_bucket("穴目", picks["ana"])
    ])
    return text

# =========================
# Flask ルーティング
# =========================
@app.get("/")
def index():
    return "yosou-bot is running"

@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        # どんな例外でも 200 を返して LINE 側の再試行ループを避ける
        app.logger.exception("Exception on /callback: %s", e)
        return "OK"
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    try:
        text = (event.message.text or "").strip()

        if any(k in text.lower() for k in ["help", "ヘルプ", "使い方"]):
            howto = (
                "入力例：『常滑 6』 / 『丸亀 8 20250812』\n"
                "形式：〈場名〉〈R〉〈任意:日付YYYYMMDD〉\n"
                "※ 参考URLはBOATRACE公式の直前情報です。"
            )
            line_api.reply_message(
                ReplyMessageRequest(
                    replyToken=event.reply_token,
                    messages=[TextMessage(text=howto)]
                )
            )
            return

        place, rno, hd = parse_user_query(text)
        if not place or not rno:
            line_api.reply_message(
                ReplyMessageRequest(
                    replyToken=event.reply_token,
                    messages=[TextMessage(text="入力例：『常滑 6』 / 『丸亀 8 20250812』\n→ 〈場名〉〈R〉〈日付(任意)〉の順で送ってください。")]
                )
            )
            return

        url = build_owpc_url(JCD[place], rno, hd)
        meta = fetch_owpc_meta(url)

        if meta.get("fetch_error"):
            msg = (
                f"📍 {place} {rno}R （{hd[:4]}/{hd[4:6]}/{hd[6:]}）\n"
                f"直前情報の取得に失敗しました。少し待ってから再度お試しください。\n"
                f"(src: 公式 / {url})"
            )
        else:
            msg = build_message(place, rno, hd, url, meta)

        line_api.reply_message(
            ReplyMessageRequest(
                replyToken=event.reply_token,
                messages=[TextMessage(text=msg)]
            )
        )
    except Exception as e:
        app.logger.exception("on_message error: %s", e)
        # フォールバック返信
        line_api.reply_message(
            ReplyMessageRequest(
                replyToken=event.reply_token,
                messages=[TextMessage(text="処理中にエラーが出ました。少し待ってもう一度お試しください。")]
            )
        )

# デバッグ：URL叩きで確認
@app.get("/_debug/owpc")
def debug_owpc():
    jcd = request.args.get("jcd", type=str)
    rno = request.args.get("rno", type=int)
    hd  = request.args.get("hd",  type=str, default=today_str_jst())
    if not jcd or not rno:
        return jsonify({"error":"params: jcd, rno[, hd]"}), 400

    place = next((k for k,v in JCD.items() if v == jcd), f"JCD{jcd}")
    url = build_owpc_url(jcd, rno, hd)
    meta = fetch_owpc_meta(url)
    if meta.get("fetch_error"):
        return f"[owpc] fetch failed url={url}", 502
    return build_message(place, rno, hd, url, meta)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
