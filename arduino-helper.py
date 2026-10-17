#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Arduino Web IDE — コンパイルサーバー (arduino-helper) 2.0

Python 3.8+ の標準ライブラリだけで動きます。arduino-cli が必要です。

■ 設計 (v1.0 からの主な変更)
  - コンパイルは「ジョブ」です。POST /compile は番号を返し、IDE は GET /jobs/<id> で結果を受け取ります。
    待ち行列には上限があり、順番が分かります。(Cloudflare の約100秒の応答制限に掛かりません)
  - コンパイルごとに専用の作業ディレクトリを作り、終了後に必ず削除します。
    同名スケッチを同時にコンパイルしても、他人のコードやバイナリが混ざりません。
  - arduino-cli は arduino-build-run ラッパー経由で、別ユーザー (arduino-build) として実行できます。
    ソースの #include や .incbin でサーバーのトークン・鍵・他人のファイルを読まれるのを防ぎます。
  - トークンは2種類です。IDE 用 (コンパイルと閲覧) / 管理者用 (ボード・ライブラリの変更)。
  - 同じ内容 (ソース+ボード+サーバーの状態) のコンパイル結果はサーバー側でも使い回します。

■ 環境変数
  HELPER_HOME            データ置き場 (既定 ~/.arduino-helper)
  HELPER_HOST / PORT     待受 (既定 127.0.0.1:8765)
  HELPER_TOKEN           IDE 用トークン (未指定なら HELPER_HOME/token に自動生成)
  HELPER_ADMIN_TOKEN     管理者用トークン (未指定なら HELPER_HOME/admin-token に自動生成)
  HELPER_SINGLE_TOKEN=1  1人で使う場合。IDE 用トークンで管理操作もできる
  HELPER_BUILD_USER      指定すると sudo でそのユーザーとして arduino-cli を実行
  HELPER_BUILD_WRAPPER   ラッパーのパス (既定 /usr/local/libexec/arduino-build-run)
  HELPER_MAX_QUEUE       待ち行列の上限 (既定 40)
  HELPER_PER_CLIENT      同じ接続元が同時に持てるコンパイル数 (既定 2)
  HELPER_ADMIN_MAX_QUEUE 管理操作の待ち行列上限 (既定 10)
  HELPER_CORS_ORIGINS   CORS の許可元。カンマ区切り。未指定はクロスオリジン拒否
  HELPER_TRUST_CF_CONNECTING_IP=1
                         loopback の信頼済みプロキシから CF-Connecting-IP を利用
  HELPER_COMPILE_TIMEOUT コンパイルの上限秒 (既定 900)
  HELPER_MIN_FREE_MB     HOME / Arduino CLI データ領域の最小空き容量を下回ると拒否 (既定 1024)
  HELPER_ALLOW_ASM=1     .s / .S ファイルとアセンブラ指令を許可 (既定は不許可)

■ API (すべて JSON。Authorization: Bearer <トークン>)
  GET  /ping                      認証不要。状態・認証結果・サーバーの世代 (gen)
  GET  /boards                    ボード一覧
  GET  /cores/search?q=           ボードパッケージ検索
  GET  /cores/list                インストール済みパッケージ
  GET  /libs/search?q=            ライブラリ検索
  GET  /libs/list                 インストール済みライブラリ
  POST /compile  {sketch, board, files:[{name,content}]} -> 202 {job, position, queued}
  GET  /jobs/<id>?from=N          進捗。コンパイルは完了時に result を含む
  POST /jobs/<id>/cancel          キャンセル
  GET  /admin                     管理画面 (HTML。認証不要。ページ内でトークンを入力)
  --- 以下は管理者トークンが必要 ---
  GET  /admin/api/overview        サーバー状態・トークン詳細・統計・アクティブジョブ (JSON)
  GET  /admin/api/history         利用履歴 (JSON。offset/limit/kind/state/q で絞り込み)
  DELETE /admin/api/history       保存済みの利用履歴をすべて削除
  GET  /admin/api/settings        アクセストークン・IDE 用トークン配布設定 (JSON)
  POST /admin/api/settings        IDE 用トークン配布設定を保存
  GET  /public-config             IDE 用トークン自動入力設定 (有効時のみトークンを返す)
  POST /account/register          個人アカウントの作成
  POST /account/login             個人アカウントのログイン
  GET  /account/sketches          ログイン中の利用者の作品一覧
  /teacher                        廃止された先生・クラス画面 (404)
  POST /cores/install   {id}      -> {job}
  POST /cores/uninstall {id}      -> {job}
  POST /libs/install    {name, version?} -> {job}
  POST /libs/uninstall  {name}    -> {job}
  POST /libs/install-zip {name, data(base64)} -> {job}
  POST /update                    -> {job}

■ 管理コマンド
  arduino-helper.py --show-tokens
  arduino-helper.py --rotate user|admin|both
  arduino-helper.py --share-link <サーバーURL> [<予備URL>]
