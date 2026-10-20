"""Prompt-injection and fraud heuristics.

Three places use this:
- ingestion (``scan_knowledge``): a knowledge chunk that tries to instruct the model, or that
  tells customers to hand over a card number or a verification code, to add a private WeChat or
  QQ, or to pay a deposit, is held back until an administrator has reviewed it. Indirect
  injection is the more dangerous kind for RAG: knowledge comes from suppliers, old wikis and
  manuals that nobody reads word by word;
- user input (``detect_injection``): suspicious messages are annotated in the trace. They are
  not blocked, because the real defence is that tools enforce permissions server-side;
- answers (``unsafe_answer``): the fraud checks run again on what the customer is about to read,
  so a chunk that got past ingestion still cannot make the agent ask for a verification code.

Matching runs on normalised text (NFKC, lower case, no whitespace or invisible characters), so
"忽 略 之 前", full-width letters and zero-width joiners match like plain text (review round 5,
N-038: those wordings, "以前" and "上面" all got through before). This is a first layer that a
determined attacker can word around; the output check and server-side permissions are the
backstop, and held-back chunks go to a person rather than being silently dropped.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


def normalize(text: str) -> str:
    """NFKC (full-width letters, digits and punctuation become ASCII), lower case, and no
    whitespace or invisible format characters (zero-width spaces and joiners, BOM, bidi marks)."""
    folded = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in folded if not ch.isspace() and unicodedata.category(ch) != "Cf")


# ---------------------------------------------------------------- instructions to the model
_SCOPE = r"(?:之前|以前|此前|先前|以上|上面|上述|前面|前述|刚才|原来|原先|原有|原本|系统|开发者)"
_ALL = r"(?:所有|全部|一切)"
# "设定的密码" is the verb (set), not the noun (settings).
_RULES = r"(?:指令|指示|提示词|提示|规则|设定(?!的)|要求|约束|命令|限制|规定)"
_IGNORE_STRONG = r"(?:忽略|无视|忘记|忘掉|不要理会|不用理会|别理会|不必理会)"
# Weak verbs ("跳过所有提示" is an installer's wording) need a word pointing back at earlier text.
_IGNORE_WEAK = r"(?:跳过|抛开|撇开|丢掉|不要管|别管)"

_INSTRUCTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("override_instructions", re.compile(
        rf"(?:{_IGNORE_STRONG}|{_IGNORE_WEAK})掉?{_SCOPE}的?{_ALL}?的?{_RULES}"
        rf"|{_IGNORE_STRONG}掉?{_ALL}的?{_SCOPE}?的?{_RULES}"
        rf"|{_IGNORE_STRONG}掉?(?:你的|这些)?(?:指令|提示词|系统提示)"
        r"|以上(?:内容|指令|规则|要求|说明)?(?:全部|都|均|一律)?(?:作废|无效|失效|不算数)")),
    ("override_instructions_en", re.compile(
        r"(?:ignore|disregard|forget|override)(?:all|any|every)?(?:of)?(?:the|your|my|these|those)?"
        r"(?:previous|prior|above|earlier|preceding|former|foregoing|original|initial|system)"
        r"(?:instructions?|prompts?|rules?|directions?|directives?|guidelines?|context|messages?|commands?)"
        r"|(?:ignore|disregard|forget)(?:all|any)?(?:of)?(?:everything|anything)(?:above|before|prior|previously|earlier)"
        r"|fromnowon,?(?:you|always|only|answer|reply|respond|tell|say)")),
    ("role_hijack", re.compile(
        r"(?:你现在是|从现在起你是|从现在开始你是|扮演|进入)(?:一个)?.{0,8}(?:模式|管理员|开发者|系统)"
        r"|youarenow(?:a|an|the|in)?(?:admin|administrator|developer|system|unrestricted|jailbroken|dan)"
        r"|(?:enter|activate|enable)(?:developer|admin|god|debug|dan)mode")),
    ("system_prompt_probe", re.compile(r"系统提示词|systemprompt|你的提示词|(?:输出|告诉我)你的(?:指令|设定|提示词)")),
    ("bypass_approval", re.compile(r"(?:无需|不需要|跳过|绕过)(?:人工)?(?:审批|审核|确认|校验)")),
    ("direct_tool_order", re.compile(r"(?:直接|立即|马上)(?:调用|执行|发起).{0,12}(?:退款|refund|工具)")),
    ("fake_system_tag", re.compile(r"【(?:系统|管理员)(?:通知|消息|指令)】|\[system\]|<system>|###system|\[系统\]")),
]

# ---------------------------------------------------------------- what a fraudster tells customers
_ASK = r"(?:提供|发送|发给|发到|告诉|告知|报给|说出|转发|上报|索要)"
_SECRET = (
    r"(?:银行卡(?:号|账号|信息|密码)|信用卡(?:号|信息|密码)|储蓄卡号|借记卡号|卡号(?:和|及|与)(?:密码|有效期|安全码)"
    r"|cvv2?|cvc|安全码|(?:短信|手机)?验证码|动态码|校验码|动态密码|(?:支付|登录|取款|交易|账户|网银)密码"
    r"|身份证(?:照片|正反面)|手持身份证)"
)
_CLAUSE = r"[^。!?;,:]"  # within one clause of normalised text
_PAY = r"(?:转账|汇款|打款|转款|缴纳|交纳|充值)"
_FEE = r"(?:押金|保证金|手续费|解冻费|激活费|验证费|认证费|保修费)"

_FRAUD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("credential_request", re.compile(
        rf"{_ASK}(?:一下)?(?:(?:您|你)的?)?(?:收到的)?(?:\d+位)?{_SECRET}"
        rf"|(?:将|把){_CLAUSE}{{0,12}}{_SECRET}{_CLAUSE}{{0,4}}(?:{_ASK}|发送给|提供给|交给)"
        r"|(?:send|provide|give|share|tell|email|text|forward|confirm)(?:us|me|them)?(?:with)?(?:your|their|the)?(?:full)?"
        r"(?:(?:bank|credit|debit)?card(?:number|details|info|pin)|bankaccount(?:number|details)?|cvv2?|cvc|securitycode"
        r"|(?:verification|sms|one-?time|otp|login)code|password|pin(?:code)?)")),
    ("transfer_request", re.compile(
        rf"{_PAY}{_CLAUSE}{{0,8}}{_FEE}"
        r"|(?:转|汇|打)(?:到|至|入)(?:以下|下面|下列|指定|该|这个|我的|专员|对方|个人)?的?(?:账户|账号|卡号|银行卡|支付宝|微信)"
        r"|(?:wire|transfer)(?:a|the|your)?(?:deposit|fee|money|funds)")),
    ("private_contact", re.compile(
        r"(?:微信|vx|v信|wx|weixin|wechat|qq|whatsapp|telegram)(?:号|号码|群|账号|id)?:?(?:是|为)?"
        r"(?:[a-z][a-z0-9_-]{5,19}|\d{5,12})")),
]
# A warning about the trick uses the same words ("客服不会索要验证码", "请勿将验证码告诉他人",
# "谨防冒充客服索要验证码", "如遇到要求您转账的情况，请挂断"). It is a warning only when the negation
# governs the request itself: right before its verb, a beware-frame earlier in the clause, or an
# if-you-meet frame around it. A purpose clause is not ("为防止诈骗，请将验证码发给客服核验"), and
# English "not" is only matched as "do not"/"will not" (the text has no spaces left: "note" ≠ not).
_NEGATED = re.compile(
    r"(?:不会|不要|请勿|切勿|勿|不得|禁止|绝不|决不|从不|无需|无须|不需要|不用|拒绝|没有人会|任何人都不会)"
    r"(?:向(?:您|你|任何人|他人)|主动|以任何(?:理由|方式|名义)|通过任何(?:渠道|方式)|在任何情况下|要求(?:您|你)|让(?:您|你))*$"
    r"|(?:never|donot|don't|dont|willnot|won't|wont|shallnot|doesnot|doesn't|noone|nobody)[a-z']{0,12}$")
_BEWARE = re.compile(r"谨防|警惕|小心|当心|提防|防范|beware")
_IF_MET = re.compile(r"如遇|遇到|若有|如有|如果有|凡是|一旦有")
_SCENARIO = re.compile(r"^[^。!?;,:]{0,12}?(?:的情况|的人|的电话|的短信|的链接|的网站|时)")
_BOUNDARY = "。!?;,:."
_CITATION = re.compile(r"\[\d+(?:[,，]\d+)*\]")

# Instructions addressed to the support agent. In an internal handbook they are its purpose; in a
# customer-facing policy they are out of place ("客服应告知顾客：请添加微信……"), so public only.
_AGENT_DIRECTIVE = re.compile(
    r"(?:客服|坐席|售后|专员|机器人|智能客服|助手)(?:人员|专员)?"
    r"(?:应|应该|应当|须|需|需要|必须|务必|要|请|一律|统一|只能|只需){1,2}(?:告知|告诉|回答|回复|答复|引导|说|转告)"
    r"|(?:一律|统一|始终|总是)(?:回答|回复|答复|告知|告诉|说)")


@dataclass
class InjectionReport:
    suspicious: bool
    matches: list[str] = field(default_factory=list)


def detect_injection(text: str) -> InjectionReport:
    """Instructions aimed at the model, in a customer's message or a knowledge chunk."""
    norm = normalize(text)
    matches = [name for name, pattern in _INSTRUCTION_PATTERNS if pattern.search(norm)]
    return InjectionReport(bool(matches), matches)


