import os
import re
import math
import datetime as dt
from typing import List, Dict, Tuple, Set

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, abort

# ==== LINE v3 SDK ====
from linebot.v3 import WebhookParser
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)

# ==== GPT (任意) ====
USE_GPT = os.getenv("USE_GPT_NARRATIVE", "false").lower() == "true"
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")
GPT_TEMP = float(os.getenv("GPT_TEMPERATURE", "0.2"))
NARRATIVE_LANG = os.getenv("NARRATIVE_LANG", "ja")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if USE_GPT and OPENAI_API_KEY:
    import openai
    openai.api_key = OPENAI_API_KEY


# -------------------------
# 基本設定
# -------------------------
LINE_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

configuration = Configuration(access_token=LINE_TOKEN)
api_client = ApiClient(configuration)
line_api = MessagingApi(api_client)
parser = WebhookParser(LINE_SECRET)

app = Flask(__name__)

# 場コード（jcd）
JCD = {
    "桐生": "01", "戸田": "02", "江戸川": "03", "平和島": "04", "多摩川": "05",
    "浜名湖": "06", "蒲郡": "07", "常滑": "08", "津": "09", "三国": "10",
    "びわこ": "11", "住之江": "12", "尼崎": "13", "鳴門": "14", "丸亀": "15",
    "児島": "16", "宮島": "17", "徳山": "18", "下関": "19", "若松": "20",
    "芦屋": "21", "福岡": "22", "唐津": "23", "大村": "24"
}

HEAD = {
    "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/125 Safari/537.36"
}

# -------------------------
# ユーティリティ
# -------------------------
def today_yyyymmdd(tz="Asia/Tokyo"):
    JST = dt.timezone(dt.timedelta(hours=9))
    return dt.datetime.now(JST).strftime("%Y%m%d")

def build_beforeinfo_url(jcd: str, rno: int, ymd: str) -> str:
    return f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={jcd}&hd={ymd}"

def safe_get(url: str) -> str:
    r = requests.get(url, headers=HEAD, timeout=12)
    r.raise_for_status()
    return r.text

def parse_user_text(text: str):
    """
    入力例:
      '常滑 8 20250812'
      '浜名湖 10'
      '08 6 20250812' など数字場も可
    """
    t = text.strip().replace("　", " ")
    parts = [p for p in re.split(r"\s+", t) if p]
    if len(parts) < 2:
        raise ValueError("入力は『場名 レース番号 [YYYYMMDD]』の形式でお願いします。")

    place_raw, race_raw = parts[0], parts[1]
    if place_raw in JCD:
        jcd = JCD[place_raw]
        place = place_raw
    elif re.fullmatch(r"\d{2}", place_raw):
        jcd = place_raw
        place = [k for k, v in JCD.items() if v == jcd]
        place = place[0] if place else place_raw
    else:
        # 前方一致でも拾う
        hit = [k for k in JCD if k.startswith(place_raw)]
        if not hit:
            raise ValueError("場名が認識できません。")
        place = hit[0]; jcd = JCD[place]

    rno = int(re.sub(r"\D", "", race_raw))
    ymd = parts[2] if len(parts) >= 3 else today_yyyymmdd()
    if not re.fullmatch(r"\d{8}", ymd):
        raise ValueError("日付はYYYYMMDDで指定してください。")

    return place, jcd, rno, ymd

