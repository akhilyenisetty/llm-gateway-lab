# Evaluation

## Session classifier (Stage 4)

The gateway assigns each session one category: the client's `X-Category` tag if it sent
one, otherwise a classification by Claude Haiku from a digest of the conversation (see
[README](../README.md#categories)). This section measures that classifier against
hand-labeled sessions.

### Result

| | |
|---|---|
| **Accuracy** | **88.4%** (38 of 43 sessions), identical in all 3 runs |
| Mixed or borderline sessions | 75.0% (6 of 8) |
| Classifier | `claude-haiku-5-5`, effort `low`, structured outputs, prompt `v1` |
| Categories | v3 of [`config/categories.yaml`](../config/categories.yaml) (sha256 `15bb6d2cf90c`) |
| Data | 43 synthetic sessions, each labeled by hand, blind to how it was generated |
| Runs | 3 per session; all 3 gave the same answer for every session |
| Cost | $0.016 for all 3 runs (about $0.00012 per session classified) |
| Date | 2026-10-18 |

No prompt tuning was done on this data (see [Prompt tuning](#prompt-tuning-not-done)), so
all 43 sessions are unseen test data.

Per category (one session counted once; all runs agreed):

| Human label | Sessions | Recall | Precision |
|---|---|---|---|
| coding | 10 | 60% | 100% |
| writing | 6 | 100% | 67% |
| analysis | 10 | 90% | 100% |
| question_answering | 4 | 100% | 80% |
| other | 13 | 100% | 93% |

Confusion (rows: human label, columns: classifier):

| | coding | writing | analysis | question_answering | other |
|---|---|---|---|---|---|
| **coding** | 6 | 2 | 0 | 1 | 1 |
| **writing** | 0 | 6 | 0 | 0 | 0 |
| **analysis** | 0 | 1 | 9 | 0 | 0 |
| **question_answering** | 0 | 0 | 0 | 4 | 0 |
| **other** | 0 | 0 | 0 | 0 | 13 |

The weak spot is **coding recall**: sessions whose work is code but whose visible
deliverable looks like text (for example, updating a README and then running its
quickstart) are called `writing`. The classifier never answers `coding` wrongly
(precision 100%).

### How the number was reached: the definitions were the bottleneck

| Run | Categories | Labels | Accuracy | Mixed |
|---|---|---|---|---|
| 1 | v1 | first labeling pass | 79.1% | 50.0% |
| 2 | v2 | first pass, copied to v2 | 79.1% | 62.5% |
| 3 | v3 | careful second pass (made under v2, carried to v3) | **88.4%** | 75.0% |

The classifier (model, effort, prompt) was the same in all three runs. Everything that
changed was the definitions and the labels:

1. **v1 → v2: making boundary rules explicit.** In run 1, four of the five mistakes on
   the dev half were sessions where the classifier followed the written definitions and
   the label did not (for example, reading logs to find a root cause "without changing
   anything" was labeled coding, while the definition calls findings `analysis`).
   Instead of tuning the prompt toward those labels, which would teach the model to
   contradict its own instructions, the labeler chose a rule for each boundary and the
   definitions were rewritten to state them: investigating a system without changing
   it is `analysis`; a question followed by an illustrative example is
   `question_answering`; personal advice is `other`; mixed sessions go to the category
   where most of the work went.
2. **Reusing old labels didn't help (run 2).** With v1 labels copied to v2, accuracy
   stayed at 79.1%: the rule changes fixed two sessions and exposed others where the old
   label contradicted the new rules.
3. **A careful relabeling pass.** The labeler relabeled all 43 sessions blind under v2
   (10 labels changed). A definition check before the next run (based on the rules, not
   on classifier output) found two general-knowledge questions labeled `other`, which v2
   called `question_answering`. The labeler confirmed this was intended, so **v3**
   narrows `question_answering` to technical and work questions. The v2 labels were
   carried to v3 unchanged, since v3 only changed the definition to match them.

The takeaway: the first measurement was capped by label noise and vague category
boundaries, not by the model. Making the rules explicit and labeling carefully raised
measured accuracy by 9 points without changing the classifier.

### Prompt tuning: not done

Tuning is allowed only on the dev half (a fixed hash split: 25 dev, 18 test), with the
test half reported once. After run 3, the two dev mistakes were:

- an essay-editing session labeled `coding`, which the definitions plainly call
  `writing` (the classifier's answer). It looks like a labeling slip, but it was left
  unchanged: correcting only the labels the classifier disagrees with biases the score
  upward.
- a README update ending in "run the quickstart", a genuine gray area between `writing`
  and `coding`.

Neither is a pattern worth encoding in the prompt; pushing README edits toward `coding`
would contradict the `writing` definition. So the prompt stays at `v1`.

### Method

- **Data.** Claude Sonnet 5.5 generated 7 sessions for each category and 8 deliberately
  mixed or borderline ones ([`evals/generate_synthetic.py`](../evals/generate_synthetic.py),
  $0.145). Generated sessions are validated (one generation returned a "placeholder"
  session and was regenerated). Each session is stored as the Messages API requests a
  client would send, so it goes through the same digest code as live traffic.
- **Labels.** [`evals/label.py`](../evals/label.py) shows only the digest the classifier
  sees and the definitions, in shuffled order, never the generator's intent or the
  source. Labels record the categories version they were made under; the eval uses only
  labels for the current version.
- **Runs.** [`evals/run_classifier_eval.py`](../evals/run_classifier_eval.py) classifies
  every labeled session 3 times and records the full configuration (model, effort,
  prompt version, category list and its hash, label provenance, git commit) in each
  report, so results can't be attributed to the wrong setup.

Reproduce:

```bash
.venv/bin/python -m evals.run_classifier_eval --runs 3   # writes results/classifier_eval_*.md
```

### Limitations

- **Synthetic data only.** Real gateway sessions are not in this measurement yet. The
  report shows a separate "real" row once they are exported
  (`python -m evals.export_real`, which needs `STORE_CONTENT=true`) and labeled.
- **Synthetic sessions are short** (at most 4 user turns), unlike long Claude Code
  sessions, and Claude both generated and classifies them.
- **Small sample.** One session is 2.3 points of accuracy, so differences of a session
  or two are noise.
- **One labeler**, including at least one probable labeling slip; no inter-annotator
  agreement was measured.
- **Effort `low` was not compared** with higher effort levels; with all runs already
  agreeing, run-to-run noise is not what limits accuracy here.
