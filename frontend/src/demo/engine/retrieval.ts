/**
 * Knowledge search: app/rag/retrieval.py without a server. BM25 and the offline embedder's lexical cosine, fused by
 * rank, then the relevance gate: the IDF-weighted share of the question's words a chunk covers. When the most specific
 * word ("货到付款") is missing, coverage falls and the answer is a refusal, not a neighbouring policy.
 */
import { pack, type KbChunk } from "./pack";
import { answerFit, cannotAnswer, documentWords, isFiller, namesNoTopic, refundRuleWords, saidOtherwise, statesWhatIsAsked, verbAsked } from "./reading";
import { canonicalQuery, expandQuery, splitSentences, tokenize } from "./text";

const RRF_K = 60;
const CANDIDATES = 12;
const MAX_SPLIT_WORD = 32;

interface Indexed {
  chunk: KbChunk;
  tf: Map<string, number>;
  length: number;
  features: Map<string, number>;
}

function counts(tokens: string[]): Map<string, number> {
  const tf = new Map<string, number>();
  for (const t of tokens) tf.set(t, (tf.get(t) ?? 0) + 1);
  return tf;
}

/** The lexical embedding of app/rag/embeddings.py, unhashed: a word, and every pair of neighbouring characters. */
function features(text: string): Map<string, number> {
  const raw = new Map<string, number>();
  for (const t of tokenize(text)) raw.set(`w:${t}`, (raw.get(`w:${t}`) ?? 0) + 1);
  const compact = text.replace(/[^一-鿿]/g, "");
  for (let i = 0; i + 1 < compact.length; i++) {
    const key = `c:${compact.slice(i, i + 2)}`;
    raw.set(key, (raw.get(key) ?? 0) + 0.5);
  }
  const out = new Map<string, number>();
  let norm = 0;
  for (const [k, tf] of raw) {
    const v = 1 + Math.log(tf + 1);
    out.set(k, v);
    norm += v * v;
  }
  norm = Math.sqrt(norm) || 1;
  for (const [k, v] of out) out.set(k, v / norm);
  return out;
}

function cosine(a: Map<string, number>, b: Map<string, number>): number {
  let sum = 0;
  for (const [k, v] of a) sum += v * (b.get(k) ?? 0);
  return sum;
}

export interface Hit {
  chunk: KbChunk;
  relevance: number;
  coverage: number;
  cosine: number;
  bm25Rank: number | null;
}

class TenantIndex {
  chunks: Indexed[];
  df = new Map<string, number>();
  avgdl: number;

  constructor(chunks: KbChunk[]) {
    this.chunks = chunks.map((chunk) => {
      const text = `${chunk.heading}\n${chunk.text}`;
      const tokens = tokenize(text);
      return { chunk, tf: counts(tokens), length: tokens.length, features: features(text) };
    });
    for (const c of this.chunks) for (const t of c.tf.keys()) this.df.set(t, (this.df.get(t) ?? 0) + 1);
    this.avgdl = this.chunks.length ? this.chunks.reduce((n, c) => n + c.length, 0) / this.chunks.length : 0;
  }

  idf(token: string): number {
    const df = this.df.get(token) ?? 0;
    return Math.log(1 + (this.chunks.length - df + 0.5) / (df + 0.5));
  }

  /** What a word says about the topic. Filler ("大概", "之后") says little, a number alone nothing; anything else the documents never use ("货到付款") names what is missing. */
  weight(token: string): number {
    if (/^\d+$/.test(token)) return 0;
    const idf = this.idf(token);
    if (namesNoTopic(token)) return idf * 0.1;
    if ((this.df.get(token) ?? 0) > 0) return idf;
    return idf * (isFiller(token) ? 0.1 : 0.8);
  }

  /** The words of a question to weigh: its own, less the everyday ones the vocabulary has read, plus what it read them as. */
  asked(text: string, base: string): string[] {
    const said = saidOtherwise(text, (words) => this.inDocuments(words) !== "");
    const own = this.knownParts(tokenize(base)).filter((t) => (this.df.get(t) ?? 0) > 0 || !said.some((s) => s.includes(t)));
    const mapped = this.knownParts(tokenize(this.inDocuments(documentWords(text))));
    const specific = [...own, ...mapped].some((t) => (this.df.get(t) ?? 0) > 0 && !/退|申请/.test(t));
    return [...own, ...mapped, ...(specific ? [] : this.knownParts(tokenize(this.inDocuments(refundRuleWords(text)))))];
  }

