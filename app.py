import os
from datetime import datetime, timedelta, timezone
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, SourceGroup, SourceRoom

app = Flask(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("環境変数 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を設定してください")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

JST = timezone(timedelta(hours=9))

# ボート場コード（jcd）
JCD = {
    "桐生":1,"戸田":2,"江戸川":3,"平和島":4,"多摩川":5,"浜名湖":6,
    "蒲郡":7,"常滑":8,"津":9,"三国":10,"琵琶湖":11,"住之江":12,
    "尼崎":13,"鳴門":14,"丸亀":15,"児島":16,"宮島":17,"徳山":18,
    "下関":19,"若松":20,"芦屋":21,"福岡":22,"唐津":23,"大村":24
}

def build_racecard_url(place: str, race_no: int, yyyymmdd: str | None) -> str:
    jcd = JCD.get(place)
    if not jcd:
        raise ValueError("場名が認識できません")
    if not (1 <= race_no <= 12):
        raise ValueError("レース番号は1-12で指定してください")
    if not yyyymmdd:
        yyyymmdd = datetime.now(JST).strftime("%Y%m%d")
    return f"https://www.boatrace.jp/owpc/pc/racedata/racecard?jcd={jcd}&hd={yyyymmdd}"

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

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    text = event.message.text.strip()

    # help
    if text.lower() == "help":
        msg = (
            "使い方：\n"
            "・『丸亀 8 20250808』のように送信（⽇付省略可。例：『丸亀 8』は今日）\n"
            "→ 出走表URLを返します。"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    # 「場名 数字(レース番号) [日付(任意8桁)]」のパターンに対応
    import re
    m = re.match(r"^(\S+)\s+(\d{1,2})(?:\s+(\d{8}))?$", text)
    if m:
        place, race_no, ymd = m.group(1), int(m.group(2)), m.group(3)
        try:
            url = build_racecard_url(place, race_no, ymd)
            reply = f"出走表URL：{url}"
        except Exception as e:
            reply = f"エラー：{e}"
    else:
        reply = "コマンドが認識できません。『help』と送ると使い方を表示します。"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    # Render では Procfile で gunicorn を使うのでここは未使用
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
