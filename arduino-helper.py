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
  - トークンは2種類です。生徒用 (コンパイルと閲覧のみ) / 管理者用 (ボード・ライブラリの変更)。
  - 同じ内容 (ソース+ボード+サーバーの状態) のコンパイル結果はサーバー側でも使い回します。

■ 環境変数
  HELPER_HOME            データ置き場 (既定 ~/.arduino-helper)
  HELPER_HOST / PORT     待受 (既定 127.0.0.1:8765)
  HELPER_TOKEN           生徒用トークン (未指定なら HELPER_HOME/token に自動生成)
  HELPER_ADMIN_TOKEN     管理者用トークン (未指定なら HELPER_HOME/admin-token に自動生成)
  HELPER_SINGLE_TOKEN=1  1人で使う場合。生徒用トークンで管理操作もできる
  HELPER_BUILD_USER      指定すると sudo でそのユーザーとして arduino-cli を実行
  HELPER_BUILD_WRAPPER   ラッパーのパス (既定 /usr/local/libexec/arduino-build-run)
  HELPER_MAX_QUEUE       待ち行列の上限 (既定 40)
  HELPER_PER_CLIENT      1端末が同時に持てるコンパイル数 (既定 2)
  HELPER_COMPILE_TIMEOUT コンパイルの上限秒 (既定 900)
  HELPER_MIN_FREE_MB     空きがこれ未満ならコンパイルを断る (既定 1024)
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
  GET  /admin/api/settings        アクセストークン・生徒用トークン配布設定 (JSON)
  POST /admin/api/settings        生徒用トークン配布設定を保存
  GET  /public-config             生徒用トークン自動入力設定 (有効時のみトークンを返す)
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
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
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

VERSION = "2.7.0"


def env_int(name, default, lo=0, hi=10 ** 9):
    try:
        v = int(os.environ.get(name, default))
    except ValueError:
        v = default
    return max(lo, min(hi, v))


HOME = Path(os.environ.get("HELPER_HOME") or (Path.home() / ".arduino-helper"))
ADMIN_SETTINGS_FILE = HOME / "admin-settings.json"
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
MAX_JOB_LINES = 4000
MAX_OUTPUT_CHARS = 300000        # コンパイルログ等テキスト出力の上限
MAX_JSON_OUTPUT_CHARS = 20000000  # JSON取得コマンド (lib search 等) の上限 (~20MB)
JOB_KEEP_SEC = 600
RESULT_CACHE_ENTRIES = 32
RESULT_CACHE_BYTES = 96 * 1024 * 1024
HISTORY_FILE = HOME / "history.jsonl"   # 利用履歴 (JSON 1行1件)
HISTORY_KEEP = 1000                     # メモリに保持する履歴件数
HISTORY_TRIM = 4000                     # これを超えたら履歴ファイルを書き直す

ALLOWED_EXT = {".ino", ".h", ".hpp", ".cpp", ".c"} | ({".s", ".S"} if ALLOW_ASM else set())
ARTIFACT_EXT = {".hex", ".bin", ".uf2"}

