# -*- coding: utf-8 -*-
"""
orekou.net 試合結果ページ (/g/{32桁ハッシュ}) の取得・解析ツール。

1回のリクエストで下記すべてが取得できる(AJAX追加リクエスト不要、JS実行不要):
- 対戦カード・スコア・イニング経過・H(安打数)・E(失策数)
- 両チームの先発メンバー + 控え選手(display:noneで存在)
- 両チームの打撃成績・投手成績
- 選手の成長データ(該当試合で成長した選手のみ)
- プレー経過ログ

★このツールの主目的: 「所属選手一覧」は学校プロフィールページだと上位20件しか
  出ないため使えない。代わりに試合ページに登場する選手(先発+控え)の
  (school_id, student_number) を全部拾うことで、選手IDを網羅的に発見する。
  試合を集めれば集めるほど、より多くの選手IDが集まる設計。
"""

import re
from bs4 import BeautifulSoup

from orekou_http import polite_get, DEFAULT_INTERVAL, DEFAULT_JITTER

BASE_URL = "https://orekou.net"

STUDENT_LINK_RE = re.compile(r"^/profile/student/([0-9a-f]{32})/(\d+)$")
SCHOOL_LINK_RE = re.compile(r"^/profile/school/([0-9a-f]{32})$")
SCOREBOARD_ASSIGN_RE = re.compile(
    r"\$\('(score_(?:top|bot)_[the])'\)\.innerHTML='([^']*)'"
)

# 公式戦/練習試合の判定用。プロキシ経由で閲覧した場合
# (例: /proxy/https://orekou.net/match/tournament/...) のように前置詞が付く
# ケースがあるため、行頭一致ではなく「文字列に含まれるか」で判定する。
OFFICIAL_MATCH_MARK = "/match/tournament/"
PRACTICE_MATCH_MARK = "/match/game_logs/"


def _parse_innings_pitched(text: str):
    """
    「2 1/3」のような投球回表記を10進数(float)に変換する。
    (2 1/3 イニング = 2 + 1/3 = 2.333...)
    「3」のように端数が無い場合はそのまま float(3.0) を返す。
    パース不能な場合は None。
    """
    text = text.strip()
    m = re.match(r"^(\d+)\s+(\d+)/(\d+)$", text)
    if m:
        whole, num, den = (int(x) for x in m.groups())
        return whole + num / den
    m = re.match(r"^(\d+)/(\d+)$", text)
    if m:
        num, den = (int(x) for x in m.groups())
        return num / den
    m = re.match(r"^\d+(\.\d+)?$", text)
    if m:
        return float(text)
    return None


def _clean_text(s: str) -> str:
    """連続する空白を単一の半角スペースに畳み込む(前後は除去)。
    「2 1/3」のような投球回表記で、数字と分数の間の区切りを保つために
    完全除去ではなく単一スペースへの畳み込みにしている。"""
    return re.sub(r"\s+", " ", s or "").strip()


def _extract_final_scoreboard(soup: BeautifulSoup) -> dict:
    """
    <script>内の setTimeout(...) 呼び出しをテキストとして正規表現で読み取り、
    最終的なスコア(T)・安打数(H)・失策数(E)を求める。
    同じidへの代入が何度も出てくるが、出現順=時系列順なので最後の値が最終値。
    JS実行は不要。
    """
    script_text = "\n".join(s.get_text() for s in soup.find_all("script"))
    values = {}
    for key, val in SCOREBOARD_ASSIGN_RE.findall(script_text):
        values[key] = val  # 後勝ち(=時系列で最後の代入が残る)
    return {
        "top_runs": int(values.get("score_top_t", 0)),
        "top_hits": int(values.get("score_top_h", 0)),
        "top_errors": int(values.get("score_top_e", 0)),
        "bot_runs": int(values.get("score_bot_t", 0)),
        "bot_hits": int(values.get("score_bot_h", 0)),
        "bot_errors": int(values.get("score_bot_e", 0)),
    }


