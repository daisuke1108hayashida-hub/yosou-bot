import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

def fetch_biyori(place_no: str, race_no: str, hiduke: str, slider: str = "4") -> dict | None:
    """ボートレース日和（PC版優先）を取得→最も行数が多いテーブルを抽出。失敗時はNone。"""
    bases = [
        "https://kyoteibiyori.com/pc/race_shusso.php",   # ★PC版（表がサーバー描画）
        "https://kyoteibiyori.com/race_shusso.php",      # フォールバック（通常版）
    ]
    params = f"place_no={place_no}&race_no={race_no}&hiduke={hiduke}&slider={slider}"
    headers = {
        "User-Agent": UA,
        "Accept-Language": "ja,en;q=0.8",
        "Referer": "https://kyoteibiyori.com/",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    for base in bases:
        url = f"{base}?{params}"
        try:
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
        except Exception:
            continue

        soup = BeautifulSoup(r.text, "lxml")

        # いろんな書き方に対応（table / .table / .table-responsive 内の table）
        tables = soup.select("table") or soup.select(".table-responsive table") or []
        if not tables:
            continue

        tb = max(tables, key=lambda t: len(t.find_all("tr")))
        rows = []
        for tr in tb.find_all("tr"):
            cells = [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
            if cells:
                rows.append(cells)

        if rows:
            return {"url": url, "rows": rows, "slider": slider}

    return None
