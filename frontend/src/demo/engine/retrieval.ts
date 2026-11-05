/**
 * Knowledge search: app/rag/retrieval.py without a server. BM25 and the offline embedder's lexical cosine, fused by
 * rank, then the relevance gate: the IDF-weighted share of the question's words a chunk covers. When the most specific
 * word ("货到付款") is missing, coverage falls and the answer is a refusal, not a neighbouring policy.
 */
import { pack, type KbChunk } from "./pack";
import { canonicalQuery, expandQuery, tokenize } from "./text";

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
      out.push(...(parts.length && leftovers.every((x) => x.length === 1) ? parts : [t]));
    }
    return out;
  }

  coverage(tokens: string[], c: Indexed): number {
    const unique = new Set(tokens);
    let total = 0;
    for (const t of unique) total += this.idf(t);
    if (total === 0) return 0;
    const weak = this.weakParts(unique, c);
    let hit = 0;
    for (const t of unique) if (c.tf.has(t) && !weak.has(t)) hit += this.idf(t);
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
  const gateTokens = index.knownParts(tokenize(canonicalQuery(asked)));
  const query = expandQuery(asked);
  const queryTokens = index.knownParts(tokenize(query));
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
