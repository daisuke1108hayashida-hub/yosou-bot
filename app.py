# -*- coding: utf-8 -*-
import os, re, time, datetime as dt
from typing import Dict, Optional, Tuple, List
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import InvalidSignatureError

# ===== LINE =====
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINE env not set")
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

app = Flask(__name__)
@app.route("/")      ; 
def root(): return "ok",200
@app.route("/health");
def health(): return "ok",200
@app.route("/callback", methods=["POST"])
def callback():
    sig = request.headers.get("X-Line-Signature","")
    body = request.get_data(as_text=True)
    try: handler.handle(body, sig)
    except InvalidSignatureError: abort(400)
    return "OK"

# ====== 場番号 ======
PLACE_NO = {"桐生":1,"戸田":2,"江戸川":3,"平和島":4,"多摩川":5,"浜名湖":6,"蒲郡":7,"常滑":8,"津":9,"三国":10,"びわこ":11,
            "住之江":12,"尼崎":13,"鳴門":14,"丸亀":15,"児島":16,"宮島":17,"徳山":18,"下関":19,"若松":20,"芦屋":21,"福岡":22,"唐津":23,"大村":24}

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

def parse_user_text(s: str) -> Tuple[Optional[int], Optional[int], str]:
    s = re.sub(r"\s+"," ", (s or "").strip())
    if s.lower()=="help": return None, None, "help"
    m = re.match(r"^(\S+)\s+(\d{1,2})(?:\s+(\d{8}))?$", s)
    if not m: return None, None, "bad"
    place, rno, ymd = m.group(1), int(m.group(2)), m.group(3)
    if place not in PLACE_NO: return None, None, "place-unknown"
    if not ymd: ymd = dt.date.today().strftime("%Y%m%d")
    return PLACE_NO[place], rno, ymd

# ===== kyoteibiyori 抽出 =====
LABEL_SYNS = {
    "展示":"tenji","展示ﾀｲﾑ":"tenji","展示タイム":"tenji",
    "周回":"shukai","回り":"mawari","周り足":"mawari","回り足":"mawari",
    "直線":"chokusen","ＳＴ":"st","ST":"st"
}
ROW_KEYS = set(["tenji","shukai","mawari","chokusen","st"])

def _clean(t: str) -> str:
    t = (t or "").replace("\u3000"," ").replace(" ", "")
    return t

def _to_num(x: str) -> Optional[float]:
    if x is None: return None
    x = x.strip().replace("－","").replace("–","")
    x = re.sub(r"^[A-Za-zＦＦ\.]*","", x)  # F.05 -> .05
    m = re.search(r"-?\d+(?:\.\d+)?", x)
    return float(m.group(0)) if m else None

def _pick_tables(soup: BeautifulSoup) -> List:
    # 直前情報の行ラベルを含むテーブルだけを候補にする
    cands = []
    for tb in soup.find_all("table"):
        row_first_cells = []
        for tr in tb.find_all("tr"):
            tds = tr.find_all(["th","td"])
            if not tds: continue
            row_first_cells.append(_clean(tds[0].get_text()))
        hit = sum(1 for txt in row_first_cells if any(_clean(k) in txt for k in LABEL_SYNS.keys()))
        if hit >= 3:        # ラベルが複数あれば直前情報テーブルとみなす
            cands.append(tb)
    return cands

def _parse_table(tb) -> Dict[int, Dict[str, Optional[float]]]:
    data: Dict[int, Dict[str, Optional[float]]] = {}
    for tr in tb.find_all("tr"):
        cells = tr.find_all(["th","td"])
        if not cells: continue
        label_raw = _clean(cells[0].get_text())
        label_key = None
        for k,v in LABEL_SYNS.items():
            if k in label_raw:
                label_key = v; break
        if not label_key: continue
        # 次の6セルが1〜6号艇（途中で広告セル等が入っても6個拾う）
        vals, cnt = [], 0
        for c in cells[1:]:
            vals.append(c.get_text(strip=True))
            cnt += 1
            if cnt==6: break
        if len(vals) < 6:  # 途中で列が足りない場合はスキップ
            continue
        for lane in range(1,7):
            if lane not in data: data[lane] = {}
            data[lane][label_key] = _to_num(vals[lane-1])
    # 妥当性チェック：最低2種類以上拾えていること
    ok = sum(1 for k in data.get(1,{}).keys() if k in ROW_KEYS)
    return data if ok >= 2 else {}

class BiyoriError(Exception): ...

