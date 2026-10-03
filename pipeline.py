#!/usr/bin/env python3
"""ctaio.dev signal brief: a two-agent pipeline with deterministic gates.

  fetch     (code)   Hacker News threads via the public Algolia API
  Agent 1   (model)  Extractor: threads -> signals, each with a verbatim evidence quote (JSON)
  Gate 1    (code)   schema, known source, quote found verbatim in source, numbers traceable, dedupe
  Agent 2   (model)  Editor: sees ONLY verified evidence -> brief items citing evidence ids (JSON)
  Gate 2    (code)   schema, every block cites known evidence, numbers traceable, no URLs, length, hype
  render    (code)   links come from the source table, never from the model
  approval  (human)  output is HELD; `--approve RUN_ID` publishes only if the file still matches
                     the hash that passed validation

A failed gate retries once with the validator's errors, then fails closed. Every step appends to
runs/audit.jsonl. Standard library only; model calls go through the Claude Code CLI (`claude -p`).
"""
import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
AUDIT = RUNS / "audit.jsonl"
PUBLISHED = ROOT / "published"
HN_API = "https://hn.algolia.com/api/v1"
MODEL = "haiku"
MODEL_TIMEOUT_S = 180
MAX_COMMENTS = 6
COMMENT_CHARS = 800
MIN_SIGNALS = 3
MIN_QUOTE_CHARS = 40
MAX_BLOCK_WORDS = 70
MAX_HEADLINE_WORDS = 12
HYPE = ("game-changer", "game changer", "revolutionary", "unprecedented", "paradigm shift")
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
CITATION = re.compile(r"\bE\d+\b")

EXTRACT_PROMPT = """You are the Extractor agent for ctaio.dev, a newsletter for CTOs, CIOs and Chief AI Officers.
Below are Hacker News threads (title + top comments). Extract up to 2 signals per thread that a
technology leader should know about AI and agentic engineering in practice.

Rules:
- evidence_quote MUST be copied character-for-character from the thread text, 40-300 characters.
  No paraphrasing, no ellipses, no joining separate sentences.
- claim: one sentence stating only what the quote supports. No numbers that are not in the quote.
- Skip threads with nothing relevant. Never invent.
Return ONLY JSON:
{"signals":[{"source_id":"S1","claim":"...","evidence_quote":"...","why_it_matters":"..."}]}

THREADS:
"""

EDIT_PROMPT = """You are the Editor agent for ctaio.dev. Write a short brief for CTOs using ONLY the
verified evidence below.

Rules:
- 3 to 5 items. Each item: 1-3 sentences, at most 70 words, citing the evidence ids it relies on.
- Use no fact, name or number that is not in the cited evidence. No URLs. No hype.
- headline: at most 12 words. takeaway: one sentence of advice for a CTO, citing evidence ids.
Return ONLY JSON:
{"headline":"...","items":[{"text":"...","evidence_ids":["E1"]}],"takeaway":{"text":"...","evidence_ids":["E2"]}}

VERIFIED EVIDENCE:
"""


def sha(data):
    return hashlib.sha256(data).hexdigest()


def log(run_id, step, status, **detail):
    event = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run_id": run_id,
             "step": step, "status": status, **detail}
    RUNS.mkdir(exist_ok=True)
    with AUDIT.open("a") as f:
        f.write(json.dumps(event) + "\n")
    print(f"[{step}] {status} {json.dumps(detail)[:240]}")


def read_audit():
    if not AUDIT.exists():
        return []
    return [json.loads(line) for line in AUDIT.read_text().splitlines() if line.strip()]


def get_json(url):
    with urllib.request.urlopen(url, timeout=20) as resp:
        return json.load(resp)


