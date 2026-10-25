"""The cheap tiers of the tiered answer check: how well a claim is grounded, and what rules say about it.

Three signals, none of them a model:
- ``sentence_score``: share of the claim's content words found in the evidence (or the customer's own
  message). Framing words ("系统提示", "抱歉", "为您") do not count, nor does a quote the evidence contains
  word for word ("系统提示“订单不存在或无权查看”" was failed for quoting the tool), nor numbers and order
  ids, which have their own rules. Tool results count with what their fields mean ("refundable": true is
  "可申请退款"), and the ways of saying "not yet" (尚未, 还没有, 暂无) count as one.
- ``clause_score``: the weakest claim clause. A sentence can be grounded overall and still carry one
  invented clause ("派送中，快递员正在派件，预计很快送达"): on the dev split most of the wrong claims the
  lexical check missed looked like that. A label ("现有规则是：") is not a clause.
- ``flags``: one changed word that turns a grounded sentence wrong, found by comparing the claim with the
  evidence sentence or clause it best matches: a flipped polarity (可指定 / 不支持指定), the other party
  paying (由商家承担 / 由买家承担), another member of the same set (免费版 / 专业版, 每周 / 每月, 多次 / 一次),
  another anchor for a deadline (下单后 7 天 / 签收后 7 天), another time unit (1 个工作日 / 1 小时), a
  scope the evidence does not give (任何团队成员), a promise (保证, 马上到账) or a relative day (今天).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..textutil import ORDER_ID_RE, normalize_width, split_sentences, token_set
from .claims import CLAUSE_SPLIT, claimless_clause

# Words that frame a claim without being part of it.
FRAMING = {
    "系统", "提示", "显示", "返回", "结果", "查询", "查到", "不到", "抱歉", "根据", "按照", "规定", "政策", "说明",
    "注意", "留意", "为您", "帮您", "为你", "帮你", "我们", "本店", "平台", "当前", "现在", "已经", "另外", "此外",
    "因此", "所以", "也就是", "温馨", "提醒", "您好", "订单号", "相关", "具体", "以下", "如下", "这边", "一般", "通常",
    "正常", "情况", "信息", "进度", "状态", "目前", "需要", "可以", "能够", "进行", "处理", "一下", "这样", "那么",
    "规则", "现有", "常规", "此前", "之前", "节点", "轨迹", "历史", "前序", "最新", "显示", "记录", "暂时", "尚且",
}
# "Not yet" and "none" said many ways; polarity itself is checked by the flags, not by word overlap.
_NOT_YET = re.compile(r"尚未|还未|还没有|还没|暂未|仍未|没有|并未|未曾")
_NONE = re.compile(r"暂无|暂时没有|目前没有|还没有|尚无")
_QUOTE = re.compile(r"“([^”]{2,})”|「([^」]{2,})」|\"([^\"]{2,})\"|『([^』]{2,})』")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_MARKUP = re.compile(r"\*\*|__|`|^#+\s*|^\s*[-*•·]\s+", re.M)


def normalize(text: str) -> str:
    text = _MARKUP.sub("", normalize_width(text))
    return _NOT_YET.sub("未", _NONE.sub("无", text))


def _content(text: str) -> set[str]:
    return {t for t in token_set(normalize(text)) if t not in FRAMING and not _NUMBER.fullmatch(t)
            and not ORDER_ID_RE.fullmatch(t)}


# ---------------------------------------------------------------- tool fields in words
def gloss(title: str, text: str) -> str:
    """What a tool result's fields mean, in the words an answer would use."""
    try:
        output = json.loads(text)
    except (TypeError, ValueError):
        return ""
    if not isinstance(output, dict):
        return ""
    words: list[str] = []
    if output.get("error"):
        words.append("未能 查询 无法 办理 订单号 有误 不属于 当前 账号")
    if title in ("get_order", "list_orders", "list_my_orders"):
        words.append("订单 商品 金额 实付 付款 支付 日期 下单 签收 送达 状态")
    if title == "get_order" and "refundable" in output:
        words.append("可以 申请 退款 退货 可退款" if output["refundable"] else "不能 无法 退款 不可退款")
    if output.get("shipped") is False:
        words.append("未发货 无 物流 信息 运单 单号 暂无 物流单号 还没 发货")
    if output.get("shipped"):
        words.append("已发货 发出 物流 运单 包裹 派送 配送 运输 途中 快递")
    if title == "create_ticket" and not output.get("error"):
        words.append("工单 已创建 已登记 报修 技术支持 联系")
    if title == "handoff_to_human" and not output.get("error"):
        words.append("转接 人工客服 排队 人工 接入 回复 留言 专人")
    if title == "request_refund" and not output.get("error"):
        words.append("退款 申请 提交 受理 原支付方式 退回 金额")
        if output.get("status") == "auto_approved":
            words.append("自动 审批 审核 通过 系统")
    return " ".join(words)


