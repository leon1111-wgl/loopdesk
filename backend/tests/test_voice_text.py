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
    ("确认", "yes"), ("对", "yes"), ("对的，退吧", "yes"), ("是的。", "yes"), ("嗯嗯", None), ("没错", "yes"),
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


# ---------------------------------------------------------------- review round 2
@pytest.mark.parametrize("reply", ["好的好的", "是的是的", "确认确认", "没问题", "好的，没问题", "那确认", "那就退吧", "哦对",
                                   "啊，是的", "对，退"])
def test_common_ways_of_saying_yes(reply):
    assert classify_reply(reply) == "yes"


@pytest.mark.parametrize("reply", ["嗯", "嗯……", "好了", "行了", "OK了"])
def test_backchannels_are_not_consent(reply):
    assert classify_reply(reply) != "yes"


@pytest.mark.parametrize("reply", ["不退", "不确定", "不", "别"])
def test_more_declines(reply):
    assert classify_reply(reply) == "no"


@pytest.mark.parametrize("sentence,leak", [
    ("您的手机号是１３８１２３４５６７８。", "１３８"),
    ("门店电话 010-12345678。", "12345678"),
    ("门店电话 02112345678。", "12345678"),
    ("验证码（六位）：482913。", "482913"),
    ("短信码 482913。", "482913"),
    ("激活码 ABCD-EFGH-1234。", "ABCD"),
    ("兑换码 7788990011。", "7788990011"),
    ("护照 E12345678 已登记。", "12345678"),
    ("您的手机号是一三八一二三四五六七八。", "一三八一二三四五六七八"),
])
def test_speakable_more_formats(sentence, leak):
    speech = speakable(sentence)
    assert speech.redacted and leak not in speech.text


@pytest.mark.parametrize("sentence,kept", [
    ("快递单号 73120123456789，可以在快递官网查询。", "七三一二"),
    ("运单号 SF 1402 9384 7512。", "1402"),
    ("工单号 2026101812345 已创建。", "2026101812345"),
    ("退款流水号 2026101812345678。", "2026101812345678"),
])
def test_speakable_keeps_reference_numbers_the_customer_asked_for(sentence, kept):
    speech = speakable(sentence)
    assert speech.redacted == []


def test_speakable_reads_thousands_as_one_amount():
    assert "1234.56元" in speakable("退款 ¥1,234.56 已提交。").text


def test_a_missing_amount_in_the_other_pass_is_not_a_disagreement():
    final = itn("SO二零二六一零一九一零一 退款一百九十八块").text
    entities = assess(extract_entities(final), other_passes=["SO二零二六一零一九一零一 退款一百九十八"])
    assert all(e.confidence > 0.9 for e in entities)


def test_two_order_numbers_after_dingdanhao():
    assert itn("订单号二零二六一零零八零零二和二零二六一零一二零零三").text == "订单号SO20261008002和SO20261012003"


def test_refund_read_back_puts_the_amount_in_its_own_sentence():
    assert split_sentences(readback_refund("SO20261019101", 18.0)) == [
        "为了您的资金安全，跟您确认一下：订单号SO20261019101，退款金额18元。", "确认请说“确认”，不对请说“取消”。"]


# ---------------------------------------------------------------- review round 3
@pytest.mark.parametrize("reply", ["对吧", "可以吧", "是吧", "没错吧"])
def test_question_tags_are_not_consent(reply):
    assert classify_reply(reply) != "yes"


@pytest.mark.parametrize("reply", ["好吧", "就退吧", "对啊", "是啊"])
def test_particles_after_a_plain_yes(reply):
    assert classify_reply(reply) == "yes"


def test_a_question_with_shi_bu_shi_does_not_cancel():
    assert classify_reply("是不是退到原来的卡上") is None


@pytest.mark.parametrize("sentence", ["验证码5分钟内有效。", "验证码 6 位，请注意查收。", "快递：73120123456789。",
                                      "物流单号为 73120123456789。", "2026101812345678 这个流水号可以查到。",
                                      "客户编号是 0012345678。"])
