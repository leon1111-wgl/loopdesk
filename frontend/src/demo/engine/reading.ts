/**
 * How the browser engine reads a question beyond the offline model's rules. The backend has a model for colloquial wording; the
 * static demo has none, so the documents' own words are mapped from how people say them, an answer is picked by the kind of
 * fact asked for, and what no rule may grant in a chat is declined up front. Money is not decided here: text.ts still does that.
 */

import { fixTypos } from "./text";

/** Everyday wording, and the words the documents use for it. */
const VOCABULARY: [RegExp, string][] = [
  // shipping
  [/拍下|下单|付完款|付了款|买完/, "付款后"],
  [/(?:多久|几天|啥时候|什么时候|何时|多长时间).{0,4}发(?!票)|发货.{0,3}(?:快吗|要多久|多久)/, "发货时效 发货"],
  [/(?<!退款|退钱|钱)(?:多久|几天|啥时候|什么时候).{0,3}(?:能)?(?:到|收到|送到)(?!账)|几天能到|要几天/, "预计 送达"],
  [/大促|双十一|双11|618/, "大促 双十一 发货时效"],
  [/哪家快递|什么快递|啥快递|发什么快递|指定.{0,4}快递|顺丰|申通|圆通|中通/, "合作快递 指定快递公司"],
  [/运单号|物流轨迹|怎么查物流/, "运单号 物流轨迹"],
  [/邮费|包邮|运费/, "运费 包邮"],
  [/香港|澳门|台湾|港澳|海外|国外/, "港澳台 海外地区 配送"],
  // refunds and returns
  [/不满意|不喜欢|不合适|买错|拍错|不想要|不要了/, "七天无理由退货"],
  [/收到之?后|到手|拿到/, "签收"],
  [/寄回|退回去|寄回去|退货.{0,3}(?:运费|邮费|快递费)|运费.{0,3}谁/, "寄回运费 买家承担"],
  [/摔|进水|自己弄|自己拆|我弄坏/, "人为损坏"],
  [/瑕疵|破了|裂了|碎了|烂了|质量.{0,2}问题|质量不好|质量差/, "质量问题 退换"],
  [/运费.{0,4}(?:谁|我出|我付|要我)/, "往返运费 寄回运费"],
  [/拍照|发照片|要照片|传图/, "上传 照片"],
  [/多少钱.{0,6}自动|自动退|秒退|小额/, "自动审核 退款"],
  [/人工审核|审核.{0,3}(?:多久|多长时间)|要审多久/, "人工审核时效"],
  [/(?:几天|多少天|多久|多长时间).{0,6}(?:内|之内|以内|还)?.{0,4}(?:能|可以)退|退.{0,4}(?:期限|时间内)/, "七天无理由退货 扣费后 申请"],
  [/(?:退款|退钱|钱).{0,4}(?:几天|多久|多长时间|啥时候|什么时候|哪天).{0,3}(?:到|回)/, "退款到账 到账 工作日"],
  // invoices, points, levels
  [/开票|开发票|要发票|开个发票|开张发票/, "申请发票 电子普通发票 开具"],
  [/抬头/, "发票抬头 修改"],
  [/积分.{0,4}(?:怎么来|怎么得|怎么攒|怎么赚|怎么获)|攒积分|送积分/, "获得 积分"],
  [/(?:能|可以)?抵(?!扣)/, "抵扣"],
  [/积分.{0,6}(?:过期|到期|有效|多久)/, "积分有效期"],
  [/升级|金卡/, "金卡会员 升级"],
  // suppliers
  [/入驻|供应商|供货|厂家|厂商|开店|合作/, "供应商 入驻 资质"],
  // membership and renewal
  [/关掉|关了|关闭|停掉|不想续|不续了|不想(?:再)?自动|别再扣|怎么取消.{0,4}(?:续费|包月)/, "取消自动续费 管理自动续费"],
  [/自动扣|老扣|一直扣|偷偷扣|每个?月.{0,2}扣|又扣/, "自动续费 扣费"],
  [/苹果|iphone|ipad|ios|app\s*store/i, "App Store iPhone Apple ID"],
  [/提醒|通知我|提前说/, "扣费前 提醒"],
  [/取消.{0,6}(?:还能|还可以|能用|有效|到期)/, "取消自动续费 会员期 仍然有效"],
  [/学生/, "学生认证"],
  [/多少钱|多贵|价格|价钱|怎么收费|收费/, "价格"],
  [/(?:年卡|年付|年费|包年|单月|月付|包月)(?!.{0,6}退)/, "会员类型 价格 连续包月"],
  [/(?:年费|月费|会员费|年卡|包月|单月).{0,6}退/, "会员扣费 退款 申请"],
  [/什么资料|啥资料|要交|准备什么|准备啥|需要什么|要什么/, "需要提交 需要提供"],
  // downloads and sound
  [/下多少|能下|下歌|下几首|下载量|离线/, "最多下载 下载 离线收听"],
  [/一个月|每个月/, "每月"],
  [/导出|转格式|mp3|拷出来|传到电脑/i, "导出 格式"],
  [/变灰|灰色|下架|放不了|听不了/, "变灰 版权"],
  [/搜不到|找不到.{0,3}歌|没有这首|没这首/, "求歌 曲库"],
  // coins and minors
  [/(?:^|[^a-z])币/i, "Echo币"],
  [/充多少|能充|充钱|充值.{0,2}(?:多少|上限)/, "充值"],
  [/提现|转赠|币.{0,6}转给/, "提现 转赠"],
  [/未成年|小孩|孩子|儿子|女儿|弟弟|妹妹|中学生|(?:^|[^\d])1[0-7]岁|十[一二三四五六七]岁/, "未成年人 未满18周岁 充值上限"],
  [/青少年/, "青少年模式"],
  [/偷偷充|误充|乱充/, "误充值 监护人"],
  // albums
  [/专辑.{0,8}(?:送|赠)|送给.{0,4}(?:朋友|好友)/, "赠送 数字专辑"],
  [/买了两次|买重复|重复买|扣了两次/, "重复购买"],
  // account
  [/换手机|换号码|改手机|手机号换|换绑/, "换绑手机号"],
  [/注销|删号|销号/, "注销账号"],
  [/后悔|反悔|撤回|找回|恢复/, "撤销"],
  [/清掉|清零|清了|没了|还在吗|还有吗/, "清空"],
  [/封号|封了|被封|解封/, "封禁 申诉"],
  [/几台|几个设备|同时登|挤下线|被踢|多少设备/, "设备 同时在线"],
  [/验证码|密码/, "验证码 登录密码"],
  // Iris
  [/空间|容量|多少g\b|多大/i, "存储空间"],
  [/备份|搬走|迁移/, "数据导出 导出"],
  [/单点|扫码|sso/i, "SSO 单点登录 企业微信"],
  [/稳定|可用率|宕机|赔/, "可用性 补偿"],
  [/打不开|全挂|挂了|崩了|彻底|整个.{0,4}(?:不能|用不了|打不开)|完全/, "P1级故障 服务完全不可用"],
  [/部分功能|有些功能|个别功能/, "P2级故障 部分功能异常"],
  [/(?:多久|多长时间|啥时候|什么时候).{0,6}(?:回|理|响应|处理)|有人(?:回|理|管)/, "响应 工单响应时效"],
];

