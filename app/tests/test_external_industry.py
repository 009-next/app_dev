"""医療・介護など機微な業種の文章は、第三者の提供元のモデルへ渡さない（実写真の検証で見つかった穴）。"""

import pytest

from app import signals

MEDICAL = {"作業前の説明": "手術室のモニターと配管が絡まっている", "作業後の説明": "医療スタッフの配線を整理した"}


@pytest.mark.parametrize("word", ["手術", "患者", "病院", "看護", "介護", "利用者", "カルテ", "診療", "入院", "医療"])
def test_a_sensitive_industry_word_blocks_the_external_model(conn, word):
    ok, why = signals.external_ok(conn, "org_1", "classify", {"a": f"{word}の現場で配線を整理した"}, current_scope="org_only")
    assert not ok and "業種" in why


def test_the_real_photo_medical_scene_is_not_sent_out(conn):
    assert not signals.external_ok(conn, "org_1", "classify", MEDICAL, current_scope="org_only")[0]


def test_an_ordinary_construction_text_still_goes_out(conn):
    ok, _ = signals.external_ok(conn, "org_1", "classify", {"a": "配筋の位置を確認した", "b": "足場を組み直した"}, current_scope="org_only")
    assert ok
