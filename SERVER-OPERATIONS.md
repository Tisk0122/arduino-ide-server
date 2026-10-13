# Arduino Web IDE サーバーの運用

この手順は `install.sh` で構築した Ubuntu / Debian 環境向けです。バックアップには生徒用・管理者用トークン、利用履歴、ボード・ライブラリのデータが含まれます。バックアップファイルは機密情報として扱い、アクセス制限と暗号化を行ったうえで、サーバー外にも保管してください。

## バックアップ

インストール後は次のコマンドでバックアップを作成できます。

```bash
sudo arduino-helper-backup
```

保存先を指定する場合:

```bash
sudo arduino-helper-backup /mnt/secure-backups/arduino-helper
```

サービスが稼働中の場合、バックアップの整合性を保つため、スクリプトは一時停止してから再起動します。実行中のコンパイルや管理操作は中断されるため、利用の少ない時間帯に実行してください。アーカイブと SHA-256 チェックサムは root のみ読み取り可能な状態で保存されます。ボードデータを含むため、初回やボード追加後のバックアップには数GB必要になることがあります。

定期実行、世代管理、暗号化、サーバー外への転送はこのコマンドだけでは設定されません。保存先の容量と保管要件に合わせて、運用者がスケジュールと安全な転送先を設定してください。

## 復元

復元には root 権限が必要です。信頼できるバックアップだけを使用してください。アプリ本体と Arduino CLI はアーカイブに含まれないため、先に同じリリースのソースから `install.sh` を実行して、サービスアカウントと実行ファイルを用意してください。

1. チェックサムを検証します。
2. 現在の状態も必要なら、先に `sudo arduino-helper-backup` で退避します。
3. サービスを停止し、アーカイブをルートディレクトリへ展開します。
4. systemd を再読込してサービスを起動し、ログと管理画面で状態を確認します。

例:

```bash
cd /var/backups/arduino-helper
sha256sum -c arduino-helper-YYYYMMDD-HHMMSS.tar.gz.sha256
sudo systemctl stop arduino-helper
sudo tar --same-owner --acls --xattrs \
  -xzf arduino-helper-YYYYMMDD-HHMMSS.tar.gz -C /
sudo systemctl daemon-reload
sudo systemctl start arduino-helper
sudo systemctl status arduino-helper
sudo journalctl -u arduino-helper -n 50 --no-pager
```

展開すると、バックアップ時点の `/var/lib/arduino-helper`、`/var/lib/arduino-build` と `/etc` 配下のサービス設定に置き換わります。誤ったアーカイブを展開しないよう、ファイル名とチェックサムを事前に確認してください。

## 再起動とジョブ

ジョブ状態はメモリ上にあり、サービス再起動・ホスト再起動では実行中・待機中のコンパイルや管理操作は復元されません。作業領域は起動時に掃除されます。ジョブの中断後は IDE から必要な処理を再実行してください。

状態とログの確認:

```bash
sudo systemctl status arduino-helper
sudo journalctl -u arduino-helper -f
```

`HELPER_MIN_FREE_MB` の容量確認に失敗した場合、コンパイルは安全のため受け付けられません。容量判定は API の作業領域 (`HELPER_HOME`) に加え、設定されている Arduino CLI のデータ、ユーザーデータ、ダウンロード領域とデータ領域配下の build cache も対象にし、確認できた領域のうち最も少ない空き容量を使います。管理画面の空き容量表示が「確認不可」のときは、対象ファイルシステムとサービスユーザーからのアクセスを確認してください。

GitHub Actions は push・pull request 時に Python 3.8 / 3.12 の単体テストと Python・shell 構文検査を実行します。Linux 上の systemd、Arduino CLI 実機、バックアップからの復元はこの CI の対象外です。

コンパイル上限とジョブ所有者の識別には接続元ネットワークを使います。同じ NAT / 学内ネットワークから接続する利用者は、`HELPER_PER_CLIENT` の上限を共有します。クライアントが自由に指定できる識別ヘッダーは制限判定に使いません。

