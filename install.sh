#!/usr/bin/env bash
# Arduino Web IDE コンパイルサーバー (arduino-helper 2.0) のセットアップ  — Ubuntu / Debian 系
#
#   sudo bash install.sh                 AVR (Uno/Nano/Mega) のみ
#   sudo bash install.sh esp32           ESP32 も入れる (約 3〜5GB、時間がかかります)
#
# 何度実行しても安全です (トークン・設定・インストール済みボードは上書きしません)。
#
# できあがる構成
#   arduino-helper (ユーザー)  API サーバー。トークンを持つ。arduino-cli は直接動かさない
#   arduino-build  (ユーザー)  arduino-cli を動かす低権限ユーザー。トークンや鍵は読めない
#   /opt/arduino-helper/arduino-helper.py       API サーバー本体 (root 所有。書き換え不可)
#   /usr/local/libexec/arduino-build-run        arduino-cli の実行ラッパー (root 所有)
#   /opt/arduino-cli/arduino-cli                arduino-cli 本体 (バージョン固定)
#   /var/lib/arduino-helper                     トークン・作業領域
#   /var/lib/arduino-build                      ボード・ライブラリ (arduino-cli のデータ)
set -euo pipefail

ARDUINO_CLI_VERSION="${ARDUINO_CLI_VERSION:-1.5.1}"
# この配布物は検証済みの Arduino CLI 1.5.1 に固定する。更新時は checksum を更新して再監査する。
[ "$ARDUINO_CLI_VERSION" = "1.5.1" ] || { echo "エラー: この版では Arduino CLI 1.5.1 のみサポートします (更新時は checksum を更新してください)" >&2; exit 1; }
CLI_CHECKSUMS_SHA256="1deac1e8d8eff69ef1b0cb8c5d4be2a3a81224b103eaf3403b77ff470c7f4ec6"
HELPER_PORT="${HELPER_PORT:-8765}"
WITH_ESP32="${1:-}"

HERE="$(cd "$(dirname "$0")" && pwd)"
H_USER=arduino-helper
B_USER=arduino-build
H_HOME=/var/lib/arduino-helper
B_HOME=/var/lib/arduino-build
CLI_DIR=/opt/arduino-cli
CLI="$CLI_DIR/arduino-cli"
APP_DIR=/opt/arduino-helper
WRAP=/usr/local/libexec/arduino-build-run
CONF_DIR=/etc/arduino-helper

cd /   # arduino-build が読めない場所 (/root など) から実行されても sudo が警告しないように

say()  { printf '\n== %s ==\n' "$*"; }
warn() { printf '警告: %s\n' "$*" >&2; }
die()  { printf 'エラー: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "root 権限で実行してください:  sudo bash install.sh"
for f in arduino-helper.py arduino-build-run github-update.sh arduino-helper-update.service arduino-helper-update.timer arduino-helper-backup.service arduino-helper-backup.timer configure-github-webhook.sh backup-arduino-helper.sh www/index.html www/admin.html www/docs.html; do
  [ -f "$HERE/$f" ] || die "$HERE/$f がありません。アーカイブを展開したフォルダで実行してください"
done
command -v systemctl >/dev/null || die "systemd が必要です"

# Windows 環境で作成された場合に CRLF が混入することがあるため、
# シェルスクリプト・systemd ユニット・Python ファイルを自動的に LF に変換する
for f in arduino-helper.py arduino-build-run github-update.sh arduino-helper-update.service arduino-helper-update.timer configure-github-webhook.sh backup-arduino-helper.sh; do
  if grep -qU $'\r' "$HERE/$f" 2>/dev/null; then
    warn "$f に Windows 改行 (CRLF) が含まれています。自動的に LF に変換します"
    if command -v dos2unix >/dev/null 2>&1; then
      dos2unix "$HERE/$f" 2>/dev/null || sed -i 's/\r//' "$HERE/$f"
    else
      sed -i 's/\r//' "$HERE/$f"
    fi
  fi
done

say "1/8 必要なパッケージ"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 curl ca-certificates sudo tar util-linux >/dev/null
if ! apt-get install -y -qq python3-venv python3-serial >/dev/null 2>&1; then
  warn "python3-venv / python3-serial を入れられませんでした (ESP32 の一部ツールで必要になることがあります)"
fi
python3 - <<'PY' || die "Python 3.8 以上が必要です"
import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)
PY

