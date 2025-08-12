# -*- coding: utf-8 -*-
import os, re, time, asyncio, logging
from datetime import datetime
from typing import Dict, Any, List, Tuple

import httpx
from bs4 import BeautifulSoup
from flask import Flask, request, abort

# ===== LINE SDK v3 =====
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

HEADERS = {"User-Agent": "yosou-bot/4.0 (+render) httpx"}

# ====== 簡易キャッシュ（TTL秒） ======
CACHE: Dict[str, Tuple[float, str]] = {}
TTL = 120.0

async def get_html(url: str) -> str:
    now = time.time()
    if url in CACHE and CACHE[url][0] > now:
        return CACHE[url][1]
    async with httpx.AsyncClient(timeout=20, headers=HEADERS) as cl:
        r = await cl.get(url)
        r.raise_for_status()
    CACHE[url] = (now + TTL, r.text)
    return r.text

# ===== 場コード =====
JCD_BY_NAME = {
    "桐生":"01","戸田":"02","江戸川":"03","平和島":"04","多摩川":"05","浜名湖":"06","蒲郡":"07","常滑":"08",
    "津":"09","三国":"10","びわこ":"11","住之江":"12","尼崎":"13","鳴門":"14","丸亀":"15","児島":"16",
    "宮島":"17","徳山":"18","下関":"19","若松":"20","芦屋":"21","福岡":"22","唐津":"23","大村":"24",
}
PLACE_NO_BY_NAME = {k:i for i,k in enumerate(
    [None,"桐生","戸田","江戸川","平和島","多摩川","浜名湖","蒲郡","常滑","津","三国","びわこ","住之江",
     "尼崎","鳴門","丸亀","児島","宮島","徳山","下関","若松","芦屋","福岡","唐津","大村"]) if k}

# ===== URL作成 =====
def url_beforeinfo(jcd:str, rno:int, hd:str)->str:
    return f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={jcd}&hd={hd}"

def url_shutuba(jcd:str, rno:int, hd:str)->str:
    return f"https://www.boatrace.jp/owpc/pc/race/shutuba?rno={rno}&jcd={jcd}&hd={hd}"

def url_odds3t(jcd:str, rno:int, hd:str)->str:
    return f"https://www.boatrace.jp/owpc/pc/race/odds3t?rno={rno}&jcd={jcd}&hd={hd}"

# ===== 小物 =====
def _to_f(x, default=0.0):
    try: return float(x)
    except: return default

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\u3000"," ")).strip()

# ===== 直前情報 =====
async def fetch_beforeinfo(jcd:str, rno:int, hd:str)->Dict[str,Any]:
    url = url_beforeinfo(jcd, rno, hd)
    res = {"src":"official.before","url":url,"ok":False,
           "weather":{}, "ex":{}, "st":{}, "tilt":{}, "parts":{}, "ex_rank":[]}
    try:
        html = await get_html(url)
    except Exception as e:
        res["reason"]=f"http:{e}"
        return res

    soup = BeautifulSoup(html, "lxml")
    text = _norm(soup.get_text(" "))

    # 天候・風・波・風向
    m_we   = re.search(r"(晴|曇|雨|雪)", text)
    m_wind = re.search(r"(向かい風|追い風|左横風|右横風|無風)?\s*風\s*([\-+]?\d+(?:\.\d+)?)m", text)
    m_wave = re.search(r"波\s*([\-+]?\d+(?:\.\d+)?)cm", text)
    if m_we:   res["weather"]["weather"]=m_we.group(1)
    if m_wind:
        if m_wind.group(1): res["weather"]["wind_dir"]=m_wind.group(1)
        res["weather"]["wind_m"]=m_wind.group(2)
    if m_wave: res["weather"]["wave_cm"]=m_wave.group(1)

    # 展示タイム
    # 例: "1 6.72 2 6.76 ..." の並びを広く拾う
    for i in range(1,7):
        m = re.search(rf"\b{i}\b[^0-9]*([1-9]\.\d{{2}})", text)
        if m: res["ex"][i]=m.group(1)
    if res["ex"]:
        res["ex_rank"] = sorted(res["ex"], key=lambda k:_to_f(res["ex"][k],9.99))

    # ST
    for i in range(1,7):
        s = re.search(rf"\b{i}\b[^0-9]*0\.\d{{2}}", text)
        if s:
            v = re.search(r"0\.\d{2}", s.group(0))
            if v: res["st"][i]=v.group(0)

    # チルト
    tilt_blk = re.search(r"チルト[^0-9\-]*([\-0-9\.\s]+)", text)
    if tilt_blk:
        nums = re.findall(r"[\-]?\d+(?:\.\d)?", tilt_blk.group(1))
        for i, v in enumerate(nums[:6], start=1):
            res["tilt"][i] = v

    # 部品交換
    pblk = re.search(r"(部品交換[^。]*。?)", text)
    if pblk:
        res["parts"]["note"] = pblk.group(1)

    res["ok"]=True
    return res

