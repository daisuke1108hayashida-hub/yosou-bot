# predictor.py
from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import statistics
import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))

JCD = {
    "桐生":"01","戸田":"02","江戸川":"03","平和島":"04","多摩川":"05",
    "浜名湖":"06","蒲郡":"07","常滑":"08","津":"09",
    "三国":"10","琵琶湖":"11","住之江":"12","尼崎":"13",
    "鳴門":"14","丸亀":"15","児島":"16","宮島":"17","徳山":"18",
    "下関":"19","若松":"20","芦屋":"21","福岡":"22","唐津":"23","大村":"24",
}

@dataclass
class Lane:
    lane: int
    name: str = ""
    nat_win: float = 0.0   # 全国勝率
    loc_win: float = 0.0   # 当地勝率
    motor2: float = 0.0    # モーター2連率(%)
    boat2: float = 0.0     # ボート2連率(%)

def build_racelist_url(place: str, rno: int, ymd: str | None) -> str:
    jcd = JCD.get(place)
    if not jcd:
        raise ValueError("場名が認識できません")
    if not (1 <= rno <= 12):
        raise ValueError("レース番号は1-12で指定してください")
    if not ymd:
        ymd = datetime.now(JST).strftime("%Y%m%d")
    return f"https://www.boatrace.jp/owpc/pc/race/racelist?rno={rno}&jcd={jcd}&hd={ymd}"

def fetch_racelist(place: str, rno: int, ymd: str | None) -> tuple[str, list[Lane]]:
    url = build_racelist_url(place, rno, ymd)
    html = requests.get(url, timeout=10).text
    soup = BeautifulSoup(html, "html.parser")

    lanes: list[Lane] = []
    # 6艇ぶんの行をざっくり走査（テーブル構造差異に強めのパターン）
    for i, tr in enumerate(soup.select("table tbody tr"), start=1):
        if i > 6: break
        t = " ".join(td.get_text(strip=True) for td in tr.select("td"))
        if not t: 
            continue
        lane = Lane(lane=i)

        # 選手名（漢字）らしき最長の日本語ブロックを仮取得
        m_name = re.search(r"[一-龥々〆ヶぁ-んァ-ン]+", t)
        if m_name: lane.name = m_name.group(0)

        # 全国/当地 勝率（例: 6.85 / 7.20）
        m_win = re.search(r"全国勝率[:：]?\s*([0-9.]+).*?当地勝率[:：]?\s*([0-9.]+)", t)
        if m_win:
            lane.nat_win = float(m_win.group(1))
            lane.loc_win = float(m_win.group(2))
        else:
            # 行に「全国勝率」「当地勝率」語が無い体裁の保険
            nums = [float(x) for x in re.findall(r"([0-9]+\.[0-9])", t)]
            if len(nums) >= 2:
                lane.nat_win, lane.loc_win = nums[0], nums[1]

        # モーター/ボート 2連率（xx.x%）
        m_motor = re.search(r"モーター.*?([0-9]+\.?[0-9]?)\s*%", t)
        m_boat  = re.search(r"ボート.*?([0-9]+\.?[0-9]?)\s*%", t)
        if m_motor: lane.motor2 = float(m_motor.group(1))
        if m_boat:  lane.boat2  = float(m_boat.group(1))

        lanes.append(lane)

    # 6行未満だったら、別テーブル体裁の保険（ページ差異対策）
    if len(lanes) < 6:
        rows = soup.select("tr")
        lanes = []
        for i in range(6):
            if i >= len(rows): break
            t = rows[i].get_text(" ", strip=True)
            lane = Lane(lane=i+1)
            m_motor = re.search(r"モーター.*?([0-9]+\.?[0-9]?)\s*%", t)
            m_boat  = re.search(r"ボート.*?([0-9]+\.?[0-9]?)\s*%", t)
            if m_motor: lane.motor2 = float(m_motor.group(1))
            if m_boat:  lane.boat2  = float(m_boat.group(1))
            lanes.append(lane)

    return url, lanes[:6]

def score_lanes(lanes: list[Lane]) -> list[tuple[int, float]]:
    # コース有利度（汎用値）: 1>2>3>4>5>6
    course_bias = {1:0.33, 2:0.19, 3:0.17, 4:0.14, 5:0.10, 6:0.07}

    # 指標（0-100 換算）
    raw = []
    for ln in lanes:
        # 勝率は×10して0-100スケールへ
        power = 0.40*ln.motor2 + 0.20*ln.boat2 + 0.25*(ln.nat_win*10) + 0.15*(ln.loc_win*10)
        raw.append(power)

    mean = statistics.fmean(raw) if raw else 0.0
    stdev = statistics.pstdev(raw) if len(raw) > 1 else 1.0

    scored = []
    for ln, p in zip(lanes, raw):
        z = (p - mean) / (stdev or 1.0)
        base = course_bias.get(ln.lane, 0.1)
        score = base * (1.0 + 0.25*z)  # zの25%だけ増減
        scored.append((ln.lane, score))
    # 高い順
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored

def build_tickets(order: list[int], lanes: list[Lane]):
    # 上位3艇を中心に組む
    top3 = order[:3]
    head = top3[0]

    # 本線（3〜5点）
    main = [
        f"{head}-{top3[1]}-{top3[2]}",
        f"{head}-{top3[2]}-{top3[1]}",
        f"{head}-{top3[1]}-全",
        f"{head}-{top3[2]}-全",
    ]
    main = list(dict.fromkeys(main))[:5]

    # 押さえ（セカンド候補頭）
    sec = order[1]
    sub = [
        f"{sec}-{head}-{top3[2]}",
        f"{sec}-{top3[2]}-{head}",
        f"{head}-全-全",  # 浅めの総流し保険
    ]
    sub = list(dict.fromkeys(sub))[:6]

    # 狙い（外の指数が高い／穴目）
    attack = []
    for ln, _ in zip(order, order):
        if ln >= 4 and ln in order[:4]:
            attack += [f"{ln}-{head}-{order[2]}", f"{ln}-{order[1]}-{head}"]
    attack = list(dict.fromkeys(attack))[:3]

    # 展開コメント
    name = lambda i: (lanes[i-1].name or f"{i}号艇")
    com = []
    com.append("進入想定：枠なり3対3")
    com.append(f"本線は{head}（{name(head)}）。指数上位＋内有利で先マイ本線。")
    com.append(f"相手筆頭は{order[1]}・{order[2]}。外が伸びるなら{attack[0].split('-')[0] if attack else order[3]}の一発ケア。")
    comment = " / ".join(com)

    # 簡易自信度
    spread = order_score_spread(order)
    conf = "A" if spread >= 0.06 else ("B" if spread >= 0.03 else "C")

    return main, sub, attack, comment, conf

def order_score_spread(order_scored: list[int] | list[tuple[int,float]]):
    # scored=[(lane,score),..]を渡された場合の広がり
    if order_scored and isinstance(order_scored[0], tuple):
        scores = [s for _, s in order_scored]
        if len(scores) >= 2:
            return scores[0] - scores[1]
    return 0.0

def predict(place: str, rno: int, ymd: str | None):
    url, lanes = fetch_racelist(place, rno, ymd)
    if len(lanes) < 6:
        return {"ok": False, "message": "出走データを取得できませんでした。", "url": url}

    scored = score_lanes(lanes)          # [(lane,score),..]
    order = [ln for ln, _ in scored]
    main, sub, attack, comment, conf = build_tickets(order, lanes)

    return {
        "ok": True,
        "url": url,
        "main": main,
        "sub": sub,
        "attack": attack,
        "comment": comment,
        "confidence": conf,
        "ranking": order,
    }
