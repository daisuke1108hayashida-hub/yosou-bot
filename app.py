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

# ==== LINE SDK v3 ====
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    MessagingApi, Configuration, ApiClient,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("yosou-bot")

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_TOKEN  = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")

handler = WebhookHandler(CHANNEL_SECRET) if CHANNEL_SECRET else None
configuration = Configuration(access_token=CHANNEL_TOKEN) if CHANNEL_TOKEN else None
line_api: MessagingApi | None = MessagingApi(ApiClient(configuration)) if configuration else None

# 場コード
JCD_BY_NAME = {
    "桐生":"01","戸田":"02","江戸川":"03","平和島":"04","多摩川":"05","浜名湖":"06","蒲郡":"07","常滑":"08",
    "津":"09","三国":"10","びわこ":"11","住之江":"12","尼崎":"13","鳴門":"14","丸亀":"15","児島":"16",
    "宮島":"17","徳山":"18","下関":"19","若松":"20","芦屋":"21","福岡":"22","唐津":"23","大村":"24",
}
PLACE_NO_BY_NAME = {k:i for i,k in enumerate([
    None,"桐生","戸田","江戸川","平和島","多摩川","浜名湖","蒲郡","常滑","津","三国","びわこ",
    "住之江","尼崎","鳴門","丸亀","児島","宮島","徳山","下関","若松","芦屋","福岡","唐津","大村"
]) if k}

HEADERS = {"User-Agent":"yosou-bot/2.0 (+render) httpx"}

# ==========
# 公式URL
# ==========
def url_beforeinfo(jcd:str, rno:int, hd:str)->str:
    return f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={jcd}&hd={hd}"

def url_shutuba(jcd:str, rno:int, hd:str)->str:
    return f"https://www.boatrace.jp/owpc/pc/race/shutuba?rno={rno}&jcd={jcd}&hd={hd}"

# ==========
# 日和（存在チェックだけ・今は補助）
# ==========
async def fetch_biyori_exists(place_no:int, race_no:int, hiduke:str, slider:int=9)->Dict[str,Any]:
    url = f"https://kyoteibiyori.com/race_shusso.php?place_no={place_no}&race_no={race_no}&hiduke={hiduke}&slider={slider}"
    out = {"src":"biyori","url":url,"ok":False}
    try:
        async with httpx.AsyncClient(timeout=15) as cl:
            r = await cl.get(url, headers=HEADERS)
            r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        out["ok"] = bool(soup.select_one("table"))
    except Exception as e:
        out["reason"] = f"http:{e}"
    return out

# ==========
# 公式：直前情報（天候/展示/STなど）
# ==========
async def fetch_beforeinfo(jcd:str, rno:int, hd:str)->Dict[str,Any]:
    url = url_beforeinfo(jcd, rno, hd)
    res = {"src":"official.before","url":url,"ok":False,"weather":{},"ex":{},"st":{}}
    try:
        async with httpx.AsyncClient(timeout=20) as cl:
            r = await cl.get(url, headers=HEADERS)
            r.raise_for_status()
    except Exception as e:
        res["reason"]=f"http:{e}"
        return res

    soup = BeautifulSoup(r.text, "lxml")
    txt = soup.get_text(" ", strip=True)

    # 天候
    m_we = re.search(r"(晴|曇|雨|雪)", txt)
    m_wind = re.search(r"風\s*([+-]?\d+(?:\.\d+)?)m", txt)
    m_wave = re.search(r"波\s*([+-]?\d+(?:\.\d+)?)cm", txt)
    if m_we:   res["weather"]["weather"]=m_we.group(1)
    if m_wind: res["weather"]["wind_m"]=m_wind.group(1)
    if m_wave: res["weather"]["wave_cm"]=m_wave.group(1)

    # 展示タイムとST（ざっくり）
    # 例: "1 6.72 2 6.78 ..." / "1 0.14 2 0.16 ..."
    for i in range(1,7):
        m = re.search(rf"\b{i}\b[^0-9]*([1-9]\.\d{{2}})", txt)
        if m: res["ex"][i]=m.group(1)
        s = re.search(rf"\b{i}\b[^0-9]*0\.\d{{2}}", txt)
        if s:
            val = re.search(r"0\.\d{2}", s.group(0))
            if val: res["st"][i]=val.group(0)

    res["ok"]=True
    return res