  bm25(tokens: string[], allowed: Set<string>): [number, number][] {
    const scores = new Map<number, number>();
    const k1 = 1.5, b = 0.75;
    for (const t of new Set(tokens)) {
      if (!this.df.has(t)) continue;
      const idf = this.idf(t);
      this.chunks.forEach((c, i) => {
        const tf = c.tf.get(t);
        if (!tf || !allowed.has(c.chunk.visibility)) return;
        const denom = tf + k1 * (1 - b + (b * c.length) / (this.avgdl || 1));
        scores.set(i, (scores.get(i) ?? 0) + (idf * tf * (k1 + 1)) / denom);
      });
    }
    return [...scores.entries()].sort((x, y) => y[1] - x[1] || x[0] - y[0]).slice(0, CANDIDATES);
  }

  vector(query: Map<string, number>, allowed: Set<string>): [number, number][] {
    const scored: [number, number][] = [];
    this.chunks.forEach((c, i) => {
      if (allowed.has(c.chunk.visibility)) scored.push([i, cosine(query, c.features)]);
    });
    return scored.filter((s) => s[1] > 1e-6).sort((x, y) => y[1] - x[1] || x[0] - y[0]).slice(0, CANDIDATES);
  }

  isKnown(token: string): boolean {
    return (this.df.get(token) ?? 0) > 0;
  }

  /** Of the words a phrase maps to, those this tenant's documents use. A piece of one they do not ("理由" of "七天无理由") is not that word. */
  inDocuments(words: string): string {
    return words.split(/\s+/).flatMap((word) => {
      const tokens = tokenize(word);
      return tokens.every((t) => this.isKnown(t)) || this.isKnown(word) ? tokens.filter((t) => this.isKnown(t)) : [];
    }).join(" ");
  }

  /** A word the knowledge base never uses, read as the KB words inside it when only single characters are left over. */
  knownParts(tokens: string[]): string[] {
    const out: string[] = [];
    for (const t of tokens) {
      if (t.length < 3 || t.length > MAX_SPLIT_WORD || (this.df.get(t) ?? 0) > 0) { // a pasted run: every slice would be tried
        out.push(t);
        continue;
      }
      const parts: string[] = [];
      const leftovers: string[] = [];
      let run = "", i = 0;
      while (i < t.length) {
        let advanced = false;
        for (let j = t.length; j > i + 1; j--) {
          if ((this.df.get(t.slice(i, j)) ?? 0) > 0) {
            parts.push(t.slice(i, j));
            if (run) leftovers.push(run);
            run = "";
            i = j;
            advanced = true;
            break;
          }
        }
        if (!advanced) {
          run += t[i];
          i += 1;
        }
      }
      if (run) leftovers.push(run);
      out.push(...(parts.length && leftovers.every((x) => x.length === 1 || isFiller(x)) ? parts : [t]));
    }
    return out;
  }

  coverage(tokens: string[], c: Indexed): number {
    const unique = new Set(tokens);
    let total = 0;
    for (const t of unique) total += this.weight(t);
    if (total === 0) return 0;
    const weak = this.weakParts(unique, c);
    let hit = 0;
    for (const t of unique) if (c.tf.has(t) && !weak.has(t)) hit += this.weight(t);
    return hit / total;
  }

  /** Sub-words of a compound the knowledge base has never seen count only if they cover most of it. */
  private weakParts(unique: Set<string>, c: Indexed): Set<string> {
    const weak = new Set<string>();
    for (const big of unique) {
      if (big.length < 3 || (this.df.get(big) ?? 0) > 0) continue;
      const parts = [...unique].filter((t) => t !== big && big.includes(t));
      const covered = new Set<number>();
      for (const t of parts) {
        if (c.tf.has(t)) {
          const start = big.indexOf(t);
          for (let i = start; i < start + t.length; i++) covered.add(i);
        }
      }
      if (parts.length && covered.size < 0.75 * big.length) parts.forEach((p) => weak.add(p));
    }
    return weak;
  }
}

const INDEXES = new Map<string, TenantIndex>();

function indexOf(tenant: string): TenantIndex {
  let index = INDEXES.get(tenant);
  if (!index) {
    index = new TenantIndex(pack.kb[tenant] ?? []);
    INDEXES.set(tenant, index);
  }
  return index;
}

