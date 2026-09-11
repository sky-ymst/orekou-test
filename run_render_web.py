# -*- coding: utf-8 -*-
"""
Render の無料 Web Service 上で「1回分の動作確認クロール」を実行するための
ラッパースクリプト。

無料プランには Background Worker が無いため、代わりに Web Service として
起動する。ポートを開いてHTTPリクエストを待ち受けつつ、実際のクロール処理は
バックグラウンドスレッドで別途進める構成にしている。

ブラウザ(または `curl`)で `/` にアクセスすると、その時点の進捗・
最終結果(整合性チェック込み)がテキストで確認できる。

環境変数:
  MAX_GAMES        取得する試合数の上限 (既定: 100)
  CRAWL_INTERVAL    リクエスト間隔(秒) (既定: 5.0、orekou.net運営から
                     確認済みの「1秒1回未満」を守るための下限を大きく下回らないこと)
  CRAWL_JITTER      ジッター(秒) (既定: 2.0)
  PORT              Renderが自動的に注入する待受ポート(通常は自分で設定不要)

無料プランは一定時間アクセスが無いとスリープするため、デプロイ後は
このサービスのURLを開いたままにしておく(または数分おきに再読み込みする)
ことをおすすめする。100試合程度であれば、スリープが発生する前に
クロール自体は完了する見込み。

継続収集(本番運用)にはこのスクリプトは使わず、外部DBに保存する版に
別途差し替える想定。あくまで動作確認用。

--- 修正内容 (2026-09-10) ---
進捗ログの出所は2種類ある:
  1. orekou_http.py: 標準 logging モジュール経由で "OK https://..." 等を出力
  2. orekou_crawler.py: print() を直接呼んで "[school_list] ..." 等を出力
どちらも標準出力/標準エラー出力には流れる(Renderの「Logs」タブには表示される)が、
このスクリプト独自の _state["log_lines"](ブラウザで見えるステータスページ)には
従来まったく反映されていなかった。

そのため、クロールが完了するまでステータスページのログ欄が起動時の2行のまま
止まって見える、という表示上の問題があった。

対応として、クロールを実行するバックグラウンドスレッドの間だけ、
sys.stdout / sys.stderr をこのスレッド専用のラッパーに差し替え、
書き込まれた内容を元のストリームに流しつつ _state["log_lines"] にも
1行ずつ追記するようにした。これにより、logging経由・print()直接呼び出し
のどちらの進捗も、ブラウザのステータスページにリアルタイムで反映される。

(Pythonの sys.stdout はプロセス全体で共有されるグローバルな差し替えになるが、
このプロセスはクロール実行専用の単一ワーカースレッドしか使わないため、
実用上は問題ない。)
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from orekou_crawler import run
from orekou_http import BUDGET_FILE, DEFAULT_DAILY_REQUEST_BUDGET
from orekou_validate import validate_matches_file

MATCHES_PATH = "matches.jsonl"
STUDENTS_PATH = "students.jsonl"
PROGRESS_PATH = "crawl_progress.json"
SCHOOL_LIST_TOTAL = 47  # 都道府県数(school_listページの総数、固定)

# バックグラウンドスレッドとHTTPハンドラの間で状態を共有するための簡易ストア。
# 単一プロセス内の単純な共有なのでロックは最小限(status文字列の置き換えのみ)にしている。
_state = {
    "status": "starting",  # starting / running / done / error
    "started_at": None,
    "finished_at": None,
    "log_lines": [],
    "summary": None,
    "max_games": None,
    "progress": None,  # _progress_monitor() が更新する詳細進捗(下記参照)
}
_state_lock = threading.Lock()


def _log(msg: str):
    """_state["log_lines"] に1行追記しつつ、元の標準出力にもそのまま出す。"""
    print(msg, file=_ORIGINAL_STDOUT, flush=True)
    with _state_lock:
        _state["log_lines"].append(msg)
        # ログが無限に増え続けないよう直近500行に制限
        _state["log_lines"] = _state["log_lines"][-500:]


_ORIGINAL_STDOUT = sys.stdout
_ORIGINAL_STDERR = sys.stderr


class _TeeStream:
    """print() や logging.StreamHandler からの書き込みを、元のストリームに
    そのまま流しつつ、1行ごとに _state["log_lines"] にも追記するラッパー。

    orekou_http.py の logger.info("OK ...") も、orekou_crawler.py の
    print("[school_list] ...") も、最終的にはこのストリームへの write()
    呼び出しに帰着するため、個々のモジュールを直接いじらずに両方を
    まとめて拾うことができる。
    """

    def __init__(self, original):
        self._original = original
        self._buffer = ""

    def write(self, s: str):
        self._original.write(s)
        self._buffer += s
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line:
                with _state_lock:
                    _state["log_lines"].append(line)
                    _state["log_lines"] = _state["log_lines"][-500:]

    def flush(self):
        self._original.flush()

    def isatty(self):
        return False


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    return float(val) if val else default


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    return int(val) if val else default


def _count_lines(path: str) -> int:
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        return sum(1 for _ in f)


def _read_json(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # 書き込みの瞬間と読み取りがかち合った場合など。次回のポーリングに任せる。
        return None


def _progress_monitor():
    """crawl_progress.json / matches.jsonl / students.jsonl / 日次予算ファイルを
    定期的に読み取り、フェーズ別進捗・実測レート・推定終了時刻・予算残りを
    _state["progress"] にまとめておくバックグラウンドスレッド。

    orekou_crawler.run() 自体には手を入れず、外側から状態ファイルを覗き見る
    方式にしているので、クローラー本体のロジックとは疎結合。
    """
    POLL_SEC = 3
    while True:
        time.sleep(POLL_SEC)

        with _state_lock:
            status = _state["status"]
            started_at_str = _state["started_at"]
            max_games = _state["max_games"] or _env_int("MAX_GAMES", 100)

        if status in ("starting",):
            continue

        progress_data = _read_json(PROGRESS_PATH) or {}
        visited = progress_data.get("visited_urls", [])
        pending_school_ids = progress_data.get("pending_school_ids", [])
        pending_game_hashes = progress_data.get("pending_game_hashes", [])
        pending_student_ids = progress_data.get("pending_student_ids", [])

        school_list_done = sum(1 for u in visited if "/profile/school_list/" in u)
        schools_visited = sum(
            1 for u in visited if "/profile/school/" in u and "/profile/school_list/" not in u
        )
        games_visited = sum(1 for u in visited if "/g/" in u)
        students_visited = sum(1 for u in visited if "/profile/student/" in u)

        matches_done = _count_lines(MATCHES_PATH)  # 練習試合として保存された数(=games完了の実数)

        # --- 実測レート & ETA ---
        total_requests = len(visited)
        rate_per_sec = None
        eta_seconds = None
        eta_display = None
        if started_at_str:
            try:
                started_ts = time.mktime(time.strptime(started_at_str, "%Y-%m-%d %H:%M:%S"))
                elapsed = max(1.0, time.time() - started_ts)
                if total_requests > 0:
                    rate_per_sec = total_requests / elapsed
                    avg_sec_per_req = elapsed / total_requests
                    remaining_requests = (
                        max(0, SCHOOL_LIST_TOTAL - school_list_done)
                        + len(pending_school_ids)
                        + max(0, max_games - matches_done)
                        + len(pending_student_ids)
                    )
                    eta_seconds = remaining_requests * avg_sec_per_req
                    eta_display = time.strftime(
                        "%H:%M", time.localtime(time.time() + eta_seconds)
                    )
            except ValueError:
                pass

        # --- 日次リクエスト予算 ---
        budget_data = _read_json(str(BUDGET_FILE))
        budget_remaining = None
        if budget_data:
            today = time.strftime("%Y-%m-%d")
            if budget_data.get("date") == today:
                budget_remaining = DEFAULT_DAILY_REQUEST_BUDGET - budget_data.get("count", 0)
        if budget_remaining is None:
            # まだ今日リクエストしていない、またはファイルが無い場合は満額扱い
            budget_remaining = DEFAULT_DAILY_REQUEST_BUDGET

        phases = {
            "school_list": {"done": school_list_done, "total": SCHOOL_LIST_TOTAL},
            "school_profile": {
                "done": schools_visited,
                "total": schools_visited + len(pending_school_ids),
            },
            "games": {"done": matches_done, "total": max_games},
            "students": {
                "done": students_visited,
                "total": students_visited + len(pending_student_ids),
            },
        }

        with _state_lock:
            _state["progress"] = {
                "phases": phases,
                "total_requests": total_requests,
                "rate_per_sec": rate_per_sec,
                "eta_seconds": eta_seconds,
                "eta_display": eta_display,
                "budget_remaining": budget_remaining,
                "budget_total": DEFAULT_DAILY_REQUEST_BUDGET,
            }

        if status in ("done", "error"):
            break


def _run_crawl():
    max_games = _env_int("MAX_GAMES", 100)
    interval = _env_float("CRAWL_INTERVAL", 5.0)
    jitter = _env_float("CRAWL_JITTER", 2.0)

    # クロール処理中だけ、標準出力・標準エラー出力をこのスレッド用のTeeに差し替える。
    sys.stdout = _TeeStream(_ORIGINAL_STDOUT)
    sys.stderr = _TeeStream(_ORIGINAL_STDERR)

    with _state_lock:
        _state["status"] = "running"
        _state["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _state["max_games"] = max_games

    _log("=== orekou.net 動作確認クロール開始 ===")
    _log(f"max_games={max_games}, interval={interval}, jitter={jitter}")

    try:
        run(
            progress_path=PROGRESS_PATH,
            matches_path=MATCHES_PATH,
            students_path=STUDENTS_PATH,
            max_games=max_games,
            interval=interval,
            jitter=jitter,
        )
    except Exception as e:
        _log(f"\n=== クロール中にエラーが発生しました: {e} ===")
        with _state_lock:
            _state["status"] = "error"
            _state["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        sys.stdout, sys.stderr = _ORIGINAL_STDOUT, _ORIGINAL_STDERR
        return

    _log("\n=== クロール完了。整合性チェックを実行します ===")
    summary = None
    if os.path.exists(MATCHES_PATH):
        result = validate_matches_file(MATCHES_PATH, max_print=20)
        summary = result["summary"]
        _log(json.dumps(summary, ensure_ascii=False, indent=2))
        if result["details"]:
            _log("\n警告のある試合の詳細(先頭20件):")
            for d in result["details"]:
                gh = d["game_hash"] or "?"
                _log(f"  L{d['line']} game_hash={gh[:12]}...")
                for w in d["warnings"]:
                    _log(f"    - {w}")
    else:
        _log("matches.jsonl が生成されませんでした(取得0件、または途中で停止した可能性があります)")

    for path in (MATCHES_PATH, STUDENTS_PATH):
        if os.path.exists(path):
            size = os.path.getsize(path)
            with open(path, encoding="utf-8") as f:
                line_count = sum(1 for _ in f)
            _log(f"{path}: {line_count}行, {size}バイト")
        else:
            _log(f"{path}: 生成されませんでした")

    _log("\n=== 全処理完了 ===")
    with _state_lock:
        _state["status"] = "done"
        _state["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _state["summary"] = summary

    # クロールが終わったら標準出力・標準エラー出力を元に戻しておく
    # (このプロセスは以後HTTPサーバーしか動かないので実害はないが、念のため)
    sys.stdout, sys.stderr = _ORIGINAL_STDOUT, _ORIGINAL_STDERR


class StatusHandler(BaseHTTPRequestHandler):
    # ダウンロード可能なファイル名とパスの対応表。
    # URLで直接ファイルパスを指定させず、ホワイトリスト化した名前だけ受け付ける
    # (任意パス読み取りを防ぐため)。
    _DOWNLOADABLE = {
        "matches": MATCHES_PATH,
        "students": STUDENTS_PATH,
        "progress": PROGRESS_PATH,
    }

    def _send_file(self, path: str, download_name: str):
        if not os.path.exists(path):
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"{path} はまだ生成されていません。".encode("utf-8"))
            return
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _snapshot(self):
        with _state_lock:
            return {
                "status": _state["status"],
                "started_at": _state["started_at"],
                "finished_at": _state["finished_at"],
                "log_text": "\n".join(_state["log_lines"]),
                "summary": _state["summary"],
                "progress": _state["progress"],
            }

    def do_GET(self):
        if self.path.startswith("/download/"):
            key = self.path[len("/download/"):].strip("/")
            path = self._DOWNLOADABLE.get(key)
            if path is None:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                names = ", ".join(self._DOWNLOADABLE.keys())
                self.wfile.write(
                    f"不明なダウンロード名です。使えるのは: {names}".encode("utf-8")
                )
                return
            self._send_file(path, os.path.basename(path))
            return

        if self.path.startswith("/status.json"):
            snap = self._snapshot()
            body = json.dumps({
                "status": snap["status"],
                "started_at": snap["started_at"],
                "finished_at": snap["finished_at"],
                "summary": snap["summary"],
                "progress": snap["progress"],
                "files": {
                    name: os.path.exists(path)
                    for name, path in self._DOWNLOADABLE.items()
                },
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # ポーリングで叩かれるエンドポイントなのでキャッシュさせない
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        snap = self._snapshot()
        status = snap["status"]
        started_at = snap["started_at"]
        finished_at = snap["finished_at"]
        log_text = snap["log_text"]
        summary = snap["summary"]

        text_lines = [
            "orekou.net 動作確認クロール ステータス",
            "=" * 40,
            f"status: {status}",
            f"started_at: {started_at}",
            f"finished_at: {finished_at}",
            "",
        ]
        if summary:
            text_lines.append("--- サマリー ---")
            text_lines.append(json.dumps(summary, ensure_ascii=False, indent=2))
            text_lines.append("")

        if snap["progress"]:
            p = snap["progress"]
            text_lines.append("--- 進捗 ---")
            for key, label in (
                ("school_list", "学校一覧"),
                ("school_profile", "学校プロフィール"),
                ("games", "試合取得"),
                ("students", "選手取得"),
            ):
                ph = p["phases"][key]
                pct = (ph["done"] / ph["total"] * 100) if ph["total"] else 0
                text_lines.append(f"  {label}: {ph['done']}/{ph['total']} ({pct:.0f}%)")
            if p["rate_per_sec"] is not None:
                text_lines.append(f"  実測レート: {p['rate_per_sec']:.3f} req/秒")
            if p["eta_display"]:
                eta_min = round(p["eta_seconds"] / 60)
                text_lines.append(f"  推定終了時刻: {p['eta_display']}頃(残り約{eta_min}分)")
            text_lines.append(f"  本日のリクエスト予算残り: {p['budget_remaining']}/{p['budget_total']}")
            text_lines.append("")

        text_lines.append("--- ダウンロード ---")
        for name, path in self._DOWNLOADABLE.items():
            if os.path.exists(path):
                size = os.path.getsize(path)
                text_lines.append(f"/download/{name}  ({path}, {size}バイト)")
            else:
                text_lines.append(f"/download/{name}  ({path}, まだ生成されていません)")
        text_lines.append("")

        text_lines.append("--- ログ(直近500行) ---")
        text_lines.append(log_text)

        text_body = "\n".join(text_lines)

        # HTMLで返し、以下2つをJavaScriptでやらせる:
        #  1. 定期的に /status.json をポーリングして表示を更新
        #     (このポーリング自体が定期的なHTTPアクセスになるので、
        #      ブラウザタブを開いたままにしておけば無料プランの
        #      「アクセスが無いとスリープする」対策にもなる)
        #  2. status が "done" になったら、matches.jsonl / students.jsonl を
        #     自動的にダウンロードする(localStorage で started_at ごとに
        #     「もうダウンロード済みか」を記録し、二重ダウンロードや
        #     ページ再読み込みのたびの再ダウンロードを防ぐ)
        status_label = {
            "starting": "起動中",
            "running": "実行中",
            "done": "完了",
            "error": "エラー",
        }.get(status, status)

        if summary:
            import html as _html_escape_mod
            summary_html = (
                "<pre style='margin:0;font-size:0.85em;white-space:pre-wrap'>"
                + _html_escape_mod.escape(json.dumps(summary, ensure_ascii=False, indent=2))
                + "</pre>"
            )
        else:
            summary_html = '<span style="color:#9aa2ad">まだ結果がありません(クロール完了後に表示されます)</span>'

        html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>orekou.net クロール ステータス</title>
<style>
:root {{
  --bg: #f4f5f7;
  --card-bg: #ffffff;
  --border: #e3e5e8;
  --text: #24292f;
  --text-muted: #6b7280;
  --accent: #2f6feb;
  --radius: 10px;
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Hiragino Sans, "Noto Sans JP", sans-serif;
  background: var(--bg);
  color: var(--text);
  margin: 0;
  padding: 2em 1em;
}}
.container {{ max-width: 760px; margin: 0 auto; }}
h1 {{ font-size: 1.25em; margin: 0 0 1em; }}
.card {{
  background: var(--card-bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.2em 1.4em;
  margin-bottom: 1em;
  box-shadow: 0 1px 2px rgba(0,0,0,0.03);
}}
.card h2 {{
  font-size: 0.85em;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-muted);
  margin: 0 0 0.9em;
  font-weight: 600;
}}
#banner {{
  display: flex;
  align-items: center;
  gap: 0.6em;
  padding: 0.9em 1.2em;
  border-radius: var(--radius);
  margin-bottom: 1em;
  font-weight: 500;
}}
.badge {{
  display: inline-block;
  padding: 0.15em 0.7em;
  border-radius: 999px;
  font-size: 0.8em;
  font-weight: 600;
  color: #fff;
}}
.status-running #banner, #banner.status-running {{ background: #fff8e1; color: #8a6d00; }}
.status-running .badge, #banner.status-running .badge {{ background: #e8a900; }}
#banner.status-done {{ background: #e6f6ec; color: #146c2e; }}
#banner.status-done .badge {{ background: #2ea44f; }}
#banner.status-error {{ background: #fdeceb; color: #a3231a; }}
#banner.status-error .badge {{ background: #cf222e; }}
#banner.status-starting {{ background: #eef0f2; color: #57606a; }}
#banner.status-starting .badge {{ background: #6e7781; }}

.phase-row {{ display: flex; align-items: center; margin-bottom: 0.6em; font-size: 0.92em; }}
.phase-row:last-child {{ margin-bottom: 0; }}
.phase-label {{ width: 9.5em; flex-shrink: 0; color: var(--text-muted); }}
.phase-bar-outer {{
  flex-grow: 1; background: #eceef1; border-radius: 999px;
  height: 0.6em; margin-right: 0.8em; overflow: hidden;
}}
.phase-bar-inner {{
  background: linear-gradient(90deg, #4caf50, #2ea44f);
  height: 100%; width: 0%; border-radius: 999px; transition: width 0.4s ease;
}}
.phase-pct {{ width: 5.5em; text-align: right; flex-shrink: 0; color: var(--text-muted); font-variant-numeric: tabular-nums; }}

#meta {{ display: flex; flex-wrap: wrap; gap: 0.5em; }}
.chip {{
  background: #eef2ff; color: #3b4cca; border-radius: 999px;
  padding: 0.3em 0.8em; font-size: 0.85em; font-weight: 500;
}}

.dl-list {{ display: flex; flex-direction: column; gap: 0.5em; }}
.dl-row {{
  display: flex; justify-content: space-between; align-items: center;
  padding: 0.5em 0.8em; background: #f9fafb; border-radius: 6px; font-size: 0.9em;
}}
.dl-row a {{ color: var(--accent); text-decoration: none; font-weight: 600; }}
.dl-row a:hover {{ text-decoration: underline; }}
.dl-row .dl-meta {{ color: var(--text-muted); }}

#log-box {{
  background: #0d1117; color: #c9d1d9; border-radius: var(--radius);
  padding: 1em 1.2em; max-height: 420px; overflow-y: auto;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.82em; line-height: 1.5; white-space: pre-wrap;
}}
</style>
</head>
<body>
<div class="container">
<h1>orekou.net 動作確認クロール ステータス</h1>

<div id="banner" class="status-{status}">
  <span class="badge">{status_label}</span>
  <span id="banner-text">自動更新中(15秒おき)。完了したら自動ダウンロードします。</span>
</div>

<div class="card">
  <h2>進捗</h2>
  <div id="phases"></div>
</div>

<div class="card">
  <h2>メトリクス</h2>
  <div id="meta"></div>
</div>

<div class="card">
  <h2>ダウンロード</h2>
  <div id="downloads" class="dl-list"></div>
</div>

<div class="card">
  <h2>整合性チェック サマリー</h2>
  <div id="summary-box">{summary_html}</div>
</div>

<div class="card">
  <h2>ログ(直近500行)</h2>
  <div id="log-box">{log_text}</div>
</div>

<pre id="body" style="display:none">{text_body}</pre>
</div>
<script>
const POLL_MS = 15000;
const DOWNLOAD_KEY_PREFIX = "orekou_auto_downloaded_";
const NOTIFY_KEY_PREFIX = "orekou_auto_notified_";
const PHASE_LABELS = {{
  school_list: "学校一覧",
  school_profile: "学校プロフィール",
  games: "試合取得",
  students: "選手取得",
}};
const STATUS_LABELS = {{
  starting: "起動中", running: "実行中", done: "完了", error: "エラー",
}};
const DL_LABELS = {{
  matches: "matches.jsonl(試合データ)",
  students: "students.jsonl(選手データ)",
  progress: "crawl_progress.json(進捗ファイル)",
}};

let audioCtx = null;

function beep(freq, durationMs) {{
  try {{
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.frequency.value = freq;
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    gain.gain.setValueAtTime(0.15, audioCtx.currentTime);
    osc.start();
    osc.stop(audioCtx.currentTime + durationMs / 1000);
  }} catch (e) {{
    // Web Audio が使えない環境では無視する
  }}
}}

function alreadyDone(prefix, startedAt) {{
  return localStorage.getItem(prefix + startedAt) === "1";
}}

function markDone(prefix, startedAt) {{
  localStorage.setItem(prefix + startedAt, "1");
}}

function triggerDownload(url) {{
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}}

function notify(title, body) {{
  if (Notification.permission === "granted") {{
    new Notification(title, {{ body: body }});
  }}
}}

if (window.Notification && Notification.permission === "default") {{
  Notification.requestPermission();
}}

function renderPhases(progress) {{
  const container = document.getElementById("phases");
  if (!progress) {{ container.innerHTML = '<span style="color:#9aa2ad">まだデータがありません</span>'; return; }}
  let html = "";
  for (const key of ["school_list", "school_profile", "games", "students"]) {{
    const ph = progress.phases[key];
    const pct = ph.total ? Math.min(100, (ph.done / ph.total) * 100) : 0;
    html += '<div class="phase-row">'
      + '<div class="phase-label">' + PHASE_LABELS[key] + '</div>'
      + '<div class="phase-bar-outer"><div class="phase-bar-inner" style="width:' + pct.toFixed(0) + '%"></div></div>'
      + '<div class="phase-pct">' + ph.done + ' / ' + ph.total + '</div>'
      + '</div>';
  }}
  container.innerHTML = html;

  const meta = document.getElementById("meta");
  let chips = "";
  if (progress.rate_per_sec !== null) {{
    chips += '<span class="chip">実測レート ' + progress.rate_per_sec.toFixed(3) + ' req/秒</span>';
  }}
  if (progress.eta_display) {{
    const etaMin = Math.round(progress.eta_seconds / 60);
    chips += '<span class="chip">推定終了 ' + progress.eta_display + '頃(残り約' + etaMin + '分)</span>';
  }}
  chips += '<span class="chip">本日の予算残り ' + progress.budget_remaining + ' / ' + progress.budget_total + '</span>';
  meta.innerHTML = chips;
}}

function renderDownloads(files) {{
  const container = document.getElementById("downloads");
  let html = "";
  for (const key of ["matches", "students", "progress"]) {{
    const ready = files && files[key];
    html += '<div class="dl-row">'
      + '<span>' + DL_LABELS[key] + '</span>'
      + (ready
          ? '<a href="/download/' + key + '">ダウンロード</a>'
          : '<span class="dl-meta">未生成</span>')
      + '</div>';
  }}
  container.innerHTML = html;
}}

async function poll() {{
  let data;
  try {{
    const res = await fetch("/status.json", {{cache: "no-store"}});
    data = await res.json();
  }} catch (e) {{
    return; // 一時的な通信失敗は無視して次回に任せる
  }}

  const banner = document.getElementById("banner");
  const bannerText = document.getElementById("banner-text");
  banner.className = "status-" + data.status;
  banner.querySelector(".badge").textContent = STATUS_LABELS[data.status] || data.status;
  renderPhases(data.progress);
  renderDownloads(data.files);

  if (data.status === "done" && data.started_at) {{
    if (!alreadyDone(DOWNLOAD_KEY_PREFIX, data.started_at)) {{
      if (data.files.matches) triggerDownload("/download/matches");
      if (data.files.students) triggerDownload("/download/students");
      markDone(DOWNLOAD_KEY_PREFIX, data.started_at);
    }}
    if (!alreadyDone(NOTIFY_KEY_PREFIX, data.started_at)) {{
      notify("orekou.net クロール完了", "matches.jsonl / students.jsonl を自動ダウンロードしました。");
      beep(880, 200);
      setTimeout(() => beep(1320, 200), 250);
      markDone(NOTIFY_KEY_PREFIX, data.started_at);
    }}
    bannerText.textContent = "完了。matches.jsonl / students.jsonl を自動ダウンロードしました(このタブでは再ダウンロードしません)。";
  }} else if (data.status === "error") {{
    if (data.started_at && !alreadyDone(NOTIFY_KEY_PREFIX + "err_", data.started_at)) {{
      notify("orekou.net クロールでエラー", "ログを確認してください。");
      beep(220, 400);
      markDone(NOTIFY_KEY_PREFIX + "err_", data.started_at);
    }}
    bannerText.textContent = "エラーが発生しました。ログを確認してください。";
  }} else {{
    bannerText.textContent = "自動更新中(15秒おき)。完了したら自動ダウンロードします。";
  }}

  // ログ・サマリーは非表示の要素を再取得して更新する
  try {{
    const pageRes = await fetch(location.pathname, {{cache: "no-store"}});
    const text = await pageRes.text();
    const doc = new DOMParser().parseFromString(text, "text/html");
    const newLog = doc.getElementById("log-box");
    if (newLog) document.getElementById("log-box").textContent = newLog.textContent;
    const newSummary = doc.getElementById("summary-box");
    if (newSummary) document.getElementById("summary-box").innerHTML = newSummary.innerHTML;
  }} catch (e) {{
    // 無視して次回のポーリングに任せる
  }}
}}

renderPhases(null);
poll();
setInterval(poll, POLL_MS);
</script>
</body>
</html>
"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # BaseHTTPRequestHandlerの標準アクセスログは冗長なので無効化
        pass


def main():
    # クロールはバックグラウンドスレッドで開始し、HTTPサーバーはすぐに待受を始める。
    # (Renderはポートが開くまでを起動処理として見ているため、先にポートを開けておく)
    worker = threading.Thread(target=_run_crawl, daemon=True)
    worker.start()

    monitor = threading.Thread(target=_progress_monitor, daemon=True)
    monitor.start()

    port = _env_int("PORT", 10000)
    server = HTTPServer(("0.0.0.0", port), StatusHandler)
    print(f"HTTP status server listening on port {port}", file=_ORIGINAL_STDOUT, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