# -------------------------
# スクレイプ & スコアリング
# -------------------------
def scrape_beforeinfo(jcd: str, rno: int, ymd: str) -> Dict:
    """
    beforeinfo から展示タイム、ST近辺、進入枠、FL情報などを可能な範囲で取得。
    ページ側の構成変化に強いよう、見出しのキーワードで探す。
    戻り値: dict { boat_no: {...指標...} }
    """
    url = build_beforeinfo_url(jcd, rno, ymd)
    html = safe_get(url)
    soup = BeautifulSoup(html, "lxml")  # HTMLとして扱う

    data = {i: {} for i in range(1, 7)}

    # 展示タイム
    # 見出し「展示タイム」「直前情報」に近い table をざっくり探索
    def try_float(x):
        try:
            return float(x)
        except:
            return None

    # テーブルの数値を総当たりで拾っていく（多少強引だが堅牢）
    for tbl in soup.find_all("table"):
        th_text = " ".join(th.get_text(strip=True) for th in tbl.find_all("th"))
        td_text = " ".join(td.get_text(strip=True) for td in tbl.find_all("td"))
        context = th_text + " " + td_text

        # 展示
        if "展示" in context and "タイム" in context:
            rows = tbl.find_all("tr")
            for tr in rows:
                tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(tds) >= 2:
                    # 先頭が号艇 or 枠
                    no = re.sub(r"\D", "", tds[0])
                    if no.isdigit():
                        b = int(no)
                        # 数値らしきものを抽出して最小を採用（xx.xx）
                        nums = [try_float(x.replace("−", "-")) for x in tds[1:]]
                        nums = [n for n in nums if n is not None and 4.0 < n < 9.0]
                        if nums:
                            data[b]["exh"] = min(nums)

        # ST近辺
        if "ST" in context and ("コンマ" in context or "近辺" in context or "タイミング" in context):
            # 号艇順に ST値が並ぶ table が多いので、0.0x の値を順に拾う
            st_nums = re.findall(r"0\.\d{2}", context)
            if len(st_nums) >= 6:
                for i in range(6):
                    v = float(st_nums[i])
                    data[i+1]["st"] = v

        # 進入想定（枠）…beforeinfoは基本枠なりだが、入れ替えがあれば記載される
        if "進入" in context and ("コース" in context or "枠" in context):
            # 明確に拾えなければ枠なり
            pass

        # F/L
        if "F" in context or "L" in context or "フライング" in context:
            # 「F1」「FL」等をざっくり拾う
            f_words = re.findall(r"F\d|FL|L\d", context)
            if f_words:
                # 見つかった場合は一律リスク+（どの艇かまで割り当て不能なことが多いので全体軽微）
                for i in range(1,7):
                    data[i]["fl_flag"] = True

    # 足りないところは None を入れておく
    for i in range(1,7):
        data[i].setdefault("exh", None)
        data[i].setdefault("st", None)
        data[i].setdefault("fl_flag", False)

    return {"url": build_beforeinfo_url(jcd, rno, ymd), "boats": data}


def score_boats(info: Dict) -> Dict[int, float]:
    """
    単純化したスコアリング:
      ・枠有利: 1>2>3>4>5>6
      ・展示タイム: 良いほど加点
      ・ST: 早いほど加点（0.10基準）
      ・F/Lがページに見えたら少し減点
    """
    lane_bias = {1: 25, 2: 15, 3: 8, 4: 2, 5: -2, 6: -5}
    boats = info["boats"]
    # 正規化用
    exh_vals = [v["exh"] for v in boats.values() if v["exh"]]
    st_vals  = [v["st"]  for v in boats.values() if v["st"]]

    min_exh = min(exh_vals) if exh_vals else None
    max_exh = max(exh_vals) if exh_vals else None
    ref_st = 0.10

    score = {}
    for b in range(1,7):
        s = lane_bias[b]

        # 展示
        ev = boats[b]["exh"]
        if ev and min_exh and max_exh and max_exh > min_exh:
            # 速いほど +5 まで
            norm = (max_exh - ev) / (max_exh - min_exh)
            s += 5 * norm

        # ST
        st = boats[b]["st"]
        if st:
            s += max(0, 6 - (st - ref_st)*100) * 0.2  # 0.10に近いほど加点（ゆるい）

        if boats[b]["fl_flag"]:
            s -= 1.0

        score[b] = s

    return score

# -------------------------
# 3連単候補生成と「まとめ表記」
# -------------------------
def unique_triples(triples: List[Tuple[int,int,int]]) -> List[Tuple[int,int,int]]:
    seen = set()
    out = []
    for a,b,c in triples:
        if a==b or b==c or a==c:  # 同着禁止
            continue
        key = (a,b,c)
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out

def expand_groups(first: Set[int], seconds: Set[int], thirds: Set[int]) -> List[Tuple[int,int,int]]:
    triples = []
    for f in first:
        for s in seconds:
            for t in thirds:
                triples.append((f,s,t))
    return unique_triples(triples)

