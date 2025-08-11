import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# 環境変数から読み込む（値は後で Render に設定する）
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
CHANNEL_TOKEN  = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")

app = Flask(__name__)
line_bot_api = LineBotApi(CHANNEL_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

@app.route("/health", methods=["GET"])
def health():
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

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event: MessageEvent):
    # まずはエコーで接続確認
    text = event.message.text
    line_bot_api.reply_message(
        event.reply_token, TextSendMessage(text=f"受け取り：{text}")
    )

if __name__ == "__main__":
    # ローカル実行用（Render では gunicorn を使う）
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