def test_speakable_round3_false_positives(sentence):
    assert speakable(sentence).redacted == []


# ---------------------------------------------------------------- tuned on the synthetic dev set (voice-eval run 39475750196)
@pytest.mark.parametrize("raw", ["订单爱思欧二零二六一零零八零一零的马克杯", "爱 斯 欧 二 零 二 六 一 零 零 八 零 一 零",
                                 "I斯欧二零二六一零零八零一零", "I欧二零二六一零零八零一零", "艾斯欧20261008010", "爱四欧二零二六一零零八零一零"])
def test_so_said_the_chinese_way(raw):
    assert "SO20261008010" in itn(raw).text


def test_a_word_that_merely_starts_like_so_is_left_alone():
    assert itn("我爱你一二三").text == "我爱你一二三"


def test_wan_in_written_amounts():
    assert itn("额度改成1万元").text == "额度改成10000元"
    assert itn("一万五千块").text == "15000块"


@pytest.mark.parametrize("raw", ["搜二零二六零九一八零一一我要退货退款", "骚 二 零 二 六 零 九 一 八 零 一 一", "嗖20260918011"])
def test_so_said_as_the_english_word(raw):
    # CosyVoice says "S O" like the word "so" (voice-eval run 39484338163): not used to tune the first rule.
    assert "SO20260918011" in itn(raw).text


def test_sou_elsewhere_is_left_alone():
    assert itn("搜一下会员价格").text == "搜一下会员价格"


# ---------------------------------------------------------------- punctuated transcripts (LOOPDESK_VOICE_PUNC)
@pytest.mark.parametrize("raw,expected", [
    # What a punctuation model writes into Paraformer's output: commas around and inside a spoken order
    # number, a question mark at the end. ITN still finds the number, and the punctuation stays Chinese
    # (NFKC alone turns "，" into ",").
    ("s o 二零二六零九一八零零一，我要退货退款，另外发票怎么开？", "SO20260918001，我要退货退款，另外发票怎么开？"),
    ("S O，二零二六，一零零八，零零二，的快递到哪了", "SO20261008002，的快递到哪了"),
    ("s，o，二零二六一零一五一零二年卡我要退款", "SO20261015102年卡我要退款"),
    ("爱思欧，二零二六一零一五一零二，年卡我要退款。", "SO20261015102，年卡我要退款。"),
    ("搜，二零二六一零一五一零二帮我查一下", "SO20261015102帮我查一下"),
    ("订单号是，二零二六一零零八零零二。", "订单号是SO20261008002。"),
    ("一百九十八块，另外能开发票吗？", "198块，另外能开发票吗？"),
    # The second of two order numbers, and 订单号 with a comma after it (Fable review of 3172ff6).
    ("SO20261008002，和 20261012003", "SO20261008002，和SO20261012003"),
    ("订单号，是二零二六一零零八零零二", "订单号，是SO20261008002"),
    ("我的手机号是，一三八，一二三四，五六七八。", "我的手机号是，13812345678。"),
    ("下载的歌能导出成 M P 三吗？", "下载的歌能导出成MP3吗？"),
    # English stays English; numbers keep their own separators.
    ("where is my order S O 二零二六一零零八零零二?", "where is my order SO20261008002?"),
    ("下午3:30以后发货吗？", "下午3:30以后发货吗？"),
])
def test_itn_on_punctuated_text(raw, expected):
    assert itn(raw).text == expected


def test_a_comma_inside_the_spoken_prefix_does_not_make_it_inferred():
    assert itn("s，o，二零二六一零一五一零二").inferred_prefix == set()
    assert itn("爱思欧，二零二六一零一五一零二").inferred_prefix == set()


def test_punctuation_does_not_change_the_canonical_text():
    assert canonical("s o 二零二六零九一八零零一，我要退货退款，另外发票怎么开？") == \
        canonical("SO20260918001 我要退货退款，另外发票怎么开？")