say "2/8 ユーザーとフォルダ"
getent group "$H_USER" >/dev/null || groupadd --system "$H_USER"
getent group "$B_USER" >/dev/null || groupadd --system "$B_USER"
id "$H_USER" >/dev/null 2>&1 || useradd --system --gid "$H_USER" --home-dir "$H_HOME" --shell /usr/sbin/nologin "$H_USER"
id "$B_USER" >/dev/null 2>&1 || useradd --system --gid "$B_USER" --home-dir "$B_HOME" --shell /usr/sbin/nologin "$B_USER"
usermod -aG "$H_USER" "$B_USER"       # arduino-build は作業フォルダ (グループ共有) にだけ書ける
install -d -o "$H_USER" -g "$H_USER" -m 0710 "$H_HOME"          # グループは通り抜けのみ。一覧・トークンは見えない
install -d -o "$H_USER" -g "$H_USER" -m 2770 "$H_HOME/work" "$H_HOME/cache"
install -d -o "$B_USER" -g "$B_USER" -m 0750 "$B_HOME"
install -d -o root -g root -m 0755 "$CONF_DIR" "$APP_DIR" "$CLI_DIR" /usr/local/libexec

say "3/8 arduino-cli $ARDUINO_CLI_VERSION"
if [ -x "$CLI" ] && "$CLI" version 2>/dev/null | grep -q "${ARDUINO_CLI_VERSION}"; then
  echo "インストール済み"
else
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) asset="arduino-cli_1.5.1_Linux_64bit.tar.gz" ;;
    i386|i686|x86) asset="arduino-cli_1.5.1_Linux_32bit.tar.gz" ;;
    aarch64|arm64) asset="arduino-cli_1.5.1_Linux_ARM64.tar.gz" ;;
    armv7l|armv7*) asset="arduino-cli_1.5.1_Linux_ARMv7.tar.gz" ;;
    armv6l|armv6*) asset="arduino-cli_1.5.1_Linux_ARMv6.tar.gz" ;;
    *) die "未対応の CPU アーキテクチャです: $arch" ;;
  esac
  base="https://github.com/arduino/arduino-cli/releases/download/v${ARDUINO_CLI_VERSION}"
  curl -fsSL --retry 3 -o "$tmp/checksums.txt" "$base/${ARDUINO_CLI_VERSION}-checksums.txt"
  printf '%s  %s\n' "$CLI_CHECKSUMS_SHA256" "$tmp/checksums.txt" | sha256sum -c - >/dev/null || die "Arduino CLI の checksum ファイル検証に失敗しました"
  expected="$(awk -v f="$asset" '$2 == f {print $1}' "$tmp/checksums.txt")"
  [ -n "$expected" ] || die "Arduino CLI の checksum が見つかりません: $asset"
  curl -fsSL --retry 3 -o "$tmp/$asset" "$base/$asset"
  printf '%s  %s\n' "$expected" "$tmp/$asset" | sha256sum -c - >/dev/null || die "Arduino CLI 本体の checksum 検証に失敗しました"
  rm -rf "$CLI_DIR"
  install -d -o root -g root -m 0755 "$CLI_DIR"
  tar -xzf "$tmp/$asset" -C "$CLI_DIR" --no-same-owner --no-same-permissions
  [ -f "$CLI_DIR/arduino-cli" ] || die "Arduino CLI の展開に失敗しました"
  chown root:root "$CLI"; chmod 755 "$CLI"
fi
"$CLI" version

B_DATA="$B_HOME/.arduino15"
B_USER_DIR="$B_HOME/Arduino"
B_DOWNLOADS="$B_DATA/staging"
B_CONFIG="$B_DATA/arduino-cli.yaml"
as_build() { sudo -H -u "$B_USER" env \
  ARDUINO_CONFIG_FILE="$B_CONFIG" \
  ARDUINO_DIRECTORIES_DATA="$B_DATA" \
  ARDUINO_DIRECTORIES_USER="$B_USER_DIR" \
  ARDUINO_DIRECTORIES_DOWNLOADS="$B_DOWNLOADS" \
  "$@"; }

say "4/8 arduino-cli の設定 (arduino-build ユーザー)"
install -d -o "$B_USER" -g "$B_USER" -m 0750 "$B_DATA" "$B_USER_DIR" "$B_DOWNLOADS" "$B_DATA/build-cache"
if [ ! -f "$B_CONFIG" ]; then
  as_build "$CLI" config init >/dev/null
