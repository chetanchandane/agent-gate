# 90 second demo

A walkthrough of agent-gate from a clean clone. The offline steps need no API
key; the live steps need `ANTHROPIC_API_KEY` and spend under the 15 USD cap.

## Setup (once)

```bash
make install
cp .env.example .env   # set ANTHROPIC_API_KEY
```

## The script

1. **(0:00) The problem.** "Every change to an agent's behavior should earn its
   way in with evidence, not vibes. agent-gate makes an improver LLM propose
   policy edits, then a deterministic gate accepts an edit only if it improves a
   dev set, breaks nothing in a protected safeguard set, and holds on a hidden
   holdout set."

2. **(0:10) Everything validates offline.**
   ```bash
   make check
   ```
   "Lint, types, and the whole offline test suite pass with no API key. The gate,
   graders, and metrics are pure functions, so they are unit tested directly."

3. **(0:25) Baseline the hand written policy.**
   ```bash
   agent-gate baseline
   ```
   "v1 is deliberately incomplete. The baseline runs it on all three sets with
   k = 5 and flags any weak safeguard case."

4. **(0:40) Run the improvement loop.**
   ```bash
   agent-gate improve --rounds 6
   ```
   "Each round the improver sees only dev failures and safeguard pass or fail
   flags, never holdout. The policy guard blocks oversized or overfit edits, and
   the gate accepts or rejects each candidate with the numbers that justify it."

5. **(1:00) Prove the gate is not decorative.**
   ```bash
   agent-gate adversarial
   ```
   "Three hand written bad policies run through the same gate. A rejection here
   is the gate doing its job; it is reported honestly either way."

6. **(1:15) Read the result.**
   ```bash
   agent-gate report && open reports/report.html
   ```
   "The report shows first versus latest on the frozen benchmark, the version
   history, and the most important table: every rejected candidate with the
   exact case ids it regressed."

7. **(1:25) Spend stayed in budget.**
   ```bash
   agent-gate cost
   ```
   "Total spend against the cap, from the run cache. The whole demo is under
   15 USD, and a repeated run costs nothing because scored attempts are cached."

## One command

```bash
make demo
```

Runs validate, baseline, improve, adversarial, report, and cost in sequence.