/** hybrid_search: `gated` false returns the nearest chunks whatever their coverage, for the model tier to read. */
export function search(tenant: string, asked: string, allowed: Set<string>, topK: number, gated = true): Hit[] {
  const index = indexOf(tenant);
  const known = index.inDocuments(documentWords(asked));
  const gateTokens = index.asked(asked, canonicalQuery(asked));
  const query = `${expandQuery(asked)} ${known} ${index.inDocuments(refundRuleWords(asked))}`;
  const queryTokens = index.asked(asked, expandQuery(asked));
  const bm25 = index.bm25(queryTokens, allowed);
  const vec = index.vector(features(query), allowed);
  const cosineOf = new Map(vec);
  const bm25Rank = new Map(bm25.map(([i], r) => [i, r + 1]));
  const fused = new Map<number, number>();
  for (const list of [bm25, vec]) list.forEach(([i], r) => fused.set(i, (fused.get(i) ?? 0) + 1 / (RRF_K + r + 1)));
  const hits: Hit[] = [];
  const minRelevance = pack.settings.min_relevance;
  // Without the offline embedder's hash collisions equal ranks are common: the closer vector goes first.
  for (const [i] of [...fused.entries()].sort((x, y) => y[1] - x[1] || (cosineOf.get(y[0]) ?? 0) - (cosineOf.get(x[0]) ?? 0))) {
    const c = index.chunks[i];
    const coverage = index.coverage(gateTokens.length ? gateTokens : queryTokens, c);
    const cos = cosineOf.get(i) ?? 0;
    if (gated && coverage < minRelevance && cos < minRelevance) continue;
    hits.push({ chunk: c.chunk, relevance: Math.max(coverage, cos), coverage, cosine: cos, bm25Rank: bm25Rank.get(i) ?? null });
    if (hits.length === topK) break;
  }
  return hits;
}

// ---------------------------------------------------------------- the sentence that answers
export interface SentenceHit {
  ref: number;
  heading?: string;
  text?: string;
}

interface Picked {
  covers: number; // how much of the question's weight the sentence has
  score: number; // the same, for the kind of answer asked for
  ref: number;
  pos: number;
  sentence: string;
}

const CUT_MARK = pack.texts.mock.CUT_MARK;
const NOT_AN_ANSWER = /SYNTHETIC|虚构/;
const LEANS_BACK = /^(?:这类|这些|这种|其|该|可(?!以)|在.{0,10}基础上)/;
const COVERS = 0.34;
const COVERS_DESPITE = 0.5;
const COVERS_ITS_CLAUSE = 0.6;
const GOES_ON = 0.2;
const ON_TOPIC = 0.15;
const A_CLAUSE = /[，,。！!？?；;~～\s]+/;

/** A chunk's sentences; a part that only makes sense after the one before it ("这类扣费…") stays with it. */
function sentencesOf(text: string): string[] {
  const out: string[] = [];
  for (const raw of splitSentences(text)) {
    const sentence = raw.replace(/^[-*• ]+/, "").trimEnd();
    if (LEANS_BACK.test(sentence) && out.length) out[out.length - 1] = `${out[out.length - 1].replace(/[；;]+$/, "")}；${sentence}`;
    else out.push(sentence);
  }
  return out;
}

/** The longest run of Chinese both say word for word, when it is at least three characters: what segmentation can miss. */
function sharedRun(question: string, sentence: string): number {
  let best = 0;
  for (let i = 0; i < question.length; i++) {
    let n = 0;
    while (i + n < question.length && /[一-鿿]/.test(question[i + n]) && sentence.includes(question.slice(i, i + n + 1))) n += 1;
    best = Math.max(best, n);
  }
  return best >= 3 ? best : 0;
}

/** Whether the question's words add up to most of a longer word: "质量" and "问题" do say "质量问题". */
function sayTheWhole(asked: string[], word: string): boolean {
  return asked.filter((t) => t !== word && word.includes(t)).reduce((n, t) => n + t.length, 0) >= 0.75 * word.length;
}

