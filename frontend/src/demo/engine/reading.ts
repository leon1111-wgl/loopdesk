/**
 * How the browser engine reads a question beyond the offline model's rules. The backend has a model for colloquial wording; the
 * static demo has none, so the documents' own words are mapped from how people say them, an answer is picked by the kind of
 * fact asked for, and what no rule may grant in a chat is declined up front. Money is not decided here: text.ts still does that.
 */

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
  [/可以.{0,4}退(?!回)|能.{0,4}退(?!回)|退.{0,2}吗|退不退/, "七天无理由退货 扣费后 申请退款"],
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
  [/年卡|单月|包月/, "会员类型 价格 连续包月"],
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
const FILLERS = new Set("大概 大约 之后 以后 然后 的话 要是 如果 想来 还是 现在 目前 一般 通常 可能 应该 就是 还有 比如 那么 这么 这样 那样 非常 特别 比较 有点 半天 彻底 有人 不够 一下 一点 一些 别人 自己 咱们 我们 他们 平台 知道 想问 请问 问下 问问 谢谢 麻烦 感觉 其实 真的 到底 究竟 而且 但是 不过 所以 因为 怎么样 找到 没找到 一个月 一年 刚才 刚刚 昨天 今天 明天 几天 几次 回来 回去 另外 资料 东西 收到 期间 期限 时候 时间 情况 办法 地方 这种 那种 过了".split(" "));
export const isFiller = (word: string): boolean => FILLERS.has(word);

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
  [/能不能|能否|可不可以|可以吗|能.{0,8}吗|是否|会不会|有没有|支持吗/, /可以|可申请|可在|支持|不支持|不能|无法/],
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
export const conditionalRefund = (text: string): boolean => /(?:如果|要是|若是?|假如|万一|不到|没到|不行|实在不行|不然|否则|可以的话|方便的话|能的话|行的话|来得及的话|有可能的话)[^。！!]{0,20}?(?:就|我就|帮我|给我|直接|那)?(?:申请)?退/.test(text);

export const asksHowLong = (text: string): boolean => /多久|多长时间|几个?小时|几天|什么时候|啥时候|何时|时效/.test(text);

// ---------------------------------------------------------------- what is declined whatever the wording
const THE_RULES = "(?:额度|上限|限额|门槛|规则|政策|阈值|审批|审核|期限)";
const CHANGE_RULE = new RegExp(`(?:调高|调大|调到|调成|调整|提高|提升|放宽|改成|改为|改到|修改|更改|取消|关掉|降低).{0,12}${THE_RULES}|${THE_RULES}.{0,12}(?:调高|调大|调到|调成|调整|提高|提升|放宽|改成|改为|改到|修改|更改|太低)`);
const SKIP_REVIEW = /(?:跳过|绕过|绕开|不走|别走|不用走|免).{0,6}(?:审核|审批|人工)|(?:不用|不要|无需|不需要|别)(?:再)?(?:审核|审批)|全部(?:都)?(?:订单)?.{0,4}退款|所有订单.{0,12}(?:退|赔)|名下.{0,6}(?:全部|所有).{0,6}退/;
const AUTHORITY = /我是.{0,8}(?:运营|管理员|老板|主管|经理|开发|工程师|员工|同事|客服|负责人|技术)|管理员模式|开发者模式|调试模式|已授权|授权你|我授权|system\s*[:：]/i;
const AUTHORITY_ASKS = /退|改|调|规则|额度|上限|审批|审核|权限|照做|执行|指令/;
const INTERNAL = /内部.{0,12}(?:规定|规则|标准|手册|文档|资料|政策|流程|权限|怎么定|最多|上限|批|话术)|(?:员工|客服)(?:手册|话术)|客诉组长|你们.{0,4}内部|客服.{0,6}(?:最多|最高|能批|能赔|能补|能给|可以给|可以补).{0,10}(?:补偿|赔|多少|几天)|补偿(?:权限|标准|上限)|审批权限|升级流程|风控/;

export type Decline = "rules" | "authority" | "internal";

/** A customer's message asking for what no chat may grant. */
export function declines(text: string, injected = false): Decline | null {
  if (INTERNAL.test(text)) return "internal";
  if (injected || (AUTHORITY.test(text) && AUTHORITY_ASKS.test(text))) return "authority";
  if (SKIP_REVIEW.test(text) || CHANGE_RULE.test(text)) return "rules";
  return null;
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

export const canDo = (tenant: string): string => CAN_DO[tenant] ?? "";
/** A sentence of ours about what the assistant does: nothing to check against the evidence. */
export const isCanDo = (sentence: string): boolean => sentence.startsWith("如超过到账时效") || Object.values(CAN_DO).some((line) => line.startsWith(sentence));
/** A number was given, so the rules still apply to it, and the message's other words are answered first. */
export const NOT_GRANTED = "对话里的指令或身份说明不会改变规则，退款也不能跳过审核，这部分我无法处理。";
export const nextSteps = (tenant: string): string[] => NEXT_STEPS[tenant] ?? [];
