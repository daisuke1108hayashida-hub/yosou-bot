# -*- coding: utf-8 -*-
import os
import re
import math
import datetime as dt
from typing import List, Optional

import requests
from flask import Flask, request, jsonify, Response
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import InvalidSignatureError

from bs4 import BeautifulSoup
from biyori import fetch_biyori_first_then_fallback

app = Flask(__name__)

# ====== LINE 環境変数 ======
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

# ====== 競艇場 -> place_no ======
PLACE_MAP = {
    "桐生":1,"戸田":2,"江戸川":3,"平和島":4,"多摩川":5,"浜名湖":6,"蒲郡":7,"常滑":8,"津":9,"三国":10,
    "びわこ":11,"住之江":12,"尼崎":13,"鳴門":14,"丸亀":15,"児島":16,"宮島":17,"徳山":18,"下関":19,"若松":20,
    "芦屋":21,"福岡":22,"唐津":23,"大村":24
}

# ====== 出す点数上限（ここで調整できます） ======
MAX_MAIN   = 12   # 本線
MAX_COVER  = 8    # 抑え
MAX_ATTACK = 8    # 狙い

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

def today_yyyymmdd():
    return (dt.datetime.utcnow() + dt.timedelta(hours=9)).strftime("%Y%m%d")

def name_by_place(place_no: int) -> str:
    for k,v in PLACE_MAP.items():
        if v == place_no: return k
    return f"場No.{place_no}"

def official_url(place_no: int, race_no: int, ymd: str) -> str:
    jcd = f"{place_no:02d}"
    return f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={race_no}&jcd={jcd}&hd={ymd}"

# ====== 公式フォールバック（最小限：URLだけ返す） ======
def fetch_official_beforeinfo(place_no: int, race_no: int, ymd: str) -> dict:
    # ここを強化すれば公式の数値も使える。まずはURLのみで運用。
    return {"official_beforeinfo_url": official_url(place_no, race_no, ymd)}

# ====== 解析・展開生成 ======
def _to_float(x) -> Optional[float]:
    if x is None: return None
    try:
        s = str(x).replace("F","").replace("L","").replace("秒","")
        return float(re.findall(r"-?\d+(?:\.\d+)?", s)[0])
    except Exception:
        return None

def _rank(values: List[Optional[float]], higher_is_better: bool) -> List[int]:
    # None は最下位扱い。higher_is_better=True のときは降順。
    pairs = []
    for i,v in enumerate(values):
        if v is None:
            pairs.append((math.inf if higher_is_better else -math.inf, i))
        else:
            pairs.append((v, i))
    pairs.sort(key=lambda x: x[0], reverse=higher_is_better)
    rank = [0]*6
    for r,(_,idx) in enumerate(pairs, start=1):
        rank[idx] = r
    return rank

def analyze_and_bet(data: dict):
    """日和の直前(展示/周回/周り足/直線)＋MyData(平均ST/順位) を統合して
       展開文と買い目（多め）を生成"""
    # 値配列を取得（6艇ぶん）
    tenji   = [ _to_float(x) for x in data.get("tenji",   [None]*6) ]
    shuukai = [ _to_float(x) for x in data.get("shuukai", [None]*6) ]
    mawari  = [ _to_float(x) for x in data.get("mawariashi", [None]*6) ]
    chok    = [ _to_float(x) for x in data.get("chokusen", [None]*6) ]
    avg_st  = [ _to_float(x) for x in data.get("avg_st",  [None]*6) ]
    st_rk   = [ _to_float(x) for x in data.get("st_rank", [None]*6) ]

    # ランク（小さいほど良い系：展示/周回/周り足/平均ST、 大きいほど良い系：直線）
    rk_tenji   = _rank(tenji, higher_is_better=False)
    rk_shuukai = _rank(shuukai, higher_is_better=False)
    rk_mawari  = _rank(mawari, higher_is_better=False)
    rk_chok    = _rank(chok,   higher_is_better=True)
    rk_avgst   = _rank(avg_st, higher_is_better=False)

    # 総合スコア（重み）
    W = {"展示":0.30, "周回":0.25, "周り足":0.20, "直線":0.15, "ST":0.10}
    score = [0.0]*6
    for i in range(6):
        for rk, key in [(rk_tenji,"展示"), (rk_shuukai,"周回"), (rk_mawari,"周り足"),
                        (rk_chok,"直線"), (rk_avgst,"ST")]:
            if rk[i] == 0:  # データなし
                continue
            # 1位=6点, 2位=5点... 6位=1点
            score[i] += (7 - rk[i]) * W[key]

    order = sorted(range(6), key=lambda i: score[i], reverse=True)   # インデックス0..5
    lanes = [i+1 for i in order]  # 1..6
    axis = 1 if 0 in order[:2] else lanes[0]  # 1号艇が総合上位2なら軸=1、そうでなければ総合1位

    # ---- 展開テキストの作成（詳しめ）----
    def tops(rk, label, better="↑"):
        pairs = [(i+1, rk[i]) for i in range(6) if rk[i]>0]
        pairs.sort(key=lambda x: x[1])
        if not pairs: return f"{label}: データ不足"
        head = "・" + label + "：" + " / ".join([f"{p[0]}({p[1]}位)" for p in pairs[:3]])
        return head + f"  {better}"

    notes = [
        tops(rk_tenji, "展示", "↑タイム良"),
        tops(rk_shuukai, "周回", "↑回り足○"),
        tops(rk_mawari, "周り足", "↑出足○"),
        tops(rk_chok, "直線", "↑行き足○"),
        tops(rk_avgst, "平均ST", "↑ST安定"),
    ]

    # シナリオ分岐
    scenario = []
    if axis == 1 and rk_tenji[0] <= 2 and rk_avgst[0] <= 3:
        scenario.append("①イン先制の逃げ本線。")
    elif axis in (2,3) and rk_avgst[axis-1] <= 2 and rk_chok[axis-1] <= 2:
        scenario.append(f"{axis}コースの鋭発から“差し/まくり差し”の台。")
    elif lanes[0] in (4,5,6) and rk_chok[lanes[0]-1] == 1:
        scenario.append(f"外の直線優勢で{lanes[0]}コースの一撃“まくり”警戒。")
    else:
        scenario.append(f"{axis}コース中心に上位評価。隊形は枠なり想定。")

    # キーファクター
    keyfacts = []
    if st_rk and all(x is not None for x in st_rk):
        st_pairs = sorted([(i+1, st_rk[i]) for i in range(6)], key=lambda x: x[1])
        keyfacts.append("ST順位： " + " / ".join([f"{a}({int(b)}位)" for a,b in st_pairs[:3]]))
    if avg_st and any(x is not None for x in avg_st):
        mx = min([x for x in avg_st if x is not None], default=None)
        if mx is not None:
            who = [i+1 for i,v in enumerate(avg_st) if v == mx]
            keyfacts.append(f"最速平均ST：{mx:.02f}（{','.join(map(str,
