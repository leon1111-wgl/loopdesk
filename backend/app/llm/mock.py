"""Deterministic offline model.

It implements the same tool-calling contract as a real LLM, so the whole platform
(agent loop, approvals, verifier, evals, UI) runs and is testable without an API key.
Behaviour is rule-based and reproducible: the same input always yields the same
trajectory, which is exactly what CI regression tests need.

It is NOT a stand-in for model quality. Numbers measured with it validate the pipeline,
not answer quality; re-run the eval suite with a real model for that.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from typing import Any

from ..textutil import (_CANCEL_AND_REFUND_RE, CANCEL_ORDER_RE, CANCEL_TALK_RE, NEGATED_PERSON_RE, NEGATED_REFUND_RE, ORDER_ID_RE, OTHER_ORDER,
                        REFUND_APPLY_RE, REFUND_RE, cancel_requested, handoff_cancelled,
                        estimate_tokens, expand_query, fix_typos, normalize_width, other_question, own_amount_text, reads_as_request,
                        refund_bare_decline,
                        refund_requested, refund_withdraw_requested, stated_refund_amount, withdraw_confirmed,
                        split_sentences, strip_citations, token_set)
from ..rag.chunking import CUT_MARK
from .base import ChatResult, LLMMessage, LLMProvider, StreamEvent, ToolCall, ToolSpec, Usage

_HANDOFF = re.compile(r"人工|真人|投诉|转接")
# Negated requests ("不用转人工") are cut out before matching, the way a real model reads them;
# otherwise the offline model (the demo path and the eval baseline) inflates the handoff rate.
_NEGATED_HANDOFF = NEGATED_PERSON_RE
_REFUND = REFUND_RE
_SHIPMENT = re.compile(r"物流|快递|到哪|发货了吗|配送|运单|什么时候到|送到|where is|track|arrive|deliver|shipping"
                       r"|还没到(?!账)|(?:怎么|咋)还没(?:到|收到|送)(?!账)|几号(?:能)?到|哪天(?:能)?到|啥时候(?:能)?(?:送)?到(?!账)"
                       r"|在哪了|在路上", re.IGNORECASE)
# "那大概几号能到", "when will it arrive": a delivery estimate, from the shipping policy (N-060).
_ETA = re.compile(r"几号|哪天|什么时候(?!.{0,3}账)|啥时候(?!.{0,3}账)|多久(?:能|才能)?(?:到|送)|几天(?:能|才能)?(?:到|送)"
                  r"|when\s+will|eta(?![A-Za-z])", re.IGNORECASE)
# "为什么这么慢", "那你说怎么办": chasing, answered with the tracking and the way to a person (N-220).
_CHASE = re.compile(r"这么慢|太慢|好慢|真慢|慢死|怎么这么久|太久|等了?(?:好|很)?久|等不了|还要等|催一?下|催件|催促|着急|急用")
# "那你说怎么办" right after the tracking: chasing too, when that is all it says (not "发票开错了怎么办").
_CHASE_SHORT = re.compile(r"(?:那|那你|你)?(?:说)?(?:怎么办|咋办|咋整)(?:啊|呀|呢|吧)?")
CHASE_TEXT = "如需催件，可以回复“转人工”，人工客服会帮您联系快递。"
# "SO… 催发货 今天能发吗": the shipping-time rule, not the order summary alone (N-157).
_RUSH = re.compile(r"催(?:一下)?发货|(?:今天|明天|什么时候|啥时候|几时|多久)(?:能|才能)?发(?:货)?|怎么还(?:不|没)发货|还没发货")
RUSH_TEXT = "如需催促发货，可以回复“转人工”。"
# Things the order tools cannot do, sent to the after-sales team as a ticket (N-157, N-063).
_NOT_RECEIVED = re.compile(r"签收了?.{0,8}(?:没|未)(?:有)?收到|没收到.{0,8}签收|丢件|(?:快递|包裹)丢了|被(?:人)?冒领")
_CHANGE_INFO = re.compile(r"(?:改|修改|更改|换)(?:个|一下|下)?(?:收货|收件)?(?:地址|电话|手机号?|收件人)|地址(?:写错|填错)|搬家")
_CANCEL = CANCEL_ORDER_RE
_REPAIR = re.compile(r"维修|修理|返修|修一下|修好|换(?:个|一个)?新的?|换货")
# "想换一个" next to a fault is an exchange too (simulations, dev card leon_mug_replace): on its own it may be an address.
_SWAP = re.compile(r"换(?:个|一个|一件|一只|一台)(?!地址|号码|手机号|电话|收货|颜色|尺码)")
# Words that say not this order, said of one of two ("抱枕 SO1 想退，机器人 SO2 别退"): it gets no offer.
_NOT_THIS_ONE = re.compile(r"(?:先|就|还是)?(?<!退)(?:别|不要|不用|不需要|先不|暂时不|暂不|不)(?:给我|帮我)?退(?!货)")
# What a number shares with the next one: "SO2 和 SO1 都退了" asks of both.
_ONLY_A_LINK = re.compile(r"[\s，,、]*(?:和|与|跟|及|以及|还有)?[\s，,、]*")
_REFUND_FAILED = re.compile(r"退款(?:失败|没成功|不成功|被退回|退回来了)|退(?:款)?不成功|没退成功")
REFUND_FAILED_TEXT = ("知识库里没有写退款失败后怎么处理。{status}如果退款没有成功或被退回，可以回复“转人工”，"
                      "人工客服会帮您核实。")
# What is wrong with it: a repair or exchange asked as a question ("能换个新的吗") is still a report then.
_FAULTY = re.compile(r"坏|碎|破|裂|漏|没声|不响|开线|划痕|瑕疵|质量问题|有问题|不能用|用不了|连不上|打不开|开不了|没反应|故障")
# Damage the customer caused is not a quality problem (N-200).
_SELF_DAMAGE = re.compile(r"自己(?:不小心)?(?:摔|弄|碰|砸|掉|拆)|(?:孩子|小孩|宠物|狗|猫)(?:给)?(?:弄|摔|咬|抓|打)|人为"
                          r"|不小心(?:摔|弄|碰|掉)|我(?:把它)?摔(?:坏|碎|了)")
SELF_DAMAGE_QUERY = "人为损坏 质量问题 退换"
# "再不到我就差评投诉！！": a threat about later, answered now (N-129); "我要投诉" still goes to a person.
_IF_COMPLAIN = re.compile(r"(?:再不|如果|要是|若|不然|否则|再这样)[^，,。！!？?]{0,12}?(?:差评|投诉|举报)+")
IF_COMPLAIN_TEXT = "如需投诉，可以回复“转人工”，人工客服会跟进处理。"
# "你好", "在吗", "谢谢", "？？？": no search, no refusal (N-076).
_GREETING = re.compile(r"(?:你好|您好|hi|hello|hey|嗨|哈喽|在吗|在不在|在么|有人吗|客服)[呀啊吗呢嘛！!。.，,？?~～\s]*|[？?！!。.\s]+",
                       re.IGNORECASE)
_THANKS = re.compile(r"(?:好的?|嗯|ok|okay)?[，,\s]*(?:谢谢|多谢|感谢|谢了|thanks?|thank\s+you)[你您啦了！!。.~～\s]*"
                     r"|(?:再见|拜拜|bye)[！!。.~～\s]*", re.IGNORECASE)
GREETING_TEXT = "请问有什么可以帮您？如需查询订单、物流或退款，直接发订单号就行。"
THANKS_TEXT = "感谢您的咨询，还有其他问题随时找我。"
NO_HANDOFF_TEXT = "如需人工客服，随时回复“转人工”就行。"
# "我的订单有哪些": a listing (N-057); "第二单" after one: that order (N-093).
_LIST_MINE = re.compile(r"我的订单(?:有哪些|列表|呢|都有)?|我(?:都)?(?:买|下)了?(?:哪些|什么)|(?:看|查)(?:一下|下)?(?:我的)?(?:订单|最近的订单)"
                        r"|最近的订单|订单列表")
_ORDINAL = re.compile(r"(?:就|是|选|要|那就)?(?:第\s*([一二三四五12345])\s*(?:个|单|笔|项|条)?|(最后)(?:一|那)?(?:个|单|笔))"
                      r"(?:吧|的|订单|那个|那单)?[\s。.，,!！~～]*")
_ORDINAL_VALUE = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
LISTING_MARK = "最近的订单有："
# "退款到哪一步了", "退款进度查一下", "SO… 的退款多久到账": a question about a refund, read-only (N-056).
# An explicit request in the same sentence ("我要退款，多久到账") still files one.
_PROGRESS = re.compile(r"进度|到哪一?步|退款到哪了|怎么样了|处理得?怎么样|到账了?(?:吗|没)|到没到账|退回来了?(?:吗|没)"
                       r"|(?:多久|几天|什么时候|啥时候)(?:能|才能|可以)?到账|status|progress"
                       # N-092, N-128: chasing money already asked for, not asking for it
                       r"|钱呢|退款呢|钱(?:退了|到了)(?:吗|没)|还没(?:有)?到账|没(?:有)?到账|没收到(?:退款|钱)", re.IGNORECASE)
# The customer's own money, so their orders are looked up even without "我的" (N-092): "钱退了吗 怎么还没到账".
_MY_MONEY = re.compile(r"钱呢|钱(?:退了|到了)(?:吗|没)|还没(?:有)?到账|没收到(?:退款|钱)")
_APPLY = REFUND_APPLY_RE
# "我的快递咋还没到", "我最近买的那个扫地机器人到哪了", "快递到哪了": about one of the customer's
# own orders even without its number, so look their orders up instead of searching policy (N-057).
# "我的银行卡号…能退到这张卡吗" is a policy question: "我的" counts only before an order word.
_OWN = re.compile(r"我的(?:快递|包裹|订单|单|货|东西|退款)|我那|我最近|我刚买|我上次|我下的"
                  r"|我(?:上周|昨天|前天|前几天|上个月|上次|刚刚?|刚才|之前)?(?:买|下)的")  # "我上周买的那个音箱" (N-223)
_MINE = re.compile(_OWN.pattern + r"|单子|到哪了|到哪儿|还没到|咋还没|怎么还没")
_TICKET = re.compile(r"报修|故障|坏了|工单|无法使用|打不开|不工作|失灵|连不上|没声|不响|没反应")
# A ticket is for a problem being reported ("坏了", "打不开", "出故障了"), not for a question about how
# problems are handled ("P1故障多久响应？", "怎么报修？"): the knowledge base answers those.
_REPORTED = re.compile(r"坏了|无法使用|打不开|不工作|失灵|连不上|开不了|没反应|出故障|故障了|没声|不响")
_ASKING = re.compile(r"多久|多长时间|怎么|如何|什么|哪些|是否|能不能|可以吗|吗[？?]?$|[？?]$")
_URGENT = re.compile(r"紧急|严重|马上|立刻")
_AMOUNT = re.compile(r"[¥￥]\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:元|块)")
_HALF = re.compile(r"退一半(?!运费|邮费|的?运费)|一半的钱|退半价")

HALLUCINATION = "另外，所有订单均支持 90 天无理由退货，退款 2 小时内到账。"
REASK_MARKER = "（系统核对，不是顾客的话）"  # verifier.REASK_MARKER (not imported: the agent package imports this one)
REFUSAL = (
    "抱歉，知识库中没有找到与该问题直接相关的依据。"
    "为避免给出不准确的信息，建议您转人工客服确认。"
)
# The search itself failed or ran partly (N-078): say so instead of "the knowledge base has nothing".
SEARCH_DOWN = "抱歉，知识库检索暂时出了故障，这次没能查到相关规定，并不代表没有。请稍后再试，或回复“转人工”。"


# "SO… 我要退货退款，另外发票怎么开？" carries two requests. The clause after a connector is
# answered on its own instead of being silently dropped.
_EXTRA = re.compile(r"[，,。；;！!？?\s]+(?:另外|还有|顺便问一下|顺便|再问一下|以及)[，,\s]*")
MULTI_REFUND_TEXT = "一次只能为一个订单申请退款，请告诉我要退哪一个订单。"
BUILTIN_TOOLS = {"search_knowledge", "get_order", "list_my_orders", "track_shipment", "request_refund",
                 "withdraw_refund_request", "create_ticket", "handoff_to_human"}
REJECTED_NEXT_STEP = "如有补充凭证，可以重新提交退款申请，也可以回复“转人工”由客服进一步处理。"


# "SO… 到哪了 退款多久到账" is two questions without an "另外" (N-221): the parcel, then refund timing in general.
_CUT = re.compile(r"[，,。；;！!？?]+|(?<=[\u4e00-\u9fff])\s+|\s+(?=[\u4e00-\u9fff])")
_TIMING = re.compile(r"多久|多长时间|几天|几个工作日|什么时候|啥时候|到账时间|时效")


def _then_refund_timing(text: str) -> tuple[str, str] | None:
    for cut in _CUT.finditer(text):
        head, rest = text[:cut.start()], text[cut.end():]
        if (_SHIPMENT.search(head) and not _REFUND.search(head) and _REFUND.search(rest) and _TIMING.search(rest)
                and not ORDER_ID_RE.search(rest) and not refund_requested(text)):
            return head.strip(), rest.strip()
    return None


def split_intents(text: str) -> tuple[str, str | None]:
    """(main request, an extra question or None)."""
    match = _EXTRA.search(text)
    if not match:
        # "自动续费怎么关 我不想续了 这个月的也退给我": the refund is the request, the renewal a question (N-179)
        other = other_question(text)
        if other and other in text:
            return text.replace(other, " ", 1).strip(" ，,。？?！!；;\u3000"), other
        return _then_refund_timing(text) or (text, None)
    extra = text[match.end():].strip()
    if len(extra) < 2 or ORDER_ID_RE.search(extra):
        return text, None
    return text[:match.start()].strip(), extra


# Beyond the refund words: "能退吗", "就帮我退了吧", "退不退", "SO… 不要了 退", "退20". Whatever does not file on these is offered.
_LOOSE_REFUND = re.compile(r"退(?:了|吧|吗|么|嘛|不退|一下|下|哈|呗|个款|回|\s*[¥￥]?[0-9]|(?=[\s，,。！!？?]|$))|(?:能|可以|可|要|想|帮我|给我)退(?!订|出|群|役|回)")
# "SO… 不要了": with the order named, what the customer may want is offered too.
_UNWANTED = re.compile(r"(?:不想要|不要)了?(?=[\s，,。！!]|$)")


def _mentions_refund(text: str, items: tuple[str, ...] = ()) -> bool:
    """A refund word, a bare demand without one ("SO… 退了吧", N-126), or a loose one."""
    return (bool(_REFUND.search(text) or _LOOSE_REFUND.search(text) or (ORDER_ID_RE.search(text) and _UNWANTED.search(text) and not _CANCEL.search(text)))
            or refund_requested(text, items))


def _item_names(order: dict[str, Any]) -> tuple[str, ...]:
    """What may be named in a request for this order: a clause naming anything else is not part of one."""
    return tuple(str(i.get("name", "")) for i in order.get("items") or [])


def asks_refund_status(text: str) -> bool:
    return (bool(_mentions_refund(text) or "到账" in text or _MY_MONEY.search(text)) and bool(_PROGRESS.search(text))
            and not _APPLY.search(text))


_ASKING_WORDS = re.compile(r"如果|要是|假如|假设|万一|的话|只是问问|就问问|问问而已|随便问问|先问问|咨询一下|想?问一?下"
                           r"|可能会?|也许|或许|看情况|还在考虑|考虑一下|还在犹豫|没想好"
                           r"|just\s+asking|just\s+wondering|what\s+if|(?<![A-Za-z])if(?![A-Za-z])", re.IGNORECASE)
_FILLER = re.compile(r"^[，,\s]*(?:我|先)?|[啊哈呀吧呢嘛]+(?=[\s，,！!。]|$)|我?就?问问|\bI\b"
                     r"|(?<!需)(?:我?(?:申请|想要?|要)|帮我|给我)(?=退)")  # "如果我申请退款，…" looks up the refund, not 申请


def refund_question(text: str) -> str:
    """What to look up for a question about refunds: the words without the order number or the "别退" part."""
    question = _ASKING_WORDS.sub(" ", ORDER_ID_RE.sub(" ", NEGATED_REFUND_RE.sub(" ", text)))
    question = _FILLER.sub(" ", question)
    question = re.sub(r"\s+", " ", question).strip(" ，,。！!？?:：")
    if question and not re.search(r"[\u4e00-\u9fff]", question):  # an English question about a Chinese policy
        return "退款 到账 工作日" if _EN_TIMING.search(question) else "七天无理由退货 质量问题 退换"
    return question or "退款 到账 工作日"


_EN_TIMING = re.compile(r"(?<![A-Za-z])(?:when|how\s+long|how\s+soon|eta|how\s+many\s+days)(?![A-Za-z])", re.IGNORECASE)


def fault_report(text: str) -> bool:
    """Something the customer bought is faulty, said without an order number ("我买的扫地机器人坏了")."""
    return bool(_FAULTY.search(text) and _OWN.search(text) and not _SELF_DAMAGE.search(text))


# Several things reported at once ("我买的音箱没声了 杯子也碎了"): a ticket for each, or the second one is lost.
_FAULT_SEP = re.compile(r"[，,。；;！!]+|(?<=[\u4e00-\u9fff])\s+|另外|还有|以及")  # not after "SO…": it starts its clause
_FAULT_QUESTION = re.compile(r"多久|多长时间|哪些|是否|什么时候|啥时候|容易|会不会|如果|要是|假如|万一|的话|吗\s*$|[?？]")
_NOT_FAULTY = re.compile(r"没有?(?:坏|碎|破|裂|漏)|不是(?:坏|碎|破)|不(?:坏|破)")
# Words before a fault that name nothing of their own: "音箱没声了 是不是坏了", "而且也打不开".
_NOT_A_THING = re.compile(r"是不是|不过|但是|其实|好像|可能|感觉|而且|并且|然后|现在|今天|昨天|前天|上周|已经|完全|根本|一直|总是|老是|经常|不知道|为什么|怎么"
                          r"|\d+|[一二两三四五六七八九十几]+(?:天|周|个月|年|次|小时|分钟)"
                          r"|是|还|也|都|又|就|才|刚|它|这个|那个|这|那|了|的|我|买|一")
# A part of the thing, what was tried on it, or no thing at all: "屏幕也碎了", "按电源键也开不了机", "订单有问题".
_A_PART = re.compile(r"(?:屏幕|声音|喇叭|电池|充电|键|按钮|开关|外壳|包装|盖子|把手|手柄|插头|电源|音量|蓝牙|遥控|拉链|线头|里面|外面|底部|顶部"
                     r"|订单|物流|快递)$|^(?:按|点|插|连|开机|重启|试|换|用|拿|放|摇|拍|摸|碰)")
_FAULTS_AT_ONCE = 3  # what fits in the steps of a run; the rest goes on the last ticket


def _thing(words: str) -> str:
    return re.sub(r"\W", "", _NOT_A_THING.sub("", ORDER_ID_RE.sub("", words)))


def several_faults(text: str) -> list[str]:
    """The clauses of a message that report different things faulty, when there are two or more. Another clause about
    the same thing ("音箱没声了 是不是坏了") stays one report, and a question or a refund goes the usual way. What the
    customer damaged is not reported: "音箱坏了 耳机也坏了 不过耳机是我自己摔的" is the speaker alone, and "音箱坏了 是我自己摔的"
    names nothing else, so it goes the usual way too."""
    if not (_OWN.search(text) or _REPORTED.search(text) or ORDER_ID_RE.search(text)) or _mentions_refund(text) or wants_human(text):
        return []
    clauses = _FAULT_SEP.split(text)
    damaged = [clause for clause in clauses if _SELF_DAMAGE.search(clause)]
    if any(len(_thing(clause[:_SELF_DAMAGE.search(clause).start()])) < 2 for clause in damaged):
        return []
    faults, before = [], ""
    for clause in clauses:
        fault = _FAULTY.search(clause)
        if not fault:
            before = clause  # "音箱 没声了": the thing, then what is wrong with it
            continue
        thing = _thing(clause[:fault.start()])
        if len(thing) < 2 and before:
            clause, thing = f"{before} {clause}", _thing(before)
        before = ""
        if _NOT_FAULTY.search(clause) or _FAULT_QUESTION.search(clause) or _SELF_DAMAGE.search(clause):
            continue
        if ((ORDER_ID_RE.search(clause) or (len(thing) >= 2 and not _A_PART.search(thing)))
                and not any(_names_item(other, thing) for other in damaged)):
            faults.append(clause)
    if len(faults) < (1 if damaged else 2):
        return []
    return [*faults[:_FAULTS_AT_ONCE - 1], "；".join(faults[_FAULTS_AT_ONCE - 1:])] if len(faults) > _FAULTS_AT_ONCE else faults


def fault_intents(text: str) -> tuple[list[str], str, str | None]:
    """(the faults, the main request, an extra question): "音箱没声了 杯子也碎了 另外发票怎么开" keeps its question, while
    "音箱没声了，还有那个耳机也坏了" is not a question after 还有."""
    main, extra = split_intents(text)
    if faults := several_faults(main):
        return faults, main, extra
    if faults := several_faults(text):
        return faults, text, None
    return [], main, extra


def about_my_order(text: str) -> bool:
    """A refund or delivery question about the customer's own order, asked without an order number."""
    if ORDER_ID_RE.search(text):
        return False
    if refund_withdraw_requested(text):  # "取消我的退款申请": which one? (N-127)
        return True
    if _LIST_MINE.search(text) or OTHER_ORDER.search(text):  # "我的订单有哪些", "另一单呢" (N-057, N-177)
        return True
    if asks_refund_status(text):
        return bool(_OWN.search(text) or "单子" in text or _MY_MONEY.search(text))
    if _mentions_refund(text):
        # "我要退款", "我买的那个扫地机器人退货"; not "快递还没到能退款吗" or "我买的东西能退货吗", which ask about policy;
        # not "不对 我不要退款了", whose 要退 is declined (N-198).
        kept = NEGATED_REFUND_RE.sub("，", text)
        return bool(_APPLY.search(kept) or (_OWN.search(text) and not _ASKING.search(text.strip())))
    if fault_report(text):
        return True  # "我上周买的那个音箱 没声了 咋整": which order is it (N-223)
    return bool(_SHIPMENT.search(text) and _MINE.search(text))


