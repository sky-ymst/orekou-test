# -*- coding: utf-8 -*-
"""
orekou_scraper.parse_game_page() が返す試合データの整合性を検証するモジュール。

目的: スクレイパーの解析ミス・想定外のページ構造を、収集後に自動で
検知できるようにする(1万件規模になると目視チェックは不可能なため)。

validate_game() は例外を投げず、問題点を文字列のリストとして返す。
致命的なエラー(クラッシュ)ではなく「後で見返すべき違和感」の一覧という位置づけ。

チェック項目:
1. イニングごとの得点の合計 == 最終スコア(box score)
2. 打撃成績の「得点」列の合計 == 最終スコア
3. 打撃成績の「安打」列の合計 == box scoreの安打数(H)
4. 自チームの投手成績「失点」列の合計 == 相手チームの最終スコア
   (投手が許した点=相手が取った点、という当然の裏付け関係)
5. 先発+控えの人数が極端(0人 等)でないか
6. 打撃/投手成績に登場する選手IDが、その試合の先発+控え名簿に
   含まれているか(参照整合性)
"""

TOLERANCE = 0  # 得点系の照合は本来ぴったり一致するはずなので許容誤差なし


def _to_int(val, default=0):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def validate_game(record: dict) -> list:
    """
    parse_game_page() の戻り値(1試合ぶんのdict)を検証し、
    問題があれば説明文字列のリストを返す(問題なければ空リスト)。
    """
    issues = list(record.get("parse_warnings", []))  # パース時点の警告も引き継ぐ

    teams = record.get("teams", [])
    if len(teams) != 2:
        issues.append(f"チーム数が{len(teams)}でした(通常2)。以降の整合性チェックは省略します。")
        return issues

    top, bot = teams[0], teams[1]

    for team, opponent, label in ((top, bot, "top(先攻)"), (bot, top, "bot(後攻)")):
        # 1. イニング合計 == 最終スコア
        innings_sum = sum(_to_int(v) for v in team.get("innings", []) if str(v).isdigit())
        if innings_sum != team.get("runs", 0):
            issues.append(
                f"[{label}] イニング合計得点({innings_sum})と最終スコア"
                f"({team.get('runs')})が一致しません"
            )

        # 2. 打撃成績の得点合計 == 最終スコア
        batting_runs_sum = sum(_to_int(b.get("得点")) for b in team.get("batting_stats", []))
        if team.get("batting_stats") and batting_runs_sum != team.get("runs", 0):
            issues.append(
                f"[{label}] 打撃成績の得点合計({batting_runs_sum})と最終スコア"
                f"({team.get('runs')})が一致しません"
            )

        # 3. 打撃成績の安打合計 == box scoreのH
        batting_hits_sum = sum(_to_int(b.get("安打")) for b in team.get("batting_stats", []))
        if team.get("batting_stats") and batting_hits_sum != team.get("hits", 0):
            issues.append(
                f"[{label}] 打撃成績の安打合計({batting_hits_sum})とbox scoreのH"
                f"({team.get('hits')})が一致しません"
            )

        # 4. 自チーム投手陣の失点合計 == 相手チームの最終スコア
        pitching_runs_sum = sum(_to_int(p.get("失点")) for p in team.get("pitching_stats", []))
        if team.get("pitching_stats") and pitching_runs_sum != opponent.get("runs", 0):
            issues.append(
                f"[{label}] 投手陣の失点合計({pitching_runs_sum})と相手チームの最終スコア"
                f"({opponent.get('runs')})が一致しません"
            )

        # 5. 名簿の人数が極端でないか
        roster_size = len(team.get("starters", [])) + len(team.get("bench", []))
        if roster_size == 0:
            issues.append(f"[{label}] 先発・控えが1人も取得できていません")

        # 6. 打撃/投手成績の選手IDが名簿(先発+控え)に含まれているか
        roster_ids = {
            (p["school_id"], p["student_number"])
            for p in team.get("starters", []) + team.get("bench", [])
        }
        for group_name in ("batting_stats", "pitching_stats"):
            for p in team.get(group_name, []):
                pid = (p.get("school_id"), p.get("student_number"))
                if pid not in roster_ids:
                    issues.append(
                        f"[{label}] {group_name}の選手 {p.get('name')}"
                        f"({pid})が先発・控え名簿に見当たりません"
                    )

    return issues


def validate_games(records) -> dict:
    """
    複数試合ぶんをまとめて検証し、
    {"total": N, "clean": M, "with_issues": [(game_hash, issues), ...]}
    の形でサマリを返す。matches.jsonl 全体の健全性チェックに使う想定。
    """
    total = 0
    clean = 0
    with_issues = []
    for record in records:
        total += 1
        issues = validate_game(record)
        if issues:
            with_issues.append((record.get("game_hash"), issues))
        else:
            clean += 1
    return {"total": total, "clean": clean, "with_issues": with_issues}


if __name__ == "__main__":
    import json
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "matches.jsonl"
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    summary = validate_games(records)
    print(f"検証件数: {summary['total']}件 / 問題なし: {summary['clean']}件 "
          f"/ 要確認: {len(summary['with_issues'])}件")
    for game_hash, issues in summary["with_issues"][:20]:
        print(f"\n--- {game_hash} ---")
        for issue in issues:
            print(" ", issue)
