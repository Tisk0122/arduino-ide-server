# Arduino Web IDE サーバーの運用

この手順は `install.sh` で構築した Ubuntu / Debian 環境向けです。バックアップにはIDE用・管理者用トークン、アカウントと作品、利用履歴、ボード・ライブラリのデータが含まれます。バックアップファイルは機密情報として扱い、アクセス制限と暗号化を行ったうえで、サーバー外にも保管してください。

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

アカウント、作品、作品の過去版は `/var/lib/arduino-helper/accounts.sqlite3` に保存されます。上記バックアップはこのデータベースも含め、稼働中のサービスを止めて整合性を保って取得します。パスワードハッシュなどのアカウント情報を含むため、アーカイブは機密データとして取り扱ってください。

## GitHubからの自動更新

Webhookを設定すると `main` へのpushを受けて更新します。加えて `arduino-helper-update.timer` が5分ごとにGitHubのコミットIDと最後に適用したIDを比較するため、Webhookの未配信・短時間の連続push・サーバー再起動中のpushも次回確認で検出します。変更がない確認ではcloneやサービス再起動を行いません。更新対象はサーバー本体、IDE各画面、更新スクリプト自身とtimer/service定義です。インストーラーや設定ファイル、トークン、Arduino CLIデータは自動更新しません。ヘルスチェックは稼働中の `arduino-helper` systemd unitから `HELPER_PORT` を読み取るため、既定の8765以外の待受ポートでも更新を検証できます。

Webhookの初回設定またはWebhook起動権限の再設定は次で行います。Webhook処理の変更を更新した後も、sudoers の起動権限を同期するためこのコマンドを一度実行してください。

```bash
sudo /usr/local/libexec/arduino-helper-configure-github-webhook
```

旧版の更新スクリプトは `www/teacher.html` を必須とし、さらに廃止済み `/teacher` 画面のヘルスチェックを行います。新しい更新スクリプトへファイルを差し替えても、実行中のプロセスは旧版のまま処理を続けるため、ヘルスチェックで失敗してロールバックし、旧版へ戻ることがあります。この世代の旧スクリプトから移行するときは、次のブートストラップを一度だけ手動実行してください。Webhookとtimerが稼働していても、旧スクリプトのままでは自動更新だけで復旧できません。

```bash
sudo bash -c '
set -euo pipefail
tmp="$(mktemp -d)"
trap '\''rm -rf "$tmp"'\'' EXIT
git clone --depth 1 --branch main https://github.com/Tisk0122/arduino-ide-server.git "$tmp/repo"
install -o root -g root -m 0700 "$tmp/repo/github-update.sh" /usr/local/libexec/arduino-helper-update
/usr/local/libexec/arduino-helper-update
'
```

このブートストラップは、既に `/etc/arduino-helper/github-webhook.env` が設定され、GitHubリポジトリへ接続できるサーバーで実行します。更新スクリプトは取得したコミットのファイルを配置し、サービス再起動と `/ping`・`/docs`・`/account` の応答を確認します。検査に失敗した場合は配置ファイルを保存済みバックアップへ戻します。成功したら `applied-commit` がGitHubの `main` と一致することを確認してください。既にWebhook・timerが設定済みなら、Webhook Secretの再入力や `configure-github-webhook` の再実行は不要です。

状態・実行結果の確認:

```bash
systemctl status arduino-helper-update.timer --no-pager
systemctl list-timers arduino-helper-update.timer --no-pager
systemctl is-enabled arduino-helper-update.timer
systemctl is-active arduino-helper-update.timer
sudo journalctl -t arduino-helper-update -n 100 --no-pager
cat /var/lib/arduino-helper-updater/applied-commit
```

`applied-commit` は最後にヘルスチェックまで成功したGitコミットです。timerは起動時にも確認し、ネットワーク障害や更新失敗があれば次の5分間隔で再試行します。Webhookの受信APIが `202` を返しただけでは更新成功を意味しないため、journalとこのコミットIDで適用結果を確認してください。自動更新には対象ブランチへのpush権限を適切に制限し、GitHub Webhook Secretを第三者へ共有しないでください。