fi
as_build "$CLI" config set build_cache.path "$B_DATA/build-cache" >/dev/null
for url in \
  "https://espressif.github.io/arduino-esp32/package_esp32_index.json" \
  "https://arduino.esp8266.com/stable/package_esp8266com_index.json" \
  "https://github.com/earlephilhower/arduino-pico/releases/download/global/package_rp2040_index.json"
do
  if ! as_build "$CLI" config dump 2>/dev/null | grep -qF "$url"; then
    as_build "$CLI" config add board_manager.additional_urls "$url"
  fi
done

say "5/8 ボードパッケージ"
as_build "$CLI" core update-index
as_build "$CLI" core install arduino:avr
if [ "$WITH_ESP32" = "esp32" ]; then
  as_build "$CLI" core install esp32:esp32
fi

say "6/8 サーバー本体・ラッパー・sudo 設定"
install -m 0644 -o root -g root "$HERE/arduino-helper.py" "$APP_DIR/arduino-helper.py"
install -m 0755 -o root -g root "$HERE/arduino-build-run" "$WRAP"
install -d -o root -g root -m 0755 "$APP_DIR/www"
install -m 0644 -o root -g root "$HERE/www/index.html" "$APP_DIR/www/index.html"
install -m 0644 -o root -g root "$HERE/www/admin.html" "$APP_DIR/www/admin.html"
install -m 0644 -o root -g root "$HERE/www/docs.html" "$APP_DIR/www/docs.html"
install -m 0700 -o root -g root "$HERE/github-update.sh" /usr/local/libexec/arduino-helper-update
install -m 0644 -o root -g root "$HERE/arduino-helper-update.service" /etc/systemd/system/arduino-helper-update.service
install -m 0644 -o root -g root "$HERE/arduino-helper-update.timer" /etc/systemd/system/arduino-helper-update.timer
install -m 0644 -o root -g root "$HERE/arduino-helper-backup.service" /etc/systemd/system/arduino-helper-backup.service
install -m 0644 -o root -g root "$HERE/arduino-helper-backup.timer" /etc/systemd/system/arduino-helper-backup.timer
install -m 0644 -o root -g root "$HERE/arduino-helper-verify-backup.service" /etc/systemd/system/arduino-helper-verify-backup.service
install -m 0644 -o root -g root "$HERE/arduino-helper-verify-backup.timer" /etc/systemd/system/arduino-helper-verify-backup.timer
install -m 0700 -o root -g root "$HERE/configure-github-webhook.sh" /usr/local/libexec/arduino-helper-configure-github-webhook
install -m 0750 -o root -g root "$HERE/backup-arduino-helper.sh" /usr/local/sbin/arduino-helper-backup
install -m 0750 -o root -g root "$HERE/verify-arduino-helper-backup.sh" /usr/local/sbin/arduino-helper-verify-backup
if [ ! -f "$CONF_DIR/backup.env" ]; then
  cat > "$CONF_DIR/backup.env" <<'EOF'
# 定期バックアップの設定。変更後: sudo systemctl restart arduino-helper-backup.timer
HELPER_BACKUP_DIR=/var/backups/arduino-helper
HELPER_BACKUP_RETENTION=14
# オフサイト保管には rclone を構成した後、例の形式で指定してください。
#HELPER_BACKUP_REMOTE=secure-backup:arduino-helper
EOF
  chmod 0600 "$CONF_DIR/backup.env"
fi
chown root:root "$CONF_DIR/backup.env"
chmod 0600 "$CONF_DIR/backup.env"
cat > "$CONF_DIR/build-run.conf" <<EOF
{"workroot": "$H_HOME", "cli": "$CLI"}
EOF
chmod 644 "$CONF_DIR/build-run.conf"
if [ ! -f "$CONF_DIR/helper.env" ]; then
  cat > "$CONF_DIR/helper.env" <<'EOF'
