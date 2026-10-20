"""Voice channel, text side: ITN, entity confidence, what TTS may say, spoken confirmation."""

import pytest

from app.voice.confirm import classify_reply, readback_refund
from app.voice.itn import assess, canonical, cn_number, extract_entities, itn, valid_order_id
from app.voice.speakable import speakable, split_sentences


@pytest.mark.parametrize("spoken,value", [
    ("一百九十八", 198), ("十八", 18), ("两千", 2000), ("一千二", 1200), ("三万五", 35000),
    ("一百零五", 105), ("二十", 20), ("九点九", 9.9), ("一万零五百", 10500), ("二零二六", 2026),
    ("九千九百九十九", 9999), ("两百零二亿六千一百零一万五千一百零二", 20261015102),
])
def test_cn_number(spoken, value):
    assert cn_number(spoken) == value


@pytest.mark.parametrize("raw,expected", [
    # Paraformer streaming wrote this for a clip of "SO20261015102 年卡我要退款 一百九十八块".
    ("SO二零二六一零一五一零二年卡我要退款一百九十八块", "SO20261015102年卡我要退款198块"),
    # SeACo-Paraformer writes characters with spaces and heard "SO" as "s wall".
    ("s wall 二 零 二 六 一 零 一 五 一 零 二 年 卡 我 要 退 款", "SO20261015102年卡我要退款"),
    ("S O 二零二六 一零零八 零零二 的快递到哪了", "SO20261008002的快递到哪了"),
    ("订单号是二零二六一零零八零零二", "订单号是SO20261008002"),
    ("先退我五十块吧", "先退我50块吧"),
    ("九块九包邮", "9.9块包邮"),
    ("一百九十八块五", "198.5块"),
    ("一块钱能充多少Echo币", "1块钱能充多少Echo币"),
    ("下载的歌能导出成 M P 三吗", "下载的歌能导出成MP3吗"),
    ("p一故障多久响应", "P1故障多久响应"),
    ("我的手机号是一三八一二三四五六七八", "我的手机号是13812345678"),
    # Left alone: ordinary words with numerals in them.
    ("七天无理由退货有什么条件", "七天无理由退货有什么条件"),
    ("帮我查一下订单", "帮我查一下订单"),
    ("app一直闪退", "app一直闪退"),
    ("一次只能退一个订单吗", "一次只能退一个订单吗"),
])
def test_itn(raw, expected):
    assert itn(raw).text == expected


def test_itn_marks_an_order_number_whose_prefix_was_not_heard():
    assert itn("s wall 二零二六一零一五一零二").inferred_prefix == {"SO20261015102"}
    assert itn("SO二零二六一零一五一零二").inferred_prefix == set()


def test_eleven_digits_without_order_context_stay_digits():
    # A phone number must stay a phone number (so that it is masked), not become an order.
    assert itn("一三八一二三四五六七八").text == "13812345678"


def test_canonical_scores_numbers_alike_on_both_sides():
    assert canonical("七天无理由退货有什么条件？") == canonical("7天 无理由退货，有什么条件")
    assert canonical("SO20261015102 年卡我要退款") == canonical("S O 二零二六一零一五一零二 年卡 我要退款。")
    assert canonical("豪华会员连续包月多少钱？") == "豪华会员连续包月多少钱"


@pytest.mark.parametrize("order_id,ok", [
    ("SO20261015102", True), ("so20261015102", True), ("SO2026101510", False), ("SO20261315102", False),
    ("SO20261000102", False), ("SO202610151023", False),
])
def test_valid_order_id(order_id, ok):
    assert valid_order_id(order_id) is ok


def test_entities_and_confidence():
    text = itn("s wall 二零二六一零一五一零二 年卡退款一百九十八块").text
    entities = assess(extract_entities(text, ["年卡", "豪华会员"]), inferred_prefix={"SO20261015102"})
    found = {(e.kind, e.value): e for e in entities}
    assert set(found) == {("order_id", "SO20261015102"), ("amount", "198"), ("term", "年卡")}
    assert found[("order_id", "SO20261015102")].confidence < 0.6  # read back before acting on it
    assert found[("amount", "198")].confidence > 0.9


def test_two_passes_that_disagree_lower_confidence():
    final = itn("SO二零二六一零一五一零二 退款").text
    entities = assess(extract_entities(final), other_passes=["SO二零二六一零一五一零八 退款"])
    assert entities[0].value == "SO20261015102" and entities[0].confidence < 0.6
    agreeing = assess(extract_entities(final), other_passes=["SO二零二六一零一五一零二退款"])
    assert agreeing[0].confidence > 0.9


def test_invalid_order_format_is_low_confidence():
    entities = assess(extract_entities("SO2026101510 退款"))
    assert entities[0].confidence < 0.6 and "format" in entities[0].notes


# ---------------------------------------------------------------- what TTS is given
def test_speakable_spells_order_numbers_and_money():
    speech = speakable("订单 SO20261015102 的退款 ¥198.00 已提交[1]。")
    assert "SO20261015102" not in speech.text and "[1]" not in speech.text
    assert "S O" in speech.text and "二零二六" in speech.text and "198元" in speech.text


