import os
import re
import datetime as dt
from typing import List, Tuple, Dict, Set

from flask import Flask, request, abort, jsonify

# ==== LINE v3 SDK ====
from linebot.v3.webhooks import WebhookHandler, MessageEvent, TextMessageContent
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)

# ==== Web / Parse ====
import httpx
from bs4 import BeautifulSoup

# ==== (任意) OpenAI ====
USE_GPT = os.getenv("USE_GPT_NARRATIVE", "false").lower() == "true"
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")
GPT_TEMP = float(os.getenv("GPT_TEMPERATURE", "0.2"))
NARRATIVE_LANG = os.getenv("NARRATIVE_LANG", "ja")

try:
    from openai import OpenAI  # openai>=1.x
    _OPENAI_OK = True
except Exception:
    _OPENAI_OK = False

# ==== LINE tokens ====
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")

if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
    print("[boot] WARNING: LINE env vars missing")

# ==== LINE wiring ====
handler = WebhookHandler(CHANNEL_SECRET)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(configuration)
messaging_api = MessagingApi(api_client)

# ==== Flask ====
app = Flask(__name__)


# ────────────────────────────────────────────────────────────
# 会場コード（jcd）マップ
# ────────────────────────────────────────────────────────────
JCD = {
    "桐生": "01", "戸田": "02", "江戸川": "03", "平和島": "04", "多摩川": "05",
    "浜名湖": "06", "蒲郡": "07", "常滑": "08", "津": "09", "三国": "10",
    "びわこ": "11", "住之江": "12", "尼崎": "13", "鳴門": "14", "丸亀": "15",
    "児島": "16", "宮島": "17", "徳山": "18", "下関": "19", "若松": "20",
    "芦屋": "21", "福岡": "22", "唐津": "23", "大村": "24",
}

# ひらがな・カナ対応
KANA = {k: v for k, v in {
    "きりゅう": "桐生", "とだ": "戸田", "えどがわ": "江戸川", "へいわじま": "平和島", "たまがわ": "多摩川",
    "はまなこ": "浜名湖", "がまごおり": "蒲郡", "とこなめ": "常滑", "つ": "津", "みくに": "三国",
    "びわこ": "びわこ", "すみのえ": "住之江", "あまがさき": "尼崎", "なると": "鳴門", "まるがめ": "丸亀",
    "こじま": "児島", "みやじま": "宮島", "とくやま": "徳山", "しものせき": "下関", "わかまつ": "若松",
    "あしや": "芦屋", "ふくおか": "福岡", "からつ": "唐津", "おおむら": "大村",
}.items()}


# ────────────────────────────────────────────────────────────
# ユーザー入力の解釈
# 例）「常滑 6 20250812」/「丸亀 9」/ shorthand「1-4-235」「45-1=235」「4-12-3」
# ────────────────────────────────────────────────────────────
def parse_user_text(text: str):
    s = text.strip().replace("　", " ")
    # shorthand なら別ルート
    if "-" in s or "=" in s:
        return {"mode": "shorthand", "expr": s}

    parts = [p for p in s.split() if p]
    if not parts:
        return None

    place_raw = parts[0]
    place = KANA.get(place_raw, place_raw)
    if place not in JCD:
        return None

    rno = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 12
    # 日付
    if len(parts) >= 3 and re.fullmatch(r"\d{8}", parts[2]):
        hd = parts[2]
    else:
        jst = dt.datetime.utcnow() + dt.timedelta(hours=9)
        hd = jst.strftime("%Y%m%d")

    return {"mode": "race", "place": place, "jcd": JCD[place], "rno": rno, "hd": hd}


# ────────────────────────────────────────────────────────────
# shorthand → 3連単展開へ展開
# 「1-4-235」/「45-1=235」/「4-12-3」
# - 数字は1〜6のみ
# - 「=」は 2着と3着の入替え許容（例：1=23 は [1,2] / [2,1] 的な2着3着の並べ替え）
# ────────────────────────────────────────────────────────────
DIGITS = set("123456")

def _set_from_token(tok: str) -> List[int]:
    return [int(c) for c in tok if c in DIGITS]