# 必要なものだけコメントを外してください。変更後: sudo systemctl restart arduino-helper
#HELPER_HOST=127.0.0.1          # Cloudflare Tunnel だけで使うなら 127.0.0.1 のまま
#HELPER_MAX_QUEUE=40            # 待ち行列の上限
#HELPER_PER_CLIENT=2            # 1台が同時に持てるコンパイル数
#HELPER_ADMIN_MAX_QUEUE=10      # 管理作業の待ち行列上限
#HELPER_CORS_ORIGINS=https://ide.example.org,https://www.example.org
#HELPER_TRUST_CF_CONNECTING_IP=0 # 信頼できるローカルプロキシを使わない場合
#HELPER_COMPILE_TIMEOUT=900     # 1回のコンパイルの上限 (秒)
#HELPER_MIN_FREE_MB=1024        # 空きがこれ未満ならコンパイルを断る
#HELPER_ACCOUNT_QUOTA_MB=50     # アカウントごとの現行スケッチ+履歴上限 (0で無制限)
#HELPER_SINGLE_TOKEN=1          # 1人で使うとき: IDE 用トークンで管理操作もできる
#HELPER_ALLOW_ASM=1             # アセンブラを明示的に許可する場合のみ
# GitHub 自動更新を使う場合は configure-github-webhook.sh で設定します。
# Webhook URL: /webhook/github
EOF
fi
sudoers_tmp="$(mktemp)"
# sudo の実装 (従来の sudo / Ubuntu 26.04 の sudo-rs) によって使える設定が違うため、
# 検証に通る最も詳しい設定を選ぶ
sudoers_ok=0
for defaults in \
  "!requiretty, !use_pty, !lecture, !mail_always" \
  "!use_pty, !lecture" \
  "!use_pty" \
  ""
do
  {
    echo "# arduino-helper は引数を検証するラッパーだけを arduino-build として実行できる"
    [ -n "$defaults" ] && echo "Defaults:$H_USER $defaults"
    # sudo-rs では引数なしのコマンド指定が引数付き実行に一致しないため、明示的に許可する。
    # ラッパー側が許可する Arduino CLI コマンドと引数を検証する。
    echo "$H_USER ALL=($B_USER) NOPASSWD: $WRAP *"
    echo "$H_USER ALL=(root) NOPASSWD: /usr/bin/systemctl --no-block start arduino-helper-update.service"
  } > "$sudoers_tmp"
  if visudo -cf "$sudoers_tmp" >/dev/null 2>&1; then sudoers_ok=1; break; fi
done
[ "$sudoers_ok" = 1 ] || die "sudoers の検証に失敗しました (変更していません)"
install -m 0440 -o root -g root "$sudoers_tmp" /etc/sudoers.d/arduino-helper
rm -f "$sudoers_tmp"