def fetch_biyori(place_no: int, race_no: int, ymd: str) -> Dict[int, Dict[str, Optional[float]]]:
    base = f"https://kyoteibiyori.com/race_shusso.php?place_no={place_no}&race_no={race_no}&hiduke={ymd}"
    urls = [base+"&slider=9", base+"&slider=4", base, base+"&sp=1", base+"&sp=1&slider=9", base+"&sp=1&slider=4"]
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Referer": "https://kyoteibiyori.com/",
        "Accept-Language":"ja-JP,ja;q=0.9"
    })
    for url in urls:
        try:
            r = sess.get(url, timeout=10)
            if r.status_code != 200 or not r.text: 
                continue
            soup = BeautifulSoup(r.text, "lxml")
            for tb in _pick_tables(soup):
                parsed = _parse_table(tb)
                if parsed: 
                    return parsed
        except requests.RequestException:
            time.sleep(0.4)
            continue
    raise BiyoriError("table-not-found")

# ===== 予想（簡易） =====
def _nz(x, d): return x if isinstance(x,(int,float)) else d

def build_forecast(b: Dict[int, Dict[str, Optional[float]]]):
    lanes = sorted(b.keys())
    tenji_min = min(_nz(b[i].get("tenji"), 999) for i in lanes)
    st_min    = min(_nz(b[i].get("st"), 999)    for i in lanes)
    choku_max = max(_nz(b[i].get("chokusen"),0) for i in lanes)
    mawa_max  = max(_nz(b[i].get("mawari"),0)   for i in lanes)

    score = {}
    for i in lanes:
        tenji  = _nz(b[i].get("tenji"), tenji_min)
        st     = _nz(b[i].get("st"), st_min)
        choku  = _nz(b[i].get("chokusen"), choku_max)
        mawari = _nz(b[i].get("mawari"), mawa_max)
        s = (tenji_min/max(tenji,0.01))*35 + (st_min/max(st,0.01))*25 \
            + (choku/max(choku_max,0.01))*20 + (mawari/max(mawa_max,0.01))*20
        score[i] = s
    order = sorted(score, key=lambda k: score[k], reverse=True)
    a,b2,c = order[0], order[1], (order[2] if len(order)>2 else order[0])

    comment = "①の逃げ本線。" if a==1 else f"{a}コース機力上位。"
    if choku_max and _nz(b.get(a,{}).get("chokusen"),0)>=choku_max*0.98:
        comment += " 直線も良好。"

    hon   = [f"{a}-{b2}-{c}", f"{a}-{c}-{b2}"]
    osa   = [f"{a}-1-{b2}", f"1-{a}-{b2}"] if a!=1 and 1 in lanes else [f"{a}-{b2}-1"]
    nerai = [f"{b2}-{a}-{c}", f"{a}-{c}-1"]
    return comment, hon, osa, nerai

def make_reply(place_no: int, race_no: int, ymd: str) -> str:
    place = next(k for k,v in PLACE_NO.items() if v==place_no)
    head = f"📍 {place} {race_no}R（{ymd[:4]}/{ymd[4:6]}/{ymd[6:8]}）\n" + "—"*18 + "\n"
    src = f"https://kyoteibiyori.com/race_shusso.php?place_no={place_no}&race_no={race_no}&hiduke={ymd}&slider=9"
    try:
        bj = fetch_biyori(place_no, race_no, ymd)
    except BiyoriError:
        return head + "直前情報の取得に失敗しました。少し待ってから再度お試しください。\n" + f"(src: {src})"
    comment, hon, osa, nerai = build_forecast(bj)
    return "\n".join([
        head,
        f"🧭 展開予想：{comment}\n",
        "🎯 本線　： " + ", ".join(hon),
        "🛡️ 抑え　： " + ", ".join(osa),
        "💥 狙い　： " + ", ".join(nerai),
        f"\n（日和優先：{src}）"
    ])

HELP = "入力例：『丸亀 5』 / 『丸亀 5 20250811』 / 『help』\n直前情報はボートレース日和（slider=9→4→通常→sp版）の順で取得します。"

@handler.add(MessageEvent, message=TextMessage)
def on_text(event: MessageEvent):
    place_no, rno, mode = parse_user_text(event.message.text)
    if mode=="help":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(HELP)); return
    if mode=="bad":
        line_bot_api.reply_message(event.reply_token, TextSendMessage("入力例：『丸亀 5』 / 『丸亀 5 20250811』")); return
    if mode=="place-unknown":
        line_bot_api.reply_message(event.reply_token, TextSendMessage("場名が見つかりません。例：唐津, 丸亀, 住之江 など")); return
    try:
        msg = make_reply(place_no, rno, mode)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg))
    except Exception:
        line_bot_api.reply_message(event.reply_token, TextSendMessage("エラー。時間をおいて再度どうぞ。"))

app.app_context().push()