GitHubのWebhook設定では Payload URL を `https://<公開ホスト>/webhook/github`、Content type を `application/json`、Secret を設定スクリプトで入力した値、イベントを `Push events` にします。GitHubの「Recent Deliveries」で `202` は更新サービスへの起動依頼が受理された状態です。`401` はSecret不一致、`404` はサーバー側Webhook設定が未完了、`503` は更新サービス起動権限またはsystemd側の問題です。配信結果とサーバー側の処理状態は次で確認します。

```bash
sudo systemctl status arduino-helper-update.service --no-pager
sudo journalctl -u arduino-helper-update.service -n 100 --no-pager
sudo journalctl -t arduino-helper-update -n 100 --no-pager
```

## サーバー規模と保存容量の目安

以下はUbuntu / Debian、単一サーバー、700アカウントを想定した初期導入の目安です。実際のコンパイル時間・メモリ・ボードデータ容量は、Arduinoボード、ライブラリ、スケッチにより変わるため、本番前に利用予定のボードで負荷試験を行い、空き容量とメモリを監視してください。

| 用途 | CPU | メモリ | SSD | 想定 |
| --- | ---: | ---: | ---: | --- |
| 最低限の試験・小規模運用 | 2 vCPU | 4 GB | 60 GB | アカウント・IDE利用と少数ずつのコンパイル。利用者全員の一斉コンパイルには不向き |
| 700アカウント、授業中に200〜300人がIDEを利用 | 4 vCPU | 8 GB | 100 GB以上 | 通常の編集・保存・ログインと、コンパイルが順番待ちになる構成 |
| 並列コンパイルを4ジョブ程度に拡張した場合 | 8 vCPU | 16 GB | 150 GB以上 | アプリを複数ワーカー化し、ジョブごとの作業領域を分離した後の初期目安 |
| 並列コンパイルを8ジョブ程度に拡張した場合 | 16 vCPU | 32 GB | 200 GB以上 | 大きいボードパッケージも使う場合の開始目安。実負荷試験で調整 |

上記の並列構成は**現在のままCPUやメモリを増やすだけでは有効になりません**。現行サーバーはHTTPリクエストをスレッド処理し、1プロセスあたり最大150接続を扱いますが、Arduino CLIのコンパイルは共有ロックと単一ワーカーで1件ずつ実行します。また、ジョブキューやCLIロックはプロセス内メモリにあるため、同じSQLite DBやArduinoデータ領域を複数サーバープロセスから共有して水平スケールする構成にもなっていません。並列化には複数コンパイルワーカー、ジョブ単位の独立したbuild path、一貫したキュー／排他制御、メモリ・CPU・ディスク使用量に応じた同時実行上限の実装と試験が必要です。

現行の既定値ではコンパイル待ちキューは40件、同じ接続元からの実行・待機は合計2件までです。同じNAT配下の利用者はこの上限を共有する場合があります。`HELPER_PER_CLIENT` は最大50まで設定できますが、`HELPER_MAX_QUEUE` を増やしてもコンパイルは直列のままです。200〜300人が同時にIDEを開いて編集・保存する負荷と、200〜300人が同時にコンパイルを押す負荷は異なります。現行構成で一斉コンパイルすると、多くはHTTP 429（混雑）になるか、受付済みのものも長い待ち時間になります。授業ではコンパイルを数回に分ける運用を推奨します。全員のコンパイルを一斉に受け付けることが要件なら、先にジョブキューの容量・接続元制限・待ち時間を含む設計と並列ワーカーを実装してください。

### 保存領域の見積もり

アカウントDBの作品データは `/var/lib/arduino-helper/accounts.sqlite3` にあり、作品の現行データに加えて変更履歴も保存します。1作品あたり現在版と最大100件の過去版を保持します。作品数・作品サイズに全体上限はないため、厳密な最大容量は定まりません。スケッチのソース上限は1作品あたり合計2 MiBですが、これは保存領域全体の上限ではありません。