// Words that carry how something is said, not what it is about.
const FILLERS = new Set("大概 大约 之后 以后 然后 的话 要是 如果 想来 还是 现在 目前 一般 通常 可能 应该 就是 还有 比如 那么 这么 这样 那样 非常 特别 比较 有点 半天 彻底 有人 不够 一下 一点 一些 别人 自己 咱们 我们 他们 平台 知道 想问 请问 问下 问问 谢谢 麻烦 感觉 其实 真的 到底 究竟 而且 但是 不过 所以 因为 怎么样 找到 没找到 一个月 一年 刚才 刚刚 昨天 今天 明天 几天 几次 回来 回去 另外 资料 东西 收到 期间 期限 时候 时间 情况 办法 地方 这种 那种 过了 限制 区别 划算 值不值 室友 朋友 同事 老婆 老公 听说 假的 真的假的 直接 不是".split(" "));
export const isFiller = (word: string): boolean => FILLERS.has(word);

// "能退吗" with nothing else to go on is about the refund rules; "数字专辑能退吗" is about digital albums.
const ANY_REFUND = /(?<!年费|月费|会员费|年卡|包月|单月)(?:可以.{0,4}退(?!回)|能.{0,4}退(?!回)|退不退)/;
export const refundRuleWords = (text: string): string => (ANY_REFUND.test(text) ? "七天无理由退货 扣费后 申请退款" : "");

