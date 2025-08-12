# -*- coding: utf-8 -*-
"""
Official BOAT RACE downloader (B=番組表, K=競走成績, Fan=期別成績)
- 公式の「各種ダウンロード」を使用（LZH配布）
- サーバ配慮で 3秒インターバル
- LZHを解凍して .txt を保存（文字コードは cp932 を想定）
- B/Kはまず「DL→解凍→生TXT保存」までを確実実装
- Fan手帳は LZH→TXT を束ねてCSV化の雛形を同梱（公式レイアウト参照）
"""
import os
import io
import sys
import time
import glob
import shutil
import zipfile
import argparse
from datetime import datetime, timedelta
from typing import List, Tuple, Optional

import requests
from lhafile import LhaFile
import pandas as pd

BASE_B = "https://www1.mbrace.or.jp/od2/B"   # 番組表ダウンロード（公式導線）  [oai_citation:6‡www1.mbrace.or.jp](https://www1.mbrace.or.jp/od2/B/dindex.html)
BASE_K = "https://www1.mbrace.or.jp/od2/K"   # 競走成績ダウンロード（公式導線）  [oai_citation:7‡www1.mbrace.or.jp](https://www1.mbrace.or.jp/od2/K/dindex.html)

MIN_INTERVAL = 3.1
_last = 0.0

def _wait():
    global _last
    dt = time.time() - _last
    if dt < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - dt)
    _last = time.time()

def yyyymmdd_seq(start: str, end: str) -> List[str]:
    """YYYYMMDD（含む）で日付列を返す"""
    sd = datetime.strptime(start, "%Y%m%d")
    ed = datetime.strptime(end, "%Y%m%d")
    out = []
    d = sd
    while d <= ed:
        out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out

def build_lzh_url(kind: str, yyyymmdd: str) -> Tuple[str, str]:
    """
    kind: 'B' or 'K'
    URLルール: .../od2/{B|K}/YYYYMM/{b|k}YYMMDD.lzh
    """
    assert kind in ("B","K")
    yyyymm = yyyymmdd[:6]
    yymmdd = yyyymmdd[2:]
    prefix = 'b' if kind == 'B' else 'k'
    base = BASE_B if kind == 'B' else BASE_K
    url = f"{base}/{yyyymm}/{prefix}{yymmdd}.lzh"
    filename = f"{prefix}{yymmdd}.lzh"
    return url, filename

def http_get(url: str) -> Optional[bytes]:
    _wait()
    headers = {"User-Agent": "official-dl/1.0 (+respecting-interval)"}
    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code == 200:
            return r.content
        return None
    except Exception:
        return None

def save_binary(path: str, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)

def extract_lzh(lzh_path: str, out_dir: str) -> List[str]:
    """LZHを解凍してTXTファイルを out_dir へ。戻り値は展開されたTXTのパス一覧"""
    os.makedirs(out_dir, exist_ok=True)
    out_files = []
    with LhaFile(lzh_path) as lha:
        for name in lha.namelist():
            data = lha.read(name)
            # ファイル名の正規化（サブフォルダは平坦化）
            fname = os.path.basename(name)
            dst = os.path.join(out_dir, fname)
            with open(dst, "wb") as f:
                f.write(data)
            out_files.append(dst)
    return out_files

def dump_txt_as_utf8(src_txt: str, dst_txt: str):
    """cp932想定のTXTをUTF-8に正規化して保存（混在時はそのまま）"""
    raw = open(src_txt, "rb").read()
    try:
        s = raw.decode("cp932", errors="strict")
    except Exception:
        s = raw.decode("cp932", errors="ignore")
    os.makedirs(os.path.dirname(dst_txt), exist_ok=True)
    with open(dst_txt, "w", encoding="utf-8", newline="\n") as f:
        f.write(s)

def download_bk_range(kind: str, start: str, end: str, save_dir: str) -> List[str]:
    """
    B/K の LZH を日付範囲で取得 → {save_dir}/lzh/{B|K}/ に保存
    取得済みはスキップ
    """
    kind = kind.upper()
    assert kind in ("B","K")
    lzh_out = os.path.join(save_dir, "lzh", kind)
    os.makedirs(lzh_out, exist_ok=True)
    saved = []
    for ymd in yyyymmdd_seq(start, end):
        url, fname = build_lzh_url(kind, ymd)
        dst = os.path.join(lzh_out, fname)
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            print(f"[SKIP] {fname}")
            saved.append(dst); continue
        bin_ = http_get(url)
        if bin_:
            save_binary(dst, bin_)
            print(f"[OK] {url}")
            saved.append(dst)
        else:
            print(f"[NG] {url}")
    return saved

