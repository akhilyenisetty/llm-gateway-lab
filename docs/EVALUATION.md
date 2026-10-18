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

## Context handoff (Stage 7)

When a session gets long, the gateway can write a structured **handoff document**
(see [README](../README.md#context-handoff)) for a fresh session to continue from. This
section measures how much of a session's context a fresh model gets from it, against
the obvious alternatives, and at what token cost.

### Result

| Briefing | Accuracy (mean of 3 runs) | Range | Reader input tokens | Cost to write |
|---|---|---|---|---|
| Raw transcript | 90.8% | 89.4–91.8% | 28,967 | $0 |
| **Structured handoff** | **89.5%** | 88.7–90.1% | **6,547** (23% of raw) | $0.145 |
| Plain summary | 71.3% | 69.5–72.6% | 2,869 | $0.093 |
| No briefing | 0.0% | 0.0% | 803 | $0 |

- **The structured handoff kept almost all of the context the raw transcript did** (89.5%
  vs 90.8%; the run ranges overlap, so the difference is within noise) **at under a
  quarter of the tokens.**
- **A plain summary is shorter but loses ~19 points**, mostly decisions and their reasons
  (43% vs 85% for the handoff) and failed attempts (79% vs 98%), which is exactly what
  the handoff's "decisions and why" and "tried and failed" sections exist for.
- **No briefing scored 0%**: the reader says "not in the briefing" rather than guessing,
  so guesses don't inflate the other scores.

### The saving grows with session length

| Session length | Raw transcript | Handoff | Handoff accuracy vs raw |
|---|---|---|---|
| Medium (6 cases) | 17,275 tokens | 6,241 (36% of raw) | 86.6% vs 87.3% |
| Long (6 cases) | 42,840 tokens | 7,387 (17% of raw) | 91.8% vs 93.5% |
| Real session (1) | 15,889 tokens | 3,338 (21% of raw) | 93.3% vs 95.0% |

The handoff stays at 6-7K tokens however long the session gets, while the raw transcript
grows with it, and its accuracy stays within 1-2 points of raw. (With 29K-token raw
transcripts, the Haiku reader did not yet lose information to length; at Claude Code's
real session sizes the raw option also stops fitting comfortably, which this eval
doesn't measure.)

Accuracy by question type (146 questions, 3 runs pooled):

| Question type | Raw | Summary | Handoff |
|---|---|---|---|
| Goal | 93.6% | 94.9% | 94.9% |
| Constraint | 94.9% | 84.6% | 94.9% |
| Decision (and why) | 89.2% | **43.2%** | 85.1% |
| Reversed decision (current approach and why) | 94.9% | 85.9% | 89.7% |
| Failed attempt (and why) | 100.0% | 78.8% | 97.7% |
| Key file | 98.5% | 77.3% | 97.0% |
| Current state | 91.1% | 82.2% | 92.2% |
| Open task | 63.6% | 71.2% | 72.7% |
| Next step | 83.3% | 69.7% | 80.3% |

Open tasks are the hardest for every briefing, including the raw transcript: they are
stated in passing late in the session and are easy to miss; the handoff's explicit "open
tasks" section beat even the raw transcript there.

### Method

- **Ground truth comes first.** For each of 12 synthetic cases, Claude Sonnet 5.5 wrote a
  fact sheet for a realistic coding scenario (goal, 3-4 decisions with reasons, one
  decision reversed mid-session, 2 failed attempts, constraints, key files, current
  state, open tasks, next step), then a coding-agent session containing those facts,
  then 10-12 quiz questions from the fact sheet. Questions are never written from a
  summary or handoff, so the eval can't be circular.
- **Facts are scattered on purpose.** Sessions are written in segments (2 for medium, 6
  for long), with facts assigned to segments in code: the reversed decision is agreed
  early and replaced late, one constraint is mentioned only once mid-session, and long
  sessions have stretches of exploration and side tasks with no new facts.
- **Every question is answerable.** Each question carries a verbatim quote from the
  transcript as evidence; a question whose quote isn't in the rendered transcript (what
  every briefing is built from) is dropped automatically (9 of 145 synthetic and 1 of
  11 real questions were).
- **One real case:** the expense-tracker Claude Code session from Stage 6, with 10
  questions written from its transcript and reviewed by the person who did the work (one
  question removed because it came from Claude Code's internal next-prompt suggestion,
  one corrected).
- **Four briefings per case and run**, all from the same rendered transcript: the
  transcript itself, a plain summary ("summarize for someone who will continue the
  work"), the structured handoff (the gateway's own code), and nothing. Summaries and
  handoffs are regenerated every run, so writer variability is included.
- **A reader** (Claude Haiku 5.5) answers all questions from one briefing, told to say
  "not in the briefing" rather than guess. **A judge** (Claude Opus 5.5, pinned, a
  different model from the writer) grades each answer against the reference as correct
  (1), partial (0.5), or wrong (0), by meaning, without knowing which briefing it came
  from.
- **Runs are checkpointed**: each graded unit is saved as it finishes, and `--resume`
  continues an interrupted run without redoing paid work.

| Setting | Value |
|---|---|
| Briefing writer | `claude-sonnet-5-5`, effort high; handoff prompt v1, summary prompt v1 |
| Reader | `claude-haiku-5-5`, effort medium |
| Judge | `claude-opus-5-5`, effort medium (pinned) |
| Dataset | 12 synthetic cases (6 medium ~9K tokens, 6 long ~24K tokens of transcript) + 1 real; 146 questions; sha256 `a398b80489af` |
| Runs | 3 |
| Cost | $13.93 for the 3 runs; dataset generation ~$9.30 (one-off) |
| Date, commit | 2026-10-18, `f6f321e` (plus the uncommitted Stage 7 code) |

Reproduce:

```bash
.venv/bin/python -m evals.generate_handoff_cases     # once (~$9); --resume fills gaps
.venv/bin/python -m evals.run_handoff_eval --runs 3   # --resume after an interruption
```

### Limitations

- **The reader is Haiku**, a small model. A stronger reader would likely do better on the
  raw transcript and summary too; the comparison between briefings is what this measures.
- **Synthetic sessions are written by Claude Sonnet**, the same model that writes the
  summaries and handoffs, which may favor its own style. Questions and grading are tied
  to the fact sheet and verified quotes, not to any briefing's wording, which limits this.
- **One real case.** The real session agrees with the synthetic result (93.3% vs 95.0%),
  but one case is anecdotal.
- **Medium sessions' minimum length was lowered** from 30K to 25K characters during
  generation after one theme repeatedly came out at 26-28K; every medium case still
  measured 30.6K-43.9K characters.
- **Questions test recall, not continuation.** The eval asks what the next session must
  know; it doesn't measure whether a model actually continues the work correctly.