# ==========
# 公式：出走表（選手情報・モーター/ボート2連率・コース別3連対率など）
# ==========
async def fetch_shutuba(jcd:str, rno:int, hd:str)->Dict[str,Any]:
    url = url_shutuba(jcd, rno, hd)
    out = {"src":"official.shutuba","url":url,"ok":False,"rows":{}}  # rows[1..6] = dict
    try:
        async with httpx.AsyncClient(timeout=20) as cl:
            r = await cl.get(url, headers=HEADERS)
            r.raise_for_status()
    except Exception as e:
        out["reason"]=f"http:{e}"
        return out

    soup = BeautifulSoup(r.text, "lxml")
    txt = soup.get_text("\n", strip=True)

    # 1号艇～6号艇でブロックを切り出して荒めに抽出
    for lane in range(1,7):
        block = {}
        # 選手名・級別
        m_name = re.search(rf"{lane}号艇\s*([^\s　]+)\s*(A1|A2|B1|B2)", txt)
        if m_name:
            block["name"]=m_name.group(1)
            block["class"]=m_name.group(2)

        # モーター No / 2連率
        m_motor = re.search(rf"{lane}号艇.*?モーター\s*No\.\s*(\d+).*?(\d+\.\d+)\s*%", txt, re.S)
        if m_motor:
            block["motor_no"]=m_motor.group(1)
            block["motor_2r"]=m_motor.group(2)

        # ボート No / 2連率
        m_boat = re.search(rf"{lane}号艇.*?ボート\s*No\.\s*(\d+).*?(\d+\.\d+)\s*%", txt, re.S)
        if m_boat:
            block["boat_no"]=m_boat.group(1)
            block["boat_2r"]=m_boat.group(2)

        # コース別3連対率（例：コース別 3連率 1:xx.x 2:..）
        m_course = re.search(rf"{lane}号艇.*?コース別.*?3連.*?(?:1|１)[:：]\s*(\d+\.\d).*?(?:2|２)[:：]\s*(\d+\.\d).*?(?:3|３)[:：]\s*(\d+\.\d)", txt, re.S)
        if m_course:
            block["course_3r"]={"1":m_course.group(1),"2":m_course.group(2),"3":m_course.group(3)}

        out["rows"][lane]=block

    out["ok"]=True if out["rows"] else False
    return out

# ==========
# スコアリング → 買い目生成（本線10＋抑え12＋穴6）
# ==========
def _to_f(x, default=0.0):
    try: return float(x)
    except: return default

def score_lane(lane:int, shutuba:Dict[int,dict], before:Dict[str,dict])->float:
    row = shutuba.get(lane, {})
    ex  = before.get("ex", {}).get(lane)
    st  = before.get("st", {}).get(lane)

    # 基本点（内優位）
    base = {1:60,2:48,3:44,4:40,5:34,6:30}[lane]

    # 展示は速いほど加点（6.50基準）
    if ex: base += max(0, (6.60 - _to_f(ex))*20)
    # STは速いほど加点（0.15基準）
    if st: base += max(0, (0.15 - _to_f(st))*200)

    # モーター・ボート2連率
    base += _to_f(row.get("motor_2r"),0)*0.6
    base += _to_f(row.get("boat_2r"),0)*0.3

    # 級別
    cls = row.get("class","")
    if cls=="A1": base += 8
    elif cls=="A2": base += 4
    elif cls=="B1": base -= 2
    else: base -= 5

    return base

