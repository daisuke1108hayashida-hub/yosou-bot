# ... 既存の import の下あたり
URL_NUMERIC = re.compile(r"https?://(?:www\.)?boatrace-db\.net/race/\d+/?$", re.IGNORECASE)
URL_WRONG   = re.compile(r"https?://(?:www\.)?boatrace-db\.net/race/\d{8}/\d{1,2}/\d{1,2}", re.IGNORECASE)

@handler.add(MessageEvent, message=TextMessage)
def on_message(event: MessageEvent):
    user_text = (event.message.text or "").strip()

    # ヘルプは省略…

    # 艇国DBのURL抽出
    m = re.search(r"https?://(?:www\.)?boatrace-db\.net/[^\s]+", user_text, re.IGNORECASE)
    if not m:
        msg = (
            "艇国データバンクのレース個別URLを送ってください。\n"
            "例) https://boatrace-db.net/race/1234567"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg))
        return

    url = m.group(0)

    # ✳ 間違った形式を明示
    if URL_WRONG.match(url):
        msg = (
            "そのURL形式（/race/日付/場/レース）は艇国DBにはありません。\n"
            "ブラウザで該当レースを開き、/race/数字 の形式のURLを送ってください。\n"
            "例) https://boatrace-db.net/race/1234567"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg))
        return

    # ✳ 正しい形式のみ受け付け
    if not URL_NUMERIC.match(url):
        msg = (
            "対応形式は /race/数字 のみです。\n"
            "例) https://boatrace-db.net/race/1234567"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(msg))
        return

    # ここから予想実行
    try:
        result = predict_from_teikoku(url)
        reply  = format_prediction_message(result)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(reply if len(reply)<=5000 else reply[:4900]))
    except Exception as e:
        err = f"取得/予想中にエラーが発生しました。\n{type(e).__name__}: {e}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(err))
