#!/usr/bin/env python3
"""Write the one-line note for each name screen.py flagged, then merge the notes back.

Usage:  python notes.py --out docs/data
Reads  OUT/needs_notes.json (written by `screen.py run`), asks Claude for one line per name,
writes OUT/new_notes.json ({symbol: note}) and runs `screen.py merge-notes`.
Needs ANTHROPIC_API_KEY in the environment; with no key or no names it exits 0 without writing.
"""
import argparse, json, os, re, subprocess, sys

MODEL = "claude-opus-5-5"
SYSTEM = (
    "You write one-line notes for a capital-markets desk's screen of abnormal share-price moves. "
    "For each name you are given the move, the tier that flagged it, the catalyst calendar, a business summary "
    "and recent headlines. Write ONE sentence (max 30 words) per name saying the most likely reason for the move, "
    "citing the headline that supports it. If the headlines do not explain the move, start with 'No clear news;' "
    "and say what the headlines do cover. Plain prose, no markdown, no ticker prefix. "
    "Reply with a single JSON object mapping each symbol to its note and nothing else."
)


def ask(client, batch):
    payload = [{k: r.get(k) for k in ("symbol", "name", "market", "day_pct", "wk_pct", "z", "tier", "catalyst", "summary", "headlines")} for r in batch]
    kwargs = dict(
        model=MODEL,
        max_tokens=4000,
        output_config={"effort": "low"},
        system=SYSTEM,
        messages=[{"role": "user", "content": "Names:\n" + json.dumps(payload, ensure_ascii=False)}],
    )
    try:
        # Server-side refusal fallback: re-runs on another model if a safety classifier declines the request.
        resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    except TypeError:
        resp = client.messages.create(**kwargs)
    if resp.stop_reason == "refusal":
        print("refused:", getattr(resp, "stop_details", None), file=sys.stderr)
        return {}
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        print("no JSON in reply:", text[:200], file=sys.stderr)
        return {}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        print("bad JSON:", e, file=sys.stderr)
        return {}
    return {k: str(v).strip() for k, v in obj.items() if isinstance(v, str) and v.strip()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/data")
    ap.add_argument("--batch", type=int, default=25)
    a = ap.parse_args()
    need_path = os.path.join(a.out, "needs_notes.json")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set; notes skipped")
        return 0
    if not os.path.exists(need_path):
        print("no needs_notes.json; nothing to do")
        return 0
    need = json.load(open(need_path, encoding="utf-8"))
    if not need:
        print("no names need a note")
        return 0
    import anthropic
    client = anthropic.Anthropic()
    notes = {}
    for i in range(0, len(need), a.batch):
        batch = need[i:i + a.batch]
        try:
            notes.update(ask(client, batch))
        except anthropic.RateLimitError as e:
            print("rate limited; stopping this run:", e, file=sys.stderr)
            break
        except anthropic.APIStatusError as e:
            print("API error %s: %s" % (e.status_code, e.message), file=sys.stderr)
            if e.status_code < 500:
                break
        except anthropic.APIConnectionError as e:
            print("network error:", e, file=sys.stderr)
            break
    print("notes written for %d of %d names" % (len(notes), len(need)))
    if not notes:
        return 0
    new_path = os.path.join(a.out, "new_notes.json")
    json.dump(notes, open(new_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    here = os.path.dirname(os.path.abspath(__file__))
    return subprocess.call([sys.executable, os.path.join(here, "screen.py"), "merge-notes", "--out", a.out, "--new", new_path])


if __name__ == "__main__":
    sys.exit(main())
