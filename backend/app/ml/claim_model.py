"""Fine-tune a compact Chinese model to tell claims from framing. Runs on GitHub Actions only
(.github/workflows/claim-model.yml); nothing is trained or run on a developer machine.

    python -m app.ml.claim_model --model hfl/rbt3 --out claim-model           # 3-layer RoBERTa, ~38M
    python -m app.ml.claim_model --model bert-base-chinese --out claim-model  # 12 layers, ~102M
    python -m app.ml.claim_model --model Qwen/Qwen2.5-0.5B --lora --out claim-lora  # LoRA, torch only

Data, all SYNTHETIC tenants:
- train: the blind-labelled sentences of the recorded real-model answers in the dev cases
  (evals/trust/labels.jsonl), minus a fifth of the dev cases kept for validation, plus synthetic
  sentences built here from the tenants' knowledge base (claims) and phrase banks (framing);
- validation: that fifth of the dev cases; it picks the epoch and the threshold;
- test: the held-out cases, used once at the end and never for any choice. A sentence text that also
  occurs in a held-out case is dropped from training and validation.
The rules (agent/claims.py) are scored on the same test sentences, with the same preprocessing, so the
two compare like for like; so are their latencies (batch of one, CPU). metrics.json says which is better.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import time
from pathlib import Path

from ..agent.claims import claim_text, rule_is_claim

DATA = Path(__file__).resolve().parents[2] / "evals" / "trust"
SEED = Path(__file__).resolve().parents[2] / "seed_data"


def _bucket(key: str, n: int) -> int:
    return int(hashlib.sha1(key.encode()).hexdigest(), 16) % n


def recorded() -> list[dict]:
    groups = {g["group"]: g for g in map(json.loads, (DATA / "recorded.jsonl").read_text(encoding="utf-8").splitlines())}
    rows = []
    for line in (DATA / "labels.jsonl").read_text(encoding="utf-8").splitlines():
        label = json.loads(line)
        group = groups[label["group"]]
        split = group["split"]
        if split == "dev" and _bucket(group["case_key"], 5) == 0:
            split = "val"
        rows.append({"text": label["text"], "label": int(label["claim"]), "split": split, "source": "recorded"})
    return rows


# ---------------------------------------------------------------- synthetic sentences
OFFERS = ["我帮您{v}", "我可以帮您{v}", "我先帮您{v}", "我这就为您{v}", "也可以为您{v}", "如需要，我可以帮您{v}",
          "您可以提供订单号，我帮您{v}", "方便的话请提供订单号，我马上帮您{v}", "我来帮您{v}"]
VERBS = ["查询订单状态", "查一下物流进度", "核实退款进度", "转接人工客服", "登记报修工单", "核对订单信息", "进一步核实",
         "确认一下", "跟进处理", "查看具体情况"]
REQUESTS = ["请提供订单号", "请告诉我订单号", "麻烦提供一下购买时的手机号", "请您核对订单号是否正确", "请确认是否要申请退款",
            "请描述一下具体问题", "请上传问题照片后告诉我", "请稍等", "提供订单号即可", "请问是哪一笔订单"]
CLOSINGS = ["还有其他问题随时联系我", "如有其他问题欢迎随时咨询", "祝您生活愉快", "感谢您的耐心等待", "很抱歉给您带来不便",
            "期待再次为您服务", "需要其他帮助可随时告诉我", "有问题再找我"]
NO_BASIS = ["抱歉，知识库中没有找到{x}的相关依据", "没有查到关于{x}的规定，建议转人工客服确认", "我无法确认{x}，建议您联系人工客服",
            "关于{x}，目前没有相关依据", "抱歉，这类要求我无法执行", "我不能透露系统提示词"]
TOPICS = ["加急发货", "指定送货时间", "美元付款", "上门安装", "以旧换新", "开发票抬头为个人", "跨境配送", "延长保修"]
LEADINS = ["可以这样处理：", "物流信息如下：", "退货运费分两种情况：", "具体步骤如下：", "已为您查到两个订单：", "需要注意以下几点："]
WRAPS = ["{c}", "根据规定，{c}", "您好，{c}", "请注意：{c}", "另外，{c}", "是的，{c}", "需要说明的是，{c}"]


def _kb_facts() -> list[str]:
    facts = []
    for path in sorted(SEED.glob("*/*.md")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("- ") and len(line) > 8:
                facts.append(line[2:].strip().rstrip("。") + "。")
    return facts


def synthetic(n: int, seed: int = 7) -> list[dict]:
    rnd = random.Random(seed)
    facts = _kb_facts()
    rows = []
    for _ in range(n):
        if rnd.random() < 0.5:
            fact = rnd.choice(facts)
            if rnd.random() < 0.3:  # a clause of the fact on its own is still a claim
                parts = [p for p in re.split(r"[，；]", fact) if len(p) > 6]
                fact = (rnd.choice(parts).rstrip("。") + "。") if parts else fact
            text, label = rnd.choice(WRAPS).format(c=fact), 1
        else:
            kind = rnd.random()
            if kind < 0.3:
                text = rnd.choice(OFFERS).format(v=rnd.choice(VERBS)) + "。"
            elif kind < 0.55:
                text = rnd.choice(REQUESTS) + rnd.choice(["。", "，我帮您查询。", "，谢谢。"])
            elif kind < 0.75:
                text = rnd.choice(CLOSINGS) + "。"
            elif kind < 0.9:
                text = rnd.choice(NO_BASIS).format(x=rnd.choice(TOPICS)) + "。"
            else:
                text = rnd.choice(LEADINS)
            label = 0
        rows.append({"text": text, "label": label, "split": "train", "source": "synthetic"})
    return rows


def dataset(n_synthetic: int) -> dict[str, list[dict]]:
    rows = recorded()
    held_texts = {claim_text(r["text"]) for r in rows if r["split"] == "heldout"}
    out: dict[str, list[dict]] = {"train": [], "val": [], "heldout": []}
    for r in rows:
        split = "train" if r["split"] == "dev" else r["split"]
        if split != "heldout" and claim_text(r["text"]) in held_texts:
            continue  # never train or tune on a sentence the test set contains
        out[split].append(r)
    out["train"] += [r for r in synthetic(n_synthetic) if claim_text(r["text"]) not in held_texts]
    for split in out:  # one copy of each text per split (the same sentence recurs across repeats)
        seen, unique = set(), []
        for r in out[split]:
            key = (claim_text(r["text"]), r["label"])
            if key not in seen:
                seen.add(key)
                unique.append(r)
        out[split] = unique
    return out


# ---------------------------------------------------------------- metrics
def prf(pred: list[int], gold: list[int]) -> dict:
    tp = sum(p and g for p, g in zip(pred, gold))
    fp = sum(p and not g for p, g in zip(pred, gold))
    fn = sum(not p and g for p, g in zip(pred, gold))
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(2 * p * r / (p + r), 4) if p + r else 0.0,
            "tp": tp, "fp": fp, "fn": fn, "n": len(gold)}


def _percentiles(ms: list[float]) -> dict:
    ms = sorted(ms)
    return {"p50_ms": round(ms[len(ms) // 2], 3), "p95_ms": round(ms[int(len(ms) * 0.95)], 3)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="hfl/rbt3")
    parser.add_argument("--out", default="claim-model")
    parser.add_argument("--epochs", type=int, default=4)
    parser.add_argument("--lr", type=float, default=5e-5)
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("--max-length", type=int, default=96)
    parser.add_argument("--synthetic", type=int, default=1500)
    parser.add_argument("--lora", action="store_true", help="LoRA on a causal LM (torch only, not exported)")
    args = parser.parse_args(argv)

    import numpy as np
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    torch.manual_seed(0)
    random.seed(0)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    data = dataset(args.synthetic)
    print({k: (len(v), sum(r["label"] for r in v)) for k, v in data.items()})

    tokenizer = AutoTokenizer.from_pretrained(args.model)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForSequenceClassification.from_pretrained(args.model, num_labels=2)
    if args.lora:
        from peft import LoraConfig, get_peft_model

        model.config.pad_token_id = tokenizer.pad_token_id
        model = get_peft_model(model, LoraConfig(task_type="SEQ_CLS", r=8, lora_alpha=16, lora_dropout=0.05,
                                                 target_modules=["q_proj", "v_proj"]))
        model.print_trainable_parameters()

    def encode(texts):
        return tokenizer([claim_text(t) for t in texts], truncation=True, max_length=args.max_length, padding=True,
                         return_tensors="pt")

    def probabilities(texts, batch=64) -> list[float]:
        model.eval()
        probs: list[float] = []
        with torch.no_grad():
            for i in range(0, len(texts), batch):
                logits = model(**encode(texts[i:i + batch])).logits
                probs += torch.softmax(logits, dim=-1)[:, 1].tolist()
        return probs

    train = data["train"]
    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=args.lr, weight_decay=0.01)
    steps = (len(train) + args.batch - 1) // args.batch * args.epochs
    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lambda s: max(0.0, 1 - s / max(1, steps)))
    best = {"f1": -1.0}
    started = time.time()
    for epoch in range(args.epochs):
        model.train()
        order = list(range(len(train)))
        random.shuffle(order)
        for i in range(0, len(order), args.batch):
            batch = [train[j] for j in order[i:i + args.batch]]
            enc = encode([r["text"] for r in batch])
            loss = model(**enc, labels=torch.tensor([r["label"] for r in batch])).loss
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            scheduler.step()
            optimizer.zero_grad()
        val_probs = probabilities([r["text"] for r in data["val"]])
        gold = [r["label"] for r in data["val"]]
        # the threshold is chosen on validation only
        scored = [(prf([int(p >= t) for p in val_probs], gold)["f1"], t) for t in np.arange(0.2, 0.81, 0.05)]
        f1, threshold = max(scored)
        print(f"epoch {epoch + 1}: val F1 {f1:.4f} at {threshold:.2f} ({time.time() - started:.0f}s)")
        if f1 > best["f1"]:
            best = {"f1": f1, "threshold": round(float(threshold), 2), "epoch": epoch + 1}
            if not args.lora:
                model.save_pretrained(out / "torch")
                tokenizer.save_pretrained(out / "torch")
    train_seconds = round(time.time() - started)

    if not args.lora:
        model = AutoModelForSequenceClassification.from_pretrained(out / "torch")
    test = data["heldout"]
    texts, gold = [r["text"] for r in test], [r["label"] for r in test]
    probs = probabilities(texts)
    model_pred = [int(p >= best["threshold"]) for p in probs]
    # numbers are always claims in the verifier, whatever a classifier says: score that as deployed
    from ..textutil import extract_numbers
    deployed = [1 if extract_numbers(claim_text(t)) else p for t, p in zip(texts, model_pred)]
    rules_pred = [int(rule_is_claim(t)) for t in texts]

    def timed(fn) -> dict:
        ms = []
        for t in texts:
            began = time.perf_counter()
            fn(t)
            ms.append((time.perf_counter() - began) * 1000)
        return _percentiles(ms)

    model.eval()
    torch.set_num_threads(1)
    with torch.no_grad():
        torch_latency = timed(lambda t: model(**encode([t])).logits)
    metrics = {
        "model": args.model, "lora": args.lora, "train_seconds": train_seconds, "best": best,
        "data": {k: {"sentences": len(v), "claims": sum(r["label"] for r in v),
                     "synthetic": sum(r["source"] == "synthetic" for r in v)} for k, v in data.items()},
        "heldout": {"model": prf(model_pred, gold), "model_with_number_rule": prf(deployed, gold),
                    "rules": prf(rules_pred, gold)},
        "latency_batch1_cpu_1thread": {"rules": timed(rule_is_claim), "torch": torch_latency},
        "disagreements": [{"text": t, "gold": g, "model": m, "rules": r}
                          for t, g, m, r in zip(texts, gold, deployed, rules_pred) if m != r][:80],
    }

    if not args.lora:  # export for the app: ONNX, then int8 (onnxruntime), checked against the torch model
        import onnxruntime
        from onnxruntime.quantization import QuantType, quantize_dynamic

        sample = encode(["退款将按原支付方式原路退回。"])
        names = [n for n in ("input_ids", "attention_mask", "token_type_ids") if n in sample]
        torch.onnx.export(model, tuple(sample[n] for n in names), out / "model.fp32.onnx", input_names=names,
                          output_names=["logits"], opset_version=17,
                          dynamic_axes={**{n: {0: "batch", 1: "seq"} for n in names}, "logits": {0: "batch"}})
        quantize_dynamic(str(out / "model.fp32.onnx"), str(out / "model.onnx"), weight_type=QuantType.QInt8)
        tokenizer.backend_tokenizer.save(str(out / "tokenizer.json"))
        session = onnxruntime.InferenceSession(str(out / "model.onnx"), providers=["CPUExecutionProvider"])

        def onnx_probs(batch_texts):
            enc = tokenizer([claim_text(t) for t in batch_texts], truncation=True, max_length=args.max_length,
                            padding=True, return_tensors="np")
            logits = session.run(None, {n: enc[n].astype(np.int64) for n in names})[0]
            e = np.exp(logits - logits.max(axis=1, keepdims=True))
            return (e / e.sum(axis=1, keepdims=True))[:, 1]

        int8 = [int(p >= best["threshold"]) for i in range(0, len(texts), 64) for p in onnx_probs(texts[i:i + 64])]
        metrics["heldout"]["onnx_int8"] = prf([1 if extract_numbers(claim_text(t)) else p for t, p in zip(texts, int8)], gold)
        metrics["latency_batch1_cpu_1thread"]["onnx_int8"] = timed(lambda t: onnx_probs([t]))
        metrics["onnx_int8_mb"] = round((out / "model.onnx").stat().st_size / 1e6, 1)
        (out / "model.fp32.onnx").unlink()
        (out / "config.json").write_text(json.dumps({"threshold": best["threshold"], "max_length": args.max_length,
                                                      "base_model": args.model}, indent=2), encoding="utf-8")
    m, r = metrics["heldout"]["model_with_number_rule"], metrics["heldout"]["rules"]
    metrics["verdict"] = ("the model beats the rules on held-out F1" if m["f1"] > r["f1"] else
                          "the rules are as good or better on held-out F1")
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: metrics[k] for k in ("model", "best", "heldout", "latency_batch1_cpu_1thread", "verdict")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