# ===== 出走表（選手データ込み） =====
async def fetch_shutuba(jcd:str, rno:int, hd:str)->Dict[str,Any]:
    url = url_shutuba(jcd, rno, hd)
    out = {"src":"official.shutuba","url":url,"ok":False,"rows":{}}  # rows[1..6]
    try:
        html = await get_html(url)
    except Exception as e:
        out["reason"]=f"http:{e}"
        return out

    soup = BeautifulSoup(html, "lxml")
    txt = _norm(soup.get_text(" "))

    # 1〜6号艇のブロックごとに拾う
    for lane in range(1,7):
        d: Dict[str,Any] = {}
        # 名前・級別
        m_name = re.search(rf"{lane}号艇\s*([^\s]+)\s*(A1|A2|B1|B2)", txt)
        if m_name: d["name"], d["class"] = m_name.group(1), m_name.group(2)

        # モーター/ボート 2連率
        mm = re.search(rf"{lane}号艇.*?モーター\s*No\.\s*(\d+).*?(\d+\.\d+)\s*%.*?ボート\s*No\.\s*(\d+).*?(\d+\.\d+)\s*%", txt, re.S)
        if mm:
            d["motor_no"], d["motor_2r"] = mm.group(1), mm.group(2)
            d["boat_no"],  d["boat_2r"]  = mm.group(3), mm.group(4)

        # 全国勝率/当地勝率/平均ST
        ww = re.search(rf"{lane}号艇.*?(全国勝率\s*(\d+\.\d))?.*?(当地勝率\s*(\d+\.\d))?.*?(平均ST\s*(0\.\d{{2}}))?", txt, re.S)
        if ww:
            if ww.group(2): d["z_win"]=ww.group(2)
            if ww.group(4): d["t_win"]=ww.group(4)
            if ww.group(6): d["avg_st"]=ww.group(6)

        out["rows"][lane]=d
    out["ok"]=bool(out["rows"])
    return out

# ===== オッズ =====
async def fetch_odds3t(jcd:str, rno:int, hd:str)->Dict[str,float]:
    url = url_odds3t(jcd, rno, hd)
    out: Dict[str,float] = {}
    try:
        html = await get_html(url)
        soup = BeautifulSoup(html, "lxml")
        tx = _norm(soup.get_text(" "))
        for m in re.finditer(r"([1-6])[-ｰ]([1-6])[-ｰ]([1-6])\s+(\d+\.\d)", tx):
            a,b,c,od = int(m.group(1)), int(m.group(2)), int(m.group(3)), float(m.group(4))
            if len({a,b,c})==3:
                out[f"{a}{b}{c}"]=od
    except Exception as e:
        log.warning("odds fetch fail: %s", e)
    return out

# ===== スコアリング／バイアス =====
def wind_bias(before:Dict[str,Any], lane:int)->float:
    # 向かい=内+、追い=外+、横=センター+、強風/高波でダッシュ寄り加点
    wx = before.get("weather",{})
    wdir = wx.get("wind_dir","")
    w    = _to_f(wx.get("wind_m"), 0.0)
    wave = _to_f(wx.get("wave_cm"), 0.0)
    b = 0.0
    if wdir.startswith("向かい"):
        if lane<=3: b += 2.0
    elif wdir.startswith("追い"):
        if lane>=4: b += 2.0
    elif "横風" in wdir:
        if 2<=lane<=5: b += 1.0
    if (w>=5 or wave>=5) and lane>=4:
        b += 1.5
    return b

def score_lane(lane:int, sh:Dict[int,dict], before:Dict[str,Any])->float:
    row = sh.get(lane, {})
    ex  = before.get("ex", {}).get(lane)
    st  = before.get("st", {}).get(lane)
    tilt = before.get("tilt", {}).get(lane)

    base = {1:60,2:48,3:44,4:40,5:34,6:30}[lane]
    if ex: base += max(0, (6.60 - _to_f(ex))*20)
    if st: base += max(0, (0.15 - _to_f(st))*200)

    base += _to_f(row.get("motor_2r"),0)*0.6
    base += _to_f(row.get("boat_2r"),0)*0.3
    base += _to_f(row.get("z_win"),0)*0.4
    base += _to_f(row.get("t_win"),0)*0.2
    base -= max(0, (_to_f(row.get("avg_st"),0)-0.16))*300  # ST遅い減点

    cls = row.get("class","")
    if cls=="A1": base += 8
    elif cls=="A2": base += 4
    elif cls=="B1": base -= 2
    else: base -= 5

    if tilt:
        t = _to_f(tilt, 0.0)
        base += t * (2.0 if lane>=4 else 0.5)

    base += wind_bias(before, lane)
    return base

