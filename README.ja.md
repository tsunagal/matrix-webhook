[日本語](README.ja.md) | [English](README.md)

# Matrix Webhook アプリケーションサービス

このリポジトリには、Matrix ルームにメッセージを送信するためのウェブフック機能を提供する Matrix アプリケーションサービスが含まれています。外部サービスが簡単な HTTP POST リクエストを通じて Matrix ルームにメッセージを送信できるようにします。

---

## 目次

1. [前提条件](#前提条件)
2. [初期セットアップ](#初期セットアップ)
3. [環境変数](#環境変数)
4. [サービスの実行](#サービスの実行)
5. [エンドツーエンド暗号化](#エンドツーエンド暗号化)
6. [使用例](#使用例)
7. [エラーハンドリング](#エラーハンドリング)

---

## 前提条件

- Docker と Docker Compose
- 実行中の Matrix ホームサーバー (Synapse)。`homeserver.yaml` を編集できること
- Python 3.10 以上 (Docker なしで実行する場合)

---

## 初期セットアップ

このサービスは Matrix のアプリケーションサービスです。最初に一度だけ Synapse に登録します。

1. このリポジトリをクローンします。
2. 登録ファイルの例をコピーします。

   ```bash
   cp webhook_registration.example.yaml webhook_registration.yaml
   ```

3. ランダムなトークンを 2 つ作り、`as_token` と `hs_token` に入れます。

   ```bash
   openssl rand -hex 32
   ```

4. users の正規表現にある `example\.com` を、自分の `SERVER_NAME` に置き換えます。ドットはエスケープしてください。`SERVER_NAME=matrix.example.org` なら `'@webhook_.*:matrix\.example\.org'` です。
5. Synapse から読める場所にファイルを置き、そのパスを `homeserver.yaml` に追加します。

   ```yaml
   app_service_config_files:
     - /data/webhook_registration.yaml
   ```

6. Synapse を再起動します。
7. `.env.example` を `.env` にコピーし、`AS_TOKEN` に `as_token` と同じ値を設定します（[環境変数](#環境変数)を参照）。

登録ファイルについて：

- `url: null`：このサービスはメッセージを送るだけです。Synapse からイベントを受け取るエンドポイントはありません。
- `sender_localpart`（`webhook_master`）は `BOT_LOCALPART`（`webhook_bot`）と別の名前にしてください。Synapse は送信者のユーザーに `/sync` を許さないため、ボットが招待や暗号鍵を受け取れなくなります。
- `BOT_LOCALPART` は users の正規表現（`@webhook_.*`）に一致する必要があります。
- これらが間違っているとき、または `AS_TOKEN` がないときは、サービスは起動時に直すべき設定を示すメッセージを出して止まります。

---

## 環境変数

`.env.example` を `.env` にコピーして、以下の変数を設定してください：

```bash
# Matrix Webhook 環境変数
MATRIX_URL=http://host.docker.internal:8008  # Matrix ホームサーバーの URL、例: https://matrix.org
SERVER_NAME=localhost                        # Matrix サーバー名、homeserver.yaml 設定の server_name と一致する必要があります
BOT_LOCALPART=webhook_bot                   # ボットのユーザー名ローカル部分。登録ファイルの users の正規表現に一致させる
AS_TOKEN=<as_token>                         # 登録ファイルの as_token
CRYPTO_DB_DIR=/data                         # ボットが暗号鍵を保存する場所（永続ボリュームをマウントする）
RECOVERY_KEY=                               # ボットの回復キー（空白と引用符なしで書く）。/data を失ったときに必要
PICKLE_KEY=matrix-webhook                   # 任意。保存する Olm アカウントの暗号化に使う鍵
```

これらの環境変数はウェブフックサービスの設定に使用されます。`.env` ファイルは安全に保管し、バージョン管理にコミットしないでください。

---

## サービスの実行

### Docker Compose を使用（推奨）

デタッチモードでサービスを開始：

```bash
docker-compose up -d
```

ポートは `127.0.0.1:9000`（このマシン）にだけ公開します。`curl http://localhost:9000/health` で確認できます。アプリケーションサービスの登録ファイルでは `url: null` にしてください。このサービスは Synapse からの送信を受け取らず、`http://localhost:9000` のような `url` を書くと、Synapse が自分自身に送り直し続けます。

`/webhook` には認証がないため、`0.0.0.0` には公開しないでください。ほかのマシンから受け取るときは、前にリバースプロキシ（Caddy、nginx）を置き、`/webhook` のパス（できれば秘密のパス）だけを公開し、`/health` は公開しないでください。プロキシと同じ Docker ネットワークで動かす場合（Synapse の compose の中など）は、ポートの公開は不要です。プロキシから `http://webhook:9000` で届きます。

### Docker なし

Python 3.10 以上が必要です。

1. 依存関係をインストールします。

   ```bash
   pip install -r requirements.txt
   ```

2. `.env` の `MATRIX_URL` を、このマシンから見たホームサーバーの URL（例：`http://localhost:8008`）にします。`CRYPTO_DB_DIR` は書き込めるディレクトリ（例：`data`）にします。
3. `.env` を読み込んで、サービスを起動します。

   ```bash
   set -a; . ./.env; set +a
   uvicorn main:app --host 127.0.0.1 --port 9000
   ```

サービスは `127.0.0.1:9000` で待ち受けます。

---

## エンドツーエンド暗号化

暗号化されたルームには暗号化して送ります。ボットは自分の端末を持ち、鍵を `CRYPTO_DB_DIR`（Docker では `/data`）の SQLite に保存します。

- **ホームサーバー**：appservice の登録ファイルに `io.element.msc4190: true` が必要です（ボットが自分の端末を作るため。Synapse 1.159 ではほかの設定は不要）。このとき Synapse はボットの登録に `inhibit_login` を求めますが、サービス側で付けています
- **永続ボリューム**：`/data` は再起動しても残るようにしてください（`docker-compose.yml` の `./data:/data`）。起動するのは 1 つだけにしてください
- **回復キー**：初回起動時にボットがクロス署名の鍵を作り、回復キーを `/data/recovery_key.txt` に書き出します。ほかの秘密情報と同じ場所に保管して、空白と引用符を外して `RECOVERY_KEY` に設定し、ファイルは削除してください
- **`/data` を失ったとき**：ボットは新しい端末を作り、古い端末を削除します。`RECOVERY_KEY` を設定しておけば、同じ ID で新しい端末に署名するので、メンバーからは引き続き信頼されます
- ボットが同期するのはメンバー・暗号化の状態・鍵のやり取りだけで、メッセージの本文は同期しません。新しいメンバーや端末には次の投稿で鍵が渡ります。参加する前の投稿は、ほかのメンバーのメッセージと同じく読めません

---

## 使用例

### メッセージの送信

curl を使用してルームにメッセージを送信：

```bash
curl -X POST http://localhost:9000/webhook \
     -H 'Content-Type: application/json' \
     -d '{"text":"Webhook via AS works test!","channel":"#test:localhost"}'
```

投稿ごとに、Tsunagal と同じ形で新しいスレッドを作ります。スレッドのルートにタイトル、最初の返信に本文が入ります。

成功レスポンス：

```json
{
  "ok": true,
  "event_id": "$VLnAIICMyUu8m39dA6lftWHz11B-WJzsRUtNuYg5mjQ",
  "thread_root": "$NhJ6sR3_nRiKwxQE-MVmQr0NlvOtVE494UQE7j3Uddk"
}
```

`event_id` は本文（返信）、`thread_root` はスレッドのルートです。`"thread": false` のときは `null` です。

### リクエストのフィールド

| フィールド | 型 | 既定値 | 説明 |
|-----------|----|--------|------|
| `text` | 文字列 | — | メッセージ。必須。ただし新しいスレッドで `title` を指定したときは省略できます。 |
| `channel` / `room_id` | 文字列 | — | ルームのエイリアス（`#room:server`）またはルーム ID（`!id:server`）。どちらかが必須です。 |
| `format` | 文字列 | プレーン | `"html"` のとき `text` を HTML として扱います。サニタイズして本文に使います。スレッドのルートは常にプレーンテキストです。 |
| `title` | 文字列 | — | スレッドのタイトル。改行と連続した空白は 1 つの空白にまとめ、本文から取るタイトルと同じく 60 文字で切ります。 |
| `thread` | 真偽値 | `true` | `false` のとき、これまでどおりスレッドなしのメッセージを 1 件送ります。 |
| `thread_root` | 文字列 | — | 同じルームにある既存のスレッドのルートのイベント ID。投稿はそのスレッドへの返信 1 件になります。 |
| `thread_ts` | 文字列 | — | Slack 形式の `thread_root` の別名。イベント ID（`$` で始まる値）のときだけ使います。`"1503435956.000247"` のような Slack のタイムスタンプや数値は無視し、これまでどおり新しいスレッドを作ります。両方を指定するときは同じ値にしてください。 |

### タイトルと本文

- `title` があるとき：ルートは `title`、本文は `text` 全体です。`text` が空のときは、Tsunagal が空のスレッドに使うものと同じ `ここから会話を始めます。` を本文にします。
- `title` がないとき：`text` の最初の空でない行をルートにします（`format: "html"` のときはプレーンテキストに変換したものから取り、HTML のままは使いません）。
  - 60 文字を超えるタイトルは 59 文字で切り、末尾に `…` を付けます。絵文字は途中で切りません（肌の色、ZWJ でつないだ絵文字、国旗、キーキャップは丸ごと残すか丸ごと外します）。
  - `text` に続きの行があり、タイトルを切っていないときは、残りの行を本文にします。それ以外（1 行だけ、またはタイトルを切ったとき）は `text` 全体を本文にします。`format: "html"` のときは、マークアップを壊さないよう常に `text` 全体を本文にします。
- ルートを送ったあとに本文を送れなかったときは、本文のないスレッドが残らないようルートを削除（redact）し、500 を返します。

```bash
# タイトルと本文
curl -X POST http://localhost:9000/webhook \
     -H 'Content-Type: application/json' \
     -d '{"title":"デプロイ完了","text":"tsunagal-web v2026.11.0 を公開しました","channel":"#test:localhost"}'

# 上で作ったスレッドに返信
curl -X POST http://localhost:9000/webhook \
     -H 'Content-Type: application/json' \
     -d '{"text":"ロールバックしました","thread_root":"$NhJ6sR3_nRiKwxQE-MVmQr0NlvOtVE494UQE7j3Uddk","channel":"#test:localhost"}'

# スレッドなしのメッセージ 1 件
curl -X POST http://localhost:9000/webhook \
     -H 'Content-Type: application/json' \
     -d '{"text":"ping","thread":false,"channel":"#test:localhost"}'
```

### メッセージフォーマットオプション
> 注意: ウェブフックボットはパブリックルームに自動的に参加できます。プライベートルームの場合は、まずボットユーザーをルームに招待してください。ボットは自動的に招待を受諾します。

異なるフォーマットでメッセージを送信できます：

```bash
# HTML フォーマットメッセージ
curl -X POST http://localhost:9000/webhook \
     -H 'Content-Type: application/json' \
     -d '{
           "text": "<b>太字テキスト</b> and <i>斜体テキスト</i>",
           "channel": "#test:localhost",
           "format": "html"
         }'

# メンション付きプレーンテキスト
curl -X POST http://localhost:9000/webhook \
     -H 'Content-Type: application/json' \
     -d '{
           "text": "Hey @user:example.com, これをチェックしてください！",
           "channel": "#test:localhost"
         }'
```

---

## エラーハンドリング

サービスは適切な HTTP ステータスコードと JSON レスポンスを返します：

- 200: メッセージ送信成功
- 400: 無効なリクエスト：本文が JSON オブジェクトでない、フィールドの型が違う、`text` がない、または `thread_root` がスレッドのルートでない（返信、リレーション、ステートイベント、削除済みのイベント）
- 403: ボットがルームに参加・投稿できない（例：招待されていないプライベートルーム）
- 404: ルームまたは `thread_root` が見つからない
- 500: 内部サーバーエラー

エラーレスポンス例：

```json
{
  "ok": false,
  "error": "`channel` or `room_id` is required"
}
```

ホームサーバーからのエラーは、ホームサーバーのメッセージを付けて `"Matrix error: ..."` として返します。