# -*- coding: utf-8 -*-
import os
import re
import asyncio
import logging
from datetime import datetime
from typing import Dict, Any, List, Tuple

import httpx
from bs4 import BeautifulSoup

from flask import Flask, request, abort

# ==== LINE SDK (v3系) ====
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    MessagingApi, Configuration, ApiClient,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# -------------------------
# Flask / logger
# -------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("yosou-bot")

# -------------------------
# LINE config
# -------------------------
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_TOKEN  = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")

handler = WebhookHandler(CHANNEL_SECRET) if CHANNEL_SECRET else None
configuration = Configuration(access_token=CHANNEL_TOKEN) if CHANNEL_TOKEN else None
line_api: MessagingApi | None = None
if configuration:
    line_api = MessagingApi(ApiClient(configuration))

# -------------------------
# 場コード（公式=JCD, 日和=place_no）
# -------------------------
JCD_BY_NAME = {
    "桐生": "01", "戸田": "02", "江戸川": "03", "平和島": "04",
    "多摩川": "05", "浜名湖": "06", "蒲郡": "07", "常滑": "08",
    "津": "09", "三国": "10", "びわこ": "11", "住之江": "12",
    "尼崎": "13", "鳴門": "14", "丸亀": "15", "児島": "16",
    "宮島": "17", "徳山": "18", "下関": "19", "若松": "20",
    "芦屋": "21", "福岡": "22", "唐津": "23", "大村": "24",
}
PLACE_NO_BY_NAME = {
    "桐生": 1, "戸田": 2, "江戸川": 3, "平和島": 4,
    "多摩川": 5, "浜名湖": 6, "蒲郡": 7, "常滑": 8,
    "津": 9, "三国": 10, "びわこ": 11, "住之江": 12,
    "尼崎": 13, "鳴門": 14, "丸亀": 15, "児島": 16,
    "宮島": 17, "徳山": 18, "下関": 19, "若松": 20,
    "芦屋": 21, "福岡": 22, "唐津": 23, "大村": 24,
}

HEADERS = {
    "User-Agent": "yosou-bot/1.0 (+https://render.com) Python httpx"
}

# =========================================================
# 取得系（1）ボート日和（在庫があれば使うが、無ければフォールバック）
# =========================================================
async def fetch_biyori(place_no: int, race_no: int, hiduke: str, slider: int = 4) -> Dict[str, Any]:
    """
    例）https://kyoteibiyori.com/race_shusso.php?place_no=15&rno=6&hiduke=20250812&slider=9
    テーブルが無い場合は ok=False を返す
    """
    url = (
        "https://kyoteibiyori.com/race_shusso.php"
        f"?place_no={place_no}&race_no={race_no}&hiduke={hiduke}&slider={slider}"
    )
    out: Dict[str, Any] = {"src": "biyori", "url": url, "ok": False, "reason": ""}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=HEADERS)
            r.raise_for_status()
    except Exception as e:
        out["reason"] = f"http_error:{e}"
        return out

    soup = BeautifulSoup(r.text, "lxml")
    table = soup.select_one("table")  # かなり雑に見る（DOMがよく変わるため）
    if not table:
        out["reason"] = "table-not-found"
        return out

    # ここで詳しくパースする実装はサイト変更で壊れやすい。
    # フラグだけ立てて本文は公式で補う。
    out["ok"] = True
    out["raw_exists"] = True
    return out


# =========================================================
# 取得系（2）公式サイト 直前情報ページ（フォールバックの本命）
# =========================================================
def _official_url(jcd: str, rno: int, hd: str) -> str:
    return (
        "https://www.boatrace.jp/owpc/pc/race/beforeinfo"
        f"?rno={rno}&jcd={jcd}&hd={hd}"
    )

