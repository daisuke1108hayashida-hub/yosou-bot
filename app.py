import os
import re
import json
import datetime as dt

import requests
from bs4 import BeautifulSoup

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

from scraper import fetch_biyori, build_biyori_url, score_and_predict, format_beforeinfo

# ---------------------- 基本設定 ----------------------
app = Flask(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("環境変数 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を設定してください。")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 24場マップ（ボートレース日和のスラッグは推定。違う場合はここを直せばOK）
STADIUM_SLUG = {
    "桐生": "kiryu", "戸田": "toda", "江戸川": "edogawa", "平和島": "heiwajima", "多摩川": "tamagawa",
    "浜名湖": "hamanako", "浜名": "hamanako", "蒲郡": "gamagori", "常滑": "tokoname", "津": "tsu",
    "三国": "mikuni", "びわこ": "biwako", "琵琶湖": "biwako", "住之江": "suminoe", "尼崎": "amagasaki",
    "鳴門": "naruto", "丸亀": "marugame", "児島": "kojima", "宮島": "miyajima", "徳山": "tokuyama",
    "下関": "shimonoseki", "若松": "wakamatsu", "芦屋": "ashiya", "福岡": "fukuoka",
    "唐津": "karatsu", "大村": "omura",
}

HELP_TEXT = (
    "使い方：\n"
    "・レース指定：『丸亀 8 20250808』のように送信（年月日は省略可。省略時は今日）\n"
    "・リンク指定：ボートレース日和の『直前情報』ページURLをそのまま貼り付けでもOK\n"
    "返す内容：本線／抑え／狙い と、展示・周回・周り足・直線・ST など直前情報の要約\n"
    "例）『浜名湖 12』 / 『https://kyoteibiyori.com/...』 / 『help』"
)

# ---------------------- ルーティング ----------------------
@app.route("/health")
def health():
    return "ok", 200

@app.route("/")
def index():
    return "ok", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ---------------------- テキスト処理 ----------------------
@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    text = (event.message.text or "").strip()

    # help / 使い方
    if text.lower() in ("help", "使い方", "ヘルプ"):
        reply(event, HELP_TEXT)
        return

    # 1) URL が入っている場合（ボートレース日和直前ページを想定）
    url = extract_url(text)
    if url:
        try:
            beforeinfo = fetch_biyori(url)
            if not beforeinfo:
                reply(event, "直前情報の取得に失敗しました。URLが直前ページか確認してください。")
                return
            # 直前情報のみからスコア → 予想
            ranks, picks = score_and_predict(beforeinfo)
            msg = build_reply_text(ranks, picks, beforeinfo, note_head="📝URLから取得しました")
            reply(event, msg)
        except Exception as e:
            reply(event, f"スクレイピングでエラー：{e}")
        return

    # 2) テキストから（場所 / レース番号 / 日付）
    parsed = parse_query(text)
    if not parsed:
        reply(event, "読み取れませんでした。例）『丸亀 8 20250808』 or 直前ページURL / 『help』")
        return

    place, rno, ymd = parsed
    slug = resolve_slug(place)
    if not slug:
        reply(event, f"場名『{place}』が分かりませんでした。『help』で一覧を確認して、短縮名は調整してください。")
        return

    # URL を組み立てて取得（URLパターンが違う場合は build_biyori_url() 内の一行を調整）
    url = build_biyori_url(slug, rno, ymd)
    try:
        beforeinfo = fetch_biyori(url)
        if not beforeinfo:
            reply(event, f"直前情報の取得に失敗しました。\nURLが合っているか確認してください。\n{url}")
            return
        ranks, picks = score_and_predict(beforeinfo)
        head = f"⛵ {place} {rno}R {ymd}（{url}）"
        msg = build_reply_text(ranks, picks, beforeinfo, note_head=head)
        reply(event, msg)
    except Exception as e:
        reply(event, f"取得エラー：{e}\nURL: {url}")

# ---------------------- 返信組み立て ----------------------
def build_reply_text(ranks, picks, beforeinfo, note_head=""):
    lines = []
    if note_head:
        lines.append(note_head)

    # 予想
    main = picks.get("main", [])
    cover = picks.get("cover", [])
    aim = picks.get("aim", [])

    def fseq(seq):  # [1,2,3] -> "1-2-3"
        return "-".join(str(x) for x in seq)

    lines.append("―― 予想（暫定）――")
    if main:
        lines.append("本線: " + " / ".join(fseq(s) for s in main))
    if cover:
        lines.append("抑え: " + " / ".join(fseq(s) for s in cover))
    if aim:
        lines.append("狙い: " + " / ".join(fseq(s) for s in aim))

    # ランキング
    order = [f"{i}号艇" for i in ranks]
    lines.append("評価順: " + " > ".join(order))

    # 直前情報の要約
    lines.append("―― 直前要約 ――")
    lines.extend(format_beforeinfo(beforeinfo))

    lines.append("\n※簡易モデルです。重みは調整可能。『help』で使い方")
    return "\n".join(lines)

def reply(event, text):
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))

# ---------------------- ユーティリティ ----------------------
def extract_url(s: str) -> str | None:
    m = re.search(r"https?://\S+", s)
    return m.group(0) if m else None

def resolve_slug(place_tok: str) -> str | None:
    # 完全一致優先 → 部分一致（先頭2文字など）
    if place_tok in STADIUM_SLUG:
        return STADIUM_SLUG[place_tok]
    for k, v in STADIUM_SLUG.items():
        if k.startswith(place_tok) or place_tok.startswith(k):
            return v
    return None

def parse_query(text: str):
    """
    パターン: <場所> [<レース番号>] [<YYYYMMDD>]
    例: '丸亀 8 20250808', '浜名湖 12', '住之江'
    """
    text = re.sub(r"\s+", " ", text.strip())
    m = re.match(r"^(?P<place>\S+)(?:\s+(?P<rno>\d{1,2}))?(?:\s+(?P<ymd>\d{8}))?$", text)
    if not m:
        return None
    place = m.group("place")
    rno = int(m.group("rno")) if m.group("rno") else 12
    if m.group("ymd"):
        ymd = m.group("ymd")
    else:
        ymd = dt.date.today().strftime("%Y%m%d")
    return place, rno, ymd

# ----------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