def expand_shorthand(expr: str) -> List[Tuple[int, int, int]]:
    s = expr.replace(" ", "")
    # パターン1：A-B-C
    if "-" in s:
        tokens = s.split("-")
        # 2トークン目または3トークン目に「=」があるときの拡張
        if len(tokens) == 2 and "=" in tokens[1]:
            # 例：45-1=235 → A={4,5}, B=1, C={2,3,5} を2,3着入替え
            A = _set_from_token(tokens[0])
            mid = tokens[1].split("=")
            if len(mid) != 2:
                return []
            fixed = _set_from_token(mid[0])
            others = _set_from_token(mid[1])
            out = []
            for a in A:
                for f in fixed:
                    for o in others:
                        if len({a, f, o}) == 3:
                            out.append((a, f, o))
                            out.append((a, o, f))
            return dedup_trio(out)

        if len(tokens) == 3:
            A, B, C = (_set_from_token(t) for t in tokens)
            out = []
            # BやCに「=」が含まれていたら分解
            if "=" in tokens[1]:
                b1, b2 = ( _set_from_token(x) for x in tokens[1].split("=") )
                for a in A:
                    for x in b1:
                        for y in b2:
                            if len({a, x, y}) == 3:
                                out.append((a, x, y))
                                out.append((a, y, x))
                return dedup_trio(out)

            if "=" in tokens[2]:
                c1, c2 = ( _set_from_token(x) for x in tokens[2].split("=") )
                for a in A:
                    for b in B:
                        for x in c1:
                            for y in c2:
                                if len({a, b, x}) == 3:
                                    out.append((a, b, x))
                                if len({a, b, y}) == 3:
                                    out.append((a, b, y))
                return dedup_trio(out)

            # 通常：直積
            for a in A:
                for b in B:
                    for c in C:
                        if len({a, b, c}) == 3:
                            out.append((a, b, c))
            return dedup_trio(out)

    return []


def dedup_trio(trios: List[Tuple[int, int, int]]) -> List[Tuple[int, int, int]]:
    seen: Set[Tuple[int, int, int]] = set()
    out = []
    for t in trios:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def join_trio(trios: List[Tuple[int, int, int]]) -> List[str]:
    return [f"{a}-{b}-{c}" for (a, b, c) in trios]


# ────────────────────────────────────────────────────────────
# 直前情報の取得（公式 beforeinfo）
# ────────────────────────────────────────────────────────────
def beforeinfo_url(jcd: str, rno: int, hd: str) -> str:
    return f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={jcd}&hd={hd}"

async_client = httpx.Client(headers={
    "User-Agent": "Mozilla/5.0 (Bot; +https://render.com)",
    "Accept-Language": "ja,en;q=0.8",
}, timeout=15)

def fetch_beforeinfo(jcd: str, rno: int, hd: str) -> Dict:
    url = beforeinfo_url(jcd, rno, hd)
    r = async_client.get(url)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # 「展示タイム」と「部品交換」あたりをざっくり抽出（サイト改修に弱いので best-effort）
    tenji_times = {}  # {枠: float}
    for tr in soup.select("table.is-tableFixed__3rdadd tr"):
        tds = tr.find_all("td")
        if len(tds) >= 7:
            try:
                waku = int(tds[0].get_text(strip=True))
                tenji = tds[-1].get_text(strip=True)  # 最右列が展示タイム想定
                tenji = float(tenji) if tenji.replace(".", "", 1).isdigit() else None
                if tenji:
                    tenji_times[waku] = tenji
            except Exception:
                pass

    # ざっくり良し悪し判定（小さい＝良）
    ranking = sorted(tenji_times.items(), key=lambda x: x[1]) if tenji_times else []

    return {
        "url": url,
        "tenji_times": tenji_times,
        "tenji_rank": [w for w, _ in ranking],  # 速い順
    }


# ────────────────────────────────────────────────────────────
# 予想ロジック（簡易）＋ 整形
# ────────────────────────────────────────────────────────────
def pick_predictions(info: Dict) -> Dict[str, List[Tuple[int, int, int]]]:
    # 展示タイムがあればそれを優先、なければ内寄り基本形
    rank = info.get("tenji_rank", [])
    base1 = rank[0] if len(rank) >= 1 else 1
    base2 = rank[1] if len(rank) >= 2 else 2
    base3 = rank[2] if len(rank) >= 3 else 3

    # 本線：1頭 or 展示1位頭
    main = dedup_trio([
        (base1, base2, x) for x in [3,4,5,6] if x not in {base1, base2}
    ] + [
        (base1, base3, x) for x in [2,4,5,6] if x not in {base1, base3}
    ])

    # 押え：2頭筋
    osa = dedup_trio([
        (base2, base1, x) for x in [3,4,5,6] if x not in {base1, base2}
    ] + [
        (base2, base3, x) for x in [1,4,5,6] if x not in {base2, base3}
    ])

    # 穴目：外枠絡み
    ana = dedup_trio([
        (4, 1, x) for x in [2,3,5,6] if x not in {1,4}
    ] + [
        (5, 1, x) for x in [2,3,4,6] if x not in {1,5}
    ] + [
        (6, 1, x) for x in [2,3,4,5] if x not in {1,6}
    ])

    return {"main": main[:8], "osa": osa[:8], "ana": ana[:8]}  # 各最大8点にトリム