@dataclass
class Grounding:
    sentence_score: float
    clause_score: float
    best_ref: int | None
    weakest_clause: str = ""
    flags: list[str] = field(default_factory=list)


def _unquoted(claim: str, corpus: str) -> str:
    """The claim without what it quotes from the evidence word for word."""
    def drop(match: re.Match) -> str:
        quoted = next(g for g in match.groups() if g)
        return " " if normalize_width(quoted) in corpus else match.group(0)
    return ORDER_ID_RE.sub(" ", _QUOTE.sub(drop, claim))


def _clauses(text: str) -> list[str]:
    """Claim clauses, without labels: what comes before a colon ("现有规则是：…") is not a claim."""
    parts = re.split(r"([，,；;：:—]+)", text)
    out = []
    for i in range(0, len(parts), 2):
        clause = parts[i].strip()
        after = parts[i + 1] if i + 1 < len(parts) else ""
        if clause and not (re.search(r"[：:]", after) and len(clause) <= 12):
            out.append(clause)
    return out


def ground(claim: str, items: list[tuple[int, str]], customer_text: str, all_text: str) -> Grounding:
    """``items``: (ref, text) of the evidence the claim cites, or all of it; ``all_text``: every evidence text,
    tool glosses included."""
    corpus = normalize_width(all_text)
    rest = _unquoted(normalize_width(claim), corpus)
    tokens = _content(rest)
    customer = _content(customer_text)
    best, best_ref = (1.0, None) if not tokens else (0.0, None)
    if tokens:
        for ref, text in items:
            known = _content(text) | customer
            score = len(tokens & known) / len(tokens)
            if score > best:
                best, best_ref = score, ref
    everything = _content(corpus) | customer
    weakest, weakest_text = 1.0, ""
    for clause in _clauses(rest):
        words = _content(clause)
        if len(words) < 2 or claimless_clause(clause):
            continue
        score = len(words & everything) / len(words)
        if score < weakest:
            weakest, weakest_text = score, clause
    return Grounding(round(best, 3), round(weakest, 3), best_ref, weakest_text, flags(claim, corpus))


# ---------------------------------------------------------------- rule flags
_NEGATOR = r"不|无法|不能|不可|不可以|不会|无需|不需要|不予|暂不|未|没有|不得|禁止"
_CAN = r"支持|可以|可|能|允许|能够"
_POLARITY = re.compile(rf"({_NEGATOR})?(?:再|在线|直接|自行)?({_CAN}|需要|需|必须)")
_BARE_NEG = re.compile(r"无法|不能|不可以|不可|不支持|不在.{0,14}范围|不属于.{0,10}范围|除外")
_PARTY = {"buyer": r"买家|顾客|消费者|客户|用户|您|你|个人", "seller": r"卖家|商家|商户|店铺|本店|我们|平台|公司|企业"
          r"|[A-Z][A-Za-z]+(?:优选|云文档|音乐|商城|官方)?", "carrier": r"快递公司|物流公司|承运商|快递员"}
_PAYS = re.compile(r"由?(" + "|".join(_PARTY.values()) + r")(?:方)?(?:需|需要)?(?:先行|先|来|自行|自己)?"
                   r"(?:承担|支付|负责|出|垫付|付)")
