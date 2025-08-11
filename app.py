# -*- coding: utf-8 -*-
import os, re, time, datetime as dt
from typing import Dict, List, Tuple, Optional

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import InvalidSignatureError

# ===== env =====
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    raise RuntimeError("LINE env not set")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

app = Flask(__name__)

@app.route("/")
def root(): return "ok", 200
@app.route("/health")
def health(): return "ok", 200

@app.route("/callback", methods=["POST"])
def callback():
    sig = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, sig)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ===== mapping =====
PLACE_NO = {
    "桐生":1,"戸田":2,"江戸川":3,"平和島":4,"多摩川":5,"浜名湖":6,"蒲郡":7,"常滑":8,"津":9,
    "三国":10,"びわこ":11,"琵琶湖":11,"住之江":12,"尼崎":13,"鳴門":14,"丸亀":15,"児島":16,"宮島":17,
    "徳山":18,"下関":19,"若松":20,"芦屋":21,"福岡":22,"唐津":23,"大村":24
}
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"

def parse_user_text(txt: str) -> Tuple[Optional[int], Optional[int], str]:
    s = re.sub(r"\s+"," ",txt.strip())
    if s.lower()=="help": return None,None,"help"
    m = re.match(r"^(\S+)\s+(\d{1,2})(?:\s+(\d{8}))?$", s)
    if not m: return None,None,"bad"
    place, rno, ymd = m.group(1), int(m.group(2)), m.group(3)
    if place not in PLACE_NO: return None,None,"place-unknown"
    if not ymd: ymd = dt.date.today().strftime("%Y%m%d")
    return PLACE_NO[place], rno, ymd

# ===== scraping kyoteibiyori =====
class BiyoriError(Exception): ...

def _float_or_none(v: str) -> Optional[float]:
    v = (v or "").replace("－","").replace("-","").replace("F","").strip()
    try: return float(v)
    except: return None

def fetch_biyori_before(place_no: int, race_no: int, ymd: str) -> Dict[int, Dict[str, Optional[float]]]:
    """slider=9 を最優先。展示/周回/周り足/直線/ST + 枠別 平均ST/順位 を取得。"""
    base = f"https://kyoteibiyori.com/race_shusso.php?place_no={place_no}&race_no={race_no}&hiduke={ymd}"
    urls = [
        base + "&slider=9",             # ← このページを最優先
        base + "&slider=4",
        base,
        base + "&sp=1",
        base + "&sp=1&slider=9",
        base + "&sp=1&slider=4",
    ]
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Referer":"https://kyoteibiyori.com/", "Accept-Language":"ja,en;q=0.8"})

    last_head = ""
    for url in urls:
        try:
            r = sess.get(url, timeout=10)
            if r.status_code != 200: 
                time.sleep(0.4); continue
            html = r.text
            last_head = html[:600]
            soup = BeautifulSoup(html, "lxml")

            # --- メイン直前テーブル（展示/周回/周り足/直線/ST）を探す
            table = None
            for tb in soup.select("table"):
                head = " ".join(th.get_text(strip=True) for th in tb.select("tr th"))
                if any(k in head for k in ["展示","周回","周り足","直線","ST"]):
                    table = tb; break

            data: Dict[int, Dict[str, Optional[float]]] = {}
            if table:
                rows = [tr for tr in table.select("tr") if tr.select("td")]
                lane_guess = 1
                for tr in rows:
                    tds = [td.get_text(strip=True) for td in tr.select("td")]
                    if not tds: continue
                    # 号艇推定
                    lane = None
                    for c in tds[:3]:
                        m = re.search(r"(\d)\s*号", c)
                        if m: lane = int(m.group(1)); break
                    if lane is None and tds[0].isdigit(): lane = int(tds[0])
                    if lane is None: lane = lane_guess
                    lane_guess += 1
                    # 数値だけ拾って順に割当
                    nums = [x for x in tds if re.search(r"\d", x)]
                    tenji  = _float_or_none(nums[0]) if len(nums)>0 else None
                    shukai = _float_or_none(nums[1]) if len(nums)>1 else None
                    mawari = _float_or_none(nums[2]) if len(nums)>2 else None
                    choku  = _float_or_none(nums[3]) if len(nums)>3 else None
                    st     = _float_or_none(nums[4]) if len(nums)>4 else None
                    data[lane] = {"tenji":tenji,"shukai":shukai,"mawari":mawari,"chokusen":choku,"st":st}

                # --- 枠別情報（平均ST/順位）も拾う（あれば）
                sub = None
                for tb in soup.select("table"):
                    head = " ".join(th.get_text(strip=True) for th in tb.select("tr th"))
                    if "平均ST" in head and "ST順位" in head:
                        sub = tb; break
                if sub:
                    # 直近6か月の行を探す
                    for tr in sub.select("tr"):
                        tds = [td.get_text(strip=True) for td in tr.select("td")]
                        if not tds: continue
                        if "直近6ヶ月" in "".join(tds) or re.search(r"直近.?6", "".join(tds)):
                            # 次の行にST順位が来る形もあるため、この行と次行の両方を見る
                            st_vals = [ _float_or_none(x) for x in tds if re.search(r"\d", x) ][:6]
                            next_tr = tr.find_next_sibling("tr")
                            rank_vals = []
                            if next_tr:
                                rank_vals = [ _float_or_none(x) for x in [td.get_text(strip=True) for td in next_tr.select("td")] ][:6]
                            for i in range(6):
                                lane = i+1
                                if lane not in data: data[lane] = {}
                                if i < len(st_vals) and st_vals[i] is not None:
                                    data[lane]["st_avg6"] = st_vals[i]
                                if i < len(rank_vals) and rank_vals[i] is not None:
                                    data[lane]["st_rank6"] = rank_vals[i]
                            break

                if len(data)>=3:
                    return data

            time.sleep(0.4)
        except requests.RequestException:
            time.sleep(0.5)
            continue

    raise BiyoriError(f"table-not-found url_tried={len(urls)} head={last_head}")