_CLAUSE_SEP = re.compile(r"[，,。；;！!？?\s]*(?:另外|还有|以及|顺便问一下|顺便)?[，,\s]*(?=SO)", re.IGNORECASE)


_PIECE_END = re.compile(r"(?<=[，,。；;！!？?\n])|(?<=\.)(?!\d)")
# A piece that only says which order, perhaps with a few closing words: "订单号SO…", "单号是 SO… 谢谢", "SO… 可以吗". Words with
# no number of their own right before it are about that order.
_REFERENCE_ONLY = re.compile(
    r"^[\s，,。；;！!？?:：.]*(?:(?:订单|单)号?(?:是|为)?|这单|那单)?[\s:：]*SO\d+[\s:：]*"
    r"(?:可以吗|行吗|好吗|谢谢|麻烦了|麻烦|对吧|是吧|这单|那单|这个|吗|呢|吧|哈|啊|呀)*[\s，,。；;！!？?.~～]*$", re.IGNORECASE)
ASK_WHICH_REFUND_TEXT = "您提到想退款，但没有写是哪一单：请回复要退的那一单，例如“订单号 申请退款”。"


def _after_each_number(text: str) -> list[tuple[str | None, str]]:
    """Each order number with the words after it, up to the next one; words before the first number on their own."""
    spans = [(m.start(), m.group().upper()) for m in ORDER_ID_RE.finditer(text)]
    head = [(None, text[:spans[0][0]])] if spans and spans[0][0] > 0 else []
    return head + [(oid, text[start:(spans[i + 1][0] if i + 1 < len(spans) else len(text))]) for i, (start, oid) in enumerate(spans)]


