import os
import re
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# ====== LINE 環境変数 ======
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("環境変数 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN が未設定です。")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ====== 共通 ======
JST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (compatible; YosouBot/1.0)"}

# 場コード（JCD）
JCD = {
    "桐生":"01","戸田":"02","江戸川":"03","平和島":"04","多摩川":"05","浜名湖":"06",
    "蒲郡":"07","常滑":"08","津":"09","三国":"10","びわこ":"11","住之江":"12",
    "尼崎":"13","鳴門":"14","丸亀":"15","児島":"16","宮島":"17","徳山":"18",
    "下関":"19","若松":"20","芦屋":"21","福岡":"22","唐津":"23","大村":"24",
}

HELP = (
    "使い方：\n"
    "・『丸亀 8 20250808』のように送信（半角スペース区切り）\n"
    "・日付省略可：『丸亀 8』→今日の日付で検索\n"
    "・対応場：桐生〜大村の24場\n"
)

def parse_user_text(text: str):
    t = text.strip().replace("　", " ")
    m = re.match(r"^([^\s]+)\s+(\d{1,2})(?:\s+(\d{8}))?$", t)
    if not m:
        return None
    place, rno, day = m.group(1), int(m.group(2)), m.group(3)
    if place not in JCD or not (1 <= rno <= 12):
        return None
    if day:
        try:
            datetime.strptime(day, "%Y%m%d")
        except ValueError:
            return None
    else:
        day = datetime.now(JST).strftime("%Y%m%d")
    return place, rno, day, JCD[place]

def http_status(url: str):
    try:
        r = requests.get(url, headers=UA, timeout=10)
        return r.status_code
    except Exception:
        return None

# ====== routes ======
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

# ====== LINE handler ======
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()

    if text.lower() in ["help", "?", "ヘルプ", "使い方"]:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(HELP))
        return

    parsed = parse_user_text(text)
    if not parsed:
        line_bot_api.reply_message(event.reply_token, TextSendMessage("形式が正しくありません。\n" + HELP))
        return

    place, rno, day, jcd = parsed

    racelist = f"https://www.boatrace.jp/owpc/pc/race/racelist?rno={rno}&jcd={jcd}&hd={day}"
    racecard = f"https://www.boatrace.jp/owpc/pc/racedata/racecard?jcd={jcd}&hd={day}"

    # racecard の方が比較的安定なので優先チェック
    s_card = http_status(racecard)
    s_list = http_status(racelist)

    if s_card == 200 or s_list == 200:
        out = [f"🧭 {place} {rno}R {day}"]
        if s_list == 200:
            out.append(f"🔗 racelist: {racelist}")
        if s_card == 200:
            out.append(f"🔗 racecard: {racecard}")
        msg = "\n".join(out)
    else:
        msg = (
            "❌ 出走表ページが見つかりませんでした（非開催 or サイト側仕様変更の可能性）\n"
            f"- racelist: {racelist}\n- racecard: {racecard}"
        )

    line_bot_api.reply_message(event.reply_token, TextSendMessage(msg))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