def build_cards(sh_rows:Dict[int,dict], before:Dict[str,Any], odds:Dict[str,float]|None):
    scores = {i: score_lane(i, sh_rows, before) for i in range(1,7)}
    order = sorted(scores, key=lambda k: scores[k], reverse=True)
    axis = order[0]
    cand = order[1:4]
    others = order[4:6]

    # 本線
    mains=[]
    for b in cand:
        for c in cand:
            if b==c: continue
            mains.append((axis,b,c))
    for b in cand:
        for c in others:
            if len(mains)>=10: break
            mains.append((axis,b,c))

    # 抑え（2着軸＆カド意識）
    subs=[]
    if axis!=4:
        subs.append((axis,4,order[0]))
    for a in cand+others:
        if a==axis: continue
        for c in cand+others:
            if c in (axis,a): continue
            subs.append((a,axis,c))
            if len(subs)>=12: break
        if len(subs)>=12: break

    # 穴（ダッシュ絡み）
    holes=[]
    dash = [i for i in order if i>=4][:3]
    for a in dash:
        for b in order[:3]:
            if a==b: continue
            holes.append((a,b,axis))
            if len(holes)>=6: break
        if len(holes)>=6: break

    # 妙味：指数÷オッズ
    values=[]
    stakes={}
    if odds:
        def key_score(t):
            return scores[t[0]]*0.55 + scores[t[1]]*0.30 + scores[t[2]]*0.15
        uniq = list(set(mains+subs+holes))
        pairs = []
        for t in uniq:
            k = f"{t[0]}{t[1]}{t[2]}"
            od = odds.get(k)
            if not od: continue
            val = key_score(t) / od
            pairs.append((val, t, od))
        pairs.sort(key=lambda x: x[0], reverse=True)
        values = [p[1] for p in pairs[:5]]

        # 資金配分（デフォ1000円を確率×逆オッズで配分、100円単位）
        budget = 1000
        buckets = mains + subs + holes
        wts, ks, ods = [], [], []
        for t in buckets:
            k = f"{t[0]}{t[1]}{t[2]}"
            od = odds.get(k, 0.0)
            score = key_score(t)
            if od<=0: 
                continue
            ks.append(k); ods.append(od)
            wts.append(max(0.0001, score/(od**0.6)))
        s = sum(wts) if wts else 1.0
        for k, w in zip(ks, wts):
            bet = int(round(budget * (w/s) / 100.0))*100
            if bet>0: stakes[k]=bet

    # 展開メモ
    memo=[]
    ex = before.get("ex",{})
    st = before.get("st",{})
    if ex:
        ex_top = sorted(ex, key=lambda i: _to_f(ex[i],9.99))[:3]
        memo.append("展示上位: " + "-".join(map(str,ex_top)))
    if st:
        st_top = sorted(st, key=lambda i: _to_f(st[i],9.99))[:3]
        memo.append("ST上位: " + "-".join(map(str,st_top)))

    wx = before.get("weather",{})
    if wx:
        memo.append("天候:{0} 風{1}m({2}) 波{3}cm".format(wx.get("weather","?"),
                    wx.get("wind_m","?"), wx.get("wind_dir","?"), wx.get("wave_cm","?")))
    parts = before.get("parts",{}).get("note")
    if parts: memo.append(parts)

    mot_rank = sorted(sh_rows, key=lambda i: _to_f(sh_rows[i].get("motor_2r")), reverse=True)
    memo.append("機力上位: " + "-".join(map(str,mot_rank[:3])))

    memo.append("軸は{0}号艇、対抗{1}・単穴{2}想定。".format(axis, cand[0], cand[1] if len(cand)>1 else cand[0]))

    return mains[:10], subs[:12], holes[:6], values, memo, stakes

def fmt_cards(label:str, picks:List[Tuple[int,int,int]], stakes:Dict[str,int]|None=None)->str:
    if not picks: return "{0}(0点)\n（データ不足）".format(label)
    rows=[]
    for a,b,c in picks:
        k = "{0}{1}{2}".format(a,b,c)
        bet = stakes.get(k) if stakes else None
        rows.append( "{0}.{1}.{2}{3}".format(a,b,c, f" [{bet}円]" if bet else "") )
    return "{0}({1}点)\n{2}".format(label, len(picks), "\n".join(rows))

