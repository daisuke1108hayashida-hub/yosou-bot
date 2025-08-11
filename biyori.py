# biyori.py
import re
import time
from typing import List, Dict, Optional
import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; YosouBot/1.0)"}

def _to_float(x: Optional[str]) -> Optional[float]:
    if not x:
        return None
    s = x.strip()
    s = s.replace("Ｆ", "F").replace("－", "-")
    if s.startswith("F."):  # F.05 → 0.05 として扱う
        s = s.replace("F.", "0.")
    if s.startswith("."):
        s = "0" + s
    try:
        return float(s)
    except Exception:
        return None

def fetch_biyori(url: str) -> Dict:
    """
    ボートレース日和の『直前情報』ページURLから6艇ぶんの指標を抽出。
    戻り値: {"meta": {...}, "lanes":[{lane,name,tenji,syuukai,mawari,chokusen,st,tilt,weight,chosei,parts},..]}
    """
    res = requests.get(url, headers=HEADERS, timeout=20)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")

    # 直前情報テーブルを特定（見出しに「展示」「ST」等を含むもの）
    table = None
    for tbl in soup.select("table"):
        labels = [th.get_text(strip=True) for th in tbl.select("tbody tr th")]
        joined = " ".join(labels)
        if ("展示" in joined or "展示タイム" in joined) and "ST" in joined:
            table = tbl
            break
    if not table:
        raise RuntimeError("直前情報のテーブルが見つかりませんでした。URLが直前ページか確認してください。")

    # 行ラベル→6列値
    rows: Dict[str, List[Optional[str]]] = {}
    for tr in table.select("tbody tr"):
        th = tr.find("th")
        if not th:
            continue
        label = th.get_text(strip=True)
        tds = tr.find_all("td")
        if len(tds) >= 6:
            rows[label] = [td.get_text(strip=True) or None for td in tds[:6]]

    # 名前（登録番号＋氏名など）も可能なら取得
    names: List[Optional[str]] = [None]*6
    candidates = soup.find_all(text=re.compile(r"\d{4}"))
    idx = 0
    for node in candidates:
        s = re.sub(r"\s+", " ", node.strip())
        m = re.match(r"(\d{4})\s*([^\s].+)", s)
        if m and idx < 6:
            names[idx] = m.group(2).replace("　", " ").strip()
            idx += 1

    # マッピング
    KEY_MAP = {
        "展示": "tenji", "展示タイム": "tenji",
        "周回": "syuukai", "一周": "syuukai",
        "回り足": "mawari", "周り足": "mawari",
        "直線": "chokusen",
        "ST": "st", "スタート": "st",
        "チルト": "tilt",
        "体重": "weight",
        "調整重量": "chosei",
        "部品交換": "parts",
    }

    lanes = []
    for lane in range(6):
        rec: Dict[str, Optional[float]] = {"lane": lane+1, "name": names[lane]}
        for lab, key in KEY_MAP.items():
            # ラベルの部分一致で拾う（「展示」「展示タイム」両対応）
            hit = next((k for k in rows.keys() if lab in k), None)
            if not hit:
                continue
            raw = rows[hit][lane]
            if key == "parts":
                rec[key] = raw if raw and raw != "0" else None
            else:
                rec[key] = _to_float(raw)
        lanes.append(rec)

    # 欠損が多い場合は None のままでOK（スコア側で吸収）
    return {"meta": {"url": url, "fetched_at": int(time.time())}, "lanes": lanes}

# ---------- 予想ロジック ----------
import itertools
import math

WEIGHTS = {
    # まずは直前重視（あとで学習で動かす）
    "tenji": 0.45,      # 小さいほど良
    "syuukai": 0.20,    # 大きいほど良（※サイト仕様で逆なら調整）
    "chokusen": 0.20,   # 大きいほど良
    "st": 0.15,         # 小さいほど良
}

def _normalize(vals: List[Optional[float]], reverse: bool) -> List[float]:
    xs = [v for v in vals if v is not None]
    if len(xs) <= 1:
        return [0.0]*len(vals)
    lo, hi = min(xs), max(xs)
    if math.isclose(lo, hi):
        return [0.0]*len(vals)
    out = []
    for v in vals:
        if v is None:
            out.append(0.0)
        else:
            t = (v - lo) / (hi - lo)
            out.append(1 - t if reverse else t)
    return out

def score_lanes(lanes: List[Dict]) -> List[tuple[int,float]]:
    ten = _normalize([x.get("tenji") for x in lanes], reverse=True)
    syu = _normalize([x.get("syuukai") for x in lanes], reverse=False)
    cho = _normalize([x.get("chokusen") for x in lanes], reverse=False)
    st  = _normalize([x.get("st") for x in lanes], reverse=True)
    # 欠損率で重みをやや減衰
    def adj(w, arr): miss = sum(1 for a in arr if a == 0.0)/len(arr); return w*(1-0.5*miss)
    w_t, w_s, w_c, w_st = adj(WEIGHTS["tenji"], ten), adj(WEIGHTS["syuukai"], syu), adj(WEIGHTS["chokusen"], cho), adj(WEIGHTS["st"], st)

    scores = []
    for i in range(6):
        s = w_t*ten[i] + w_s*syu[i] + w_c*cho[i] + w_st*st[i]
        scores.append((i+1, s))
    return sorted(scores, key=lambda x: x[1], reverse=True)

def make_trifecta(scores: List[tuple[int,float]], max_patterns=9) -> Dict[str, List[str]]:
    order = [i for i,_ in scores]
    pats = []
    for a,b,c in itertools.permutations(order[:5], 3):
        if a!=b and b!=c and a!=c:
            pats.append(f"{a}-{b}-{c}")
        if len(pats) >= max_patterns: break
    return {"hon": pats[:3], "osae": pats[3:6], "nerai": pats[6:9]}

def narrative(lanes: List[Dict], scores: List[tuple[int,float]]) -> str:
    # ST最良・直線最良の枠を拾って、自然文を作る（数値は出さない）
    st_vals  = [x.get("st") for x in lanes]
    cho_vals = [x.get("chokusen") for x in lanes]
    def argmin(arr):
        m = None; idx = None
        for i,v in enumerate(arr):
            if v is None: continue
            if m is None or v < m:
                m, idx = v, i
        return None if idx is None else idx+1
    def argmax(arr):
        m = None; idx = None
        for i,v in enumerate(arr):
            if v is None: continue
            if m is None or v > m:
                m, idx = v, i
        return None if idx is None else idx+1

    head = scores[0][0]
    st_best  = argmin(st_vals)
    cho_best = argmax(cho_vals)

    if head == 1 and (st_best in (1,2)):
        return "①の先制想定でイン本線。相手は直前上位の内側。"
    if head in (2,3) and (st_best in (2,3)):
        return f"{head}の差し・まくり差しが軸。①は残しまで。"
    if head >= 4 and (cho_best in (4,5,6)):
        return f"外勢の直線が良化。{head}頭の強攻まで一考。"
    return "混戦。直前の上位気配を素直に評価。"