function ranked(tenant: string, question: string, hits: SentenceHit[], oneQuestion = true): Picked[] {
  const index = indexOf(tenant);
  // The synonyms a search adds count only where the documents use them: "退货" in a music app was never asked about.
  const own = new Set(index.asked(question, question));
  const asked = [...new Set(index.asked(question, expandQuery(question)))].filter((t) => index.isKnown(t) || own.has(t));
  const total = asked.reduce((n, t) => n + index.weight(t), 0);
  if (!total) return [];
  const verb = verbAsked(question, (word) => index.isKnown(word));
  const out: Picked[] = [];
  hits.forEach((hit, rank) => {
    const heading = new Set(tokenize(hit.heading ?? ""));
    sentencesOf(hit.text ?? "").forEach((sentence, pos) => {
      if ([...sentence].length < 6 || NOT_AN_ANSWER.test(sentence) || sentence.endsWith(CUT_MARK) || (oneQuestion && cannotAnswer(question, sentence, hit.heading, verb))) return;
      const tokens = new Set(tokenize(sentence));
      const list = [...tokens];
      let direct = 0, context = 0;
      for (const t of asked) {
        // "手机" at the head of "手机号" modifies another word; "发票" at the end of "电子普通发票" is the word asked about.
        if (tokens.has(t)) direct += index.weight(t) * (list.some((o) => o.length > t.length && o.startsWith(t) && !sayTheWhole(asked, o)) ? 0.5 : 1);
        else if (heading.has(t)) context += index.weight(t);
      }
      if (!direct) return;
      const covers = (direct + 0.3 * context) / total;
      out.push({ covers, score: covers * answerFit(question, sentence) + 0.03 * Math.min(6, sharedRun(question, sentence)) - rank * 0.01 - pos * 0.005, ref: hit.ref, pos, sentence });
    });
  });
  return out.sort((a, b) => b.score - a.score);
}

const quoted = (p: Picked) => `${p.sentence.replace(/[。；;…]+$/, "")}[${p.ref}]。`;

function unknownWord(tenant: string, question: string): boolean {
  const index = indexOf(tenant);
  return index.asked(question, question).some((t) => !index.isKnown(t) && !isFiller(t) && !/^\d+$/.test(t));
}

/** The sentences that answer a question: the best for each of its clauses that says something of its own, then the best overall. */
export function answerSentences(tenant: string, question: string, hits: SentenceHit[], limit: number): string[] {
  const clauses = question.split(A_CLAUSE).filter((c) => c.length >= 3);
  // What one clause asks for ("多少钱") says nothing about the sentence another clause needs.
  const every = ranked(tenant, question, hits, clauses.length < 2);
  // A word the documents never use names something they may not cover ("纸质发票", "保修"): the rest must say more.
  const whole = every.filter((p) => p.covers >= (unknownWord(tenant, question) ? COVERS_DESPITE : COVERS));
  const overall = new Map(every.map((p) => [p.sentence, p]));
  const picked: Picked[] = [];
  const add = (p: Picked | undefined) => {
    if (p && picked.length < limit && !picked.some((q) => q.sentence === p.sentence)) picked.push(p);
  };
  if (clauses.length > 1) {
    for (const clause of clauses) {
      // A clause counts what the whole question says too: "审核要多久" after "怎么入驻" is about joining, not about refunds.
      const best = ranked(tenant, clause, hits).filter((p) => p.covers >= COVERS_ITS_CLAUSE && (overall.get(p.sentence)?.covers ?? 0) >= ON_TOPIC)
        .map((p) => ({ ...p, score: p.score + 0.5 * (overall.get(p.sentence)?.score ?? 0) })).sort((x, y) => y.score - x.score);
      add(best[0]);
    }
  }
  add(whole[0]);
  for (const p of whole) if (p.score >= 0.7 * whole[0].score) add(p);
  // The passage that answers often goes on in the next line: "审核周期为5个工作日", then what to submit.
  const refs = new Set(picked.map((p) => p.ref));
  for (const p of every) if (refs.has(p.ref) && p.covers >= GOES_ON && statesWhatIsAsked(question, p.sentence)) add(p);
  return picked.sort((a, b) => a.ref - b.ref || a.pos - b.pos).map(quoted);
}

/** The clauses of a run-on question, each searched on its own. */
export function clauseHits(tenant: string, question: string, allowed: Set<string>): Hit[] {
  const clauses = question.split(A_CLAUSE).filter((c) => c.length >= 3);
  return clauses.length > 1 ? clauses.flatMap((clause) => search(tenant, clause, allowed, 2)) : [];
}