export function documentWords(text: string): string {
  return VOCABULARY.filter(([phrase]) => phrase.test(text)).map(([, words]) => words).join(" ");
}

/** The parts of a question the vocabulary already read: said in everyday words, so not missing from the documents. */
export function saidOtherwise(text: string): string[] {
  return VOCABULARY.map(([phrase]) => phrase.exec(text)?.[0]).filter((said): said is string => Boolean(said));
}

/** The kind of fact a question asks for, and what a sentence stating that kind of fact looks like. */
const KINDS: [RegExp, RegExp][] = [
  [/多久|多长时间|几天|几个?小时|什么时候|啥时候|何时|哪天|时效|周期|有效期/, /\d+\s*(?:个)?(?:工作日|小时|天|分钟|月|年)|到期|当月/],
  [/多少钱|几块|价格|价钱|多贵|费用|收费|折扣|优惠/, /\d+(?:\.\d+)?\s*(?:元|块)|[¥￥]\d/],
  [/多少|几[个张首台次]|最多|最高|上限|多大|怎么算/, /\d+(?:\.\d+)?\s*(?:元|块|GB|TB|%|首|个|张|次|台|积分)|上限|最多|不超过/],
  [/怎么|怎样|如何|咋|在哪|哪里|步骤|流程|方法|要啥|需要什么|准备什么|什么资料|啥资料|交什么|交啥/, /“[^”]+”|进入|可通过|提交|点击|需要提供|需要提交/],
  [/能不能|能否|可不可以|可以吗|能.{0,8}吗|是否|会不会|有没有|支持吗/, /可以|可申请|可在|支持|不支持|不能|无法|自动审核|人工审核/],
];

const ASKS_MONEY = /多少钱|几块|价格|价钱|多贵|费用|收费|折扣/;
const STATES_MONEY = /\d+(?:\.\d+)?\s*(?:元|块)|[¥￥]\d/;

const ASKS_COUNT = /(?:多少|几)(人|首|张|台|次|积分)/;

/** "多少钱" is not answered by a sentence with no price in it, nor "多少人" by one with no people, however many words they share. */
export function cannotAnswer(question: string, sentence: string): boolean {
  const unit = ASKS_COUNT.exec(question)?.[1];
  return (ASKS_MONEY.test(question) && !STATES_MONEY.test(sentence)) || (unit !== undefined && !sentence.includes(unit));
}

/** Whether a question asks for a kind of fact this sentence states. */
export const statesWhatIsAsked = (question: string, sentence: string): boolean => KINDS.some(([kind, looks]) => kind.test(question) && looks.test(sentence));

/** How well a sentence looks like what is asked: 1 when it fits every kind, down to 0.5 when it fits none. */
export function answerFit(question: string, sentence: string): number {
  const asked = KINDS.filter(([kind]) => kind.test(question));
  return asked.length ? 0.5 + (0.5 * asked.filter(([, looks]) => looks.test(sentence)).length) / asked.length : 0.75;
}