def _extract_innings(soup: BeautifulSoup, suffix: str) -> list:
    """suffix: 'top' or 'bot'。inning_N_{suffix} の値をリストで返す。"""
    innings = []
    i = 1
    while True:
        span = soup.find("span", id=f"inning_{i}_{suffix}")
        if not span:
            break
        innings.append(_clean_text(span.get_text()))
        i += 1
    return innings


def _parse_player_link(a_tag) -> dict:
    m = STUDENT_LINK_RE.match(a_tag["href"]) if a_tag else None
    if not m:
        return None
    return {
        "school_id": m.group(1),
        "student_number": m.group(2),
        "name": a_tag.get_text(strip=True),
    }


def _parse_lineup_table(table, warnings: list = None) -> dict:
    """
    先発+控えの一覧テーブルを解析。
    先発: 打順・守備位置・調子・才・選手・投・打
    控え: display:none行。打順/守備位置は"-"。

    列数が7以外(想定と違うレイアウト)でも、選手リンクの位置さえ特定できれば
    可能な範囲で抽出を続ける。完全に読めない行はスキップしつつ warnings に記録する。
    """
    starters, bench = [], []
    if table is None:
        if warnings is not None:
            warnings.append("lineup_table_missing")
        return {"starters": starters, "bench": bench}

    rows = table.find_all("tr")
    in_bench = False
    for tr in rows:
        if tr.find("th"):
            continue
        # 「+ベンチ」の行(colspan=7)を境に切り替え。colspanの具体的な数値が
        # 将来変わっても検知できるよう、「th以外のセルが1つだけの行」も
        # 区切り行の候補として扱う。
        cells = tr.find_all("td")
        if len(cells) == 1 and ("ベンチ" in cells[0].get_text() or cells[0].get("colspan")):
            in_bench = True
            continue

        if len(cells) < 5:
            # 想定より列が少ない行は解析不能。選手リンクの有無だけ確認し、
            # あれば警告付きで記録、無ければ単なる区切り/空行として静かにスキップ。
            if warnings is not None and tr.find("a", href=STUDENT_LINK_RE):
                warnings.append(f"lineup_row_too_few_cells:{len(cells)}")
            continue

        # 選手名セル(リンクを含むセル)を探す。列構成が7列と違う場合に備え、
        # 固定インデックスではなくリンクの位置から特定する。
        name_cell = None
        name_idx = None
        for idx, c in enumerate(cells):
            if c.find("a", href=STUDENT_LINK_RE):
                name_cell = c
                name_idx = idx
                break

        if name_cell is None:
            continue

        a = name_cell.find("a", href=STUDENT_LINK_RE)
        player = _parse_player_link(a)
        if not player:
            continue

        # 想定の7列レイアウト(打順・守備位置・調子・才・選手・投・打)であれば
        # 通常どおり各項目を埋める。ずれている場合は取得できる範囲のみ埋め、
        # 欠けた項目は None のままにして warnings に記録する。
        if name_idx == 4 and len(cells) >= 7:
            order, pos_cell, cond_cell, rarity_cell = cells[0], cells[1], cells[2], cells[3]
            throw_cell, bat_cell = cells[5], cells[6]
            pos_tag = pos_cell.find("b")
            cond_img = cond_cell.find("img")
            rarity_span = rarity_cell.find("span")
            entry = {
                **player,
                "batting_order": _clean_text(order.get_text()) if not in_bench else None,
                "position": pos_tag.get_text(strip=True) if pos_tag else None,
                "condition": cond_img.get("alt") if cond_img else None,
                "rarity": rarity_span.get_text(strip=True) if rarity_span else None,
                "throws": _clean_text(throw_cell.get_text()),
                "bats": _clean_text(bat_cell.get_text()),
            }
        else:
            if warnings is not None:
                warnings.append(f"lineup_row_unexpected_layout:name_idx={name_idx},cells={len(cells)}")
            entry = {
                **player,
                "batting_order": None,
                "position": None,
                "condition": None,
                "rarity": None,
                "throws": None,
                "bats": None,
            }
        (bench if in_bench else starters).append(entry)

    return {"starters": starters, "bench": bench}


