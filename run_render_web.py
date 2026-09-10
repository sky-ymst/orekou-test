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
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from orekou_crawler import run
from orekou_validate import validate_matches_file

MATCHES_PATH = "matches.jsonl"
STUDENTS_PATH = "students.jsonl"
PROGRESS_PATH = "crawl_progress.json"

# バックグラウンドスレッドとHTTPハンドラの間で状態を共有するための簡易ストア。
# 単一プロセス内の単純な共有なのでロックは最小限(status文字列の置き換えのみ)にしている。
_state = {
    "status": "starting",       # starting / running / done / error
    "started_at": None,
    "finished_at": None,
    "log_lines": [],
    "summary": None,
}
_state_lock = threading.Lock()


def _log(msg: str):
    print(msg, flush=True)
    with _state_lock:
        _state["log_lines"].append(msg)
        # ログが無限に増え続けないよう直近500行に制限
        _state["log_lines"] = _state["log_lines"][-500:]


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    return float(val) if val else default


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    return int(val) if val else default


def _run_crawl():
    max_games = _env_int("MAX_GAMES", 100)
    interval = _env_float("CRAWL_INTERVAL", 5.0)
    jitter = _env_float("CRAWL_JITTER", 2.0)

    with _state_lock:
        _state["status"] = "running"
        _state["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

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
                    _log(f"      - {w}")
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


class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        with _state_lock:
            status = _state["status"]
            started_at = _state["started_at"]
            finished_at = _state["finished_at"]
            log_text = "\n".join(_state["log_lines"])
            summary = _state["summary"]

        body_lines = [
            "orekou.net 動作確認クロール ステータス",
            "=" * 40,
            f"status: {status}",
            f"started_at: {started_at}",
            f"finished_at: {finished_at}",
            "",
        ]
        if summary:
            body_lines.append("--- サマリー ---")
            body_lines.append(json.dumps(summary, ensure_ascii=False, indent=2))
            body_lines.append("")
        body_lines.append("--- ログ(直近500行) ---")
        body_lines.append(log_text)

        body = "\n".join(body_lines).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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

    port = _env_int("PORT", 10000)
    server = HTTPServer(("0.0.0.0", port), StatusHandler)
    print(f"HTTP status server listening on port {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
