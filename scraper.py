# scraper.py
from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict

import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))

PLACE_CODE = {
    "桐生":"01","戸田":"02","江戸川":"03","平和島":"04","多摩川":"05",
    "浜名湖":"06","蒲郡":"07","常滑":"08","津":"09","三国":"10",
    "びわこ":"11","住之江":"12","尼崎":"13","鳴門":"14","丸亀":"15",
    "児島":"16","宮島":"17","徳山":"18","下関":"19","若松":"20",
    "芦屋":"21","福岡":"22","唐津":"23","大村":"24",
}

UA = {
    "User-Agent": "Mozilla/5.0 (compatible; KyoteiYosouBot/1.0; +https://example.com)",
    "Accept-Language": "ja,en;q=0.8",
    "Cache-Control": "no-cache",
    "Referer": "https://www.boatrace.jp/",
}

def today_ymd() -> str:
    return datetime.now(JST).strftime("%Y%m%d")

def build_urls(place: str, rno: int, ymd: Optional[str]) -> Dict[str, str]:
    jcd = PLACE_CODE.get(place)
    if not jcd:
        raise ValueError("未対応の場名です")
    if not ymd:
        ymd = today_ymd()
    return {
        "racelist": f"https://www.boatrace.jp/owpc/pc/race/racelist?rno={rno}&jcd={jcd}&hd={ymd}",
        "racecard": f"https://www.boatrace.jp/owpc/pc/racedata/racecard?jcd={jcd}&hd={ymd}",
        # 直前情報（候補を複数用意、どれかが200なら使う）
        "beforeinfo1": f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={jcd}&hd={ymd}",
        "beforeinfo2": f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?jcd={jcd}&rno={rno}&hd={ymd}",
    }

def fetch(url: str, timeout: float = 10.0) -> Optional[str]:
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        if r.status_code == 200 and r.text:
            return r.text
    except Exception:
        pass
    return None

# ---------------- パース（できるだけ頑丈に） ----------------

def parse_racelist(html: str) -> List[Dict]:
    """
    1〜6号艇のベーシック情報（名前・勝率・モーター/ボート2連率など）を
    ゆるく拾って返す。見つからない項目は None。
    """
    soup = BeautifulSoup(html, "html.parser")
    results: List[Dict] = []

    # テーブル行を総当りで 6行ぶん拾う
    rows = soup.select("table tr")
    if not rows:
        rows = soup.find_all("tr")
    lane = 0
    for tr in rows:
        txt = tr.get_text(" ", strip=True)
        if not txt:
            continue
        # 号艇らしき数字（全角半角）
        m_lane = re.search(r"(^|\s)([1１][^0-9]|[2２]|[3３]|[4４]|[5５]|[6６])(\s|$)", txt)
        if not m_lane:
            # インデックスで補完
            pass
        lane += 1
        if lane > 6:
            break

        # 選手名（漢字/カタカナっぽい最適一致）
        name = None
        cand = re.findall(r"[一-龥々〆ヶァ-ヶー]+", txt)
        if cand:
            name = max(cand, key=len)

        # 全国勝率/当地勝率/平均STらしき数値
        nat_win = _find_first_float_after_keywords(txt, ["全国勝率", "全国"])
        loc_win = _find_first_float_after_keywords(txt, ["当地勝率", "当地"])
        st_avg  = _find_first_float_after_keywords(txt, ["ST", "平均ST"])

        # モーター/ボート2連率（xx.x%）
        motor2 = _find_percent_after_keywords(txt, ["モーター", "MNo", "M No", "MN"])
        boat2  = _find_percent_after_keywords(txt, ["ボート", "BNo", "B No", "BN"])

        results.append({
            "lane": lane, "name": name,
            "nat_win": nat_win, "loc_win": loc_win, "st": st_avg,
            "motor2": motor2, "boat2": boat2
        })

    # 6行足りなければ埋める
    while len(results) < 6:
        results.append({"lane": len(results)+1, "name": None, "nat_win": None, "loc_win": None, "st": None, "motor2": None, "boat2": None})

    return results[:6]

def _find_first_float_after_keywords(text: str, keys: List[str]) -> Optional[float]:
    for k in keys:
        m = re.search(k + r".{0,8}?([0-9]+\.[0-9])", text)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass
    # 直接 6.89 のような数列を拾う fallback
    m2 = re.search(r"([0-9]+\.[0-9])", text)
    if m2:
        try:
            return float(m2.group(1))
        except Exception:
            pass
    return None

def _find_percent_after_keywords(text: str, keys: List[str]) -> Optional[float]:
    for k in keys:
        m = re.search(k + r".{0,8}?([0-9]+(?:\.[0-9])?)\s*%", text)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass
    return None