def _order_pieces(text: str) -> tuple[dict[str, str], str]:
    """(each order's words, the words that belong to no order for sure). Words after a number belong to it ("SO1 到哪了 另外
    SO2 杯子能退吗"); a sentence or clause with no number belongs to the number right after it when that piece only names the
    order ("抱枕想退，订单号SO2 谢谢"), as customers often put the number last. A clause with no number that sits between two
    other numbers belongs to neither: guessing gave "抱枕想退" to the robot before it (simulations, run 42664792823)."""
    if not ORDER_ID_RE.search(text):
        return {}, ""
    pieces: list[tuple[str | None, str]] = []
    for piece in (p for p in _PIECE_END.split(text) if p):
        pieces.extend(_after_each_number(piece) if ORDER_ID_RE.search(piece) else [(None, piece)])
    owners: list[str | None] = [oid for oid, _ in pieces]
    claimed = [False] * len(pieces)  # number-less words taken by a bare reference after them
    for i in range(len(pieces) - 2, -1, -1):
        after = i + 1
        if owners[i] is None and owners[after] is not None and (claimed[after] or _REFERENCE_ONLY.match(pieces[after][1])):
            owners[i], claimed[i] = owners[after], True
    last_number = max((i for i, (oid, _) in enumerate(pieces) if oid is not None), default=-1)
    last = None
    for i, (oid, _) in enumerate(pieces):  # words after the last number follow it, as before; those between numbers do not
        last = oid if oid is not None else last
        if owners[i] is None and i > last_number:
            owners[i] = last
    clauses: dict[str, str] = {}
    loose = ""
    for owner, (_, words) in zip(owners, pieces):
        if owner is not None:
            clauses[owner] = clauses.get(owner, "") + words
        else:
            loose += words
    return clauses, loose