def extract_all_lzh(save_dir: str) -> List[str]:
    """
    lzh/ 以下にある全LZHを解凍 → txt_raw/{B|K|FAN}/YYYYMMDD/ に格納
    戻り値: 展開されたTXTのパス一覧
    """
    txt_paths = []
    for kind in ("B","K","FAN"):
        for lzh in glob.glob(os.path.join(save_dir, "lzh", kind, "*.lzh")):
            base = os.path.basename(lzh)
            # bYYMMDD.lzh / kYYMMDD.lzh / fanXXXX.lzh
            if kind in ("B","K"):
                yymmdd = base[1:7]
                ymd = "20" + yymmdd  # 1996年以前に触る場合は調整
                out_dir = os.path.join(save_dir, "txt_raw", kind, ymd)
            else:
                out_dir = os.path.join(save_dir, "txt_raw", kind, base.replace(".lzh",""))
            try:
                files = extract_lzh(lzh, out_dir)
                txt_paths.extend(files)
            except Exception as e:
                print(f"[ERR] extract {base}: {e}")
    return txt_paths

# --- Fan手帳（期別成績） ----------------------------------------------

FAN_INDEX = "https://www.boatrace.jp/owpc/pc/extra/data/download.html"  # 公式導線（年・前期/後期）  [oai_citation:8‡ボートレース](https://www.boatrace.jp/owpc/pc/extra/data/download.html)

def build_fan_url(yyqq: str) -> Tuple[str, str]:
    """
    yyqq: 例 '2504' = 2025年 前期, '2510' = 2025年 後期
    実ファイル名は "fan{yyqq}.lzh"（通例）
    """
    # 近年の命名は fanYYQQ.lzh（QQは04/10）。念のため失敗時はユーザーにファイル名現物を指定してもらう想定
    fname = f"fan{yyqq}.lzh"
    # 置き場所は公式導線からの直リンクになるが、年ごとにURL直下が変わる可能性があるため、
    # まずは「ダウンロード・他」から手動で1つ取得して、そこからのベースURLを教えてもらうのが確実。
    # ここでは便宜上、過去の一般例に合わせて /owpc/pc/extra/data/ 配下想定はせず、ユーザー入力を推奨。
    return fname, fname  # URLは手動指定を推奨（安全運用）

def parse_fan_txts_to_csv(txt_dir: str, out_csv: str):
    """
    Fan手帳TXT群→CSV（最小サンプル）
    - 公式レイアウトは固定長。ここでは代表的項目のみを切り出す雛形。
    - 本格運用ではレイアウト全項目分の幅テーブルを拡張してください。  [oai_citation:9‡ボートレース](https://www.boatrace.jp/owpc/pc/extra/data/layout.html)
    """
    rows = []
    for txt in sorted(glob.glob(os.path.join(txt_dir, "*.TXT")) + glob.glob(os.path.join(txt_dir, "*.txt"))):
        raw = open(txt, "rb").read()
        try:
            s = raw.decode("cp932", errors="strict")
        except Exception:
            s = raw.decode("cp932", errors="ignore")
        for line in s.splitlines():
            # ★超簡易：固定幅の先頭帯だけ切り出す雛形（登番, 名前漢字, 支部, 級, 勝率, 複勝率, 平均ST）
            # 公式レイアウトの「順」による概算。実環境で幅を微調整してください。  [oai_citation:10‡ボートレース](https://www.boatrace.jp/owpc/pc/extra/data/layout.html)
            # 例示幅（バイト基準→全角混在により見かけ幅とズレる可能性あり）
            try:
                # バイトで固定幅切り出し
                b = line.encode("cp932", errors="ignore")
                # 幅テーブル（例示）
                # 登番4 / 名前漢字16 / 名前カナ15 / 支部4 / 級2 / 年号1 / 生年月日6 / 性別1 / 年齢2 / 身長3 / 体重2 / 血液型2
                # 勝率4 / 複勝率4 / 1着3 / 2着3 / 出走3 / 優出2 / 優勝2 / 平均ST3 ...
                cuts = [4,16,15,4,2,1,6,1,2,3,2,2,4,4,3,3,3,2,2,3]
                off = 0
                fields = []
                for w in cuts:
                    seg = b[off:off+w]; off += w
                    try:
                        fields.append(seg.decode("cp932", errors="ignore").strip())
                    except:
                        fields.append("")
                (reg, name_kan, name_kana, shibu, grade, _y, _bd, _sx, _age, _ht, _wt,
                 _blood, shoritsu, fukusho, win1, win2, syusso, yuushutsu, yusho, avgst) = fields
                rows.append({
                    "登録番号": reg, "氏名": name_kan, "支部": shibu, "級": grade,
                    "勝率": shoritsu, "複勝率": fukusho, "平均ST": avgst,
                    "出走": syusso, "1着回数": win1, "2着回数": win2
                })
            except Exception:
                continue
    if not rows:
        print("[WARN] Fan手帳のTXTから行が取れませんでした（幅テーブル要調整）")
    df = pd.DataFrame(rows).drop_duplicates()
    os.makedirs(os.path.dirname(out_csv), exist_ok=True)
    df.to_csv(out_csv, index=False, encoding="utf-8")
    print(f"[CSV] {out_csv}  rows={len(df)}")

