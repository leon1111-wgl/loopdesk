"""Prompt-injection and fraud heuristics.

Three places use this:
- ingestion (``scan_knowledge``): a knowledge chunk that tries to instruct the model, or that
  tells customers to hand over a card number or a verification code, to contact someone outside
  the tenant's official channels, to pay money to get money, or to fill in details on a site
  that is not the tenant's, is held back until an administrator has reviewed it. Indirect
  injection is the more dangerous kind for RAG: knowledge comes from suppliers, old wikis and
  manuals that nobody reads word by word;
- user input (``detect_injection``): suspicious messages are annotated in the trace. They are
  not blocked, because the real defence is that tools enforce permissions server-side;
- answers (``unsafe_answer``): what the customer is about to read is checked again, and any link
  or contact outside the tenant's official channels stops the answer.

Matching runs on normalised text (NFKC, lower case, no whitespace or invisible characters), so
"忽 略 之 前", full-width letters and zero-width joiners match like plain text. Links are read
from the text before whitespace is removed, so their boundaries survive.

These are rules: a second-model review and a blind set written by a model that never saw them
measured how far they get (docs/EVALUATION.md). They are a first layer. Held-back chunks go to a
person, answers that fail go to a person, and permissions are enforced by the tools themselves.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlsplit


def normalize(text: str) -> str:
    """NFKC (full-width letters, digits and punctuation become ASCII), lower case, and no
    whitespace or invisible format characters (zero-width spaces and joiners, BOM, bidi marks)."""
    folded = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in folded if not ch.isspace() and unicodedata.category(ch) != "Cf")


def _folded(text: str) -> str:
    """Like ``normalize`` but keeping whitespace, which ends a link."""
    folded = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")


@dataclass(frozen=True)
class Channels:
    """A tenant's official channels: the sites customers may be sent to and the contact accounts
    that are really the tenant's. Everything else is outside."""

    domains: tuple[str, ...] = ()
    contacts: tuple[str, ...] = ()

    @classmethod
    def of(cls, domains=(), contacts=()) -> Channels:
        return cls(tuple(clean_domain(d) for d in domains or () if clean_domain(d)),
                   tuple(str(c).strip().lower() for c in contacts or () if str(c).strip()))

    def official_host(self, host: str) -> bool:
        host = host.rstrip(".")
        return any(host == d or host.endswith("." + d) for d in self.domains)

    def official_contact(self, account: str) -> bool:
        return account.lower() in self.contacts


NO_CHANNELS = Channels()

_DOMAIN = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}$")


def clean_domain(value: str) -> str:
    """"https://www.Example.com/help" -> "example.com"; "" when it is not a domain."""
    value = unicodedata.normalize("NFKC", str(value)).strip().lower()
    value = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value)
    value = re.split(r"[/?#:]", value, 1)[0].split("@")[-1].lstrip("*.")
    value = value[4:] if value.startswith("www.") else value
    return value if _DOMAIN.match(value) else ""


# ---------------------------------------------------------------- links
_TLDS = ("com|cn|net|org|top|xyz|cc|vip|shop|info|io|co|me|link|club|site|online|store|tech|app|ltd|work|wang|"
         "hk|tw|mo|us|uk|jp|kr|sg|biz|pro|live|fun|icu|ru|de|fr|in|ly|gl|gd|to|tk|ml|ga|cf|gq|ws|su|example|test")
_DOT_TRICKS = re.compile(r"\s*(?:\[\.\]|\(\.\)|【\.】|\[dot\]|\(dot\)|\{dot\}|\[点\]|（点）)\s*")
_URL = re.compile(r"(?:https?|hxxps?)://[^\s<>\"'()（）\[\]{}，。、；！？]+")
_HOST = rf"(?:[a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?\.)+(?:{_TLDS})"
_BARE = re.compile(rf"(?<![a-z0-9@._/-])({_HOST})(?![a-z0-9-])")
_EMAIL = re.compile(rf"[a-z0-9._%+-]+@({_HOST})(?![a-z0-9-])")
_BARE_IP = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?=[:/])")