def order_clauses(text: str) -> dict[str, str]:
    """Each order number with the words that are about it (_order_pieces)."""
    return _order_pieces(text)[0]


def loose_refund(text: str) -> bool:
    """Refund words in a message with several orders that belong to none of them for sure: ask which one, never guess."""
    loose = _order_pieces(text)[1]
    return bool(loose) and _mentions_refund(loose) and not ORDER_ID_RE.search(loose)


_LOOKUP_WORDS = re.compile(r"帮我|麻烦|请|查(?:一下|询|下)?|看(?:一下|看|下)?|订单号?|状态|的|一下|了|吧|呢|啊|这个|那个|这单|我")
_QUESTION_MARK = re.compile(r"怎么|如何|在哪|哪里|能不能|可不可以|可以吗|能吗|吗|[？?]|多少|多久|多长时间|是否|有没有")


def order_question(text: str) -> str:
    """What a message asks besides looking up the order it names ("SO… 的扫地机器人怎么用？说明书在哪"), or ""."""
    rest = ORDER_ID_RE.sub(" ", text)
    if not _QUESTION_MARK.search(rest):
        return ""
    bare = _LOOKUP_WORDS.sub("", re.sub(r"[\s，,。.！!？?~～]+", "", rest))
    return rest.strip(" ，,") if len(bare) >= 3 else ""


def _names_item(text: str, name: str) -> bool:
    """"扫地机器人" names "扫地机器人 X1": any two Chinese characters of the item name in a row."""
    han = re.sub(r"[^\u4e00-\u9fff]", "", name)
    return any(han[i:i + 2] in text for i in range(len(han) - 1))


def _named_orders(text: str, listing: dict[str, Any], exclude: set[str] | frozenset = frozenset()) -> list[dict[str, Any]]:
    return [o for o in listing.get("orders") or [] if o.get("order_id") not in exclude
            and any(_names_item(text, n) for n in o.get("items") or [])]


def fault_tickets(text: str, faults: list[str], listing: dict[str, Any], looked_up: list[dict[str, Any]],
                  exclude: set[str] | frozenset = frozenset()) -> list[dict[str, Any]]:
    """One ticket for each thing reported: on its order when the clause gives the number or names the one delivered order
    it can be, else without one. A number that is not the customer's gets none: the answer says so."""
    known = {o.get("order_id"): o for o in [*(listing.get("orders") or []), *looked_up]}
    tickets, by_order = [], {}
    for clause in faults:
        number = ORDER_ID_RE.search(clause)
        if number:
            order = known.get(number.group().upper())
            if order is None or order.get("error"):
                continue
        else:
            arrived = [o for o in _named_orders(clause, listing, exclude) if o.get("status") == "delivered"]
            order = arrived[0] if len(arrived) == 1 else None
        title = clause[:30]
        if order is not None:
            if order["order_id"] in by_order:  # a second thing wrong with the same order: on its ticket
                by_order[order["order_id"]]["description"] += f"；{clause}"
                continue
            items = "、".join(i.get("name", "") if isinstance(i, dict) else i for i in order.get("items") or [])
            title = f"{order['order_id']} {items} 换货/维修".replace("  ", " ")[:60]
        tickets.append({"title": title, "description": clause, "priority": "high" if _URGENT.search(text) else "normal"})
        if order is not None:
            by_order[order["order_id"]] = tickets[-1]
    return tickets


def pick_my_order(text: str, listing: dict[str, Any], exclude: set[str] | frozenset = frozenset()) -> str | None:
    """The one order a question without a number is about, or None when it has to be asked.
    Only read-only questions are followed up on a guess; a refund is never filed on one."""
    orders = [o for o in listing.get("orders") or [] if o.get("order_id") not in exclude]
    named = _named_orders(text, listing, exclude)
    if named:
        candidates = named
    elif asks_refund_status(text):
        candidates = [o for o in orders if o.get("status") == "refunding"]
    elif _SHIPMENT.search(text) and not _mentions_refund(text):
        candidates = [o for o in orders if o.get("status") == "shipped"]
    else:
        candidates = []
    return candidates[0]["order_id"] if len(candidates) == 1 else None


def wants_human(text: str) -> bool:
    return bool(_HANDOFF.search(_IF_COMPLAIN.sub("", _NEGATED_HANDOFF.sub("", text))))


def chasing(text: str) -> bool:
    """"为什么这么慢", "催一下", or nothing but "那你说怎么办" (after the order number the rewriter put in front)."""
    rest = re.sub(r"[\s，,。.！!？?~～]+", "", ORDER_ID_RE.sub("", text))
    return bool(_CHASE.search(text) or _CHASE_SHORT.fullmatch(rest))


def smalltalk(text: str) -> str | None:
    """The reply to a greeting, a thank-you or a "不用人工了" that asks nothing, or None."""
    stripped = text.strip()
    if not stripped or _GREETING.fullmatch(stripped):
        return GREETING_TEXT
    cancelled, rest = handoff_cancelled(stripped)
    if cancelled and not rest:
        return NO_HANDOFF_TEXT
    return THANKS_TEXT if _THANKS.fullmatch(stripped) else None


def resolve_ordinal(text: str, history: list[LLMMessage]) -> str | None:
    """"第二单" right after a listing of the customer's orders: that order, with what the listing was for. A refund is
    named, not asked for ("SO… 退款"), so it is offered, never filed on a number picked from a list."""
    match = _ORDINAL.fullmatch(text.strip())
    if not match:
        return None
    for i in range(len(history) - 1, -1, -1):
        if history[i].role == "assistant" and LISTING_MARK in history[i].content:
            listed = list(dict.fromkeys(m.upper() for m in ORDER_ID_RE.findall(
                history[i].content.split(LISTING_MARK, 1)[1].split("。", 1)[0])))
            n = len(listed) if match.group(2) else (_ORDINAL_VALUE.get(match.group(1)) or int(match.group(1)))
            if not 1 <= n <= len(listed):
                return None
            asked = next((m.content for m in reversed(history[:i]) if m.role == "user"), "")
            if refund_withdraw_requested(asked):
                intent = "撤回退款申请"
            elif asks_refund_status(asked):
                intent = "退款进度"
            elif _mentions_refund(asked):
                intent = "退款"
            elif _SHIPMENT.search(asked):
                intent = "到哪了"
            else:
                intent = "查一下"
            return f"{listed[n - 1]} {intent}"
    return None


def earlier_orders(history: list[LLMMessage]) -> set[str]:
    return {m.upper() for msg in history if msg.role == "user" for m in ORDER_ID_RE.findall(msg.content)}


