/**
 * The logic of backend/app/textutil.py, on the backend's own regular expressions (pack.ts). Money only moves on an
 * explicit request: the functions here decide that, and gates.test.ts compares every one of them with the Python
 * original on the phrases of the backend's test suite.
 */
import { T, pack } from "./pack";

const GLOBAL = new Map<RegExp, RegExp>();

function global(re: RegExp): RegExp {
  let copy = GLOBAL.get(re);
  if (!copy) {
    copy = new RegExp(re.source, re.flags.includes("g") ? re.flags : `${re.flags}g`);
    GLOBAL.set(re, copy);
  }
  copy.lastIndex = 0;
  return copy;
}

/** re.finditer */
export function findAll(re: RegExp, text: string): RegExpExecArray[] {
  return [...text.matchAll(global(re))];
}

/** re.sub */
export function sub(re: RegExp, replacement: string | ((match: string, ...groups: string[]) => string), text: string): string {
  return text.replace(global(re), replacement as never);
}

/** pattern.search(text, pos): lookbehinds still see what comes before pos. */
export function searchFrom(re: RegExp, text: string, pos: number): RegExpExecArray | null {
  const copy = global(re);
  copy.lastIndex = pos;
  return copy.exec(text);
}

/** Full-width digits and Latin letters as ASCII ("ＳＯ２０２６…"); Chinese punctuation stays. */
export function normalizeWidth(text: string): string {
  return text.replace(/[０-９Ａ-Ｚａ-ｚ＋－]/g, (c) => {
    if (c === "＋") return "+";
    if (c === "－") return "-";
    return String.fromCharCode(c.charCodeAt(0) - 0xfee0);
  });
}

const T2S = new Map<string, string>();
for (let i = 0; i + 1 < pack.t2s.length; i += 2) T2S.set(pack.t2s[i], pack.t2s[i + 1]);

/** script.canonical: traditional input read in simplified (a character table, not OpenCC's phrases). */
export function canonical(text: string): string {
  return [...text].map((c) => T2S.get(c) ?? c).join("");
}

const TYPOS: [RegExp, string][] = pack.texts.typos.map(([source, right]) => [new RegExp(source), right]);

export function fixTypos(text: string): string {
  let out = text || "";
  for (const [pattern, right] of TYPOS) out = sub(pattern, right, out);
  return out;
}

// ---------------------------------------------------------------- numerals (app/voice/itn.py cn_number)
const CN_DIGIT: Record<string, string> = { 零: "0", 〇: "0", 一: "1", 幺: "1", 二: "2", 三: "3", 四: "4", 五: "5", 六: "6", 七: "7", 八: "8", 九: "9" };
const CN_VALUE: Record<string, number> = { 零: 0, 〇: 0, 一: 1, 幺: 1, 二: 2, 两: 2, 兩: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9 };
const SMALL_UNIT: Record<string, number> = { 十: 10, 百: 100, 千: 1000 };
const BIG_UNIT: Record<string, number> = { 万: 10_000, 萬: 10_000, 亿: 100_000_000, 億: 100_000_000 };

export function cnNumber(text: string): number | null {
  if (!text) return null;
  if ([...text].every((c) => c in CN_DIGIT) && text.length > 1) return Number([...text].map((c) => CN_DIGIT[c]).join(""));
  let total = 0, section = 0, number = 0, lastUnit = 1, seenDigit = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch in CN_VALUE) {
      number = CN_VALUE[ch];
      seenDigit = true;
    } else if (ch in SMALL_UNIT) {
      const unit = SMALL_UNIT[ch];
      if (!seenDigit && (ch === "十" || (i === 0 && "百千".includes(ch)))) number = 1;
      section += number * unit;
      number = 0; lastUnit = unit; seenDigit = false;
    } else if (ch in BIG_UNIT) {
      section += number;
      total = ch === "亿" || ch === "億" ? (total + section) * BIG_UNIT[ch] : total + section * BIG_UNIT[ch];
      section = 0; number = 0; lastUnit = BIG_UNIT[ch]; seenDigit = false;
    } else {
      return null;
    }
  }
  if (number && text.length >= 2 && "百千万萬".includes(text[text.length - 2]) && lastUnit >= 100) number *= Math.floor(lastUnit / 10);
  return total + section + number;
}