def format_group(fset: Set[int], sset: Set[int], tset: Set[int]) -> str:
    a = "".join(str(i) for i in sorted(fset))
    b = "".join(str(i) for i in sorted(sset))
    c = "".join(str(i) for i in sorted(tset))
    return f"{a}-{b}-{c}"

def compress_triples(first: int, triples: List[Tuple[int,int,int]]) -> List[str]:
    """
    与えられた first について、(second, third) の存在行列から
    “完全な長方形”を貪欲に抜き出して 1-23-3456 のように圧縮。
    残りは個別 1-2-3 で出す。
    """
    # second, third の集合
    pairs = [(b,c) for (a,b,c) in triples if a == first]
    if not pairs:
        return []

    seconds = sorted(set(b for b,_ in pairs))
    thirds  = sorted(set(c for _,c in pairs))

    # 存在表
    has = {(b,c) for b,c in pairs}

    used = set()
    lines = []

    remain_pairs = set(pairs)

    while remain_pairs:
        # 度数が高い second を起点に最大共通 third 群を探す
        deg = {}
        for b in seconds:
            deg[b] = sum((b,c) in remain_pairs for c in thirds)
        base = max(seconds, key=lambda x: deg.get(x,0))
        if deg.get(base,0) <= 1:
            break

        # base を含む seconds の候補（共通 third が2つ以上になる範囲）
        cand_seconds = [b for b in seconds if b!=base and any((b,c) in remain_pairs for c in thirds)]
        group_seconds = {base}
        common_thirds = {c for c in thirds if (base,c) in remain_pairs}

        for b in cand_seconds:
            bt = {c for c in thirds if (b,c) in remain_pairs}
            inter = common_thirds & bt
            if len(inter) >= 2:
                group_seconds.add(b)
                common_thirds = inter

        if len(group_seconds) >= 2 and len(common_thirds) >= 2:
            # 長方形として採用
            for b in list(group_seconds):
                for c in list(common_thirds):
                    remain_pairs.discard((b,c))
            lines.append(format_group({first}, set(group_seconds), set(common_thirds)))
        else:
            # 長方形にならない → 個別に一つ吐く
            b,c = next(iter(remain_pairs))
            remain_pairs.remove((b,c))
            lines.append(f"{first}-{b}-{c}")

    # 残りを個別で
    for b,c in sorted(remain_pairs):
        lines.append(f"{first}-{b}-{c}")

    return lines

def make_ticket_sets(scores: Dict[int, float]):
    """
    スコア上位から本線/押え/穴 の“候補のかたまり”を返す。
    1着は基本1枠寄り、対抗に2-3、3着に残り といった型にしつつ
    スコアで動的に入替。
    """
    ranks = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top = [r[0] for r in ranks]  # 号艇順位

    # ざっくりロジック
    fav = top[0]
    seconds = set(top[1:3])  # 2頭目候補
    thirds  = set(i for i in range(1,7)) - {fav}  # 3着候補

    main = expand_groups({fav}, seconds, thirds)

    # 押さえ：2頭目に fav を外して、fav を2着固定も混ぜる
    sub1 = expand_groups(seconds, {fav}, thirds - seconds)
    sub2 = expand_groups({fav}, {top[3]}, thirds)  # 3番手を2着へ
    osa = unique_triples(sub1 + sub2)

    # 穴：4-5軸/センター軸を持った塊
    hole_first = set(top[3:5])  # 4番手,5番手
    hole_sec   = set(top[:3])   # 上位を2着に
    hole_third = set(range(1,7)) - hole_first
    ana = expand_groups(hole_first, hole_sec, hole_third)

    # 圧縮文字列へ
    main_lines = compress_triples(fav, main)
    osa_lines  = sorted(set(sum((compress_triples(x, osa) for x in hole_sec|{fav}), [])), key=lambda s:s)
    ana_lines  = sorted(set(sum((compress_triples(x, ana) for x in hole_first), [])), key=lambda s:s)

    return main_lines, osa_lines, ana_lines

