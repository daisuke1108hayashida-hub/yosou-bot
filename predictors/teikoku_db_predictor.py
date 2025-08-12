# -*- coding: utf-8 -*-
"""
teikoku_db_predictor.py (v2)
- データ源: 艇国データバンクのレース個別ページ（/race/数字）
- 規約配慮: 1回のみ取得・3秒以上のアクセス間隔
- パーサ: テーブル優先＋自由テキスト走査で 1~6号艇の {lane,name,shibu,motor_two_rate,tenji_time} を推定
- 予想: イン/まくり/差しの簡易展開＋重み付け（イン有利ベース, モーター2連率, 展示タイム）
- 出力: 本線/抑え/穴 を各6〜8点に整形（毎回同じ目を避ける軽いシード）

依存: requests, beautifulsoup4
"""
import re
import time
import requests
from typing import List, Dict, Any, Optional
from hashlib import md5
from bs4 import BeautifulSoup

# ---- 規約順守 (3秒以上) ----
_LAST_FETCH_TS = 0.0
_MIN_INTERVAL_SEC = 3.1

def _wait_interval():
    global _LAST_FETCH_TS
    now = time.time()
    dt = now - _LAST_FETCH_TS
    if dt < _MIN_INTERVAL_SEC:
        time.sleep(_MIN_INTERVAL_SEC - dt)
    _LAST_FETCH_TS = time.time()

# ---- Utils ----
def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def _safe_text(el) -> str:
    return _clean(el.get_text(" ")) if el else ""

def _float_or_none(s: str) -> Optional[float]:
    try:
        return float(s)
    except:
        return None

# ---- 抽出 ----
def _extract_rows(soup: BeautifulSoup) -> List[List[str]]:
    rows: List[List[str]] = []
    for tbl in soup.select("table"):
        for tr in tbl.select("tr"):
            cols = [_safe_text(td) for td in tr.select("th,td")]
            cols = [c for c in cols if c]
            if len(cols) >= 2:
                rows.append(cols)
    if rows:
        return rows
    # 保険（カード/リスト断片）
    backup: List[List[str]] = []
    for blk in soup.select("section,article,div,li"):
        t = _safe_text(blk)
        ts = [x for x in re.split(r"[ \n\t]+", t) if x]
        if 2 <= len(ts) <= 16:
            backup.append(ts)
    return backup

LANE_RX = re.compile(r"^([1-6])\s*号?艇?$")
PCT_RX  = re.compile(r"(\d{1,2}(?:\.\d)?)\s*%")
TENJI_RX = re.compile(r"(?:展示|直前|TMP|T[^\w]?)\s*[:：]?\s*([0-2]?\d\.\d)")

def _guess_players(rows: List[List[str]]) -> List[Dict[str, Any]]:
    """1~6号艇の選手情報を推定抽出"""
    players: Dict[int, Dict[str, Any]] = {}

    # 1) 行単位でざっくり拾う
    for row in rows:
        joined = " ".join(row)
        tokens = [t for t in re.split(r"[ /｜|│・\[\]（）(),:：\u3000]+", joined) if t]
        lane = None
        # lane
        for t in tokens:
            m = LANE_RX.match(t)
            if m:
                lane = int(m.group(1))
                break
            if t.isdigit() and 1 <= int(t) <= 6:
                lane = int(t); break
        if not lane:
            continue

        d = players.get(lane, {"lane": lane, "name": None, "shibu": None,
                               "motor_two_rate": None, "tenji_time": None})

        # name候補（漢字/カナ優先）
        name_cands = [t for t in tokens if 2 <= len(t) <= 10 and re.search(r"[ぁ-んァ-ン一-龥]", t)]
        if not d["name"] and name_cands:
            d["name"] = name_cands[0]

        # 支部っぽい 2-3文字
        if not d["shibu"]:
            for t in tokens:
                if 2 <= len(t) <= 3 and re.search(r"[一-龥ァ-ヶ]", t):
                    d["shibu"] = t; break

        # motor two rate
        if d["motor_two_rate"] is None:
            m = PCT_RX.search(joined)
            if m:
                d["motor_two_rate"] = _float_or_none(m.group(1))

        # 展示タイム
        if d["tenji_time"] is None:
            m2 = TENJI_RX.search(joined)
            if m2:
                d["tenji_time"] = _float_or_none(m2.group(1))

        players[lane] = d

    # 2) 欠け埋め
    out: List[Dict[str, Any]] = []
    for l in range(1,7):
        if l in players:
            out.append(players[l])
        else:
            out.append({"lane": l, "name": f"不明{l}", "shibu": None,
                        "motor_two_rate": None, "tenji_time": None})
    return out

# ---- 予想ロジック ----
def _lane_base_scores() -> Dict[int, float]:
    """
    イン有利の一般的傾向を素点化（平均的な場を想定）。
    必要なら後で場別補正を足す（今はページから場名が取りにくいので一定値）。
    """
    # 1~6の基礎点（相対）。合計は任意でOK
    return {1: 62.0, 2: 20.0, 3: 10.5, 4: 5.5, 5: 1.5, 6: 0.5}

def _score_players(players: List[Dict[str, Any]]) -> Dict[int, float]:
    base = _lane_base_scores()
    scores: Dict[int, float] = {i: base[i] for i in range(1,7)}

    # モーター2連率補正（±10%幅）
    for p in players:
        r = p.get("motor_two_rate")
        if r is not None:
            # 50% を中立、10%で ±3pt 程度の補正
            scores[p["lane"]] += (r - 50.0) * 0.3

    # 展示タイム補正（速い=加点 / 遅い=減点）
    tenjis = [p["tenji_time"] for p in players if p.get("tenji_time") is not None]
    if len(tenjis) >= 2:
        best = min(tenjis)
        worst = max(tenjis)
        span = max(0.01, worst - best)
        for p in players:
            t = p.get("tenji_time")
            if t is not None:
                # 速いほど +4pt まで
                scores[p["lane"]] += (worst - t) / span * 4.0

    return scores

