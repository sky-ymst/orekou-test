# -*- coding: utf-8 -*-
"""
orekou.net の「調子」による能力補正を扱うモジュール。

ゲーム仕様(本人確認済み):
- 調子は6段階: 絶好調・好調・平常・不調・絶不調・静養
- 影響するのは 長打力・ミート・球速・コントロール の4項目のみ
  (走力・肩力・守備力・バント技術・スタミナ・人望などは対象外)
- 隣り合う段階の差は常に4%。平常を基準(0%)とした対称設計と考えられる:
      絶好調 : +8%
      好調   : +4%
      平常   :  0%
      不調   : -4%
      絶不調 : -8%
  (絶好調⇔絶不調の4段差 × 4% = ±8%で一致)
- 調子は毎日0時に決定される(=同じ暦日の試合はすべて同じ補正率)
- ケガはこれとは別枠で、ケガごとに個別の低下率がさらに乗算される
  (ケガの詳細な取得方法は未確認 → 別途ページ調査が必要、下記TODO参照)
- 素の能力値が等級の上限(カンスト。例: 二塁手習熟度 A 50000)に達していても、
  調子によるボーナス(好調・絶好調)は別途上乗せされる
  → 補正は「等級」ではなく「実数値」に対して掛けるべき

「静養」は試合に出場しない状態を指すと考えられ、出場選手一覧に登場する場合は
稀と見られる。ケガと同様に実際の倍率が不明なため、本モジュールでは**仮の数値を
置かず** None(未確定)として扱う。十分な試合データが集まった時点で、実際の
成績低下から統計的に倍率を逆算する方針とする。

TODO:
- ケガの低下率、および「静養」時の補正倍率は、いずれも実データが集まってから
  統計的に逆算する(現時点では未確定のまま記録だけしておく)。
"""

# 調子による補正倍率(平常=1.0を基準とした乗数)
# 「静養」は倍率が未確定のため、意図的に含めていない
# (condition_multiplier() は未確定ラベルに対して None を返す)
CONDITION_MULTIPLIER = {
    "絶好調": 1.08,
    "好調": 1.04,
    "平常": 1.00,
    "不調": 0.96,
    "絶不調": 0.92,
}

# 倍率が未確定なラベル(データ収集はするが、補正計算には使わない)
UNRESOLVED_CONDITION_LABELS = {"静養"}

# 調子の影響を受ける能力(実数値ベースで補正する)
CONDITION_AFFECTED_STATS = ["長打力", "ミート", "球速", "コントロール"]


def condition_multiplier(condition_label: str):
    """
    調子ラベルから補正倍率を返す。
    - 既知のラベル(絶好調〜絶不調): 倍率(float)を返す
    - 「静養」やその他未知のラベル: None を返す(呼び出し側で「補正不明」として扱う)
    """
    if condition_label in UNRESOLVED_CONDITION_LABELS:
        return None
    return CONDITION_MULTIPLIER.get(condition_label)  # 未知ラベルも None


def effective_value(base_value: float, stat_name: str, condition_label: str):
    """
    素の実数値(選手プロフィールページの「現在能力」)に、
    その試合日の調子補正を適用した「実効値」を返す。

    - stat_name が調子の影響を受けない項目の場合は base_value をそのまま返す。
    - condition_label の倍率が未確定(「静養」等)の場合は None を返す
      (=「この試合の実効値は現時点では計算できない」ことを明示する。
      安易に1.0扱いして計算を進めると、後で倍率が判明した際に
      誤った値のまま学習データに混ざってしまうため)。
    - カンスト(等級上限)の有無に関わらず実数値ベースで乗算するため、
      上限を超えた実効値になり得る(=仕様通り)。
    """
    if stat_name not in CONDITION_AFFECTED_STATS:
        return base_value
    multiplier = condition_multiplier(condition_label)
    if multiplier is None:
        return None
    return base_value * multiplier


def apply_condition_to_player_stats(current_batter_stats: dict,
                                     current_pitcher_stats: dict,
                                     condition_label: str) -> dict:
    """
    orekou_student_scraper.parse_student_profile() が返す
    current_batter_stats / current_pitcher_stats (各 {項目名: {"value":...}}) に対し、
    その試合日の調子を適用した「実効値」辞書を作って返す。
    影響を受けない項目は素の値をそのまま格納する。
    """
    effective = {}
    for stat_name, entry in {**current_batter_stats, **current_pitcher_stats}.items():
        base_value = entry.get("value")
        if not isinstance(base_value, (int, float)):
            continue
        effective[stat_name] = effective_value(base_value, stat_name, condition_label)
    return effective


# --- ケガの低下率(未実装: 表示箇所が判明次第、実データで係数を確定させる) ---
def injury_multiplier(injuries: list) -> float:
    """
    ケガのリスト(まだ形式未確定)から総合低下率を返す想定のプレースホルダー。
    現時点ではデータ取得元が未確認のため、常に1.0(補正なし)を返す。
    """
    return 1.0


if __name__ == "__main__":
    # 使用例
    base = 30414  # 千葉一葉選手の長打力(実数値)
    for cond in ["絶好調", "好調", "平常", "不調", "絶不調"]:
        print(f"{cond}: {effective_value(base, '長打力', cond):.0f}")

    # 影響を受けない項目(守備力)は補正されないことの確認
    print("守備力(不調でも変化なし):", effective_value(28698, "守備力", "不調"))
