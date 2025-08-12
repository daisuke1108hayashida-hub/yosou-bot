# -*- coding: utf-8 -*-
import re
from datetime import datetime
from typing import Optional, Tuple

PLACE_MAP = {
    "桐生":1,"戸田":2,"江戸川":3,"平和島":4,"多摩川":5,"浜名湖":6,"蒲郡":7,"常滑":8,
    "津":9,"三国":10,"琵琶湖":11,"住之江":12,"尼崎":13,"鳴門":14,"丸亀":15,"児島":16,
    "宮島":17,"徳山":18,"下関":19,"若松":20,"芦屋":21,"福岡":22,"唐津":23,"大村":24,
    "まるがめ":15,"丸ガメ":15,"MARUGAME":15,
}

def _normalize_date(s: str) -> Optional[str]:
    s = re.sub(r"[^\d]", "", s or "")
    if len(s) == 8:
        try:
            datetime.strptime(s, "%Y%m%d")
            return s
        except:
            return None
    if len(s) == 8 and s[4:6] in {"80","90"}:
        ss = s[:4] + s[6:7] + s[5:6] + s[7:]
        try:
            datetime.strptime(ss, "%Y%m%d")
            return ss
        except:
            pass
    return None

def parse_free_text(text: str) -> Optional[Tuple[int,int,str]]:
    """
    例)
      丸亀 11 20250812
      丸亀 11R 2025-08-12
      2025/08/12 丸亀 11
    -> (place_no, race_no, yyyymmdd)
    """
    t = (text or "").strip()
    place_no = None
    for name, no in PLACE_MAP.items():
        if re.search(name, t, re.IGNORECASE):
            place_no = no; break
    m_r = re.search(r"\b(\d{1,2})\s*R?\b", t, re.IGNORECASE)
    race_no = int(m_r.group(1)) if m_r else None
    if race_no is not None and not (1 <= race_no <= 12):
        race_no = None
    m_d = re.search(r"(\d{4}[^\d]?\d{2}[^\d]?\d{2})", t)
    date = _normalize_date(m_d.group(1)) if m_d else None
    if place_no and race_no and date:
        return (place_no, race_no, date)
    return None
