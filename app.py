# app.py  ←この名前で保存
# -*- coding: utf-8 -*-
import os
import re
import math
import datetime as dt
from typing import List, Optional

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, Response

# ===== LINE Bot =====
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# ===== 環境変数 =====
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

# ===== 場コード（日和/公式 共通 01..24）=====
PLACE_MAP = {
    "桐生":1,"戸田":2,"江戸川":3,"平和島":4,"多摩川":5,"浜名湖":6,"蒲郡":7,"常滑":8,"津":9,"三国":10,
    "びわこ":11,"住之江":12,"尼崎":13,"鳴門":14,"丸亀":15,"児島":16,"宮島":17,"徳山":18,"下関":19,"若松":20,
    "芦屋":21,"福岡":22,"唐津":23,"大村":24
}
INV_PLACE = {v:k for k,v in PLACE_MAP.items()}

# ===== 買い目の最大点数 =====
MAX_MAIN   = 12   # 本線
MAX_COVER  = 8    # 抑え
MAX_ATTACK = 8    # 狙い

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HDRS = {
    "User-Agent": UA,
    "Referer": "https://kyoteibiyori.com/",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

def today_ymd():
    return (dt.datetime.utcnow() + dt.timedelta(hours=9)).strftime("%Y%m%d")

def fmt_ymd(ymd: str) -> str:
    try:
        return dt.datetime.strptime(ymd, "%Y%m%d").strftime("%Y/%m/%d")
    except Exception:
        return ymd

def parse_user_text(text: str):
    """
    例:
      丸亀 8
      丸亀 8R
      丸亀 8 20250811
    -> (place_no, race_no, ymd) or (None, None, None)
    """
    t = text.strip()
    if t.lower() in ("help","使い方","?","？"):
        return ("HELP", None, None)

    m = re.match(r"^\s*([^\s0-9]+)\s*([0-9]{1,2})[Rr]?\s*(\d{8})?\s*$", t)
    if not m:
        return (None, None, None)
    name = m.group(1)
    rno  = int(m.group(2))
    ymd  = m.group(3) or today_ymd()
    place_no = PLACE_MAP.get(name)
    if not place_no:
        return (None, None, None)
    return (place_no, rno, ymd)

# ========== 日和スクレイプ（直前 slider=4 / MyData slider=9） ==========
def biyori_url(place_no: int, race_no: int, ymd: str, slider: int, pc: bool=False):
    base = "https://kyoteibiyori.com/pc/race_shusso.php" if pc else "https://kyoteibiyori.com/race_shusso.php"
    return f"{base}?place_no={place_no}&race_no={race_no}&hiduke={ymd}&slider={slider}"

def _clean(s: str) -> str:
    return re.sub(r"\s+", "", s or "")

def _row_values(tbl, row_label: str, expected_cols=6):
    for tr in tbl.find_all("tr"):
        cells = [c.get_text(strip=True) for c in tr.find_all(["th","td"])]
        if not cells: continue
        if _clean(cells[0]).startswith(_clean(row_label)):
            vals = cells[1:1+expected_cols]
            while len(vals) < expected_cols: vals.append(None)
            return vals
    return [None]*expected_cols

class TableNotFound(Exception): ...

def fetch_biyori_once(place_no: int, race_no: int, ymd: str, slider: int):
    """
    1回ぶん（日和 PC優先→SP）を試し、テーブルを辞書で返す。
    slider=4 -> {tenji, shuukai, mawariashi, chokusen}
    slider=9 -> {avg_st, st_rank}
    """
    for pc in (True, False):
        url = biyori_url(place_no, race_no, ymd, slider, pc=pc)
        r = requests.get(url, headers=HDRS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        # キー語を含む“大きい”テーブルを探す
        keys = ["展示","周回","周り足","直線"] if slider==4 else ["平均ST","ST順位","平均ＳＴ"]
        target = None
        for t in soup.find_all("table"):
            txt = _clean(t.get_text(" ", strip=True))
            if all(k in txt for k in keys):
                target = t
                break
        if target:
            out = {"source":"biyori","url":url,"slider":slider}
            if slider==4:
                out["tenji"]      = _row_values(target, "展示")
                out["shuukai"]    = _row_values(target, "周回")
                out["mawariashi"] = _row_values(target, "周り足")
                out["chokusen"]   = _row_values(target, "直線")
            else:
                out["avg_st"]  = _row_values(target, "平均ST")
                out["st_rank"] = _row_values(target, "ST順位")
            return out
    raise TableNotFound(f"[biyori] table not found: slider={slider}")

def fetch_biyori_with_fallback(place_no: int, race_no: int, ymd: str):
    """
    直前(4)→MyData(9) の順で集め、どちらもNGなら公式へフォールバック。
    戻り値:
      {"fallback": False, "tenji":[...],...,"src":"biyori","ref_url": "..."}
      or
      {"fallback": True, "src":"official","ref_url": "..."}
    """
    collected = {}
    errs = []
    for s in (4, 9):
        try:
            d = fetch_biyori_once(place_no, race_no, ymd, s)
            collected.update(d)
        except Exception as e:
            errs.append(str(e))
    if collected.get("tenji") or collected.get("avg_st"):
        collected["fallback"] = False
        collected["src"] = "biyori"
        collected["ref_url"] = collected.get("url","")
        collected["errors"] = errs
        return collected

    # 公式フォールバック（URLだけ返す簡易）
    url = official_beforeinfo_url(place_no, race_no, ymd)
    return {"fallback": True, "src":"official", "ref_url": url, "errors": errs}

# ========== 公式 beforeinfo ==========
def official_beforeinfo_url(place_no: int, race_no: int, ymd: str) -> str:
    jcd = f"{place_no:02d}"
    return f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={race_no}&jcd={jcd}&hd={ymd}"

# ========== 解析 & 買い目生成 ==========
def _to_float(x) -> Optional[float]:
    if x is None: return None
    try:
        s = str(x).replace("F","").replace("L","").replace("秒","")
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        return float(m.group(0)) if m else None
    except Exception:
        return None

def _rank(vals: List[Optional[float]], higher_is_better: bool) -> List[int]:
    pairs = []
    for i,v in enumerate(vals):
        if v is None:
            pairs.append((math.inf if higher_is_better else -math.inf, i))
        else:
            pairs.append((v, i))
    pairs.sort(key=lambda x:x[0], reverse=higher_is_better)
    ranks = [0]*6
    for pos,(_,idx) in enumerate(pairs, start=1):
        ranks[idx]=pos
    return ranks

def analyze_and_build_bets(data: dict):
    """
    日和の直前(展示/周回/周り足/直線)＋MyData(平均ST) を統合して
    詳細な展開文＋本線/抑え/狙い（多め）を作る
    """
    tenji   = [ _to_float(x) for x in data.get("tenji",      [None]*6) ]
    shuukai = [ _to_float(x) for x in data.get("shuukai",    [None]*6) ]
    mawari  = [ _to_float(x) for x in data.get("mawariashi", [None]*6) ]
    choku   = [ _to_float(x) for x in data.get("chokusen",   [None]*6) ]
    avg_st  = [ _to_float(x) for x in data.get("avg_st",     [None]*6) ]
    st_rank = [ _to_float(x) for x in data.get("st_rank",    [None]*6) ]

    # ランク化（小さいほど良: 展示/周回/周り足/平均ST, 大きいほど良: 直線）
    rk_tenji = _rank(tenji,   higher_is_better=False)
    rk_shu   = _rank(shuukai, higher_is_better=False)
    rk_mawa  = _rank(mawari,  higher_is_better=False)
    rk_choku = _rank(choku,   higher_is_better=True)
    rk_st    = _rank(avg_st,  higher_is_better=False)

    # 総合スコア（重み）
    W = {"展示":0.30, "周回":0.25, "周り足":0.20, "直線":0.15, "ST":0.10}
    score = [0.0]*6
    for i in range(6):
        for rk,key in [(rk_tenji,"展示"),(rk_shu,"周回"),(rk_mawa,"周り足"),(rk_choku,"直線"),(rk_st,"ST")]:
            if rk[i]==0: continue
            score[i] += (7 - rk[i]) * W[key]
    order = sorted(range(6), key=lambda i: score[i], reverse=True)  # indices 0..5
    lanes = [i+1 for i in order]

    # 軸決定：1号艇が総合上位2以内なら1軸、そうでなければ総合1位
    axis = 1 if 0 in order[:2] else lanes[0]

    # 詳細展開テキスト
    def top3(rk, label, tip):
        pairs = [(i+1,rk[i]) for i in range(6) if rk[i]>0]
        pairs.sort(key=lambda x:x[1])
        if not pairs: return f"・{label}: データ不足"
        txt = " / ".join(f"{a}({b}位)" for a,b in pairs[:3])
        return f"・{label}: {txt}  {tip}"

    notes = [
        top3(rk_tenji,"展示","↑タイム良"),
        top3(rk_shu,"周回","↑旋回力○"),
        top3(rk_mawa,"周り足","↑出足○"),
        top3(rk_choku,"直線","↑行き足○"),
        top3(rk_st,"平均ST","↑スタート安定"),
    ]

    scenario_lines = []
    if axis==1 and rk_tenji[0]<=2 and rk_st[0]<=3:
        scenario_lines.append("①イン先制の逃げ本線。STも安定、少なくとも2コースの差しを封じる想定。")
    elif axis in (2,3) and rk_st[axis-1]<=2 and rk_choku[axis-1]<=2:
        scenario_lines.append(f"{axis}コースの好発から“差し/まくり差し”本線。内残りは2–1/3–1軸で。")
    elif lanes[0] in (4,5,6) and rk_choku[lanes[0]-1]==1:
        scenario_lines.append(f"外勢の直線優勢。{lanes[0]}の一撃“まくり”本線、内の残りも押さえ。")
    else:
        scenario_lines.append(f"{axis}中心。枠なり想定で相手は上位評価順。")

    # ---- 買い目（多め）----
    def tri(a,b,c): return f"{a}-{b}-{c}"
    others = [x for x in lanes if x!=axis]
    top4 = others[:4] if len(others)>=4 else others

    # 本線：軸→上位4→上位4（順序違い）最大 MAX_MAIN
    main=[]
    for i,b in enumerate(top4):
        for j,c in enumerate(top4):
            if i==j: continue
            main.append(tri(axis,b,c))

    # 抑え：相手頭→軸→相手（上位3）最大 MAX_COVER
    cover=[]
    top3 = others[:3] if len(others)>=3 else others
    for i,b in enumerate(top3):
        for j,c in enumerate(top3):
            if i==j: continue
            cover.append(tri(b,axis,c))

    # 狙い：外勢/3番手絡みなど  最大 MAX_ATTACK
    attack=[]
    if len(others)>=4:
        attack += [tri(axis,others[3],others[0]), tri(axis,others[3],others[1])]
    if len(others)>=3:
        attack += [tri(others[0],others[2],axis), tri(others[1],others[2],axis)]

    def dedup(seq):
        out=[]
        for x in seq:
            if x not in out: out.append(x)
        return out
    main   = dedup(main)[:MAX_MAIN]
    cover  = [x for x in dedup(cover) if x not in main][:MAX_COVER]
    attack = [x for x in dedup(attack) if x not in main+cover][:MAX_ATTACK]

    return {
        "axis": axis,
        "lanes": lanes,
        "scenario": " ".join(scenario_lines),
        "notes": notes,
        "main": main,
        "cover": cover,
        "attack": attack
    }

def build_reply(place_no: int, race_no: int, ymd: str, data: dict, res: dict) -> str:
    header = f"📍 {INV_PLACE.get(place_no, f'場No.{place_no}')} {race_no}R ({fmt_ymd(ymd)})\n" + "─"*24
    body = [
        f"🧭 展開予想：{res['scenario']}",
        "🧩 根拠：",
        *res["notes"],
        "─"*24,
        f"🎯 本線（{}点）: ".format(len(res["main"])) + ", ".join(res["main"]) if res["main"] else "🎯 本線: なし",
        f"🛡️ 抑え（{}点）: ".format(len(res["cover"])) + ", ".join(res["cover"]) if res["cover"] else "🛡️ 抑え: なし",
        f"💥 狙い（{}点）: ".format(len(res["attack"])) + ", ".join(res["attack"]) if res["attack"] else "💥 狙い: なし",
    ]
    tail = []
    if data.get("fallback"):
        tail.append(f"\n（データ元：公式フォールバック）\n{data.get('ref_url')}")
    else:
        tail.append(f"\n（データ元：ボートレース日和）\n{data.get('ref_url')}")
    return header + "\n" + "\n".join(body) + "\n" + "\n".join(tail)

# ========== Flask ルート ==========
@app.get("/")
def root():
    return "ok"

@app.get("/health")
def health():
    return jsonify(status="ok")

@app.get("/_debug/biyori")
def debug_biyori():
    place_no = int(request.args.get("place_no", "15"))
    race_no  = int(request.args.get("race_no", "5"))
    ymd      = request.args.get("hiduke", today_ymd())
    data = fetch_biyori_with_fallback(place_no, race_no, ymd)
    return jsonify(data)

@app.post("/callback")
def callback():
    if not handler:
        return "LINE handler not set", 500
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        return "Invalid signature", 400
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def on_text(event: MessageEvent):
    text = (event.message.text or "").strip()
    place_no, race_no, ymd = parse_user_text(text)
    if place_no == "HELP":
        msg = ("使い方：\n"
               "・『丸亀 8』 / 『丸亀 8 20250811』のように送信\n"
               "・日和の直前&MyDataを優先取得、ダメなら公式に自動フォールバック\n"
               "・買い目は 本線/抑え/狙い を多めに表示")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return
    if not (place_no and race_no and ymd):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(
            text="入力例：『丸亀 8』 / 『丸亀 8 20250811』 / 『help』"))
        return

    data = fetch_biyori_with_fallback(place_no, race_no, ymd)
    if data.get("fallback") and data.get("src")=="official":
        # データ不足時：汎用の押さえ目（最小限）を返す
        msg = (f"📍 {INV_PLACE.get(place_no)} {race_no}R ({fmt_ymd(ymd)})\n"
               "─"*24 + "\n"
               "日和の直前/MyDataが未取得のため、公式へフォールバック。\n"
               "データ不足なので汎用の押さえ目のみ。\n\n"
               "🎯 本線: 1-2-3, 1-3-2, 1-2-4, 1-3-4\n"
               "🛡️ 抑え: 2-1-3, 3-1-2\n"
               f"\n（公式URL）\n{data.get('ref_url')}")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    try:
        res = analyze_and_build_bets(data)
        msg = build_reply(place_no, race_no, ymd, data, res)
    except Exception as e:
        msg = (f"📍 {INV_PLACE.get(place_no)} {race_no}R ({fmt_ymd(ymd)})\n"
               "解析中にエラーが発生しました。時間をおいて再度お試しください。")
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))

# ===== エントリーポイント =====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
