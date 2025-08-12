# -*- coding: utf-8 -*-
import os
import re
import traceback
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

from predictors.teikoku_db_predictor import predict_from_teikoku, format_prediction_message

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_TOKEN  = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
if not CHANNEL_SECRET or not CHANNEL_TOKEN:
    raise RuntimeError("環境変数 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を設定してください。")

line_bot_api = LineBotApi(CHANNEL_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

app = Flask(__name__)

@app.get("/health")
def health(): return "ok", 200

@ app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

URL_NUMERIC = re.compile(r"https?://(?:www\.)?boatrace-db\.net/race/\d+/?$", re.I)
URL_WRONG   = re.compile(r"https?://(?:www\.)?boatrace-db\.net/race/\d{8}/\d{1,2}/\d{1,2}", re.I)

HELP_TEXT = (
    "艇国DB予想Bot 使い方👇\n"
    "・艇国データバンクのレース個別URL（/race/数字）を送るだけ\n"
    "例) https://boatrace-db.net/race/1234567\n"
    "※最小アクセスのためURL解決は行いません（規約配慮 / 安定運用）"
)

@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    user_text = (event.message.text or "").strip()

    if user_text in ("help", "ヘルプ", "使い方", "？"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(HELP_TEXT))
        return

    m = re.search(r"https?://(?:www\.)?boatrace-db\.net/[^\s]+", user_text, re.I)
    if not m:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(HELP_TEXT))
        return

    url = m.group(0)

    if URL_WRONG.match(url):
        msg = (
            "そのURL形式（/race/日付/場/レース）は艇国DBにはありません。\n"
            "ブラウザで該当レースを開き、/race/数字 のURLを送ってください。\n"
            "例) https://boatrace-db.net/race/1234567"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg))
        return

    if not URL_NUMERIC.match(url):
        msg = (
            "対応形式は /race/数字 のみです。\n"
            "例) https://boatrace-db.net/race/1234567"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg))
        return

    try:
        result = predict_from_teikoku(url)
        reply = format_prediction_message(result)
        if len(reply) <= 5000:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(reply))
        else:
            # 念のため分割
            chunk = reply[:4900]
            rest  = reply[4900:]
            msgs = [TextSendMessage(chunk)]
            if rest:
                msgs.append(TextSendMessage(rest[:4900]))
            line_bot_api.reply_message(event.reply_token, msgs)
    except Exception as e:
        traceback.print_exc()
        err = f"取得/予想中にエラーが発生しました。\n{type(e).__name__}: {e}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(err))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