def _parse_stat_table(table, stat_keys, warnings: list = None) -> list:
    """打撃成績/投手成績テーブル(1行=1選手)を解析。
    テーブルが存在しない/空/ヘッダーだけ、といったケースでもクラッシュしない。"""
    records = []
    if table is None:
        return records

    header_row = table.find("tr")
    if header_row is None:
        if warnings is not None:
            warnings.append("stat_table_no_header_row")
        return records

    header_cells = [th.get_text(strip=True) for th in header_row.find_all("th")]
    if not header_cells:
        if warnings is not None:
            warnings.append("stat_table_no_th_in_header_row")
        return records

    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all("td")
        if not cells:
            continue
        if len(cells) != len(header_cells):
            # 列数がヘッダーとずれている行はスキップするが、選手リンクが
            # 含まれていた場合は取りこぼしとして記録しておく。
            if warnings is not None and tr.find("a", href=STUDENT_LINK_RE):
                warnings.append(
                    f"stat_row_column_mismatch:expected={len(header_cells)},got={len(cells)}"
                )
            continue
        a = cells[0].find("a", href=STUDENT_LINK_RE)
        player = _parse_player_link(a)
        if not player:
            continue
        record = dict(player)
        for key, cell in zip(header_cells[1:], cells[1:]):
            val = _clean_text(cell.get_text())
            record[key] = val
        records.append(record)
    return records


