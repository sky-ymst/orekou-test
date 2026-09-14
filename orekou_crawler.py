# -*- coding: utf-8 -*-
"""
orekou.net 統合クローラー。

  ① school_list/1〜47 を巡回          → 学校IDを収集
  ② 各学校プロフィールページを取得     → 試合ハッシュを収集(所属選手一覧は使わない)
  ③ 収集した試合ハッシュを取得         → 試合データ保存 + 選手IDを収集
  ④ 収集した選手IDを取得               → 選手プロフィールデータを保存

すべてのステップで crawl_state.CrawlState により「同じURLは二度と取得しない」を
徹底し、進捗はJSONファイルに永続化するので、中断してもそこから再開できる。

出力:
  matches.jsonl  : 1試合1行のJSON(box score・両チーム選手成績など)
  students.jsonl : 1選手1行のJSON(現在能力・成績など)

実行例:
    python orekou_crawler.py --max-games 10000
"""

import json
import argparse
import sys
from pathlib import Path

from crawl_state import CrawlState
from orekou_http import CircuitBreakerOpen, DailyBudgetExceeded, DEFAULT_INTERVAL, DEFAULT_JITTER
from orekou_school_list_scraper import fetch_school_list
from orekou_school_profile_scraper import fetch_school_profile
from orekou_scraper import fetch_game_page
from orekou_student_scraper import fetch_student_profile

# orekou_http.py と同じ理由で、実行時のカレントディレクトリに依存させない。
# ダブルクリック起動や別フォルダからの実行でも、常にこのスクリプトが置かれた
# フォルダ内の同じファイルを見にいくようにする。
_BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PROGRESS_PATH = str(_BASE_DIR / "crawl_progress.json")
DEFAULT_MATCHES_PATH = str(_BASE_DIR / "matches.jsonl")
DEFAULT_STUDENTS_PATH = str(_BASE_DIR / "students.jsonl")


def append_jsonl(path: str, record: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def run(progress_path=DEFAULT_PROGRESS_PATH,
        matches_path=DEFAULT_MATCHES_PATH,
        students_path=DEFAULT_STUDENTS_PATH,
        prefecture_ids=range(1, 48),
        max_games=10000,
        interval=DEFAULT_INTERVAL,
        jitter=DEFAULT_JITTER):

    state = CrawlState(progress_path)
    print(f"[起動] 進捗ファイル: {progress_path}")
    print(f"       (前回の続きがあれば自動で再開します。既存の統計: {state.stats()})")

    try:
        _run_phases(state, matches_path, students_path, prefecture_ids,
                    max_games, interval, jitter)
    except CircuitBreakerOpen as e:
        state.save()
        print(f"\n[停止] サーバーへの連続アクセス失敗が続いたため、安全のため停止しました: {e}")
        print("進捗は保存済みです。サイトの状況を確認後、同じコマンドで再開できます。")
        sys.exit(1)
    except DailyBudgetExceeded as e:
        state.save()
        print(f"\n[停止] 本日のリクエスト上限に達したため停止しました: {e}")
        print("進捗は保存済みです。日付が変わってから同じコマンドで再開できます。")
        sys.exit(1)

    print("完了:", state.stats())


def _run_phases(state, matches_path, students_path, prefecture_ids,
                 max_games, interval, jitter):
    # --- ① 学校一覧巡回 ---
    for pref_id in prefecture_ids:
        url = f"https://orekou.net/profile/school_list/{pref_id}"
        if state.is_visited(url):
            continue
        result = fetch_school_list(pref_id, interval=interval, jitter=jitter)
        state.mark_visited(url)
        state.add_school_ids(s["school_id"] for s in result["schools"])
        state.save()
        print(f"[school_list] {pref_id}: {result['prefecture']} "
              f"{result['school_count']}校 発見")

    # --- ② 学校プロフィール巡回(試合ハッシュ収集) ---
    for school_id in list(state.pending_school_ids):
        url = f"https://orekou.net/profile/school/{school_id}"
        if state.is_visited(url):
            state.pending_school_ids.discard(school_id)
            continue
        result = fetch_school_profile(school_id, interval=interval, jitter=jitter)
        state.mark_visited(url)
        state.add_game_hashes(g["game_hash"] for g in result["recent_games"])
        state.pending_school_ids.discard(school_id)
        state.save()
        print(f"[school] {result['school_info'].get('監督', '?')}"
              f"({school_id[:8]}...): 試合{len(result['recent_games'])}件発見 "
              f"(累計試合候補 {len(state.pending_game_hashes)})")

        if len(state.pending_game_hashes) >= max_games:
            print("目標試合数に到達したため学校巡回を打ち切ります")
            break

    # --- ③ 試合ページ取得(選手ID収集 + 試合データ保存) ---
    games_done = 0
    for game_hash in list(state.pending_game_hashes):
        if games_done >= max_games:
            break
        url = f"https://orekou.net/g/{game_hash}"
        if state.is_visited(url):
            state.pending_game_hashes.discard(game_hash)
            continue
        result = fetch_game_page(game_hash, interval=interval, jitter=jitter)
        state.mark_visited(url)  # 公式戦でも「取得済み」扱いにして再取得は防ぐ
        state.pending_game_hashes.discard(game_hash)

        if result.get("is_official"):
            # 公式戦(甲子園予選・地区大会等)は予測モデルの対象外のため保存しない。
            # ただし選手ID自体は有効なので、選手プロフィール収集には使う。
            state.add_student_ids(result["player_ids"])
            state.save()
            continue

        append_jsonl(matches_path, result)
        state.add_student_ids(result["player_ids"])
        games_done += 1
        state.save()
        if games_done % 20 == 0:
            print(f"[game] {games_done}試合取得済み(練習試合のみカウント) "
                  f"(選手候補 {len(state.pending_student_ids)}名)")

    # --- ④ 選手プロフィール取得 ---
    students_done = 0
    for school_id, student_number in list(state.pending_student_ids):
        url = f"https://orekou.net/profile/student/{school_id}/{student_number}"
        if state.is_visited(url):
            state.pending_student_ids.discard((school_id, student_number))
            continue
        result = fetch_student_profile(school_id, student_number,
                                        interval=interval, jitter=jitter)
        state.mark_visited(url)
        append_jsonl(students_path, result)
        state.pending_student_ids.discard((school_id, student_number))
        students_done += 1
        state.save()
        if students_done % 50 == 0:
            print(f"[student] {students_done}名取得済み")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-games", type=int, default=10000)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--jitter", type=float, default=DEFAULT_JITTER)
    args = parser.parse_args()
    run(max_games=args.max_games, interval=args.interval, jitter=args.jitter)
