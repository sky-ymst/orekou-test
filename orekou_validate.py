# -*- coding: utf-8 -*-
"""
orekou_scraper.parse_game_page() が返す試合レコード(matches.jsonl の1行)の
データ整合性を検証するモジュール。

parse_game_page() 自体は「パース中に何が起きたか」(parse_warnings)を記録するが、
本モジュールは1試合分のレコードが出揃った後に、フィールド間の突き合わせによって
「パースは成功したように見えるが、値として矛盾している」ケースを検出する。
両者は役割が異なる:
  - parse_warnings: パーサーが構造を読み取れなかった/想定外だった箇所
  - validate_game_record(): 読み取れた値同士が食い違っている箇所

チェック項目:
  1. スコア一致: イニングごとの得点合計 == 最終スコア(top_runs / bot_runs)
  2. 打者数の整合性: 打線表の人数(先発+控え) と 打撃成績テーブルの行数の関係
     (打撃成績に載るのは出場した選手のみなので、打撃成績人数 <= 先発+控え人数、
      かつ最低でも先発9人分は基本的に存在するはず)
  3. 選手IDの突き合わせ: 打線表(starters/bench)に登場する選手と、
     打撃成績・投手成績テーブルに登場する選手の (school_id, student_number) が
     一方にしか無いケースがないか(あれば「出場記録はあるのにスタメン表に居ない」
     等の食い違いとして警告)
  4. 投球回の合計: そのチームの投手成績の投球回合計が、試合イニング数と
     大きくズレていないか(継投を考慮し、厳密一致は求めずレンジチェックのみ)

本モジュールは「間違っている」と断定はしない(HTML構造の解釈自体が推測ベースの
部分があるため)。矛盾を発見したら validation_warnings に事実ベースで記録し、
後で人間・統計的な確認に回せるようにすることを目的とする。
"""

import argparse
import json
import sys


# ---------------------------------------------------------------------------
# 個別チェック
# ---------------------------------------------------------------------------

def check_score_matches_innings(team: dict) -> list:
    """イニングごとの得点合計が最終スコアと一致するかを確認する。
    引き分け・再試合中断など、イニング表記に "X"(まだ無得点)や空文字が
    混じる場合があるため、数値化できないイニングは無視してチェックを続行する。
    """
    warnings = []
    innings = team.get("innings") or []
    total = 0
    unparsable = []
    for inning_val in innings:
        text = (inning_val or "").strip()
        if text == "" or text == "-":
            continue
        try:
            total += int(text)
        except ValueError:
            unparsable.append(text)

    if unparsable:
        warnings.append(
            f"score_check_skipped_unparsable_innings:side={team.get('side')},values={unparsable}"
        )
        return warnings  # 一部でもパースできないイニングがあれば、合計値は信頼できないため比較自体をスキップ

    final_runs = team.get("runs")
    if isinstance(final_runs, int) and total != final_runs:
        warnings.append(
            f"score_mismatch:side={team.get('side')},innings_total={total},final_runs={final_runs}"
        )
    return warnings


def check_batter_counts(team: dict) -> list:
    """打撃成績に載っている選手が、打線表(先発+控え)の範囲内に収まっているか確認する。"""
    warnings = []
    lineup_ids = {
        (p.get("school_id"), p.get("student_number"))
        for p in (team.get("starters") or []) + (team.get("bench") or [])
    }
    batting_ids = {
        (p.get("school_id"), p.get("student_number"))
        for p in (team.get("batting_stats") or [])
    }

    if not lineup_ids and not batting_ids:
        return warnings  # 両方空 = 打線表・成績表とも取得できていない(別途パース側の警告で検知済み)

    extra_in_batting = batting_ids - lineup_ids
    if extra_in_batting:
        warnings.append(
            f"batting_stats_players_not_in_lineup:side={team.get('side')},"
            f"count={len(extra_in_batting)}"
        )

    starters = team.get("starters") or []
    if starters and len(starters) != 9:
        warnings.append(
            f"starter_count_not_nine:side={team.get('side')},count={len(starters)}"
        )
    return warnings


def check_pitcher_player_ids(team: dict) -> list:
    """投手成績に登場する選手IDが、打線表(先発+控え、投手はベンチ扱いのこともある)
    に含まれているかを確認する。"""
    warnings = []
    lineup_ids = {
        (p.get("school_id"), p.get("student_number"))
        for p in (team.get("starters") or []) + (team.get("bench") or [])
    }
    pitching_ids = {
        (p.get("school_id"), p.get("student_number"))
        for p in (team.get("pitching_stats") or [])
    }

    if not lineup_ids and not pitching_ids:
        return warnings

    extra_in_pitching = pitching_ids - lineup_ids
    if extra_in_pitching:
        warnings.append(
            f"pitching_stats_players_not_in_lineup:side={team.get('side')},"
            f"count={len(extra_in_pitching)}"
        )
    return warnings