def _clause(norm: str, start: int, end: int) -> tuple[str, str]:
    """The text of the clause before a match, and after it."""
    left = max(norm.rfind(mark, 0, start) for mark in _BOUNDARY) + 1
    rights = [i for i in (norm.find(mark, end) for mark in _BOUNDARY) if i >= 0]
    return norm[left:start], norm[end:min(rights) if rights else len(norm)]


def _is_warning(before: str, after: str) -> bool:
    return bool(_NEGATED.search(before) or _BEWARE.search(before)
                or (_IF_MET.search(before) and _SCENARIO.search(after)))


def _fraud(norm: str, trusted: str = "") -> list[str]:
    found = []
    for name, pattern in _FRAUD_PATTERNS:
        for match in pattern.finditer(norm):
            before, after = _clause(norm, match.start(), match.end())
            if _is_warning(before, after):
                continue  # a warning about the trick, not the trick
            # Quoted from knowledge an administrator reviewed and released: the whole clause, not
            # only the matched words ("请先缴纳 5000 元保证金后再退款" is not the supplier FAQ).
            if trusted and _CITATION.sub("", before + match.group(0) + after) in trusted:
                continue
            found.append(name)
            break
    return found


def scan_knowledge(text: str, public: bool = True) -> list[str]:
    """Why a knowledge chunk should be held back for review (empty: index it)."""
    norm = normalize(text)
    reasons = [name for name, pattern in _INSTRUCTION_PATTERNS if pattern.search(norm)] + _fraud(norm)
    if public and _AGENT_DIRECTIVE.search(norm):
        reasons.append("agent_directive")
    return reasons


def unsafe_answer(text: str, reviewed: list[str] | None = None) -> list[str]:
    """Why an answer must not reach the customer: it asks for a card number, a verification code
    or a password, moves the customer to a private WeChat or QQ, or asks for a transfer. A match
    quoted verbatim from a chunk an administrator released after review (``reviewed``) is allowed:
    a supplier FAQ may really say that suppliers pay a deposit."""
    trusted = "\n".join(normalize(t) for t in reviewed or [])
    return _fraud(normalize(text), trusted)


REASON_TEXT = {
    "override_instructions": "要求忽略之前的指令",
    "override_instructions_en": "要求忽略之前的指令（英文）",
    "role_hijack": "要求切换身份或模式",
    "system_prompt_probe": "套取系统提示词",
    "bypass_approval": "要求跳过审批",
    "direct_tool_order": "要求直接执行退款等操作",
    "fake_system_tag": "伪造系统通知",
    "credential_request": "索要银行卡号、验证码或密码",
    "transfer_request": "要求转账或缴纳押金",
    "private_contact": "引导添加私人微信或QQ",
    "agent_directive": "面向顾客的文档里有写给客服的指令",
}