def parse_beforeinfo(html: str) -> Dict:
    """
    直前情報（展示タイム/チルト/天候 など）をできるだけ拾う。
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # 展示タイム: 6つの 6.5x〜6.9x などを拾って昇順ではなく出現順で
    tenji = [float(x) for x in re.findall(r"([6-9]\.[0-9]{2})", text)]
    tenji = tenji[:6] if len(tenji) >= 6 else tenji

    # チルト：-0.5 / 0 / +0.5 など6つ
    tilts = []
    for m in re.findall(r"[+\-]?\d(?:\.\d)?", text):
        # チルトっぽいレンジのみ採用
        try:
            v = float(m)
            if -1.0 <= v <= 2.0:
                tilts.append(v)
        except Exception:
            pass
    if len(tilts) > 6:
        tilts = tilts[:6]

    # 天候/風/波（ざっくり）
    weather = {}
    m_wthr = re.search(r"(晴|曇|雨|雪|雷|小雨|くもり)", text)
    if m_wthr: weather["weather"] = m_wthr.group(1)
    m_wind = re.search(r"風\s*([0-9]+(?:\.[0-9])?)", text)
    if m_wind: weather["wind"] = f"{m_wind.group(1)}m"
    m_wave = re.search(r"波\s*([0-9]+(?:\.[0-9])?)", text)
    if m_wave: weather["wave"] = f"{m_wave.group(1)}cm"

    # 進入（スタート展示）っぽい並び（例: 123/456）
    m_si = re.search(r"進入[：:\s]*([1-6]{1,3}(?:/[1-6]{1,3})?)", text)
    start_exhibit = m_si.group(1) if m_si else None

    return {
        "tenji_times": tenji,
        "tilts": tilts,
        "weather": weather,
        "start_exhibit": start_exhibit
    }

# ---------------- 予想ロジック（簡易版） ----------------

def _nz(x: Optional[float], default: float = 0.0) -> float:
    return x if isinstance(x, (int, float)) else default

def score_and_predict(rlist: List[Dict], before: Dict) -> Dict:
    """
    合成スコア → 本線/抑え/狙い/展開 コメント
    """
    # コース有利度（汎用）
    bias = {1:0.33, 2:0.19, 3:0.17, 4:0.14, 5:0.10, 6:0.07}

    tenji = before.get("tenji_times") or []
    # 展示タイムは低いほど良い → 正規化（中央値基準）
    if len(tenji) >= 3:
        median = sorted(tenji)[len(tenji)//2]
    else:
        median = None

    scores = []
    for row in rlist:
        i = row["lane"]
        s = 0.0
        s += 0.40 * _nz(row.get("motor2"))            # モーター2連率
        s += 0.10 * _nz(row.get("boat2"))             # ボート2連率
        s += 0.25 * (10*_nz(row.get("nat_win")))      # 全国勝率×10
        s += 0.10 * (10*_nz(row.get("loc_win")))      # 当地勝率×10
        s += 0.07 * (100*_nz(row.get("st"), 0.20))*(-1)  # STは低い方が良い→符号逆
        s += 0.08 * (1.0 if median and len(tenji)>=i and tenji[i-1] <= median else 0.0)  # 展示T良好ボーナス
        s *= (1.0 + 0.15*bias.get(i,0))               # コース補正
        scores.append({"lane": i, "score": s})

    scores.sort(key=lambda x: x["score"], reverse=True)
    order = [x["lane"] for x in scores]

    # 券面組成
    head = order[0]
    two  = order[1]
    three= order[2]
    main = [f"{head}-{two}-{three}", f"{head}-{three}-{two}", f"{head}-{two}-全"]
    sub  = [f"{two}-{head}-{three}", f"{head}-{three}-全"]
    attack=[]
    # 外が上位なら穴を拾う
    for ln in order[:4]:
        if ln >= 4 and ln != head:
            attack.append(f"{ln}-{head}-{two}")
    attack = attack[:3]

    # コメント
    n = lambda i: (rlist[i-1].get("name") or f"{i}号艇")
    tenkai = f"枠なり想定。本線は{head}（{n(head)}）の先マイ。対抗は{two}・{three}。外の気配なら{attack[0].split('-')[0] if attack else order[3]}の一発ケア。"

    # 自信度（1位−2位スコア差でラフに）
    diff = scores[0]["score"] - scores[1]["score"]
    conf = "A" if diff >= 6 else ("B" if diff >= 3 else "C")

    return {
        "main": main,
        "sub": sub,
        "attack": attack,
        "comment": tenkai,
        "confidence": conf,
        "ranking": order,
        "scores": scores,
    }

# ---------------- 全体フロー ----------------

def collect_all(place: str, rno: int, ymd: Optional[str]) -> Dict:
    urls = build_urls(place, rno, ymd)

    # 出走表を最優先で取得
    html = fetch(urls["racelist"]) or fetch(urls["racecard"])
    if not html:
        raise RuntimeError("出走表ページを取得できませんでした。")

    rlist = parse_racelist(html)

    # 直前情報トライ（失敗しても続行）
    bhtml = fetch(urls["beforeinfo1"]) or fetch(urls["beforeinfo2"])
    before = parse_beforeinfo(bhtml) if bhtml else {}

    # 予想（簡易）
    pred = score_and_predict(rlist, before)

    return {
        "place": place, "rno": rno, "ymd": ymd or today_ymd(),
        "racelist": rlist,
        "beforeinfo": before,
        "prediction": pred,
    }