def clean_html(text):
    text = re.sub(r"<p>", "\n", text or "")
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def fetch_sources(query, days, limit):
    since = int(time.time()) - days * 86400
    params = urllib.parse.urlencode({"query": query, "tags": "story", "hitsPerPage": limit,
                                     "numericFilters": f"created_at_i>{since},points>50"})
    sources = []
    for n, hit in enumerate(get_json(f"{HN_API}/search?{params}")["hits"], 1):
        item = get_json(f"{HN_API}/items/{hit['objectID']}")
        comments = [clean_html(c["text"])[:COMMENT_CHARS]
                    for c in item.get("children", []) if c.get("text")][:MAX_COMMENTS]
        sources.append({"id": f"S{n}", "title": hit["title"], "points": hit.get("points"),
                        "thread": f"https://news.ycombinator.com/item?id={hit['objectID']}",
                        "text": "\n\n".join([hit["title"]] + comments)})
    return sources


def normalise(text):
    text = html.unescape(text)
    for a, b in (("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"')):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip().lower()


def numbers(text):
    return {n.replace(",", "").rstrip(".") for n in NUMBER.findall(CITATION.sub("", text))}


def parse_json(raw):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start:end + 1])


def call_model(run_id, run_dir, step, prompt):
    started = time.time()
    try:
        proc = subprocess.run(["claude", "-p", prompt, "--model", MODEL], capture_output=True,
                              text=True, timeout=MODEL_TIMEOUT_S, cwd=tempfile.gettempdir())
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(run_id, step, "model_unavailable", error=type(e).__name__)
        return None
    (run_dir / f"{step}.raw.txt").write_text(proc.stdout)
    ok = proc.returncode == 0 and proc.stdout.strip()
    log(run_id, step, "model_returned" if ok else "model_error", model=MODEL,
        seconds=round(time.time() - started, 1), raw_sha256=sha(proc.stdout.encode())[:16])
    return proc.stdout if ok else None


def gate1(data, sources):
    """Keep only signals whose quote exists verbatim in the cited source."""
    by_id = {s["id"]: s for s in sources}
    valid, rejected, seen = [], [], set()
    signals = data.get("signals") if isinstance(data, dict) else None
    for index, sig in enumerate(signals if isinstance(signals, list) else []):
        reasons = [f"missing {k}" for k in ("source_id", "claim", "evidence_quote", "why_it_matters")
                   if not isinstance(sig, dict) or not str(sig.get(k) or "").strip()]
        if not reasons:
            source = by_id.get(sig["source_id"])
            quote = normalise(sig["evidence_quote"])
            if not source:
                reasons.append(f"unknown source_id {sig['source_id']}")
            elif len(quote) < MIN_QUOTE_CHARS:
                reasons.append(f"evidence_quote under {MIN_QUOTE_CHARS} chars")
            elif quote not in normalise(source["text"]):
                reasons.append("evidence_quote not found verbatim in source")
            else:
                extra = numbers(sig["claim"]) - numbers(sig["evidence_quote"] + " " + source["title"])
                if extra:
                    reasons.append(f"claim numbers not in quote: {sorted(extra)}")
            if quote in seen:
                reasons.append("duplicate evidence_quote")
        if reasons:
            rejected.append({"index": index, "signal": sig, "reasons": reasons})
        else:
            seen.add(quote)
            valid.append({**sig, "id": f"E{len(valid) + 1}"})
    return valid, rejected


