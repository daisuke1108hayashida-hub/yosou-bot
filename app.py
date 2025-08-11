# app.py
import os, re, json, math, time, datetime as dt
from datetime import timezone, timedelta
from collections import defaultdict

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# ====== 環境変数 ======
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("環境変数が未設定: LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ====== Flask ======
app = Flask(__name__)

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

# ====== 共通 ======
JST = timezone(timedelta(hours=9))
UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (YosouBot/1.0; +https://example.com)",
    "Accept-Language": "ja,en;q=0.9",
    "Referer": "https://kyoteibiyori.com/",
}
BIYORI_URL_TEMPLATE = os.getenv(
    "BIYORI_URL_TEMPLATE",
    "https://kyoteibiyori.com/race?jcd={jcd}&hd={date}&rno={rno}#preinfo"
)

# 競艇場 → JCD
JCD = {
    "桐生":"01","戸田":"02","江戸川":"03","平和島":"04","多摩川":"05",
    "浜名湖":"06","蒲郡":"07","常滑":"08","津":"09","三国":"10",
    "びわこ":"11","住之江":"12","尼崎":"13","鳴門":"14","丸亀":"15",
    "児島":"16","宮島":"17","徳山":"18","下関":"19","若松":"20",
    "芦屋":"21","福岡":"22","唐津":"23","大村":"24"
}
PLACE_ALIAS = {
    "はまなこ":"浜名湖","はまな湖":"浜名湖","常滑":"常滑","とこなめ":"常滑",
    "からつ":"唐津","まるがめ":"丸亀","からつ競艇":"唐津","丸亀競艇":"丸亀",
    "住之江競艇":"住之江","鳴門競艇":"鳴門","児島競艇":"児島"
}

def norm_place(s: str) -> str:
    s = s.strip()
    s = s.replace("競艇","").replace("ボートレース","").replace("場","")
    if s in PLACE_ALIAS: s = PLACE_ALIAS[s]
    return s

DATE_RE = re.compile(r"\b(20\d{6})\b")
INPUT_RE = re.compile(r"^\s*(\S+)\s+(\d{1,2})(?:\s+(20\d{6}))?\s*$")

def parse_input(text: str):
    """『丸亀 8 20250811』/『丸亀 8』 を解析。返り値: (place, rno:int, yyyymmdd:str)"""
    m = INPUT_RE.match(text)
    if not m: return None, None, None
    place = norm_place(m.group(1))
    try:
        rno = int(m.group(2))
    except:
        rno = None
    ymd = m.group(3)
    if not ymd:
        today = dt.datetime.now(JST).strftime("%Y%m%d")
        ymd = today
    return place, rno, ymd

def build_biyori_url(place: str, rno: int, ymd: str) -> str:
    jcd = JCD.get(place)
    if not jcd: return ""
    return BIYORI_URL_TEMPLATE.format(jcd=jcd, rno=rno, date=ymd)

def cache_get(key): return None
def cache_set(key, val, ttl=180): return

# ====== kyoteibiyori 直前情報 ======
def fetch_biyori_preinfo(url: str) -> str:
    sess = requests.Session()
    sess.headers.update(UA_HEADERS)

    try_urls = [url, url.split("#")[0]]
    for u in try_urls:
        r = sess.get(u, timeout=20, allow_redirects=True)
        if r.status_code == 200 and ("直前" in r.text or "展示" in r.text or "周回" in r.text):
            return r.text
    raise RuntimeError("直前ページの取得に失敗")

NUM = re.compile(r"[0-9]+\.[0-9]+|[0-9]+")

def _num(x):
    if x is None: return None
    x = str(x).strip()
    if not x: return None
    # ST 例: F.05, .05, 0.05
    x = x.replace("F.","").replace("F", "")
    m = NUM.search(x)
    return float(m.group()) if m else None

def parse_biyori_table(html: str):
    """
    直前タブの表をざっくり抽出。
    返り: [{lane, name, show, lap, mawari, straight, st}, ...]
    値が無い時は None
    """
    soup = BeautifulSoup(html, "html.parser")
    # 「直前情報」タブ配下の最初の table を狙う
    tables = soup.find_all("table")
    if not tables: return []
    cand = None
    for t in tables:
        txt = t.get_text(" ", strip=True)
        if ("直前" in txt or "展示" in txt) and any(col in txt for col in ["展示","周回","直線","ST"]):
            cand = t; break
    if cand is None:
        # それでも見つからない時は最初の大きめテーブル
        cand = tables[0]

    # ヘッダ列のインデックス推定
    headers = [th.get_text(strip=True) for th in cand.find_all("th")]
    head_row = None
    for tr in cand.find_all("tr"):
        ths = [th.get_text(strip=True) for th in tr.find_all("th")]
        if ths: head_row = ths; break
    cols = {"展示":-1,"周回":-1,"周り足":-1,"直線":-1,"ST":-1,"選手":-1,"進入":-1}
    if head_row:
        for i,h in enumerate(head_row):
            for k in list(cols.keys()):
                if k in h and cols[k] == -1:
                    cols[k] = i

    rows = []
    for tr in cand.find_all("tr"):
        tds = [td.get_text(strip=True) for td in tr.find_all(["td"])]
        if not tds: continue
        # レーン判定（1〜6がどこかに含まれている/進入列）
        lane = None
        if cols["進入"] >=0 and cols["進入"] < len(tds):
            lane = _num(tds[cols["進入"]])
        if lane is None:
            # 左端に 1〜6 らしき表示が来るケース
            lane = _num(tds[0])
        if lane is None or not (1 <= int(lane) <= 6):
            continue

        name = None
        if cols["選手"] >=0 and cols["選手"]<len(tds):
            name = tds[cols["選手"]]

        show = _num(tds[cols["展示"]]) if cols["展示"]>=0 and cols["展示"]<len(tds) else None
        lap  = _num(tds[cols["周回"]]) if cols["周回"] >=0 and cols["周回"] <len(tds) else None
        mawa = _num(tds[cols["周り足"]]) if cols["周り足"]>=0 and cols["周り足"]<len(tds) else None
        stra = _num(tds[cols["直線"]]) if cols["直線"] >=0 and cols["直線"] <len(tds) else None
        st   = _num(tds[cols["ST"]]) if cols["ST"]    >=0 and cols["ST"]    <len(tds) else None

        rows.append({
            "lane": int(lane), "name": name or "",
            "show": show, "lap": lap, "mawari": mawa, "straight": stra, "st": st
        })
    rows.sort(key=lambda x:x["lane"])
    return rows

def _scale_desc(arr):  # 小さいほど良い → 点数大
    xs = [a for a in arr if a is not None]
    if not xs: return {i:0 for i in range(1,7)}
    mn, mx = min(xs), max(xs)
    res = {}
    for lane,val in enumerate(arr, start=1):
        if val is None: res[lane]=0
        else:
            res[lane] = 1.0 if mx==mn else (mx-val)/(mx-mn)
    return res

def _scale_asc(arr):   # 大きいほど良い → 点数大
    xs = [a for a in arr if a is not None]
    if not xs: return {i:0 for i in range(1,7)}
    mn, mx = min(xs), max(xs)
    res = {}
    for lane,val in enumerate(arr, start=1):
        if val is None: res[lane]=0
        else:
            res[lane] = 1.0 if mx==mn else (val-mn)/(mx-mn)
    return res

def build_scores(rows):
    show    = [None]*6
    lap     = [None]*6
    mawari  = [None]*6
    straight= [None]*6
    st      = [None]*6
    for r in rows:
        i = r["lane"]-1
        show[i]=r["show"]; lap[i]=r["lap"]; mawari[i]=r["mawari"]
        straight[i]=r["straight"]; st[i]=r["st"]

    s_show  = _scale_desc(show)     # 展示タイムは低いほど◎
    s_lap   = _scale_desc(lap)      # 周回は低いほど◎
    s_mawa  = _scale_asc(mawari)    # 周り足は高いほど◎（サイトの数値に依存）
    s_stra  = _scale_asc(straight)  # 直線は高いほど◎
    s_st    = _scale_desc(st)       # STは低いほど◎

    # 重み（好みで調整可）
    w = {"show":0.35,"lap":0.25,"mawari":0.15,"straight":0.15,"st":0.10}

    scores = {}
    for lane in range(1,7):
        scores[lane] = (
            s_show[lane]*w["show"] + s_lap[lane]*w["lap"] +
            s_mawa[lane]*w["mawari"] + s_stra[lane]*w["straight"] +
            s_st[lane]*w["st"]
        )
    return scores

def make_narrative(rows, scores):
    # 上位を説明（簡易）
    order = sorted(scores.items(), key=lambda x:x[1], reverse=True)
    top = [o[0] for o in order[:3]]
    basis = []
    for lane in top:
        r = rows[lane-1]
        tips = []
        if r["show"] is not None: tips.append(f"展示{r['show']:.2f}")
        if r["straight"] is not None: tips.append(f"直線{r['straight']:.2f}")
        if r["st"] is not None: tips.append(f"ST{r['st']:.2f}")
        basis.append(f"{lane}号艇（{'・'.join(tips)}）")
    txt = f"展開予想：内寄り優勢。直前指標は上位{', '.join(map(str, top))}が良好。\n根拠："
    txt += " / ".join(basis[:3])
    return txt

def make_picks(scores):
    order = [lane for lane,_ in sorted(scores.items(), key=lambda x:x[1], reverse=True)]
    # 本線：1着=上位2、2着=上位3、3着=上位4の組み合わせから重複なしで数点
    a = order[:2]; b = order[:3]; c = order[:4]
    hon = []
    for x in a:
        for y in b:
            if y==x: continue
            for z in c:
                if z==x or z==y: continue
                hon.append(f"{x}-{y}-{z}")
                if len(hon)>=4: break
            if len(hon)>=4: break
        if len(hon)>=4: break

    # 抑え：1着=上位3から、2-3着は上位4
    osa = []
    for x in order[:3]:
        for y in order[:4]:
            if y==x: continue
            for z in order[:4]:
                if z in (x,y): continue
                pair = f"{x}-{y}-{z}"
                if pair not in hon:
                    osa.append(pair)
                    if len(osa)>=3: break
            if len(osa)>=3: break
        if len(osa)>=3: break

    # 狙い：中穴（4-5位を1着に絡める）
    nerai = []
    for x in order[3:5]:
        for y in order[:3]:
            if y==x: continue
            for z in order[:4]:
                if z in (x,y): continue
                nerai.append(f"{x}-{y}-{z}")
                if len(nerai)>=2: break
            if len(nerai)>=2: break
        if len(nerai)>=2: break

    return hon, osa, nerai

def render_card(place, rno, ymd, url, rows, narrative, hon, osa, nerai):
    head = f"📍 {place} {rno}R ({ymd[:4]}/{ymd[4:6]}/{ymd[6:]})"
    line = "――――――――――――――――"
    body = [head, line, f"🧭 {narrative}", line, "———",]
    body.append(f"🎯 本線：{', '.join(hon) if hon else '—'}")
    body.append(f"🛡️ 抑え：{', '.join(osa) if osa else '—'}")
    body.append(f"💥 狙い：{', '.join(nerai) if nerai else '—'}")
    body.append(f"\n(直前情報 元: {url})")
    return "\n".join(body)

# ====== LINE handler ======
HELP_TEXT = (
"入力例：『丸亀 8』 / 『丸亀 8 20250811』\n"
"'help' で使い方を表示します。"
)

def extract_url(text:str):
    m = re.search(r"https?://\S+", text)
    return m.group(0) if m else None

@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    text = (event.message.text or "").strip()

    # ヘルプ
    if text.lower() in ("help","使い方"):
        reply(event.reply_token, HELP_TEXT); return

    # デバッグ（任意）
    if text.startswith("debug"):
        q = text.replace("debug","",1).strip()
        url = extract_url(q)
        if not url:
            place, rno, ymd = parse_input(q)
            if place and rno and place in JCD:
                url = build_biyori_url(place, rno, ymd)
        if not url:
            reply(event.reply_token, "debug 使い方: debug 丸亀 8 20250811  または  debug <kyoteibiyori URL>"); return
        try:
            s = requests.Session(); s.headers.update(UA_HEADERS)
            r = s.get(url, timeout=15, allow_redirects=True)
            reply(event.reply_token, f"URL: {url}\nstatus: {r.status_code}\nlen: {len(r.text)}")
        except Exception as e:
            reply(event.reply_token, f"取得失敗: {e}")
        return

    # kyoteibiyori のURL直貼り対応
    url = extract_url(text)
    place=rno=ymd=None
    if url and "kyoteibiyori.com" in url:
        # URLに jcd, rno, hd が入っている場合は拾う（無くてもOK）
        m_jcd = re.search(r"jcd=(\d{2})", url)
        m_rno = re.search(r"rno=(\d{1,2})", url)
        m_hd  = re.search(r"(?:hd|hiduke)=(20\d{6})", url)
        if m_jcd:
            for k,v in JCD.items():
                if v==m_jcd.group(1): place=k; break
        if m_rno: rno=int(m_rno.group(1))
        if m_hd:  ymd=m_hd.group(1)
        if not ymd: ymd = dt.datetime.now(JST).strftime("%Y%m%d")
    else:
        place, rno, ymd = parse_input(text)
        if not (place and rno and place in JCD):
            reply(event.reply_token, HELP_TEXT); return
        url = build_biyori_url(place, rno, ymd)

    try:
        html = fetch_biyori_preinfo(url)
        rows = parse_biyori_table(html)
        if len(rows) < 3:
            raise RuntimeError("直前表の解析に失敗")
        scores = build_scores(rows)
        nar = make_narrative(rows, scores)
        hon, osa, nerai = make_picks(scores)
        card = render_card(place or "—", rno or 0, ymd, url, rows, nar, hon, osa, nerai)
        reply(event.reply_token, card)
    except Exception as e:
        reply(event.reply_token, "直前情報の取得に失敗しました。少し待ってから再度お試しください。")

def reply(token, text):
    line_bot_api.reply_message(token, TextSendMessage(text=text))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), debug=False)