def links(text: str) -> list[tuple[str, int]]:
    """(host, position in the text) of every link, address or e-mail domain in ``text``. Reads
    the target of a Markdown link, not its label, and the host of https://official@evil."""
    folded = _DOT_TRICKS.sub(".", _folded(text)).replace("hxxp", "http")
    found: list[tuple[str, int]] = []
    taken: list[tuple[int, int]] = []
    for match in _URL.finditer(folded):
        try:
            host = urlsplit(match.group(0)).hostname or ""
        except ValueError:
            host = ""
        if host:
            found.append((host, match.start()))
            taken.append(match.span())
    for pattern in (_EMAIL, _BARE, _BARE_IP):
        for match in pattern.finditer(folded):
            if not any(a <= match.start() < b for a, b in taken):
                found.append((match.group(1), match.start()))
                taken.append(match.span())
    return found


# ---------------------------------------------------------------- instructions to the model
_SCOPE = r"(?:之前|以前|此前|先前|以上|上面|上述|前面|前述|刚才|原来|原先|原有|原本)"
_ALL = r"(?:所有|全部|一切)"
# "设定的密码" is the verb (set), not the noun (settings).
_RULES = r"(?:指令|指示|提示词|提示|规则|设定(?!的)|要求|约束|命令|限制|规定)"
_IGNORE_STRONG = r"(?:忽略|无视|忘记|忘掉|不要理会|不用理会|别理会|不必理会|覆盖|取代)"
# Weak verbs ("跳过所有提示" is an installer's wording) need a word pointing back at earlier text.
_IGNORE_WEAK = r"(?:跳过|抛开|撇开|丢掉|不要管|别管)"

_OVERRIDE = (
    "override_instructions", re.compile(
        rf"(?:{_IGNORE_STRONG}|{_IGNORE_WEAK})掉?{_SCOPE}的?{_ALL}?的?(?:{_RULES}|内容|说明|信息)"
        # "忽略系统提示并继续安装" is an installer's instruction; the system's rules are not.
        rf"|(?:{_IGNORE_STRONG}|{_IGNORE_WEAK})掉?(?:系统|开发者)的?{_ALL}?的?(?:指令|指示|提示词|规则|设定(?!的)|约束|命令)"
        rf"|{_IGNORE_STRONG}掉?{_ALL}的?{_SCOPE}?的?{_RULES}"
        rf"|{_IGNORE_STRONG}掉?(?:你的|这些)?(?:指令|提示词)"
        r"|以上(?:内容|指令|规则|要求|说明)?(?:全部|都|均|一律)?(?:作废|无效|失效|不算数)"))
_OVERRIDE_EN = (
    "override_instructions_en", re.compile(
        r"(?:ignore|disregard|forget|override|paynoattentionto|paynoheedto|setaside)(?:all|any|every)?(?:of)?"
        r"(?:the|your|my|these|those)?"
        r"(?:previous|prior|above|earlier|preceding|former|foregoing|original|initial|system)"
        r"(?:instructions?|prompts?|rules?|directions?|directives?|guidelines?|context|messages?|commands?)"
        r"|(?:ignore|disregard|forget)(?:all|any)?(?:of)?(?:everything|anything)(?:above|before|prior|previously|earlier)"
        r"|fromnowon,?(?:you|always|only|answer|reply|respond|tell|say)"))
_PROMPT_PROBE = ("system_prompt_probe",
                 re.compile(r"系统提示词|systemprompt|你的提示词|(?:输出|告诉我)你的(?:指令|设定|提示词)"))