def build_reply(title:str, url:str, mains, subs, holes, values, memo, stakes)->str:
    lines = [
        "📍 {0}".format(title),
        "――――――――――",
        fmt_cards("本線", mains, stakes),
        "",
        fmt_cards("抑え", subs, stakes),
        "",
        fmt_cards("穴目", holes, stakes),
    ]
    if values:
        lines += ["", fmt_cards("妙味", values, stakes)]
    lines += ["", "展開メモ: {0}".format(" / ".join(memo)), "(src: 公式 / {0})".format(url)]
    return "\n".join(lines)

# ===== 入力解析 =====
def parse_user_text(text:str)->Dict[str,Any]:
    t = text.strip().replace("　"," ")
    ps = re.split(r"\s+", t)
    if not ps: return {}
    name = ps[0]
    rno = int(ps[1]) if len(ps)>=2 and ps[1].isdigit() else 12
    hd  = ps[2] if len(ps)>=3 and re.fullmatch(r"\d{8}", ps[2]) else datetime.now().strftime("%Y%m%d")
    if name not in JCD_BY_NAME: return {}
    return {"name":name, "jcd":JCD_BY_NAME[name], "place_no":PLACE_NO_BY_NAME[name], "rno":rno, "hd":hd}

# ===== Routes =====
@app.get("/")
def root():
    return (
        "yosou-bot v4 MAX\n"
        "例: 『常滑 6 20250812』\n"
        "debug:\n"
        "  /_debug/shutuba?jcd=08&rno=6&hd=20250812\n"
        "  /_debug/before?jcd=08&rno=6&hd=20250812\n"
        "  /_debug/odds?jcd=08&rno=6&hd=20250812\n"
    ), 200, {"Content-Type":"text/plain; charset=utf-8"}

@app.get("/_debug/before")
def dbg_before():
    jcd = request.args.get("jcd","08")
    rno = int(request.args.get("rno","6"))
    hd  = request.args.get("hd", datetime.now().strftime("%Y%m%d"))
    res = asyncio.run(fetch_beforeinfo(jcd,rno,hd))
    return str(res), 200, {"Content-Type":"text/plain; charset=utf-8"}

@app.get("/_debug/shutuba")
def dbg_shutuba():
    jcd = request.args.get("jcd","08")
    rno = int(request.args.get("rno","6"))
    hd  = request.args.get("hd", datetime.now().strftime("%Y%m%d"))
    res = asyncio.run(fetch_shutuba(jcd,rno,hd))
    return str(res), 200, {"Content-Type":"text/plain; charset=utf-8"}

@app.get("/_debug/odds")
def dbg_odds():
    jcd = request.args.get("jcd","08")
    rno = int(request.args.get("rno","6"))
    hd  = request.args.get("hd", datetime.now().strftime("%Y%m%d"))
    res = asyncio.run(fetch_odds3t(jcd,rno,hd))
    return str(res) if res else "{}", 200, {"Content-Type":"text/plain; charset=utf-8"}

# ===== LINE Webhook =====
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

    name, jcd, rno, hd = q["name"], q["jcd"], q["rno"], q["hd"]
    d = datetime.strptime(hd,"%Y%m%d").strftime("%Y/%m/%d")
    title = "{0} {1}R ({2})".format(name, rno, d)

    shutuba = asyncio.run(fetch_shutuba(jcd, rno, hd))
    before  = asyncio.run(fetch_beforeinfo(jcd, rno, hd))
    odds    = asyncio.run(fetch_odds3t(jcd, rno, hd))

    if not shutuba.get("ok") and not before.get("ok"):
        txt = "📍 {0}\n――――――――――\n直前情報の取得に失敗しました。少し待ってから再度お試しください。\n(src: 公式 / {1})".format(
            title, url_beforeinfo(jcd,rno,hd))
        _reply(event.reply_token, [TextMessage(text=txt)])
        return

    mains, subs, holes, values, memo, stakes = build_cards(shutuba.get("rows",{}), before, odds)
    txt = build_reply(title, before.get("url", url_beforeinfo(jcd,rno,hd)), mains, subs, holes, values, memo, stakes)
    _reply(event.reply_token, [TextMessage(text=txt)])

def _reply(token:str, msgs:List[TextMessage]):
    if not line_api: return
    try:
        line_api.reply_message(ReplyMessageRequest(replyToken=token, messages=msgs))
    except Exception as e:
        log.exception("line reply error: %s", e)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","10000")))
