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
from orekou_validate import validate_matches_file

MATCHES_PATH = "matches.jsonl"
STUDENTS_PATH = "students.jsonl"
PROGRESS_PATH = "crawl_progress.json"

# バックグラウンドスレッドとHTTPハンドラの間で状態を共有するための簡易ストア。
# 単一プロセス内の単純な共有なのでロックは最小限(status文字列の置き換えのみ)にしている。
_state = {
    "status": "starting",  # starting / running / done / error
    "started_at": None,
    "finished_at": None,
    "log_lines": [],
    "summary": None,
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

        body_lines.append("--- ダウンロード ---")
        for name, path in self._DOWNLOADABLE.items():
            if os.path.exists(path):
                size = os.path.getsize(path)
                body_lines.append(f"/download/{name}  ({path}, {size}バイト)")
            else:
                body_lines.append(f"/download/{name}  ({path}, まだ生成されていません)")
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
    print(f"HTTP status server listening on port {port}", file=_ORIGINAL_STDOUT, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