# Customer messages: broad patterns, only annotated in the trace (a false alarm costs nothing).
_MESSAGE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    _OVERRIDE, _OVERRIDE_EN,
    ("role_hijack", re.compile(
        r"(?:你现在是|从现在起你是|从现在开始你是|扮演|进入)(?:一个)?.{0,8}(?:模式|管理员|开发者|系统)"
        r"|youarenow(?:a|an|the|in)?(?:admin|administrator|developer|system|unrestricted|jailbroken|dan)"
        r"|(?:enter|activate|enable)(?:developer|admin|god|debug|dan)mode")),
    _PROMPT_PROBE,
    ("bypass_approval", re.compile(r"(?:无需|不需要|跳过|绕过)(?:人工)?(?:审批|审核|确认|校验)")),
    ("direct_tool_order", re.compile(r"(?:直接|立即|马上)(?:调用|执行|发起).{0,12}(?:退款|refund|工具)")),
    ("fake_system_tag", re.compile(r"【(?:系统|管理员)(?:通知|消息|指令)】|\[system\]|<system>|###system|\[系统\]")),
]

# Knowledge: only what instructs the model. Manuals say "进入配对模式", policies say "无需审核" and
# "可直接发起退款", announcements start with 【系统通知】: those words alone held back ordinary
# knowledge (13 of 16 ordinary texts in review), so they stay message-only signals.
_KNOWLEDGE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    _OVERRIDE, _OVERRIDE_EN,
    ("role_hijack", re.compile(
        r"(?:你现在是|从现在起你是|从现在开始你(?:就)?是|你的新身份是|请扮演|你将扮演)(?:一个|一名)?"
        r"(?:不受限制的|没有限制的)?(?:管理员|开发者|系统|超级用户|ai|助手|机器人|客服主管|dan)"
        r"|(?:进入|切换到|开启|激活)(?:开发者|管理员|调试|越狱|上帝|无限制|dan)模式"
        r"|youarenow(?:a|an|the|in)?(?:admin|administrator|developer|system|unrestricted|jailbroken|dan)"
        r"|(?:enter|activate|enable)(?:developer|admin|god|debug|dan)mode")),
    _PROMPT_PROBE,
    ("bypass_approval", re.compile(
        r"(?:跳过|绕过|无视)(?:人工)?(?:审批|审核|校验|风控)"
        r"|(?:一律|统统|全部|所有|任何)(?:退款|申请)?(?:都)?(?:直接)?(?:批准|通过|同意)(?:所有|全部|任何)?(?:退款|申请)")),
    ("direct_tool_order", re.compile(r"(?:调用|执行|运行)[^。!?;,:]{0,10}(?:工具|接口|函数|api|tool|function)")),
]

# ---------------------------------------------------------------- what a fraudster tells customers
_ASK = r"(?:提供|发送|发给|发到|告诉|告知|报给|说出|转发|上报|索要|回复|发我|发给我|告诉我|报一下|私信)"
_TO = r"(?:给)?(?:客服|专员|我们|我|工作人员|售后|对方|本人)?"
_SECRET = (
    r"(?:银行卡(?:号|账号|信息|密码)|信用卡(?:号|信息|密码)|储蓄卡号|借记卡号|(?<!会员)(?<!积分)(?<!礼品)(?<!购物)(?<!储值)卡号"
    r"|cvv2?|cvc|安全码|(?:短信|手机)?验证码|动态码|校验码|动态密码|(?:支付|登录|取款|交易|账户|网银)密码"
    r"|身份证(?:照片|正反面)|手持身份证)"
)
_CLAUSE = r"[^。!?;,:]"  # within one clause of normalised text
_PAY = r"(?:转账|汇款|打款|转款|缴纳|交纳|充值|支付|付款|先付)"
_FEE = r"(?:押金|保证金|手续费|解冻费|激活费|验证费|认证费|保修费|服务费|会员费|税费|工本费)"
# Paying to get money back is the scam; a rental deposit returned on return is not.
_GAIN = re.compile(r"退款|理赔|补偿|赔付|赔偿|解冻|激活|提现|领取|加急|核验|认证|返现|中奖|refund|claim|compensat|unfreeze|unlock")