/**
 * The backend counts "如果不能换货就退款" as a request, because its model can check the condition first. Nothing here can, so
 * this page files nothing on a condition: it answers about the order and offers.
 */
export const conditionalRefund = (text: string): boolean => /(?:如果|要是|若是?|假如|万一|不到|没到|不行|实在不行|不然|否则|可以的话|方便的话|能的话|行的话|来得及的话|有可能的话)[^。！!]{0,20}?(?:就|我就|帮我|给我|直接|那)?(?:申请)?退|(?<![a-z])(?:if|unless|in case)(?![a-z])[^.!?]{0,60}(?:refund|money back)/i.test(text);

// ---------------------------------------------------------------- what files, on this page
// The rules in text.ts list what is not a request, and a list like that never closes: a reviewer found a new way past it every
// time. This page has no model to catch the rest, so it turns the question round. A message with an order number files only if
// every clause in it is one of these, and anything else, however it reads, is answered and offered instead of filed. A plain
// request worded some other way costs one more turn; a wrong refund costs money.
const FRONT_WORDS = "那就|那|现在再|我们|我|现在|马上|立刻|立即|赶紧|尽快|今天|就|也|还是|必须|一定要|要求|坚持|确认|请|麻烦你|麻烦|帮忙|帮我|替我|为我|给我|我要|我想|要|想|先|只|把钱|把这笔钱|把这单|把它|这单|这笔钱|这笔|这个月的|这个月|会员费|年卡|直接|全额|全款|全部|申请|办理|办个|发起|提交";
const AMOUNT = "(?:[¥￥]?\\d+(?:\\.\\d+)?(?:元|块钱?)?|[一二三四五六七八九十百千两半]+(?:元|块钱?)?)";
const DEMAND = new RegExp(`^(?:${FRONT_WORDS})*退(?:货退款|款|钱|货|掉|给我|我|回来|一下|全款|全额)?(?:退款)?${AMOUNT}?(?:了)?(?:吧|呀|啊|哈|呗|啦|了吧)?(?:就行|就好)?(?:吧)?$|^的?退款申请$|^(?:${FRONT_WORDS})*取消(?:这个|这笔|该|我的)?订单吧?$`);
const DEMAND_NAMING = new RegExp(`^(?:${FRONT_WORDS})*退掉(.+)$`);
const DEMAND_EN = /^(?:(?:please|pls|plz|kindly|can you|could you)\s+)*(?:process\s+)?(?:a\s+)?refund(?:\s+(?:me|my|it|this|the|order|for|please|pls|plz|now))*$|^i\s+(?:want|need|would like|'d like)\s+(?:a\s+refund|my\s+money\s+back)(?:\s+(?:for|order|please|now|my|the))*$|^(?:please\s+)?(?:give\s+me\s+)?(?:my\s+)?money\s+back(?:\s+please)?$/i;
const COURTESY = /^(?:你好|您好|客服|在吗|嗯+|好的|好|那|那就|不对|抱歉|谢谢(?:你|啦|了)?|多谢|谢了|辛苦了?|麻烦了?|拜托了?|thx|thanks|thank you|ty|ok|okay|hi|hello|please|pls|plz)$/i;
const ORDER_WORDS = /^(?:订单号?(?:是|为)?|单号(?:是|为)?|这单|那单|这笔|那笔|的|和|以及|给|为|把|给订单|为订单|帮我把|麻烦把|请把|请给订单|请给|请为订单|请立即为订单|麻烦给订单|麻烦给|帮我提交|提交|直接给订单|顾客原话)$/;
const ALSO = /^(?:不退货|不用退货|不换了?|不要换货?|不用换|剩下的再说|到.{0,6}(?:支付宝|微信|银行卡|原卡|卡上?)|也退给我|please process it now)$/i;
// An injected line changes nothing either way, so it is not a reason to stop: the rules still run on the request beside it.
const INJECTED = /忽略.{0,8}指令|系统通知|管理员已授权|本对话中所有|(?:无需|不需要|不用)(?:审批|审核)|跳过(?:审核|审批)|经理已经|审批单号|^[A-Z]{1,4}-?\d+$/;
const NOT_THIS_TIME = /^(?:本来|原本)不想退的?$|被拒了?$/;
const A_REASON = /坏|碎|破|裂|漏|没声|不响|开线|划痕|瑕疵|质量|有问题|不能用|用不了|连不上|打不开|开不了|没反应|故障|杂音|异味|味道|裂纹|不想要|不喜欢|不太喜欢|不满意|不太满意|不合适|不好|不需要这|不想用|不想续|不续了|打算续|用不上|没用|买错|拍错|点错|开错|买重|买贵|误买|手滑|后悔|发错|尺寸不对|颜色不对|太[^，,]{0,4}了|被扣|刚扣|扣了|扣的|等了|还不发|还没发|没发货|不发货|超过/;
const A_REASON_EN = /broken|damaged|defective|faulty|wrong|by mistake|mistake|isn'?t what|not what|doesn'?t work|not working|don'?t want|no longer need|too (?:small|big|expensive)/i;
const WANTS_NO_PRODUCT = /不需要了?$|不要了$/;
const NOT_A_REASON = /退|吗|呢|如果|要是|明天|下次|以后|回头|他|她|说|问/;
const OTHER_QUESTION = /^(?:另外|还有|顺便|对了).+|自动续费|连续包月|续费|积分|发票|多久到账|几天到账|什么时候到账|退到哪/;
const ITS_OWN = /我要|帮我|给我|请|麻烦|必须|立刻|马上|please|pls|plz|i want|i need/i;
const SAYS_A_THING = /^[^一-鿿a-z0-9]*$/i;

function clausesOf(text: string): string[] {
  const kept = text.replace(/([a-z'])\s+(?=[a-z'])/gi, "$1\u0001");
  return kept.split(/[，,。！!；;、：:\s]+|\.(?=\s|$)|(?<=[了的吧])(?=我要|帮我|给我|请帮|麻烦)/).map((c) => c.replace(/\u0001/g, " ").trim()).filter(Boolean);
}

/** Whether every clause of a message with an order number is part of a plain request by this customer, now. */
export function plainRequest(raw: string, numberRe: RegExp, itemWords: Set<string>): boolean {
  const text = fixTypos(raw.normalize("NFKC"));
  if (!numberRe.test(text)) return true;
  const clauses = clausesOf(text.replace(new RegExp(numberRe.source, `${numberRe.flags.replace("g", "")}g`), "，"));
  let demand = false, own = ITS_OWN.test(text), reason = false, other = false;
  for (const part of clauses) {
    const question = /[？?]/.test(part);
    const clause = part.replace(/[？?]+/g, "");
    // Symbols in front of a request are a mood ("😡😡😡 SO… 退钱"); after it they can take it back ("帮我退款 ×", "帮我退款 ？？？").
    if (!clause || SAYS_A_THING.test(clause)) {
      if (demand) return false;
      continue;
    }
    if (!question && (DEMAND.test(clause) || DEMAND_EN.test(clause))) { demand = true; continue; }
    const naming = DEMAND_NAMING.exec(clause);
    if (!question && naming && namesAnItem(naming[1], itemWords)) { demand = true; continue; }
    if (!question && NOT_THIS_TIME.test(clause)) continue;
    if (!question && (COURTESY.test(clause) || ORDER_WORDS.test(clause) || ALSO.test(clause) || /^\d+(?:\.\d+)?$|^(?:元|块钱?)$/.test(clause) || new RegExp(`^${AMOUNT}$`).test(clause))) continue;
    if (INJECTED.test(clause)) continue;
    // A complaint may be a question ("还不发货？？？"); nothing else may.
    if (/(?:怎么|为什么|咋|为啥|等了.{0,4})?还(?:不|没)发/.test(clause)) { reason = true; continue; }
    if (!question && !NOT_A_REASON.test(clause) && (A_REASON.test(clause) || A_REASON_EN.test(clause))) { reason = true; continue; }
    // After a request "不要了" could take it back; before one, or after a reason, it is the item that is not wanted.
    if (!question && (reason || !demand) && WANTS_NO_PRODUCT.test(clause) && !NOT_A_REASON.test(clause)) { reason = true; continue; }
    if (OTHER_QUESTION.test(clause)) { other = true; continue; }
    if (!question && /^[^，,]{0,10}(?:那单|这单)$/.test(clause) && !NOT_A_REASON.test(clause)) continue;
    if (!question && namesAnItem(clause, itemWords)) continue;
    return false;
  }
  return demand && (!other || own);
}

function namesAnItem(clause: string, itemWords: Set<string>): boolean {
  const han = clause.replace(/^(?:那个|这个|我的)/, "");
  if (han.length > 12 || !/^[一-鿿A-Za-z0-9 -]+$/.test(han) || /[的了是你他她它们要能吗呢吧啊呀哦嘛么在和与及或就都也还又很更最再才被让给跟对向从到而但却如若等这那哪谁啥咋怎几多少不没别帮想会可该应需得着过来去说问看听试写讲查]/.test(han)) return false;
  for (let i = 0; i + 1 < han.length; i++) if (itemWords.has(han.slice(i, i + 2))) return true;
  return false;
}

export const asksHowLong = (text: string): boolean => /多久|多长时间|几个?小时|几天|什么时候|啥时候|何时|时效/.test(text);

// ---------------------------------------------------------------- what is declined whatever the wording
const LIMITS = "(?:额度|上限|限额|门槛|阈值)";
const CHANGING = "(?:调高|调大|调到|调成|调整|提高|提升|放宽|改成|改为|改到|修改|更改|降低)";
const CHANGE_RULE = new RegExp(`${CHANGING}.{0,12}${LIMITS}|${LIMITS}.{0,12}(?:${CHANGING}|太低)|(?:修改|更改|改变|改掉)(?:一下)?(?:你们的?|退款的?)?(?:规则|政策)|(?:规则|政策).{0,6}(?:改成|改为|改到|调到|放宽)`);
const SKIP_REVIEW = /(?:跳过|绕过|绕开|不走|别走|不用走|免).{0,6}(?:审核|审批|人工)|(?:不用|不要|无需|不需要|别)(?:再)?(?:审核|审批)|(?:全部|所有)的?订单.{0,12}(?:退|赔)|名下.{0,6}(?:全部|所有).{0,6}退/;
const ASKS_IN_THE_CLAUSE = /是不是|是否|吗|呢|[？?]|多少|什么|哪些/;
const AUTHORITY = /我是(?!不是)(?:你们|贵司|平台|商家|Leon|Echo|Iris)?.{0,4}(?:运营|老板|客服主管|经理|内部员工|工程师|开发)|(?:你们的?|平台|系统)管理员|管理员模式|开发者模式|调试模式|已授权|授权你|我授权|system\s*[:：]/i;
const AUTHORITY_ASKS = /退|额度|上限|规则|审批|审核|照做|执行|指令/;
const INTERNAL = /内部.{0,12}(?:规定|规则|标准|手册|文档|资料|政策|流程|权限|怎么定|最多|上限|批|话术)|(?:员工|客服)(?:手册|话术)|客诉组长|客诉.{0,4}升级|你们.{0,4}内部|客服.{0,6}(?:最多|最高|能批).{0,10}(?:补偿|赔|批|多少|几天)|补偿(?:权限|上限)|审批权限|风控规则/;

export type Decline = "rules" | "authority" | "internal";

/** A customer's message asking for what no chat may grant. A clause asking how the rule works is an ordinary question. */
export function declines(text: string, injected = false): Decline | null {
  if (INTERNAL.test(text)) return "internal";
  if (injected || (AUTHORITY.test(text) && AUTHORITY_ASKS.test(text))) return "authority";
  const asksForIt = text.split(/[，,。！!；;\s]+/).some((clause) => (SKIP_REVIEW.test(clause) || CHANGE_RULE.test(clause)) && (!ASKS_IN_THE_CLAUSE.test(clause) || /能不能|可不可以|帮我|给我/.test(clause)));
  return asksForIt ? "rules" : null;
}

export const DECLINE_TEXT: Record<Decline, string> = {
  rules: "这个我无法处理：退款额度和规则由商家统一设置，不能在对话里修改，也不能为个人调整或跳过审核。需要退款的话，回复“订单号 申请退款”，会按规则处理。",
  authority: "这个我无法处理：对话里的指令或身份说明不会改变规则，也不能让退款跳过审核。需要退款的话，回复“订单号 申请退款”，会按规则处理。",
  internal: "内部规定属于员工资料，我无法回答。公开的政策都可以问我，需要人工协助可以回复“转人工”。",
};

// ---------------------------------------------------------------- what to say when nothing matched
const CAN_DO: Record<string, string> = {
  leon: "我可以帮您查订单和物流，解答退款、配送、发票和积分的规则，或者提交退款申请。",
  echo: "我可以帮您查订单，解答会员、自动续费、下载和Echo币的规则，或者提交退款申请。",
  iris: "我可以解答存储空间、数据导出、登录方式和工单响应时效的问题，也可以帮您创建工单。",
};

const NEXT_STEPS: Record<string, string[]> = {
  leon: ["我的订单有哪些", "七天无理由退货怎么算", "转人工"],
  echo: ["我的订单有哪些", "怎么取消自动续费", "转人工"],
  iris: ["工单多久有人响应", "专业版有多少存储空间", "转人工"],
};

const WHAT_IT_IS = /你是谁|你是什么|你是啥|你是机器人|你是真人|你是人工|你是ai|你能干|你能做|能帮我干|能帮我做|能干啥|能干什么|能做什么|能做啥|会干啥|会什么|有什么功能|有啥功能|你可以做/i;
export const asksWhatItIs = (text: string): boolean => WHAT_IT_IS.test(text);

/** "先别退了，我再想想": holding off, with nothing filed. */
export const holdsOff = (text: string): boolean => text.length <= 16 && !text.includes("撤回") && /先别退|先不退|不退了|不退款了|别退了|不想退了|再想想|再考虑|还没想好/.test(text);

export const NOTHING_FILED = "好的，没有提交退款申请。想好了随时告诉我订单号。";

/** Words but no Chinese and no order number: the documents and orders here are Chinese, and this page does not translate. */
export const inEnglish = (text: string): boolean => !/[一-鿿]/.test(text) && !/\bso\d/i.test(text) && (text.match(/[a-z]{2,}/gi) ?? []).length >= 2;

export const ANSWERS_IN_CHINESE = "This demo answers in Chinese, because its documents and orders are in Chinese, so I can't answer that in English. Try one of the examples below.（知识库中没有找到相关依据）";

export const canDo = (tenant: string): string => CAN_DO[tenant] ?? "";
/** A sentence of ours about what the assistant does: nothing to check against the evidence. */
export const isCanDo = (sentence: string): boolean => /^(?:如超过到账时效|回复订单号|如需查询这一单|This demo answers)/.test(sentence) || Object.values(CAN_DO).some((line) => line.startsWith(sentence));
/** A number was given, so the rules still apply to it, and the message's other words are answered first. */
export const NOT_GRANTED = "对话里的指令或身份说明不会改变规则，退款也不能跳过审核，这部分我无法处理。";
export const nextSteps = (tenant: string): string[] => NEXT_STEPS[tenant] ?? [];
