import os
import re
import datetime as dt
from typing import List, Tuple, Dict, Set, DefaultDict
from collections import defaultdict

from flask import Flask, request, abort, jsonify

# ===== LINE v3 SDK =====
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.webhook import WebhookParser
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.exceptions import InvalidSignatureError

# ===== Web / Parse =====
import httpx
from bs4 import BeautifulSoup

# ===== (任意) OpenAI =====
USE_GPT = os.getenv("USE_GPT_NARRATIVE", "false").lower() == "true"
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")
GPT_TEMP = float(os.getenv("GPT_TEMPERATURE", "0.2"))
NARRATIVE_LANG = os.getenv("NARRATIVE_LANG", "ja")

try:
    from openai import OpenAI  # openai>=1.x
    _OPENAI_OK = True
except Exception:
    _OPENAI_OK = False

# ===== LINE tokens =====
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(configuration)
messaging_api = MessagingApi(api_client)
parser = WebhookParser(CHANNEL_SECRET)

app = Flask(__name__)

# ─────────────────────────────────────────
# 会場コード
# ─────────────────────────────────────────
JCD = {
    "桐生":"01","戸田":"02","江戸川":"03","平和島":"04","多摩川":"05",
    "浜名湖":"06","蒲郡":"07","常滑":"08","津":"09","三国":"10",
    "びわこ":"11","住之江":"12","尼崎":"13","鳴門":"14","丸亀":"15",
    "児島":"16","宮島":"17","徳山":"18","下関":"19","若松":"20",
    "芦屋":"21","福岡":"22","唐津":"23","大村":"24",
}
KANA = {
    "きりゅう":"桐生","とだ":"戸田","えどがわ":"江戸川","へいわじま":"平和島","たまがわ":"多摩川",
    "はまなこ":"浜名湖","がまごおり":"蒲郡","とこなめ":"常滑","つ":"津","みくに":"三国",
    "びわこ":"びわこ","すみのえ":"住之江","あまがさき":"尼崎","なると":"鳴門","まるがめ":"丸亀",
    "こじま":"児島","みやじま":"宮島","とくやま":"徳山","しものせき":"下関","わかまつ":"若松",
    "あしや":"芦屋","ふくおか":"福岡","からつ":"唐津","おおむら":"大村",
}

# ─────────────────────────────────────────
# 入力解釈 & shorthand 展開
# ─────────────────────────────────────────
def parse_user_text(text: str):
    s = text.strip().replace("　", " ")
    if "-" in s or "=" in s:
        return {"mode":"shorthand","expr":s}
    parts = [p for p in s.split() if p]
    if not parts: return None
    place_raw = parts[0]
    place = KANA.get(place_raw, place_raw)
    if place not in JCD: return None
    rno = int(parts[1]) if len(parts)>=2 and parts[1].isdigit() else 12
    if len(parts)>=3 and re.fullmatch(r"\d{8}", parts[2]):
        hd = parts[2]
    else:
        jst = dt.datetime.utcnow() + dt.timedelta(hours=9)
        hd = jst.strftime("%Y%m%d")
    return {"mode":"race","place":place,"jcd":JCD[place],"rno":rno,"hd":hd}

DIGITS = set("123456")
def _set_from_token(tok: str) -> List[int]:
    return [int(c) for c in tok if c in DIGITS]

def dedup_trio(trios: List[Tuple[int,int,int]]) -> List[Tuple[int,int,int]]:
    seen=set(); out=[]
    for t in trios:
        if len({t[0],t[1],t[2]})!=3: continue
        if t not in seen:
            seen.add(t); out.append(t)
    return out

