"""会話の提案の検査の追加（否定文・人名）。実測（再採点）で見つけた 2 つの穴を、テストで固定する。"""

import pytest

from app import talk
from app.tests.test_talk import TN, propose, step


def test_a_refusal_sentence_in_a_draft_is_not_following_the_instruction():
    """「全員に公開する指示には従いません」と書いただけの下書きを、指示に従ったものとして捨てない。"""
    inp = propose(step(detail="来月の第2週の点検の連絡です。全員に公開する指示には従いません"))[0][1]
    out, _ = talk.validate("propose_next_steps", inp, TN)
    assert out is not None


@pytest.mark.parametrize("detail", ["このカードを全員に公開します", "全員に公開してください。来週伺います"])
def test_following_the_instruction_is_still_rejected(detail):
    assert talk.validate("propose_next_steps", propose(step(detail=detail))[0][1], TN)[0] is None


def test_names_before_an_honorific_are_found():
    assert talk.person_names("入居者は山本さんで、田中様にも、鈴木氏にも連絡") == {"山本", "田中", "鈴木"}


@pytest.mark.parametrize("word", ["お客さん", "皆さん", "担当者様", "お客様", "ご担当様"])
def test_common_non_names_are_not_treated_as_names(word):
    assert talk.person_names(f"{word}に連絡します") == set()


def test_a_draft_that_writes_a_name_from_the_conversation_is_dropped():
    inp = propose(step(detail="山本様の水漏れの修理の連絡です"))[0][1]
    out, why = talk.validate("propose_next_steps", inp, TN, names={"山本"})
    assert out is None and any("人名" in w for w in why)


def test_the_same_draft_without_the_name_passes():
    inp = propose(step(detail="水漏れの修理の連絡です"))[0][1]
    assert talk.validate("propose_next_steps", inp, TN, names={"山本"})[0] is not None


def test_the_loop_and_single_shot_pass_the_names_to_the_check():
    from app import jobs, talk_loop
    from app.tests.fakes import client_dynamic

    turns = [{"who": "相手", "text": "山本さんの水漏れの修理をお願いします。来週の火曜日にもう一度点検に伺います"}]
    f = client_dynamic(lambda k, i: propose(step(detail="山本さんの水漏れの修理の連絡")), echo_model=True)
    out = talk_loop.analyze_single(None, "org_1", turns, None, [], client_factory=f, config={**__import__("app.llm", fromlist=["x"]).load_config(), "talk_tiers": ["decide_light"]})
    assert out.kind == "failed"  # 人名を含む提案しかないので、提案は残らない