def gate2(data, evidence):
    """Every block must cite verified evidence and add no numbers, URLs or hype."""
    if not isinstance(data, dict):
        return ["output is not a JSON object"]
    by_id = {e["id"]: e for e in evidence}
    errors = []
    headline = data.get("headline")
    if not isinstance(headline, str) or not headline.strip():
        errors.append("missing headline")
    else:
        if len(headline.split()) > MAX_HEADLINE_WORDS:
            errors.append(f"headline over {MAX_HEADLINE_WORDS} words")
        all_evidence = " ".join(e["claim"] + " " + e["evidence_quote"] for e in evidence)
        extra = numbers(headline) - numbers(all_evidence)
        if extra:
            errors.append(f"headline numbers not in evidence: {sorted(extra)}")
    items = data.get("items")
    if not isinstance(items, list) or not 3 <= len(items) <= 5:
        errors.append("need 3-5 items")
        items = items if isinstance(items, list) else []
    for n, block in enumerate(items + [data.get("takeaway")], 1):
        label = f"takeaway" if n == len(items) + 1 else f"item {n}"
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            errors.append(f"{label}: missing text")
            continue
        ids = block.get("evidence_ids") if isinstance(block.get("evidence_ids"), list) else []
        unknown = [i for i in ids if i not in by_id]
        if not ids or unknown:
            errors.append(f"{label}: no evidence_ids or unknown ids {unknown}")
            continue
        text = block["text"]
        if len(text.split()) > MAX_BLOCK_WORDS:
            errors.append(f"{label}: over {MAX_BLOCK_WORDS} words")
        if "http" in text.lower() or "www." in text.lower():
            errors.append(f"{label}: contains a URL")
        hype = [w for w in HYPE if w in text.lower()]
        if hype:
            errors.append(f"{label}: hype words {hype}")
        cited = " ".join(by_id[i]["claim"] + " " + by_id[i]["evidence_quote"] for i in ids)
        extra = numbers(text) - numbers(cited)
        if extra:
            errors.append(f"{label}: numbers not in cited evidence {sorted(extra)}")
    return errors


def extract(run_id, run_dir, sources, inject_fabrication):
    threads = "\n\n".join(f"=== {s['id']}\n{s['text']}" for s in sources)
    feedback = ""
    for attempt in (1, 2):
        raw = call_model(run_id, run_dir, f"agent1_extract_try{attempt}", EXTRACT_PROMPT + threads + feedback)
        if raw is None:
            continue
        try:
            data = parse_json(raw)
        except ValueError as e:
            log(run_id, "gate1", "malformed_output", attempt=attempt, error=str(e)[:160])
            feedback = f"\n\nYour previous output was not valid JSON ({e}). Return ONLY the JSON object."
            continue
        if inject_fabrication and attempt == 1 and isinstance(data, dict):
            data.setdefault("signals", []).append({
                "source_id": sources[0]["id"],
                "claim": "Teams cut agent infrastructure costs by 73% after switching models.",
                "evidence_quote": "We cut our agent infrastructure costs by 73% in one quarter after switching models.",
                "why_it_matters": "POSITIVE CONTROL: fabricated on purpose; Gate 1 must reject it."})
        valid, rejected = gate1(data, sources)
        (run_dir / f"gate1_try{attempt}.json").write_text(
            json.dumps({"valid": valid, "rejected": rejected}, indent=2))
        passed = len(valid) >= MIN_SIGNALS
        log(run_id, "gate1", "pass" if passed else "fail", attempt=attempt, valid=len(valid),
            rejected=len(rejected), reasons=[r["reasons"] for r in rejected])
        if passed:
            return valid
        feedback = ("\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED BY THE VALIDATOR:\n"
                    + "\n".join(f"- {str(r['signal'].get('evidence_quote', ''))[:90]!r}: {'; '.join(r['reasons'])}"
                                for r in rejected if isinstance(r["signal"], dict))
                    + f"\nReturn at least {MIN_SIGNALS} signals with quotes copied exactly from the thread text.")
    log(run_id, "pipeline", "failed_closed", stage="extract")
    return None


def edit(run_id, run_dir, evidence):
    block = "\n".join(f'{e["id"]}: {e["claim"]} | quote: "{e["evidence_quote"]}" | why: {e["why_it_matters"]}'
                      for e in evidence)
    feedback = ""
    for attempt in (1, 2):
        raw = call_model(run_id, run_dir, f"agent2_edit_try{attempt}", EDIT_PROMPT + block + feedback)
        if raw is None:
            continue
        try:
            data = parse_json(raw)
        except ValueError as e:
            log(run_id, "gate2", "malformed_output", attempt=attempt, error=str(e)[:160])
            feedback = f"\n\nYour previous output was not valid JSON ({e}). Return ONLY the JSON object."
            continue
        errors = gate2(data, evidence)
        log(run_id, "gate2", "pass" if not errors else "fail", attempt=attempt, errors=errors)
        if not errors:
            return data
        feedback = ("\n\nYOUR PREVIOUS DRAFT WAS REJECTED BY THE VALIDATOR:\n- " + "\n- ".join(errors)
                    + "\nFix these and return the full JSON again.")
    log(run_id, "pipeline", "failed_closed", stage="edit")
    return None


