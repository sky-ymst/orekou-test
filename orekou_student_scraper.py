# -*- coding: utf-8 -*-
"""
orekou.net 選手プロフィールページ (/profile/student/{school_id}/{student_id}) の
取得・解析ツール。

- fetch_student_profile(): レート制限(デフォルト5秒+ランダムジッター)とキャッシュ付きで
  プロフィールページを取得
- parse_student_profile(): HTMLから現在能力・推定潜在能力・各種成績を構造化データに変換

orekou_scraper.py (練習試合結果ページ用) と組み合わせて使う想定。
1万試合分の試合データ収集と合わせて、両チーム計最大36人×試合数ぶんの選手ID
(school_id, student_number) が集まるので、重複を除いた選手のみプロフィールを
取得すれば効率的。
"""

import re
import json
from bs4 import BeautifulSoup

from orekou_http import polite_get, DEFAULT_INTERVAL, DEFAULT_JITTER

BASE_URL = "https://orekou.net"

_cache = {}  # (school_id, student_number) -> parsed dict (プロセス内の高速な追加キャッシュ。
             # ディスクキャッシュは orekou_http.polite_get 側が担当)


# ---------------------------------------------------------------------------
# 取得 (fetch)
# ---------------------------------------------------------------------------

def fetch_student_profile(school_id: str, student_number: str,
                           interval: float = DEFAULT_INTERVAL,
                           jitter: float = DEFAULT_JITTER,
                           use_cache: bool = True) -> dict:
    """
    選手プロフィールページを1回のGETリクエストで取得し、解析結果を返す。
    同一選手は再取得せずキャッシュ(プロセス内メモリ + ディスク)を返す。
    """
    key = (school_id, student_number)
    if key in _cache:
        return _cache[key]

    url = f"{BASE_URL}/profile/student/{school_id}/{student_number}"
    html = polite_get(url, interval=interval, jitter=jitter, use_cache=use_cache)

    result = parse_student_profile(html)
    result["url"] = url
    _cache[key] = result
    return result


# ---------------------------------------------------------------------------
# 解析 (parse)
# ---------------------------------------------------------------------------

RECORD_TABLE_LABELS = {
    "record_type_1_b": "公式戦_打撃",
    "record_type_1_p": "公式戦_投手",
    "record_type_2_b": "練習試合_打撃",
    "record_type_2_p": "練習試合_投手",
    "record_type_3_b": "春夏甲子園_打撃",
    "record_type_3_p": "春夏甲子園_投手",
}


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _parse_stat_cell(text: str):
    """
    '<等級><実数値>(+成長分)' / '球速表記(+成長分)' / 等級なし数値のみ、
    を (grade, value, growth) に分解する。該当なしはNone。
    """
    text = _clean_text(text)

    m = re.match(r"^(\d+)km/h(?:\((\+?-?\d+)\))?$", text)
    if m:
        return None, int(m.group(1)), (int(m.group(2)) if m.group(2) else None)

    m = re.match(r"^([A-Z])(\d+)(?:\((\+?-?\d+)\))?$", text)
    if m:
        grade, value, growth = m.group(1), int(m.group(2)), m.group(3)
        return grade, value, (int(growth) if growth is not None else None)

    m = re.match(r"^(\d+)$", text)
    if m:
        return None, int(m.group(1)), None

    return None, text, None