def check_innings_pitched_total(team: dict, game_innings_count: int) -> list:
    """
    投手成績の投球回合計が、実際の試合イニング数とかけ離れていないか確認する。
    継投・延長・コールドなどで厳密な一致は期待できないため、
    「0イニングなのに得点が入っている」のような明らかな異常のみ拾う
    緩いレンジチェックにとどめる。
    """
    warnings = []
    pitching_stats = team.get("pitching_stats") or []
    if not pitching_stats or not game_innings_count:
        return warnings

    total_ip = 0.0
    has_unparsed = False
    for rec in pitching_stats:
        ip = rec.get("投球回")
        if isinstance(ip, (int, float)):
            total_ip += ip
        else:
            has_unparsed = True

    if has_unparsed:
        warnings.append(f"innings_pitched_total_skipped_unparsed:side={team.get('side')}")
        return warnings

    # 相手チームが投げ切ったイニング数が、このチームの攻撃回数の目安になる。
    # 大きく外れている(半分未満、または2倍超)場合のみ警告する程度の粗いチェック。
    if total_ip <= 0:
        warnings.append(f"innings_pitched_total_zero:side={team.get('side')}")
    elif total_ip < game_innings_count * 0.5 or total_ip > game_innings_count * 2:
        warnings.append(
            f"innings_pitched_total_out_of_range:side={team.get('side')},"
            f"total_ip={total_ip},game_innings={game_innings_count}"
        )
    return warnings


# ---------------------------------------------------------------------------
# 1試合分の統合チェック
# ---------------------------------------------------------------------------

def validate_game_record(record: dict) -> list:
    """
    1試合分のレコード(parse_game_page() の戻り値と同じ構造)を検証し、
    発見した矛盾のリスト(文字列)を返す。矛盾が無ければ空リスト。
    """
    warnings = []
    teams = record.get("teams") or []

    if len(teams) != 2:
        warnings.append(f"team_count_not_two:{len(teams)}")
        return warnings  # これ以上の突き合わせは前提が崩れているため打ち切る

    # 試合の総イニング数(表側のイニング配列の長さを基準とする)
    game_innings_count = len(teams[0].get("innings") or [])

    for team in teams:
        warnings += check_score_matches_innings(team)
        warnings += check_batter_counts(team)
        warnings += check_pitcher_player_ids(team)
        warnings += check_innings_pitched_total(team, game_innings_count)

    # 両チームの player_ids と record["player_ids"] の整合性
    recomputed = set()
    for team in teams:
        for group in (team.get("starters") or [], team.get("bench") or [],
                      team.get("batting_stats") or [], team.get("pitching_stats") or []):
            for p in group:
                recomputed.add((p.get("school_id"), p.get("student_number")))
    recorded = {tuple(x) for x in (record.get("player_ids") or [])}
    if recomputed != recorded:
        warnings.append(
            f"player_ids_field_out_of_sync:recomputed={len(recomputed)},recorded={len(recorded)}"
        )

    return warnings


# ---------------------------------------------------------------------------
# ファイル単位のバッチ検証
# ---------------------------------------------------------------------------

def validate_matches_file(path: str, max_print: int = 50) -> dict:
    """
    matches.jsonl を1行ずつ読み、各試合に validate_game_record() を適用する。
    集計結果(件数・警告種別ごとの出現回数)と、個別レコードの警告一覧を返す。
    ファイル自体は読み取り専用で扱う。
    """
    total = 0
    records_with_warnings = 0
    warning_type_counts = {}
    per_record_warnings = []

    with open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError as e:
                per_record_warnings.append({
                    "line": line_no,
                    "game_hash": None,
                    "warnings": [f"json_decode_error:{e}"],
                })
                warning_type_counts["json_decode_error"] = warning_type_counts.get("json_decode_error", 0) + 1
                records_with_warnings += 1
                continue

            warnings = validate_game_record(record)
            # parse_game_page() 自身が記録した parse_warnings も合わせて集計対象にする
            warnings = list(record.get("parse_warnings") or []) + warnings

            if warnings:
                records_with_warnings += 1
                per_record_warnings.append({
                    "line": line_no,
                    "game_hash": record.get("game_hash"),
                    "warnings": warnings,
                })
                for w in warnings:
                    key = w.split(":")[0]
                    warning_type_counts[key] = warning_type_counts.get(key, 0) + 1

    summary = {
        "total_records": total,
        "records_with_warnings": records_with_warnings,
        "clean_records": total - records_with_warnings,
        "warning_type_counts": dict(
            sorted(warning_type_counts.items(), key=lambda kv: -kv[1])
        ),
    }

    return {
        "summary": summary,
        "details": per_record_warnings[:max_print],
        "details_truncated": len(per_record_warnings) > max_print,
    }


def main():
    parser = argparse.ArgumentParser(
        description="orekou.net matches.jsonl のデータ整合性チェック"
    )
    parser.add_argument("matches_path", help="検証対象の matches.jsonl のパス")
    parser.add_argument("--max-print", type=int, default=50,
                         help="詳細を表示する最大件数(既定50件)")
    parser.add_argument("--json", action="store_true",
                         help="結果をJSONとして出力する(既定は人間向けテキスト)")
    args = parser.parse_args()

    result = validate_matches_file(args.matches_path, max_print=args.max_print)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    s = result["summary"]
    print(f"検証対象: {args.matches_path}")
    print(f"総レコード数: {s['total_records']}")
    print(f"警告なし: {s['clean_records']}")
    print(f"警告あり: {s['records_with_warnings']}")
    if s["total_records"]:
        rate = s["records_with_warnings"] / s["total_records"] * 100
        print(f"警告あり率: {rate:.1f}%")

    if s["warning_type_counts"]:
        print("\n警告の種類別件数:")
        for key, count in s["warning_type_counts"].items():
            print(f"  {key}: {count}")

    if result["details"]:
        print(f"\n詳細(先頭{len(result['details'])}件" +
              ("、以降省略" if result["details_truncated"] else "") + "):")
        for d in result["details"]:
            gh = d["game_hash"] or "?"
            print(f"  L{d['line']} game_hash={gh[:12]}...")
            for w in d["warnings"]:
                print(f"      - {w}")

    if s["records_with_warnings"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