def render(run_id, brief, evidence, sources):
    by_source = {s["id"]: s for s in sources}
    lines = [f"# {brief['headline']}", "",
             f"*ctaio.dev signal brief (prototype) · run {run_id} · every line cites evidence that passed "
             f"a verbatim-quote check*", ""]
    lines += [f"- {b['text']} [{', '.join(b['evidence_ids'])}]" for b in brief["items"]]
    takeaway = brief["takeaway"]
    lines += ["", f"**For CTOs:** {takeaway['text']} [{', '.join(takeaway['evidence_ids'])}]", "",
              "## Evidence", ""]
    for e in evidence:
        source = by_source[e["source_id"]]
        lines.append(f"- **{e['id']}** \"{e['evidence_quote']}\" ({source['title']}, {source['thread']})")
    return "\n".join(lines) + "\n"


def run_pipeline(args):
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True)
    try:
        sources = (json.loads(Path(args.sources).read_text()) if args.sources
                   else fetch_sources(args.query, args.days, args.limit))
    except Exception as e:  # network, API or file errors stop the run here, logged
        log(run_id, "fetch", "failed", error=f"{type(e).__name__}: {str(e)[:160]}")
        return 1
    if not sources:
        log(run_id, "fetch", "failed", error="no sources returned")
        return 1
    (run_dir / "sources.json").write_text(json.dumps(sources, indent=2))
    log(run_id, "fetch", "ok", mode="replay" if args.sources else "live", query=args.query, sources=len(sources))
    evidence = extract(run_id, run_dir, sources, args.inject_fabrication)
    if evidence is None:
        return 1
    brief = edit(run_id, run_dir, evidence)
    if brief is None:
        return 1
    markdown = render(run_id, brief, evidence, sources)
    (run_dir / "brief.md").write_text(markdown)
    log(run_id, "render", "ok", brief_sha256=sha(markdown.encode()))
    log(run_id, "approval", "held", reason="default deny: nothing publishes without a human",
        next=f"python3 pipeline.py --approve {run_id}")
    print("\n" + markdown)
    return 0


def approve(run_id, approver):
    brief = RUNS / run_id / "brief.md"
    rendered = [e for e in read_audit()
                if e["run_id"] == run_id and e["step"] == "render" and e["status"] == "ok"]
    if not brief.exists() or not rendered:
        log(run_id, "approval", "rejected", reason="no validated brief for this run")
        return 1
    if sha(brief.read_bytes()) != rendered[-1]["brief_sha256"]:
        log(run_id, "approval", "rejected", reason="brief changed after validation; re-run the pipeline")
        return 1
    dest = PUBLISHED / f"{run_id}.md"
    if dest.exists():
        log(run_id, "publish", "already_published", path=f"published/{run_id}.md")
        return 0
    PUBLISHED.mkdir(exist_ok=True)
    shutil.copy(brief, dest)
    log(run_id, "approval", "approved", by=approver)
    log(run_id, "publish", "ok", path=f"published/{run_id}.md")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--query", default="AI agents")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--sources", help="replay a saved sources.json instead of fetching live")
    parser.add_argument("--inject-fabrication", action="store_true",
                        help="positive control: add a fabricated signal that Gate 1 must reject")
    parser.add_argument("--approve", metavar="RUN_ID", help="human approval: publish a held, validated run")
    parser.add_argument("--by", default="editor", help="approver name recorded in the audit log")
    args = parser.parse_args()
    return approve(args.approve, args.by) if args.approve else run_pipeline(args)


if __name__ == "__main__":
    raise SystemExit(main())