RE_FQBN = re.compile(r"^[A-Za-z0-9_.\-]+:[A-Za-z0-9_.\-]+:[A-Za-z0-9_.\-]+(:[A-Za-z0-9_.,=\-]+)?$")
RE_CORE = re.compile(r"^[A-Za-z0-9_.\-]+:[A-Za-z0-9_.\-]+(@[0-9A-Za-z.\-+]+)?$")
RE_LIBNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.+()&'\-]{0,98}$")
RE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.\-+]{0,40}$")
RE_FILE = re.compile(r"^[^/\\\x00-\x1f.][^/\\\x00-\x1f]{0,99}$")
RE_CLIENT = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")
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
        try:
            p.kill()
        except Exception:
            pass
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
    # arduino-cli v1.5+ は lib/core search で結果があっても rc=1 を返すことがある。
    # JSON がパースできれば rc は無視する。rc=127 (コマンド未発見) だけは即エラー。
    if rc == 127:
        log("arduino-cli エラー %s: %s" % (args, out[-500:]))
        raise ApiError(500, "arduino-cli の実行に失敗しました。サーバーのログを確認してください")
    if rc != 0:
        log("arduino-cli rc=%d %s (JSON パースを試みます): %s" % (rc, args, out[-200:]))
    # 1) 全体をそのままパース
    try:
        return json.loads(out)
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
        return objects[0]
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
            return merged
        return objects
    # 3) 先頭の { または [ から末尾まで切り出してパース
    for i, ch in enumerate(out):
        if ch in "{[":
            try:
                return json.loads(out[i:])
            except ValueError:
                continue
    log("arduino-cli JSON 解析失敗 (args=%s) 出力先頭: %r" % (args, out[:300]))
    raise ApiError(500, "arduino-cli の出力を解析できません")


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
        for k in [k for k, v in JOBS.items()
                  if (v["state"] in ("done", "error", "cancelled") and now - v["t"] > JOB_KEEP_SEC)
                  or (v["state"] == "queued" and now - v["created"] > 3 * 3600)]:
            JOBS.pop(k, None)
            if k in QUEUE:
                QUEUE.remove(k)
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
            lines = f.readlines()
    except OSError:
        return
    recs = []
    for line in lines[-HISTORY_KEEP:]:
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
        HISTORY_LINES = len(lines)
    if len(lines) > HISTORY_TRIM:
        trim_history()