async def fetch_official_preinfo(jcd: int | str, rno: int, hd: str) -> Dict[str, Any]:
    """
    直前情報をざっくり抜く（展示タイム/天候/風/波 など最低限）
    失敗しても ok=False と URL を返す。
    """
    jcd_str = str(jcd).zfill(2)
    url = _official_url(jcd_str, rno, hd)
    out: Dict[str, Any] = {
        "src": "official",
        "url": url,
        "ok": False,
        "raw_exists": False,
        "weather": {},
        "ex_times": {},   # 1～6号艇 展示タイム
        "st": {},         # 1～6号艇 ST（コンマ）
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url, headers=HEADERS)
            r.raise_for_status()
    except Exception as e:
        out["reason"] = f"http_error:{e}"
        return out

    soup = BeautifulSoup(r.text, "lxml")
    # 天候周り
    try:
        weather_box = soup.select_one(".weather1_body") or soup.select_one(".weather1")
        if weather_box:
            text = weather_box.get_text(" ", strip=True)
            # 雑に抽出
            m_wind = re.search(r"風\s*([+-]?\d+\.?\d*)m", text)
            m_wave = re.search(r"波\s*([+-]?\d+\.?\d*)cm", text)
            m_weather = re.search(r"(晴|曇|雨|雪)", text)
            if m_weather: out["weather"]["weather"] = m_weather.group(1)
            if m_wind:    out["weather"]["wind_m"] = m_wind.group(1)
            if m_wave:    out["weather"]["wave_cm"] = m_wave.group(1)
    except Exception:
        pass

    # 展示タイム/スタート
    try:
        # 展示タイム表は class 名が変わることがあるので th を頼りに拾う
        table_candidates = soup.select("table")
        for t in table_candidates:
            th_text = t.get_text(" ", strip=True)
            if "展示" in th_text and ("タイム" in th_text or "T" in th_text):
                # 下の行に 1～6 の数値が並ぶことが多い
                for i in range(1, 7):
                    m = re.search(rf"\b{i}\b.*?([0-9]\.[0-9]{{2}})", th_text)
                    if m:
                        out["ex_times"][i] = m.group(1)
        # ST（コンマ）
        if not out["ex_times"]:
            # うまく拾えなかったら諦める
            pass

        # ST は「進入コース別 ST」などからざっくり拾う
        for i in range(1, 7):
            m = re.search(rf"{i}\s*([0-9]\.[0-9]{{2}})", soup.get_text(" ", strip=True))
            if m:
                out["st"][i] = m.group(1)
    except Exception:
        pass

    out["ok"] = True
    out["raw_exists"] = True
    return out


# =========================================================
# 予想生成（簡易ロジック＋点数多め＋展開コメント）
# =========================================================
def build_predictions(ex_times: Dict[int, str], st: Dict[int, str]) -> Tuple[List[Tuple[int,int,int]], List[Tuple[int,int,int]], str]:
    """
    ex_times / st があればそれっぽく。無ければ 1コース軸の定型。
    戻り値：(本線3連単, 穴/抑え3連単, コメント)
    """
    # 数値化
    def f2(x: str) -> float:
        try:
            return float(x)
        except Exception:
            return 9.99

    # 展示の速い順
    ex_rank = sorted(range(1,7), key=lambda i: f2(ex_times.get(i, "9.99")))
    st_rank = sorted(range(1,7), key=lambda i: f2(st.get(i, "9.99")))

    # 軸候補
    axis = 1
    if ex_rank and ex_rank[0] != 1:
        # 展示トップが別なら迷わずそこも評価
        axis = ex_rank[0]

    # 本線 6点（◎→◯▲の相手流し）
    order = [axis] + [i for i in [1,2,3,4,5,6] if i != axis]
    mains: List[Tuple[int,int,int]] = []
    for b in order[1:4]:           # 3パターン
        for c in order[1:4]:
            if b == c: 
                continue
            mains.append((axis, b, c))
    mains = mains[:6]

    # 抑え（軸2着パターン＋カド絡み）6～8点
    sub: List[Tuple[int,int,int]] = []
    # 軸2着固定で手広く
    for a in order[1:5]:
        if a == axis: 
            continue
        for c in order[1:5]:
            if c in (a, axis): 
                continue
            sub.append((a, axis, c))
    # 4コース（カド）絡み
    if 4 != axis:
        sub.extend([(4, axis, 1), (axis, 4, 1)])

    # コメント
    com = []
    if ex_rank:
        com.append(f"展示タイム上位: {'-'.join(str(i) for i in ex_rank[:3])}")
    if st_rank:
        com.append(f"ST上位: {'-'.join(str(i) for i in st_rank[:3])}")
    if axis == 1:
        com.append("基本はイン先マイ。外が残る展開でヒモ荒れも。")
    elif axis == 4:
        com.append("カド一撃ケア。1マーク混戦なら道中逆転も。")
    else:
        com.append(f"{axis}号艇の足色良し。スタート決まれば押し切り。")

    return mains, sub[:8], " / ".join(com)


def format_reply(title: str, url: str, mains: List[Tuple[int,int,int]], subs: List[Tuple[int,int,int]], comment: str) -> str:
    def fmt(sets: List[Tuple[int,int,int]]) -> str:
        return "・" + "\n・".join("".join(map(str, s)) for s in sets) if sets else "（データ不足）"
    lines = [
        f"📍 {title}",
        "――――――――――",
        f"🎯本線（{}点）".format(len(mains)),
        fmt(mains),
        "",
        f"🔸抑え（{}点）".format(len(subs)),
        fmt(subs),
        "",
        f"📝展開メモ：{comment}" if comment else "📝展開メモ：データ薄",
        f"(src: 公式 / {url})"
    ]
    return "\n".join(lines)