_FRAUD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("credential_request", re.compile(
        rf"{_ASK}{_TO}(?:一下)?(?:(?:您|你)的?)?(?:收到的)?(?:\d+位)?(?:{_CLAUSE}{{0,8}}(?:和|及|、|与|以及))?{_SECRET}"
        rf"|{_ASK}{_TO}(?:以下|如下|下列)(?:信息|资料|内容)?(?:以便[^:]{{0,6}})?:?-?(?:\d[.、)])?{_SECRET}"
        rf"|(?:将|把){_CLAUSE}{{0,12}}{_SECRET}{_CLAUSE}{{0,4}}(?:{_ASK}|发送给|提供给|交给)"
        rf"|{_SECRET}{_CLAUSE}{{0,4}}(?:发我|发给我|告诉我|报给我|发过来|私信我)"
        rf"|(?:下方|下面|此处|这里|聊天框|对话框|输入框)(?:输入|填写|回复|发送){_CLAUSE}{{0,6}}{_SECRET}"
        r"|(?:send|provide|give|share|tell|email|text|forward|confirm|replywith)(?:us|me|them)?(?:with)?(?:your|their|the)?"
        r"(?:full)?(?:(?:bank|credit|debit)?card(?:number|details|info|pin)|bankaccount(?:number|details)?|cvv2?|cvc"
        r"|securitycode|(?:verification|sms|one-?time|otp|login)code|password|pin(?:code)?)")),
    ("transfer_request", re.compile(
        rf"{_PAY}{_CLAUSE}{{0,10}}{_FEE}|{_FEE}{_CLAUSE}{{0,6}}{_PAY}"
        r"|(?:wire|transfer|pay)(?:a|the|your)?(?:deposit|fee|money|funds)")),
    ("transfer_request", re.compile(
        r"(?:转|汇|打)(?:到|至|入|进)(?:我们|我|公司|平台|专员|客服|指定|以下|下面|下列|该|这个|对方|个人)的?"
        r"(?:收款)?(?:账户|账号|卡号|银行卡|卡|支付宝|微信)")),
]
_CONTACT_ID = re.compile(
    r"(?:微信|vx|v信|wx|weixin|wechat|qq|whatsapp|telegram)(?:号|号码|群|账号|id)?:?(?:是|为)?"
    r"([a-z][a-z0-9_-]{5,19}|\d{5,12})")
_ADD_FRIEND = re.compile(
    r"(?:加|添加)(?:我|专员|客服|售后|他|她)?的?(?:微信|qq)?好友"
    r"|(?:扫|扫描)(?:下方|下面|以下|这个|该|图中)?(?:的)?二维码(?:添加|加)(?:专员|客服|好友|微信|售后|我)")
# A link next to these is asking for something on a site that is not the tenant's.
_LINK_ASK = re.compile(
    r"填写|输入|提交|登记|上传|核验|验证|认证|激活|绑定|修改(?:支付|登录)?密码|领取|申领|补偿|赔付|赔偿|理赔|返现|红包|中奖|解冻"
    r"|银行卡|卡号|验证码|密码|身份证|收款账户|个人信息"
    r"|signin|login|logon|verify|confirm|update|enter|card|password|claim|reward")

# A warning about the trick uses the same words ("客服不会索要验证码", "请勿将验证码告诉他人",
# "谨防冒充客服索要验证码", "如遇到要求您转账的情况，请挂断"). It is a warning only when the negation
# governs the request itself: right before its verb, a beware-frame earlier in the clause, or an
# if-you-meet frame around it. A purpose clause is not ("为防止诈骗，请将验证码发给客服核验"), and
# English "not" is only matched as "do not"/"will not" (the text has no spaces left: "note" ≠ not).
_NEGATED = re.compile(
    r"(?:不会|不要|请勿|切勿|勿|不得|禁止|绝不|决不|从不|无需|无须|不需要|不用|拒绝|没有人会|任何人都不会)"
    r"(?:向(?:您|你|任何人|他人)|主动|以任何(?:理由|方式|名义)|通过任何(?:渠道|方式)|在任何情况下|要求(?:您|你)|让(?:您|你))*$"
    r"|(?:never|donot|don't|dont|willnot|won't|wont|shallnot|doesnot|doesn't|noone|nobody)[a-z']{0,12}$")