# --- CLI ---------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data_official", help="保存先ルート")
    sub = ap.add_subparsers(dest="cmd")

    sp_b = sub.add_parser("bangumi", help="番組表(B)を日付範囲でDL→解凍→UTF-8化")
    sp_b.add_argument("start_yyyymmdd")
    sp_b.add_argument("end_yyyymmdd")

    sp_k = sub.add_parser("results", help="競走成績(K)を日付範囲でDL→解凍→UTF-8化")
    sp_k.add_argument("start_yyyymmdd")
    sp_k.add_argument("end_yyyymmdd")

    sp_f = sub.add_parser("fan", help="ファン手帳(LZH)を手持ちファイルから解凍→CSV雛形")
    sp_f.add_argument("fan_lzh_path", help="例: fan2510.lzh を手動DLしたパス")
    args = ap.parse_args()

    out = args.out
    os.makedirs(out, exist_ok=True)

    if args.cmd == "bangumi":
        lzhs = download_bk_range("B", args.start_yyyymmdd, args.end_yyyymmdd, out)
        for lzh in lzhs:
            # 展開→UTF-8正規化
            txts = extract_lzh(lzh, os.path.join(out, "txt_raw", "B", os.path.basename(lzh)[1:7]))
            for t in txts:
                dst = os.path.join(out, "txt_utf8", "B", os.path.basename(os.path.dirname(t)), os.path.basename(t))
                dump_txt_as_utf8(t, dst)

    elif args.cmd == "results":
        lzhs = download_bk_range("K", args.start_yyyymmdd, args.end_yyyymmdd, out)
        for lzh in lzhs:
            txts = extract_lzh(lzh, os.path.join(out, "txt_raw", "K", os.path.basename(lzh)[1:7]))
            for t in txts:
                dst = os.path.join(out, "txt_utf8", "K", os.path.basename(os.path.dirname(t)), os.path.basename(t))
                dump_txt_as_utf8(t, dst)

    elif args.cmd == "fan":
        fan_lzh = args.fan_lzh_path
        # 例：fan2510.lzh を data_official/lzh/FAN/ 配下へコピーして展開
        dst_lzh = os.path.join(out, "lzh", "FAN", os.path.basename(fan_lzh))
        os.makedirs(os.path.dirname(dst_lzh), exist_ok=True)
        shutil.copy2(fan_lzh, dst_lzh)
        txts = extract_lzh(dst_lzh, os.path.join(out, "txt_raw", "FAN", os.path.basename(fan_lzh).replace(".lzh","")))
        # Fan手帳CSV（雛形）
        parse_fan_txts_to_csv(os.path.join(out, "txt_raw", "FAN", os.path.basename(fan_lzh).replace(".lzh","")),
                              os.path.join(out, "fan_csv", os.path.basename(fan_lzh).replace(".lzh",".csv")))
    else:
        ap.print_help()

if __name__ == "__main__":
    main()