"""
import base64
import collections
import contextlib
import csv
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zipfile
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

VERSION = "2.8.0"


def env_int(name, default, lo=0, hi=10 ** 9):
    try:
        v = int(os.environ.get(name, default))
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def normalize_origin(value):
    try:
        parsed = urlparse(value)
        if (parsed.scheme.lower() not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError
        host = parsed.hostname.lower()
        if ":" in host:
            host = "[" + host + "]"
        port = parsed.port
        default_port = (parsed.scheme.lower() == "http" and port == 80) or (
            parsed.scheme.lower() == "https" and port == 443
        )
        netloc = host if port is None or default_port else "%s:%d" % (host, port)
        return parsed.scheme.lower() + "://" + netloc
    except ValueError:
        raise ValueError("CORS origin は http(s) の origin 形式で指定してください: %s" % value)


def parse_cors_origins(value):
    origins = [part.strip() for part in value.split(",") if part.strip()]
    if "*" in origins:
        raise ValueError("HELPER_CORS_ORIGINS にワイルドカード * は指定できません")
    return tuple(dict.fromkeys(normalize_origin(origin) for origin in origins))


HOME = Path(os.environ.get("HELPER_HOME") or (Path.home() / ".arduino-helper"))
ADMIN_SETTINGS_FILE = HOME / "admin-settings.json"
ACCOUNT_LOCK = threading.RLock()
ACCOUNT_DB_INIT_LOCK = threading.Lock()
ACCOUNT_DB_INITIALIZED_PATH = None
ADMIN_SETTINGS_LOCK = threading.Lock()
WORK_DIR = HOME / "work"
HOST = os.environ.get("HELPER_HOST", "127.0.0.1")
PORT = env_int("HELPER_PORT", 8765, 1, 65535)
BUILD_USER = os.environ.get("HELPER_BUILD_USER") or None
SINGLE_TOKEN = os.environ.get("HELPER_SINGLE_TOKEN") == "1"
ALLOW_ASM = os.environ.get("HELPER_ALLOW_ASM") == "1"

MAX_BODY = 12 * 1024 * 1024
MAX_SOURCE_TOTAL = 2 * 1024 * 1024
MAX_FILES = 60
MAX_ZIP_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT = 12 * 1024 * 1024
MAX_QUEUE = env_int("HELPER_MAX_QUEUE", 40, 1, 1000)
PER_CLIENT = env_int("HELPER_PER_CLIENT", 2, 1, 50)
ADMIN_MAX_QUEUE = env_int("HELPER_ADMIN_MAX_QUEUE", 10, 1, 100)
CORS_ORIGINS = parse_cors_origins(os.environ.get("HELPER_CORS_ORIGINS", ""))
TRUST_CF_CONNECTING_IP = os.environ.get("HELPER_TRUST_CF_CONNECTING_IP") == "1"
COMPILE_TIMEOUT = env_int("HELPER_COMPILE_TIMEOUT", 900, 30, 7200)
OP_TIMEOUT = 3600
MIN_FREE_MB = env_int("HELPER_MIN_FREE_MB", 1024, 0, 10 ** 6)
GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
GITHUB_WEBHOOK_REPO = os.environ.get("GITHUB_WEBHOOK_REPO", "")
GITHUB_WEBHOOK_BRANCH = os.environ.get("GITHUB_WEBHOOK_BRANCH", "main")
GITHUB_UPDATE_COMMAND = os.environ.get("GITHUB_UPDATE_COMMAND", "/usr/local/libexec/arduino-helper-update")
WEBHOOK_MAX_BODY = 1024 * 1024
WEBHOOK_LOCK = threading.Lock()
WEBHOOK_LAST = 0.0
WEBHOOK_COOLDOWN = 30
ACCOUNT_SESSION_LIMIT = 10
ACCOUNT_AUDIT_LIMIT = 10000
ACCOUNT_SIGNUP_LIMIT = 20
ACCOUNT_SIGNUP_WINDOW = 3600
ACCOUNT_SIGNUP_ATTEMPTS = {}
ACCOUNT_SIGNUP_LOCK = threading.Lock()
ACCOUNT_SESSION_CLEANUP_LOCK = threading.Lock()
ACCOUNT_SESSION_CLEANUP_LAST = 0
MAX_JOB_LINES = 4000
MAX_OUTPUT_CHARS = 300000        # コンパイルログ等テキスト出力の上限
MAX_JSON_OUTPUT_CHARS = 20000000  # JSON取得コマンド (lib search 等) の上限 (~20MB)
JOB_KEEP_SEC = 600
RESULT_CACHE_ENTRIES = 32
RESULT_CACHE_BYTES = 96 * 1024 * 1024
AUTH_STATE_LIMIT = 5000
HISTORY_FILE = HOME / "history.jsonl"   # 利用履歴 (JSON 1行1件)
HISTORY_KEEP = 4000                     # ファイル保持上限まで統計・表示に使う
HISTORY_TRIM = 4000                     # これを超えたら履歴ファイルを書き直す

ALLOWED_EXT = {".ino", ".h", ".hpp", ".cpp", ".c"} | ({".s", ".S"} if ALLOW_ASM else set())
ARTIFACT_EXT = {".hex", ".bin", ".uf2"}

RE_FQBN = re.compile(r"^[A-Za-z0-9_.\-]+:[A-Za-z0-9_.\-]+:[A-Za-z0-9_.\-]+(:[A-Za-z0-9_.,=\-]+)?$")
RE_CORE = re.compile(r"^[A-Za-z0-9_.\-]+:[A-Za-z0-9_.\-]+(@[0-9A-Za-z.\-+]+)?$")
RE_LIBNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.+()&'\-]{0,98}$")
RE_VERSION = re.compile(
    r"^(?=.{1,41}$)[0-9]+\.[0-9]+(?:\.[0-9]+)?(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
RE_FILE = re.compile(r"^[^/\\\x00-\x1f.][^/\\\x00-\x1f]{0,99}$")
RE_JOBID = re.compile(r"^[0-9a-f]{24}$")

CLI = None
TOKEN = None
ADMIN_TOKEN = None
STARTED = time.time()

JOBS = {}
JOBS_LOCK = threading.RLock()
QUEUE_CV = threading.Condition(JOBS_LOCK)
QUEUE = []                      # 待機中のコンパイルジョブ id (先頭が次)
CLI_LOCK = threading.Lock()     # arduino-cli の変更操作は同時に1つ (コンパイルも含む)
ADMIN_WAITING = 0
GEN = 0
GEN_LOCK = threading.Lock()
RESULT_CACHE = collections.OrderedDict()
RESULT_CACHE_SIZE = 0
READ_SEM = threading.BoundedSemaphore(4)
READ_CACHE = {}                 # path+query -> (expires, gen, data)
LIB_SEARCH_LOCK = threading.RLock()
LIB_SEARCH_OMIT_RELEASES_DETAILS = None
CLI_VER = {"t": 0, "v": ""}
COMPILES_DONE = 0
HISTORY = collections.deque(maxlen=HISTORY_KEEP)   # 過去 HISTORY_KEEP 件の利用履歴
HISTORY_LOCK = threading.Lock()
HISTORY_LINES = 0                                   # history.jsonl の現在の行数


class ApiError(Exception):
    def __init__(self, code, msg, extra=None):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.extra = extra or {}


def log(msg):
    sys.stderr.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stderr.flush()


# ---------------------------------------------------------------
# arduino-cli 実行 (ラッパー経由)
# ---------------------------------------------------------------
def find_wrapper():
    cand = os.environ.get("HELPER_BUILD_WRAPPER")
    if cand and os.path.exists(cand):
        return cand
    for p in ("/usr/local/libexec/arduino-build-run", str(Path(__file__).resolve().parent / "arduino-build-run")):
        if os.path.exists(p):
            return p
    return None


WRAPPER = None


def cli_available():
    return bool(WRAPPER)


def build_cmd(args, timeout, cancel_file=None, cleanup=()):
    opts = ["--timeout", str(int(timeout))]
    if cancel_file:
        opts += ["--cancel-file", str(cancel_file)]
    for c in cleanup:
        opts += ["--cleanup", str(c)]
    tail = ["--"] + list(args)
    if BUILD_USER:
        return ["sudo", "-n", "-u", BUILD_USER, WRAPPER] + opts + tail
    return [sys.executable, WRAPPER] + opts + tail


def kill_process_tree(process):
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        return
    except OSError as e:
        log("CLI プロセスグループを終了できません: %s" % e)


def run_cli(args, timeout=120, on_line=None, cancel_file=None, cleanup=()):
    """arduino-cli を実行して (returncode, output) を返す。"""
    if not cli_available():
        return 127, "arduino-build-run (arduino-cli の実行ラッパー) が見つかりません"
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
    for k in ("HELPER_HOME", "ARDUINO_CLI", "ARDUINO_BUILD_RUN_CONF", "ARDUINO_DIRECTORIES_DATA",
              "ARDUINO_DIRECTORIES_USER", "ARDUINO_CONFIG_FILE", "FAKE_CLI_LOG", "HOME", "TMPDIR"):
        if k in os.environ:
            env[k] = os.environ[k]
    try:
        p = subprocess.Popen(build_cmd(args, timeout, cancel_file, cleanup), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors="replace", env=env,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return 127, str(e)
    # ラッパーが上限時間で止めるはずだが、念のため API 側でも見張る
    def _kill():
        kill_process_tree(p)
    timer = threading.Timer(timeout + 60, _kill)
    timer.daemon = True
    timer.start()
    lines = []
    total = 0
    is_json_cmd = on_line is None  # JSON取得コマンドは行カットしない
    try:
        for line in p.stdout:
            line = line.rstrip("\r\n")
            # コンパイルログ表示用の行は2000文字でカット。
            # JSON取得コマンド (on_line=None) はカットしない — JSON が壊れるため
            if not is_json_cmd and len(line) > 2000:
                line = line[:2000] + " …"
            total += len(line) + 1
            limit = MAX_JSON_OUTPUT_CHARS if is_json_cmd else MAX_OUTPUT_CHARS
            if total <= limit:
                lines.append(line)
            elif total - len(line) - 1 <= limit:
                if not is_json_cmd:
                    lines.append("(出力が長いため以降を省略しました)")
            if on_line:
                on_line(line)
        p.wait()
    finally:
        timer.cancel()
    return p.returncode, "\n".join(lines)


def run_cli_json(args, timeout=120):
    rc, out = run_cli(list(args) + ["--format", "json"], timeout=timeout)
    # arduino-cli v1.5+ は検索結果があっても rc=1 を返すことがあるため、
    # 有効なJSONを伴う rc=1 のみ許容する。
    if rc == 127:
        log("arduino-cli エラー %s: %s" % (args, out[-500:]))
        raise ApiError(500, "arduino-cli の実行に失敗しました。サーバーのログを確認してください")
    if rc in (124, 130) or rc < 0:
        log("arduino-cli が中断されました (rc=%d, args=%s): %s" % (rc, args, out[-200:]))
        raise ApiError(504 if rc == 124 else 503,
                       "arduino-cli の処理が時間切れまたは中断になりました")
    unsupported_flag = "--omit-releases-details" in args and re.search(
        r"(?:unknown|unrecognized|unsupported|not defined|not recognized).{0,100}--omit-releases-details|"
        r"--omit-releases-details.{0,100}(?:unknown|unrecognized|unsupported|not defined|not recognized)",
        out, re.I | re.S)
    if unsupported_flag:
        _record_library_search_flag(args, False)
        fallback_args = [arg for arg in args if arg != "--omit-releases-details"]
        return run_cli_json(fallback_args, timeout=timeout)
    allows_search_rc1 = args[:2] in (["core", "search"], ["lib", "search"])
    if rc != 0 and not (rc == 1 and allows_search_rc1):
        log("arduino-cli がエラーを返しました (rc=%d, args=%s): %s" % (rc, args, out[-200:]))
        raise ApiError(502, "arduino-cli の処理に失敗しました。ネットワークやパッケージインデックスを確認してください")
    if rc != 0:
        log("arduino-cli rc=%d %s (JSON パースを試みます): %s" % (rc, args, out[-200:]))
    # 1) 全体をそのままパース
    try:
        return _record_library_search_flag(args, True, json.loads(out))
    except ValueError:
        pass
    # 2) NDJSON (arduino-cli v1.0+ が複数行 JSON を出力する場合) — 各行をパースして結合
    objects = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            objects.append(json.loads(line))
        except ValueError:
            pass
    if len(objects) == 1:
        return _record_library_search_flag(args, True, objects[0])
    if len(objects) > 1:
        # 同じキーを持つオブジェクト群はリストをマージして返す
        merged = {}
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            for k, v in obj.items():
                if k not in merged:
                    merged[k] = v
                elif isinstance(merged[k], list) and isinstance(v, list):
                    merged[k].extend(v)
        if merged:
            return _record_library_search_flag(args, True, merged)
        return _record_library_search_flag(args, True, objects)
    # 3) 先頭の { または [ から末尾まで切り出してパース
    for i, ch in enumerate(out):
        if ch in "{[":
            try:
                return _record_library_search_flag(args, True, json.loads(out[i:]))
            except ValueError:
                continue
    log("arduino-cli JSON 解析失敗 (args=%s) 出力先頭: %r" % (args, out[:300]))
    raise ApiError(502, "arduino-cli から有効な応答を取得できません。ネットワークやパッケージインデックスを確認してください")


def _record_library_search_flag(args, supported, result=None):
    global LIB_SEARCH_OMIT_RELEASES_DETAILS
    if "--omit-releases-details" in args:
        with LIB_SEARCH_LOCK:
            LIB_SEARCH_OMIT_RELEASES_DETAILS = supported
    return result


def _library_search_cache_key(query):
    with LIB_SEARCH_LOCK:
        mode = ("unknown" if LIB_SEARCH_OMIT_RELEASES_DETAILS is None else
                "omit-details" if LIB_SEARCH_OMIT_RELEASES_DETAILS else "full-details")
    return "ls:%s:%s" % (mode, query)


def cli_version():
    now = time.time()
    if now - CLI_VER["t"] > 300:
        rc, out = run_cli(["version"], timeout=15)
        CLI_VER["v"] = out.strip().splitlines()[0] if (rc == 0 and out.strip()) else ""
        CLI_VER["t"] = now
    return CLI_VER["v"]


# ---------------------------------------------------------------
# サーバーの世代 (ボード・ライブラリが変わると増える。キャッシュの無効化に使う)
# ---------------------------------------------------------------
def load_gen():
    global GEN
    f = HOME / "generation"
    try:
        GEN = int(f.read_text().strip())
    except Exception:
        GEN = int(time.time())
        save_gen()


def save_gen():
    f = HOME / "generation"
    try:
        tmp = f.with_suffix(".tmp")
        tmp.write_text(str(GEN))
        os.replace(tmp, f)
    except OSError as e:
        log("generation を保存できません: %s" % e)


def bump_gen():
    global GEN
    with GEN_LOCK:
        GEN += 1
        save_gen()
    with JOBS_LOCK:
        READ_CACHE.clear()
        clear_result_cache()


# ---------------------------------------------------------------
# 正規化 (arduino-cli の JSON → IDE 用)
# ---------------------------------------------------------------
def g(d, *keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _ver_tuple(v):
    """バージョン文字列を比較用タプルに変換 (例: "1.2.10" → (1, 2, 10))"""
    parts = []
    for x in re.split(r"[.\-+]", str(v)):
        try:
            parts.append((0, int(x)))
        except ValueError:
            parts.append((1, x))   # 数値でない部分は文字列比較
    return parts


def _latest_ver(keys):
    """バージョン文字列のコレクションから最新のキーを返す"""
    ks = list(keys)
    if not ks:
        return ""
    try:
        return max(ks, key=_ver_tuple)
    except Exception:
        return sorted(ks)[-1]


def norm_cores(data):
    # arduino-cli v0.x: {"platforms": [...]}
    # arduino-cli v1.0+: {"platforms": [...]} or top-level list
    plats = data if isinstance(data, list) else g(data, "platforms", default=[]) or []
    res = []
    for p in plats:
        if not isinstance(p, dict):
            continue
        # v1.0+: "id" field may be at top level or inside "platform_meta"
        pid = (g(p, "id", "ID")
               or g(g(p, "platform_meta", default={}), "id")
               or g(p, "platform_id", default=""))
        if not pid:
            continue
        # v1.0+: latest_version / installed_version (same keys, keep both spellings)
        latest = g(p, "latest_version", "Latest", "latestVersion", default="")
        inst   = g(p, "installed_version", "Installed", "installedVersion", default="")
        # v1.0+: releases dict key is version string; also accept direct "release" object
        rel = {}
        releases = g(p, "releases", default={})
        if isinstance(releases, dict) and releases:
            rel = releases.get(latest) or releases.get(inst) or next(iter(releases.values())) or {}
        if not rel:
            rel = g(p, "release", default={}) or {}
        name = g(rel, "name") or g(p, "name", "Name", default=pid)
        boards_raw = g(rel, "boards", default=[]) or g(p, "boards", default=[]) or []
        boards = [b.get("name") for b in boards_raw if isinstance(b, dict) and b.get("name")]
        res.append({
            "id": pid, "name": name, "latest": latest, "installed": inst,
            "maintainer": (g(rel, "maintainer") or g(p, "maintainer", "Maintainer", default="")),
            "boards": boards[:12],
        })
    return res


def norm_libs_search(data):
    # arduino-cli v0.x: {"libraries": [...]}  each entry has "latest": {...}
    # arduino-cli v1.5+: {"libraries": [...]}  each entry has "releases": {"1.0.0": {...}, ...}
    #                    "latest" キーは廃止。releases 辞書から最新バージョンを取る
    libs = data if isinstance(data, list) else g(data, "libraries", default=[]) or []
    res = []
    for l in libs:
        if not isinstance(l, dict):
            continue
        name = g(l, "name", "Name")
        if not name:
            continue

        # releases 辞書から最新バージョンのオブジェクトを取得
        rel = {}
        releases = g(l, "releases", default={}) or {}
        if isinstance(releases, dict) and releases:
            latest_key = _latest_ver(releases.keys())
            rel = releases.get(latest_key) or {}

        # v0.x 互換: "latest" サブオブジェクト
        if not rel:
            rel = g(l, "latest", default={}) or {}

        ver    = g(rel, "version") or g(l, "version", default="")
        author = g(rel, "author")  or g(l, "author",  default="")
        desc   = (g(rel, "sentence") or g(rel, "paragraph")
                  or g(l, "sentence") or g(l, "paragraph", default=""))
        res.append({"name": name, "version": ver, "author": author, "desc": desc})
    return res


def sort_libs_by_relevance(libs, query):
    """名前にクエリが含まれるものを優先してソートする"""
    q = query.lower()
    def score(l):
        name = l["name"].lower()
        if name == q:               return 0  # 完全一致
        if name.startswith(q):      return 1  # 前方一致
        if q in name:               return 2  # 名前に含む
        return                             3  # 説明文等にのみ含む
    return sorted(libs, key=score)


def norm_libs_list(data):
    # arduino-cli v0.x: {"installed_libraries": [{"library": {...}}, ...]}
    # arduino-cli v1.0+: same wrapper key, but inner "library" object keys may differ
    items = data if isinstance(data, list) else g(data, "installed_libraries", default=[]) or []
    res = []
    for it in items:
        if not isinstance(it, dict):
            continue
        lib = g(it, "library", default=it)
        if not isinstance(lib, dict):
            continue
        name = g(lib, "name")
        if not name:
            continue
        desc = g(lib, "sentence") or g(lib, "paragraph", default="")
        res.append({
            "name": name, "version": g(lib, "version", default=""),
            "author": g(lib, "author", default=""),
            "desc": desc,
        })
    return res


def validate_cli_collection(data, key, command):
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get(key), list):
        return data
    raise ApiError(502, "arduino-cli の%s応答形式が不正です。サーバーのログを確認してください" % command)


def norm_boards(data):
    # arduino-cli v0.x: {"boards": [...]}  each has "fqbn", "platform" dict
    # arduino-cli v1.0+: same shape; "platform" may have "metadata.id" or direct "id"
    boards = g(data, "boards", default=[]) if isinstance(data, dict) else data
    res = []
    for b in boards or []:
        if not isinstance(b, dict) or b.get("hidden"):
            continue
        fqbn = g(b, "fqbn", "FQBN")
        if not fqbn:
            continue
        plat = g(b, "platform", default={}) or {}
        pid = (g(g(plat, "metadata", default={}), "id")
               or g(plat, "id", "platform_id", default=""))
        res.append({"name": g(b, "name", default=fqbn), "fqbn": fqbn, "platform": pid})
    return res


# ---------------------------------------------------------------
# ジョブ共通
# ---------------------------------------------------------------
def new_job(kind, client, admin):
    jid = secrets.token_hex(12)
    now = time.time()
    job = {"id": jid, "kind": kind, "state": "queued", "lines": [], "rc": None, "t": now,
           "created": now, "client": client, "admin": admin, "result": None, "error": None,
           "title": "", "plan": None, "cancel": False}
    with JOBS_LOCK:
        JOBS[jid] = job
    return job


def cancel_path(job):
    return WORK_DIR / ("cancel_" + job["id"])


def finish(job, state, error=None):
    prev = job["state"]
    job["state"] = state
    job["error"] = error
    job["t"] = time.time()
    job["plan"] = None
    try:
        cancel_path(job).unlink()
    except OSError:
        pass
    if prev not in ("done", "error", "cancelled"):
        try:
            history_record(job, state, error)
        except Exception as e:  # noqa
            log("履歴の記録に失敗: %r" % (e,))


def purge_jobs():
    now = time.time()
    with JOBS_LOCK:
        stale_compile_ids = [
            k for k, v in JOBS.items()
            if v["kind"] == "compile" and v["state"] == "queued"
            and now - v["created"] > 3 * 3600
        ]
        for jid in stale_compile_ids:
            job = JOBS[jid]
            if jid in QUEUE:
                QUEUE.remove(jid)
            job["result"] = {"success": False, "output": "",
                             "errors": ["待機時間が上限を超えたため中断されました"], "time": 0}
            finish(job, "error", "待機時間が上限を超えたため中断されました")
        expired_ids = [
            k for k, v in JOBS.items()
            if v["state"] in ("done", "error", "cancelled")
            and now - v["t"] > JOB_KEEP_SEC
        ]
        for jid in expired_ids:
            JOBS.pop(jid, None)
            if jid in QUEUE:
                QUEUE.remove(jid)
    # 取り残された作業ディレクトリ
    try:
        for p in WORK_DIR.iterdir():
            try:
                if now - p.stat().st_mtime > 3 * 3600:
                    if p.is_dir():
                        rmtree(p)
                    else:
                        p.unlink()
            except OSError:
                pass
    except OSError:
        pass


def rmtree(p):
    def onerr(func, path, exc):
        try:
            os.chmod(os.path.dirname(path), 0o2770)
            os.chmod(path, 0o770)
            func(path)
        except Exception:
            pass
    shutil.rmtree(str(p), onerror=onerr)


# ---------------------------------------------------------------
# 利用履歴 (コンパイル・管理操作の記録)。管理画面 /admin で表示する。
# メモリ (直近 HISTORY_KEEP 件) + HELPER_HOME/history.jsonl に永続化。
# ---------------------------------------------------------------
def load_history():
    global HISTORY_LINES
    try:
        with open(str(HISTORY_FILE), "r", encoding="utf-8", errors="replace") as f:
            lines = collections.deque(maxlen=HISTORY_KEEP)
            line_count = 0
            for line in f:
                lines.append(line)
                line_count += 1
    except OSError:
        return
    recs = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("t"), (int, float)):
            recs.append(r)
    with HISTORY_LOCK:
        HISTORY.clear()
        HISTORY.extend(recs)
        HISTORY_LINES = line_count
    if line_count > HISTORY_TRIM:
        trim_history()


def trim_history():
    """履歴が上限を超えたら直近半分を残し、メモリ上の履歴も同期する。"""
    global HISTORY_LINES
    with HISTORY_LOCK:
        try:
            with open(str(HISTORY_FILE), "r", encoding="utf-8", errors="replace") as f:
                lines = collections.deque((line for line in f if line.strip()),
                                          maxlen=HISTORY_KEEP // 2)
            tmp = HISTORY_FILE.with_suffix(".tmp")
            with open(str(tmp), "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.replace(str(tmp), str(HISTORY_FILE))
            recs = []
            for line in lines:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and isinstance(record.get("t"), (int, float)):
                    recs.append(record)
            HISTORY.clear()
            HISTORY.extend(recs)
            HISTORY_LINES = len(lines)
        except OSError as e:
            log("履歴ファイルを整理できません: %s" % e)


def history_record(job, state, error=None):
    """ジョブ完了時に 1 件記録する (失敗しても処理は止めない)。"""
    global HISTORY_LINES
    res = job.get("result") or {}
    now = time.time()
    rec = {"t": round(now, 1), "id": job["id"], "kind": job["kind"], "state": state,
           "client": job["client"], "admin": bool(job["admin"]),
           "success": bool(state == "done" and res.get("success", True)),
           "elapsed": round(now - job["created"], 1)}
    if job["kind"] == "compile":
        rec["fqbn"] = job.get("fqbn") or ""
        rec["sketch"] = job.get("sketch") or ""
        if res.get("time") is not None:
            rec["time"] = res["time"]
        if res.get("cached"):
            rec["cached"] = True
        size = res.get("size") or {}
        if size.get("program") is not None:
            rec["program"] = size["program"]
    else:
        rec["title"] = job.get("title") or ""
        if job.get("rc") is not None:
            rec["rc"] = job["rc"]
    if not rec["success"]:
        errs = res.get("errors") or []
        msg = error or (errs[0] if errs else None)
        if not msg and state == "cancelled":
            msg = "キャンセル"
        if msg:
            rec["error"] = str(msg)[:300]
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with HISTORY_LOCK:
        HISTORY.append(rec)
        try:
            with open(str(HISTORY_FILE), "a", encoding="utf-8") as f:
                f.write(line)
            HISTORY_LINES += 1
        except OSError as e:
            log("履歴を保存できません: %s" % e)
            return
        trim = HISTORY_LINES > HISTORY_TRIM
    if trim:
        trim_history()


def clear_history():
    """保存済みの利用履歴を原子的に削除し、メモリ上の履歴も空にする。"""
    global HISTORY_LINES
    temp_path = None
    with HISTORY_LOCK:
        deleted = HISTORY_LINES
        fd = None
        try:
            fd, temp_name = tempfile.mkstemp(prefix=".history-", suffix=".tmp", dir=str(HOME))
            temp_path = Path(temp_name)
            os.close(fd)
            fd = None
            os.replace(str(temp_path), str(HISTORY_FILE))
            temp_path = None
            HISTORY.clear()
            HISTORY_LINES = 0
            return deleted
        except OSError as e:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            log("履歴を削除できません: %s" % e)
            raise ApiError(500, "利用履歴を削除できませんでした")
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass


def history_stats(recs):
    """履歴レコード群から管理画面用の集計を作る。"""
    now = time.time()
    total = len(recs)
    ok = sum(1 for r in recs if r.get("success"))
    fail = sum(1 for r in recs if not r.get("success") and r.get("state") != "cancelled")
    cancelled = sum(1 for r in recs if r.get("state") == "cancelled")
    compiles = [r for r in recs if r.get("kind") == "compile"]
    times = [r["time"] for r in compiles if r.get("success") and isinstance(r.get("time"), (int, float))]
    by_board, by_client, by_day = {}, {}, {}

    def day_key(ts):
        return time.strftime("%Y-%m-%d", time.localtime(ts))

    today = day_key(now)
    for r in recs:
        b = r.get("fqbn") or r.get("title") or "(不明)"
        by_board[b] = by_board.get(b, 0) + 1
        c = r.get("client") or "?"
        by_client[c] = by_client.get(c, 0) + 1
        k = day_key(r["t"])
        by_day[k] = by_day.get(k, 0) + 1
    top = lambda d, n: sorted([{"key": k, "count": v} for k, v in d.items()],
                              key=lambda x: -x["count"])[:n]
    week = sorted(day_key(now - 86400 * i) for i in range(7))
    return {
        "total": total, "ok": ok, "fail": fail, "cancelled": cancelled,
        "successRate": round(ok * 100.0 / total, 1) if total else None,
        "avgTime": round(sum(times) / len(times), 1) if times else None,
        "today": by_day.get(today, 0),
        "last24h": sum(1 for r in recs if now - r["t"] <= 86400),
        "last7days": [{"day": d, "count": by_day.get(d, 0)} for d in week],
        "byBoard": top(by_board, 10),
        "byClient": top(by_client, 10),
    }


def _token_file_info(fname, env_name, value):
    """トークンのメタ情報 (本体は絶対に返さない)。"""
    f = HOME / fname
    from_env = bool(os.environ.get(env_name))
    info = {"source": "env" if from_env else ("file" if f.exists() else "不明"),
            "path": "" if from_env else str(f), "length": len(value),
            "fingerprint": hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12],
            "masked": (value[:4] + "…" + value[-4:]) if len(value) > 12 else "********"}
    if not from_env and f.exists():
        try:
            st = f.stat()
            info["mode"] = oct(st.st_mode & 0o777)[2:]
            info["mtime"] = int(st.st_mtime)
        except OSError:
            pass
    return info


def token_infos():
    return {
        "singleToken": SINGLE_TOKEN,
        "user": _token_file_info("token", "HELPER_TOKEN", TOKEN or ""),
        "admin": _token_file_info("admin-token", "HELPER_ADMIN_TOKEN", ADMIN_TOKEN or ""),
    }


def load_admin_settings():
    with ADMIN_SETTINGS_LOCK:
        try:
            with ADMIN_SETTINGS_FILE.open("r", encoding="utf-8") as f:
                settings = json.load(f)
        except FileNotFoundError:
            return {"prefillStudentToken": False}
        except (OSError, ValueError) as e:
            raise ApiError(500, "管理者設定を読み込めません: %s" % e)
    if not isinstance(settings, dict) or not isinstance(settings.get("prefillStudentToken", False), bool):
        raise ApiError(500, "管理者設定の形式が正しくありません")
    return {"prefillStudentToken": settings.get("prefillStudentToken", False)}


def save_admin_settings(settings):
    with ADMIN_SETTINGS_LOCK:
        HOME.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(settings, ensure_ascii=False, separators=(",", ":")) + "\n"
        fd, temp_path = tempfile.mkstemp(prefix=".admin-settings-", dir=str(HOME))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(raw)
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, str(ADMIN_SETTINGS_FILE))
            os.chmod(str(ADMIN_SETTINGS_FILE), 0o600)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise


def process_rss_mb():
    try:
        with open("/proc/self/statm", "r") as f:
            pages = int(f.read().split()[1])
        return round(pages * (os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)), 1)
    except (OSError, ValueError, IndexError, AttributeError):
        return None


# ---------------------------------------------------------------
# 管理ジョブ (ボード・ライブラリの変更)
# ---------------------------------------------------------------
def start_admin_job(title, args, cleanup=None, client="admin"):
    with JOBS_LOCK:
        waiting = sum(1 for item in JOBS.values()
                      if item["kind"] == "admin" and item["state"] == "queued")
        overloaded = waiting >= ADMIN_MAX_QUEUE
        if not overloaded:
            job = new_job("admin", client, True)
            job["title"] = title
    if overloaded:
        if cleanup:
            cleanup()
        raise ApiError(429, "管理作業が混み合っています。処理が終わってからもう一度お試しください",
                       {"retryAfter": 30})

    def worker():
        global ADMIN_WAITING
        waiting = False
        acquired = False
        try:
            with JOBS_LOCK:
                ADMIN_WAITING += 1
                waiting = True
            if CLI_LOCK.locked():
                job["lines"].append("待機中...(他の処理が終わるまでお待ちください)")
            if not CLI_LOCK.acquire(timeout=OP_TIMEOUT):
                finish(job, "error", "CLI の待機時間が上限を超えました")
                return
            acquired = True
            with JOBS_LOCK:
                ADMIN_WAITING -= 1
                waiting = False
            if job["cancel"]:
                finish(job, "cancelled")
                return
            job["state"] = "running"
            def on_line(l):
                if len(job["lines"]) < MAX_JOB_LINES:
                    job["lines"].append(l)
            rc, _ = run_cli(args, timeout=OP_TIMEOUT, on_line=on_line, cancel_file=cancel_path(job))
            job["rc"] = rc
            if job["cancel"]:
                finish(job, "cancelled")
            else:
                if rc == 0:
                    bump_gen()
                finish(job, "done" if rc == 0 else "error", None if rc == 0 else "失敗しました")
        except Exception as e:  # noqa
            log("管理ジョブの内部エラー: %r" % (e,))
            job["lines"].append("内部エラーが発生しました")
            finish(job, "error", "内部エラー")
        finally:
            if acquired:
                CLI_LOCK.release()
            if waiting:
                with JOBS_LOCK:
                    ADMIN_WAITING -= 1
            if cleanup:
                try:
                    cleanup()
                except Exception:
                    pass

    thread = threading.Thread(target=worker, daemon=True)
    try:
        thread.start()
    except RuntimeError as e:
        log("管理ジョブのスレッドを開始できません: %r" % (e,))
        finish(job, "error", "管理作業を開始できません")
        if cleanup:
            try:
                cleanup()
            except Exception as cleanup_error:
                log("管理ジョブの一時データを削除できません: %r" % (cleanup_error,))
        raise ApiError(503, "管理作業を開始できません")
    return job["id"]


# ---------------------------------------------------------------
# ソースの検査 (#include / .incbin などによるファイル読み出しの抑止)
#   これは「うっかり・安直な悪用」を防ぐ補助です。完全な防御ではありません。
#   確実な防御は、コンパイルを別ユーザー (arduino-build) で実行することです。
# ---------------------------------------------------------------
def strip_comments(text):
    """文字列・文字リテラルを尊重しながら、コメントを空白に置き換える。"""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        c2 = text[i:i + 2]
        if c2 == "//":
            j = text.find("\n", i)
            if j < 0:
                j = n
            out.append(" ")
            i = j
        elif c2 == "/*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(" ")
            i = j
        elif c == 'R' and text[i + 1:i + 2] == '"':
            m = re.match(r'R"([^()\\ \t\n]{0,16})\(', text[i:])
            if m:
                end = text.find(")" + m.group(1) + '"', i + len(m.group(0)))
                end = n if end < 0 else end + len(m.group(1)) + 2
                out.append(text[i:end])
                i = end
            else:
                out.append(c)
                i += 1
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


RE_DIRECTIVE = re.compile(r"(?:^|\n)[ \t\f\v]*(?:#|%:|\?\?=)[ \t\f\v]*(include_next|include|import|embed)\b([^\n]*)")
RE_GAS = re.compile(r"(?<![\w])\.\s*(?:incbin|include)\b", re.I)
RE_ASM_KW = re.compile(r"\b(__asm__|__asm|asm)\b")
RE_ASM_ALIAS = re.compile(r"#\s*define\s+\w+(?:\([^)]*\))?\s+(?:__asm__|__asm|asm)\b")
RE_LITERALS = re.compile(r'^\s*(?:"(?:[^"\\\n]|\\.)*"\s*)+$')


def _bad_include_path(p):
    return (p.startswith(("/", "~", "\\")) or ".." in p or re.match(r"^[A-Za-z]:", p) is not None
            or "\x00" in p)


def blank_strings(T):
    """文字列リテラルの中身を \x01 に置き換える (同じ長さ)。キーワード検索で文字列内の asm を拾わないため。"""
    out = []
    i, n = 0, len(T)
    while i < n:
        c = T[i]
        if c in "\"'":
            j = i + 1
            while j < n and T[j] != c and T[j] != "\n":
                j += 2 if T[j] == "\\" else 1
            j = min(j, n - 1)
            out.append(c + "\x01" * max(0, j - i - 1) + (T[j] if j > i else ""))
            i = j + 1
        else:
            out.append(c)
            i += 1
    r = "".join(out)
    return r if len(r) == n else T


def _scan_directives(name, T):
    for m in RE_DIRECTIVE.finditer(T):
        kind, rest = m.group(1), m.group(2).strip()
        if kind == "embed":
            return "%s: #embed は使用できません" % name
        mm = re.match(r'^(?:<([^>\n]*)>|"([^"\n]*)")', rest)
        if not mm:
            return "%s: #%s にはマクロではなく <ファイル> か \"ファイル\" を直接書いてください" % (name, kind)
        path = mm.group(1) if mm.group(1) is not None else mm.group(2)
        if _bad_include_path(path):
            return "%s: #include に絶対パスや .. を含むパスは使用できません" % name
    if re.search(r"__has_embed", T):
        return "%s: __has_embed は使用できません" % name
    if re.search(r'__has_include(?:_next)?\s*\(\s*["<]\s*(?:/|~|[^">]*\.\.)', T):
        return "%s: __has_include に絶対パスや .. は使用できません" % name
    return None


def _scan_asm(name, T):
    """コメント除去済みのテキストに対する、アセンブラ指令 (.incbin / .include) の検査。"""
    if ALLOW_ASM:
        return None
    joined = re.sub(r'"\s*"', "", T)
    if RE_GAS.search(joined) or RE_GAS.search(T):
        return "%s: アセンブラ指令 (.incbin / .include) は使用できません" % name
    if RE_ASM_ALIAS.search(T):
        return "%s: asm の別名マクロは使用できません" % name
    B = blank_strings(T)
    for m in RE_ASM_KW.finditer(B):
        i = m.end()
        mq = re.match(r"\s*(?:(?:volatile|__volatile__|__volatile|goto|inline|__inline__)\s*)*\(", T[i:])
        if not mq:
            continue
        i += mq.end()
        depth, j, in_str, esc = 1, i, False, False
        first_colon = None
        while j < len(T) and depth > 0:
            ch = T[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == ":" and depth == 1 and first_colon is None:
                first_colon = j
            j += 1
        if depth != 0:
            return "%s: asm 文の括弧が閉じていません" % name
        body = T[i:(first_colon if first_colon is not None else j - 1)]
        if not RE_LITERALS.match(body):
            return "%s: asm の命令は文字列リテラルだけで書いてください" % name
        text = "".join(re.findall(r'"((?:[^"\\\n]|\\.)*)"', body))
        text = text.replace("\\n", "\n").replace("\\t", " ")
        if re.search(r"(?:^|[\s;])\.\s*[A-Za-z]", text):
            return "%s: asm 内のアセンブラ指令 (.xxx) は使用できません" % name
    return None


def scan_source(name, content):
    spliced = re.sub(r"\\[ \t]*\r?\n", "", content.replace("\r\n", "\n").replace("\r", "\n"))
    stripped = strip_comments(spliced)
    for T in (spliced, stripped):
        err = _scan_directives(name, T)
        if err:
            return err
    err = _scan_asm(name, stripped)
    if err:
        return err
    if "\x00" in content:
        return "%s: 制御文字 (NUL) が含まれています" % name
    return None


# ---------------------------------------------------------------
# コンパイル: 検証 → キュー → 実行
# ---------------------------------------------------------------
def safe_sketch_name(raw):
    raw = (raw or "sketch").strip()
    s = re.sub(r"[^A-Za-z0-9_]", "_", raw)
    if s != raw or not s:
        s = (s or "sketch") + "_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]
    if not re.match(r"[A-Za-z0-9_]", s[0]):
        s = "s" + s
    return s[:60]


def validate_compile(req):
    if not isinstance(req, dict):
        raise ApiError(400, "リクエストが不正です")
    fqbn = req.get("board")
    if not isinstance(fqbn, str) or not RE_FQBN.match(fqbn):
        raise ApiError(400, "ボード(FQBN)が不正です")
    files = req.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise ApiError(400, "files が不正です (1〜%d 個)" % MAX_FILES)
    clean = []
    seen = set()
    total = 0
    for f in files:
        if not isinstance(f, dict):
            raise ApiError(400, "files が不正です")
        name = str(f.get("name", ""))
        content = f.get("content", "")
        if name != os.path.basename(name) or not RE_FILE.match(name) or Path(name).suffix not in ALLOWED_EXT:
            raise ApiError(400, "使用できないファイル名です: %s (使えるのは %s)" % (
                name[:60], " ".join(sorted(ALLOWED_EXT))))
        if not isinstance(content, str):
            raise ApiError(400, "内容が不正です: %s" % name)
        key = name.casefold()
        if key in seen:
            raise ApiError(400, "同じ名前のファイルが複数あります: %s" % name)
        seen.add(key)
        total += len(content.encode("utf-8", "replace"))
        err = scan_source(name, content)
        if err:
            raise ApiError(400, err)
        clean.append((name, content))
    if total > MAX_SOURCE_TOTAL:
        raise ApiError(413, "ソースが大きすぎます")
    sketch = safe_sketch_name(req.get("sketch"))
    inos = [n for n, _ in clean if n.endswith(".ino")]
    if not inos:
        raise ApiError(400, ".ino ファイルがありません")
    main_orig = None
    for n in inos:
        if n == sketch + ".ino" or n[:-4] == (req.get("sketch") or ""):
            main_orig = n
            break
    main_orig = main_orig or inos[0]
    main_new = sketch + ".ino"
    written = {}
    for name, content in clean:
        w = main_new if name == main_orig else name
        if w.casefold() in {k.casefold() for k in written}:
            raise ApiError(400, "ファイル名が重複します: %s (メインの .ino は %s に変換されます)" % (name, main_new))
        written[w] = (name, content)
    return {"fqbn": fqbn, "sketch": sketch, "main": main_new, "files": written}


def plan_key(plan):
    h = hashlib.sha256()
    h.update(("%d|%s|%s|%s\n" % (GEN, VERSION, plan["fqbn"], plan["sketch"])).encode())
    for w in sorted(plan["files"]):
        orig, content = plan["files"][w]
        h.update(("%s\0%s\0" % (w, orig)).encode("utf-8", "replace"))
        h.update(content.encode("utf-8", "replace"))
        h.update(b"\1")
    return h.hexdigest()


def result_size(res):
    return sum(len(v) for v in (res.get("artifacts") or {}).values()) + len(res.get("output", ""))


def cache_put(key, res):
    global RESULT_CACHE_SIZE
    if not res.get("success"):
        return
    sz = result_size(res)
    if sz > RESULT_CACHE_BYTES // 4:
        return
    with JOBS_LOCK:
        old = RESULT_CACHE.pop(key, None)
        if old:
            RESULT_CACHE_SIZE -= result_size(old)
        RESULT_CACHE[key] = res
        RESULT_CACHE_SIZE += sz
        while len(RESULT_CACHE) > RESULT_CACHE_ENTRIES or RESULT_CACHE_SIZE > RESULT_CACHE_BYTES:
            _, v = RESULT_CACHE.popitem(last=False)
            RESULT_CACHE_SIZE -= result_size(v)


def clear_result_cache():
    global RESULT_CACHE_SIZE
    RESULT_CACHE.clear()
    RESULT_CACHE_SIZE = 0


def submit_compile(req, client):
    plan = validate_compile(req)
    key = plan_key(plan)
    with JOBS_LOCK:
        active = sum(1 for j in JOBS.values()
                     if j["kind"] == "compile" and j["client"] == client and j["state"] in ("queued", "running"))
        if active >= PER_CLIENT:
            raise ApiError(429, "この端末のコンパイルがまだ終わっていません。完了してからもう一度実行してください",
                           {"retryAfter": 10})
        hit = RESULT_CACHE.get(key)
        if hit is not None:
            RESULT_CACHE.move_to_end(key)
            job = new_job("compile", client, False)
            job["fqbn"], job["sketch"] = plan["fqbn"], plan["sketch"]
            res = dict(hit)
            res["cached"] = True
            job["result"] = res
            job["rc"] = 0
            finish(job, "done")
            return job, 0
        if len(QUEUE) >= MAX_QUEUE:
            raise ApiError(429, "コンパイルサーバーが混み合っています。少し待ってからもう一度実行してください",
                           {"retryAfter": 30})
        job = new_job("compile", client, False)
        job["fqbn"], job["sketch"] = plan["fqbn"], plan["sketch"]
        job["plan"] = plan
        job["key"] = key
        QUEUE.append(job["id"])
        QUEUE_CV.notify()
        return job, len(QUEUE)


def free_mb():
    paths = [HOME, WORK_DIR]
    data_dir = os.environ.get("ARDUINO_DIRECTORIES_DATA")
    user_dir = os.environ.get("ARDUINO_DIRECTORIES_USER")
    downloads_dir = os.environ.get("ARDUINO_DIRECTORIES_DOWNLOADS")
    if data_dir:
        data_path = Path(data_dir)
        paths.extend((data_path, data_path / "build-cache"))
    if user_dir:
        paths.append(Path(user_dir))
    if downloads_dir:
        paths.append(Path(downloads_dir))

    try:
        free_bytes = []
        for path in dict.fromkeys(paths):
            while not path.exists():
                parent = path.parent
                if parent == path:
                    raise OSError("No existing parent directory for %s" % path)
                path = parent
            free_bytes.append(shutil.disk_usage(str(path)).free)
        return min(free_bytes) // (1024 * 1024)
    except OSError:
        return None


def run_compile(job):
    """作業ディレクトリを作り、コンパイルし、必ず片付ける。"""
    global COMPILES_DONE
    plan = job["plan"]
    fqbn, sketch, main_new = plan["fqbn"], plan["sketch"], plan["main"]
    available_mb = free_mb()
    if available_mb is None:
        raise ApiError(503, "サーバーの空き容量を確認できません。管理者に連絡してください")
    if available_mb < MIN_FREE_MB:
        raise ApiError(507, "サーバーのディスク空きが不足しています。管理者に連絡してください")
    jobdir = WORK_DIR / ("j_" + job["id"])
    sdir = jobdir / sketch
    odir = jobdir / "out"
    bdir = jobdir / "build"
    try:
        for d in (jobdir, sdir, odir, bdir):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(str(d), 0o2770)
        name_map = {}
        for w, (orig, content) in plan["files"].items():
            fp = sdir / w
            with open(str(fp), "w", encoding="utf-8", errors="replace", newline="") as fh:
                fh.write(content)
            os.chmod(str(fp), 0o660)
            name_map[w] = orig
            # --build-path は各ジョブ専用に固定する。Arduino CLI の build cache は
        # 設定側で管理し、compile コマンドへ --build-cache-path を渡さない。
        # これにより CLI バージョン差異とキャッシュ経路の混在を避ける。
        base = ["compile", "--fqbn", fqbn, "--output-dir", str(odir), "--build-path", str(bdir)]
        t0 = time.time()
        rc, out = run_cli(base + [str(sdir)],
                          timeout=COMPILE_TIMEOUT, cancel_file=cancel_path(job), cleanup=[bdir])
        elapsed = round(time.time() - t0, 1)
        if job["cancel"]:
            return None

        def remap(text):
            for w, orig in name_map.items():
                text = text.replace(str(sdir / w), orig)
            text = text.replace(str(sdir) + "/", "")
            return text.replace(str(jobdir) + "/", "")

        out = remap(out)
        errors = [l for l in out.splitlines() if re.search(r"\b(error|fatal error)\b", l)]
        if rc == 124:
            return {"success": False, "output": out, "errors": ["コンパイルが時間切れになりました"], "time": elapsed}
        if rc != 0:
            return {"success": False, "output": out, "errors": errors or ["コンパイルに失敗しました"],
                    "time": elapsed}

        artifacts, skipped = {}, []
        if odir.is_dir():
            for p in sorted(odir.iterdir()):
                if p.is_file() and p.suffix in ARTIFACT_EXT:
                    if p.stat().st_size <= MAX_ARTIFACT:
                        artifacts[p.name] = base64.b64encode(p.read_bytes()).decode("ascii")
                    else:
                        skipped.append(p.name)
        size = {}
        m = re.search(r"Sketch uses (\d+) bytes.*?Maximum is (\d+) bytes", out, re.S)
        if m:
            size["program"], size["programMax"] = int(m.group(1)), int(m.group(2))
        m = re.search(r"Global variables use (\d+) bytes.*?Maximum is (\d+) bytes", out, re.S)
        if m:
            size["data"], size["dataMax"] = int(m.group(1)), int(m.group(2))
        res = {"success": True, "output": out, "errors": [], "artifacts": artifacts,
               "size": size or None, "time": elapsed, "main": main_new, "gen": GEN}
        if skipped:
            res["skipped"] = skipped
        return res
    finally:
        rmtree(jobdir)
        with JOBS_LOCK:
            COMPILES_DONE += 1


def compile_worker():
    while True:
        with JOBS_LOCK:
            while not QUEUE:
                QUEUE_CV.wait(timeout=5)
            jid = QUEUE[0]
            job = JOBS.get(jid)
            if job is None or job["state"] != "queued":
                QUEUE.remove(jid)
                continue
        # 管理作業 (ボード・ライブラリ変更) を優先して待つ
        while ADMIN_WAITING:
            time.sleep(0.2)
        CLI_LOCK.acquire()
        try:
            with JOBS_LOCK:
                if jid not in QUEUE or job["state"] != "queued":
                    continue
                QUEUE.remove(jid)
                job["state"] = "running"
                job["t"] = time.time()
            try:
                res = run_compile(job)
                if job["cancel"] or res is None:
                    finish(job, "cancelled")
                else:
                    job["result"] = res
                    cache_put(job["key"], res)
                    finish(job, "done")
            except ApiError as e:
                job["result"] = {"success": False, "output": "", "errors": [e.msg], "time": 0}
                finish(job, "error", e.msg)
            except Exception as e:  # noqa
                log("コンパイルの内部エラー: %s" % traceback.format_exc())
                finish(job, "error", "サーバー内部でエラーが発生しました")
        finally:
            CLI_LOCK.release()


def cancel_job(job):
    with JOBS_LOCK:
        if job["state"] == "queued":
            job["cancel"] = True
            if job["id"] in QUEUE:
                QUEUE.remove(job["id"])
            finish(job, "cancelled")
            return True
        if job["state"] == "running":
            job["cancel"] = True
            try:
                cancel_path(job).write_text("1")
            except OSError:
                pass
            return True
    return False


# ---------------------------------------------------------------
# ビルドキャッシュの掃除
# ---------------------------------------------------------------
def housekeeping():
    while True:
        time.sleep(60)
        try:
            purge_jobs()
            if HISTORY_LINES > HISTORY_TRIM:
                trim_history()
        except Exception as e:  # noqa
            log("housekeeping: %r" % (e,))


# ---------------------------------------------------------------
# 認証
# ---------------------------------------------------------------
AUTH_FAILS = {}
AUTH_BLOCK = {}
AUTH_LOCK = threading.Lock()


def auth_blocked(ip):
    with AUTH_LOCK:
        now = time.time()
        until = AUTH_BLOCK.get(ip, 0)
        if until > now:
            return int(until - time.time()) + 1
        AUTH_BLOCK.pop(ip, None)
    return 0


def auth_failed(ip):
    now = time.time()
    with AUTH_LOCK:
        for address, until in list(AUTH_BLOCK.items()):
            if until <= now:
                AUTH_BLOCK.pop(address, None)
        lst = [t for t in AUTH_FAILS.get(ip, []) if now - t < 60]
        lst.append(now)
        AUTH_FAILS[ip] = lst
        if len(lst) >= 10:
            if ip not in AUTH_BLOCK and len(AUTH_BLOCK) >= AUTH_STATE_LIMIT:
                oldest = min(AUTH_BLOCK, key=AUTH_BLOCK.get)
                AUTH_BLOCK.pop(oldest, None)
            AUTH_BLOCK[ip] = now + 120
            AUTH_FAILS.pop(ip, None)
        if len(AUTH_FAILS) > AUTH_STATE_LIMIT:
            oldest = min(AUTH_FAILS, key=lambda address: AUTH_FAILS[address][-1])
            AUTH_FAILS.pop(oldest, None)


def auth_succeeded(ip):
    with AUTH_LOCK:
        AUTH_FAILS.pop(ip, None)
        AUTH_BLOCK.pop(ip, None)


def role_for(token):
    if not token:
        return None
    t = token.encode("utf-8", "replace")
    if hmac.compare_digest(t, ADMIN_TOKEN.encode()):
        return "admin"
    if hmac.compare_digest(t, TOKEN.encode()):
        return "admin" if SINGLE_TOKEN else "user"
    return None


@contextlib.contextmanager
def account_db():
    global ACCOUNT_DB_INITIALIZED_PATH
    HOME.mkdir(parents=True, exist_ok=True)
    db_path = HOME / "accounts.sqlite3"
    fd = os.open(str(db_path), os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(str(db_path), 0o600)
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        path_key = str(db_path.resolve())
        with ACCOUNT_DB_INIT_LOCK:
            if ACCOUNT_DB_INITIALIZED_PATH != path_key:
                conn.execute("PRAGMA journal_mode = WAL")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version > 1:
                    raise RuntimeError("アカウントDBのバージョンが新しすぎます")
                if version < 1:
                    conn.execute("BEGIN IMMEDIATE")
                    for table in (
                            "account_comments", "account_sketch_versions", "account_sketches",
                            "account_sessions", "account_audit", "account_classes", "account_users"):
                        conn.execute("DROP TABLE IF EXISTS " + table)
                    conn.execute("""
                        CREATE TABLE account_users (
                            id TEXT PRIMARY KEY,
                            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                            display_name TEXT NOT NULL,
                            role TEXT NOT NULL DEFAULT 'user' CHECK(role='user'),
                            password_hash TEXT NOT NULL,
                            password_salt TEXT NOT NULL,
                            active INTEGER NOT NULL DEFAULT 1,
                            created_at INTEGER NOT NULL
                        )
                    """)
                    conn.execute("""
                        CREATE TABLE account_sessions (
                            token_hash TEXT PRIMARY KEY,
                            user_id TEXT NOT NULL REFERENCES account_users(id) ON DELETE CASCADE,
                            expires_at INTEGER NOT NULL,
                            created_at INTEGER NOT NULL
                        )
                    """)
                    conn.execute("""
                        CREATE TABLE account_sketches (
                            id TEXT NOT NULL,
                            user_id TEXT NOT NULL REFERENCES account_users(id) ON DELETE CASCADE,
                            data TEXT NOT NULL,
                            revision INTEGER NOT NULL DEFAULT 1,
                            updated_at INTEGER NOT NULL,
                            PRIMARY KEY(id, user_id)
                        )
                    """)
                    conn.execute("""
                        CREATE TABLE account_sketch_versions (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            sketch_id TEXT NOT NULL,
                            user_id TEXT NOT NULL REFERENCES account_users(id) ON DELETE CASCADE,
                            revision INTEGER NOT NULL,
                            editor_id TEXT NOT NULL,
                            data TEXT NOT NULL,
                            created_at INTEGER NOT NULL
                        )
                    """)
                    conn.execute("""
                        CREATE TABLE account_audit (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            actor_id TEXT REFERENCES account_users(id) ON DELETE SET NULL,
                            action TEXT NOT NULL,
                            target TEXT NOT NULL,
                            details TEXT NOT NULL,
                            created_at INTEGER NOT NULL
                        )
                    """)
                    conn.execute("CREATE INDEX account_sessions_expiry ON account_sessions(expires_at)")
                    conn.execute("CREATE INDEX account_sessions_user ON account_sessions(user_id,created_at)")
                    conn.execute("CREATE INDEX account_sketches_user ON account_sketches(user_id,updated_at)")
                    conn.execute(
                        "CREATE INDEX account_sketch_versions_lookup "
                        "ON account_sketch_versions(user_id,sketch_id,revision)"
                    )
                    conn.execute("CREATE INDEX account_audit_created ON account_audit(created_at)")
                    conn.execute("PRAGMA user_version = 1")
                    conn.commit()
                ACCOUNT_DB_INITIALIZED_PATH = path_key
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _account_password(password, salt=None):
        if not isinstance(password, str) or len(password) < 10 or len(password) > 256:
            raise ApiError(400, "パスワードは10文字以上256文字以内で指定してください")
        salt_bytes = bytes.fromhex(salt) if salt else secrets.token_bytes(16)
        digest = _account_password_digest(password, salt_bytes)
        return salt_bytes.hex(), digest.hex()


def _account_password_digest(password, salt_bytes):
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, 310000)


def _account_password_matches(password, salt, expected):
        if not isinstance(password, str) or len(password) > 256:
            return False
        digest = _account_password_digest(password, bytes.fromhex(salt)).hex()
        return hmac.compare_digest(digest, expected)


def _account_user(row):
        return {"id": row["id"], "username": row["username"], "name": row["display_name"]}


def _account_audit(conn, actor, action, target, details=None):
        conn.execute("INSERT INTO account_audit(actor_id, action, target, details, created_at) VALUES(?,?,?,?,?)",
                     (actor, action, target, json.dumps(details or {}, ensure_ascii=False), int(time.time())))
        conn.execute(
            "DELETE FROM account_audit WHERE id NOT IN "
            "(SELECT id FROM account_audit ORDER BY id DESC LIMIT ?)",
            (ACCOUNT_AUDIT_LIMIT,),
        )


def _account_signup_allowed(ip):
    now = time.time()
    with ACCOUNT_SIGNUP_LOCK:
        attempts = [timestamp for timestamp in ACCOUNT_SIGNUP_ATTEMPTS.get(ip, [])
                    if now - timestamp < ACCOUNT_SIGNUP_WINDOW]
        if len(attempts) >= ACCOUNT_SIGNUP_LIMIT:
            ACCOUNT_SIGNUP_ATTEMPTS[ip] = attempts
            return max(1, int(ACCOUNT_SIGNUP_WINDOW - (now - attempts[0])))
        attempts.append(now)
        ACCOUNT_SIGNUP_ATTEMPTS[ip] = attempts
        if len(ACCOUNT_SIGNUP_ATTEMPTS) > AUTH_STATE_LIMIT:
            oldest = min(ACCOUNT_SIGNUP_ATTEMPTS,
                         key=lambda address: ACCOUNT_SIGNUP_ATTEMPTS[address][-1])
            ACCOUNT_SIGNUP_ATTEMPTS.pop(oldest, None)
        return 0


def _trim_account_sessions(conn, user_id):
    conn.execute(
        "DELETE FROM account_sessions WHERE user_id=? AND token_hash NOT IN "
        "(SELECT token_hash FROM account_sessions WHERE user_id=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?)",
        (user_id, user_id, ACCOUNT_SESSION_LIMIT),
    )


def _clean_expired_account_sessions(conn, now):
    global ACCOUNT_SESSION_CLEANUP_LAST
    if now - ACCOUNT_SESSION_CLEANUP_LAST < 3600:
        return
    with ACCOUNT_SESSION_CLEANUP_LOCK:
        if now - ACCOUNT_SESSION_CLEANUP_LAST >= 3600:
            conn.execute("DELETE FROM account_sessions WHERE expires_at<=?", (now,))
            ACCOUNT_SESSION_CLEANUP_LAST = now


def _session_user(handler, conn):
        header = handler.headers.get("Authorization", "")
        token = header[8:].strip() if header.startswith("Session ") else ""
        if not token or len(token) > 256:
            raise ApiError(401, "ログインしてください")
        token_hash = hashlib.sha256(token.encode("ascii", "ignore")).hexdigest()
        row = conn.execute(
            "SELECT u.* FROM account_sessions s JOIN account_users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>? AND u.active=1",
            (token_hash, int(time.time())),
        ).fetchone()
        if not row:
            raise ApiError(401, "ログインの有効期限が切れました。もう一度ログインしてください")
        return row


def _valid_sketch(sketch):
        if not isinstance(sketch, dict):
            raise ApiError(400, "スケッチ形式が不正です")
        sid = sketch.get("id")
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", sid):
            raise ApiError(400, "スケッチIDが不正です")
        name = sketch.get("name")
        files = sketch.get("files")
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ApiError(400, "スケッチ名が不正です")
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
            raise ApiError(400, "ファイル数は1〜%d個にしてください" % MAX_FILES)
        total = 0
        clean_files = []
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("content"), str):
                raise ApiError(400, "スケッチ内のファイル形式が不正です")
            if len(item["name"]) > 240 or "\x00" in item["name"] or ".." in item["name"].replace("\\", "/").split("/"):
                raise ApiError(400, "ファイル名が不正です")
            total += len(item["content"].encode("utf-8"))
            clean_files.append({"name": item["name"], "content": item["content"]})
        if total > MAX_SOURCE_TOTAL:
            raise ApiError(413, "スケッチの合計サイズは2MBまでです")
        timestamps = []
        for key in ("createdAt", "updatedAt"):
            value = sketch.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
                raise ApiError(400, "%s が不正です" % key)
            timestamps.append(int(value if value is not None else time.time() * 1000))
        return {"id": sid, "name": name.strip(), "files": clean_files,
                "board": str(sketch.get("board") or "")[:160],
                "createdAt": timestamps[0], "updatedAt": timestamps[1]}


def _account_sketch_view(row):
        sketch = json.loads(row["data"])
        sketch["revision"] = row["revision"]
        return sketch


def _trim_account_versions(conn, user_id, sketch_id):
    conn.execute(
        "DELETE FROM account_sketch_versions WHERE id IN ("
        "SELECT id FROM account_sketch_versions WHERE user_id=? AND sketch_id=? "
        "ORDER BY revision DESC, id DESC LIMIT -1 OFFSET 100)",
        (user_id, sketch_id),
    )


# ---------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------
def check_name(value, rx, label):
    if not isinstance(value, str) or not rx.match(value):
        raise ApiError(400, "%s が不正です" % label)
    return value


def read_cached(key, ttl, fn):
    now = time.time()
    with JOBS_LOCK:
        hit = READ_CACHE.get(key)
        if hit and hit[0] > now and hit[1] == GEN:
            return hit[2]
        gen = GEN
    if not READ_SEM.acquire(timeout=25):
        raise ApiError(503, "サーバーが混み合っています。しばらくしてからもう一度お試しください")
    try:
        data = fn()
    finally:
        READ_SEM.release()
    with JOBS_LOCK:
        READ_CACHE[key] = (now + ttl, gen, data)
        if len(READ_CACHE) > 300:
            READ_CACHE.clear()
    return data


# ---------------------------------------------------------------
# 管理画面 API (/admin/api/*。管理者トークンが必要)
# ---------------------------------------------------------------
def _int_q(q, name, default, lo, hi):
    try:
        v = int(q.get(name, default) or default)
    except ValueError:
        raise ApiError(400, "%s が不正です" % name)
    return max(lo, min(hi, v))


def admin_overview():
    """サーバー状態・トークン詳細・統計・アクティブジョブをまとめて返す。"""
    now = time.time()
    with JOBS_LOCK:
        jobs = list(JOBS.values())
        queued_ids = list(QUEUE)
        cache_entries, cache_bytes = len(RESULT_CACHE), RESULT_CACHE_SIZE
        read_cache_entries = len(READ_CACHE)
        gen = GEN
        compiles_done = COMPILES_DONE
    active = []
    for j in jobs:
        if j["state"] not in ("queued", "running"):
            continue
        item = {"id": j["id"], "kind": j["kind"], "state": j["state"], "client": j["client"],
                "admin": bool(j["admin"]), "age": round(now - j["created"], 1),
                "title": j.get("title") or j.get("sketch") or j.get("fqbn") or "",
                "detail": j.get("fqbn") or "", "lines": len(j["lines"])}
        if j["state"] == "queued" and j["id"] in queued_ids:
            item["position"] = queued_ids.index(j["id"]) + 1
        active.append(item)
    active.sort(key=lambda x: (0 if x["state"] == "queued" else 1, x.get("position", 0), x["age"]))
    with HISTORY_LOCK:
        recs = list(HISTORY)
        hist_lines = HISTORY_LINES
    with AUTH_LOCK:
        blocks = [{"ip": ip, "remain": int(un - now) + 1}
                  for ip, un in AUTH_BLOCK.items() if un > now]
        fails = sum(1 for lst in AUTH_FAILS.values() for t in lst if now - t < 60)
    stats = history_stats(recs)
    stats["compilesSinceStart"] = compiles_done
    return {
        "ok": True,
        "now": round(now, 1),
        "server": {
            "version": VERSION, "started": round(STARTED, 1), "uptime": round(now - STARTED, 1),
            "pid": os.getpid(), "python": sys.version.split()[0],
            "host": HOST, "port": PORT, "home": str(HOME),
            "wrapper": WRAPPER or "", "wrapperFound": cli_available(),
            "buildUser": BUILD_USER or "", "singleToken": SINGLE_TOKEN, "allowAsm": ALLOW_ASM,
            "corsOrigins": list(CORS_ORIGINS), "trustCfConnectingIp": TRUST_CF_CONNECTING_IP,
            "cliVersion": cli_version() if cli_available() else "",
            "cliBusy": CLI_LOCK.locked(), "gen": gen,
            "freeMb": free_mb(), "rssMb": process_rss_mb(),
        },
        "limits": {
            "maxQueue": MAX_QUEUE, "perClient": PER_CLIENT, "adminMaxQueue": ADMIN_MAX_QUEUE,
            "compileTimeout": COMPILE_TIMEOUT,
            "minFreeMb": MIN_FREE_MB, "jobKeepSec": JOB_KEEP_SEC, "maxBodyMb": MAX_BODY // (1024 * 1024),
            "maxSourceMb": MAX_SOURCE_TOTAL // (1024 * 1024), "maxFiles": MAX_FILES,
            "resultCacheEntries": RESULT_CACHE_ENTRIES, "resultCacheMb": RESULT_CACHE_BYTES // (1024 * 1024),
        },
        "queue": {"queued": len(queued_ids), "running": sum(1 for j in active if j["state"] == "running"),
                  "adminWaiting": ADMIN_WAITING, "active": active},
        "jobsInMemory": len(jobs),
        "tokens": token_infos(),
        "caches": {"resultEntries": cache_entries, "resultBytes": cache_bytes,
                   "readEntries": read_cache_entries, "historyLines": hist_lines},
        "auth": {"blocked": blocks, "recentFails": fails},
        "webhook": {"enabled": bool(GITHUB_WEBHOOK_SECRET and GITHUB_WEBHOOK_REPO),
                    "repo": GITHUB_WEBHOOK_REPO, "branch": GITHUB_WEBHOOK_BRANCH,
                    "last": round(WEBHOOK_LAST, 1) if WEBHOOK_LAST else 0},
        "stats": stats,
        "history": list(reversed(recs))[:50],
    }


def admin_history(q):
    """利用履歴を新しい順にページングして返す。"""
    offset = _int_q(q, "offset", 0, 0, 10 ** 9)
    limit = _int_q(q, "limit", 50, 1, 200)
    kind = q.get("kind", "").strip()
    state = q.get("state", "").strip()
    text = q.get("q", "").strip().lower()
    with HISTORY_LOCK:
        recs = list(HISTORY)
        kept = len(recs)
    recs.reverse()   # 新しい順
    if kind in ("compile", "admin"):
        recs = [r for r in recs if r.get("kind") == kind]
    if state == "ok":
        recs = [r for r in recs if r.get("success")]
    elif state == "ng":
        recs = [r for r in recs if not r.get("success") and r.get("state") != "cancelled"]
    elif state:
        recs = [r for r in recs if r.get("state") == state]
    if text:
        def hay(r):
            return " ".join(str(r.get(k, "")) for k in
                            ("fqbn", "sketch", "title", "client", "id", "error")).lower()
        recs = [r for r in recs if text in hay(r)]
    return {"ok": True, "total": len(recs), "offset": offset, "kept": kept,
            "items": recs[offset:offset + limit]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "arduino-helper/" + VERSION
    timeout = 60

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s - %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), self._client_ip(), fmt % args))

    def _client_ip(self):
        ip = self.client_address[0]
        if TRUST_CF_CONNECTING_IP and ip in ("127.0.0.1", "::1"):
            fwd = self.headers.get("CF-Connecting-IP") if self.headers else None
            if fwd and len(fwd) <= 64 and "," not in fwd:
                try:
                    return str(ipaddress.ip_address(fwd.strip()))
                except ValueError:
                    pass
        return ip

    def _client_id(self):
        # 偽装可能なリクエスト値ではなく、接続元を管理者トークンで不可逆化して識別する。
        peer = self._client_ip().encode("utf-8", "replace")
        key = (ADMIN_TOKEN or TOKEN or "").encode("utf-8", "replace")
        digest = hmac.new(key, peer, hashlib.sha256).hexdigest()[:32]
        return "net-" + digest

    def _cors(self):
        origin = self.headers.get("Origin", "") if self.headers else ""
        self.send_header("Vary", "Origin")
        if origin and CORS_ORIGINS:
            try:
                normalized = normalize_origin(origin)
            except ValueError:
                normalized = ""
            if normalized in CORS_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", normalized)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Helper-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")

    def _send(self, code, obj, close=False, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def _send_html(self, fpath):
        p = Path(fpath)
        if not p.is_file():
            raise ApiError(404, "ページが見つかりません (%s がありません)" % p.name)
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _token(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return self.headers.get("X-Helper-Token", "").strip()

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Content-Length が不正です")
        if n < 0:
            raise ApiError(400, "Content-Length が不正です")
        if n > MAX_BODY:
            self.close_connection = True
            raise ApiError(413, "リクエストが大きすぎます")
        if n == 0:
            return {}
        raw = self.rfile.read(n)
        if len(raw) != n:
            raise ApiError(400, "リクエストが途中で切れました")
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError(400, "JSON が不正です")
        if not isinstance(body, dict):
            raise ApiError(400, "JSON オブジェクトを指定してください")
        return body

    def _need(self, role, admin):
        if role is None:
            raise ApiError(401, "トークンが正しくありません")
        if admin and role != "admin":
            raise ApiError(403, "この操作には管理者トークンが必要です")

    def _need_job_access(self, job, role):
        if job["admin"]:
            if role != "admin":
                raise ApiError(403, "この操作には管理者トークンが必要です")
            return
        if role == "admin":
            return
        # Client-supplied IDs are forgeable; use the server-derived network identity.
        if job["client"] != self._client_id():
            raise ApiError(403, "このジョブを操作する権限がありません")

    def _github_webhook(self):
        global WEBHOOK_LAST
        if not GITHUB_WEBHOOK_SECRET or not GITHUB_WEBHOOK_REPO:
            raise ApiError(404, "webhook は無効です")
        event = self.headers.get("X-GitHub-Event", "")
        signature = self.headers.get("X-Hub-Signature-256", "")
        if event != "push":
            return self._send(204, {}, close=True)
        if not signature.startswith("sha256="):
            raise ApiError(401, "webhook 署名がありません")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Content-Length が不正です")
        if n <= 0 or n > WEBHOOK_MAX_BODY:
            raise ApiError(413, "webhook のサイズが不正です")
        raw = self.rfile.read(n)
        if len(raw) != n:
            raise ApiError(400, "webhook が途中で切れました")
        expected = "sha256=" + hmac.new(GITHUB_WEBHOOK_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ApiError(401, "webhook 署名が正しくありません")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "webhook JSON が不正です")
        repo = payload.get("repository") or {}
        full_name = repo.get("full_name")
        ref = payload.get("ref")
        expected_ref = "refs/heads/" + GITHUB_WEBHOOK_BRANCH
        if full_name != GITHUB_WEBHOOK_REPO or ref != expected_ref:
            return self._send(202, {"ok": True, "updated": False, "ignored": True}, close=True)
        with WEBHOOK_LOCK:
            now = time.time()
            if now - WEBHOOK_LAST < WEBHOOK_COOLDOWN:
                return self._send(202, {"ok": True, "updated": False, "cooldown": True}, close=True)
            try:
                result = subprocess.run(
                    ["sudo", "-n", "/usr/bin/systemctl", "--no-block", "start",
                     "arduino-helper-update.service"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                self.log_message("GitHub webhook update trigger failed: %s", e)
                raise ApiError(503, "自動更新を開始できません")
            if result.returncode != 0:
                detail = (result.stderr or "").strip() or "systemctl が失敗しました"
                self.log_message("GitHub webhook update trigger failed: %s", detail[:500])
                try:
                    subprocess.Popen(
                        ["sudo", "-n", GITHUB_UPDATE_COMMAND],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                        close_fds=True,
                    )
                except OSError as e:
                    self.log_message("GitHub webhook updater fallback failed: %s", e)
                    raise ApiError(503, "自動更新を開始できません")
                self.log_message("systemd 起動に失敗したため、従来の更新スクリプトを直接起動しました")
                trigger = "legacy"
            else:
                trigger = "systemd"
            WEBHOOK_LAST = now
        return self._send(202, {"ok": True, "updated": False, "scheduled": True,
                                "trigger": trigger, "ref": ref}, close=True)

    def _dispatch(self, method):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        ip = self._client_ip()

        if method == "POST" and path == "/webhook/github":
            return self._github_webhook()

        if method == "GET" and path in ("/", "/index.html"):
            www = Path(__file__).resolve().parent / "www" / "index.html"
            if www.exists():
                body = www.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(302)
                self.send_header("Location", "https://claude.ai")
                self.send_header("Content-Length", "0")
                self.end_headers()
            return

        if method == "GET" and path in ("/admin", "/admin.html"):
            return self._send_html(Path(__file__).resolve().parent / "www" / "admin.html")

        if method == "GET" and path in ("/docs", "/docs.html"):
            return self._send_html(Path(__file__).resolve().parent / "www" / "docs.html")

        if method == "GET" and path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if method == "GET" and path == "/public-config":
            settings = load_admin_settings()
            config = {"prefillStudentToken": settings["prefillStudentToken"]}
            if settings["prefillStudentToken"]:
                config["studentToken"] = TOKEN
            return self._send(200, config, extra_headers={"Cache-Control": "no-store"})

        if method == "GET" and path == "/ping":
            role = role_for(self._token())
            d = {"ok": True, "version": "arduino-helper %s" % VERSION, "cliFound": cli_available(),
                 "authRequired": True, "authed": role is not None, "admin": role == "admin", "gen": GEN}
            if role is not None:
                with JOBS_LOCK:
                    d["queued"] = len(QUEUE)
                    d["running"] = sum(1 for j in JOBS.values() if j["kind"] == "compile" and j["state"] == "running")
                d["cli"] = cli_version() if cli_available() else ""
                d["maxQueue"] = MAX_QUEUE
            return self._send(200, d)

        if path in ("/account", "/account/") or path.startswith("/account/") or path.startswith("/teacher"):
            return self._account_dispatch(method, path, q, ip)

        wait = auth_blocked(ip)
        if wait:
            return self._send(429, {"success": False, "error": "認証の失敗が続いたため、しばらく受け付けません (%d 秒)" % wait,
                                    "retryAfter": wait}, close=True, extra_headers={"Retry-After": str(wait)})
        role = role_for(self._token())
        if role is None:
            auth_failed(ip)
            return self._send(401, {"success": False, "error": "トークンが正しくありません"}, close=True)
        if method == "GET" and path.startswith("/admin/api"):
            # 管理画面 API。arduino-cli が無くても状態を確認できるよう、CLI 判定より先に処理する。
            self._need(role, True)
            if path.startswith("/admin/api/teachers"):
                raise ApiError(404, "先生アカウント機能は提供していません")
            if path.startswith("/admin/api/accounts"):
                return self._dispatch_remainder(method, path, q, role)
            if path == "/admin/api/overview":
                return self._send(200, admin_overview())
            if path == "/admin/api/history":
                return self._send(200, admin_history(q))
            if path == "/admin/api/settings":
                return self._send(200, {
                    "ok": True,
                    "studentToken": TOKEN,
                    "adminToken": ADMIN_TOKEN,
                    "prefillStudentToken": load_admin_settings()["prefillStudentToken"],
                })
            raise ApiError(404, "不明なエンドポイント: %s %s" % (method, path[:80]))

        return self._dispatch_remainder(method, path, q, role)

    def _account_dispatch(self, method, path, q, ip):
        if path in ("/account", "/account/") and method == "GET":
            return self._send_html(Path(__file__).resolve().parent / "www" / "index.html")
        if path.startswith("/teacher"):
            raise ApiError(404, "先生・クラス機能は提供していません")

        with ACCOUNT_LOCK, account_db() as conn:
            if method == "POST" and path == "/account/register":
                wait = _account_signup_allowed(ip)
                if wait:
                    raise ApiError(429, "アカウント作成の回数が上限に達しました。時間をおいて再試行してください",
                                   {"retryAfter": wait})
                body = self._body()
                username = body.get("username")
                display_name = body.get("name", "")
                password = body.get("password")
                if not isinstance(username, str) or not re.fullmatch(
                        r"[A-Za-z0-9_.@-]{3,64}", username.strip()):
                    raise ApiError(400, "ユーザー名は英数字と . _ @ - の3〜64文字で指定してください")
                username = username.strip()
                if not isinstance(display_name, str) or len(display_name.strip()) > 80 \
                        or any(ord(char) < 32 for char in display_name):
                    raise ApiError(400, "表示名は80文字以内で入力してください")
                # パスワードチェックを重複確認より先に行い、レスポンスタイムによるユーザー名列挙を防ぐ
                salt, digest = _account_password(password)
                if conn.execute("SELECT 1 FROM account_users WHERE username=? COLLATE NOCASE",
                                (username,)).fetchone():
                    raise ApiError(409, "このユーザー名はすでに使われています")
                user_id = secrets.token_hex(16)
                now = int(time.time())
                conn.execute(
                    "INSERT INTO account_users(id,username,display_name,password_hash,password_salt,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (user_id, username, display_name.strip() or username, digest, salt, now),
                )
                _account_audit(conn, user_id, "account.register", user_id)
                token = secrets.token_urlsafe(32)
                conn.execute(
                    "INSERT INTO account_sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                    (hashlib.sha256(token.encode("ascii")).hexdigest(), user_id, now + 12 * 3600, now),
                )
                user = conn.execute("SELECT * FROM account_users WHERE id=?", (user_id,)).fetchone()
                return self._send(201, {"ok": True, "token": token, "user": _account_user(user)})

            if method == "POST" and path == "/account/login":
                wait = auth_blocked(ip)
                if wait:
                    raise ApiError(429, "認証の失敗が続いたため、しばらく受け付けません (%d 秒)" % wait,
                                   {"retryAfter": wait})
                body = self._body()
                username = body.get("username")
                password = body.get("password")
                if not isinstance(username, str) or not isinstance(password, str):
                    raise ApiError(400, "ユーザー名とパスワードを入力してください")
                row = conn.execute(
                    "SELECT * FROM account_users WHERE username=? COLLATE NOCASE",
                    (username.strip(),),
                ).fetchone()
                if not row or not row["active"]:
                    auth_failed(ip)
                    raise ApiError(401, "ユーザー名またはパスワードが正しくありません")
                if not _account_password_matches(password, row["password_salt"], row["password_hash"]):
                    auth_failed(ip)
                    raise ApiError(401, "ユーザー名またはパスワードが正しくありません")
                token = secrets.token_urlsafe(32)
                now = int(time.time())
                _clean_expired_account_sessions(conn, now)
                conn.execute(
                    "INSERT INTO account_sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                    (hashlib.sha256(token.encode("ascii")).hexdigest(), row["id"], now + 12 * 3600, now),
                )
                _trim_account_sessions(conn, row["id"])
                auth_succeeded(ip)
                return self._send(200, {"ok": True, "token": token, "user": _account_user(row)})

            if method == "POST" and path == "/account/logout":
                header = self.headers.get("Authorization", "")
                token = header[8:].strip() if header.startswith("Session ") else ""
                if token:
                    conn.execute("DELETE FROM account_sessions WHERE token_hash=?",
                                 (hashlib.sha256(token.encode("ascii", "ignore")).hexdigest(),))
                return self._send(200, {"ok": True})

            user = _session_user(self, conn)
            if method == "GET" and path == "/account/me":
                return self._send(200, {"ok": True, "user": _account_user(user)})
            if method == "POST" and path == "/account/password":
                wait = auth_blocked(ip)
                if wait:
                    raise ApiError(429, "認証の失敗が続いたため、しばらく受け付けません (%d 秒)" % wait,
                                   {"retryAfter": wait})
                body = self._body()
                old = body.get("currentPassword")
                new = body.get("newPassword")
                if not isinstance(old, str):
                    auth_failed(ip)
                    raise ApiError(403, "現在のパスワードが正しくありません")
                if not _account_password_matches(old, user["password_salt"], user["password_hash"]):
                    auth_failed(ip)
                    raise ApiError(403, "現在のパスワードが正しくありません")
                salt, digest = _account_password(new)
                conn.execute("UPDATE account_users SET password_hash=?, password_salt=? WHERE id=?",
                             (digest, salt, user["id"]))
                conn.execute("DELETE FROM account_sessions WHERE user_id=?", (user["id"],))
                _account_audit(conn, user["id"], "account.password_change", user["id"])
                auth_succeeded(ip)
                return self._send(200, {"ok": True, "reauthenticate": True})

            if method == "GET" and path == "/account/sketches":
                rows = conn.execute("SELECT * FROM account_sketches WHERE user_id=? ORDER BY updated_at DESC",
                                    (user["id"],)).fetchall()
                return self._send(200, {"sketches": [_account_sketch_view(row) for row in rows]})

            match = re.fullmatch(r"/account/sketches/([A-Za-z0-9_-]{1,80})", path)
            if method == "PUT" and match:
                body = self._body()
                sketch = _valid_sketch(body.get("sketch"))
                if sketch["id"] != match.group(1):
                    raise ApiError(400, "スケッチIDが一致しません")
                expected = body.get("revision", 0)
                if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
                    raise ApiError(400, "revision が不正です")
                row = conn.execute("SELECT revision FROM account_sketches WHERE user_id=? AND id=?",
                                   (user["id"], sketch["id"])).fetchone()
                current = row["revision"] if row else 0
                if current != expected:
                    raise ApiError(409, "別の端末でスケッチが更新されています。同期し直してください")
                revision = current + 1
                now = int(time.time())
                if row:
                    previous = conn.execute(
                        "SELECT data FROM account_sketches WHERE id=? AND user_id=?",
                        (sketch["id"], user["id"]),
                    ).fetchone()
                    conn.execute(
                        "INSERT INTO account_sketch_versions(sketch_id,user_id,revision,editor_id,data,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (sketch["id"], user["id"], current, user["id"], previous["data"], now),
                    )
                    _trim_account_versions(conn, user["id"], sketch["id"])
                conn.execute(
                    "INSERT INTO account_sketches(id,user_id,data,revision,updated_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(id,user_id) DO UPDATE SET data=excluded.data,revision=excluded.revision,"
                    "updated_at=excluded.updated_at",
                    (sketch["id"], user["id"], json.dumps(sketch, ensure_ascii=False), revision, now),
                )
                return self._send(200, {"ok": True, "revision": revision})
            if method == "DELETE" and match:
                row = conn.execute("SELECT revision FROM account_sketches WHERE user_id=? AND id=?",
                                   (user["id"], match.group(1))).fetchone()
                if not row:
                    return self._send(200, {"ok": True, "deleted": False})
                expected = q.get("revision")
                if expected is None or not expected.isdigit() or int(expected) != row["revision"]:
                    raise ApiError(409, "別の端末でスケッチが更新されています。削除を中止しました")
                conn.execute("DELETE FROM account_sketch_versions WHERE user_id=? AND sketch_id=?",
                             (user["id"], match.group(1)))
                conn.execute("DELETE FROM account_sketches WHERE user_id=? AND id=?",
                             (user["id"], match.group(1)))
                return self._send(200, {"ok": True, "deleted": True})
            raise ApiError(404, "不明なアカウントAPI: %s %s" % (method, path[:80]))

    def _dispatch_remainder(self, method, path, q, role):
        if path.startswith("/admin/api/teachers"):
            raise ApiError(404, "先生アカウント機能は提供していません")

        # ── 管理者用アカウント管理API ──────────────────────────────────────
        if path.startswith("/admin/api/accounts"):
            self._need(role, True)
            with ACCOUNT_LOCK, account_db() as conn:
                # GET /admin/api/accounts  → ユーザー一覧（スケッチ数付き）
                if method == "GET" and path == "/admin/api/accounts":
                    rows = conn.execute(
                        "SELECT u.id, u.username, u.display_name, u.active, u.created_at, "
                        "COUNT(s.id) AS sketch_count "
                        "FROM account_users u "
                        "LEFT JOIN account_sketches s ON s.user_id = u.id "
                        "GROUP BY u.id ORDER BY u.created_at DESC"
                    ).fetchall()
                    users = []
                    for r in rows:
                        users.append({
                            "id": r["id"],
                            "username": r["username"],
                            "name": r["display_name"],
                            "active": bool(r["active"]),
                            "createdAt": r["created_at"],
                            "sketchCount": r["sketch_count"],
                        })
                    return self._send(200, {"ok": True, "users": users})

                # GET /admin/api/accounts/<uid>/sketches  → ユーザーのスケッチ一覧
                m = re.fullmatch(r"/admin/api/accounts/([A-Za-z0-9_-]{1,80})/sketches", path)
                if method == "GET" and m:
                    uid = m.group(1)
                    rows = conn.execute(
                        "SELECT * FROM account_sketches WHERE user_id=? ORDER BY updated_at DESC", (uid,)
                    ).fetchall()
                    return self._send(200, {"ok": True, "sketches": [_account_sketch_view(r) for r in rows]})

                # GET /admin/api/accounts/<uid>/sketches/<sid>  → スケッチの中身
                m2 = re.fullmatch(r"/admin/api/accounts/([A-Za-z0-9_-]{1,80})/sketches/([A-Za-z0-9_-]{1,80})", path)
                if method == "GET" and m2:
                    uid, sid = m2.group(1), m2.group(2)
                    row = conn.execute(
                        "SELECT * FROM account_sketches WHERE user_id=? AND id=?", (uid, sid)
                    ).fetchone()
                    if not row:
                        raise ApiError(404, "スケッチが見つかりません")
                    return self._send(200, {"ok": True, "sketch": _account_sketch_view(row)})

                # DELETE /admin/api/accounts/<uid>  → ユーザー削除（スケッチも含む）
                m3 = re.fullmatch(r"/admin/api/accounts/([A-Za-z0-9_-]{1,80})", path)
                if method == "DELETE" and m3:
                    uid = m3.group(1)
                    row = conn.execute("SELECT id FROM account_users WHERE id=?", (uid,)).fetchone()
                    if not row:
                        raise ApiError(404, "ユーザーが見つかりません")
                    conn.execute("DELETE FROM account_sketch_versions WHERE user_id=?", (uid,))
                    conn.execute("DELETE FROM account_sketches WHERE user_id=?", (uid,))
                    conn.execute("DELETE FROM account_sessions WHERE user_id=?", (uid,))
                    conn.execute("DELETE FROM account_users WHERE id=?", (uid,))
                    return self._send(200, {"ok": True, "deleted": uid})

                # POST /admin/api/accounts/<uid>/toggle  → アカウント有効/無効切替
                m4 = re.fullmatch(r"/admin/api/accounts/([A-Za-z0-9_-]{1,80})/toggle", path)
                if method == "POST" and m4:
                    uid = m4.group(1)
                    row = conn.execute("SELECT active FROM account_users WHERE id=?", (uid,)).fetchone()
                    if not row:
                        raise ApiError(404, "ユーザーが見つかりません")
                    new_active = 0 if row["active"] else 1
                    conn.execute("UPDATE account_users SET active=? WHERE id=?", (new_active, uid))
                    if not new_active:
                        conn.execute("DELETE FROM account_sessions WHERE user_id=?", (uid,))
                    return self._send(200, {"ok": True, "active": bool(new_active)})

                raise ApiError(404, "不明な管理APIエンドポイント: %s %s" % (method, path[:80]))
        # ── end 管理者用アカウント管理API ──────────────────────────────────

        if method == "DELETE" and path == "/admin/api/history":
            self._need(role, True)
            deleted = clear_history()
            return self._send(200, {"ok": True, "deleted": deleted})
        if method == "POST" and path == "/admin/api/settings":
            self._need(role, True)
            body = self._body()
            if not isinstance(body, dict) or not isinstance(body.get("prefillStudentToken"), bool):
                raise ApiError(400, "prefillStudentToken は true または false を指定してください")
            settings = {"prefillStudentToken": body["prefillStudentToken"]}
            save_admin_settings(settings)
            return self._send(200, {"ok": True, **settings})
        if not cli_available():
            return self._send(500, {"success": False, "error": "arduino-cli を実行できません (arduino-build-run が見つかりません)"})

        if method == "GET":
            if path == "/boards":
                data = read_cached("boards", 60, lambda: {"boards": norm_boards(run_cli_json(["board", "listall"]))})
                return self._send(200, data)
            if path == "/cores/search":
                qq = q.get("q", "")[:100]
                # 2文字未満または特殊クエリは空リストを返す (arduino-cli がエラーを返す場合がある)
                if len(qq) < 2:
                    return self._send(200, {"cores": []})
                def _cores_search():
                    data = validate_cli_collection(run_cli_json(["core", "search", qq]),
                                                   "platforms", "core search")
                    return {"cores": norm_cores(data)[:200]}
                data = read_cached("cs:" + qq, 120, _cores_search)
                return self._send(200, data)
            if path == "/cores/list":
                def _cores_list():
                    data = validate_cli_collection(run_cli_json(["core", "list"]),
                                                   "platforms", "core list")
                    return {"cores": norm_cores(data)}
                data = read_cached("cl", 20, _cores_list)
                return self._send(200, data)
            if path == "/libs/search":
                qq = q.get("q", "")[:100]
                # 2文字未満または特殊クエリは空リストを返す (arduino-cli がエラーを返す場合がある)
                if len(qq) < 2:
                    return self._send(200, {"libs": []})
                def _libs_search():
                    # --omit-releases-details: 最新バージョン以外の詳細を省略 → 出力を大幅削減
                    # 未対応CLIの場合は run_cli_json がオプションエラーを限定的に検出して再試行する。
                    data = validate_cli_collection(
                        run_cli_json(["lib", "search", qq, "--omit-releases-details"]),
                        "libraries", "lib search")
                    libs = norm_libs_search(data)
                    return {"libs": sort_libs_by_relevance(libs, qq)[:200]}
                with LIB_SEARCH_LOCK:
                    cache_key = _library_search_cache_key(qq)
                    data = read_cached(cache_key, 120, _libs_search)
                return self._send(200, data)
            if path == "/libs/list":
                def _libs_list():
                    data = validate_cli_collection(run_cli_json(["lib", "list"]),
                                                   "installed_libraries", "lib list")
                    return {"libs": norm_libs_list(data)}
                data = read_cached("ll", 20, _libs_list)
                return self._send(200, data)
            m = re.match(r"^/jobs/([0-9a-f]{24})$", path)
            if m:
                return self._send(200, self._job_view(m.group(1), q))
        elif method == "POST":
            m = re.match(r"^/jobs/([0-9a-f]{24})/cancel$", path)
            if m:
                with JOBS_LOCK:
                    job = JOBS.get(m.group(1))
                if not job:
                    raise ApiError(404, "ジョブが見つかりません")
                self._need_job_access(job, role)
                return self._send(200, {"ok": cancel_job(job)})
            admin_paths = ("/cores/install", "/cores/uninstall", "/libs/install", "/libs/uninstall",
                           "/libs/install-zip", "/update")
            if path == "/compile":
                body = self._body()
                job, pos = submit_compile(body, self._client_id())
                with JOBS_LOCK:
                    queued = len(QUEUE)
                return self._send(202, {"job": job["id"], "position": pos, "queued": queued,
                                        "cached": bool(job["result"] and job["result"].get("cached"))})
            if path in admin_paths:
                self._need(role, True)
                body = self._body()
                cid = self._client_id()
                if path == "/cores/install":
                    cid_ = check_name(body.get("id"), RE_CORE, "パッケージID")
                    return self._send(200, {"job": start_admin_job("install " + cid_, ["core", "install", cid_], client=cid)})
                if path == "/cores/uninstall":
                    cid_ = check_name(body.get("id"), RE_CORE, "パッケージID")
                    return self._send(200, {"job": start_admin_job("uninstall " + cid_, ["core", "uninstall", cid_], client=cid)})
                if path == "/libs/install":
                    name = check_name(body.get("name"), RE_LIBNAME, "ライブラリ名")
                    ver = body.get("version")
                    arg = name
                    if ver:
                        arg += "@" + check_name(ver, RE_VERSION, "バージョン")
                    return self._send(200, {"job": start_admin_job("install " + arg, ["lib", "install", arg], client=cid)})
                if path == "/libs/uninstall":
                    name = check_name(body.get("name"), RE_LIBNAME, "ライブラリ名")
                    return self._send(200, {"job": start_admin_job("uninstall " + name, ["lib", "uninstall", name], client=cid)})
                if path == "/libs/install-zip":
                    return self._send(200, {"job": self._install_zip(body, cid)})
                if path == "/update":
                    return self._send(200, {"job": start_admin_job("update", ["update"], client=cid)})
        raise ApiError(404, "不明なエンドポイント: %s %s" % (method, path[:80]))

    def _install_zip(self, body, cid):
        data = body.get("data")
        if not isinstance(data, str) or len(data) > MAX_ZIP_BYTES * 4 // 3 + 16:
            raise ApiError(400, "data が不正です (8MB まで)")
        # base64 は緩く無視せず、入力そのものを厳密に検証する。
        try:
            raw = base64.b64decode(data, validate=True)
        except Exception:
            raise ApiError(400, "base64 が不正です")
        if len(raw) > MAX_ZIP_BYTES or not raw.startswith(b"PK"):
            raise ApiError(400, "ZIP ファイルではありません")
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                infos = zf.infolist()
                if len(infos) > 5000:
                    raise ApiError(400, "ZIP のファイル数が多すぎます")
                total_uncompressed = 0
                seen_zip = set()
                for i in infos:
                    n = i.filename.replace("\\", "/")
                    # 重複エントリは展開時の上書き順に依存するため拒否。
                    key = n.casefold()
                    if key in seen_zip:
                        raise ApiError(400, "ZIP に同名ファイルが複数あります: %s" % n[:60])
                    seen_zip.add(key)
                    if (not n or n.startswith("/") or re.match(r"^[A-Za-z]:", n)
                            or any(part in ("", ".", "..") for part in n.split("/"))
                            or "\x00" in n):
                        raise ApiError(400, "ZIP に使用できないパスが含まれています: %s" % n[:60])
                    mode = (i.external_attr >> 16) & 0o170000
                    if mode in (0o120000, 0o010000):
                        raise ApiError(400, "ZIP にシンボリックリンク等の特殊ファイルが含まれています: %s" % n[:60])
                    if i.file_size < 0 or i.file_size > 16 * 1024 * 1024:
                        raise ApiError(400, "ZIP 内の単一ファイルが大きすぎます: %s" % n[:60])
                    total_uncompressed += i.file_size
                    if total_uncompressed > 64 * 1024 * 1024:
                        raise ApiError(400, "ZIP の展開後サイズが大きすぎます")
                    if i.compress_size == 0 and i.file_size > 0:
                        raise ApiError(400, "ZIP の圧縮情報が不正です: %s" % n[:60])
                    if i.file_size > 1024 * 1024 and i.compress_size and i.file_size / i.compress_size > 100:
                        raise ApiError(400, "ZIP の圧縮率が高すぎます: %s" % n[:60])
        except zipfile.BadZipFile:
            raise ApiError(400, "壊れた ZIP ファイルです")
        zdir = WORK_DIR / ("zip_" + secrets.token_hex(8))
        zdir.mkdir(parents=True)
        os.chmod(str(zdir), 0o2770)
        zp = zdir / "lib.zip"
        zp.write_bytes(raw)
        os.chmod(str(zp), 0o660)
        return start_admin_job("install zip", ["lib", "install", "--zip-path", str(zp)],
                               cleanup=lambda: rmtree(zdir), client=cid)

    def _job_view(self, jid, q):
        with JOBS_LOCK:
            job = JOBS.get(jid)
            if not job:
                raise ApiError(404, "ジョブが見つかりません (サーバーが再起動した可能性があります)")
            try:
                start = max(0, min(MAX_JOB_LINES, int(q.get("from", "0") or 0)))
            except ValueError:
                raise ApiError(400, "from が不正です")
            self._need_job_access(job, role_for(self._token()))
            d = {"id": job["id"], "kind": job["kind"], "state": job["state"], "rc": job["rc"],
                 "lines": job["lines"][start:], "next": len(job["lines"]), "error": job["error"],
                 "elapsed": round(time.time() - job["created"], 1)}
            if job["state"] == "queued" and job["id"] in QUEUE:
                d["position"] = QUEUE.index(job["id"]) + 1
                d["queued"] = len(QUEUE)
                d["blocked"] = "admin" if CLI_LOCK.locked() else ""
            if job["state"] in ("done", "error") and job["result"] is not None:
                d["result"] = job["result"]
            return d

    def _run(self, method):
        try:
            self._dispatch(method)
        except ApiError as e:
            body = {"success": False, "error": e.msg}
            body.update(e.extra)
            hdr = {"Retry-After": str(e.extra["retryAfter"])} if "retryAfter" in e.extra else None
            try:
                self._send(e.code, body, close=e.code in (400, 401, 403, 413), extra_headers=hdr)
            except Exception:
                pass
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception:  # noqa
            eid = secrets.token_hex(3)
            sys.stderr.write("内部エラー [%s]\n%s\n" % (eid, traceback.format_exc()))
            try:
                self._send(500, {"success": False, "error": "サーバー内部でエラーが発生しました (ID %s)" % eid}, close=True)
            except Exception:
                pass

    def do_GET(self):
        self._run("GET")

    def do_POST(self):
        self._run("POST")

    def do_PUT(self):
        self._run("PUT")

    def do_DELETE(self):
        self._run("DELETE")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128
    max_threads = 150
    _active = 0
    _lock = threading.Lock()

    def process_request(self, request, client_address):
        with self._lock:
            if self._active >= self.max_threads:
                try:
                    request.close()
                except Exception:
                    pass
                return
            self._active += 1
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._lock:
                self._active -= 1


# ---------------------------------------------------------------
# トークン
# ---------------------------------------------------------------
def write_secret(path, value):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")
    os.chmod(str(path), 0o600)


def load_secret(env_name, fname):
    t = os.environ.get(env_name)
    if t:
        if len(t) < 16:
            sys.exit("%s が短すぎます (16文字以上)" % env_name)
        return t
    f = HOME / fname
    if f.exists():
        v = f.read_text().strip()
        if len(v) >= 16:
            return v
    v = secrets.token_urlsafe(24)
    write_secret(f, v)
    return v


def cmd_show_tokens():
    HOME.mkdir(parents=True, exist_ok=True)
    print("IDE 用トークン  : %s" % load_secret("HELPER_TOKEN", "token"))
    print("管理者トークン  : %s" % load_secret("HELPER_ADMIN_TOKEN", "admin-token"))


def cmd_rotate(which):
    HOME.mkdir(parents=True, exist_ok=True)
    if which in ("user", "both"):
        write_secret(HOME / "token", secrets.token_urlsafe(24))
    if which in ("admin", "both"):
        write_secret(HOME / "admin-token", secrets.token_urlsafe(24))
    print("再発行しました。サーバーを再起動してください: sudo systemctl restart arduino-helper")
    cmd_show_tokens()


def cmd_share_link(url, fallback=None):
    tok = load_secret("HELPER_TOKEN", "token")
    cfg = {"u": url.rstrip("/"), "t": tok}
    if fallback:
        cfg["f"] = fallback.rstrip("/")
    b = base64.urlsafe_b64encode(json.dumps(cfg, separators=(",", ":")).encode()).decode().rstrip("=")
    print("IDE の URL の末尾にこの文字列を付けて配布してください (IDE 用トークンのみ含まれます):")
    print("#cfg=" + b)


def main():
    global CLI, TOKEN, ADMIN_TOKEN, WRAPPER
    args = sys.argv[1:]
    if args and args[0] == "--show-tokens":
        return cmd_show_tokens()
    if args and args[0] == "--rotate":
        return cmd_rotate(args[1] if len(args) > 1 else "both")
    if args and args[0] == "--share-link" and len(args) > 1:
        return cmd_share_link(args[1], args[2] if len(args) > 2 else None)
    os.umask(0o007)
    HOME.mkdir(parents=True, exist_ok=True)
    for d in (WORK_DIR,):
        d.mkdir(exist_ok=True)
        try:
            os.chmod(str(d), 0o2770)
        except OSError:
            pass
    # 前回の残骸を掃除
    for p in list(WORK_DIR.iterdir()):
        try:
            rmtree(p) if p.is_dir() else p.unlink()
        except OSError:
            pass
    WRAPPER = find_wrapper()
    TOKEN = load_secret("HELPER_TOKEN", "token")
    if SINGLE_TOKEN:
        ADMIN_TOKEN = TOKEN
    else:
        ADMIN_TOKEN = load_secret("HELPER_ADMIN_TOKEN", "admin-token")
        if ADMIN_TOKEN == TOKEN:
            sys.exit("IDE用と管理者用のトークンが同じです。--rotate admin で再発行してください")
    load_gen()
    load_history()
    log("Arduino Web IDE helper %s" % VERSION)
    log("  実行ラッパー : %s" % (WRAPPER or "見つかりません"))
    log("  コンパイル   : %s" % (("sudo -u %s" % BUILD_USER) if BUILD_USER else "このユーザーで直接実行 (権限分離なし)"))
    log("  待受         : http://%s:%d" % (HOST, PORT))
    log("  トークン     : %s (表示するには --show-tokens)" % HOME)
    if not BUILD_USER:
        log("  注意: 権限分離なし。複数人で使う場合は install.sh の構成にしてください")
    threading.Thread(target=compile_worker, daemon=True).start()
    threading.Thread(target=housekeeping, daemon=True).start()
    srv = Server((HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("終了します")


if __name__ == "__main__":
    main()
