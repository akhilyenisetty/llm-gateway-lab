"""Accuracy metrics for the classifier eval (pure functions, unit-tested)."""

from collections import Counter
from statistics import mean


def accuracy(gold: list[str], predicted: list[str]) -> float:
    if not gold:
        return 0.0
    return sum(g == p for g, p in zip(gold, predicted, strict=True)) / len(gold)


def confusion(gold: list[str], predicted: list[str]) -> dict[str, dict[str, int]]:
    """confusion[gold][predicted] = count."""
    table: dict[str, Counter[str]] = {}
    for g, p in zip(gold, predicted, strict=True):
        table.setdefault(g, Counter())[p] += 1
    return {g: dict(c) for g, c in table.items()}


def per_class(
    gold: list[str], predicted: list[str], classes: list[str]
) -> dict[str, dict[str, float | int | None]]:
    """Precision, recall, and support per class. None where undefined (nothing predicted,
    or no gold examples), rather than a misleading 0."""
    out: dict[str, dict[str, float | int | None]] = {}
    for c in classes:
        tp = sum(g == c and p == c for g, p in zip(gold, predicted, strict=True))
        predicted_c = sum(p == c for p in predicted)
        support = sum(g == c for g in gold)
        out[c] = {
            "precision": tp / predicted_c if predicted_c else None,
            "recall": tp / support if support else None,
            "support": support,
        }
    return out


def consistency(runs: list[list[str]]) -> float:
    """Fraction of items given the same answer in every run (how stable the classifier is)."""
    if not runs or not runs[0]:
        return 0.0
    return mean(len(set(answers)) == 1 for answers in zip(*runs, strict=True))