def _loads(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return {"raw": text}
    return value if isinstance(value, dict) else {"value": value}


def _clean(sentence: str) -> str:
    return sentence.lstrip("-*• ").strip()


class MockLLM(LLMProvider):
    name = "mock"

    def __init__(self, stream_delay: float = 0.0):
        self.stream_delay = stream_delay

    # ------------------------------------------------------------------ public API
    def chat(self, messages: list[LLMMessage], tools: list[ToolSpec], model: str,
             options: dict[str, Any] | None = None) -> ChatResult:
        options = options or {}
        prompt_tokens = sum(estimate_tokens(m.content) for m in messages)
        if options.get("task") == "kb_patch":
            content = self._kb_patch(messages[-1].content)
            return ChatResult(content, [], Usage(prompt_tokens, estimate_tokens(content), True), model)
        if options.get("task") == "kb_questions":
            content = self._kb_questions(messages[-1].content)
            return ChatResult(content, [], Usage(prompt_tokens, estimate_tokens(content), True), model)

        available = {t.name for t in tools}
        user_idx = max(i for i, m in enumerate(messages) if m.role == "user")
        # A re-ask from the answer check (verifier.reask_message) is not the customer speaking: answer the
        # customer's question again, without the sentences it named, the way a model corrects itself.
        reask = messages[user_idx].content if messages[user_idx].content.startswith(REASK_MARKER) else None
        if reask is not None:
            user_idx = max(i for i, m in enumerate(messages[:user_idx]) if m.role == "user")
        history = [m for m in messages[:user_idx] if m.role in ("user", "assistant")]
        user_text = fix_typos(messages[user_idx].content)
        user_text = resolve_ordinal(user_text, history) or user_text
        exclude = earlier_orders(history) if OTHER_ORDER.search(user_text) else set()
        turn = messages[user_idx + 1 :]
        called = [c.name for m in turn if m.role == "assistant" for c in m.tool_calls]
        results: dict[str, list[dict[str, Any]]] = {}
        for m in turn:
            if m.role == "tool" and m.name:
                results.setdefault(m.name, []).append(_loads(m.content))

        custom = {t.name: t for t in tools if (t.hints or {}).get("custom")}
        call = self._next_call(user_text, called, results, available, custom, exclude)
        if call is not None:
            name, args = call
            tool_call = ToolCall(id=f"mock_{len(called) + 1}_{name}", name=name, arguments=args)
            completion = estimate_tokens(json.dumps(args, ensure_ascii=False))
            return ChatResult("", [tool_call], Usage(prompt_tokens, completion, True), model, "tool_calls")

        content = self._compose(user_text, results, custom, exclude)
        if reask is not None:
            named = {strip_citations(q)[0] for q in re.findall(r"“(.+?)”：", reask)}
            content = "".join(s for s in split_sentences(content) if strip_citations(s)[0] not in named) or REFUSAL
        elif options.get("inject_hallucination"):
            content += HALLUCINATION
        return ChatResult(content, [], Usage(prompt_tokens, estimate_tokens(content), True), model)

    def stream(self, messages, tools, model, options=None) -> Iterator[StreamEvent]:
        result = self.chat(messages, tools, model, options)
        text = result.content
        delay = 0.0 if (options or {}).get("dry_run") else self.stream_delay
        for start in range(0, len(text), 4):
            if delay:
                time.sleep(delay)
            yield StreamEvent(type="delta", text=text[start : start + 4])
        yield StreamEvent(type="done", result=result)

    # ------------------------------------------------------------------ planning
    def _next_call(self, text: str, called: list[str], results: dict, available: set[str],
                   custom: dict[str, ToolSpec] | None = None, exclude: set[str] | None = None):
        if smalltalk(text):
            return None
        faults, main, extra = fault_intents(text) if "create_ticket" in available else ([], *split_intents(text))
        if wants_human(main) and "handoff_to_human" in available and "handoff_to_human" not in called:
            return "handoff_to_human", {"reason": main[:120]}
        plan = plan_custom(main, custom or {}, called, results)
        if plan is not None:
            return plan.call
        call = (self._fault_call(main, faults, called, results, available, exclude or set(), not extra) if faults
                else self._main_call(main, called, results, available, exclude or set()))
        if call is not None:
            return call
        searched = {r.get("query") for r in results.get("search_knowledge") or []}
        if extra and "search_knowledge" in available and extra not in searched:
            return "search_knowledge", {"query": extra}
        return None

    def _fault_call(self, text: str, faults: list[str], called: list[str], results: dict, available: set[str], exclude: set[str],
                    policy: bool):
        if "list_my_orders" in available and "list_my_orders" not in called:
            return "list_my_orders", {}
        listing = (results.get("list_my_orders") or [{}])[-1]
        listed = {o.get("order_id") for o in listing.get("orders") or []}
        unlisted = [m.group().upper() for clause in faults if (m := ORDER_ID_RE.search(clause)) and m.group().upper() not in listed]
        looked_up = results.get("get_order") or []
        if len(looked_up) < len(unlisted) and "get_order" in available:
            return "get_order", {"order_id": unlisted[len(looked_up)]}
        if policy and "search_knowledge" in available and "search_knowledge" not in called:  # or the extra question's search
            return "search_knowledge", {"query": SELF_DAMAGE_QUERY if _SELF_DAMAGE.search(text) else "质量问题 退货 换货"}
        tickets = fault_tickets(text, faults, listing, looked_up, exclude)
        made = len(results.get("create_ticket") or [])
        return ("create_ticket", tickets[made]) if made < len(tickets) else None

    def _main_call(self, text: str, called: list[str], results: dict, available: set[str], exclude: set[str]):
        def can(name: str) -> bool:
            return name in available and name not in called

        order_ids = list(dict.fromkeys(m.upper() for m in ORDER_ID_RE.findall(text)))
        if wants_human(text) and can("handoff_to_human"):
            return "handoff_to_human", {"reason": text[:120]}

        if len(order_ids) > 1:  # look every order up; track each if asked; never guess which to refund
            orders = results.get("get_order") or []
            if len(orders) < len(order_ids) and "get_order" in available:
                return "get_order", {"order_id": order_ids[len(orders)]}
            found = [o["order_id"] for o in orders if not o.get("error")]
            tracked = {t.get("order_id") for t in results.get("track_shipment") or []}
            clauses = order_clauses(text)
            for order_id in found:
                clause = clauses.get(order_id, text)
                if not (_SHIPMENT.search(clause) or _mentions_refund(clause)):
                    clause = text  # "SO1和SO2到哪了": what is asked after the last one is asked of each
                if (order_id not in tracked and _SHIPMENT.search(clause) and not _mentions_refund(clause)
                        and "track_shipment" in available):
                    return "track_shipment", {"order_id": order_id}
            asking = [c for c in clauses.values() if _mentions_refund(c) and not refund_requested(c)]
            if asking and can("search_knowledge") and not refund_requested(text):
                return "search_knowledge", {"query": refund_question(asking[0])}
            return None

        if not order_ids and withdraw_confirmed(text):  # "确认撤回": the tool takes the order from its own offer (N-145)
            return ("withdraw_refund_request", {}) if can("withdraw_refund_request") else None

        if about_my_order(text) and "list_my_orders" in available:
            if can("list_my_orders"):
                return "list_my_orders", {}
            listing = (results.get("list_my_orders") or [{}])[-1]
            # Read-only questions may follow a single match; money never moves on an order we picked.
            fault = fault_report(text) and not refund_requested(text)
            if fault:  # a repair is for something that arrived: not a refunded, unpaid or travelling order (review)
                arrived = [o for o in _named_orders(text, listing, exclude) if o.get("status") == "delivered"]
                picked = arrived[0]["order_id"] if len(arrived) == 1 else None
            else:
                picked = (None if (refund_requested(text) and not asks_refund_status(text)) or refund_withdraw_requested(text)
                          else pick_my_order(text, listing, exclude))
            if picked is None and fault and len(_named_orders(text, listing, exclude)) <= 1:
                pass  # the item is on none of these orders, or not delivered: a ticket without one, as before
            elif picked is None:
                return None  # list the orders and ask which one
            else:
                order_ids = [picked]

        if order_ids:
            order_id = order_ids[0]
            if can("get_order"):
                return "get_order", {"order_id": order_id}
            order = (results.get("get_order") or [{}])[-1]
            if order.get("error"):
                return None
            # "我刚才的退款申请能撤回吗", or "不想退了" while one waits for review: take it back (N-127).
            if (refund_withdraw_requested(text) or withdraw_confirmed(text)
                    or (order.get("refund_awaiting_approval") and refund_bare_decline(text))):
                return ("withdraw_refund_request", {"order_id": order_id}) if can("withdraw_refund_request") else None
            if asks_refund_status(text):  # answer from the order's refunds; never file one
                live = order.get("refund_awaiting_approval") or order.get("status") == "refunded" or any(
                    r.get("status") in ("pending", "processing") for r in order.get("refunds") or [])
                if live and can("search_knowledge"):
                    return "search_knowledge", {"query": "退款 到账 工作日"}
                return None
            items = _item_names(order)
            if (_CANCEL.search(text) or CANCEL_TALK_RE.search(text)) and not _mentions_refund(text, items):  # N-063: an unshipped order is cancelled by refunding it
                if (cancel_requested(text, items) and order.get("status") == "paid" and order.get("refundable")
                        and can("request_refund")):
                    return "request_refund", {"order_id": order_id, "amount": float(order.get("amount", 0)),
                                              "reason": f"顾客要求取消未发货订单：{text[:100]}"}
                return None  # a question about cancelling, or an order already on its way: answered, nothing filed
            # "我买的收纳盒碎了": a fault on the order it found is a report, ticket words or not (N-223, review)
            reported = ((_TICKET.search(text) and (_REPORTED.search(text) or not _ASKING.search(text.strip())))
                        or (not ORDER_ID_RE.search(text) and fault_report(text)))
            repair = ((_REPAIR.search(text) and (_FAULTY.search(text) or not _ASKING.search(text.strip())))
                      or (_SWAP.search(text) and _FAULTY.search(text) and not _NOT_FAULTY.search(text)))
            if (repair or reported) and not refund_requested(text, items):  # N-198: a repair or an exchange is a ticket
                if can("search_knowledge"):
                    return "search_knowledge", {"query": "质量问题 退货 换货"}
                if can("create_ticket"):
                    items = "、".join(i.get("name", "") for i in order.get("items") or [])  # "扫地机器人 X1": who to send
                    return "create_ticket", {"title": f"{order_id} {items} 换货/维修".replace("  ", " ")[:60],
                                             "description": text, "priority": "high" if _URGENT.search(text) else "normal"}
                return None
            if _SELF_DAMAGE.search(text):  # N-200
                return ("search_knowledge", {"query": SELF_DAMAGE_QUERY}) if can("search_knowledge") else None
            if _mentions_refund(text, items):
                if not order.get("refundable", False):
                    return None
                # "先别退款，如果退款的话钱退到哪" asks about a refund: answer it, never file one (N-091).
                if not refund_requested(text, items):
                    if can("search_knowledge"):
                        return "search_knowledge", {"query": refund_question(text)}
                    return None
                if can("search_knowledge"):
                    return "search_knowledge", {"query": "退款 到账 工作日"}
                if can("request_refund"):
                    # A gullible model takes the user's number: the one after 退 ("899 块买的 退 100 块"), never a declined one.
                    # "退我20就行" names 20 without a unit (N-218).
                    stated = stated_refund_amount(text)
                    amount_match = None if stated is not None else _AMOUNT.search(NEGATED_REFUND_RE.sub("，", own_amount_text(text)))
                    amount = (stated if stated is not None
                              else float(re.search(r"\d+(?:\.\d+)?", amount_match.group()).group()) if amount_match
                              else round(float(order.get("amount", 0)) / 2, 2) if _HALF.search(text)
                              else float(order.get("amount", 0)))
                    return "request_refund", {"order_id": order_id, "amount": amount, "reason": text[:120]}
                return None
            if _NOT_RECEIVED.search(text):  # N-157: shown as delivered, never arrived
                if can("track_shipment"):
                    return "track_shipment", {"order_id": order_id}
                if can("create_ticket"):
                    return "create_ticket", {"title": f"{order_id} 显示签收但未收到", "description": text, "priority": "high"}
                return None
            if _CHANGE_INFO.search(text):  # N-157, N-063: nothing the order tools can change
                if can("create_ticket"):
                    return "create_ticket", {"title": f"{order_id} 修改收货信息", "description": text, "priority": "normal"}
                return None
            if (_SHIPMENT.search(text) or chasing(text)) and order.get("status") != "paid":
                if can("track_shipment"):
                    return "track_shipment", {"order_id": order_id}
                shipment = (results.get("track_shipment") or [{}])[-1]
                if _ETA.search(text) and shipment.get("shipped") and not shipment.get("delivered") and can("search_knowledge"):
                    return "search_knowledge", {"query": "发货后 预计 送达"}
                return None
            if _SHIPMENT.search(text) and can("track_shipment"):
                return "track_shipment", {"order_id": order_id}
            if _RUSH.search(text) and can("search_knowledge"):
                return "search_knowledge", {"query": "发货时效 付款后 小时"}
            rest = order_question(text)
            if rest and can("search_knowledge"):  # "SO… 的扫地机器人怎么用", "SO… 能开发票吗": the question itself
                return "search_knowledge", {"query": rest}
            return None

        if _SELF_DAMAGE.search(text):  # "我昨天买的那个杯子摔坏了是我自己摔的能退吗" (N-200): not a fault ticket
            return ("search_knowledge", {"query": SELF_DAMAGE_QUERY}) if can("search_knowledge") else None

        if ((_TICKET.search(text) and (_REPORTED.search(text) or not _ASKING.search(text.strip())))
                or (fault_report(text) and not refund_requested(text))):  # "我买的收纳盒碎了", no order to put it on
            if can("search_knowledge"):
                return "search_knowledge", {"query": text}
            if can("create_ticket"):
                priority = "high" if _URGENT.search(text) else "normal"
                return "create_ticket", {"title": text[:30], "description": text, "priority": priority}
            return None

        if not called and can("search_knowledge"):
            return "search_knowledge", {"query": text}
        return None

    # ------------------------------------------------------------------ answering
    def _compose(self, text: str, results: dict[str, list[dict[str, Any]]],
                 custom: dict[str, ToolSpec] | None = None, exclude: set[str] | None = None) -> str:
        if not any(results.values()) and (reply := smalltalk(text)):
            return reply
        other = other_question(text)
        faults, text, extra = fault_intents(text) if results.get("create_ticket") else ([], *split_intents(text))
        custom_parts = compose_custom(text, results, custom or {})
        searches = results.get("search_knowledge") or []
        extra_search = next((r for r in searches if extra and r.get("query") == extra), None)
        results = {**results, "search_knowledge": [r for r in searches if r is not extra_search]}
        parts: list[str] = []

        def last(name: str) -> dict[str, Any] | None:
            return results[name][-1] if results.get(name) else None

        handoff = last("handoff_to_human")
        if handoff:
            parts.append(handoff.get("message") or handoff.get("error", ""))
        parts.extend(custom_parts)
        # A tenant tool switched off while its application waited, then rejected (N-230): still the customer's answer.
        for name, outputs in results.items():
            if name not in BUILTIN_TOOLS and name not in (custom or {}):
                parts.extend(f"{o.get('message')}{REJECTED_NEXT_STEP}" for o in outputs
                             if o.get("status") == "rejected" and o.get("message"))

        listing = last("list_my_orders")
        if listing and not results.get("get_order") and not results.get("create_ticket"):  # a ticket says what was done
            if exclude:
                listing = {**listing, "orders": [o for o in listing.get("orders") or [] if o.get("order_id") not in exclude]}
            parts.append(self._ask_which(text, listing))

        withdrawal = last("withdraw_refund_request")
        if withdrawal:  # its message says what happened; the order's own summary would only repeat it
            parts.append(withdrawal.get("message") or f"未能撤回退款申请：{withdrawal.get('error', '')}。")
        orders = [] if withdrawal else results.get("get_order") or []
        refund = last("request_refund")
        progress = len(orders) == 1 and not orders[0].get("error") and asks_refund_status(text)
        if progress:
            parts.append(self._refund_progress(orders[0]))
            orders = []
        shipments = results.get("track_shipment") or []
        tracked = {s.get("order_id") for s in shipments if not s.get("error")}
        for order in orders:
            if order.get("error"):  # a wrong number is worth checking; an outage is not
                check = "请核对订单号后再试。" if "订单不存在" in order["error"] else ""
                parts.append(f"未能查询到该订单：{order['error']}。{check}")
            elif not refund and order.get("order_id") not in tracked:
                items = "、".join(f"{i['name']}×{i['qty']}" for i in order.get("items", []))
                parts.append(
                    f"订单 {order['order_id']} 当前状态为“{order['status_text']}”，"
                    f"订单金额 ¥{order['amount']:.2f}，商品：{items}。"
                )
                if len(orders) == 1 and _mentions_refund(text) and not order.get("refundable", False):
                    parts.append(f"该订单暂不能发起退款：{order.get('refund_note', '')}。")
        clauses = order_clauses(text) if len(orders) > 1 else {}

        def asked_of(order: dict[str, Any]) -> str:  # its own words; "SO2 和 SO1 都退了": SO2 shares SO1's
            clause = clauses.get(order.get("order_id"), "")
            return text if _ONLY_A_LINK.fullmatch(ORDER_ID_RE.sub("", clause)) else clause

        # "另外 SO2 杯子能退吗": that order's refund answer (N-094); not for one the customer said not to refund (probe of 1.1.0)
        wanted = [o for o in (orders if len(orders) > 1 else []) if not o.get("error") and _mentions_refund(asked_of(o))
                  and not _NOT_THIS_ONE.search(asked_of(o))]
        # One order asked for, the other only talked about ("SO2 物流太慢了。SO1 退款。"): offered, not "one at a time"
        only_one = len(wanted) == 1 and not any(_mentions_refund(asked_of(o)) for o in orders if o is not wanted[0])
        for order in wanted:
            if not refund_requested(text) or only_one:
                parts.append(f"订单 {order['order_id']} 暂不能发起退款：{order.get('refund_note', '')}。"
                             if not order.get("refundable") else
                             (f"订单 {order['order_id']}：{order['refund_note']}。" if order.get("refund_note") else "")
                             + f"如需为订单 {order['order_id']} 申请退款，回复“{order['order_id']} 申请退款”即可。")
        if len(orders) > 1 and refund_requested(text) and not only_one:
            parts.append(MULTI_REFUND_TEXT)
        elif len(orders) > 1 and loose_refund(text):
            parts.append(ASK_WHICH_REFUND_TEXT)

        for shipment in shipments:
            if shipment.get("error"):
                parts.append(f"物流查询失败：{shipment['error']}。")
            elif shipment.get("message"):
                parts.append(shipment["message"])

        # Only a refund that went through gets the payout timing; a rejected or failed one
        # gets its reason and a next step, never "arrives in 1-3 working days".
        refund_failed = bool(refund) and (bool(refund.get("error")) or refund.get("status") == "rejected"
                                          or refund.get("refund_status") == "pending")  # not paid, or not yet known
        if refund:
            if refund.get("awaiting_review") or refund.get("confirm_order"):
                parts.append(f"{refund['error']}。")
            elif refund.get("error"):
                parts.append(f"退款申请未能提交：{refund['error']}。")
            elif refund.get("status") == "rejected":
                parts.append(refund.get("message", ""))
                parts.append(REJECTED_NEXT_STEP)
            else:
                parts.append(refund.get("message", ""))

        ticket = last("create_ticket")
        knowledge = last("search_knowledge")
        self_damage = bool(_SELF_DAMAGE.search(text))
        eta = bool(shipments) and bool(_ETA.search(text))
        cancel_and_refund = bool(_CANCEL_AND_REFUND_RE.search(ORDER_ID_RE.sub("", normalize_width(text))))  # "还没发货，想取消退款"
        cancelling = bool(_CANCEL.search(text) or CANCEL_TALK_RE.search(text)) or cancel_and_refund
        rush = bool(orders) and not shipments and bool(_RUSH.search(text)) and not cancelling
        # A question about refunds on one of the customer's orders, answered without filing one (N-091).
        inquiry = (len(orders) == 1 and not refund and not orders[0].get("error") and orders[0].get("refundable")
                   and _mentions_refund(text, _item_names(orders[0])) and not refund_requested(text, _item_names(orders[0])))
        if knowledge:
            query = ("退款 到账 工作日" if (refund and not refund_failed) or progress
                     else SELF_DAMAGE_QUERY if self_damage
                     else expand_query(knowledge.get("query") or text) if (eta or rush or ticket)
                     else expand_query(refund_question(text) if inquiry else text))
            if not refund_failed:
                limit = 1 if (refund or ticket or progress or custom_parts or eta or rush or self_damage) else 3
                sentences = self._extract(query, knowledge.get("results") or [], limit)
                if sentences:
                    parts.extend(sentences)
                elif (len(orders) == 1 and not inquiry and not _mentions_refund(text) and (asked := order_question(text))
                      and not (eta or rush or ticket)):  # "SO… 的扫地机器人怎么用" with nothing on it: say so
                    parts.append(f"关于“{asked}”，知识库中没有找到相关依据。")
                elif not parts and not ticket:
                    down = knowledge.get("error") or knowledge.get("degraded")
                    parts.append(SEARCH_DOWN if down else REFUSAL)
        if inquiry and _REFUND_FAILED.search(text) and not any("失败" in p for p in parts):
            # "SO… 退款失败怎么办": the documents say nothing about it; say so, and what this order's ledger shows (probe of 1.1.0)
            ledger = orders[0].get("refunds") or []
            parts.append(REFUND_FAILED_TEXT.format(status=f"订单 {orders[0]['order_id']} 目前没有退款记录。" if not ledger else ""))
        if inquiry:
            oid = orders[0]["order_id"]
            # Only a customer who said "别退 / 不退" is told nothing will be filed; otherwise ask (N-126): an angry
            # customer misread as asking is offered to apply, not told "不会为您发起退款".
            if cancel_and_refund and orders[0].get("status") == "paid":  # to cancel, not to take a refund back
                parts.append(f"如需取消这个订单，回复“{oid} 帮我取消订单”即可，未发货的订单按全额退款提交审核。")
            elif NEGATED_REFUND_RE.search(normalize_width(text)):
                parts.append(f"如需退款，可以直接回复“{oid} 申请退款”，这次只解答问题、不会为您发起退款。")
            elif reads_as_request(text):  # they want it, in words the grammar leaves out: asked outright, and a yes files it
                parts.append(f"需要为订单 {oid} 申请退款吗？如需提交，回复“是的”或“{oid} 申请退款”即可。")
            else:
                parts.append(f"如需现在申请退款，回复“{oid} 申请退款”即可，我马上为您提交。")
        if faults:  # which is which: the tickets' own sentences say only their numbers
            things = fault_tickets(text, faults, last("list_my_orders") or {}, results.get("get_order") or [], exclude or set())
            found = [t["title"].removesuffix(" 换货/维修") for t in things if t["title"].endswith(" 换货/维修")]
            loose = [t["description"] for t in things if not t["title"].endswith(" 换货/维修")]
            if len(found) > 1 or (found and loose):
                parts.append(f"这几件分开处理：{'、'.join(found)}。")
            if loose:
                parts.append(f"如有{'、'.join(f'“{c}”' for c in loose)}对应的订单号，可以回复给我，方便同事核对。")
        for made in results.get("create_ticket") or []:
            parts.append(made.get("message") or f"工单创建失败：{made.get('error', '')}。")
        if self_damage:
            parts.append("如需协商处理，可以回复“转人工”。")
        if shipments and chasing(text) and not any(s.get("delivered") for s in shipments):
            parts.append(CHASE_TEXT)
        if rush:
            parts.append(RUSH_TEXT)
        if len(orders) == 1 and not refund and cancelling and not _mentions_refund(text) and not orders[0].get("error"):
            oid = orders[0]["order_id"]
            parts.append(f"如需取消这个订单，回复“{oid} 帮我取消订单”即可，未发货的订单按全额退款提交审核。"
                         if orders[0].get("status") == "paid" and orders[0].get("refundable") else
                         "如需取消：这个订单已经发货或不能在线退款，不能直接取消；签收后可以按售后政策申请退货退款，或回复“转人工”。")
        if _IF_COMPLAIN.search(text) and not handoff:
            parts.append(IF_COMPLAIN_TEXT)

        if extra_search is not None:
            # The renewal question next to this month's refund (N-179): one sentence, "当前会员期仍然有效" would contradict it
            sentences = self._extract(expand_query(extra), extra_search.get("results") or [],
                                      1 if refund and extra == other else 2)
            parts.append(f"另外，{''.join(sentences)}" if sentences else f"关于“{extra}”，知识库中没有找到相关依据。")

        if not parts:
            parts.append(REFUSAL)
        return "".join(p for p in parts if p)

    @staticmethod
    def _ask_which(text: str, listing: dict[str, Any]) -> str:
        if listing.get("error"):
            return f"暂时查不到您的订单：{listing['error']}。也可以直接告诉我订单号。"
        orders = listing.get("orders") or []
        if not orders:
            return "没有查到您账号下的订单。如果是用其他账号下的单，请告诉我订单号。"
        lines = "；".join(f"{o['order_id']}（{'、'.join(o.get('items') or [])}，{o['status_text']}，¥{o['amount']:.2f}）"
                         for o in orders)
        if refund_withdraw_requested(text):
            return (f"您账号下最近的订单有：{lines}。请问要撤回的是哪一单的退款申请？"
                    f"请回复“订单号 撤回退款申请”，例如“{orders[0]['order_id']} 撤回退款申请”。")
        if _mentions_refund(text) and not asks_refund_status(text):
            return (f"您账号下最近的订单有：{lines}。请问要申请退款的是哪一单？"
                    f"请回复“订单号 申请退款”，例如“{orders[0]['order_id']} 申请退款”。")
        if asks_refund_status(text) and not any(o.get("status") == "refunding" for o in orders):  # N-092
            return (f"没有查到您账号下正在处理中的退款。最近的订单有：{lines}。"
                    "如果问的是其中一单的退款，回复订单号即可帮您查。")
        if _FAULTY.search(text):  # "我买的东西坏了" (N-223)
            return f"您账号下最近的订单有：{lines}。请问出问题的是哪一单？回复订单号即可帮您处理。"
        return f"您账号下最近的订单有：{lines}。请问要查询的是哪一单？回复订单号即可。"

    @staticmethod
    def _refund_progress(order: dict[str, Any]) -> str:
        oid = order["order_id"]
        lines: list[str] = []
        waiting = order.get("refund_awaiting_approval")
        if waiting:
            lines.append(f"订单 {oid} 有一笔 ¥{waiting['amount']:.2f} 的退款申请（{waiting['filed_on']} 提交）正在等待人工审核，"
                         "审核通过后会按原支付方式退回。")
        for r in (order.get("refunds") or [])[:3]:
            lines.append(f"订单 {oid} 的退款单 #{r['refund_id']}（¥{r['amount']:.2f}，{r['filed_on']} 提交）{r['status_text']}。")
        if not lines and order.get("status") == "refunded":  # refunded in the order system, not through this chat
            lines.append(f"订单 {oid} 的状态为“已退款”，款项已按原支付方式退回。如超过到账时效仍未收到，可以回复“转人工”帮您核查。")
        if not lines:
            lines.append(f"订单 {oid} 当前状态为“{order['status_text']}”，没有查到在线提交的退款记录。")
            if order.get("refundable"):
                lines.append(f"如需退款，可以直接回复“{oid} 申请退款”。")
        return "".join(lines)

    @staticmethod
    def _extract(query: str, hits: list[dict[str, Any]], limit: int) -> list[str]:
        """Pick the sentences that best cover the query, only from strongly relevant hits."""
        query_tokens = token_set(query)
        if not hits or not query_tokens:
            return []
        top = max(float(h.get("relevance", 0)) for h in hits)
        scored: list[tuple[float, int, int, str]] = []
        for rank, hit in enumerate(hits):
            if float(hit.get("relevance", 0)) < 0.6 * top:
                continue
            heading_tokens = token_set(hit.get("heading", ""))
            for pos, raw in enumerate(split_sentences(hit.get("text", ""))):
                sentence = _clean(raw)  # a window that starts mid-clause keeps its "…": it is not a sentence start (N-234)
                if len(sentence) < 6:
                    continue
                sentence_tokens = token_set(sentence)
                direct = len(query_tokens & sentence_tokens) / len(query_tokens)
                context = len(query_tokens & (heading_tokens - sentence_tokens)) / len(query_tokens)
                score = direct + 0.3 * context
                if score >= 0.34:
                    scored.append((score - rank * 0.05, hit["ref"], pos, sentence))
        # A window a length cut ended ("…空调安装费为…") is half a sentence: quoted only when nothing whole answers.
        if any(not s[3].endswith(CUT_MARK) for s in scored):
            scored = [s for s in scored if not s[3].endswith(CUT_MARK)]
        scored = [(score, ref, pos, sentence.removesuffix(CUT_MARK)) for score, ref, pos, sentence in scored]
        scored.sort(key=lambda s: -s[0])
        picked: list[tuple[int, int, str]] = []
        seen: set[str] = set()
        for _, ref, pos, sentence in scored:
            if sentence in seen:
                continue
            seen.add(sentence)
            picked.append((ref, pos, sentence))
            if len(picked) == limit:
                break
        picked.sort()
        return [f"{sentence.rstrip('。；;')}[{ref}]。" for ref, _, sentence in picked]

    @staticmethod
    def _kb_questions(prompt: str) -> str:
        """Three questions from the section title: enough to exercise the review flow offline. Real
        colloquial wordings need a real model (the generator runs on GitHub Actions)."""
        match = re.search(r"^标题：(.*)$", prompt, re.M)
        topic = (match.group(1) if match else "").split(" > ")[-1].strip() or "这个问题"
        return json.dumps({"questions": [f"{topic}是怎么规定的？", f"想问一下{topic}", f"{topic}有什么要求？"]},
                          ensure_ascii=False)

    @staticmethod
    def _kb_patch(prompt: str) -> str:
        match = re.search(r"\{.*\}", prompt, re.S)
        data = _loads(match.group(0)) if match else {}
        question = str(data.get("question", "")).strip().rstrip("？?")
        correction = str(data.get("correction", "")).strip()
        return f"## {question}\n\n{correction}\n"


# ---------------------------------------------------------------- tenant-declared tools
# The offline model has no language understanding. For a tool a tenant declared (app/tooling) it
# goes by what the tool declares: the phrases that call for it (hints.intents), the read tools it
# needs first (hints.requires), and its parameters' patterns, enums and defaults. A real model reads
# the description instead. A write or money tool is not called for a question about it ("如果退年费会
# 怎样", "能退吗"): only for a request.
_HYPOTHETICAL = re.compile(r"如果|要是|假如|假设|万一|的话|能不能|可不可以|可以吗|能吗|吗[？?]?$|什么条件|怎么(?:申请|办理)|多久")
_CURRENCY_AMOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:港币|港元|元|块|HKD|CNY|RMB|USD|美元|MOP)", re.IGNORECASE)


