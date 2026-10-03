# ctaio-signal-brief

A two-agent pipeline that turns live Hacker News discussion into a short, evidence-cited brief for
CTOs. Deterministic code gates sit between the agents, and nothing publishes until a person approves
it. Built in a 40-minute time-box for the Flywheel Motion × ctaio.dev assessment. It is a prototype,
not a production system.

## Workflow

```
fetch (code)        HN Algolia API: top "AI agents" stories, last 14 days, title + 6 top comments
   │                saved to runs/<id>/sources.json (replayable with --sources)
   ▼
Agent 1 Extractor   claude -p (Haiku). Reads the threads and returns JSON signals:
   │                {source_id, claim, evidence_quote, why_it_matters}
   ▼
Gate 1 (code)       schema · source_id exists · quote ≥ 40 chars and found VERBATIM in that source ·
   │                every number in the claim appears in the quote · no duplicate quotes.
   │                Survivors get ids E1..En. Fewer than 3 → retry once with the rejection reasons
   │                → otherwise fail closed.
   ▼
Agent 2 Editor      claude -p (Haiku). Sees ONLY the verified evidence, never the raw threads.
   │                Returns {headline, items[{text, evidence_ids}], takeaway}
   ▼
Gate 2 (code)       3–5 items · every block cites known evidence ids · every number traces to the
   │                cited evidence · no URLs · ≤ 70 words · headline ≤ 12 words · no hype words.
   │                Fails → retry once with the exact errors → otherwise fail closed.
   ▼
render (code)       Markdown. Thread links come from the source table, never from model text.
   ▼
HELD                Default deny. `--approve RUN_ID` publishes to published/ only if the brief's
                    SHA-256 still matches the one recorded at validation (edited after validation
                    → rejected). Re-approving is idempotent.

Every step appends one JSON line to runs/audit.jsonl: model, latency, hash of the raw output,
gate results with rejection reasons, approval, publish. Raw model outputs are kept in runs/<id>/.
```

**Why two agents.** Extraction and writing fail in different ways. The extractor's risk is
fabricated or paraphrased evidence, which a string check can catch. The editor's risk is adding
facts and numbers, so it is given only the evidence that passed and is checked against it. Neither
model grades its own output.

## Run

```bash
python3 pipeline.py                                   # live fetch → two agents → gates → HELD
python3 pipeline.py --inject-fabrication              # positive control: Gate 1 must reject a fake quote
python3 pipeline.py --sources examples/sources.json   # replay a saved input
python3 pipeline.py --approve <RUN_ID> --by <name>    # human approval → published/<RUN_ID>.md
```

Needs Python 3.9+ (standard library only) and the Claude Code CLI (`claude -p`) for the model calls.

## Where it broke and what I changed

All four are real. The evidence is in `runs/20261003T071304Z/` and `runs/audit.jsonl`.

1. **The model's JSON was malformed.** Asked for "ONLY JSON", Haiku still wrapped it in
   ```` ```json ```` fences, both in a smoke test and in every agent output in this run
   (`agent1_extract_try1.raw.txt`). A plain `json.loads` raised an error.
   **Fix:** `parse_json()` strips the fences and takes the outermost object, then the gates check the
   schema. If the output still isn't JSON, the agent is asked again once, with the parse error, and
   after that the run fails closed. I trust the schema check, not the prompt.
2. **Agent 1 quoted a real comment but cited the wrong thread.** It returned a quote with
   `source_id: S2`, but that text exists only in **S3**. It was a genuine quote with the wrong
   attribution, and would have been published with a link to the wrong discussion. Gate 1 rejected
   it ("evidence_quote not found verbatim in source").
   **What I changed:** I kept the check per cited source rather than "anywhere in the corpus". A
   corpus-wide check would have passed this quote with the wrong link.
3. **Positive control.** `--inject-fabrication` adds a fake "cut costs by 73%" quote. Gate 1 rejected
   it in the same run, which shows the verifier can actually fail.
4. **The Editor added a detail the evidence doesn't contain.** Item 1 says "advertiser networks";
   evidence E1 only says "third parties". Gate 2 checks numbers, citations, URLs and length, so it
   passed. A deterministic check can't catch that kind of meaning drift. That is why the output stops
   at **HELD** and why I did **not** approve this run.
   **Also tested:** editing the brief after validation and then approving it gives `approval rejected:
   brief changed after validation`, because of the SHA-256 match. A human can't approve something the
   gates never saw.

**Next fixes, with more time:** an entailment check per item (a small model with a fixed rubric
returning a yes/no per sentence, run alongside the deterministic checks rather than instead of them),
and a named-entity check between the brief and the cited quotes, which would have caught "advertiser
networks".

## Not built (time-box)

Scheduling, email delivery, subscriber personalisation, a UI, a database and multi-source ingestion.
Number checks are digit-based, so numbers written as words are not caught. The verbatim check proves
that a quote exists in the source, not that the claim is a fair reading of it; that part is the
human approver's job.