# ===== 簡易予想 =====
def build_forecast(b: Dict[int, Dict[str, Optional[float]]]) -> Tuple[str, List[str], List[str], List[str]]:
    lanes = sorted(b.keys())
    if not lanes: return "直前データ不足。", [], [], []

    def nz(x, d): return x if isinstance(x,(int,float)) else d

    tenji_min = min(nz(b[i].get("tenji"), 999) for i in lanes)
    st_min    = min(nz(b[i].get("st"), 999) for i in lanes)
    choku_max = max(nz(b[i].get("chokusen"), 0) for i in lanes)
    mawa_max  = max(nz(b[i].get("mawari"), 0) for i in lanes)

    scores = {}
    for i in lanes:
        tenji  = nz(b[i].get("tenji"), tenji_min)
        st     = nz(b[i].get("st"), st_min)
        st_avg = nz(b[i].get("st_avg6"), st)  # 枠別平均があれば優先
        choku  = nz(b[i].get("chokusen"), choku_max)
        mawari = nz(b[i].get("mawari"), mawa_max)

        s_tenji = (tenji_min/max(tenji,0.01))*35
        s_st    = (st_min/max(st_avg,0.01))*25
        s_choku = (choku/max(choku_max,0.01))*20
        s_mawa  = (mawari/max(mawa_max,0.01))*20
        scores[i] = s_tenji+s_st+s_choku+s_mawa

    order = sorted(scores, key=lambda k: scores[k], reverse=True)
    a,b2,c = order[0], order[1], order[2] if len(order)>2 else order[0]

    com = []
    if a==1: com.append("①の逃げ本線。")
    else: com.append(f"{a}コースの機力上位。")
    if st_min<999 and b.get(1,{}).get("st")==st_min: com.append("①のST反応良。")
    if choku_max and any(nz(b[i].get('chokusen'),0)>=choku_max*0.98 for i in lanes if i!=a):
        com.append("直線互角で混戦気配。")
    comment = " ".join(com)

    hon  = [f"{a}-{b2}-{c}", f"{a}-{c}-{b2}"]
    osa  = [f"{a}-1-{b2}", f"1-{a}-{b2}"] if a!=1 and 1 in lanes else [f"{a}-{b2}-1", f"{a}-1-{b2}"]
    nerai= [f"{b2}-{a}-{c}", f"{a}-{c}-1"]
    return comment, hon, osa, nerai

def make_reply(place_no: int, race_no: int, ymd: str) -> str:
    place = next(k for k,v in PLACE_NO.items() if v==place_no)
    head = f"📍 {place} {race_no}R（{ymd[:4]}/{ymd[4:6]}/{ymd[6:8]}）\n" + "—"*18 + "\n"
    src = f"https://kyoteibiyori.com/race_shusso.php?place_no={place_no}&race_no={race_no}&hiduke={ymd}&slider=9"
    try:
        bj = fetch_biyori_before(place_no, race_no, ymd)
    except BiyoriError:
        return head + "直前情報の取得に失敗しました。少し待ってから再度お試しください。\n" + f"(src: {src})"
    comment, hon, osa, nerai = build_forecast(bj)
    out = [head, f"🧭 展開予想：{comment}\n",
           "🎯 本線　： " + ", ".join(hon),
           "🛡️ 抑え　： " + ", ".join(osa),
           "💥 狙い　： " + ", ".join(nerai),
           f"\n(日和: {src})"]
    return "\n".join(out)

HELP = "入力例：『唐津 12』 / 『丸亀 8 20250811』 / 『help』\n直前情報はボートレース日和 slider=9 を優先して取得します。"

@handler.add(MessageEvent, message=TextMessage)
def on_text(event: MessageEvent):
    text = (event.message.text or "").strip()
    place_no, rno, mode = parse_user_text(text)
    if mode=="help": line_bot_api.reply_message(event.reply_token, TextSendMessage(HELP)); return
    if mode=="bad": line_bot_api.reply_message(event.reply_token, TextSendMessage("入力例：『丸亀 8』 / 『丸亀 8 20250811』")); return
    if mode=="place-unknown": line_bot_api.reply_message(event.reply_token, TextSendMessage("場名が見つかりません。例：唐津, 丸亀, 住之江 など")); return
    try:
        msg = make_reply(place_no, rno, mode)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg))
    except Exception:
        line_bot_api.reply_message(event.reply_token, TextSendMessage("エラー。時間をおいて再度どうぞ。"))

app.app_context().push()