say "7/8 systemd サービス"
read -r mem_high mem_max < <(python3 - <<'PY'
import pathlib
import re
import sys

meminfo = pathlib.Path("/proc/meminfo").read_text()
match = re.search(r"^MemTotal:\s+(\d+)\s+kB$", meminfo, re.M)
if not match:
    sys.exit("MemTotal を取得できません")
host_kb = int(match.group(1))
limits = []
cgroups = pathlib.Path("/proc/self/cgroup").read_text().splitlines()
for entry in cgroups:
    parts = entry.split(":", 2)
    if len(parts) != 3:
        continue
    controllers, relative = parts[1], parts[2].lstrip("/")
    if ".." in pathlib.PurePosixPath(relative).parts:
        continue
    if not controllers:
        files = (pathlib.Path("/sys/fs/cgroup") / relative / "memory.max",)
    elif "memory" in controllers.split(","):
        files = (pathlib.Path("/sys/fs/cgroup/memory") / relative / "memory.limit_in_bytes",)
    else:
        continue
    for file in files:
        try:
            value = file.read_text().strip()
        except OSError:
            continue
        if value.isdigit() and int(value) > 0:
            limit_kb = int(value) // 1024
            if 0 < limit_kb < host_kb:
                limits.append(limit_kb)
effective_kb = min([host_kb] + limits)
if effective_kb < 128 * 1024:
    sys.exit("利用可能メモリが128 MiB未満です。systemd MemoryMax を安全に設定できません")
budget_mb = effective_kb // 1024
print(max(1, budget_mb * 75 // 100), max(1, budget_mb * 85 // 100))
PY
)
cat > /etc/systemd/system/arduino-helper.service <<EOF
[Unit]
Description=Arduino Web IDE compile server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$H_USER
Group=$H_USER
UMask=0007
Environment=HELPER_HOME=$H_HOME
Environment=HELPER_HOST=127.0.0.1
Environment=HELPER_PORT=$HELPER_PORT
Environment=HELPER_TRUST_CF_CONNECTING_IP=1
Environment=HELPER_BUILD_USER=$B_USER
Environment=HELPER_BUILD_WRAPPER=$WRAP
Environment=ARDUINO_CONFIG_FILE=$B_CONFIG
Environment=ARDUINO_DIRECTORIES_DATA=$B_DATA
Environment=ARDUINO_DIRECTORIES_USER=$B_USER_DIR
Environment=ARDUINO_DIRECTORIES_DOWNLOADS=$B_DOWNLOADS
EnvironmentFile=-$CONF_DIR/helper.env
ExecStart=/usr/bin/python3 $APP_DIR/arduino-helper.py
Restart=always
RestartSec=3
Nice=5
MemoryHigh=${mem_high}M
MemoryMax=${mem_max}M
TasksMax=2048
LimitNOFILE=8192
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=full
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictRealtime=yes
LockPersonality=yes
# NoNewPrivileges は付けません (sudo で arduino-build に切り替えるため)

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now arduino-helper-backup.timer
systemctl enable --now arduino-helper-verify-backup.timer
if [ -r "$CONF_DIR/github-webhook.env" ]; then
  systemctl enable --now arduino-helper-update.timer
fi
systemctl enable arduino-helper >/dev/null 2>&1
systemctl restart arduino-helper
sleep 3

say "8/8 動作確認"
fail=0
if curl -fsS "http://127.0.0.1:$HELPER_PORT/ping" | grep -q '"ok": true'; then echo "[OK] サーバーが応答しています"; else echo "[NG] サーバーが応答しません: journalctl -u arduino-helper -n 50"; fail=1; fi

if sudo -u "$H_USER" sudo -n -u "$B_USER" "$WRAP" -- --timeout 15 -- version >/dev/null 2>&1; then echo "[OK] arduino-helper → arduino-build の切り替え"; else echo "[NG] sudo の切り替えに失敗: sudo -u $H_USER sudo -n -u $B_USER $WRAP -- --timeout 15 -- version"; fail=1; fi

if sudo -u "$B_USER" cat "$H_HOME/token" >/dev/null 2>&1 || sudo -u "$B_USER" cat "$H_HOME/admin-token" >/dev/null 2>&1; then
  echo "[NG] arduino-build がトークンを読めてしまいます (権限の設定を確認してください)"; fail=1
else echo "[OK] コンパイル用ユーザーはトークンを読めません"; fi

UTOK="$(cat "$H_HOME/token" 2>/dev/null || true)"
if [ -n "$UTOK" ]; then
  python3 - "$HELPER_PORT" "$UTOK" <<'PY' && echo "[OK] 実際の arduino-cli で Blink をコンパイルできました" || { echo "[NG] テストコンパイルに失敗しました"; fail=1; }
import json, sys, time, urllib.request
port, tok = sys.argv[1], sys.argv[2]
def call(method, path, body=None):
    r = urllib.request.Request("http://127.0.0.1:%s%s" % (port, path), method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as f:
        return json.loads(f.read().decode())
d = call("POST", "/compile", {"sketch": "SelfTest", "board": "arduino:avr:uno",
    "files": [{"name": "SelfTest.ino", "content": "void setup(){pinMode(13,OUTPUT);}\nvoid loop(){digitalWrite(13,HIGH);delay(500);digitalWrite(13,LOW);delay(500);}\n"}]})
for _ in range(300):
    j = call("GET", "/jobs/" + d["job"])
    if j["state"] in ("done", "error", "cancelled"):
        ok = j["state"] == "done" and j["result"]["success"] and any(k.endswith(".hex") for k in j["result"].get("artifacts", {}))
        if not ok: print(json.dumps(j, ensure_ascii=False)[:1500])
        sys.exit(0 if ok else 1)
    time.sleep(1)
sys.exit(1)
PY
fi

echo
echo "================ 完了 ================"
echo "API の待受     : http://127.0.0.1:$HELPER_PORT  (Cloudflare Tunnel / Tailscale からここへ転送します)"
echo
sudo -u "$H_USER" env HELPER_HOME="$H_HOME" python3 "$APP_DIR/arduino-helper.py" --show-tokens
echo
echo "  IDE 用トークン … IDE に入力。コンパイルと閲覧に使う"
echo "  管理者トークン … ボード・ライブラリの追加削除と管理画面に使う"
echo
echo "状態確認   : systemctl status arduino-helper"
echo "ログ       : journalctl -u arduino-helper -f"
echo "管理画面   : http://127.0.0.1:$HELPER_PORT/admin  (管理者トークンが必要)"
echo "再起動     : sudo systemctl restart arduino-helper"
echo "バックアップ: sudo arduino-helper-backup"
echo "配布リンク : sudo -u $H_USER env HELPER_HOME=$H_HOME python3 $APP_DIR/arduino-helper.py --share-link https://あなたのURL"
[ "$fail" -eq 0 ] || { echo; echo "※ [NG] の項目があります。上のメッセージを確認してください。"; exit 1; }
