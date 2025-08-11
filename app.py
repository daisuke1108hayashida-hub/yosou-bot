import os
import re
from datetime import datetime, timezone, timedelta

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# -------------------------------
# 基本セットアップ
# -------------------------------
app = Flask(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("環境変数 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN が未設定です。")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

JST = timezone(timedelta(hours=9))

# -------------------------------
# 場名 → 場コード
# -------------------------------
PLACE_CODE = {
    "桐生": "01", "戸田": "02", "江戸川": "03", "平和島": "04", "多摩川": "05",
    "浜名湖": "06", "蒲郡": "07", "常滑": "08", "津": "09", "三国": "10",
    "びわこ": "11", "住之江": "12", "尼崎": "13", "鳴門": "14", "丸亀": "15",
    "児島": "16", "宮島": "17", "徳山": "18", "下関": "19", "若松": "20",
    "芦屋": "21", "福岡": "22", "唐津": "23", "大村": "24",
}

HELP_TEXT = (
    "使い方：\n"
    "・『丸亀 8 20250808』のように送信（半角/全角スペースOK）\n"
    "・日付省略可。例：『丸亀 8』→今日の日付で出走表URLと予想を返します\n"
    "・対応競艇場："
    + "、".join(PLACE_CODE.keys())
)

# -------------------------------
# ユーティリティ
# -------------------------------
def today_ymd() -> str:
    return datetime.now(JST).strftime("%Y%m%d")

def racelist_url(place: str, race_no: int, ymd: str) -> str:
    """公式サイトの出走表URLを生成"""
    jcd = PLACE_CODE.get(place)
    if not jcd:
        return ""
    return f"https://www.boatrace.jp/owpc/pc/race/racelist?rno={race_no}&jcd={jcd}&hd={ymd}"

def parse_input(text: str):
    """
    例:
      丸亀 8 20250808
      浜名湖12 20250811
      住之江 9
    を (place, race_no, ymd) にして返す。失敗時は None。
    """
    t = text.strip()

    # help
    if t.lower() in {"help", "ヘルプ", "使い方"}:
        return ("__HELP__", None, None)

    # 場名・R・日付(任意) を拾う
    m = re.match(r"^\s*(\S+?)\s*([0-9１-９]{1,2})(?:\s*([0-9]{8}))?\s*$", t)
    if not m:
        return (None, None, None)

    place = m.group(1)
    # 全角数字→半角
    race_no_str = m.group(2).translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    try:
        race_no = int(race_no_str)
    except ValueError:
        return (None, None, None)

    if not (1 <= race_no <= 12):
        return (None, None, None)

    ymd = m.group(3) if m.group(3) else today_ymd()

    # 対応場のみ
    if place not in PLACE_CODE:
        return (None, None, None)

    return (place, race_no, ymd)

def predict(place: str, race_no: int, ymd: str) -> str:
    """
    v0.1：まずは定型フォーマットで返す（のちほど実データで強化）
    """
    url = racelist_url(place, race_no, ymd)
    header = f"\n"
    if url:
        header += f"出走表：{url}\n"
    else:
        header += "※場コード未対応\n"

    # 超簡易テンプレ（後でロジックを入れ替える）
    main =  "本線：1-2-3 / 1-3-2（イン想定）"
    osa  =  "抑え：2-1-全（差し・差し返し）"
    ner  =  "狙い：4-1-2, 1-4-2（カド一撃／まくり差し）"
    tenkai = "展開：①先マイ本線。②の差し・④のカド気配に注意。"

    return "\n".join([header, main, osa, ner, tenkai])

# -------------------------------
# ルーティング
# -------------------------------
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

# -------------------------------
# イベントハンドラ
# -------------------------------
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    text = event.message.text or ""
    place, race_no, ymd = parse_input(text)

    if place == "__HELP__":
        reply = HELP_TEXT
    elif place and race_no and ymd:
        reply = predict(place, race_no, ymd)
    else:
        reply = "うまく読めませんでした。\n" + HELP_TEXT

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply)
    )

# -------------------------------
# ローカル実行用
# -------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