def format_prediction_block(pred: Dict) -> str:
    def lines(title: str, items: List[Tuple[int,int,int]]) -> List[str]:
        if not items:
            return []
        return [f"{title}（{len(items)}点）"] + join_trio(items)

    out = []
    out += lines("本線", pred.get("main", []))
    out += [""]
    out += lines("押え", pred.get("osa", []))
    out += [""]
    out += lines("穴目", pred.get("ana", []))
    return "\n".join([line for line in out if line is not None])


# ────────────────────────────────────────────────────────────
# 生成AI 叙述文（任意）
# ────────────────────────────────────────────────────────────
def build_narrative(place: str, rno: int, info: Dict) -> str:
    # OpenAI 連携オフ or ライブラリ未導入なら簡易文
    if not (USE_GPT and _OPENAI_OK and os.getenv("OPENAI_API_KEY")):
        rank = info.get("tenji_rank", [])
        lead = f"{place}{rno}Rの展望。"
        if rank:
            lead += f" 展示タイム上位は{rank[:3]}番の順。上位枠からの押し切りを本線に。"
        else:
            lead += " 内有利の傾向。スタート一定なら1→2,3本線。"
        return lead

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    sys = f"あなたは競艇の予想コメントを書くアナリストです。出力言語は{NARRATIVE_LANG}。150～220文字で、同じ言い回しを避け、根拠を簡潔に。"
    user = {
        "place": place, "race": rno,
        "tenji_rank": info.get("tenji_rank", []),
        "note": "展示が無い場合は内有利の一般論で可"
    }
    try:
        res = client.chat.completions.create(
            model=GPT_MODEL,
            temperature=GPT_TEMP,
            messages=[
                {"role":"system","content":sys},
                {"role":"user","content":f"データ: {user}"}
            ],
            max_tokens=320,
        )
        return res.choices[0].message.content.strip()
    except Exception as e:
        print(f"[gpt] error: {e}")
        return "内有利を前提に、展示気配次第で2・3の差し／まくり差しに注意。"


# ────────────────────────────────────────────────────────────
# LINE webhook
# ────────────────────────────────────────────────────────────
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        print("[/callback] handle error:", e)
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    text = event.message.text.strip()

    parsed = parse_user_text(text)
    if not parsed:
        reply(event.reply_token, "「常滑 6 20250812」のように送ってください。短縮記法は「1-4-235」「45-1=235」「4-12-3」に対応。")
        return

    # 短縮記法の展開だけ欲しい場合
    if parsed["mode"] == "shorthand":
        trios = expand_shorthand(parsed["expr"])
        if not trios:
            reply(event.reply_token, "展開できませんでした。例）1-4-235 / 45-1=235 / 4-12-3")
            return
        text_out = "展開（{}点）\n{}".format(len(trios), "\n".join(join_trio(trios)))
        reply(event.reply_token, text_out)
        return

    # レース情報の取得→予想
    place, jcd, rno, hd = parsed["place"], parsed["jcd"], parsed["rno"], parsed["hd"]
    info = {}
    try:
        info = fetch_beforeinfo(jcd, rno, hd)
    except Exception as e:
        print("[beforeinfo] fetch error:", e)
        # 公式URLだけ返す
        url = beforeinfo_url(jcd, rno, hd)
        msg = f"📍 {place} {rno}R（{hd}）\n直前情報の取得に失敗しました。時間をおいて再試行してください。\n(参考: {url})"
        reply(event.reply_token, msg)
        return

    preds = pick_predictions(info)
    url = info.get("url", beforeinfo_url(jcd, rno, hd))
    nar = build_narrative(place, rno, info)

    header = "ーーーーーーーーーーーーーー\n＿＿＿＿\n"
    head2 = f"{place}{rno}Rの展望。{nar}\n（参考: {url}）\n"
    block = format_prediction_block(preds)
    out = f"{header}{head2}\n{block}".strip()
    reply(event.reply_token, out)


def reply(reply_token: str, text: str):
    try:
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text[:4800])]
            )
        )
    except Exception as e:
        print("[reply] error:", e)


# ────────────────────────────────────────────────────────────
# Health & debug
# ────────────────────────────────────────────────────────────
@app.get("/healthz")
def healthz():
    return jsonify(ok=True)

@app.get("/")
def root():
    return "bot alive"


if __name__ == "__main__":
    # ローカル実行用
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