@pytest.mark.parametrize("sentence,kind,leak", [
    ("您绑定的手机号是13812345678。", "phone", "13812345678"),
    ("您绑定的手机号是138****5678。", "phone", "138"),
    ("退款将退回银行卡6222021234567890123。", "bank_card", "6222"),
    ("退款将退回银行卡6222***********0123。", "bank_card", "6222"),
    ("身份证号110101199003071234已核验。", "id_card", "110101"),
    ("账单已发送到 mia@example.com。", "email", "mia@"),
    ("您的验证码是 482913。", "code", "482913"),
])
def test_speakable_never_reads_sensitive_data_aloud(sentence, kind, leak):
    speech = speakable(sentence)
    assert kind in speech.redacted
    assert leak not in speech.text.replace(" ", "")
    digits = "".join(c for c in speech.text if c.isdigit())
    assert len(digits) <= 4  # at most a "尾号" of four digits survives, never the number


def test_speakable_keeps_last_four_as_spoken_digits():
    assert "尾号五六七八" in speakable("手机号 13812345678 已绑定。").text


def test_split_sentences():
    assert split_sentences("您好。订单已发货！预计明天到；请留意短信") == ["您好。", "订单已发货！", "预计明天到；", "请留意短信"]
    assert split_sentences("第一行\n第二行。") == ["第一行", "第二行。"]


# ---------------------------------------------------------------- spoken confirmation
@pytest.mark.parametrize("reply,kind", [
    ("确认", "yes"), ("对", "yes"), ("对的，退吧", "yes"), ("是的。", "yes"), ("嗯嗯", "yes"), ("没错", "yes"),
    ("好的", "yes"), ("确定", "yes"),
    ("不对", "no"), ("不是", "no"), ("取消", "no"), ("算了不退了", "no"), ("错了", "no"), ("对不对啊", "no"),
    ("SO20261015102 物流到哪了", None), ("我想问一下会员能不能退款", None), ("", None),
])
def test_classify_reply(reply, kind):
    assert classify_reply(reply) == kind


def test_readback_states_order_and_amount():
    text = readback_refund("SO20261015102", 198.0)
    assert "SO20261015102" in text and "198" in text and "确认" in text
    spoken = speakable(text).text
    assert "二零二六" in spoken and "198元" in spoken


# ---------------------------------------------------------------- review round 1 (second model)
@pytest.mark.parametrize("reply", [
    # P0: short requests that start like a yes must not move money
    "可以退另一个订单吗", "好的帮我查下物流", "行再退一个", "好那换成另一个订单吧", "确认一下营业时间", "对了我还想问运费",
    "是不是退到原来的卡上", "嗯我想想", "好像不对吧啊",
])
def test_a_request_that_starts_like_yes_is_not_a_confirmation(reply):
    assert classify_reply(reply) != "yes"


@pytest.mark.parametrize("reply", ["不确认", "不要退了", "别退了"])
def test_declines(reply):
    assert classify_reply(reply) == "no"


@pytest.mark.parametrize("sentence,leak", [
    ("您的手机号是138 1234 5678。", "12345678"),
    ("您的手机号是138-1234-5678。", "12345678"),
    ("您的手机号是1 3 8 1 2 3 4 5 6 7 8。", "1 2 3 4"),
    ("您的手机号是138 **** 5678。", "138"),
    ("动态码 482913，请勿告诉他人。", "482913"),
    ("校验码是482913。", "482913"),
    ("验证码为 48 29 13。", "48 29 13"),
    ("取件码 6620。", "6620"),
    ("身份证 110101 19900307 1234 已核验。", "19900307"),
])
def test_speakable_catches_spaced_and_dashed_numbers(sentence, leak):
    speech = speakable(sentence)
    assert speech.redacted and leak not in speech.text
    assert sum(c.isdigit() for c in speech.text) == 0


@pytest.mark.parametrize("sentence", ["退款预计1-3个工作日到账。", "订单于2026-10-18签收。", "运费 10 元，满 99 元包邮。"])
def test_speakable_leaves_ordinary_numbers(sentence):
    assert speakable(sentence).redacted == []


def test_passes_are_compared_by_entity_not_substring():
    final = itn("SO二零二六一零一九一零一 退款一百九十八块").text
    entities = assess(extract_entities(final), other_passes=["SO二零二六一零一九一零一 退款一千九百八十块"])
    amount = next(e for e in entities if e.kind == "amount")
    assert amount.value == "198" and amount.confidence < 0.6  # "198" is a substring of "1980块"


def test_partial_transcripts_hide_long_digit_runs():
    from app.voice.session import mask_partial

    assert "12345" not in mask_partial("我的手机号是一三八一二三四五")
    assert mask_partial("SO二零二六一零一九一零一 物流") == "SO20261019101物流"  # an order number is not personal data


def test_the_second_of_two_order_numbers_keeps_its_prefix():
    assert itn("查一下 S O 二零二六一零零八零零二 和 二零二六一零一二零零三 两个单子").text == \
        "查一下SO20261008002和SO20261012003两个单子"