def expand_shorthand(expr: str) -> List[Tuple[int,int,int]]:
    s = expr.replace(" ", "")
    if "-" not in s: return []
    tokens = s.split("-")
    out: List[Tuple[int,int,int]] = []
    if len(tokens)==2 and "=" in tokens[1]:
        A=_set_from_token(tokens[0]); m=tokens[1].split("=")
        if len(m)!=2: return []
        fixed=_set_from_token(m[0]); others=_set_from_token(m[1])
        for a in A:
            for f in fixed:
                for o in others:
                    if len({a,f,o})==3:
                        out.append((a,f,o)); out.append((a,o,f))
        return dedup_trio(out)

    if len(tokens)==3:
        A,B,C = tokens
        if "=" in B:
            b1,b2 = (_set_from_token(x) for x in B.split("="))
            A=_set_from_token(A); C=_set_from_token(C)
            for a in A:
                for x in b1:
                    for y in b2:
                        if len({a,x,y})==3:
                            for c in C:
                                if c not in {a,x,y}: out.append((a,x,c)); out.append((a,y,c))
            return dedup_trio(out)
        if "=" in C:
            c1,c2 = (_set_from_token(x) for x in C.split("="))
            A=_set_from_token(A); B=_set_from_token(B)
            for a in A:
                for b in B:
                    for x in c1:
                        if x not in {a,b}: out.append((a,b,x))
                    for y in c2:
                        if y not in {a,b}: out.append((a,b,y))
            return dedup_trio(out)
        A=_set_from_token(A); B=_set_from_token(B); C=_set_from_token(C)
        for a in A:
            for b in B:
                for c in C:
                    if len({a,b,c})==3: out.append((a,b,c))
        return dedup_trio(out)
    return []

def join_trio(trios: List[Tuple[int,int,int]]) -> List[str]:
    return [f"{a}-{b}-{c}" for (a,b,c) in trios]

# ─────────────────────────────────────────
# 集合表記への圧縮（例：1-23-3456）
# ─────────────────────────────────────────
def compress_trios_to_sets(trios: List[Tuple[int,int,int]]) -> List[str]:
    by_a: DefaultDict[int, DefaultDict[int, Set[int]]] = defaultdict(lambda: defaultdict(set))
    for a,b,c in trios:
        by_a[a][b].add(c)
    lines: List[str] = []
    for a, bmap in sorted(by_a.items()):
        inv: DefaultDict[frozenset, List[int]] = defaultdict(list)
        for b, cset in bmap.items():
            inv[frozenset(sorted(cset))].append(b)
        for cset, blist in sorted(inv.items(), key=lambda x: ("".join(map(str,sorted(x[0]))), sorted(x[1]))):
            bset = "".join(map(str, sorted(blist)))
            cset_s = "".join(map(str, sorted(cset)))
            if bset and cset_s:
                lines.append(f"{a}-{bset}-{cset_s}")
    return lines

# ─────────────────────────────────────────
# beforeinfo 取得
# ─────────────────────────────────────────
def beforeinfo_url(jcd: str, rno: int, hd: str) -> str:
    return f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={jcd}&hd={hd}"

http = httpx.Client(headers={
    "User-Agent":"Mozilla/5.0 (Bot; +https://render.com)",
    "Accept-Language":"ja,en;q=0.8",
}, timeout=15)

def fetch_beforeinfo(jcd: str, rno: int, hd: str) -> Dict:
    url = beforeinfo_url(jcd, rno, hd)
    r = http.get(url); r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    tenji_times = {}
    for tr in soup.select("table.is-tableFixed__3rdadd tr"):
        tds = tr.find_all("td")
        if len(tds) >= 7:
            try:
                waku = int(tds[0].get_text(strip=True))
                tenji = tds[-1].get_text(strip=True)
                tenji = float(tenji) if re.fullmatch(r"\d+\.\d", tenji) else None
                if tenji: tenji_times[waku] = tenji
            except:
                pass
    ranking = sorted(tenji_times.items(), key=lambda x: x[1]) if tenji_times else []
    return {"url": url, "tenji_times": tenji_times, "tenji_rank": [w for w,_ in ranking]}

# ─────────────────────────────────────────
# 予想
# ─────────────────────────────────────────
def pick_predictions(info: Dict) -> Dict[str, List[Tuple[int,int,int]]]:
    rank = info.get("tenji_rank", [])
    base1 = rank[0] if len(rank)>=1 else 1
    base2 = rank[1] if len(rank)>=2 else 2
    base3 = rank[2] if len(rank)>=3 else 3

    main = dedup_trio([
        (base1, base2, x) for x in [3,4,5,6] if x not in {base1,base2}
    ] + [
        (base1, base3, x) for x in [2,4,5,6] if x not in {base1,base3}
    ])
    osa = dedup_trio([
        (base2, base1, x) for x in [3,4,5,6] if x not in {base1,base2}
    ] + [
        (base3, base1, x) for x in [2,4,5,6] if x not in {base1,base3}
    ])
    ana = dedup_trio([
        (4,1,x) for x in [2,3,5,6] if x not in {1,4}
    ] + [
        (5,1,x) for x in [2,3,4,6] if x not in {1,5}
    ] + [
        (6,1,x) for x in [2,3,4,5] if x not in {1,6}
    ])

    return {"main": main[:12], "osa": osa[:12], "ana": ana[:12]}

