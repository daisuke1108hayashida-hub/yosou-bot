import os
import re
import logging
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# ===== 基本設定 =====
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("yosou-bot")

CH_SECRET = os.getenv("LINE_CHANNEL_SECRET")
CH_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
if not CH_SECRET or not CH_TOKEN:
    raise RuntimeError("LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN が未設定")

line_bot_api = LineBotApi(CH_TOKEN)
handler = WebhookHandler(CH_SECRET)

app = Flask(__name__)

# 出す点数
MAX_MAIN = 8
MAX_COVER = 6
MAX_ATTACK = 6

# 場コード（日和 place_no）
PLACE = {
    "桐生":1,"戸田":2,"江戸川":3,"平和島":4,"多摩川":5,"浜名湖":6,"蒲郡":7,"常滑":8,"津":9,
    "三国":10,"びわこ":11,"住之江":12,"尼崎":13,"鳴門":14,"丸亀":15,"児島":16,"宮島":17,
    "徳山":18,"下関":19,"若松":20,"芦屋":21,"福岡":22,"唐津":23,"大村":24
}

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

# ---------- HTTP ----------
def new_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ja,en;q=0.8",
        "Cache-Control": "no-cache",
        "Referer": "https://kyoteibiyori.com/",
    })
    return s

# ---------- Utilities ----------
def reply(event, text):
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))

def ymd_today():
    return datetime.now().strftime("%Y%m%d")

def ymd_fmt(yyyymmdd):
    try:
        return datetime.strptime(yyyymmdd, "%Y%m%d").strftime("%Y/%m/%d")
    except Exception:
        return yyyymmdd

def biyori_url(place_no, race_no, yyyymmdd, slider):
    return (f"https://kyoteibiyori.com/race_shusso.php"
            f"?place_no={place_no}&race_no={race_no}&hiduke={yyyymmdd}&slider={slider}")

def official_url(jcd, rno, yyyymmdd):
    # 公式 直前（フォールバック用）
    return (f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?"
            f"rno={rno}&jcd={jcd}&hd={yyyymmdd}")

# ---------- Parsing helpers ----------
class TableNotFound(Exception): ...
class NoData(Exception): ...

KEYS = ["展示", "周回", "周り足", "直線", "ST", "平均ST"]

def extract_table_rows(soup: BeautifulSoup):
    """<table> ベースのテーブルから行配列を返す"""
    tables = soup.find_all("table")
    if not tables:
        raise TableNotFound("no <table>")
    def score(tbl):
        text = tbl.get_text(" ", strip=True)
        key_hit = sum(k in text for k in KEYS)
        # 列数（6艇＋ラベル）を期待
        max_cols = 0
        for tr in tbl.find_all("tr"):
            max_cols = max(max_cols, len(tr.find_all(["th","td"])))
        return key_hit*10 + (1 if max_cols >= 7 else 0)
    tables.sort(key=score, reverse=True)
    best = tables[0]
    if score(best) == 0:
        raise TableNotFound("keyword not found in tables")
    rows = []
    for tr in best.find_all("tr"):
        cols = [c.get_text(strip=True) for c in tr.find_all(["th","td"])]
        if cols:
            rows.append(cols)
    return rows

def extract_div_grid(soup: BeautifulSoup):
    """スマホ向け DIV グリッドから ‘ラベル＋6艇値’ を推定抽出"""
    text = soup.get_text("\n", strip=True)
    # 1行にまとまっているケースにも対応してラベルごとに数値を収集
    rows = []
    for k in KEYS:
        # ラベルの近辺から数字を6個拾う
        m = re.search(re.escape(k) + r".{0,40}", text)
        if not m:
            continue
        tail = text[m.end():m.end()+400]
        nums = re.findall(r"\d+(?:\.\d+)?", tail)
        if len(nums) >= 6:
            rows.append([k] + nums[:6])
    if not rows:
        raise TableNotFound("div grid not found")
    return rows

def parse_metrics(rows):
    """rows -> {ラベル:[float×6]}"""
    wanted = {"展示":"展示", "展示ﾀｲﾑ":"展示", "周回":"周回", "周り足":"周り足", "ﾏﾜﾘ足":"周り足",
              "直線":"直線", "ST":"ST", "平均ST":"ST", "平均ＳＴ":"ST"}
    metrics = {}
    for row in rows:
        if not row: continue
        label_raw = str(row[0])
        label = None
        for k,v in wanted.items():
            if k in label_raw:
                label = v; break
        if not label: continue
        vals = []
        for v in row[1:7]:
            try:
                vv = float(re.findall(r"-?\d+(?:\.\d+)?", str(v))[0])
            except:
                vv = None
            vals.append(vv)
        while len(vals) < 6: vals.append(None)
        metrics[label] = vals[:6]
    if not metrics:
        raise NoData("metrics empty")
    return metrics

def rank_from_numbers(vals, reverse=False):
    if not vals: return [None]*6
    pairs=[]
    for i,v in enumerate(vals):
        if v is None: pairs.append((9999 if not reverse else -9999, i))
        else: pairs.append((v,i))
    pairs.sort(key=lambda x:x[0], reverse=reverse)
    ranks=[0]*6
    for r,(_,idx) in enumerate(pairs, start=1): ranks[idx]=r
    return ranks

