# -*- coding: utf-8 -*-
"""
orekou.net 学校一覧ページ (/profile/school_list/{1〜47}) の取得・解析ツール。

- 都道府県ごとに /profile/school_list/{1〜47} が存在すると推測される
  (サンプルでは 28 = 京都)。
- 1ページで「最近活動のあった順に300校まで」を表示。300校を超える都道府県では
  非アクティブな学校が漏れる点に注意(ただし練習試合データ収集の目的では
  「最近活動している学校」が優先的に取れるので、むしろ都合が良い)。
- 各校は /profile/school/{32桁ハッシュ} へのリンクを持つ。このハッシュが
  学校の一意なID(school_id)で、選手プロフィールURL
  (/profile/student/{school_id}/{student_number}) にも使われる。
"""

import re
from bs4 import BeautifulSoup

from orekou_http import polite_get, DEFAULT_INTERVAL, DEFAULT_JITTER

BASE_URL = "https://orekou.net"

SCHOOL_LINK_RE = re.compile(r"^/profile/school/([0-9a-f]{32})$")


def parse_school_list(html: str) -> dict:
    """
    school_list ページのHTMLから
    - 都道府県/地区名
    - 学校一覧 [{"school_id":..., "school_name":...}, ...]
    - 300校上限に達しているか(注記の有無)
    を抽出する。
    """
    soup = BeautifulSoup(html, "html.parser")
    result = {}

    # 都道府県名 (title2内のリンクテキスト)
    pref_link = soup.select_one("div.title2 a")
    result["prefecture"] = pref_link.get_text(strip=True) if pref_link else None

    schools = []
    seen = set()
    for a in soup.find_all("a", href=SCHOOL_LINK_RE):
        m = SCHOOL_LINK_RE.match(a["href"])
        school_id = m.group(1)
        school_name = a.get_text(strip=True)
        if school_id not in seen:
            seen.add(school_id)
            schools.append({"school_id": school_id, "school_name": school_name})

    result["schools"] = schools
    result["school_count"] = len(schools)

    # 300校上限の注記があるか(=表示しきれていない可能性がある)
    result["truncated_note"] = "300校までを表示" in soup.get_text()

    return result


def fetch_school_list(list_id: int,
                       interval: float = DEFAULT_INTERVAL,
                       jitter: float = DEFAULT_JITTER,
                       use_cache: bool = True) -> dict:
    """
    school_list/{list_id} を1リクエストで取得し解析する。
    list_id は 1〜47 (都道府県数) を想定。
    """
    url = f"{BASE_URL}/profile/school_list/{list_id}"
    html = polite_get(url, interval=interval, jitter=jitter, use_cache=use_cache)

    result = parse_school_list(html)
    result["list_id"] = list_id
    result["url"] = url
    return result


def fetch_all_school_lists(list_ids=range(1, 48), **kwargs) -> list:
    """全都道府県分(デフォルト1〜47)を巡回して学校一覧をまとめて取得する。"""
    all_results = []
    for list_id in list_ids:
        all_results.append(fetch_school_list(list_id, **kwargs))
    return all_results


if __name__ == "__main__":
    with open("/home/claude/sample_school_list.html", encoding="utf-8") as f:
        html = f.read()
    result = parse_school_list(html)
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))