# =========================================================
# 文章 → パラメータ解析（「常滑 6 20250812」など）
# =========================================================
def parse_user_text(text: str) -> Dict[str, Any]:
    t = text.strip().replace("　", " ")
    parts = re.split(r"\s+", t)
    if not parts:
        return {}
    name = parts[0]
    rno = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 12
    if len(parts) >= 3 and re.fullmatch(r"\d{8}", parts[2]):
        hd = parts[2]
    else:
        hd = datetime.now().strftime("%Y%m%d")
    if name not in JCD_BY_NAME:
        return {}
    return {"name": name, "jcd": JCD_BY_NAME[name], "place_no": PLACE_NO_BY_NAME[name], "rno": rno, "hd": hd}


# =========================================================
# ルート / デバッグ用エンドポイント
# =========================================================
@app.get("/")
def root():
    return (
        "yosou-bot OK. 使用例: 『常滑 6 20250812』をLINEに送信\n"
        "デバッグ: /_debug/official?jcd=08&rno=6&hd=20250812\n"
        "          /_debug/biyori?place_no=15&race_no=6&hiduke=20250812&slider=9"
    ), 200, {"Content-Type": "text/plain; charset=utf-8"}

@app.get("/_debug/official")
def debug_official():
    jcd = request.args.get("jcd", "08")
    rno = int(request.args.get("rno", "6"))
    hd  = request.args.get("hd", datetime.now().strftime("%Y%m%d"))
    res = asyncio.run(fetch_official_preinfo(jcd, rno, hd))
    lines = [f"[official] url={res.get('url')}", f"ok={res.get('ok')} exists={res.get('raw_exists')}"]
    if res.get("weather"): lines.append("weather=" + str(res["weather"]))
    if res.get("ex_times"): lines.append("ex_times=" + str(res["ex_times"]))
    if res.get("st"): lines.append("st=" + str(res["st"]))
    return "\n".join(lines), 200, {"Content-Type": "text/plain; charset=utf-8"}

@app.get("/_debug/biyori")
def debug_biyori():
    place_no = int(request.args.get("place_no", "15"))
    race_no  = int(request.args.get("race_no", request.args.get("rno", "6")))
    hiduke   = request.args.get("hiduke", request.args.get("hd", datetime.now().strftime("%Y%m%d")))
    slider   = int(request.args.get("slider", "9"))
    res = asyncio.run(fetch_biyori(place_no, race_no, hiduke, slider))
    return str(res), 200, {"Content-Type": "text/plain; charset=utf-8"}


# =========================================================
# LINE webhook
# =========================================================
@app.post("/callback")
def callback():
    if not handler:
        abort(500, "LINE handler not initialized. Set envs.")
    signature = request.headers.get("x-line-signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400, "Invalid signature.")
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    text = event.message.text.strip()
    q = parse_user_text(text)
    if not q:
        msg = (
            "入力例：\n"
            "『丸亀 8』 / 『丸亀 8 20250812』 / 『help』\n"
            "※ 場名（全角）+ 半角スペース + レース番号 + 任意で日付(YYYYMMDD)"
        )
        _reply(event.reply_token, [TextMessage(text=msg)])
        return

    name, jcd, place_no, rno, hd = q["name"], q["jcd"], q["place_no"], q["rno"], q["hd"]
    title = f"{name} {rno}R ({datetime.strptime(hd,'%Y%m%d').strftime('%Y/%m/%d')})"

    # 1) まずボート日和を軽く当ててみる（テーブル有無だけ）
    biyori = asyncio.run(fetch_biyori(place_no, rno, hd, slider=9))

    # 2) 公式から実データを取りに行く（最終的にこれで生成）
    official = asyncio.run(fetch_official_preinfo(jcd, rno, hd))

    # 予想を組み立て
    mains, subs, memo = build_predictions(official.get("ex_times", {}), official.get("st", {}))
    reply_text = format_reply(title, official.get("url", ""), mains, subs, memo)

    # 直前データ取得エラー時の文言
    if not official.get("ok"):
        reply_text = (
            f"📍 {title}\n――――――――――\n"
            "直前情報の取得に失敗しました。少し待ってから再度お試しください。\n"
            f"(src: 公式 / {_official_url(jcd, rno, hd)})"
        )

    _reply(event.reply_token, [TextMessage(text=reply_text)])


def _reply(token: str, messages: List[TextMessage]):
    if not line_api:
        logger.error("MessagingApi not initialized")
        return
    try:
        line_api.reply_message(ReplyMessageRequest(replyToken=token, messages=messages))
    except Exception as e:
        logger.exception("reply_message error: %s", e)


# ====== Render (gunicorn) ======
if __name__ == "__main__":
    # ローカル起動用（Render では gunicorn が起動）
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
