import os
import re
from datetime import datetime, timedelta, timezone

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

from biyori import fetch_biyori, score_lanes, make_trifecta, narrative

app = Flask(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("環境変数 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN が未設定です。")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)
JST = timezone(timedelta(hours=9))

HELP = (
    "使い方：\n"
    "① ボートレース日和の『直前情報』ページURLをそのまま送る\n"
    "　例）https://kyoteibiyori.com/ ...\n"
    "②（次の段階で）『丸亀 8 20250811』の形式からURL自動生成に対応します\n"
)

# --------- Utilities ---------
def extract_url(s: str) -> str | None:
    m = re.search(r"https?://\S+", s)
    return m.group(0) if m else None

def reply(token, text): line_bot_api.reply_message(token, TextSendMessage(text=text))

# --------- Routes ---------
@app.route("/health")
def health(): return "ok", 200

@app.route("/")
def index(): return "ok", 200

@app.route("/callback", methods=["POST"])
def callback():
    sig = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, sig)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# --------- Handler ---------
@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    text = (event.message.text or "").strip()

    # help
    if text.lower() in ("help","ヘルプ","使い方","?"):
        reply(event.reply_token, HELP)
        return

    # まずは URL 優先（Biyori直前ページ）
    url = extract_url(text)
    if url and "kyoteibiyori.com" in url:
        try:
            before = fetch_biyori(url)
            lanes = before["lanes"]
            scores = score_lanes(lanes)
            picks = make_trifecta(scores, max_patterns=9)
            story = narrative(lanes, scores)

            msg = []
            msg.append("🧭 展開予想：" + story)
            msg.append("🎯 本線：" + (", ".join(picks["hon"]) if picks["hon"] else "—"))
            msg.append("🛡 抑え：" + (", ".join(picks["osae"]) if picks["osae"] else "—"))
            msg.append("💥 狙い：" + (", ".join(picks["nerai"]) if picks["nerai"] else "—"))
            msg.append(f"\n直前情報：{before['meta']['url']}")
            reply(event.reply_token, "\n".join(msg))
        except Exception as e:
            reply(event.reply_token, f"ボートレース日和の直前情報取得でエラー：{e}\nURLが直前ページか確認してください。\n（helpで使い方）")
        return

    # URLでなければ今はガイド
    reply(event.reply_token, "直前情報はボートレース日和のURLを送ってください。\n（例）https://kyoteibiyori.com/... の『直前情報』ページ\nhelp で使い方")
    return

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
