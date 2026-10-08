# GitHub Push → 自動更新

この構成では、GitHub の `push` を受けるとサーバーが自動更新します。

## 動作

```text
GitHub push
  ↓
GitHub Webhook
  ↓ POST /webhook/github
Tailscale Funnel
  ↓
arduino-helper が HMAC-SHA256 署名を検証
  ↓
固定された root 更新スクリプトを sudo で起動
  ↓
git clone（指定 branch の最新コミット）
  ↓
Python の構文検査
  ↓
許可した3ファイルだけ反映
  ↓
systemctl restart arduino-helper
  ↓
/ping を確認
```

## 初回設定

1. GitHub にリポジトリを作る。
2. サーバーにこの配布物を `install.sh` で入れる。
3. Webhook Secret を生成する。

```bash
openssl rand -hex 32
```

4. サーバーで設定する。

```bash
sudo /usr/local/libexec/arduino-helper-configure-github-webhook
```

5. GitHub の Settings → Webhooks → Add webhook に以下を設定する。

- Payload URL: `https://<公開URL>/webhook/github`
- Content type: `application/json`
- Secret: 3 で生成した値
- Events: `Just the push event`
- Active: ON

以後は GitHub に push するだけで更新されます。

## 更新対象

自動更新で本番へ反映するのは次の3つだけです。

```text
arduino-helper.py
arduino-build-run
www/index.html
```

`install.sh`、`github-update.sh`、設定ファイル、トークン、Arduino CLI、ボード、ライブラリなどは GitHub の push では上書きしません。

## 手動更新

自動更新を使わない場合でも従来どおり `install.sh` を実行できます。


## 更新スクリプトの安全策

- `arduino-helper.py` と `arduino-build-run` は Python として構文検査します。
- `www` ディレクトリを含め、許可ファイルの symlink を拒否します。
- GitHub リポジトリ内の `install.sh` / `deploy.sh` などは実行しません。
- 本番へ反映するのは `arduino-helper.py`、`arduino-build-run`、`www/index.html` の3ファイルだけです。
- 新版の再起動または `/ping` に失敗した場合は、直前の3ファイルへ自動ロールバックします。
- Webhook の repo / branch / secret は初期設定時に `/etc/arduino-helper/helper.env` と `github-webhook.env` の両方へ同期します。
