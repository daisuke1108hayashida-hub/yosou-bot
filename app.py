# app.py
import os
import re
import json
import logging
from datetime import datetime

import httpx
from bs4 import BeautifulSoup
from flask import Flask, request, abort

# ===== LINE SDK v3 =====
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi
)
from linebot.v3.messaging.models import (
    ReplyMessageRequest,
    TextMessage
)

# --------------------
# 環境変数
# --------------------
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")

if not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINE_CHANNEL_ACCESS_TOKEN が設定されていません。")

# MessagingApi は ApiClient を介して使う（v3の正しい使い方）
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

# --------------------
# Flask
# --------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = app.logger

# --------------------
# 場コード(一部)
# --------------------
JCD = {
    "桐生": "01", "戸田": "02", "江戸川": "03", "平和島": "04", "多摩川": "05",
    "浜名湖": "06", "蒲郡": "07", "常滑": "08", "津": "09", "三国": "10",
    "琵琶湖": "11", "住之江": "12", "尼崎": "13", "鳴門": "14", "丸亀": "15",
    "児島": "16", "宮島": "17", "徳山": "18", "下関": "19", "若松": "20",
    "芦屋": "21", "福岡": "22", "唐津": "23", "大村": "24"
}

# --------------------
# 便利関数
# --------------------
def reply_text(reply_token: str, text: str) -> None:
    """v3は ApiClient を作ってから MessagingApi を使う"""
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                replyToken=reply_token,
                messages=[TextMessage(text=text)]
            )
        )

def fmt_combo(a, b, c) -> str:
    return f"{a}-{b}-{c}"

def unique_keep_order(items):
    seen = set()
    out = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def parse_user_query(text: str):
    """
    例: '常滑 6 20250812' / '丸亀　8 20250811'
    戻り: (場名, jcd, rno:str, hd:str) or None
    """
    t = re.sub(r"[　\t]+", " ", text.strip())
    m = re.match(r"^(\S+)\s+(\d{1,2})\s+(\d{8})$", t)
    if not m:
        return None
    place = m.group(1)
    rno = f"{int(m.group(2))}"
    hd = m.group(3)
    jcd = JCD.get(place)
    if not jcd:
        return None
    return place, jcd, rno, hd

def beforeinfo_url(jcd: str, rno: str, hd: str) -> str:
    return f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={jcd}&hd={hd}"

async def fetch_beforeinfo(jcd: str, rno: str, hd: str):
    """公式 直前情報ページを取得（失敗しても None を返すだけ）"""
    url = beforeinfo_url(jcd, rno, hd)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(url, headers={"User-Agent": "yosou-bot/1.0"})
            r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        return url, soup
    except Exception as e:
        log.warning("beforeinfo fetch failed: %s", e)
        return url, None

def generate_narrative(place: str, rno: str):
    """
    データ無しでもそれっぽい展開コメントを作る簡易ルール。
    実測値が取れたらここで上書きする想定。
    """
    r = int(rno)
    lines = ["＿＿＿＿＿＿＿＿＿＿＿＿＿＿",
             "――――",
             f"{place} {r}Rの展望。 基本は内有利。"]

    # 時刻やレース番号で少し味付けを変えるだけの軽いロジック
    if r in (1,2):
        lines.append("①の先マイ本線。②の差しが対抗。③のまくり差しまで。")
    elif r in (3,4,5):
        lines.append("①の信頼はやや割引。②の差し、③のまくり差しに注意。")
    elif r in (6,7,8,9):
        lines.append("センターの機動力に要警戒。④⑤の一撃が怖い。")
    else:
        lines.append("波乱含み。ダッシュ勢④⑤⑥の仕掛けと隊形乱れに注意。")

    return "\n".join(lines)