概算例（700人分、JSON等の付加分やWALを除く）:

| 利用量の仮定 | 概算 |
| --- | ---: |
| 1人10作品、平均10 KiB、1作品あたり現行＋過去4版の計5コピー | 約350 MB |
| 1人20作品、平均25 KiB、1作品あたり計10コピー | 約3.5 GB |
| 1人30作品、平均100 KiB、1作品あたり計21コピー | 約44 GB |

通常の学習用コードなら中段の例より小さくなる可能性がありますが、保存回数・コードサイズ・ZIPや長い文字列の有無で増減します。運用開始時は作品DB用に**最低10 GB、余裕を持たせるなら20〜50 GB**を見込み、実測に応じて拡張してください。データベースのほかにArduino CLIのボードコア、ライブラリ、ダウンロード一時データ、コンパイルキャッシュ、ログも同じサーバー側ストレージを使用します。ボードの種類によって必要量は大きく違うため、これらを含めたSSDは通常利用で100 GB以上を推奨し、インストール後に実際の使用量を確認してください。初回インストール、ボード追加、コンパイル、復元のため、少なくとも20 GB程度の空き領域を維持する運用が安全です。

バックアップはライブデータとは別の保存先を用意してください。バックアップにはArduinoデータと作品DBも含まれ、1世代だけでもライブ使用量相当以上の領域が必要になる場合があります。複数世代を保管する場合はその分を加算し、サーバーと同じディスクだけにバックアップを置かないでください。

## 個人アカウント

利用者はIDEのアカウント画面からユーザー名とパスワードを使って登録・ログインします。ユーザー名は重複不可、パスワードは10〜256文字です。メールによるパスワード再設定はありません。パスワード変更後はそのアカウントの全セッションが失効し、再ログインが必要です。アカウント作品は他の利用者のアカウントから参照できません。

最初の管理者トークンを確認する場合は、サーバー上で次を実行します。

```bash
sudo -u arduino-helper env HELPER_HOME=/var/lib/arduino-helper \
  python3 /opt/arduino-helper/arduino-helper.py --show-tokens
```

この版では最初のアカウントAPI利用時に、アカウントDBを一般ユーザー用スキーマへ更新します。旧学校用のアカウント、クラス、作品、コメント、過去版のデータは削除され、元に戻せません。必要な場合はアップデート前にバックアップを確認して保管してください。ユーザーセッションの有効期間は12時間です。

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

既定ではCORSを許可しません。IDEを別オリジンから配信する場合は、`/etc/arduino-helper/helper.env` にIDEの配信元だけをカンマ区切りで指定し、サービスを再起動してください。ワイルドカード `*` は指定できません。

```ini
HELPER_CORS_ORIGINS=https://ide.example.org,https://www.example.org
```

origin は `https://ホスト名[:ポート]` の形式で、パスやクエリは指定しません。ブラウザーから許可されていない origin でAPIを呼び出すと、CORS応答ヘッダーは返りません。IDEを別ドメインで配信している場合は、その配信元を含めないとブラウザーからAPIを利用できません。

管理画面の「IDEアクセストークンを自動入力」を有効にすると、認証不要の `GET /public-config` がIDE用トークンを返します。有効にするのは、そのIDEを利用する全員にトークンを配布する運用を意図している場合だけにしてください。ブラウザーからサーバーまでHTTPSを必須にし、TLS終端プロキシからオリジンまでの経路も信頼できる暗号化ネットワーク内に置いてください。さらに `HELPER_CORS_ORIGINS` をIDE配信元のHTTPS originだけに制限してください。CORSはブラウザーの読み取り制御であり、curlなどの非ブラウザーアクセスを認証・遮断する仕組みではありません。自動入力が不要なら設定を無効にしてください。

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
