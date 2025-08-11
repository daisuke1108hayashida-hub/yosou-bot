# app.py
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# ====== Flask & LINE SDK 初期化 ======
app = Flask(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("環境変数 LINE_CHANNEL_SECRET と LINE_CHANNEL_ACCESS_TOKEN を設定してください。")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ====== 便利関数 ======
def reply_text(token: str, text: str) -> None:
    line_bot_api.reply_message(token, TextSendMessage(text=text))

JST = ZoneInfo("Asia/Tokyo")
USAGE = (
    "使い方：\n"
    "・『丸亀 8 20250808』のように送信（場名 レース番号 日付）\n"
    "・日付は省略可（例：『丸亀 8』は今日の日付）\n"
    "・ヘルプ：『help』または『使い方』"
)

# ====== 予想ロジック（まずはダミー） ======
def predict(place: str, race_no: int, ymd: str) -> str:
    """
    本線/抑え/狙い を返すダミー。
    あとでここを実データ（成績/直前情報）で置き換える。
    """
    # ここではサンプル固定返答
    header = f"{place} {race_no}R（{ymd}）予想\n"
    hon = "本線：1-2-全 / 1-全-2\n"
    osa = "抑え：2-1-全\n"
    ner = "狙い：4-1-2, 1-4-2\n"
    tenkai = "展開：①スタート先手→2差し本線。4カド気配なら一撃注意。"

    return header + "\n".join([hon, osa, ner, tenkai])

# ====== ルーティング ======
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

# ====== メッセージ受信 ======
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    raw = (event.message.text or "").strip()

    # ヘルプ
    if raw.lower() in ("help", "ヘルプ", "使い方"):
        reply_text(event.reply_token, USAGE)
        return

    # 形式: 「場名 レース番号 [YYYYMMDD]」
    # 例: 「丸亀 8 20250808」/「丸亀 8」
    m = re.match(r"^(\S+?)\s*(\d{1,2})(?:\s+(\d{8}))?$", raw)
    if not m:
        reply_text(event.reply_token, "入力形式が違います。\n" + USAGE)
        return

    place = m.group(1)
    race_no = int(m.group(2))
    ymd = m.group(3) or datetime.now(JST).strftime("%Y%m%d")

    try:
        result = predict(place, race_no, ymd)
    except Exception as e:
        result = f"予想中にエラーが発生しました：{e}"

    reply_text(event.reply_token, result)

# ====== ローカル実行用（Renderでは不要） ======
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
