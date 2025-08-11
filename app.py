# app.py
# LINE Bot / Flask / Render 用
# 直前情報は「ボートレース日和」を最優先でスクレイピングします
# 必要ENV: LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN
# 任意ENV: BIYORI_URL_TEMPLATE (URLテンプレ差し替え用)

import os
import re
import time
import json
import traceback
import requests
from datetime import datetime, timedelta, timezone

from flask import Flask, request, abort
from bs4 import BeautifulSoup

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# ========= 基本セットアップ =========
app = Flask(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("環境変数 LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN を設定してください")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

JST = timezone(timedelta(hours=9))

# 競艇場名 → jcd
JCD = {
    "桐生": 1, "戸田": 2, "江戸川": 3, "平和島": 4, "多摩川": 5, "浜名湖": 6, "蒲郡": 7, "常滑": 8,
    "津": 9, "三国": 10, "びわこ": 11, "住之江": 12, "尼崎": 13, "鳴門": 14, "丸亀": 15,
    "児島": 16, "宮島": 17, "徳山": 18, "下関": 19, "若松": 20, "芦屋": 21, "福岡": 22,
    "唐津": 23, "大村": 24,
}

# ========= ヘルスチェック =========
@app.route("/health")
def health():
    return "ok", 200

@app.route("/")
def index():
    return "ok", 200

# ========= 直前情報（ボートレース日和優先） =========
def get_biyori_preinfo(venue: str, rno: int, yyyymmdd: str):
    """
    ボートレース日和の「直前情報」テーブルを取得して整形
    戻り値:
      {
        "per_lane": {1:{指標:値,...},...,6:{...}},
        "url": "<参照URL>",
        "src": "biyori"
      }
    失敗時は None
    """
    try:
        jcd = JCD.get(venue)
        if not jcd:
            return None

        # URL テンプレ（変わる可能性に備え ENV で上書き可）
        tmpl = os.getenv(
            "BIYORI_URL_TEMPLATE",
            "https://kyoteibiyori.com/race?jcd={jcd}&hd={date}&rno={rno}"
        )
        url = tmpl.format(jcd=jcd, date=yyyymmdd, rno=rno)

        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=15)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "lxml")

        # 「直前情報」タブのテーブルを推定（展示/周回/周り足/直線/ST が含まれている）
        target_table = None
        for table in soup.find_all("table"):
            txt = table.get_text(" ", strip=True)
            hits = sum(k in txt for k in ["展示", "周回", "周り足", "直線", "ST"])
            if hits >= 4:
                target_table = table
                break
        if not target_table:
            return None

        rows = [
            [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
            for tr in target_table.find_all("tr")
        ]
        if not rows or len(rows) < 2:
            return None

        # 1行目: 1号艇〜6号艇（想定）
        metrics = [r[0] for r in rows[1:]]             # 指標名（展示/周回/…）
        values_matrix = [r[1:] for r in rows[1:]]       # 各号艇の値

        per_lane = {}
        num_lanes = min(6, max(len(v) for v in values_matrix))
        for lane in range(num_lanes):
            per_lane[lane + 1] = {}
            for midx, m in enumerate(metrics):
                val = values_matrix[midx][lane] if lane < len(values_matrix[midx]) else ""
                per_lane[lane + 1][m] = val

        return {"per_lane": per_lane, "url": url, "src": "biyori"}
    except Exception:
        return None

def get_official_preinfo(venue: str, rno: int, yyyymmdd: str):
    """
    公式はフォールバック用途。必要なら後で実装を厚くする。
    ここでは None を返す（＝利用しない）。
    """
    return None

def get_pre_race_info(venue: str, rno: int, yyyymmdd: str):
    """
    呼び出し用：①日和 → ②公式 の順で試す
    """
    data = get_biyori_preinfo(venue, rno, yyyymmdd)
    if data:
        return data
    return get_official_preinfo(venue, rno, yyyymmdd)

# ========= 予想ロジック（軽量版） =========
def _to_float(s):
    try:
        return float(re.findall(r"[0-9.]+", s)[0])
    except Exception:
        return None

def build_prediction_from_preinfo(preinfo: dict):
    """
    per_lane から簡易スコアを作成して展開文&買い目を生成
    - 展示（小さいほど良い）
    - 直線（大きいほど良い）
    の2軸を0〜1で正規化して合算。
    """
    per = preinfo.get("per_lane", {})
    # 値の取得
    demo = {i: _to_float(per[i].get("展示", "")) for i in per}
    straight = {i: _to_float(per[i].get("直線", "")) for i in per}

    # 正規化
    score = {}
    d_vals = [v for v in demo.values() if v is not None]
    s_vals = [v for v in straight.values() if v is not None]
    d_min, d_max = (min(d_vals), max(d_vals)) if d_vals else (None, None)
    s_min, s_max = (min(s_vals), max(s_vals)) if s_vals else (None, None)

    for i in per:
        sc = 0.0
        d = demo[i]; s = straight[i]
        if d is not None and d_min is not None and d_max is not None and d_max != d_min:
            sc += (d_max - d) / (d_max - d_min)  # 展示は小さいほど加点
        if s is not None and s_min is not None and s_max is not None and s_max != s_min:
            sc += (s - s_min) / (s_max - s_min)  # 直線は大きいほど加点
        score[i] = sc

    # スコア順
    order = sorted(score.keys(), key=lambda k: score[k], reverse=True)
    if len(order) < 3:
        # データ不足なら適当に並び替え
        order = list(range(1, 7))[:max(3, len(order))]

    # 展開コメント
    lead = order[0]; chase = order[1]
    scenario = f"直前指標は{lead}号艇が軸、続く{chase}号艇。内先行から差し・まくり差し警戒。"

    # 買い目
    # 本線：軸-相手-三番手
    hon = [f"{lead}-{chase}-{order[2]}", f"{lead}-{order[2]}-{chase}"]
    # 抑え：相手-軸-三番手
    osa = [f"{chase}-{lead}-{order[2]}", f"{chase}-{lead}-{order[3] if len(order)>3 else order[2]}"]
    # 狙い：三番手絡みのひねり
    ner = [f"{order[2]}-{lead}-{chase}", f"{order[2]}-{chase}-{lead}"]

    return scenario, hon, osa, ner

# ========= 入力パース =========
HELP_TEXT = (
    "使い方：\n"
    "・『丸亀 8 20250811』のように送信（日時省略可。例：『丸亀 8』は今日の8R）\n"
    "・『help』でこの説明を表示\n"
)

def parse_user_text(text: str):
    """
    返り値: (venue, rno, yyyymmdd) or (None,None,None)  ※helpは別扱い
    例: '丸亀 8 20250811' / '丸亀 8'
    """
    t = text.strip().replace("　", " ")
    if re.fullmatch(r"(?i)help|ヘルプ", t):
        return "HELP", None, None

    m = re.match(r"^(\S+)\s+(\d{1,2})(?:\s+(\d{8}))?$", t)
    if not m:
        return None, None, None
    venue = m.group(1)
    rno = int(m.group(2))
    date = m.group(3)
    if date is None:
        date = datetime.now(JST).strftime("%Y%m%d")
    return venue, rno, date

# ========= LINE Webhook =========
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "ok"

@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    user_text = (event.message.text or "").strip()
    venue, rno, date = parse_user_text(user_text)

    if venue == "HELP":
        reply(event.reply_token, HELP_TEXT)
        return

    if not venue:
        reply(event.reply_token, "入力例：『丸亀 8』 / 『丸亀 8 20250811』\n'help' で使い方を表示します。")
        return

    if venue not in JCD:
        reply(event.reply_token, f"場名『{venue}』が見つかりません。例：丸亀、浜名湖、徳山…")
        return
    if not (1 <= rno <= 12):
        reply(event.reply_token, "レース番号は 1〜12 を指定してください。")
        return

    try:
        pre = get_pre_race_info(venue, rno, date)
        if not pre:
            reply(event.reply_token, "直前情報の取得に失敗しました。少し待ってから再度お試しください。")
            return

        scenario, hon, osa, ner = build_prediction_from_preinfo(pre)

        title = f"📍 {venue} {rno}R ({datetime.strptime(date,'%Y%m%d').strftime('%Y/%m/%d')})"
        bar = "―" * 28
        body_lines = [title, bar, f"🧭 展開予想：{scenario}", "", "――――", f"🎯 本線：{', '.join(hon)}",
                      f"🛡️ 抑え：{', '.join(osa)}", f"💥 狙い：{', '.join(ner)}"]
        src = pre.get("src", "")
        url = pre.get("url")
        if url:
            body_lines.append(f"\n（直前情報 元：{url}）" if src == "biyori" else f"\n（直前情報 元：公式）")

        reply(event.reply_token, "\n".join(body_lines))
        # 軽い間隔（アクセス集中回避）
        time.sleep(0.5)
    except Exception as e:
        traceback.print_exc()
        reply(event.reply_token, "処理中にエラーが発生しました。時間をおいて再度お試しください。")

def reply(token, text):
    line_bot_api.reply_message(token, TextSendMessage(text=text))

# ========= エントリーポイント =========
if __name__ == "__main__":
    # Render では Procfile から gunicorn を使う想定。ローカル実行用に以下を有効化
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