function numeralAmounts(text: string): string {
  return sub(T._CN_AMOUNT_RE, (numeral) => {
    const value = cnNumber(numeral);
    return value === null ? numeral : String(value);
  }, text);
}

export function numeralCounts(text: string): string {
  return text.replace(global(T._CN_COUNT_RE), (numeral: string, offset: number) => {
    if (T._RANGE_RE.source && new RegExp(`^(?:${T._RANGE_RE.source})$`).test(numeral)) return numeral;
    if (text.slice(Math.max(0, offset - 1), offset + 2).startsWith("万一")) return numeral;
    const value = cnNumber(numeral);
    return !value ? numeral : String(value);
  });
}

// ---------------------------------------------------------------- who asks for a person
function fullMatch(re: RegExp, text: string): boolean {
  return new RegExp(`^(?:${re.source})$`, re.flags.replace("g", "")).test(text);
}

export { fullMatch };

export function asksForPerson(text: string): boolean {
  const stripped = sub(/[\s，,。.！!？?~～]+/, "", sub(T.NEGATED_PERSON_RE, "，", normalizeWidth(text)));
  return Boolean(stripped) && T._EXPLICIT_PERSON_RE.test(stripped);
}

export function handoffCancelled(text: string): [boolean, string] {
  const parts = normalizeWidth(text || "").split(/[，,。.！!？?；;~～\s]+/).filter(Boolean);
  const hit = parts.filter((p) => fullMatch(T._HANDOFF_CANCEL_CLAUSE, p));
  if (!hit.length) return [false, text];
  return [true, parts.filter((p) => !hit.includes(p) && !fullMatch(T._CANCEL_FILLER, p)).join("，")];
}

// ---------------------------------------------------------------- the money rules
function inTimingQuestion(before: string): boolean {
  const all = findAll(T._TIMING_WORD, before);
  if (!all.length) return false;
  const last = all[all.length - 1];
  const rest = before.slice(last.index + last[0].length);
  return rest.length <= T_TIMING_REACH && !T._TIMING_ENDS.test(rest);
}

const T_TIMING_REACH = pack.settings.timing_reach;

function clauseOf(match: RegExpExecArray, text: string): [string, string] {
  const matchEnd = match.index + match[0].length;
  let start = 0, end = text.length;
  for (const sep of findAll(T._CLAUSE_END, text)) {
    const sepEnd = sep.index + sep[0].length;
    if (sepEnd <= match.index) start = sepEnd;
    else if (sep.index >= matchEnd) {
      end = /[？?]/.test(sep[0]) ? sepEnd : sep.index;
      if (fullMatch(T._TAG_QUESTION, text.slice(sepEnd))) end = text.length;
      break;
    }
  }
  return [text.slice(start, match.index), text.slice(matchEnd, end)];
}

function askedWhether(match: RegExpExecArray, text: string): boolean {
  const [before, after] = clauseOf(match, text);
  if (/^[A-Za-z]/.test(match[0])) return T._EN_ASKS.test(before + match[0] + after);
  if (T._ASKED_WHETHER.test(before) || T._WHETHER_JUST_BEFORE.test(text.slice(0, match.index - before.length))) return true;
  if (T._NAMES_WHO.test(before.slice(-2) + match[0])) return false;
  if (match[0].startsWith("就") && T._CAN_THEN.test(before)) return !fullMatch(T._FINISHES_DEMAND, after);
  return T._ASKING_BEFORE.test(before) || !fullMatch(T._FINISHES_DEMAND, after);
}

