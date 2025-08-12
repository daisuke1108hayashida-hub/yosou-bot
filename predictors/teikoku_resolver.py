# -*- coding: utf-8 -*-
"""
艇国DB内で /race/<数字> のURLを推定する軽量リゾルバ。
戦略:
  1) 入力が /race/<digits> を含めば即採用
  2) それ以外の boatrace-db.net ページから a[href*='/race/\\d+'] を探索（1〜2ホップ）
  3) テキストに '11R' 等があれば優先
※ 3秒インターバル順守
"""
import re
import time
import requests
from bs4 import BeautifulSoup
from typing import Optional

_MIN_INTERVAL = 3.1
_last = 0.0
def _wait():
    global _last
    dt = time.time() - _last
    if dt < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - dt)
    _last = time.time()

URL_NUMERIC  = re.compile(r"https?://(?:www\.)?boatrace-db\.net/race/\d+/?$", re.I)
URL_ANY_DB   = re.compile(r"https?://(?:www\.)?boatrace-db\.net/[^\s]+", re.I)

def _fetch(url: str) -> Optional[BeautifulSoup]:
    _wait()
    try:
        r = requests.get(url, headers={"User-Agent": "yosou-bot/1.0"}, timeout=15)
        if r.status_code != 200:
            return None
        r.encoding = r.apparent_encoding
        return BeautifulSoup(r.text, "html.parser")
    except Exception:
        return None

def _abs(href: str) -> str:
    if href.startswith("http"):
        return href
    return "https://boatrace-db.net" + (href if href.startswith("/") else "/" + href)

def _pick_race_link(soup: BeautifulSoup, race_no_pref: Optional[int]) -> Optional[str]:
    anchors = soup.select("a[href]")
    cands = []
    for a in anchors:
        href = a.get("href", "")
        if re.search(r"/race/\d+/?$", href):
            text = a.get_text(strip=True)
            cands.append((_abs(href), text))
    if not cands:
        return None
    if race_no_pref is not None:
        for url, text in cands:
            if re.search(fr"\b{race_no_pref}\s*R\b", text):
                return url
    cands.sort(key=lambda it: 1 if re.search(r"\b\d{1,2}\s*R\b", it[1]) else 0, reverse=True)
    return cands[0][0]

def resolve_from_any_db_page(src_url: str, race_no_pref: Optional[int]) -> Optional[str]:
    soup = _fetch(src_url)
    if not soup:
        return None
    link = _pick_race_link(soup, race_no_pref)
    if link:
        return link
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if "race" in href and not href.startswith("#"):
            s2 = _fetch(_abs(href))
            if not s2:
                continue
            l2 = _pick_race_link(s2, race_no_pref)
            if l2:
                return l2
    return None
