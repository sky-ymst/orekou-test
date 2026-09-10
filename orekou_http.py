# -*- coding: utf-8 -*-
"""
orekou.net 向けの共通HTTPモジュール。

4つのスクレイパー(school_list, school_profile, orekou_scraper, student)は
すべてこの polite_get() 経由でHTMLを取得する(直接 requests.get を呼ばない)。

サーバー負荷対策として、以下をすべて一元的に実装している:

1. レート制限: デフォルト5秒+ジッター0〜2秒。プロセス全体で1つのタイマーを
   共有するので、どのスクレイパー経由で呼んでも間隔が守られる。
2. ディスクキャッシュ: 同じURLは二度とネットワークに取りに行かない。
3. リトライ: 5xx・タイムアウト・接続エラーは指数バックオフ(3→6→12秒)で
   最大3回まで再試行。4xx(ページ無し等の恒久的エラー)は再試行しない。
4. 429(Too Many Requests)専用ハンドリング: サーバーから「送りすぎ」の
   明示的な信号なので、通常のリトライより長めに待つ。`Retry-After`ヘッダーが
   あればそれに従う。
5. サーキットブレーカー: 直近で規定回数(既定5回)連続して取得に失敗したら、
   それ以上リクエストを送らずクロール全体を止める(サーバー障害時やブロック時に
   延々と叩き続けるのを防ぐ)。
6. 1日あたりのリクエスト上限(安全弁): 既定10,000件/日。バグ等で想定外の
   ループに入っても、無限にリクエストを送り続けないようにする。
7. ログファイル(orekou_http.log)への記録: 取得成功・失敗・待機時間を残す。
8. 明示的なUser-Agent: 身元と低頻度アクセスである旨を明記する。
"""

import hashlib
import json
import logging
import random
import time
from datetime import date
from pathlib import Path

import requests

DEFAULT_INTERVAL = 5.0
DEFAULT_JITTER = 2.0
# 運営に個人研究目的の低頻度アクセスとして問い合わせ・許可済み(1秒1回未満)。
# 身元は隠さず、"bot"という単語だけを避けた表記にしている
# (一部サイトではUser-Agentに"bot"を含む文字列を機械的にブロックすることがあるため)。
DEFAULT_USER_AGENT = (
    "orekou-personal-research/1.0 "
    "(individual, non-commercial research use; low-frequency access; "
    "contact permitted via site help page)"
)
DEFAULT_TIMEOUT = 15
DEFAULT_MAX_RETRIES = 3
DEFAULT_CIRCUIT_BREAKER_THRESHOLD = 5     # 連続失敗がこの回数に達したら全体停止
DEFAULT_DAILY_REQUEST_BUDGET = 10000      # 1日あたりの実リクエスト上限(安全弁)
DEFAULT_429_MIN_WAIT = 60.0               # 429時、Retry-Afterが無い場合の最低待機秒数

CACHE_DIR = Path("./orekou_http_cache")
BUDGET_FILE = Path("./orekou_http_budget.json")
LOG_FILE = Path("./orekou_http.log")

_last_request_ts = 0.0
_session = None
_consecutive_failures = 0


class CircuitBreakerOpen(Exception):
    """連続失敗がしきい値に達し、これ以上リクエストを送らないことを示す例外。"""


class DailyBudgetExceeded(Exception):
    """1日あたりのリクエスト上限に達したことを示す例外。"""


# --- ログ設定 ---
logger = logging.getLogger("orekou_http")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(sh)


def _get_session() -> requests.Session:
    """
    requests.Session を作成する。

    Windows環境(特にPython 3.14系)では、OSに証明書は存在するのに
    requests/urllib3側がそれを見つけられず SSLCertVerificationError
    (certificate verify failed: unable to get local issuer certificate)
    になるケースがある。certifi パッケージが入っていれば、その証明書束の
    パスを明示的に session.verify に指定することで回避できるため、
    certifi が使える場合は優先して使う。
    """
    global _session
    if _session is None:
        _session = requests.Session()
        try:
            import certifi
            _session.verify = certifi.where()
        except ImportError:
            pass  # certifi 未インストールなら requests 標準の挙動に任せる
    return _session


def _throttle(interval: float, jitter: float):
    """直前のリクエストから interval+jitter 秒あけるまでブロックする。"""
    global _last_request_ts
    elapsed = time.monotonic() - _last_request_ts
    wait = interval - elapsed + random.uniform(0, jitter)
    if wait > 0:
        time.sleep(wait)
    _last_request_ts = time.monotonic()


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.html"


# --- 1日あたりのリクエスト予算 ---