function namedDemand(pattern: RegExp, text: string): boolean {
  for (const match of findAll(pattern, text)) {
    const before = text.slice(0, match.index);
    if (T._REPORTED_BEFORE.test(before) || inTimingQuestion(before) || askedWhether(match, text)) continue;
    return true;
  }
  return false;
}

function asksWhatIf(text: string): boolean {
  const condition = T._CONDITION_RE.exec(text);
  if (!condition || !searchFrom(T._QUESTION_RE, text, condition.index)) return false;
  for (const clause of findAll(T._CLAUSE_RE, text)) {
    const part = clause[0];
    for (const request of findAll(T.REFUND_APPLY_RE, part)) {
      const end = request.index + request[0].length;
      if (searchFrom(T._QUESTION_RE, part, end)) continue; // "…，钱会退我原来的卡吗" asks
      const starts = findAll(T._CONDITION_RE, part).map((m) => m.index).filter((s) => s <= request.index);
      if (!starts.length) {
        if (!T._QUESTION_RE.test(part.slice(0, request.index))) return false; // "我要退款，如果…"
        continue;
      }
      const lastStart = starts[starts.length - 1];
      const consequence = searchFrom(T._CONSEQUENCE_RE, part, lastStart);
      if (lastStart < request.index && consequence && consequence.index <= request.index) return false; // "如果可以的话帮我退款，多久到账"
    }
  }
  return true;
}

const BARE_DEMAND_MAX = pack.settings.bare_demand_max;

function demandsRefund(text: string): boolean {
  const questions = [T.REFUND_INQUIRY_RE, T._QUESTION_RE, T._COLLOQUIAL_QUESTION_RE].flatMap((p) => findAll(p, text).map((m) => m.index));
  const lastQuestion = Math.max(-1, ...questions);
  let start = 0, said = false, whether = false;
  const seps: (RegExpExecArray | null)[] = [...findAll(T._DEMAND_SEP_RE, text), null];
  for (const sep of seps) {
    const end = sep ? sep.index : text.length;
    const bare = sub(T._DEMAND_FILLER_RE, "", sub(T.ORDER_ID_RE, "SO0", text.slice(start, end)));
    if (start > lastQuestion && bare.length > 0 && bare.length <= BARE_DEMAND_MAX && fullMatch(T._BARE_DEMAND_RE, bare) && !whether) {
      if (!fullMatch(T._BARE_TOPIC_RE, bare) || said || (sep && /[！!]/.test(sep[0]))) return true;
    }
    said = said || Boolean(bare);
    whether = fullMatch(T._ONLY_WHETHER_RE, bare);
    start = sep ? sep.index + sep[0].length : end;
  }
  return false;
}

function retracted(text: string): boolean {
  let last: RegExpExecArray | null = null;
  for (const match of findAll(T.NEGATED_REFUND_RE, text)) {
    if (match[0].includes("退货") || match[0].includes("退掉")) continue;
    if (/^\s*(?:我?就|的话|我要投诉|投诉)/.test(text.slice(match.index + match[0].length))) continue;
    last = match;
  }
  if (!last) return false;
  const before = text.slice(0, last.index), after = text.slice(last.index + last[0].length);
  const askedBefore = T.REFUND_APPLY_RE.test(before) || T._IMPLIED_DEMAND_RE.test(before) || demandsRefund(before);
  return askedBefore && !(T.REFUND_RE.test(after) || demandsRefund(after));
}

function otherQuestionClause(clause: string): boolean {
  return T._REAL_QUESTION_RE.test(clause) && T._OTHER_SUBJECT_RE.test(clause)
    && !T._REFUND_TALK_RE.test(sub(T._OTHER_SUBJECT_RE, "", clause));
}

function asksAboutRefund(text: string): boolean {
  if (T._HEDGED_RE.test(text)) return true;
  return text.split(T._CLAUSE_CUT_RE).filter((c) => c !== undefined).some((clause) => T.REFUND_INQUIRY_RE.test(clause) && !otherQuestionClause(clause));
}

