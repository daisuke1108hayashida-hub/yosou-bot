# formatter.py
from collections import defaultdict
from typing import Dict, List, Tuple, Iterable, Optional

Triple = Tuple[int, int, int]

def _norm(tri: Iterable[int]) -> Tuple[int, int, int]:
    a, b, c = map(int, tri)
    return (a, b, c)

def _tri_str(tri: Triple) -> str:
    return f"{tri[0]}-{tri[1]}-{tri[2]}"

def dedup_buckets(buckets: Dict[str, List[Iterable[int]]]) -> Dict[str, List[Triple]]:
    """
    各バケット（本線/抑え/穴）にある3連単を重複排除。
    バケット間の重複も1回だけ残す（優先順位: 本線 > 抑え > 穴）
    入力例:
      {"main":[(1,3,2), (1,3,4)], "sub":[(1,3,2)], "ana":[(5,1,2)]}
    """
    order = ["main", "sub", "ana"]
    seen = set()
    out: Dict[str, List[Triple]] = {k: [] for k in order}
    for k in order:
        for tri in buckets.get(k, []):
            t = _norm(tri)
            if t not in seen:
                seen.add(t)
                out[k].append(t)
    return out

def _group_by_two_fixed(tris: List[Triple]) -> List[str]:
    """
    3連単リストを 2つ固定＋1つ可変 の圧縮表記にまとめる。
    例:
      (2,5,1) (2,5,3) (2,5,4) -> "2-5-134"
      (2,1,5) (2,3,5) (2,4,5) -> "2-134-5"
      (1,2,3) (4,2,3) (5,2,3) -> "145-2-3"
    """
    used = set()
    res: List[str] = []

    # (a,b)->{c}
    by_ab = defaultdict(set)
    # (a,c)->{b}
    by_ac = defaultdict(set)
    # (b,c)->{a}
    by_bc = defaultdict(set)

    for t in tris:
        a, b, c = t
        by_ab[(a, b)].add(c)
        by_ac[(a, c)].add(b)
        by_bc[(b, c)].add(a)

    def compress(items, pattern):
        # items: dict[key]->set(values)
        for key, vals in items.items():
            if len(vals) >= 2:
                vals_str = "".join(map(str, sorted(vals)))
                s = pattern(key, vals_str)
                # 使い切る（三つ組を使用済みに）
                if pattern is pat_ab:
                    a, b = key
                    for v in vals:
                        used.add((a, b, v))
                elif pattern is pat_ac:
                    a, c = key
                    for v in vals:
                        used.add((a, v, c))
                else:
                    b, c = key
                    for v in vals:
                        used.add((v, b, c))
                res.append(s)

    def pat_ab(key, vals):
        a, b = key
        return f"{a}-{b}-{vals}"

    def pat_ac(key, vals):
        a, c = key
        return f"{a}-{vals}-{c}"

    def pat_bc(key, vals):
        b, c = key
        return f"{vals}-{b}-{c}"

    compress(by_ab, pat_ab)
    compress(by_ac, pat_ac)
    compress(by_bc, pat_bc)

    # 圧縮されなかった単発はそのまま
    for t in tris:
        if t not in used:
            res.append(_tri_str(t))

    # 表記の重複も排除しつつ順序を保つ
    uniq = []
    seen_s = set()
    for s in res:
        if s not in seen_s:
            seen_s.add(s)
            uniq.append(s)
    return uniq

def compress_bucket(tris: List[Triple]) -> List[str]:
    """バケット内（三連単群）を圧縮表記へ。"""
    if not tris:
        return []
    return _group_by_two_fixed(tris)

def build_explanation(meta: Dict) -> str:
    """
    展開予想の文章を自動生成。
    meta は取れる範囲でOK。欠けていても動くようにしてます。
      例:
      {
        "場名": "唐津",
        "レース": 9,
        "日付": "2025/08/12",
        "風速": 4.0,         # m/s（あれば）
        "風向": "追い",       # 追い/向い/横（あれば）
        "選手": {
          1: {"名":"上野","級":"A1","ST":0.12},
          2: {"名":"富樫","級":"A2","ST":0.14},
          ...
        },
        "参考": "https://..."  # 参照URL（任意）
      }
    """
    jname = meta.get("場名", "")
    race = meta.get("レース")
    wind = meta.get("風速")
    wdir = meta.get("風向")
    players: Dict[int, Dict] = meta.get("選手", {})

    def st_txt(lane):
        st = players.get(lane, {}).get("ST")
        return f"{st:.2f}" if isinstance(st, (int, float)) else "-"

    # 内枠・スタートの簡易評価
    inner_bias = "内有利の傾向"  # デフォルト
    if isinstance(wind, (int, float)) and wind >= 5:
        inner_bias = "向かい風強めで内からの押し有利" if wdir in ("向い", "向かい") else \
                     "追い風強めでセンター勢のまくり差しに注意"

    # ST早い枠
    fasters = sorted(
        [i for i in players if isinstance(players[i].get("ST"), (int, float))],
        key=lambda i: players[i]["ST"]
    )[:2]  # 2人まで
    faster_txt = "・".join([f"{i}={st_txt(i)}" for i in fasters]) if fasters else "データ不足"

    lines = []
    lines.append(f"{jname}{race}Rの展望。{inner_bias}。")
    if fasters:
        lines.append(f"ST注目は {faster_txt}。")
    if players.get(1, {}).get("級") in ("A1", "A2"):
        lines.append("①は機力/実力ともに上位。先マイから“逃げ本線”。")
    else:
        lines.append("①の信頼はやや割引。②の差し・③のまくり差しが怖い。")

    # 外の扱い
    if any(players.get(i, {}).get("級") == "A1" for i in (4,5,6)):
        lines.append("外枠にも攻め手あり。カド勢の仕掛けから紐荒れも。")

    # まとめ
    src = meta.get("参考")
    if src:
        lines.append(f"（参考: {src}）")

    return "\n".join(lines)

def build_message(
    title: str,
    meta: Dict,
    buckets: Dict[str, List[Iterable[int]]],
) -> str:
    """
    返信テキスト作成の総合関数。
    - 重複排除
    - ハイフン区切り
    - 2固定圧縮
    - 展開予想の文章
    """
    buckets = dedup_buckets(buckets)
    main_c = compress_bucket(buckets.get("main", []))
    sub_c  = compress_bucket(buckets.get("sub", []))
    ana_c  = compress_bucket(buckets.get("ana", []))

    parts = [f"📍 {title}"]
    parts.append("――――――――――")
    parts.append(build_explanation(meta))
    parts.append("")

    if main_c:
        parts.append(f"本線（{len(main_c)}点）")
        parts.append("\n".join(main_c))
        parts.append("")

    if sub_c:
        parts.append(f"押え（{len(sub_c)}点）")
        parts.append("\n".join(sub_c))
        parts.append("")

    if ana_c:
        parts.append(f"穴目（{len(ana_c)}点）")
        parts.append("\n".join(ana_c))
        parts.append("")

    return "\n".join([p for p in parts if p.strip()])