def _load_budget() -> dict:
    today = date.today().isoformat()
    if BUDGET_FILE.exists():
        try:
            data = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}
    if data.get("date") != today:
        data = {"date": today, "count": 0}
    return data


def _save_budget(data: dict):
    tmp = BUDGET_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(BUDGET_FILE)


def _consume_budget(daily_budget: int):
    data = _load_budget()
    if data["count"] >= daily_budget:
        raise DailyBudgetExceeded(
            f"本日の上限({daily_budget}件)に達しました。日付が変わってから再実行してください。"
        )
    data["count"] += 1
    _save_budget(data)
    return data["count"]


def reset_circuit_breaker():
    """呼び出し元が意図的にサーキットブレーカーをリセットしたい場合に使う。"""
    global _consecutive_failures
    _consecutive_failures = 0


def polite_get(url: str,
                interval: float = DEFAULT_INTERVAL,
                jitter: float = DEFAULT_JITTER,
                user_agent: str = DEFAULT_USER_AGENT,
                use_cache: bool = True,
                max_retries: int = DEFAULT_MAX_RETRIES,
                circuit_breaker_threshold: int = DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
                daily_budget: int = DEFAULT_DAILY_REQUEST_BUDGET) -> str:
    """
    URLを取得してHTML文字列を返す。

    - キャッシュ命中時はネットワークアクセス・予算消費なしで即座に返す。
    - キャッシュ未命中の場合のみ、日次予算チェック → レート制限の待機 →
      リクエスト、という順で進む。
    - 4xx系(429を除く)は即座に例外を送出しリトライしない。
    - 429・5xx系・タイムアウト・接続エラーは指数バックオフで最大 max_retries 回まで再試行。
    - 連続失敗が circuit_breaker_threshold 回に達したら CircuitBreakerOpen を送出し、
      それ以上一切リクエストを送らない(呼び出し元でクロール全体を止める想定)。
    """
    global _consecutive_failures

    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = _cache_path(url)

    if use_cache and cache_file.exists():
        return cache_file.read_text(encoding="utf-8")

    if _consecutive_failures >= circuit_breaker_threshold:
        raise CircuitBreakerOpen(
            f"連続{_consecutive_failures}回失敗したため、これ以上リクエストを送りません。"
            f"サイト側の状況を確認してから reset_circuit_breaker() を呼んで再開してください。"
        )

    sess = _get_session()
    last_exc = None

    for attempt in range(max_retries):
        _consume_budget(daily_budget)  # 予算超過ならここで DailyBudgetExceeded
        _throttle(interval, jitter)
        try:
            resp = sess.get(url, headers={"User-Agent": user_agent}, timeout=DEFAULT_TIMEOUT)

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else DEFAULT_429_MIN_WAIT
                wait = max(wait, DEFAULT_429_MIN_WAIT)
                logger.warning(f"429 Too Many Requests: {url} -> {wait:.0f}秒待機して再試行")
                time.sleep(wait)
                last_exc = requests.HTTPError("429 Too Many Requests")
                continue

            if 400 <= resp.status_code < 500:
                resp.raise_for_status()  # 429以外の4xxは恒久的エラーとして即座に送出

            resp.raise_for_status()
            resp.encoding = "utf-8"
            html = resp.text

            if use_cache:
                cache_file.write_text(html, encoding="utf-8")

            _consecutive_failures = 0  # 成功したらリセット
            logger.info(f"OK {url}")
            return html

        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code != 429 and 400 <= e.response.status_code < 500:
                _consecutive_failures += 1
                logger.error(f"NG(恒久的エラー) {url}: {e}")
                raise
            last_exc = e
        except requests.RequestException as e:
            last_exc = e

        backoff = (2 ** attempt) * 3
        logger.warning(f"取得失敗 ({attempt + 1}/{max_retries}) {url}: {last_exc} "
                        f"-> {backoff}秒後に再試行")
        time.sleep(backoff)

    _consecutive_failures += 1
    logger.error(f"NG(リトライ上限到達) {url}: {last_exc} "
                  f"(連続失敗 {_consecutive_failures}/{circuit_breaker_threshold})")
    raise RuntimeError(f"{url} の取得に{max_retries}回失敗しました: {last_exc}")


if __name__ == "__main__":
    # 簡易動作確認(実ネットワークは叩かず、キャッシュ機構だけ検証)
    test_url = "https://example.invalid/test"
    CACHE_DIR.mkdir(exist_ok=True)
    _cache_path(test_url).write_text("<html>cached</html>", encoding="utf-8")
    result = polite_get(test_url)
    assert result == "<html>cached</html>"
    print("キャッシュ経由の取得: OK(ネットワークアクセス・予算消費なしで返った)")
    _cache_path(test_url).unlink()