# Before a link: "不会要求您在 x 填写", "请勿访问 x", "谨防假冒网站 x".
_NEGATED_LINK = re.compile(r"(?:不会|不要|请勿|切勿|勿|不得|禁止|绝不|决不|从不|并非|不是|非)[^。!?;,:]{0,8}$")
_BEWARE = re.compile(r"谨防|警惕|小心|当心|提防|防范|假冒|仿冒|钓鱼|beware|fake|phishing")
_IF_MET = re.compile(r"如遇|遇到|若有|如有|如果有|凡是|一旦有")
_SCENARIO = re.compile(r"^[^。!?;,:]{0,12}?(?:的情况|的人|的电话|的短信|的链接|的网站|时)")
_BOUNDARY = "。!?;,:."
_SENTENCE = "。!?;"
_CITATION = re.compile(r"\[\d+(?:[,，]\d+)*\]")

# Instructions addressed to the support agent. In an internal handbook they are its purpose; in a
# customer-facing policy they are out of place ("客服应告知顾客：请添加微信……"), so public only.
# "联系客服请说明订单号" addresses the customer: 客服 is the object there, and 说明 is "explain".
_AGENT_DIRECTIVE = re.compile(
    r"(?<![联系咨询致电拨打找问向给])(?:客服|坐席|售后专员|售后客服|专员|机器人|智能客服|助手)(?:人员)?"
    r"(?:应|应该|应当|须|需|需要|必须|务必|要|请|一律|统一|只能|只需|直接){1,2}(?:告知|告诉|回答|回复|答复|引导|转告|说(?!明))"
    r"|(?:一律|统一|始终|总是)(?:回答|回复|答复|告知|告诉|说(?!明))")


@dataclass
class InjectionReport:
    suspicious: bool
    matches: list[str] = field(default_factory=list)


def detect_injection(text: str) -> InjectionReport:
    """Instructions aimed at the model in a customer's message (annotated, not blocked)."""
    norm = normalize(text)
    matches = [name for name, pattern in _MESSAGE_PATTERNS if pattern.search(norm)]
    return InjectionReport(bool(matches), matches)


def _bounds(norm: str, start: int, end: int, marks: str = _BOUNDARY) -> tuple[int, int]:
    left = max(norm.rfind(mark, 0, start) for mark in marks) + 1
    rights = [i for i in (norm.find(mark, end) for mark in marks) if i >= 0]
    return left, min(rights) if rights else len(norm)


def _is_warning(before: str, after: str) -> bool:
    return bool(_NEGATED.search(before) or _BEWARE.search(before)
                or (_IF_MET.search(before) and _SCENARIO.search(after)))


def _fraud(norm: str, channels: Channels, trusted: str = "") -> list[str]:
    found: list[str] = []
    for name, pattern in _FRAUD_PATTERNS:
        if name in found:
            continue
        for match in pattern.finditer(norm):
            left, right = _bounds(norm, match.start(), match.end())
            before, after = norm[left:match.start()], norm[match.end():right]
            if _is_warning(before, after):
                continue  # a warning about the trick, not the trick
            if name == "transfer_request" and pattern is _FRAUD_PATTERNS[1][1]:
                s_left, s_right = _bounds(norm, match.start(), match.end(), _SENTENCE)
                if not _GAIN.search(norm[s_left:s_right]):
                    continue  # a deposit for a rental or a supplier, not paying to get money
            # Quoted from knowledge an administrator reviewed and released: the whole clause, not
            # only the matched words ("请先缴纳 5000 元保证金后再退款" is not the supplier FAQ).
            if trusted and _CITATION.sub("", before + match.group(0) + after) in trusted:
                continue
            found.append(name)
            break
    if _private_contact(norm, channels):
        found.append("private_contact")
    return found


