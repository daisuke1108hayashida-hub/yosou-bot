# app.py  ←この名前で保存（RenderのProcfileは web: gunicorn app:app を想定）
from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)

# ---- 基本ルート ----
@app.get("/")
def root():
    return "yosou-bot OK"

@app.get("/health")
def health():
    return jsonify(status="ok")

@app.get("/__routes")
def list_routes():
    out = []
    for rule in app.url_map.iter_rules():
        methods = ",".join(sorted(m for m in rule.methods if m in {"GET","POST"}))
        out.append(f"{methods:4s}  {rule.rule}")
    return "Available routes:\n" + "\n".join(sorted(out))

# ---- ボートレース日和 取得 & パース ----
def fetch_biyori_html(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    }
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.text

def parse_biyori_table(html: str):
    """日和の出走ページのテーブルをざっくり抽出。
       失敗時は (False, '理由')、成功時は (True, dict情報)
    """
    soup = BeautifulSoup(html, "lxml")
    # たくさん行があり、数字が多いテーブルを優先的に拾う
    candidates = soup.find_all("table")
    best = None
    best_rows = 0
    for tb in candidates:
        rows = tb.find_all("tr")
        if len(rows) >= 6:
            text = tb.get_text(" ", strip=True)
            digits = sum(ch.isdigit() for ch in text)
            score = len(rows) + digits / 50
            if score > best_rows:
                best_rows = score
                best = tb
    if not best:
        return False, "table not found"

    # 先頭2〜3行だけをサマリ化（デバッグ用）
    rows = best.find_all("tr")
    preview = []
    for r in rows[:3]:
        cells = [c.get_text(strip=True) for c in r.find_all(["th","td"])]
        if cells:
            preview.append(" | ".join(cells))
    return True, {"rows": len(rows), "preview": preview}

# ---- デバッグ用：直前/MyData を確認 ----
@app.get("/_debug/biyori")
def debug_biyori():
    place_no = request.args.get("place_no")
    race_no  = request.args.get("race_no")
    hiduke   = request.args.get("hiduke")
    slider   = request.args.get("slider", "4")   # 4=直前, 9=MyData

    if not (place_no and race_no and hiduke):
        return ("missing query: place_no, race_no, hiduke "
                "(+ optional slider=4|9)"), 400

    url = (f"https://kyoteibiyori.com/race_shusso.php?"
           f"place_no={place_no}&race_no={race_no}&hiduke={hiduke}&slider={slider}")

    try:
        html = fetch_biyori_html(url)
    except Exception as e:
        return f"[biyori] fetch error: {e}\nurl={url}", 502

    ok, data = parse_biyori_table(html)
    if not ok:
        return f"[biyori] {data}\nurl={url}", 404

    # 画面で見やすいようにテキストで返す
    lines = [
        f"[biyori] OK slider={slider}",
        f"url={url}",
        f"rows={data['rows']}",
        "preview:",
        *("  - " + p for p in data["preview"])
    ]
    return "\n".join(lines), 200
