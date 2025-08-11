import os
from datetime import datetime, timedelta, timezone
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# ==== 環境変数 ====
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("環境変数 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN が未設定です")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 場コード
PLACE_JCD = {
    "桐生":"01","戸田":"02","江戸川":"03","平和島":"04","多摩川":"05","浜名湖":"06",
    "蒲郡":"07","常滑":"08","津":"09","三国":"10","びわこ":"11","住之江":"12",
    "尼崎":"13","鳴門":"14","丸亀":"15","児島":"16","宮島":"17","徳山":"18",
    "下関":"19","若松":"20","芦屋":"21","福岡":"22","唐津":"23","大村":"24"
}

JST = timezone(timedelta(hours=9))

# ====== Health / Index ======
@app.route("/health")
def health():
    return "ok", 200

@app.route("/")
def index():
    return "ok", 200

# ====== LINE callback ======
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ====== メッセージハンドラ ======
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    text = (event.message.text or "").strip()

    # ヘルプ
    if text in ("help","ヘルプ","？","?"):
        usage = (
            "使い方：\n"
            "・出走表URL：『丸亀 8 20250808』のように送信\n"
            "　（日付省略可。例：『丸亀 8』は今日の出走表）\n"
        )
        reply(event.reply_token, usage)
        return

    # パース（場名 レース番号 日付任意）
    parts = text.split()
    if len(parts) >= 2:
        place_jp = parts[0]
        rno_str = parts[1]
        yyyymmdd = parts[2] if len(parts) >= 3 else datetime.now(JST).strftime("%Y%m%d")

        jcd = PLACE_JCD.get(place_jp)
        if jcd and rno_str.isdigit():
            rno = int(rno_str)
            # 公式 racecard ページ（まずはURLを返すだけ）
            url = f"https://www.boatrace.jp/owpc/pc/racedata/racecard?jcd={jcd}&hd={yyyymmdd}"
            msg = (
                f"🗺 出走表URL：\n{url}\n\n"
                f"※レース番号: {rno}R（ページ内で選択してください）"
            )
            reply(event.reply_token, msg)
            return

    # どれにも当てはまらないとき
    reply(event.reply_token, "形式は『丸亀 8 20250808』です（最後の年月日は省略可）。\nhelp で説明を表示します。")

def reply(token, message):
    line_bot_api.reply_message(token, TextSendMessage(text=message))

if __name__ == "__main__":
    # Render では Procfile/Gunicorn が使われるが、ローカル実行用に残す
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
