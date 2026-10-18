"""Measure the session classifier against the hand labels.

    python -m evals.run_classifier_eval                 # all labeled sessions, 3 runs
    python -m evals.run_classifier_eval --split dev     # tune on dev only
    python -m evals.run_classifier_eval --runs 1        # quick check

Single LLM runs are noisy, so the classifier runs several times and the report gives the
mean and range of accuracy, plus how often all runs agreed. The report records the full
configuration (classifier model, effort, prompt version, category list and its hash, git
commit) so a result can never be attributed to the wrong setup.

Writes results/classifier_eval_<timestamp>.json and .md (results/ is gitignored); the
summary for the docs is copied into docs/EVALUATION.md by hand.
"""

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from statistics import mean
from typing import Any

from evals import metrics
from evals.dataset import EvalSession, label_provenance, load_labels, load_sessions, split
from gateway.classifier import PROMPT_VERSION, ClassificationError, Classifier
from gateway.classifier import classifier_from_settings as make_classifier
from gateway.pricing import PriceTable
from gateway.settings import PROJECT_ROOT, get_settings

RESULTS_DIR = PROJECT_ROOT / "results"


def git_commit() -> str:
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
        dirty = subprocess.run(["git", "diff", "--quiet"], check=False).returncode != 0
        return sha + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


async def classify_all(
    classifier: Classifier, sessions: list[EvalSession], concurrency: int
) -> tuple[list[str], Decimal]:
    gate = asyncio.Semaphore(concurrency)

    async def one(session: EvalSession) -> tuple[str, Decimal]:
        async with gate:
            try:
                result = await classifier.classify(session.digest)
                return result.category, result.cost_usd or Decimal(0)
            except ClassificationError as exc:
                return f"ERROR:{exc}", Decimal(0)

    results = await asyncio.gather(*(one(s) for s in sessions))
    return [r[0] for r in results], sum((r[1] for r in results), Decimal(0))