def format_prediction_block(pred: Dict) -> str:
    blocks = []
    for title in ("main","osa","ana"):
        trios = pred.get(title, [])
        if not trios: continue
        label = {"main":"本線","osa":"押え","ana":"穴目"}[title]
        line_sets = compress_trios_to_sets(trios)
        head = f"{label}（{len(trios)}点）"
        blocks.append("\n".join([head] + line_sets))
    return "\n\n".join(blocks)

# ─────────────────────────────────────────
# 叙述
# ─────────────────────────────────────────
def build_narrative(place: str, rno: int, info: Dict) -> str:
    if USE_GPT and _OPENAI_OK and os.getenv("OPENAI_API_KEY"):
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        sys = (f"あなたは競艇の予想コメントを書く解説者。言語は{NARRATIVE_LANG}。"
               "180〜260文字で、具体的根拠（展示上位、進入は内有利前提など）を簡潔に。"
               "同じ語尾の連続を避け、買い目の狙い所を最後にまとめる。")
        user = {
            "place":place, "race":rno,
            "tenji_rank": info.get("tenji_rank", []),
            "policy": "展示上位厚め。1本線、2差し・3まくり差しケア、外は展開待ち。"
        }
        try:
            res = client.chat.completions.create(
                model=GPT_MODEL,
                temperature=GPT_TEMP,
                messages=[{"role":"system","content":sys},
                          {"role":"user","content":f"データ: {user}"}],
                max_tokens=360,
            )
            return res.choices[0].message.content.strip()
        except Exception as e:
            print("[gpt] error:", e)

    rank = info.get("tenji_rank", [])
    top = "展示計測未取得のため内基本線。" if not rank else f"展示上位は{','.join(map(str,rank[:3]))}番。"
    msg = [
        f"{place}{rno}Rの展望。", top,
        "1の先マイ本線。2は差しで内差詰、3はまくり差しの形で怖い。",
        "外は4→5→6の序列。スタ展次第で一撃は4-1型まで。"
    ]
    return " ".join(msg)

# ─────────────────────────────────────────
# LINE Webhook（v3は WebhookParser を使う）
# ─────────────────────────────────────────
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        return "invalid signature", 400
    except Exception as e:
        print("[parse] error:", e); return "error", 400

    for event in events:
        if isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
            handle_text_message(event)
    return "OK"

def handle_text_message(event: MessageEvent):
    text = event.message.text.strip()
    parsed = parse_user_text(text)
    if not parsed:
        reply(event.reply_token,
              "例）「常滑 6 20250812」/「丸亀 9」\n展開だけなら「1-4-235」「45-1=235」「4-12-3」。")
        return

    if parsed["mode"]=="shorthand":
        trios = expand_shorthand(parsed["expr"])
        if not trios:
            reply(event.reply_token, "展開できませんでした。例）1-4-235 / 45-1=235 / 4-12-3")
            return
        line_sets = compress_trios_to_sets(trios)
        out = f"展開（{len(trios)}点）\n" + "\n".join(line_sets)
        reply(event.reply_token, out)
        return

    place, jcd, rno, hd = parsed["place"], parsed["jcd"], parsed["rno"], parsed["hd"]
    try:
        info = fetch_beforeinfo(jcd, rno, hd)
    except Exception as e:
        print("[beforeinfo] fetch error:", e)
        url = beforeinfo_url(jcd, rno, hd)
        reply(event.reply_token, f"📍 {place}{rno}R（{hd}）\n直前情報の取得に失敗。少し待って再試行を。\n（参考: {url}）")
        return

    preds = pick_predictions(info)
    url = info.get("url", beforeinfo_url(jcd, rno, hd))
    nar = build_narrative(place, rno, info)
    header = "――――――――――――――――\n"
    head2 = f"{nar}\n（参考: {url}）\n"
    block = format_prediction_block(preds)
    reply(event.reply_token, f"{header}{head2}\n{block}".strip())

def reply(reply_token: str, text: str):
    try:
        messaging_api.reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text[:4800])])
        )
    except Exception as e:
        print("[reply] error:", e)

# health
@app.get("/healthz")
def healthz(): return jsonify(ok=True)

@app.get("/")
def root(): return "bot alive"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","10000")))