def _decide_scenario(players: List[Dict[str, Any]], seed: str) -> str:
    """イン逃げ/まくり(3)/まくり(4)/差し の大枠選択"""
    sc = _score_players(players)
    s = int(md5(seed.encode("utf-8")).hexdigest(), 16) % 100

    # キー比較
    r1, r3, r4 = sc[1], sc[3], sc[4]
    if r1 >= max(r3, r4) + 4.0:
        return "イン逃げ"
    if r4 >= max(r1, r3) + 2.0:
        return "まくり(4)"
    if r3 >= max(r1, r4) + 2.0:
        return "まくり(3)"
    return "差し" if s < 40 else ("イン逃げ" if s < 70 else "まくり(4)")

def _uniq(seq: List[str]) -> List[str]:
    out, seen = [], set()
    for x in seq:
        if x not in seen:
            out.append(x); seen.add(x)
    return out

def _tickets_for(base: str, scores: Dict[int, float]) -> Dict[str, List[str]]:
    """
    展開→買い目（各6〜8点）。中穴寄りを少し入れる。
    """
    if base == "イン逃げ":
        hon = [f"1-{a}-{b}" for a in [2,3] for b in [2,3,4,5,6] if a != b]
        osa = [f"1-{a}-{b}" for a in [4,5] for b in [2,3,4,5,6] if a != b][:6]
        # 穴は外の伸び・スコア高めを少量
        outs = [i for i,_ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True) if i>=4][:2]
        ana = [f"{a}-1-{b}" for a in [2,3] for b in outs][:6]
        if len(ana) < 6:
            ana += ["2-1-3","3-1-2"][:6-len(ana)]
    elif base == "まくり(3)":
        hon = ["3-1-2","3-1-4","3-4-1","3-2-1","3-5-1","3-1-5","3-1-6"][:8]
        osa = ["1-3-2","1-3-4","3-2-4","3-4-2","2-3-1","4-3-1"][:6]
        ana = ["4-5-3","5-3-1","2-3-5","3-6-1","2-1-3","1-2-3"][:6]
    elif base == "まくり(4)":
        hon = ["4-1-2","4-1-3","4-5-1","4-2-1","4-3-1","4-1-5","4-1-6"][:8]
        osa = ["1-4-2","1-4-3","4-2-3","4-3-2","2-4-1","5-4-1"][:6]
        ana = ["5-4-2","6-4-1","2-1-4","3-1-4","4-6-1","2-4-6"][:6]
    else:  # 差し（2中心）
        hon = ["2-1-3","2-1-4","1-2-3","1-2-4","2-3-1","2-4-1","1-3-2","1-4-2"][:8]
        osa = ["3-2-1","4-2-1","2-1-5","1-2-5","2-1-6","1-2-6"][:6]
        ana = ["3-1-2","4-1-2","2-5-1","5-2-1","6-2-1","2-6-1"][:6]

    return {"本線": _uniq(hon)[:8], "抑え": _uniq(osa)[:6], "穴": _uniq(ana)[:6]}

# ---- 公開API ----
def predict_from_teikoku(url: str) -> Dict[str, Any]:
    """
    Parameters
    ----------
    url: str  # 例: https://boatrace-db.net/race/1234567

    Returns
    -------
    {
      "source": url,
      "players": [{"lane":1,"name":"..","shibu":"..","motor_two_rate":36.5,"tenji_time":6.69}, ...],
      "scenario": "イン逃げ",
      "scores": {1:..,2:.., ...},
      "tickets": {"本線":[...], "抑え":[...], "穴":[...]}
    }
    """
    if not re.match(r"^https?://(?:www\.)?boatrace-db\.net/race/\d+/?$", url, re.I):
        raise ValueError("対応形式は https://boatrace-db.net/race/数字 です。")

    _wait_interval()
    headers = {"User-Agent": "yosou-bot/1.0 (+respecting-site-rules)"}
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")

    rows = _extract_rows(soup)
    players = _guess_players(rows)
    scores = _score_players(players)
    scenario = _decide_scenario(players, seed=url)
    tickets = _tickets_for(scenario, scores)

    return {
        "source": url,
        "players": players,
        "scenario": scenario,
        "scores": scores,
        "tickets": tickets
    }

def format_prediction_message(result: Dict[str, Any]) -> str:
    lines = []
    lines.append("【艇国DB 予想】")
    lines.append(f"展開見立て：{result['scenario']}")
    lines.append("")
    def blk(ttl, arr): return f"《{ttl}》\n" + " / ".join(arr)
    lines.append(blk("本線", result["tickets"]["本線"]))
    lines.append(blk("抑え", result["tickets"]["抑え"]))
    lines.append(blk("穴",   result["tickets"]["穴"]))
    lines.append("")
    lines.append("出走想定：")
    for p in result["players"]:
        rate = f"{p['motor_two_rate']}%" if p.get("motor_two_rate") is not None else "-"
        tenj = f"{p['tenji_time']}" if p.get("tenji_time") is not None else "-"
        shibu = p.get("shibu") or "-"
        lines.append(f"{p['lane']}号艇 {p['name']}（{shibu} / M2連:{rate} / 展示:{tenj}）")
    lines.append("")
    lines.append("※データ取得: 艇国データバンク（1アクセス/回・3秒インターバル遵守）")
    return "\n".join(lines)