class CustomPlan:
    def __init__(self, call: tuple[str, dict[str, Any]] | None, missing: tuple[str, str] | None = None):
        self.call, self.missing = call, missing


def _matches(text: str, spec: ToolSpec) -> int:
    norm = normalize_width(text).lower()
    return sum(1 for phrase in (spec.hints or {}).get("intents") or [] if phrase.lower() in norm)


def _best(text: str, custom: dict[str, ToolSpec]) -> ToolSpec | None:
    scored = [(_matches(text, spec), -i, spec) for i, spec in enumerate(custom.values())]
    scored = [s for s in scored if s[0] > 0]
    if not scored:
        return None
    best = max(scored, key=lambda s: (s[0], s[1]))[2]
    if (best.hints or {}).get("risk") in ("write", "money") and _HYPOTHETICAL.search(text.strip()):
        return None  # a question about the action: the knowledge base answers it
    return best


def _fill(spec: ToolSpec, text: str, results: dict[str, list[dict[str, Any]]]) -> tuple[dict[str, Any], str | None]:
    """Arguments from the customer's words and earlier results; the first required one missing."""
    props = (spec.parameters or {}).get("properties") or {}
    required = (spec.parameters or {}).get("required") or []
    earlier = [r for name in (spec.hints or {}).get("requires") or [] for r in results.get(name) or []
               if not r.get("error")]
    norm = normalize_width(text)
    args: dict[str, Any] = {}
    for name, prop in props.items():
        value: Any = None
        if prop.get("pattern"):  # "^HB\\d{8}$" finds "HB10020031" (or "hb10020031") in the sentence
            pattern = prop["pattern"].lstrip("^").rstrip("$")
            match = re.search(pattern, norm) or re.search(pattern, norm.upper())
            if match:
                value = match.group(0)
        if value is None and prop.get("enum"):
            # In the order they are said, each value once: "HKD 换 CNY" gives base HKD and quote CNY.
            mentioned = sorted((norm.lower().find(str(e).lower()), e) for e in prop["enum"]
                               if str(e).lower() in norm.lower())
            value = next((e for _, e in mentioned if e not in args.values()), None)
        if value is None and prop.get("type") in ("number", "integer"):
            match = _CURRENCY_AMOUNT.search(norm)
            if match and name in ("amount", "value"):
                value = float(match.group(1))
        if value is None:
            value = next((r[name] for r in reversed(earlier) if r.get(name) is not None), None)
        if value is None and "default" in prop:
            continue  # the tool fills its own default
        if value is None:
            if name in required:
                return args, prop.get("description") or name
            continue
        args[name] = value
    return args, None