export function refundWithdrawRequested(text: string): boolean {
  const folded = sub(T._CJK_SPACE_RE, "", sub(T.ORDER_ID_RE, "", normalizeWidth(text || "")));
  return T.WITHDRAW_REFUND_RE.test(folded) && (T.REFUND_RE.test(folded) || folded.includes("退")) && !T._NOT_WITHDRAW_RE.test(folded);
}

/** True when the message asks for a refund, not when it asks about one or declines one. */
export function refundRequested(text: string): boolean {
  if (refundWithdrawRequested(text)) return false;
  const folded = numeralAmounts(normalizeWidth(text || ""));
  if (retracted(folded)) return false;
  const kept = sub(T.NEGATED_REFUND_RE, "，", folded);
  if (T._ONLY_ASKING_RE.test(kept)) return false;
  if (!T.REFUND_RE.test(kept)) return demandsRefund(kept);
  if (asksWhatIf(kept)) return false;
  if (namedDemand(T.REFUND_APPLY_RE, kept) || demandsRefund(kept)) return true;
  return namedDemand(T._IMPLIED_DEMAND_RE, kept) && !asksAboutRefund(kept);
}

export function otherQuestion(text: string): string | null {
  if (!refundRequested(text)) return null;
  const clause = normalizeWidth(text).split(T._CLAUSE_CUT_RE).find((c) => c !== undefined && otherQuestionClause(c));
  return clause ? sub(T.ORDER_ID_RE, "", clause).replace(/^[ ，,。？?]+|[ ，,。？?]+$/g, "") : null;
}

export function withdrawConfirmed(text: string): boolean {
  return fullMatch(T._WITHDRAW_CONFIRM_RE, sub(T._CONFIRM_FILLER_RE, "", sub(T.ORDER_ID_RE, "SO0", normalizeWidth(text || ""))));
}

export function refundBareDecline(text: string): boolean {
  const folded = sub(T.ORDER_ID_RE, "", normalizeWidth(text || ""));
  if (!T.NEGATED_REFUND_RE.test(folded)) return false;
  const rest = sub(T.NEGATED_REFUND_RE, "，", folded);
  return !(T.REFUND_INQUIRY_RE.test(rest) || T._QUESTION_RE.test(rest) || T.REFUND_RE.test(rest));
}

export function statedRefundAmount(text: string): number | null {
  const kept = sub(T.NEGATED_REFUND_RE, "，", numeralAmounts(normalizeWidth(text || "")));
  const match = T._REFUND_AMOUNT_RE.exec(kept);
  let number: string | null = null;
  if (match) number = /\d+(?:\.\d+)?/.exec(match[0])?.[0] ?? null;
  else {
    const bare = T._BARE_REFUND_AMOUNT.exec(kept);
    if (bare) number = bare[1].replace(/,/g, "");
  }
  return number !== null && Number(number) > 0 ? Number(number) : null;
}

export function cancelRequested(text: string): boolean {
  const folded = normalizeWidth(text || "");
  return T.CANCEL_ORDER_RE.test(folded) && T._CANCEL_VERB_RE.test(folded) && !(T.REFUND_INQUIRY_RE.test(folded) || T._QUESTION_RE.test(folded));
}

export function refundDeclined(text: string): boolean {
  const folded = normalizeWidth(text || "");
  if (T.CANCEL_ORDER_RE.test(folded) && !T.REFUND_RE.test(folded)) return !cancelRequested(folded);
  return (T.REFUND_RE.test(folded) || T.NEGATED_REFUND_RE.test(folded)) && !refundRequested(folded);
}

// ---------------------------------------------------------------- query forms
const SYNONYMS = pack.texts.query_synonyms;
const VARIANT = (text: string) => text.replace(/帐/g, "账");

function numberExtras(text: string): string[] {
  const digits = numeralCounts(text);
  const extras = findAll(/\d+\s*(?:个工作日|工作日|个小时|小时|个月|天|日|周|年|块|元)/, digits).map((m) => m[0]).filter((m) => !text.includes(m));
  if (T._REFUND_DAYS_RE.test(digits)) extras.push("退款 到账 工作日");
  return extras;
}

