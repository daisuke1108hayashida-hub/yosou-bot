# -*- coding: utf-8 -*-
"""
teikoku_db_predictor.py
- 艇国データバンク(boatrace-db.net)のレース個別ページ1件から出走情報を抽出
- サイト規約に合わせてアクセスは3秒以上の間隔を強制
- ページ構造変化に耐えるため、表/カード/自由テキストの3段抽出でロバスト化
- 簡易ロジックで「イン逃げ / まくり(3) / まくり(4) / 差し」を判定
- 本線/抑え/穴 を各6〜8点に整形して返す（毎回同じ目の固定化を回避するシード付き）
- 依存: requests, bs4
"""
import re
import time
import requests
from hashlib import md5
from typing import List, Dict, Any, Optional
from bs4 import BeautifulSoup

# --- 規約順守: 3秒以上のアクセス間隔 ---
_LAST_FETCH_TS = 0.0
_MIN_INTERVAL_SEC = 3.1  # 3秒よりわずかに大きくして安全側

def _wait_interval():
    global _LAST_FETCH_TS
    now = time.time()
    dt = now - _LAST_FETCH_TS
    if dt < _MIN_INTERVAL_SEC:
        time.sleep(_MIN_INTERVAL_SEC - dt)
    _LAST_FETCH_TS = time.time()

# --- HTML抽出のユーティリティ ---
def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def _safe_text(el) -> str:
    return _clean_text(el.get_text(" ")) if el else ""

def _extract_rows(soup: BeautifulSoup) -> List[List[str]]:
    """
    壊れにくさ優先の多段抽出:
      1) table > tr > (th|td)
      2) リスト/カード断片から 2..12語を一行として抽出
    """
    rows: List[List[str]] = []
    # 1) テーブル優先
    for tbl in soup.select("table"):
        for tr in tbl.select("tr"):
            cols = [_safe_text(td) for td in tr.select("th,td")]
            cols = [c for c in cols if c]
            if len(cols) >= 2:
                rows.append(cols)
    if rows:
        return rows

    # 2) カード/リストの断片から拾う（保険）
    generic_rows: List[List[str]] = []
    for blk in soup.select("section,article,div,li"):
        txts = [t.strip() for t in _safe_text(blk).split(" ") if t.strip()]
        if 2 <= len(txts) <= 12:
            generic_rows.append(txts)
    return generic_rows

def _guess_players(rows: List[List[str]]) -> List[Dict[str, Any]]:
    """
    1〜6号艇の {lane,name,shibu,motor_two_rate} を推定抽出。
    - 「%」を含む数値は2連率候補として取得（例: 36.5%）
    - 号艇候補は 1..6 の数字
    - 選手名は漢字/カナを多く含む語を優先
    """
    players: List[Dict[str, Any]] = []

    for row in rows:
        joined = " ".join(row)
        tokens = re.split(r"[ \t,／/|│・\[\]（）()\u3000>:\-]+", joined)

        # lane
        lane: Optional[int] = None
        for t in tokens:
            if t.isdigit() and 1 <= int(t) <= 6:
                lane = int(t); break

        # name
        name: Optional[str] = None
        name_cands = [t for t in tokens if 2 <= len(t) <= 10 and re.search(r"[ぁ-んァ-ン一-龥]", t)]
        if name_cands:
            name = name_cands[0]

        # shibu（2-3文字を優先）
        shibu: Optional[str] = None
        for t in tokens:
            if 2 <= len(t) <= 3 and re.search(r"[一-龥ァ-ヶ]", t):
                shibu = t; break

        # motor two-rate
        rate: Optional[float] = None
        m = re.search(r"(\d{1,2}(?:\.\d)?)\s*%", joined)
        if m:
            try:
                rate = float(m.group(1))
            except:
                rate = None

        if lane and name:
            players.append({
                "lane": lane,
                "name": name,
                "shibu": shibu,
                "motor_two_rate": rate
            })

        if len(players) >= 6:
            break

    # 欠けをダミーで埋める
    exist = {p["lane"] for p in players}
    for l in range(1, 7):
        if l not in exist:
            players.append({
                "lane": l, "name": f"不明{l}", "shibu": None, "motor_two_rate": None
            })

    players.sort(key=lambda x: x["lane"])
    return players

# --- 展開推定と買い目生成 ---
def _decide_scenario(players: List[Dict[str, Any]], seed: str) -> str:
    """
    イン逃げ / まくり(3) / まくり(4) / 差し を簡易判定。
    ・1=イン強ければ「イン逃げ」
    ・3/4の率が強ければ各「まくり」
    ・拮抗時はURL等から作るシードで揺らぎ→毎回同じ目を回避
    """
    def rate(lane, default=52.0):
        p = next((x for x in players if x["lane"] == lane), None)
        return (p.get("motor_two_rate") or default) if p else default

    r1 = rate(1, 55.0)
    r3 = rate(3, 50.0) + 2.0
    r4 = rate(4, 50.0) + 3.0

    s = int(md5(seed.encode("utf-8")).hexdigest(), 16) % 100
    if r1 >= max(r3, r4) + 3:
        return "イン逃げ"
    if r4 >= max(r1, r3) + 2:
        return "まくり(4)"
    if r3 >= max(r1, r4) + 2:
        return "まくり(3)"
    return "差し" if s < 40 else ("イン逃げ" if s < 70 else "まくり(4)")