## CORS とプロキシ

既定の `HELPER_CORS_ORIGINS=*` は従来との互換性のため、任意のWebオリジンからのブラウザーアクセスを許可します。公開運用でIDEの配信元が固定されている場合は、`/etc/arduino-helper/helper.env` に許可する origin のみをカンマ区切りで指定し、サービスを再起動してください。

```ini
HELPER_CORS_ORIGINS=https://ide.example.org,https://www.example.org
```

origin は `https://ホスト名[:ポート]` の形式で、パスやクエリは指定しません。ブラウザーから許可されていない origin でAPIを呼び出すと、CORS応答ヘッダーは返りません。IDEを別ドメインで配信している場合は、その配信元を含めないとブラウザーからAPIを利用できません。

管理画面の「生徒用トークンを自動入力」を有効にすると、認証不要の `GET /public-config` が生徒用トークンを返します。有効にするのは、トークンをそのIDEの全利用者へ配布する運用を意図している場合だけにしてください。ブラウザーからサーバーまでHTTPSを必須にし、TLS終端プロキシからオリジンまでの経路も信頼できる暗号化ネットワーク内に置いてください。さらに `HELPER_CORS_ORIGINS` をIDE配信元のHTTPS originだけに制限してください。CORSはブラウザーの読み取り制御であり、curlなどの非ブラウザーアクセスを認証・遮断する仕組みではありません。自動入力が不要なら設定を無効にしてください。

標準インストールはループバックで受け付け、Cloudflare Tunnel経由の接続元識別に `CF-Connecting-IP` を使う設定です。`HELPER_TRUST_CF_CONNECTING_IP=1` は、ローカル接続が信頼済みプロキシだけに限られる場合にのみ有効にしてください。ローカルマシン上の別プロセスからもポートへ接続できる場合、そのプロセスはこのヘッダーを偽装できます。信頼済みプロキシを使わない場合は値を `0` にしてサービスを再起動します。

認証失敗の追跡はプロセス内で上限付きに管理され、サービス再起動や複数インスタンスをまたいで共有されません。公開環境では、Tunnel / リバースプロキシ側にも認証エンドポイントのレート制限を設定してください。

## コンテナ・メモリ制限

インストーラーはホストの総メモリと、検出できる現在の cgroup v1/v2 メモリ上限の小さい方を基準に、systemd の `MemoryHigh` / `MemoryMax` を設定します。コンテナや特殊なsystemd構成では自動検出できない場合があるため、導入後に次で実際の制限値を確認してください。

```bash
systemctl show arduino-helper -p MemoryHigh -p MemoryMax
cat /sys/fs/cgroup/memory.max
```

## タイムアウトと子プロセス

コンパイルや管理操作のタイムアウト時、ラッパーは CLI のプロセスグループを終了します。APIサーバー側の最終監視も POSIX 環境ではプロセスグループ全体を終了します。アップデート後やsystemd停止後にプロセスが残っていないことは、対象Linux環境で `ps` と systemd journal を使って確認してください。

## ボード・ライブラリの検索エラー

Arduino CLIの検索・一覧取得が失敗した場合、サーバーは空の検索結果として扱わず、APIエラーを返します。検索結果が空の場合と通信・インデックス取得に失敗した場合を区別してください。エラーが続くときは `sudo journalctl -u arduino-helper -n 100 --no-pager` で詳細を確認し、サーバーからArduinoのパッケージ／ライブラリインデックスへ接続できることを確認してください。

Arduino CLIが検索結果を出力しても、タイムアウト・キャンセル・想定外の終了コードの場合は応答を成功扱いしません。CLIの一部バージョンが検索結果ありで返す終了コード `1` のみ、有効なJSONがある場合に限って許可します。

## 長時間待機したジョブ

コンパイルジョブが3時間以上キューで待機した場合は、単に一覧から消すのではなくエラー状態にして利用履歴へ記録します。管理ジョブは処理スレッドがロック待機している場合があるため、掃除対象にせず、CLIロックの取得待ち時間上限で失敗として終了します。