export function expandQuery(text: string): string {
  const folded = VARIANT(fixTypos(text));
  const extras = [...new Set([...Object.entries(SYNONYMS).filter(([phrase]) => folded.includes(phrase)).map(([, canon]) => canon), ...numberExtras(folded)])];
  return extras.length ? `${folded} ${extras.join(" ")}` : folded;
}

export function canonicalQuery(text: string): string {
  const folded = VARIANT(fixTypos(text));
  const canon = sub(T._SYNONYM_RE, (m) => ` ${SYNONYMS[m]} `, folded);
  const numbers = numberExtras(folded);
  return numbers.length ? `${canon} ${numbers.join(" ")}` : canon;
}

export function splitSentences(text: string): string[] {
  return text.split(T._SENT_RE).filter((p) => p !== undefined).map((p) => p.trim()).filter(Boolean);
}

export function stripCitations(text: string): [string, number[]] {
  const refs = findAll(T.CITE_RE, text).map((m) => Number(m[1]));
  return [sub(T.CITE_RE, "", text).trim(), refs];
}

export function extractNumbers(text: string): number[] {
  return findAll(T._NUM_RE, sub(T._THOUSANDS_RE, "", text)).map((m) => Number(m[0]));
}

/** python's "%.2f" */
export const money = (value: number): string => value.toFixed(2);

// ---------------------------------------------------------------- segmentation (jieba's max-probability route)
const FREQ = new Map<string, number>();
let maxWord = 1;
for (const item of pack.words.split(" ")) {
  const match = /^(.*?)(\d+)$/.exec(item);
  if (!match) continue;
  FREQ.set(match[1], Number(match[2]) / 2);
  maxWord = Math.max(maxWord, match[1].length);
}
const LOG_TOTAL = Math.log2(60_101_967); // jieba's dictionary total
// jieba's HMM keeps the characters of a word it does not know together ("打分"), and function words apart. These stand
// alone; any other characters next to each other are an unknown word and keep their weight in the relevance gate.
const FUNCTION_CHARS = new Set("的了是我你他她它们要能吗呢吧啊呀哦嘛么在有和与及或就都也还又很太更最再才把被让给跟对向从到为以于而但却如若等个这那哪谁啥咋怎几多少不没别请帮想会可该应需得着过来去上下里外中前后时候好大小一二三四五六七八九十两百千万元块天月年日号次件台张本买卖用说看听吃喝拿放问查退换发收寄送钱货单");
const STOP = new Set(pack.texts.stop_tokens);

function cutHan(block: string): string[] {
  const n = block.length;
  const best = new Array<number>(n + 1).fill(0);
  const next = new Array<number>(n + 1).fill(n);
  for (let i = n - 1; i >= 0; i--) {
    best[i] = -Infinity;
    for (let j = Math.min(n, i + maxWord); j > i; j--) {
      const bucket = FREQ.get(block.slice(i, j));
      if (bucket === undefined && j > i + 1) continue;
      const score = (bucket ?? 0) - LOG_TOTAL + best[j];
      if (score > best[i]) {
        best[i] = score;
        next[i] = j;
      }
    }
  }
  const words: string[] = [];
  let run = "";
  const flush = () => {
    if (run.length > 1 && !FREQ.has(run)) words.push(run);
    else if (run) words.push(...run);
    run = "";
  };
  for (let i = 0; i < n; i = next[i]) {
    const word = block.slice(i, next[i]);
    if (word.length === 1 && !FUNCTION_CHARS.has(word)) run += word;
    else {
      flush();
      words.push(word);
    }
  }
  flush();
  return words;
}