def build_candidates():
    """
    汎用の買い目テンプレを返す。
    ・重複は除去
    ・表記は 1-2-3 のハイフン
    """
    main = [
        "1-2-3456", "1-3-2456",
        "1-23-4", "1-23-5", "1-23-6"
    ]
    press = [
        "2-1-345", "2-3-145", "2-13-4", "2-13-5",
        "1-4-23", "1-4-56"      # 1-4 がらみ少し追加
    ]
    hole = [
        "4-1-235", "5-1-234",
        "45-1-2", "45-1-3",
        "34-1-5"                # 追加の穴
    ]
    return main, press, hole

def expand_pattern(pat: str):
    """
    '1-23-56' → ['1-2-5','1-2-6','1-3-5','1-3-6']
    """
    a, b, c = pat.split("-")
    def exp(x): return list(x) if len(x) > 1 else [x]
    out = []
    for bb in exp(b):
        for cc in exp(c):
            out.append(fmt_combo(a, bb, cc))
    return out

def render_bets():
    main_pats, press_pats, hole_pats = build_candidates()

    main = unique_keep_order([x for p in main_pats for x in expand_pattern(p)])
    press = unique_keep_order([x for p in press_pats for x in expand_pattern(p)])
    hole = unique_keep_order([x for p in hole_pats for x in expand_pattern(p)])

    # 念のため 3連単の重複(完全一致)を全体でも除去
    seen = set()
    def dedup(lst):
        out = []
        for s in lst:
            if s not in seen:
                seen.add(s); out.append(s)
        return out
    return dedup(main), dedup(press), dedup(hole)

def render_message(place: str, jcd: str, rno: str, hd: str, src_url: str):
    header = generate_narrative(place, rno)
    main, press, hole = render_bets()

    def block(title, items):
        body = "\n".join(items)
        return f"{title}（{len(items)}点）\n{body}\n"

    msg = [
        header,
        f"(参考: {src_url})",
        block("本線", main),
        block("押え", press),
        block("穴目", hole),
    ]
    return "\n".join(msg).strip()

# --------------------
# ルート
# --------------------
@app.get("/")
def health():
    return "yosou-bot is running"

@app.get("/_debug/beforeinfo")
async def debug_beforeinfo():
    jcd = request.args.get("jcd", "08")
    rno = request.args.get("rno", "6")
    hd  = request.args.get("hd",  datetime.now().strftime("%Y%m%d"))
    url, soup = await fetch_beforeinfo(jcd, rno, hd)
    if soup is None:
        return f"[beforeinfo] fetch failed url={url}", 200
    title = soup.title.text if soup.title else "no-title"
    return f"[beforeinfo] ok url={url} title={title}", 200

@app.post("/callback")
def callback():
    # 署名検証は割愛（LINE Developers での quick test 用）
    body = request.get_data(as_text=True)
    try:
        payload = json.loads(body)
    except Exception:
        abort(400)

    events = payload.get("events", [])
    for ev in events:
        if ev.get("type") != "message":
            continue
        msg = ev.get("message", {})
        if msg.get("type") != "text":
            continue

        text = msg.get("text", "").strip()
        q = parse_user_query(text)
        if not q:
            help_text = (
                "使い方: 『場名 半角レース番号 半角日付(YYYYMMDD)』\n"
                "例) 常滑 6 20250812 / 丸亀 8 20250811"
            )
            reply_text(ev["replyToken"], help_text)
            continue

        place, jcd, rno, hd = q

        # 公式 直前情報(失敗してもURLだけは載せる)
        src_url, _soup = (beforeinfo_url(jcd, rno, hd), None)
        try:
            # 取得を試す（結果は今は使っていない／将来ここで強化）
            # 非同期を使わず同期で軽く叩く
            r = httpx.get(src_url, timeout=8.0, headers={"User-Agent": "yosou-bot/1.0"})
            r.raise_for_status()
        except Exception as e:
            log.warning("fetch beforeinfo failed: %s", e)

        message = render_message(place, jcd, rno, hd, src_url)
        reply_text(ev["replyToken"], message)

    return "OK"