_UNIT = re.compile(r"(\d+(?:\.\d+)?)(?:\s*[-~～至到]\s*(\d+(?:\.\d+)?))?\s*(?:个)?\s*(工作日|自然日|天|日|小时|分钟|周|个月|月|年)")
_SAME_UNIT = {"自然日": "天", "日": "天", "月": "个月"}
_ANCHORS = {"签收": "received", "收货": "received", "确认收货": "received", "收到": "received", "下单": "ordered",
            "付款": "paid", "支付": "paid", "发货": "shipped", "开具": "issued", "开票": "issued", "申请": "applied"}
_ANCHOR = re.compile(r"(确认收货|签收|收货|收到|下单|付款|支付|发货|开具|开票|申请)(?:之日)?(?:后|起|以来)[^\d，。；]{0,4}(\d+(?:\.\d+)?)")
# The same action allowed from another event: "付款后即可开具" where the policy says "确认收货后可开具".
_ACTION_ANCHOR = re.compile(r"(确认收货|签收|收货|收到|下单|付款|支付|发货)(?:之日)?(?:后|起)[^。；]{0,24}?(开具|开票|申请发票|退货|退款|换货|修改|寄出|评价)")
# Members of one set: a claim naming another member than the evidence it matches is a swap.
_SETS = [
    ("plan", ["免费版", "专业版", "企业版", "基础版", "旗舰版"]),
    ("member", ["普通会员", "豪华会员", "黄金会员", "白金会员"]),
    ("pay", ["微信支付", "支付宝", "银行卡", "信用卡", "花呗", "云闪付", "现金"]),
    ("channel", ["人工客服", "邮箱", "官网", "客服热线", "电话", "小程序", "在线客服", "App"]),
    ("role", ["管理员", "团队成员", "普通成员", "所有成员", "创建者"]),
    ("invoice", ["电子普通发票", "增值税专用发票", "纸质发票"]),
    ("count", ["一次", "两次", "三次", "多次", "不限次数", "无限次"]),
    ("period", ["每天", "每日", "每周", "每月", "每年"]),
    ("carrier", ["Atlas快运", "Orion快递"]),
    ("per", ["每个团队", "每个成员", "每人", "每个账号", "每个用户", "每个客户", "每单", "每笔", "每个订单"]),
]
_SCOPE = re.compile(r"(所有|任何|全部|一切)(的)?(用户|会员|成员|团队成员|商品|订单|人|顾客|客户|版本|地区|情况)"
                    r"|个人和企业|企业和个人|无论|不论|(?:和|与|及|、)[^，。；]{1,8}?(?:中|里)?(?:均|都)(?:可|能|会|在|支持|需|为)"
                    r"|长期有效|永久有效|终身有效|永久|终身")
_PROMISE = re.compile(r"保证|一定(?:会|能|可以)|肯定(?:会|能)|必定|绝对|百分之百|马上到账|立即到账|立刻到账|秒到|很快(?:就)?(?:送达|到账|到|发货)"
                      r"|当天(?:到账|送达|发货)|全额退")
_RELATIVE_DAY = re.compile(r"今天|今日|明天|明日|昨天|后天|本周|下周|今晚")


def _role(party: str) -> str:
    return next((role for role, words in _PARTY.items() if re.fullmatch(words, party)), "seller")


def _polarity(text: str) -> dict[str, set[bool]]:
    """{"can": {True}} for "可以 X", {"can": {False}} for "不支持 X", "无法 X", "不在…范围内"; "need" likewise."""
    found: dict[str, set[bool]] = {}
    text = re.sub(r"可能|可惜|可是|只能确认|可参考", "，", text)  # "可能是订单号有误" is not "can"
    if _NO_NEED.search(text):
        found.setdefault("need", set()).add(False)
        text = _NO_NEED.sub("，", text)
    for neg, verb in _POLARITY.findall(text):
        kind = "need" if verb in ("需要", "需", "必须") else "can"
        found.setdefault(kind, set()).add(not neg)
    if re.search(r"请(?!勿|不要|放心|注意|留意|耐心|稍等|您)(?:先|务必)?(?:上传|提供|携带|保持|填写|提交|寄回|保留)", text):
        found.setdefault("need", set()).add(True)  # an instruction in the policy: it is needed
    if _BARE_NEG.search(text):
        found.setdefault("can", set()).add(False)
    if re.search(r"不在.{0,14}范围|不属于.{0,10}范围", text):  # "不在支持范围内": the 支持 is negated
        found["can"] = {False}
    return found


