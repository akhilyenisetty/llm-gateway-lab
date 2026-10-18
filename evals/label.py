"""Hand-label eval sessions, blind.

    python -m evals.label            # label sessions with no label for the current
                                     # categories version (all of them after a version bump)
    python -m evals.label --relabel  # go through every session again

Shows each session's digest (exactly what the classifier reads) and the category
definitions, nothing else: not the generator's intent, not whether it's synthetic or real.
Sessions come in a shuffled order. Each answer is appended to evals/data/labels.jsonl
immediately, so you can stop (q) and resume any time.
"""

import argparse
import json
import random
import textwrap
from datetime import UTC, datetime

from evals.dataset import LABELS_PATH, load_labels, load_sessions
from gateway.classifier import Categories
from gateway.settings import get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.label")
    parser.add_argument("--relabel", action="store_true")
    args = parser.parse_args(argv)

    categories = Categories.load(get_settings().categories_path)
    labels = load_labels(categories.version)
    sessions = [s for s in load_sessions() if args.relabel or s.id not in labels]
    # A new order per categories version, so relabeling is not a replay of the last pass.
    random.Random(f"order-v{categories.version}").shuffle(sessions)
    if not sessions:
        print("Nothing to label.")
        return 0

    keys = {str(i): name for i, name in enumerate(categories.names, start=1)}
    legend = "\n".join(
        f"  {k} {name}\n"
        + textwrap.indent(textwrap.fill(categories.definitions[name], 92), "      ")
        for k, name in keys.items()
    )
    print(f"{len(sessions)} sessions to label (categories v{categories.version}).")
    print(f"Tie-breaker: {categories.tie_breaker}\n")
    LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)

    for n, session in enumerate(sessions, start=1):
        print("=" * 100)
        print(f"[{n}/{len(sessions)}]\n")
        print(session.digest or "(empty)")
        print("\n" + legend)
        while True:
            answer = input("label [1-5], s=skip, q=quit: ").strip().lower()
            if answer == "q":
                return 0
            if answer == "s":
                break
            if answer in keys:
                record = {
                    "id": session.id,
                    "label": keys[answer],
                    "labeled_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "categories_version": categories.version,
                }
                with LABELS_PATH.open("a") as f:
                    f.write(json.dumps(record) + "\n")
                break
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