def summarize(gold: list[str], runs: list[list[str]], classes: list[str]) -> dict[str, Any]:
    accuracies = [metrics.accuracy(gold, run) for run in runs]
    flat_gold = gold * len(runs)
    flat_pred = [p for run in runs for p in run]
    return {
        "n": len(gold),
        "accuracy_mean": mean(accuracies) if runs else None,
        "accuracy_min": min(accuracies) if runs else None,
        "accuracy_max": max(accuracies) if runs else None,
        "accuracy_per_run": accuracies,
        "consistency": metrics.consistency(runs),
        "per_class": metrics.per_class(flat_gold, flat_pred, classes),
        "confusion": metrics.confusion(flat_gold, flat_pred),
    }


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def markdown(report: dict[str, Any]) -> str:
    cfg = report["config"]
    lines = [
        f"# Classifier eval, {report['created_at']}",
        "",
        f"- Classifier: `{cfg['model']}`, effort `{cfg['effort']}`, "
        f"prompt `{cfg['prompt_version']}`",
        f"- Categories: v{cfg['categories_version']} ({', '.join(cfg['categories'])}), "
        f"sha256 `{cfg['categories_sha256'][:12]}`",
        f"- Sessions: {report['overall']['n']} labeled ({cfg['split']} split), "
        f"{cfg['runs']} runs each; git `{cfg['git_commit']}`",
        f"- Labels: {cfg['labels']['labeled_under_this_version']} labeled under "
        f"v{cfg['categories_version']}, {cfg['labels']['copied_from_earlier']} copied from an "
        "earlier version's labels",
        f"- Cost: ${report['cost_usd']}",
        "",
        "| Slice | n | Accuracy (mean) | Min | Max | All runs agree |",
        "|---|---|---|---|---|---|",
    ]
    for name, s in report["slices"].items():
        if s["n"]:
            lines.append(
                f"| {name} | {s['n']} | {pct(s['accuracy_mean'])} | {pct(s['accuracy_min'])} "
                f"| {pct(s['accuracy_max'])} | {pct(s['consistency'])} |"
            )
    lines += ["", "Per category (all runs pooled):", "",
              "| Category | Support | Precision | Recall |", "|---|---|---|---|"]  # fmt: skip
    for c, m in report["overall"]["per_class"].items():
        lines.append(
            f"| {c} | {m['support'] // cfg['runs']} | {pct(m['precision'])} | {pct(m['recall'])} |"
        )
    classes = cfg["categories"]
    lines += ["", "Confusion (rows: human label, columns: classifier; all runs pooled):", "",
              "| | " + " | ".join(classes) + " | other output |",
              "|---" * (len(classes) + 2) + "|"]  # fmt: skip
    for g in classes:
        row = report["overall"]["confusion"].get(g, {})
        extra = sum(v for k, v in row.items() if k not in classes)
        lines.append(
            f"| **{g}** | " + " | ".join(str(row.get(p, 0)) for p in classes) + f" | {extra} |"
        )
    if report["mistakes"]:
        lines += ["", "Sessions misclassified in at least one run:", ""]
        for m in report["mistakes"]:
            lines.append(f"- `{m['id']}`: label **{m['label']}**, predicted {m['predicted']}")
    return "\n".join(lines) + "\n"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.run_classifier_eval")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--split", choices=["all", "dev", "test"], default="all")
    parser.add_argument("--concurrency", type=int, default=5)
    args = parser.parse_args(argv)

    settings = get_settings()
    prices = PriceTable.load(settings.price_table_path)
    classifier = make_classifier(settings, prices)
    if classifier is None:
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 1

    labels = load_labels(classifier.categories.version)
    sessions = [
        s for s in load_sessions()
        if s.id in labels and (args.split == "all" or split(s.id) == args.split)
    ]  # fmt: skip
    if not sessions:
        print(
            f"No sessions labeled for categories v{classifier.categories.version}. "
            "Run python -m evals.label first.",
            file=sys.stderr,
        )
        return 1
    gold = [labels[s.id] for s in sessions]
    classes = classifier.categories.names

    runs: list[list[str]] = []
    cost = Decimal(0)
    for i in range(args.runs):
        print(f"run {i + 1}/{args.runs}: classifying {len(sessions)} sessions …", file=sys.stderr)
        predicted, run_cost = await classify_all(classifier, sessions, args.concurrency)
        runs.append(predicted)
        cost += run_cost

    def slice_of(keep: list[bool]) -> dict[str, Any]:
        g = [x for x, k in zip(gold, keep, strict=True) if k]
        r = [[x for x, k in zip(run, keep, strict=True) if k] for run in runs]
        return summarize(g, r, classes)

    categories_text = settings.categories_path.read_text()
    report: dict[str, Any] = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "config": {
            "model": classifier.model,
            "effort": classifier.effort,
            "prompt_version": PROMPT_VERSION,
            "categories": classes,
            "categories_version": classifier.categories.version,
            "categories_sha256": hashlib.sha256(categories_text.encode()).hexdigest(),
            "runs": args.runs,
            "split": args.split,
            "labels": label_provenance(classifier.categories.version),
            "git_commit": git_commit(),
        },
        "cost_usd": str(cost),
        "overall": summarize(gold, runs, classes),
        "slices": {
            "all": slice_of([True] * len(sessions)),
            "synthetic": slice_of([s.source == "synthetic" for s in sessions]),
            "real": slice_of([s.source == "real" for s in sessions]),
            "synthetic, asked for mixed": slice_of(
                [(s.generator or {}).get("asked_for") == "mixed" for s in sessions]
            ),
        },
        "mistakes": [
            {"id": s.id, "label": g, "predicted": [run[i] for run in runs]}
            for i, (s, g) in enumerate(zip(sessions, gold, strict=True))
            if any(run[i] != g for run in runs)
        ],
    }

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (RESULTS_DIR / f"classifier_eval_{stamp}.json").write_text(json.dumps(report, indent=2))
    text = markdown(report)
    (RESULTS_DIR / f"classifier_eval_{stamp}.md").write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