def build_cards(shutuba_rows:Dict[int,dict], beforeinfo:Dict[str,Any])->Tuple[List[Tuple[int,int,int]], List[Tuple[int,int,int]], List[Tuple[int,int,int]], str]:
    # スコア
    scores = {i: score_lane(i, shutuba_rows, beforeinfo) for i in range(1,7)}
    order = sorted(scores, key=lambda k: scores[k], reverse=True)

    # 本命軸＝top
    axis = order[0]
    second = order[1:4]  # 相手本線
    others = order[4:6]

    mains=[]   # 10点
    for b in second:
        for c in second:
            if b==c: continue
            mains.append((axis,b,c))
    # 6点→足りない分をothersで補充
    for b in second:
        for c in others:
            if len(mains)>=10: break
            mains.append((axis,b,c))

    # 抑え（軸2着固定＋カド）12点
    subs=[]
    for a in second+others:
        for c in second+others:
            if a in (axis,) or c in (axis,a): continue
            subs.append((a,axis,c))
            if len(subs)>=12: break
        if len(subs)>=12: break
    if 4!=axis:
        subs[:0]=[(axis,4,order[0]), (4,axis,order[0])]

    # 穴（上位同士の裏目＋ダッシュ絡み）6点
    dash = [i for i in order if i>=4][:2]  # 4,5 or 4,5,6
    holes=[]
    for a in dash:
        for b in order[:3]:
            if a==b: continue
            holes.append((a,b,axis))
            if len(holes)>=6: break
        if len(holes)>=6: break

    # 展開メモ
    com=[]
    ex = beforeinfo.get("ex",{})
    st = beforeinfo.get("st",{})
    if ex:
        ex_top = sorted(ex, key=lambda i: _to_f(ex[i],9.99))[:3]
        com.append("展示上位: " + "-".join(map(str,ex_top)))
    if st:
        st_top = sorted(st, key=lambda i: _to_f(st[i],9.99))[:3]
        com.append("ST上位: " + "-".join(map(str,st_top)))
    wx = beforeinfo.get("weather",{})
    if wx:
        w = wx.get("weather","")
        wm = wx.get("wind_m","?")
        wave = wx.get("wave_cm","?")
        com.append(f"天候:{w} 風{wm}m 波{wave}cm")
    # モーター良い順
    mot_rank = sorted(shutuba_rows, key=lambda i: _to_f(shutuba_rows[i].get("motor_2r")), reverse=True)
    com.append("機力上位: " + "-".join(map(str, mot_rank[:3])))
    com.append(f"軸は{axis}号艇。対抗{second[0]}、単穴{second[1]}評価。")

    return mains[:10], subs[:12], holes[:6], " / ".join(com)

def format_reply(title:str, url:str, mains, subs, holes, memo)->str:
    def cat(label, picks):
        rows = "・" + "\n・".join("".join(map(str,p)) for p in picks) if picks else "（データ不足）"
        return f"{label}（{len(picks)}点）\n{rows}"
    lines = [
        f"📍 {title}",
        "――――――――――",
        cat("🎯本線", mains),
        "",
        cat("🔸抑え", subs),
        "",
        cat("🌋穴目", holes),
        "",
        f"📝展開メモ：{memo}",
        f"(src: 公式 / {url})"
    ]
    return "\n".join(lines)

# ==========
# 入力解析
# ==========
def parse_user_text(text:str)->Dict[str,Any]:
    t = text.strip().replace("　"," ")
    ps = re.split(r"\s+", t)
    if not ps: return {}
    name = ps[0]
    rno = int(ps[1]) if len(ps)>=2 and ps[1].isdigit() else 12
    hd  = ps[2] if len(ps)>=3 and re.fullmatch(r"\d{8}", ps[2]) else datetime.now().strftime("%Y%m%d")
    if name not in JCD_BY_NAME: return {}
    return {"name":name, "jcd":JCD_BY_NAME[name], "place_no":PLACE_NO_BY_NAME[name], "rno":rno, "hd":hd}

