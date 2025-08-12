# まずルートに簡易ページ（404回避）
@app.get("/")
def ping():
    return "yosou-bot OK. try /_debug/official?jcd=08&rno=6&hd=20250812"

# デバッグ: 公式直前情報の取得結果をテキストで表示
@app.get("/_debug/official")
def debug_official():
    jcd = int(request.args.get("jcd", "08"))
    rno = int(request.args.get("rno", "6"))
    hd  = request.args.get("hd", datetime.now().strftime("%Y%m%d"))
    res = asyncio.run(fetch_official_preinfo(jcd, rno, hd))  # ←前に追加した関数を呼ぶ
    text = [f"[official] url={res.get('url')}"]
    text.append(f"ok={res.get('ok')} raw_exists={res.get('raw_exists')} reason={res.get('reason','')}")
    if res.get("weather"):
        text.append("weather: " + ", ".join([f"{k}={v}" for k,v in res["weather"].items()]))
    if res.get("ex_times"):
        text.append("ex_times: " + ", ".join([f"{k}={v}" for k,v in sorted(res['ex_times'].items())]))
    return "\n".join(text), 200, {"Content-Type": "text/plain; charset=utf-8"}
