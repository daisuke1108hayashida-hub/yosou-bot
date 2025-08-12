# -*- coding: utf-8 -*-
import os
import re
import traceback
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# 予想モジュール（あなたが設置済みのファイル）
from predictors.teikoku_db_predictor import (
    predict_from_teikoku, format_prediction_message
)

# ========= 環境変数 =========
# LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN を .env / Render の環境変数に設定
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_TOKEN  = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
if not CHANNEL_SECRET or not CHANNEL_TOKEN:
    raise RuntimeError("環境変数 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を設定してください。")

line_bot_api = LineBotApi(CHANNEL_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ========= Flask =========
app = Flask(__name__)

# ヘルスチェック
@app.get("/health")
def health():
    return "ok", 200

# LINE Webhook 受け口
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ========= メッセージハンドラ =========
@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    user_text = (event.message.text or "").strip()

    # ヘルプ
    if user_text.lower() in ("help", "？", "使い方", "ヘルプ"):
        help_msg = (
            "艇国DB予想Bot 使い方👇\n"
            "・艇国データバンク（boatrace-db.net）のレース個別ページURLを送るだけ\n"
            "例) https://boatrace-db.net/race/xxxxxxxx\n\n"
            "※アクセスはサイト規約順守（3秒以上の間隔・同一URL多重アクセスなし）\n"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(help_msg))
        return

    # 艇国DBのURLだけを対象（完全に艇国データバンク運用）
    url_pat = re.compile(r"https?://(?:www\.)?boatrace-db\.net/[^\s]+", re.IGNORECASE)
    m = url_pat.search(user_text)

    if not m:
        msg = (
            "艇国データバンクのレースURLを送ってください。\n"
            "例) https://boatrace-db.net/race/xxxxxxxx"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg))
        return

    url = m.group(0)

    # 予想実行（predictor側で3秒インターバル順守）
    try:
        result = predict_from_teikoku(url)
        reply = format_prediction_message(result)
        # LINEの1メッセージ上限対策：長すぎる場合は分割
        if len(reply) <= 5000:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(reply))
        else:
            # 適当に分割（5,000字目安）
            chunks = []
            buf = []
            count = 0
            for line in reply.split("\n"):
                if count + len(line) + 1 > 4900:
                    chunks.append("\n".join(buf))
                    buf, count = [], 0
                buf.append(line)
                count += len(line) + 1
            if buf:
                chunks.append("\n".join(buf))
            msgs = [TextSendMessage(t) for t in chunks[:5]]  # 念のため5通上限程度
            line_bot_api.reply_message(event.reply_token, msgs)
    except Exception as e:
        traceback.print_exc()
        err = f"取得/予想中にエラーが発生しました。\n{type(e).__name__}: {e}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(err))

# ローカル実行用
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