_NO_NEED = re.compile(r"无需|不需要|不用|不必|免于")


def _units(text: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for low, high, unit in _UNIT.findall(text):
        for n in (low, high):
            if n:
                found.setdefault(n, set()).add(_SAME_UNIT.get(unit, unit))
    return found


def _strip(text: str, words) -> str:
    for w in sorted(words, key=len, reverse=True):
        text = text.replace(w, " ")
    return text


def _ranked(claim: str, pieces: list[str], strip=()) -> list[tuple[int, str]]:
    """Evidence pieces by how many content words they share with the claim (``strip``: words that do not count)."""
    words = _content(_strip(claim, strip))
    scored = [(len(words & _content(_strip(p, strip))), p) for p in pieces]
    return sorted((s for s in scored if s[0] > 0), key=lambda s: -s[0])


def _top(ranked: list[tuple[int, str]]) -> list[str]:
    return [p for score, p in ranked if score == ranked[0][0]] if ranked else []


_PARTY_WORDS = [w for words in _PARTY.values() for w in words.split("|")]
# A charge with no payer named is the customer's: "偏远地区需额外支付15元运费", "不满99元收取8元运费".
_BUYER_PAYS = re.compile(r"需(?:要)?(?:额外|另外|自行)?(?:支付|付|承担)|另加|另收|收取|加收|自付")
_CONCESSIVE = re.compile(r"即使|即便|哪怕|就算|无论是否|不管是否")


def best_sentence_score(claim: str, corpus: str) -> float:
    """Share of the claim's content words found in its single best evidence sentence. Low while the whole
    evidence grounds the claim means facts were spliced from different sentences ("付款方式为微信支付、支付宝和
    银行卡" built from three refund lines)."""
    words = _content(claim)
    if not words:
        return 1.0
    return max((len(words & _content(s)) / len(words) for s in split_sentences(normalize(corpus))), default=0.0)


_NEGATED_WORD = re.compile(r"(?:不会|不能|无法|不|没有|未|无需|不再|永不)([\u4e00-\u9fff]{2})")


def _negated_words(text: str) -> tuple[set[str], set[str]]:
    """(words said negated, words said plainly) among two-character words: "不会过期清零" negates 过期."""
    negated = {m.group(1) for m in _NEGATED_WORD.finditer(text)}
    plain = {text[i:i + 2] for i in range(len(text) - 1)} - negated
    return negated, plain


def _clause_flags(clause: str, sentences: list[str], clauses: list[str], corpus: str) -> list[str]:
    out: list[str] = []
    # a word the claim negates that its best-matching evidence clause states plainly, or the reverse
    ranked = _ranked(clause, clauses)
    if ranked and ranked[0][0] >= 2:
        mine_neg, _ = _negated_words(clause)
        for best in _top(ranked):
            their_neg, their_plain = _negated_words(best)
            their_plain = {w for w in their_plain if w not in their_neg}
            if (mine_neg & their_plain) - their_neg:
                return [f"polarity:{'/'.join(sorted((mine_neg & their_plain) - their_neg))}"]
    # polarity: compared with the best-matching evidence clause (or sentence) about the same kind of thing
    for kind, values in _polarity(clause).items():
        for pieces in (clauses, sentences):
            candidates = [(score, p) for score, p in _ranked(clause, pieces) if kind in _polarity(p)]
            if candidates and candidates[0][0] >= 2:
                top = candidates[0][0]
                theirs = set().union(*(_polarity(p)[kind] for score, p in candidates if score == top))
                if not (values & theirs):
                    out.append(f"polarity:{kind}")
                break
        if out:
            return out
    # party: who pays, against the best-matching evidence clause that says who pays
    claimed = {_role(p) for p in _PAYS.findall(clause)}
    if claimed:
        paying = [(score, c) for score, c in _ranked(clause, clauses, _PARTY_WORDS)
                  if _PAYS.search(c) or _BUYER_PAYS.search(c)]
        stated = {_role(p) for c in _top(paying) for p in _PAYS.findall(c)}
        stated |= {"buyer" for c in _top(paying) if not _PAYS.search(c) and _BUYER_PAYS.search(c)}
        if stated and not (claimed & stated):
            return [f"party:{'/'.join(sorted(claimed))}"]
    # set members: another plan, payment method, channel, count or period than the clause it matches
    for name, members in _SETS:
        said = {m for m in members if m in clause}
        if not said:
            continue
        ranked = _ranked(clause, clauses, members)
        if not ranked or ranked[0][0] < 2:  # too little in common to say which evidence it restates
            continue
        best = _top(ranked)
        named = {m for c in best for m in members if m in c}
        extra = said - named
        if named and extra:
            return [f"{name if name in ('count', 'period') else 'entity'}:{'/'.join(sorted(extra))}"]
    return out


def _unit_flags(claim: str, sentences: list[str], corpus: str) -> list[str]:
    """The claim's number with another unit than its best-matching evidence sentence gives it."""
    for n, units in _units(claim).items():
        local = [p for p in _top(_ranked(claim, [s for s in sentences if n in _units(s)]))]
        theirs = set().union(*(_units(p)[n] for p in local)) if local else _units(corpus).get(n, set())
        if theirs and not (units & theirs):
            return [f"unit:{n}{'/'.join(sorted(units))}"]
    return []


# A refund to an account the customer names, where the policy says the original payment method: the
# classic way a scam asks for money to be "refunded" somewhere else.
_OTHER_ACCOUNT = re.compile(r"[您你](?:提供|指定|填写|告知)的(?:银行卡|账户|卡|账号|支付宝|微信)|(?:退到|退回到|打到|转到)(?:其他|另一张|新的|别的)")
_ORIGINAL_ROUTE = re.compile(r"原支付方式|原路退回|原路返回")


def flags(claim: str, corpus: str) -> list[str]:
    claim = normalize(claim)
    corpus = normalize(corpus)
    sentences = split_sentences(corpus)
    clauses = [c.strip() for s in sentences for c in CLAUSE_SPLIT.split(s) if c.strip()]
    out: list[str] = []
    for clause in [c.strip() for c in re.split(r"[，,；;：:—（）()]+", claim) if c.strip()]:
        out += [f for f in _clause_flags(clause, sentences, clauses, corpus) if f not in out]
    out += [f for f in _unit_flags(claim, sentences, corpus) if f not in out]
    if _OTHER_ACCOUNT.search(claim) and _ORIGINAL_ROUTE.search(corpus) and not re.search(
            r"(不能|无法|不可|不支持|不可以)(指定|改|更换)", claim):
        out.append("entity:destination")
    # anchor: the same deadline counted from another event
    evidence_anchors: dict[str, set[str]] = {}
    for word, n in _ANCHOR.findall(corpus):
        evidence_anchors.setdefault(n, set()).add(_ANCHORS[word])
    for word, n in _ANCHOR.findall(claim):
        if n in evidence_anchors and _ANCHORS[word] not in evidence_anchors[n]:
            out.append(f"anchor:{word}{n}")
            break
    action_anchors: dict[str, set[str]] = {}
    for word, action in _ACTION_ANCHOR.findall(corpus):
        action_anchors.setdefault(action, set()).add(_ANCHORS[word])
    for word, action in _ACTION_ANCHOR.findall(claim):
        if action in action_anchors and _ANCHORS[word] not in action_anchors[action] and not any(a.startswith("anchor") for a in out):
            out.append(f"anchor:{word}后{action}")
            break
    for pattern, name in ((_SCOPE, "scope"), (_CONCESSIVE, "condition"), (_PROMISE, "promise"),
                          (_RELATIVE_DAY, "relative_day")):
        match = pattern.search(claim)
        if name == "relative_day" and match and re.match(r"\s*[（(]\s*(\d{4}-)?\d{1,2}-\d{1,2}", claim[match.end():]):
            continue  # "今天（2026-10-20）": the date itself is checked as numbers
        if match and match.group(0) not in corpus:
            out.append(f"{name}:{match.group(0)}")
    return out