def analyze(metrics):
    # 直前重視
    weights = {"展示":0.35, "周回":0.30, "直線":0.25, "ST":0.10}
    rk_ex = rank_from_numbers(metrics.get("展示"), False)
    rk_lp = rank_from_numbers(metrics.get("周回"), False)
    rk_ln = rank_from_numbers(metrics.get("直線"), True)
    rk_st = rank_from_numbers(metrics.get("ST"), False)
    score=[0]*6
    for i in range(6):
        for lb,rk in [("展示",rk_ex),("周回",rk_lp),("直線",rk_ln),("ST",rk_st)]:
            if rk[i]: score[i] += (7-rk[i]) * weights[lb]
    order = sorted(range(6), key=lambda i: score[i], reverse=True)
    axis = order[0]+1
    scenario = "①逃げ本線" if axis==1 else f"{axis}コース中心の攻め"
    reason = f"展示/周回/直線/ST 総合評価で {axis}号艇が最上位"
    return {"axis":axis, "order":[i+1 for i in order], "scenario":scenario, "reason":reason}

def mk_trio(a,b,c): return f"{a}-{b}-{c}"

def build_tickets(ana):
    axis = ana["axis"]
    others = [x for x in ana["order"] if x!=axis]
    top4 = others[:4]
    main=[]; cover=[]; attack=[]

    # 本線：軸→上位相手流し（順序付）
    for i in range(min(4,len(others))):
        for j in range(min(4,len(others))):
            if i==j: continue
            main.append(mk_trio(axis, others[i], others[j]))
    # 抑え：相手軸のパターン
    for i in range(min(3,len(others))):
        for j in range(min(3,len(others))):
            if i==j: continue
            cover.append(mk_trio(others[i], axis, others[j]))
    # 狙い：深い相手・捲り差し目
    if len(top4)>=4:
        attack += [mk_trio(axis, top4[3], top4[0]), mk_trio(axis, top4[3], top4[1])]
        attack += [mk_trio(top4[0], top4[2], axis), mk_trio(top4[1], top4[2], axis)]

    # 重複除去と上限
    def dedup(lst):
        out=[]
        for x in lst:
            if x not in out: out.append(x)
        return out
    main = dedup(main)[:MAX_MAIN]
    cover = [x for x in dedup(cover) if x not in main][:MAX_COVER]
    attack = [x for x in dedup(attack) if x not in main+cover][:MAX_ATTACK]
    return {"main":main,"cover":cover,"attack":attack}

# ---------- Fetch logic ----------
def fetch_from_biyori(place_no, race_no, yyyymmdd):
    s = new_session()
    # 軽くトップを踏んでCookie確保
    try:
        s.get("https://kyoteibiyori.com/", timeout=10)
    except Exception:
        pass

    tried = []
    for slider in (4, 9):  # 直前 → MyData
        url = biyori_url(place_no, race_no, yyyymmdd, slider)
        tried.append(url)
        try:
            r = s.get(url, timeout=12)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "lxml")
            # まず table、ダメなら div グリッド
            try:
                rows = extract_table_rows(soup)
            except TableNotFound:
                rows = extract_div_grid(soup)
            metrics = parse_metrics(rows)
            return metrics, url, tried
        except (TableNotFound, NoData):
            log.warning("yosou-bot[biyori]: table not found (slider=%s) url=%s", slider, url)
            continue
        except Exception as e:
            log.warning("yosou-bot[biyori]: request error %s url=%s", e, url)
            continue
    return None, None, tried

# ---------- Routes ----------
@app.route("/")
def root(): return "ok", 200

@app.route("/health")
def health(): return "ok", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ---------- LINE handler ----------
@handler.add(MessageEvent, message=TextMessage)
def on_text(event: MessageEvent):
    text = event.message.text.strip()

    if text.lower() in ("help","使い方","？"):
        reply(event,
              "使い方：『丸亀 8』／『丸亀 8 20250811』\n"
              "※日和優先（直前→MyData）。取れない場合は公式に自動フォールバックします。")
        return

    m = re.match(r"^\s*([^\s\d]+)\s+(\d{1,2})(?:\s+(\d{8}))?\s*$", text)
    if not m:
        reply(event, "入力例：『丸亀 8』 / 『丸亀 8 20250811』 / 『help』")
        return

    place_name = m.group(1)
    rno = int(m.group(2))
    ymd = m.group(3) or ymd_today()

    if place_name not in PLACE:
        reply(event, f"場名が分かりません：{place_name}")
        return

    place_no = PLACE[place_name]
    header = f"📍 {place_name} {rno}R ({ymd_fmt(ymd)})\n" + "─"*22

    # 日和
    metrics, used_url, tried = fetch_from_biyori(place_no, rno, ymd)

    # フォールバック（公式の簡易STのみ）※最後の手段
    if metrics is None:
        # 公式コード(jcd)は place_no そのまま
        off = official_url(place_no, rno, ymd)
        msg = (f"{header}\n直前情報の取得に失敗しました。\n"
               f"試行URL:\n- " + "\n- ".join(tried) + f"\n\n（参考）公式：{off}")
        reply(event, msg)
        return

    ana = analyze(metrics)
    tks = build_tickets(ana)

    msg = (
        f"{header}\n"
        f"🧭 展開予想：{ana['scenario']}\n"
        f"🧩 根拠：{ana['reason']}\n"
        + "─"*22 + "\n\n"
        f"🎯 本線：{', '.join(tks['main'])}\n"
        f"🛡️ 抑え：{', '.join(tks['cover'])}\n"
        f"💥 狙い：{', '.join(tks['attack'])}\n"
        f"\n(src: 日和 / {used_url})"
    )
    reply(event, msg)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
