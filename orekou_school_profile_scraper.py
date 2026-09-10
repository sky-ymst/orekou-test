# -*- coding: utf-8 -*-
"""
orekou.net 学校プロフィールページ (/profile/school/{school_id}) の取得・解析ツール。

このページ1回の取得で、以下が同時に手に入る:
1. 学校情報(監督・チーム評価・レート・所在地・代表地区など)
2. 「最近の試合結果」= 直近の練習試合IDリスト(/g/{hash})。実測で約20件、日付降順
3. 「在校生名簿」= その学校に所属する全選手ID一覧(/profile/student/{school_id}/{number})

これにより、
  school_list(47都道府県) → 学校ID一覧
  → 各学校プロフィール(本ツール) → 試合ID・選手ID一覧
  → 試合ページ(orekou_scraper.py) / 選手プロフィール(orekou_student_scraper.py)
という完全自動巡回パイプラインが組める。

注意:
- 「最近の試合結果」は直近分のみ(ページネーションの有無は未確認)。
  試合は対戦した両校のページに載るため、多くの学校を巡回すれば重複込みで
  効率的に試合IDが集まる(重複はセットで自然に除去される)。
- 「在校生名簿」は末尾に "その他XX名、計XX名" と出て一部省略される場合がある
  (このケースは選手個別ページを別途辿るか、名簿ページ自体のページネーションを
  要調査)。
"""

import re
from bs4 import BeautifulSoup

from orekou_http import polite_get, DEFAULT_INTERVAL, DEFAULT_JITTER

BASE_URL = "https://orekou.net"

GAME_LINK_RE = re.compile(r"^/g/([0-9a-f]{32})$")
STUDENT_LINK_RE = re.compile(r"^/profile/student/([0-9a-f]{32})/(\d+)$")


def _clean_text(s: str) -> str:
    """連続する空白を単一の半角スペースに畳み込む(前後は除去)。
    完全除去だと「山田 太郎」のような氏名の区切りが失われるため。"""
    return re.sub(r"\s+", " ", s or "").strip()


def parse_school_profile(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    data = {}

    title_link = soup.select_one("div.title a")
    data["school_name"] = title_link.get_text(strip=True) if title_link else None
    if title_link and title_link.get("href"):
        m = re.search(r"/profile/school/([0-9a-f]+)", title_link["href"])
        if m:
            data["school_id"] = m.group(1)

    basic_tables = soup.select("table.basic")

    # --- 学校情報 ---
    info = {}
    for table in basic_tables:
        header = table.find("th")
        if header and "学校情報" in header.get_text():
            for tr in table.select("tr"):
                cells = tr.find_all("td")
                if len(cells) == 2:
                    key = _clean_text(cells[0].get_text())
                    if key == "監督":
                        info["監督"] = _clean_text(cells[1].get_text())
                    elif key == "監督就任":
                        info["監督就任"] = _clean_text(cells[1].get_text())
                    elif key == "主将":
                        a = cells[1].find("a")
                        if a:
                            m = STUDENT_LINK_RE.match(a["href"])
                            info["主将"] = {
                                "name": a.get_text(strip=True),
                                "student_number": m.group(2) if m else None,
                            }
                    elif key == "所在地":
                        info["所在地"] = cells[1].get_text(strip=True)
                    elif key == "代表地区":
                        info["代表地区"] = cells[1].get_text(strip=True)
                    elif key == "予選区":
                        info["予選区"] = _clean_text(cells[1].get_text())
                    elif key == "チーム評価":
                        spans = cells[1].find_all("span")
                        text = cells[1].get_text()
                        labels = re.findall(r"([投打守走総])", text)
                        grades = [sp.get_text(strip=True) for sp in spans]
                        info["チーム評価"] = dict(zip(labels, grades))
                    elif key == "レート":
                        info["レート"] = _clean_text(cells[1].get_text())
            break
    data["school_info"] = info

    # --- 最近の試合結果 (/g/{hash} リンク一覧) ---
    recent_games = []
    for table in basic_tables:
        header = table.find("th")
        if header and "最近の試合結果" in header.get_text():
            for tr in table.select("tr"):
                cells = tr.find_all("td")
                if len(cells) == 2:
                    date_text = _clean_text(cells[0].get_text())
                    a = cells[1].find("a", href=GAME_LINK_RE)
                    if a:
                        m = GAME_LINK_RE.match(a["href"])
                        recent_games.append({
                            "date": date_text,
                            "game_hash": m.group(1),
                            "summary": a.get_text(strip=True),
                        })
            break
    data["recent_games"] = recent_games

    # --- 監督成績 ---
    manager_record = {}
    for table in basic_tables:
        header = table.find("th")
        if header and "監督成績" in header.get_text():
            for tr in table.select("tr"):
                cells = tr.find_all("td")
                if len(cells) == 2:
                    key = _clean_text(cells[0].get_text())
                    val = _clean_text(cells[1].get_text())
                    manager_record[key] = val
            break
    data["manager_record"] = manager_record

    # --- 在校生名簿 ---
    roster = []
    roster_truncated_note = None
    for table in basic_tables:
        header = table.find("th")
        if header and "在校生名簿" in header.get_text():
            for tr in table.select("tr"):
                cells = tr.find_all("td")
                if len(cells) == 4:
                    grade = _clean_text(cells[0].get_text())
                    rarity_span = cells[1].find("span")
                    rarity = rarity_span.get_text(strip=True) if rarity_span else None
                    a = cells[2].find("a", href=STUDENT_LINK_RE)
                    status = _clean_text(cells[3].get_text())
                    if a:
                        m = STUDENT_LINK_RE.match(a["href"])
                        roster.append({
                            "grade": grade,
                            "rarity": rarity,
                            "name": a.get_text(strip=True),
                            "student_number": m.group(2),
                            "status": status,
                        })
                else:
                    # "その他XX名、計XX名" のような省略行
                    th = tr.find("th")
                    if th and "その他" in th.get_text():
                        roster_truncated_note = th.get_text(strip=True)
            break
    data["roster"] = roster
    data["roster_truncated_note"] = roster_truncated_note

    return data


def fetch_school_profile(school_id: str,
                          interval: float = DEFAULT_INTERVAL,
                          jitter: float = DEFAULT_JITTER,
                          use_cache: bool = True) -> dict:
    url = f"{BASE_URL}/profile/school/{school_id}"
    html = polite_get(url, interval=interval, jitter=jitter, use_cache=use_cache)

    result = parse_school_profile(html)
    result["url"] = url
    return result


if __name__ == "__main__":
    import json
    with open("/home/claude/sample_school_profile.html", encoding="utf-8") as f:
        html = f.read()
    result = parse_school_profile(html)
    print(json.dumps(result, ensure_ascii=False, indent=2))
