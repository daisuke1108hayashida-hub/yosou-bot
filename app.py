# 先頭あたりの import に追加
from biyori import fetch_biyori_first_then_fallback

# 公式の直前データを取る既存関数をラップ（あなたの既存実装名に合わせてください）
def fetch_official_beforeinfo(place_no: int, race_no: int, hiduke: str) -> dict:
    """
    ここはあなたの既存の公式スクレイパ/API結果を返す関数でOK。
    返り値は dict なら何でもよい。例：{"beforeinfo": ..., "url": official_url}
    """
    return existing_fetch_official(place_no, race_no, hiduke)  # ←既存関数名に置き換え

# 予想生成の直前データ取得部分を、下記のように一本化
def get_beforeinfo(place_no: int, race_no: int, hiduke: str) -> dict:
    return fetch_biyori_first_then_fallback(
        place_no=place_no, race_no=race_no, hiduke=hiduke,
        official_func=fetch_official_beforeinfo
    )

# （デバッグ用）ルートを1個追加しておくと便利
@app.route("/_debug/biyori")
def debug_biyori():
    from flask import request
    place_no = int(request.args.get("place_no", "15"))
    race_no = int(request.args.get("race_no", "5"))
    hiduke = request.args.get("hiduke", "20250811")
    data = get_beforeinfo(place_no, race_no, hiduke)
    return (str(data), 200, {"Content-Type": "text/plain; charset=utf-8"})
