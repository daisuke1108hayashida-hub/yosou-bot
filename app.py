# -------------------------
# 3連単候補生成（少点数 & 変化を出す版）
# -------------------------

def _norm_scores(scores: Dict[int, float]) -> Dict[int, float]:
    vals = list(scores.values())
    mn, mx = min(vals), max(vals)
    span = (mx - mn) if (mx > mn) else 1.0
    return {k: (v - mn) / span for k, v in scores.items()}

def _meta_from_info(info: Dict):
    boats = info["boats"]
    # 展示タイム: 速いほど高評価（小さいほど良い）
    ex_vals = [v["exh"] for v in boats.values() if v["exh"] is not None]
    st_vals = [v["st"] for v in boats.values() if v["st"] is not None]
    ex_min, ex_max = (min(ex_vals), max(ex_vals)) if ex_vals else (None, None)
    st_ref = 0.10
    ex_norm = {}
    st_norm = {}
    for i in range(1, 7):
        ex = boats[i]["exh"]
        st = boats[i]["st"]
        if ex is not None and ex_max and ex_max > ex_min:
            ex_norm[i] = (ex_max - ex) / (ex_max - ex_min)  # 0..1 (速いほど1)
        else:
            ex_norm[i] = 0.0
        if st is not None:
            st_norm[i] = max(0.0, 1.0 - (st - st_ref) * 25)  # 0.10付近で高め
        else:
            st_norm[i] = 0.0
    return ex_norm, st_norm

def _triple_value(a, b, c, ns, exn, stn) -> float:
    """ 3連単(a-b-c)のスコア。1着を重め評価＋展示/STボーナス。 """
    v = 1.00 * ns[a] + 0.65 * ns[b] + 0.25 * ns[c]
    v += 0.30 * exn[a] + 0.15 * exn[b] + 0.08 * exn[c]
    v += 0.20 * stn[a] + 0.10 * stn[b] + 0.05 * stn[c]
    # 枠相性ボーナス（1枠頭は素直に少し加点、外頭は展示/Ｓが強ければOK）
    if a == 1:
        v += 0.15
    if a in (4, 5) and (exn[a] > 0.4 or stn[a] > 0.4):
        v += 0.12
    # ほんの少しだけ番号でタイブレーク（同点量産防止）
    v += (a*37 + b*11 + c*3) * 1e-4
    return v

def _rank_all_triples(scores: Dict[int, float], info: Dict):
    ns = _norm_scores(scores)
    exn, stn = _meta_from_info(info)
    ranked = []
    for a in range(1, 7):
        for b in range(1, 7):
            if b == a: 
                continue
            for c in range(1, 7):
                if c == a or c == b:
                    continue
                ranked.append((_triple_value(a, b, c, ns, exn, stn), (a, b, c)))
    ranked.sort(reverse=True)
    return ranked  # [(score, (a,b,c)), ...]

def _compress_for_first(first: int, triples: List[Tuple[int,int,int]]) -> List[str]:
    # 既存の圧縮ロジックをそのまま利用
    return compress_triples(first, triples)

def _line_points(line: str) -> int:
    # "1-23-3456" → 2*4 = 8点
    _, sec, thi = line.split("-")
    return len(sec) * len(thi)

def _cap_by_budget(lines: List[str], max_lines: int, max_points: int) -> List[str]:
    picked, pts = [], 0
    for s in lines:
        p = _line_points(s)
        if (len(picked) < max_lines) and (pts + p <= max_points):
            picked.append(s)
            pts += p
        if len(picked) >= max_lines:
            break
    return picked

def make_ticket_sets(scores: Dict[int, float], info: Dict):
    """
    ・全120通りをスコア順に評価
    ・本線/押え/穴を少点数に分配
    既定の上限（環境変数で変更可）:
      MAIN_MAX_LINES=4, MAIN_MAX_POINTS=12
      OSAE_MAX_LINES=4, OSAE_MAX_POINTS=10
      ANA_MAX_LINES=3,  ANA_MAX_POINTS=8
    """
    # 予算
    m_lines = int(os.getenv("MAIN_MAX_LINES", "4"))
    m_pts   = int(os.getenv("MAIN_MAX_POINTS", "12"))
    o_lines = int(os.getenv("OSAE_MAX_LINES", "4"))
    o_pts   = int(os.getenv("OSAE_MAX_POINTS", "10"))
    a_lines = int(os.getenv("ANA_MAX_LINES", "3"))
    a_pts   = int(os.getenv("ANA_MAX_POINTS", "8"))

    ranked = _rank_all_triples(scores, info)
    fav = max(scores, key=scores.get)
    order = [k for k,_ in sorted(scores.items(), key=lambda x:x[1], reverse=True)]

    # 本線：頭=本命、上位から厳選
    main_tris = [t for _, t in ranked if t[0] == fav][:18]
    main_lines = _compress_for_first(fav, main_tris)
    main_lines = _cap_by_budget(main_lines, m_lines, m_pts)

    # 押え：本命頭の取りこぼし＋対抗頭を少し
    used = set()
    for s in main_lines:
        a, b, c = s.split("-")
        # used は細かく展開せず、firstだけ避ける用途に
        used.add(int(a[0]))

    sub_heads = {fav, order[1]}  # 本命＋対抗
    osae_lines_all = []
    for h in sub_heads:
        tris = [t for _, t in ranked if t[0] == h][:14]
        osae_lines_all += _compress_for_first(h, tris)
    # 同文行の重複排除
    osae_lines = []
    for s in osae_lines_all:
        if s not in osae_lines and s not in main_lines:
            osae_lines.append(s)
    osae_lines = _cap_by_budget(osae_lines, o_lines, o_pts)

    # 穴：4,5頭を中心に上位を相手に
    hole_heads = set(order[3:5])
    ana_lines_all = []
    for h in hole_heads:
        tris = [t for _, t in ranked if t[0] == h][:12]
        ana_lines_all += _compress_for_first(h, tris)
    ana_lines = []
    for s in ana_lines_all:
        if s not in main_lines and s not in osae_lines:
            ana_lines.append(s)
    ana_lines = _cap_by_budget(ana_lines, a_lines, a_pts)

    return main_lines, osae_lines, ana_lines