def plan_custom(text: str, custom: dict[str, ToolSpec], called: list[str],
                results: dict[str, list[dict[str, Any]]]) -> CustomPlan | None:
    """The next call for a tenant tool, a plan that stops (ask, or explain an error), or None when no
    declared tool fits and the built-in planning goes on."""
    spec = _best(text, custom)
    if spec is None:
        return None
    steps = [*((spec.hints or {}).get("requires") or []), spec.name]
    for name in steps:
        if name in called:
            outputs = results.get(name) or []
            if outputs and outputs[-1].get("error"):
                return CustomPlan(None)  # the answer explains the error; nothing after it runs
            continue
        step = custom.get(name)
        if step is None:
            return CustomPlan(None)
        args, missing = _fill(step, text, results)
        if missing:
            return CustomPlan(None, (step.name, missing))
        return CustomPlan((name, args))
    return CustomPlan(None)


def _label(spec: ToolSpec | None, name: str) -> str:
    return str((spec.hints or {}).get("label") or name) if spec else name


def compose_custom(text: str, results: dict[str, list[dict[str, Any]]], custom: dict[str, ToolSpec]) -> list[str]:
    parts: list[str] = []
    spec = _best(text, custom)
    if spec is not None:
        plan = plan_custom(text, custom, [n for n in custom if n in results], results)
        if plan is not None and plan.missing and plan.missing[0] not in results:
            return [f"请提供{plan.missing[1]}，我来为您办理{_label(custom.get(plan.missing[0]), plan.missing[0])}。"]
    for name, spec in custom.items():
        for output in results.get(name) or []:
            label = _label(spec, name)
            if output.get("error"):
                line = f"{label}未能办理：{output['error']}。"
                if output.get("restricted"):
                    line += f"{output.get('appeal', '')}"
                parts.append(line)
            elif output.get("status") == "rejected":
                parts.append(str(output.get("message") or f"{label}未通过人工审核。"))
                parts.append(REJECTED_NEXT_STEP)
            elif output.get("message"):
                parts.append(str(output["message"]))
            elif (spec.hints or {}).get("risk") == "read":
                facts = "，".join(f"{k}：{v}" for k, v in output.items() if not isinstance(v, (dict, list)))
                if facts:
                    parts.append(f"{label}结果：{facts}。")
    return parts