# ==========
# ルーティング
# ==========
@app.get("/")
def root():
    return (
        "yosou-bot v2 OK\n"
        "例: 『常滑 6 20250812』\n"
        "debug: /_debug/shutuba?jcd=08&rno=6&hd=20250812\n"
        "       /_debug/before?jcd=08&rno=6&hd=20250812\n"
        "       /_debug/biyori?place_no=15&race_no=6&hiduke=20250812"
    ), 200, {"Content-Type":"text/plain; charset=utf-8"}

@app.get("/_debug/before")
def dbg_before():
    jcd = request.args.get("jcd","08"); rno=int(request.args.get("rno","6")); hd=request.args.get("hd", datetime.now().strftime("%Y%m%d"))
    res = asyncio.run(fetch_beforeinfo(jcd,rno,hd))
    return str(res), 200, {"Content-Type":"text/plain; charset=utf-8"}

@app.get("/_debug/shutuba")
def dbg_shutuba():
    jcd = request.args.get("jcd","08"); rno=int(request.args.get("rno","6")); hd=request.args.get("hd", datetime.now().strftime("%Y%m%d"))
    res = asyncio.run(fetch_shutuba(jcd,rno,hd))
    return str(res), 200, {"Content-Type":"text/plain; charset=utf-8"}

@app.get("/_debug/biyori")
def dbg_biyori():
    place_no=int(request.args.get("place_no","15")); race_no=int(request.args.get("race_no", request.args.get("rno","6"))); hiduke=request.args.get("hiduke", request.args.get("hd", datetime.now().strftime("%Y%m%d")))
    res = asyncio.run(fetch_biyori_exists(place_no,race_no,hiduke))
    return str(res), 200, {"Content-Type":"text/plain; charset=utf-8"}

# ==========
# LINE webhook
# ==========
@app.post("/callback")
def callback():
    if not handler: abort(500, "LINE handler not init")
    signature = request.headers.get("x-line-signature","")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400, "Invalid signature")
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    q = parse_user_text(event.message.text)
    if not q:
        help_msg = (
            "入力例：『丸亀 8』 / 『丸亀 8 20250812』\n"
            "対応場：桐生〜大村（漢字名）"
        )
        _reply(event.reply_token, [TextMessage(text=help_msg)])
        return

    name, jcd, place_no, rno, hd = q["name"], q["jcd"], q["place_no"], q["rno"], q["hd"]
    title = f"{name} {rno}R ({datetime.strptime(hd,'%Y%m%d').strftime('%Y/%m/%d')})"

    # 1) 出走表 2) 直前情報 3) （任意）日和の存在チェック
    shutuba = asyncio.run(fetch_shutuba(jcd, rno, hd))
    before  = asyncio.run(fetch_beforeinfo(jcd, rno, hd))
    _ = asyncio.run(fetch_biyori_exists(place_no, rno, hd))  # 使い所があれば表示に混ぜる

    if not shutuba.get("ok") and not before.get("ok"):
        txt = f"📍 {title}\n――――――――――\n直前情報の取得に失敗しました。時間をおいて再度お試しください。\n(src: 公式 / {url_beforeinfo(jcd,rno,hd)})"
        _reply(event.reply_token, [TextMessage(text=txt)])
        return

    mains, subs, holes, memo = build_cards(shutuba.get("rows",{}), before)
    txt = format_reply(title, before.get("url", url_beforeinfo(jcd,rno,hd)), mains, subs, holes, memo)
    _reply(event.reply_token, [TextMessage(text=txt)])

def _reply(token:str, msgs:List[TextMessage]):
    if not line_api: return
    try:
        line_api.reply_message(ReplyMessageRequest(replyToken=token, messages=msgs))
    except Exception as e:
        log.exception("line reply error: %s", e)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","10000")))