def _uniq(seq: List[str]) -> List[str]:
    out, seen = [], set()
    for x in seq:
        if x not in seen:
            out.append(x); seen.add(x)
    return out

def _tickets_for(base: str) -> Dict[str, List[str]]:
    """
    ベース展開 → 買い目(各6〜8点)
    """
    if base == "イン逃げ":
        hon = [f"1-{a}-{b}" for a in [2,3] for b in [2,3,4,5,6] if a != b]
        osa = [f"1-{a}-{b}" for a in [4,5] for b in [2,3,4,5,6] if a != b][:6]
        ana = ["2-1-3","2-1-4","3-1-2","3-1-4","1-6-2","1-2-6"]
    elif base == "まくり(3)":
        hon = ["3-1-2","3-1-4","3-4-1","3-2-1","3-5-1","3-1-5"]
        osa = ["1-3-2","1-3-4","3-2-4","3-4-2","2-3-1","4-3-1"]
        ana = ["4-5-3","5-3-1","2-3-5","3-6-1","2-1-3","1-2-3"]
    elif base == "まくり(4)":
        hon = ["4-1-2","4-1-3","4-5-1","4-2-1","4-3-1","4-1-5"]
        osa = ["1-4-2","1-4-3","4-2-3","4-3-2","2-4-1","5-4-1"]
        ana = ["5-4-2","6-4-1","2-1-4","3-1-4","4-6-1","2-4-6"]
    else:  # 差し
        hon = ["2-1-3","2-1-4","1-2-3","1-2-4","2-3-1","2-4-1","1-3-2","1-4-2"]
        osa = ["3-2-1","4-2-1","2-1-5","1-2-5","2-1-6","1-2-6"]
        ana = ["3-1-2","4-1-2","2-5-1","5-2-1","6-2-1","2-6-1"]

    return {
        "本線": _uniq(hon)[:8],
        "抑え": _uniq(osa)[:6],
        "穴":   _uniq(ana)[:6]
    }

# --- 公開API ---
def predict_from_teikoku(url: str) -> Dict[str, Any]:
    """
    Parameters
    ----------
    url : str
        艇国データバンクのレース個別ページURL（ユーザーがブラウザで開けるページ）
    Returns
    -------
    dict (メッセージ化しやすい形)
    {
      "source": url,
      "players": [
         {"lane":1,"name":"○○","shibu":"福岡","motor_two_rate": 36.5}, ...
      ],
      "scenario": "イン逃げ",
      "tickets": {"本線":[...6-8点], "抑え":[...], "穴":[...] }
    }
    """
    if not url.startswith("http"):
        raise ValueError("URLを確認してください（http から始まるフルURLが必要です）。")

    _wait_interval()  # 3秒以上の間隔

    # 1回だけ取得（同一URLに繰り返しアクセスしない）
    headers = {
        "User-Agent": "yosou-bot/1.0 (+respecting-site-rules)"
    }
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")

    rows = _extract_rows(soup)
    players = _guess_players(rows)

    # 展開決定（URLでシード固定：同じURLは同じ揺らぎ）
    scenario = _decide_scenario(players, seed=url)

    tickets = _tickets_for(scenario)
    return {
        "source": url,
        "players": players,
        "scenario": scenario,
        "tickets": tickets
    }

# --- フォーマッタ（LINE返信用の体裁） ---
def format_prediction_message(result: Dict[str, Any]) -> str:
    lines = []
    lines.append("【艇国DB 予想】")
    lines.append(f"展開見立て：{result['scenario']}")
    lines.append("")
    def fmt_block(ttl, arr):
        rows = " / ".join(arr)
        return f"《{ttl}》\n{rows}"
    lines.append(fmt_block("本線", result["tickets"]["本線"]))
    lines.append(fmt_block("抑え", result["tickets"]["抑え"]))
    lines.append(fmt_block("穴",   result["tickets"]["穴"]))
    lines.append("")
    # 選手一覧（簡易）
    plist = []
    for p in result["players"]:
        rate = f"{p['motor_two_rate']}%" if p['motor_two_rate'] is not None else "-"
        shibu = p['shibu'] or "-"
        plist.append(f"{p['lane']}号艇 {p['name']}（{shibu} / M2連 {rate}）")
    lines.append("出走想定：\n" + "\n".join(plist))
    lines.append("")
    lines.append("※データ取得: 艇国データバンク（サイト規約順守 / 3秒間隔・過剰アクセスなし）")
    return "\n".join(lines)

if __name__ == "__main__":
    # 手動テスト用（例のURLを入れて実行）
    TEST_URL = "https://boatrace-db.net/race/..."  # ←レースページURLに差し替え
    try:
        res = predict_from_teikoku(TEST_URL)
        print(format_prediction_message(res))
    except Exception as e:
        print("実行エラー:", e)
