# -*- coding: utf-8 -*-
import os
import re
import traceback
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

from predictors.teikoku_db_predictor import predict_from_teikoku, format_prediction_message
from predictors.input_parser import parse_free_text
from predictors.teikoku_resolver import URL_NUMERIC, URL_ANY_DB, resolve_from_any_db_page

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_TOKEN  = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
if not CHANNEL_SECRET or not CHANNEL_TOKEN:
    raise RuntimeError("LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を設定してください。")

line_bot_api = LineBotApi(CHANNEL_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)
app = Flask(__name__)

@app.get("/health")
def health():
    return "ok", 200

@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

HELP = (
    "艇国DB 予想Bot 使い方\n"
    "① 最速：/race/数字 のURLを送る（例 https://boatrace-db.net/race/1234567）\n"
    "② テキストでもOK：『丸亀 11 20250812』のように 場所 R 日付(8桁)\n"
    "   ※日付省略で“今日”は未対応。8桁で送ってください\n"
    "③ 自動解決に失敗したら、/race/数字 のURLを送ってください"
)

@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    user_text = (event.message.text or "").strip()

    if user_text in ("help","ヘルプ","使い方","？"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(HELP))
        return

    # 1) すでに /race/数字 が含まれている？
    m_num = URL_NUMERIC.search(user_text)
    if m_num:
        url = m_num.group(0)
        _run_predict(event.reply_token, url)
        return

    # 2) 何らかの艇国DB URLを含む？ → そのページから /race/数字 を探す（1〜2ホップ）
    m_any = URL_ANY_DB.search(user_text)
    if m_any:
        any_url = m_any.group(0)
        race_hint = None
        m_r = re.search(r"\b(\d{1,2})\s*R\b", user_text, re.IGNORECASE)
        if m_r:
            race_hint = int(m_r.group(1))
        url = resolve_from_any_db_page(any_url, race_hint)
        if url:
            _run_predict(event.reply_token, url)
            return

    # 3) テキスト解析（丸亀 11 20250812）
    parsed = parse_free_text(user_text)
    if parsed:
        place_no, race_no, yyyymmdd = parsed
        msg = (
            f"受け取り：場={place_no} / R={race_no} / 日付={yyyymmdd}\n"
            "完全自動で /race/数字 を見つけるには、艇国DBの開催関連ページURLを一緒に送ってください。\n"
            "例）当日の開催一覧や結果ページなど（boatrace-db.net内）。\n"
            "※ 直接 /race/数字 のURLを送るのが最速です。"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg))
        return

    # 4) どれにも当てはまらない → ヘルプ
    line_bot_api.reply_message(event.reply_token, TextSendMessage(HELP))

def _run_predict(reply_token: str, url: str):
    try:
        result = predict_from_teikoku(url)
        reply  = format_prediction_message(result)
        if len(reply) <= 5000:
            line_bot_api.reply_message(reply_token, TextSendMessage(reply))
        else:
            # 念のため分割
            chunk = reply[:4900]
            rest  = reply[4900:]
            msgs = [TextSendMessage(chunk)]
            if rest:
                msgs.append(TextSendMessage(rest[:4900]))
            line_bot_api.reply_message(reply_token, msgs)
    except Exception as e:
        traceback.print_exc()
        err = f"取得/予想中にエラーが発生しました。\n{type(e).__name__}: {e}"
        line_bot_api.reply_message(reply_token, TextSendMessage(err))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