def parse_game_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    data = {}
    warnings = []

    # --- 基本情報 ---
    date_div = soup.select_one("div.content > div")
    data["date"] = None
    for div in soup.select("div.content div"):
        text = div.get_text(strip=True)
        if re.match(r"^\d{4}年\d{2}月\d{2}日$", text):
            data["date"] = text
            break
    if data["date"] is None:
        warnings.append("date_not_found")

    match_type_a = soup.find("a", href=lambda h: h and (
        OFFICIAL_MATCH_MARK in h or PRACTICE_MATCH_MARK in h))
    data["match_type_label"] = match_type_a.get_text(strip=True) if match_type_a else None
    data["match_type_url"] = match_type_a["href"] if match_type_a else None
    data["is_official"] = bool(match_type_a and OFFICIAL_MATCH_MARK in match_type_a["href"])
    if match_type_a is None:
        warnings.append("match_type_not_found")

    # --- 対戦校 (school_id, name) ---
    school_links = soup.find_all("a", href=SCHOOL_LINK_RE)
    teams_meta = []
    seen_school_ids = set()
    for a in school_links:
        m = SCHOOL_LINK_RE.match(a["href"])
        sid = m.group(1)
        if sid in seen_school_ids:
            continue
        seen_school_ids.add(sid)
        teams_meta.append({"school_id": sid, "school_name": a.get_text(strip=True)})
        if len(teams_meta) == 2:
            break
    if len(teams_meta) < 2:
        warnings.append(f"teams_meta_incomplete:found={len(teams_meta)}")

    # --- スコアボード ---
    scoreboard = _extract_final_scoreboard(soup)
    innings_top = _extract_innings(soup, "top")
    innings_bot = _extract_innings(soup, "bot")

    basic_tables = soup.select("table.basic")
    if not basic_tables:
        warnings.append("no_basic_tables_found")

    # 打線表は th に「打」「守」「調」...を含むテーブルとして識別する。
    # 完全一致だと列順や表記の微差で1つも拾えなくなるため、まず主要ラベルの
    # 部分一致で判定する(順序に依存しない)。それでも見つからない場合は、
    # 「選手」列と守備位置っぽい列を含むテーブルを緩い条件でフォールバック識別する。
    LINEUP_HEADER_REQUIRED = {"選手"}
    LINEUP_HEADER_HINTS = {"打", "守", "調", "才"}

    lineup_tables = []
    for t in basic_tables:
        th_texts = {th.get_text(strip=True) for th in t.find_all("th")}
        if LINEUP_HEADER_REQUIRED <= th_texts and len(th_texts & LINEUP_HEADER_HINTS) >= 2:
            lineup_tables.append(t)

    if len(lineup_tables) < 2:
        warnings.append(f"lineup_tables_found:{len(lineup_tables)} (expected 2)")

    # 打撃成績/投手成績テーブルはIDで直接特定できる
    def find_by_id(id_):
        el = soup.find(id=id_)
        if el is None:
            return None
        found = el.find("table", class_="basic")
        return found

    teams = []
    suffixes = ["top", "bot"]
    for i, suffix in enumerate(suffixes):
        meta = teams_meta[i] if i < len(teams_meta) else {}
        if not meta:
            warnings.append(f"team_meta_missing_for_side:{suffix}")
        lineup_table = lineup_tables[i] if i < len(lineup_tables) else None
        lineup = _parse_lineup_table(lineup_table, warnings=warnings)

        batting_table = find_by_id(f"f_record_area_{suffix}")
        pitching_table = find_by_id(f"p_record_area_{suffix}")
        if batting_table is None:
            warnings.append(f"batting_table_missing:{suffix}")
        if pitching_table is None:
            warnings.append(f"pitching_table_missing:{suffix}")

        batting_stats = _parse_stat_table(
            batting_table,
            ["打席", "打数", "安打", "本塁打", "打点", "得点", "犠飛", "犠打", "四球", "死球", "三振", "盗塁"],
            warnings=warnings,
        )
        pitching_stats = _parse_stat_table(
            pitching_table,
            ["球数", "投球回", "失点", "自責点", "防御率", "与死球", "与四球", "被安打", "被本塁打", "奪三振"],
            warnings=warnings,
        )

        # 投球回(例: "2 1/3")を10進数に変換する。元表記も "投球回_表記" として残す。
        for rec in pitching_stats:
            raw = rec.get("投球回")
            rec["投球回_表記"] = raw
            if raw:
                parsed = _parse_innings_pitched(raw)
                if parsed is None:
                    warnings.append(f"innings_pitched_unparsable:{raw!r}")
                rec["投球回"] = parsed
            else:
                rec["投球回"] = None

        teams.append({
            "side": suffix,  # "top"=先攻(通常アウェイ), "bot"=後攻(通常ホーム)
            "school_id": meta.get("school_id"),
            "school_name": meta.get("school_name"),
            "runs": scoreboard[f"{suffix}_runs"],
            "hits": scoreboard[f"{suffix}_hits"],
            "errors": scoreboard[f"{suffix}_errors"],
            "innings": innings_top if suffix == "top" else innings_bot,
            "starters": lineup["starters"],
            "bench": lineup["bench"],
            "batting_stats": batting_stats,
            "pitching_stats": pitching_stats,
        })

    data["teams"] = teams

    # --- 選手ID一覧(このツールの主目的: 重複排除込みで収集) ---
    player_ids = set()
    for team in teams:
        for group in (team["starters"], team["bench"], team["batting_stats"],
                      team["pitching_stats"]):
            for p in group:
                player_ids.add((p["school_id"], p["student_number"]))
    data["player_ids"] = sorted(player_ids)
    data["player_count"] = len(player_ids)

    # --- 簡易な内部整合性チェック(パース時点で分かる範囲) ---
    # 先発が極端に少ない(通常は9人)場合は、打線表の読み取り漏れの可能性が高い。
    for team in teams:
        starter_count = len(team["starters"])
        if starter_count not in (0, 9) and team["starters"] is not None:
            # 0人は「打線表自体が見つからなかった」ケースで別途 lineup_tables_found
            # 警告が出ているはずなので、ここでは主に「見つかったが人数がおかしい」を拾う
            if starter_count > 0:
                warnings.append(
                    f"unexpected_starter_count:side={team['side']},count={starter_count}"
                )

    data["parse_warnings"] = warnings

    return data


def fetch_game_page(game_hash: str,
                     interval: float = DEFAULT_INTERVAL,
                     jitter: float = DEFAULT_JITTER,
                     use_cache: bool = True) -> dict:
    url = f"{BASE_URL}/g/{game_hash}"
    html = polite_get(url, interval=interval, jitter=jitter, use_cache=use_cache)

    result = parse_game_page(html)
    result["url"] = url
    result["game_hash"] = game_hash
    return result


if __name__ == "__main__":
    import json
    with open("/home/claude/sample_game.html", encoding="utf-8") as f:
        html = f.read()
    result = parse_game_page(html)
    print(json.dumps(result, ensure_ascii=False, indent=2))
