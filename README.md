# yosou-bot（直前情報×簡易予想）

## 環境変数
- LINE_CHANNEL_SECRET
- LINE_CHANNEL_ACCESS_TOKEN

## デプロイ
- Render → Web Service
- Build Command: （空でOK / Procfile 使用）
- Start Command: （空でOK）

## 使い方（LINE）
- 「丸亀 8 20250811」: 丸亀12桁日付指定
- 「丸亀 8」: 今日の日付
- 「help」: 使い方

## 取得元
- boatrace.jp 直前情報
  - `https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={R}&jcd={JCD}&hd=YYYYMMDD`

## 備考
- 5分キャッシュ付き（関数 `@lru_cache`）。サーバ再起動でリセット。
- 解析が落ちたら公式リンクだけ返すフェイルセーフあり。