def parse_student_profile(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    data = {}

    title_link = soup.select_one("div.title a")
    data["student_name"] = title_link.get_text(strip=True) if title_link else None
    if title_link and title_link.get("href"):
        m = re.search(r"/profile/student/([0-9a-f]+)/(\d+)", title_link["href"])
        if m:
            data["school_id"] = m.group(1)
            data["student_number"] = m.group(2)

    basic_tables = soup.select("table.basic")

    school_link = soup.select_one("table.basic a[href*='/profile/school/']")
    if school_link:
        data["school_name"] = school_link.get_text(strip=True)

    detail = {}
    for table in basic_tables:
        header = table.find("th")
        if header and "選手詳細" in header.get_text():
            rarity_span = table.select_one("span[class^='rarity_']")
            if rarity_span:
                detail["rarity"] = rarity_span.get_text(strip=True)

            grade_spans = table.select("td div[style*='font-weight:normal'] span")
            pos_labels = ["投", "打", "走", "守"]
            pos_aptitude = {}
            if grade_spans:
                for lbl, sp in zip(pos_labels, grade_spans):
                    pos_aptitude[lbl] = sp.get_text(strip=True)
            detail["position_aptitude"] = pos_aptitude

            # --- ケガ情報 (div.injury: 例 "肩の痛み-完治:09月11日") ---
            injuries = []
            for injury_div in table.select("div.injury"):
                text = injury_div.get_text(strip=True)
                m = re.match(r"^(.+?)-完治:(\d{2}月\d{2}日)$", text)
                if m:
                    injuries.append({
                        "description": m.group(1),   # 例: "肩の痛み"
                        "recovery_date": m.group(2),  # 例: "09月11日"
                        "raw_text": text,
                    })
                else:
                    injuries.append({"description": None, "recovery_date": None, "raw_text": text})
            detail["injuries"] = injuries

            for tr in table.select("tr"):
                cells = tr.find_all("td")
                if len(cells) == 2:
                    key = _clean_text(cells[0].get_text())
                    val = _clean_text(cells[1].get_text())
                    if key in ("扱い", "コスト", "学年", "利き手", "打席",
                               "投法", "身長", "体重", "Lv", "Number"):
                        detail[key] = val
                    elif key == "覚醒":
                        detail["覚醒レベル"] = cells[1].get_text().count("★")
                    elif key == "人望":
                        grade, value, _ = _parse_stat_cell(cells[1].get_text())
                        detail["人望"] = {"grade": grade, "value": value}
            break
    data["detail"] = detail

    current_batter, current_pitcher, potential = {}, {}, {}
    for table in basic_tables:
        header = table.find("th")
        if not header:
            continue
        header_text = header.get_text()

        if "野手能力" in header_text:
            for tr in table.select("tr")[1:]:
                cells = tr.find_all("td")
                if len(cells) == 2:
                    key = _clean_text(cells[0].get_text())
                    grade, value, growth = _parse_stat_cell(cells[1].get_text())
                    current_batter[key] = {"grade": grade, "value": value, "growth": growth}

        elif "投手能力" in header_text:
            for tr in table.select("tr")[1:]:
                cells = tr.find_all("td")
                if len(cells) == 2:
                    key = _clean_text(cells[0].get_text())
                    grade, value, growth = _parse_stat_cell(cells[1].get_text())
                    current_pitcher[key] = {"grade": grade, "value": value, "growth": growth}

        elif "推定潜在能力" in header_text:
            for tr in table.select("tr")[1:]:
                cells = tr.find_all("td")
                if len(cells) == 2:
                    key = _clean_text(cells[0].get_text())
                    grade, value, _ = _parse_stat_cell(cells[1].get_text())
                    potential[key] = {"grade": grade, "value": value}

    data["current_batter_stats"] = current_batter
    data["current_pitcher_stats"] = current_pitcher
    data["potential_stats"] = potential

    records = {}
    for prefix, label in RECORD_TABLE_LABELS.items():
        table = soup.find("table", id=re.compile(rf"^{prefix}_\d+$"))
        if not table:
            continue
        record = {}
        for tr in table.select("tr"):
            cells = tr.find_all("td")
            if len(cells) == 2:
                key = _clean_text(cells[0].get_text())
                val = _clean_text(cells[1].get_text())
                if re.match(r"^-?\d+\.\d+$", val):
                    val = float(val)
                elif re.match(r"^-?\d+$", val):
                    val = int(val)
                record[key] = val
        records[label] = record
    data["records"] = records

    return data


if __name__ == "__main__":
    # ローカルの保存済みHTMLでテストする例
    with open("sample_profile.html", encoding="utf-8") as f:
        html = f.read()
    result = parse_student_profile(html)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # 実際にネットワーク越しに取得する場合の例(コメントアウト):
    # profile = fetch_student_profile(
    #     "6a56c2a5f08a538bc7ce6fdcac957d06", "20250139"
    # )
    # print(json.dumps(profile, ensure_ascii=False, indent=2))
