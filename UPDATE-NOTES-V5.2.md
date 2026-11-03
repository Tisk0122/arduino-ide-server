# Arduino Helper v5.2 / 2.10.1 更新内容

## 修正した問題

管理画面の「ボード情報インデックスの同期」が、次の実行ラッパーエラーで失敗する問題を修正しました。

```text
arduino-build-run: 許可されていない core サブコマンドです: update-index
```

原因は、API側が Boards Manager の更新・追加ソース管理コマンドを作成していた一方、root所有の実行ラッパー `arduino-build-run` の許可リストに必要なコマンドが登録されていなかったことです。

## 変更点

- `core update-index` を引数なしの限定コマンドとして許可。
- `config dump --json` のみ許可し、登録済みBoards Manager URLの読み取りに対応。
- `config add/remove board_manager.additional_urls <URL>` のみ許可。
- URLをラッパー側でも再検証（HTTPS、公開ドメイン/グローバルIP、JSONパス、認証情報なし、query/fragmentなし、ポート443のみ、localhost/内部/プライベートIP拒否）。
- 任意の `config set` / `config delete` や未許可の `core` / `lib` 操作は引き続き拒否。
- 自動更新スクリプトの配置先を修正し、実際にサービスが使用する `/usr/local/libexec/arduino-build-run` に更新。
- 自動更新時の実行ファイル権限をroot所有 `0755` に修正。`arduino-build` ユーザーが実行できるようにしました。
- サーバーバージョンを `2.10.1` に更新。

## テスト

- 許可する4コマンドのラッパー経由実行テスト。
- 任意config変更や危険なURLの拒否テスト。
- 自動更新の実ファイル配置先・実行権限・ロールバックテストを追加。
- 全ユニットテスト、構文検査、シェル構文検査を実施。

実サーバー上での外部インデックス取得とブラウザー操作は、利用者のサーバーに適用した後に確認が必要です。
