#!/usr/bin/env python3
"""Regression runner for the surfacing hook.

Drives the real hook as a subprocess against your real memory server and reranker —
no logic is duplicated here. Each case is a real message plus an expectation:

  silent  → the hook must print nothing
  inject  → the hook must print something; optional must_contain / must_contain_any /
            must_not_contain checks on the output.
  allow_pointer:true (either expectation) → a one-line pointer is tolerated, a full-text
            injection fails ("a clue is fine, content is not"); must_not_contain is still
            enforced on pointers.

Case discipline that keeps the set honest over time:
  1. Never hard-code memory ids in positive cases — memories decay and get archived.
  2. Use the user's real wording; a paraphrase can flip TRIGGER to LIGHT.
  3. Anchor keywords to the *context*, not to one specific answer — when the store grows a
     better memory, injecting that one should still pass.
  4. Operational questions ("where is the config") should stay silent; only episodic recall
     should surface.

Usage:
  python3 tests/test_memory_surface.py            # all cases
  python3 tests/test_memory_surface.py --only id  # one case
  python3 tests/test_memory_surface.py -v         # show hook output for passes too

Env: same variables as the hook (MEMORY_MCP_URL, RERANKER_API_KEY, ...). Point HOOK_PATH
at a different hook file if needed. Set CASES_PATH to your own cases file.
"""
import json, os, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.environ.get("HOOK_PATH", os.path.join(HERE, "..", "hook", "memory_surface.py"))
CASES_PATH = os.environ.get("CASES_PATH", os.path.join(HERE, "cases.example.json"))


def make_transcript(context):
    """context = [[role, text], ...] → a minimal Claude Code transcript JSONL."""
    tf = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    for role, text in (context or []):
        if role == "user":
            obj = {"type": "user", "message": {"role": "user", "content": text}}
        else:
            obj = {"type": "assistant", "message": {"role": "assistant",
                                                    "content": [{"type": "text", "text": text}]}}
        tf.write(json.dumps(obj, ensure_ascii=False) + "\n")
    tf.close()
    return tf.name


def run_hook(query, transcript):
    event = json.dumps({"prompt": query, "transcript_path": transcript})
    r = subprocess.run([sys.executable, HOOK], input=event, capture_output=True, text=True,
                       timeout=40, env={**os.environ})
    if r.stderr.strip():
        print(f"       ! stderr: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def judge(case, out):
    """expect=silent: nothing may be printed (allow_pointer:true tolerates a pointer line).
    expect=inject: something must be printed (allow_pointer:true also forbids full text).
    must_not_contain is enforced on whatever was printed, pointers included."""
    reasons = []
    expect = case["expect"]
    lines = [l for l in out.splitlines() if l.strip()]
    is_pointer_only = bool(lines) and all(l.startswith("⟦") for l in lines)
    allow_ptr = bool(case.get("allow_pointer"))
    if expect == "silent":
        if out and not (allow_ptr and is_pointer_only):
            reasons.append("expected silence but got a full-text injection" if allow_ptr else "expected silence but got an injection")
    elif expect == "inject":
        if not out:
            reasons.append("expected an injection but got silence")
        else:
            if allow_ptr and not is_pointer_only:
                reasons.append("allow_pointer case got a full-text injection")
            for kw in case.get("must_contain", []):
                if kw not in out:
                    reasons.append(f"missing must_contain: {kw}")
            anys = case.get("must_contain_any", [])
            if anys and not any(kw in out for kw in anys):
                reasons.append(f"none of must_contain_any present: {anys}")
    for kw in case.get("must_not_contain", []):
        if kw in out:
            reasons.append(f"forbidden text present: {kw}")
    return (not reasons), reasons


def main():
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else None
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    with open(CASES_PATH, encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    if only:
        cases = [c for c in cases if c["id"] == only]
        if not cases:
            print(f"no case with id={only}"); sys.exit(2)
    passed, failures = 0, []
    for case in cases:
        tp = make_transcript(case.get("context"))
        try:
            t0 = time.time()
            out = run_hook(case["query"], tp)
            ok, reasons = judge(case, out)
            passed += ok
            print(f"[{'PASS' if ok else 'FAIL'}] {case['id']:<26} {case.get('label', '')}  ({time.time() - t0:.1f}s)")
            if not ok:
                failures.append(case["id"])
                for r in reasons:
                    print(f"       ✗ {r}")
            if (not ok) or verbose:
                for ln in (out.splitlines() or ["(silent)"]):
                    print(f"       | {ln[:110]}")
        finally:
            os.unlink(tp)
    print("=" * 72)
    print(f"strict accuracy: {passed}/{len(cases)}" + (f"  failed: {', '.join(failures)}" if failures else ""))
    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