def trim_history():
    """履歴ファイルが大きくなったら直近 HISTORY_KEEP*2 件だけ残して書き直す。"""
    global HISTORY_LINES
    with HISTORY_LOCK:
        try:
            with open(str(HISTORY_FILE), "r", encoding="utf-8", errors="replace") as f:
                lines = [l for l in f.readlines() if l.strip()][-HISTORY_KEEP * 2:]
            tmp = HISTORY_FILE.with_suffix(".tmp")
            with open(str(tmp), "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.replace(str(tmp), str(HISTORY_FILE))
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
    job = new_job("admin", client, True)
    job["title"] = title

    def worker():
        global ADMIN_WAITING
        try:
            with JOBS_LOCK:
                ADMIN_WAITING += 1
            if CLI_LOCK.locked():
                job["lines"].append("待機中...(他の処理が終わるまでお待ちください)")
            CLI_LOCK.acquire()
            with JOBS_LOCK:
                ADMIN_WAITING -= 1
            try:
                if job["cancel"]:
                    finish(job, "cancelled")
                    return
                job["state"] = "running"
                def on_line(l):
                    if len(job["lines"]) < MAX_JOB_LINES:
                        job["lines"].append(l)
                rc, _ = run_cli(args, timeout=OP_TIMEOUT, on_line=on_line, cancel_file=cancel_path(job))
            finally:
                CLI_LOCK.release()
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
            if cleanup:
                try:
                    cleanup()
                except Exception:
                    pass

    threading.Thread(target=worker, daemon=True).start()
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
    try:
        return shutil.disk_usage(str(HOME)).free // (1024 * 1024)
    except OSError:
        return 10 ** 9


def run_compile(job):
    """作業ディレクトリを作り、コンパイルし、必ず片付ける。"""
    global COMPILES_DONE
    plan = job["plan"]
    fqbn, sketch, main_new = plan["fqbn"], plan["sketch"], plan["main"]
    if free_mb() < MIN_FREE_MB:
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
        until = AUTH_BLOCK.get(ip, 0)
        if until > time.time():
            return int(until - time.time()) + 1
        AUTH_BLOCK.pop(ip, None)
    return 0


def auth_failed(ip):
    now = time.time()
    with AUTH_LOCK:
        lst = [t for t in AUTH_FAILS.get(ip, []) if now - t < 60]
        lst.append(now)
        AUTH_FAILS[ip] = lst
        if len(lst) >= 10:
            AUTH_BLOCK[ip] = now + 120
            AUTH_FAILS.pop(ip, None)
        if len(AUTH_FAILS) > 5000:
            AUTH_FAILS.clear()


def role_for(token):
    if not token:
        return None
    t = token.encode("utf-8", "replace")
    if hmac.compare_digest(t, ADMIN_TOKEN.encode()):
        return "admin"
    if hmac.compare_digest(t, TOKEN.encode()):
        return "admin" if SINGLE_TOKEN else "user"
    return None


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
    stats["compilesSinceStart"] = COMPILES_DONE
    return {
        "ok": True,
        "now": round(now, 1),
        "server": {
            "version": VERSION, "started": round(STARTED, 1), "uptime": round(now - STARTED, 1),
            "pid": os.getpid(), "python": sys.version.split()[0],
            "host": HOST, "port": PORT, "home": str(HOME),
            "wrapper": WRAPPER or "", "wrapperFound": cli_available(),
            "buildUser": BUILD_USER or "", "singleToken": SINGLE_TOKEN, "allowAsm": ALLOW_ASM,
            "cliVersion": cli_version() if cli_available() else "",
            "cliBusy": CLI_LOCK.locked(), "gen": gen,
            "freeMb": free_mb(), "rssMb": process_rss_mb(),
        },
        "limits": {
            "maxQueue": MAX_QUEUE, "perClient": PER_CLIENT, "compileTimeout": COMPILE_TIMEOUT,
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
        if ip in ("127.0.0.1", "::1"):
            fwd = self.headers.get("CF-Connecting-IP") if self.headers else None
            if fwd:
                return fwd.strip()[:64]
        return ip

    def _client_id(self):
        cid = self.headers.get("X-Client-Id", "")
        return cid if RE_CLIENT.match(cid) else "ip-" + self._client_ip()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Helper-Token, X-Client-Id")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")
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
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError(400, "JSON が不正です")

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
        # 生徒用トークンは全員共通になり得るため、ジョブIDだけ知っている別端末から
        # 他端末のジョブを操作できないよう X-Client-Id を所有者識別子として使う。
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
            WEBHOOK_LAST = now
        try:
            subprocess.Popen(
                ["sudo", "-n", GITHUB_UPDATE_COMMAND],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except Exception as e:
            raise ApiError(503, "自動更新を開始できません")
        return self._send(202, {"ok": True, "updated": True, "ref": ref}, close=True)

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

        if method == "GET" and path == "/public-config":
            settings = load_admin_settings()
            config = {"prefillStudentToken": settings["prefillStudentToken"]}
            if settings["prefillStudentToken"]:
                config["studentToken"] = TOKEN
            return self._send(200, config)

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
                    try:
                        return {"cores": norm_cores(run_cli_json(["core", "search", qq]))[:200]}
                    except ApiError:
                        return {"cores": []}
                data = read_cached("cs:" + qq, 120, _cores_search)
                return self._send(200, data)
            if path == "/cores/list":
                def _cores_list():
                    try:
                        return {"cores": norm_cores(run_cli_json(["core", "list"]))}
                    except ApiError:
                        return {"cores": []}
                return self._send(200, read_cached("cl", 20, _cores_list))
            if path == "/libs/search":
                qq = q.get("q", "")[:100]
                # 2文字未満または特殊クエリは空リストを返す (arduino-cli がエラーを返す場合がある)
                if len(qq) < 2:
                    return self._send(200, {"libs": []})
                def _libs_search():
                    try:
                        # --omit-releases-details: 最新バージョン以外の詳細を省略 → 出力を大幅削減
                        # (arduino-cli v0.x にはこのフラグがないためフォールバックする)
                        try:
                            data = run_cli_json(["lib", "search", qq, "--omit-releases-details"])
                        except ApiError:
                            data = run_cli_json(["lib", "search", qq])
                        libs = norm_libs_search(data)
                        return {"libs": sort_libs_by_relevance(libs, qq)[:200]}
                    except ApiError:
                        return {"libs": []}
                data = read_cached("ls:" + qq, 120, _libs_search)
                return self._send(200, data)
            if path == "/libs/list":
                def _libs_list():
                    try:
                        return {"libs": norm_libs_list(run_cli_json(["lib", "list"]))}
                    except ApiError:
                        return {"libs": []}
                return self._send(200, read_cached("ll", 20, _libs_list))
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
    print("生徒用トークン  : %s" % load_secret("HELPER_TOKEN", "token"))
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
    print("IDE の URL の末尾にこの文字列を付けて配布してください (生徒用トークンのみ含まれます):")
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
            sys.exit("生徒用と管理者用のトークンが同じです。--rotate admin で再発行してください")
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