# -------------------------
# 文章生成（GPT 任意）
# -------------------------
def build_plain_narrative(place, rno, info, scores) -> str:
    def f(x): return f"{x:.02f}" if x is not None else "-"
    b = info["boats"]
    # 展示/Ｓの簡易表
    rows = []
    for i in range(1,7):
        rows.append(f"{i}: 展示{f(b[i]['exh'])} / ST{f(b[i]['st'])}")
    table = " / ".join(rows)

    tops = sorted(scores.items(), key=lambda x:x[1], reverse=True)
    lead = f"【{place} {rno}Rの展望】イン優勢寄り。"
    if tops[0][0] != 1:
        lead = f"【{place} {rno}Rの展望】枠なりでも{tops[0][0]}号艇が機力上位で主役。"
    txt = (
        f"{lead} 展示やST傾向から総合力をスコア化。"
        f" 上位は {', '.join(str(k) for k,_ in tops[:3])} 。"
        f" 直前指標: {table}"
    )
    return txt

def build_gpt_narrative(place, rno, info, scores) -> str:
    if not (USE_GPT and OPENAI_API_KEY):
        return build_plain_narrative(place, rno, info, scores)

    b = info["boats"]
    def val(x): return "-" if x is None else f"{x:.02f}"

    context = []
    for i in range(1,7):
        context.append(
            f"{i}号艇: 展示タイム={val(b[i]['exh'])}, ST近辺={val(b[i]['st'])}, "
            f"FLリスク={'有' if b[i]['fl_flag'] else '無'}"
        )
    tops = sorted(scores.items(), key=lambda x:x[1], reverse=True)

    sys = (
        "あなたはボートレースの予想コメントを作るアナリストです。"
        "専門用語は使いすぎず、要点を3〜6文で、読みやすい日本語で。"
        "『固い/波乱/センターの仕掛け/まくり差し警戒』などの表現は自然に。"
    )
    usr = (
        f"レース: {place} {rno}R\n"
        f"指標:\n" + "\n".join(context) + "\n\n"
        f"スコア上位: " + ", ".join(f"{k}:{round(v,1)}" for k,v in tops[:6]) + "\n"
        "これを踏まえて、簡潔だが少し踏み込んだ展開予想文を書いて。"
    )

    try:
        res = openai.ChatCompletion.create(
            model=GPT_MODEL,
            temperature=GPT_TEMP,
            messages=[
                {"role":"system","content":sys},
                {"role":"user","content":usr}
            ]
        )
        return res.choices[0].message.content.strip()
    except Exception as e:
        # 失敗時はプレーン文
        return build_plain_narrative(place, rno, info, scores)

# -------------------------
# 返信メッセージ整形
# -------------------------
def build_reply(place, jcd, rno, ymd) -> str:
    info = scrape_beforeinfo(jcd, rno, ymd)
    scores = score_boats(info)

    main, osa, ana = make_ticket_sets(scores)
    url = info["url"]

    nar = build_gpt_narrative(place, rno, info, scores)

    def section(title, lines):
        if not lines:
            return ""
        return f"\n{title}\n" + "\n".join(lines)

    text = (
        "――――――――――――――\n"
        f"{nar}\n"
        f"(参考: {url})\n"
        f"{section('本線', main)}"
        f"{section('押え', osa)}"
        f"{section('穴目', ana)}"
    ).strip()

    return text

# -------------------------
# LINE Webhook
# -------------------------
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        events = parser.parse(body, signature)
    except Exception:
        abort(400)

    for ev in events:
        # Text only
        if ev.type == "message" and getattr(ev.message, "type", "") == "text":
            text = ev.message.text
            try:
                place, jcd, rno, ymd = parse_user_text(text)
                reply = build_reply(place, jcd, rno, ymd)
            except Exception as e:
                reply = (
                    "入力例:『常滑 8 20250812』/『浜名湖 10』\n"
                    f"error: {e}"
                )

            line_api.reply_message(
                ReplyMessageRequest(
                    reply_token=ev.reply_token,
                    messages=[TextMessage(text=reply)]
                )
            )

    return "OK"

# ヘルスチェック
@app.route("/")
def index():
    return "ok"
