import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

from scraper import collect_all, build_urls, PLACE_CODE  # ← 取得＆予想

app = Flask(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("環境変数 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を設定してください。")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

JST = ZoneInfo("Asia/Tokyo")

HELP = (
    "使い方：\n"
    "・『丸亀 8 20250811』のように送信（場名 レース番号 日付）\n"
    "・日付は省略可（『丸亀 8』→今日）\n"
    "・返答：出走表URL＋直前サマリ＋本線/抑え/狙い＋展開\n"
    "対応場：" + "、".join(PLACE_CODE.keys())
)

def parse_input(text: str):
    t = (text or "").strip().replace("　", " ")
    if t.lower() in ("help", "ヘルプ", "使い方", "?"):
        return ("__HELP__", None, None)
    m = re.match(r"^\s*(\S+?)\s*([0-9１-９]{1,2})(?:\s+(\d{8}))?\s*$", t)
    if not m:
        return (None, None, None)
    place = m.group(1)
    rno = int(m.group(2).translate(str.maketrans("０１２３４５６７８９","0123456789")))
    ymd = m.group(3) or datetime.now(JST).strftime("%Y%m%d")
    if place not in PLACE_CODE or not (1 <= rno <= 12):
        return (None, None, None)
    return (place, rno, ymd)

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

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event: MessageEvent):
    raw = event.message.text
    place, rno, ymd = parse_input(raw)

    if place == "__HELP__":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=HELP))
        return
    if not place:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="入力を理解できませんでした。\n" + HELP))
        return

    # 取得＋予想（エラー耐性あり）
    try:
        data = collect_all(place, rno, ymd)   # dict
        urls = build_urls(place, rno, ymd)    # dict

        # 直前サマリ（展示タイム・チルト・風/波）
        def fmt_float(x):
            return f"{x:.2f}" if isinstance(x, float) else (x if x else "—")

        tenji = data.get("tenji_times", [])
        tilt  = data.get("tilts", [])
        wind  = data.get("weather", {}).get("wind", "—")
        wave  = data.get("weather", {}).get("wave", "—")
        wthr  = data.get("weather", {}).get("weather", "—")
        shinnyu = data.get("start_exhibit", "—")

        # 予想（本線/抑え/狙い/展開）
        pred = data.get("prediction", {})
        main = pred.get("main", [])
        sub  = pred.get("sub", [])
        atk  = pred.get("attack", [])
        cmt  = pred.get("comment", "—")
        conf = pred.get("confidence", "C")

        # 見やすい短文を組む
        lines = []
        lines.append(f"")
        lines.append(f"出走表: {urls['racelist']}")
        lines.append(f"直前: 展示T 1~6 = " + " / ".join(fmt_float(x) for x in tenji[:6]) )
        lines.append(f"　　: チルト 1~6 = " + " / ".join(str(x) if x is not None else "—" for x in tilt[:6]))
        lines.append(f"　　: 天候={wthr} 風={wind} 波={wave} 進入={shinnyu}")
        lines.append("")
        lines.append(f"本線: {', '.join(main) if main else '—'}")
        lines.append(f"抑え: {', '.join(sub) if sub else '—'}")
        lines.append(f"狙い: {', '.join(atk) if atk else '—'}")
        lines.append(f"展開: {cmt}（自信度:{conf}）")

        msg = "\n".join(lines)

    except Exception as e:
        urls = build_urls(place, rno, ymd)
        msg = (
            f"データ取得でエラーが発生しました：{e}\n"
            f"とりあえず出走表はこちら → {urls['racelist']}\n"
            f"racecard: {urls['racecard']}"
        )

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
