# -*- coding: utf-8 -*-
"""
orekou.net クローラー用の「訪問済みURL管理」モジュール。

- 同じURLを二度とリクエストしないようにする(重複スクレイピング防止)
- 進捗をJSONファイルに保存し、中断→再開できるようにする
  (1万試合規模の収集は数時間〜半日単位になるため必須)

使い方:
    state = CrawlState("crawl_progress.json")

    if not state.is_visited(url):
        html = fetch(url)
        state.mark_visited(url)
        state.save()  # こまめに保存(クラッシュ対策)
"""

import json
import os


class CrawlState:
    def __init__(self, path: str):
        self.path = path
        self.visited_urls = set()          # 取得済みURL(重複排除の中核)
        self.pending_school_ids = set()    # 発見済みだがまだ学校ページ未取得
        self.pending_game_hashes = set()   # 発見済みだがまだ試合ページ未取得
        self.pending_student_ids = set()   # 発見済みだがまだ選手ページ未取得(school_id, number)のタプル
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.visited_urls = set(data.get("visited_urls", []))
            self.pending_school_ids = set(data.get("pending_school_ids", []))
            self.pending_game_hashes = set(data.get("pending_game_hashes", []))
            self.pending_student_ids = {
                tuple(x) for x in data.get("pending_student_ids", [])
            }

    def save(self):
        data = {
            "visited_urls": sorted(self.visited_urls),
            "pending_school_ids": sorted(self.pending_school_ids),
            "pending_game_hashes": sorted(self.pending_game_hashes),
            "pending_student_ids": sorted(list(self.pending_student_ids)),
        }
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.path)  # 書き込み途中でのファイル破損を防止

    # --- URL単位の重複チェック ---
    def is_visited(self, url: str) -> bool:
        return url in self.visited_urls

    def mark_visited(self, url: str):
        self.visited_urls.add(url)

    # --- 発見済み(未取得)キューへの追加。重複しているものは自然に無視される(set) ---
    def add_school_ids(self, school_ids):
        for sid in school_ids:
            self.pending_school_ids.add(sid)

    def add_game_hashes(self, game_hashes):
        for gh in game_hashes:
            self.pending_game_hashes.add(gh)

    def add_student_ids(self, student_ids):
        # student_ids: iterable of (school_id, student_number)
        for sid, num in student_ids:
            self.pending_student_ids.add((sid, num))

    def stats(self) -> dict:
        return {
            "visited_urls": len(self.visited_urls),
            "pending_school_ids": len(self.pending_school_ids),
            "pending_game_hashes": len(self.pending_game_hashes),
            "pending_student_ids": len(self.pending_student_ids),
        }


if __name__ == "__main__":
    # 簡易動作確認
    state = CrawlState("/home/claude/test_progress.json")
    state.add_school_ids(["abc123", "def456", "abc123"])  # 重複は自動的に1件扱い
    state.mark_visited("https://orekou.net/profile/school/abc123")
    state.save()
    print(state.stats())
    print("重複排除の確認:", state.pending_school_ids)