/** jieba.lcut_for_search: every word, and the dictionary words inside the longer ones. */
function cutForSearch(block: string): string[] {
  const out: string[] = [];
  for (const word of cutHan(block)) {
    if (word.length > 2) for (let i = 0; i + 2 <= word.length; i++) if (FREQ.has(word.slice(i, i + 2))) out.push(word.slice(i, i + 2));
    if (word.length > 3) for (let i = 0; i + 3 <= word.length; i++) if (FREQ.has(word.slice(i, i + 3))) out.push(word.slice(i, i + 3));
    out.push(word);
  }
  return out;
}

// A word that mixes scripts ("Echo币") is one word, as jieba.add_word makes it.
const MIXED = new Set(pack.texts.mixed_words.map((w) => w.toLowerCase()));
const PIECES = new RegExp(`${pack.texts.mixed_words.join("|")}|[\\u4e00-\\u9fff]+|[A-Za-z0-9]+`, "i");
const TOKEN_CACHE = new Map<string, string[]>();

/** Tokens for BM25 and overlap scoring. Single Chinese characters and stop words are dropped. */
export function tokenize(text: string): string[] {
  const cached = TOKEN_CACHE.get(text);
  if (cached) return cached;
  const tokens: string[] = [];
  for (const match of findAll(PIECES, text)) {
    const piece = match[0];
    if (MIXED.has(piece.toLowerCase())) tokens.push(piece.toLowerCase());
    else if (/^[\u4e00-\u9fff]/.test(piece)) {
      for (const word of cutForSearch(piece)) if (word.length > 1 && !STOP.has(word)) tokens.push(word);
    } else {
      const word = piece.toLowerCase();
      if (!STOP.has(word)) tokens.push(word);
    }
  }
  if (text.length <= 2000 && TOKEN_CACHE.size < 4096) TOKEN_CACHE.set(text, tokens);
  return tokens;
}

export const tokenSet = (text: string): Set<string> => new Set(tokenize(text));

export function overlapRecall(claim: string, evidence: string): number {
  const claimTokens = tokenSet(claim);
  if (!claimTokens.size) return 1;
  const have = tokenSet(evidence);
  let hit = 0;
  for (const t of claimTokens) if (have.has(t)) hit += 1;
  return hit / claimTokens.size;
}

export function isRefusal(answer: string): boolean {
  return REFUSAL.test(answer);
}

const REFUSAL = new RegExp(pack.rules.outcome.REFUSAL_RE.source, pack.rules.outcome.REFUSAL_RE.flags);

// ---------------------------------------------------------------- a yes or a no (app/voice/confirm.py)
const C = Object.fromEntries(Object.entries(pack.rules.confirm).map(([name, rule]) => [name, new RegExp(rule.source, rule.flags)]));
const YES_WORDS = pack.texts.yes_words, BA_OK = new Set(pack.texts.ba_ok);

function clauseIsYes(raw: string): boolean | null {
  let clause = sub(C._LEAD, "", raw);
  if (!clause) return null;
  if (clause.endsWith("吧") && !YES_WORDS.includes(clause)) {
    const bare = clause.replace(/吧+$/, "");
    if (!BA_OK.has(bare)) return false;
    clause = bare;
  }
  for (const candidate of [clause, sub(C._PARTICLES, "", clause)]) {
    for (const word of YES_WORDS) {
      if (candidate && candidate.length % word.length === 0 && candidate === word.repeat(candidate.length / word.length)) return true;
    }
  }
  return false;
}

/** "yes", "no", or null when the reply is not clearly an answer. A false yes moves money. */
export function classifyReply(text: string): "yes" | "no" | null {
  const raw = (text || "").trim().toLowerCase();
  const t = sub(C._PUNCT, "", raw);
  if (!t || t.length > 12) return null;
  if (C._NO.test(t)) return "no";
  if (C._NOT_AN_ANSWER.test(t)) return null;
  const verdicts = raw.split(C._PUNCT).filter(Boolean).map(clauseIsYes).filter((v): v is boolean => v !== null);
  return verdicts.length && verdicts.every(Boolean) ? "yes" : null;
}
