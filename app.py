# biyori.py
import re
import requests
from bs4 import BeautifulSoup

BIYORI_BASE = "https://kyoteibiyori.com/race_shusso.php"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HDRS = {
    "User-Agent": UA,
    "Referer": "https://kyoteibiyori.com/",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

class BiyoriError(Exception): ...
class TableNotFound(BiyoriError): ...

def _get(url: str, timeout=12):
    r = requests.get(url, headers=HDRS, timeout=timeout)
    r.raise_for_status()
    return r.text

def _build_url(place_no: int, race_no: int, hiduke: str, slider: int) -> str:
    return f"{BIYORI_BASE}?place_no={place_no}&race_no={race_no}&hiduke={hiduke}&slider={slider}"

def _clean(t: str) -> str:
    return re.sub(r"\s+", "", t).strip()

def _find_table_with_row_labels(soup: BeautifulSoup, labels: list[str]):
    # ラベル（例：展示/周回/周り足/直線）が含まれる行を持つテーブルを探す
    for tbl in soup.find_all("table"):
        txt = _clean(tbl.get_text(" "))
        if all(lbl in txt for lbl in labels):
            return tbl
    return None

def _row_values(tbl, row_label: str, expected_cols=6):
    # 行先頭に row_label を含む行の数値（6艇分）を返す
    for tr in tbl.find_all("tr"):
        raw = [c.get_text(strip=True) for c in tr.find_all(["th","td"])]
        if not raw:
            continue
        if _clean(raw[0]).startswith(_clean(row_label)):
            vals = raw[1:1+expected_cols]
            # 不足分は None 埋め
            while len(vals) < expected_cols:
                vals.append(None)
            return vals
    return [None]*expected_cols

def fetch_biyori(place_no: int, race_no: int, hiduke: str, slider: int):
    """slider=4(直前) / 9(MyData)。見つからなければ TableNotFound。"""
    url = _build_url(place_no, race_no, hiduke, slider)
    html = _get(url)
    soup = BeautifulSoup(html, "lxml")

    if slider == 4:
        labels = ["展示","周回","周り足","直線"]
        tbl = _find_table_with_row_labels(soup, labels)
        if not tbl:
            raise TableNotFound(f"[biyori] table not found url={url}")
        return {
            "source": "biyori",
            "url": url,
            "slider": 4,
            "tenji": _row_values(tbl, "展示"),
            "shuukai": _row_values(tbl, "周回"),
            "mawariashi": _row_values(tbl, "周り足"),
            "chokusen": _row_values(tbl, "直線"),
        }

    if slider == 9:
        labels = ["平均ST","ST順位"]
        tbl = _find_table_with_row_labels(soup, labels)
        if not tbl:
            raise TableNotFound(f"[biyori] table not found url={url}")
        return {
            "source": "biyori",
            "url": url,
            "slider": 9,
            "avg_st": _row_values(tbl, "平均ST", expected_cols=6),
            "st_rank": _row_values(tbl, "ST順位", expected_cols=6),
        }

    raise ValueError("slider must be 4 or 9")

def fetch_biyori_first_then_fallback(place_no: int, race_no: int, hiduke: str, official_func):
    """直前(4) → MyData(9) の順で日和を試し、ダメなら公式関数 official_func() を呼ぶ。"""
    collected = {}
    errors = []

    for s in (4, 9):
        try:
            data = fetch_biyori(place_no, race_no, hiduke, s)
            collected.update(data)
        except TableNotFound as e:
            errors.append(str(e))
        except Exception as e:
            errors.append(f"[biyori] unexpected: {e}")

    if collected.get("tenji") or collected.get("avg_st"):
        collected["fallback"] = False
        collected["errors"] = errors
        return collected

    # 公式フォールバック
    off_data = official_func(place_no, race_no, hiduke)
    return {
        "source": "official",
        "fallback": True,
        "errors": errors,
        **off_data
    }