def _private_contact(norm: str, channels: Channels) -> bool:
    for match in _CONTACT_ID.finditer(norm):
        left, right = _bounds(norm, match.start(), match.end())
        if channels.official_contact(match.group(1)) or _is_warning(norm[left:match.start()], norm[match.end():right]):
            continue
        return True
    for match in _ADD_FRIEND.finditer(norm):
        left, right = _bounds(norm, match.start(), match.end(), _SENTENCE)
        sentence = norm[left:right]
        if any(channels.official_contact(m.group(1)) for m in _CONTACT_ID.finditer(sentence)):
            continue  # "添加官方客服微信 leonshop2026 为好友"
        if _is_warning(norm[left:match.start()], norm[match.end():right]):
            continue
        return True
    return False


def _outside_links(text: str, channels: Channels) -> list[tuple[str, int]]:
    """Links to hosts outside the official channels that are not named as a warning."""
    folded = _DOT_TRICKS.sub(".", _folded(text))
    outside = []
    for host, position in links(text):
        if channels.official_host(host):
            continue
        before = normalize(folded[max(0, position - 40):position])
        cut = max(before.rfind(mark) for mark in "。!?;,:")
        clause_before = before[cut + 1:]
        if _NEGATED_LINK.search(clause_before) or _BEWARE.search(before[max(0, cut - 20):]):
            continue  # "谨防假冒网站 x", "不会要求您在 x 填写"
        outside.append((host, position))
    return outside


def scan_knowledge(text: str, public: bool = True, channels: Channels = NO_CHANNELS) -> list[str]:
    """Why a knowledge chunk should be held back for review (empty: index it)."""
    norm = normalize(text)
    reasons = [name for name, pattern in _KNOWLEDGE_PATTERNS if pattern.search(norm)]
    reasons += _fraud(norm, channels)
    # A link outside the official channels in a chunk that asks for something anywhere in it: the
    # request and the link are often two sentences ("点击下方链接完成验证。\nhttp://…").
    if _outside_links(text, channels) and _LINK_ASK.search(norm):
        reasons.append("outside_link")
    if public and _AGENT_DIRECTIVE.search(norm):
        reasons.append("agent_directive")
    return list(dict.fromkeys(reasons))


def unsafe_answer(text: str, reviewed: list[str] | None = None, channels: Channels = NO_CHANNELS) -> list[str]:
    """Why an answer must not reach the customer: it asks for a card number, a verification code
    or a password, sends the customer to an account or a site outside the official channels, or
    asks for money to get money. A support answer has no reason to link anywhere else, so any
    such link stops it, whatever the sentence around it says. A clause quoted verbatim from a
    chunk an administrator released after review is allowed (a supplier FAQ may really say that
    suppliers pay a deposit); links and contacts are not exempted that way: an official one
    belongs in the tenant's channels."""
    trusted = "\n".join(normalize(t) for t in reviewed or [])
    reasons = _fraud(normalize(text), channels, trusted)
    if _outside_links(text, channels):
        reasons.append("outside_link")
    return reasons


REASON_TEXT = {
    "override_instructions": "要求忽略之前的指令",
    "override_instructions_en": "要求忽略之前的指令（英文）",
    "role_hijack": "要求切换身份或模式",
    "system_prompt_probe": "套取系统提示词",
    "bypass_approval": "要求跳过审批或一律通过",
    "direct_tool_order": "要求直接调用工具",
    "fake_system_tag": "伪造系统通知",
    "credential_request": "索要银行卡号、验证码或密码",
    "transfer_request": "要求先付钱才能退款、理赔或解冻，或要求把钱转到某个账户",
    "private_contact": "引导添加官方渠道以外的微信、QQ 等联系方式",
    "outside_link": "官方域名以外的链接，并要求填写信息、领取或核验",
    "agent_directive": "面向顾客的文档里有写给客服的指令",
}
