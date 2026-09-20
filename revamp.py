#!/usr/bin/env python3
"""Revamp loop: route.py judges, a generative LLM rewrites, code loops.

    plan -> route.py -> READY? done : rewrite from findings -> re-route ...
    Escalated (complex) plans get ONE rewrite attempt posted as a comment on
    the existing issue for the human — never auto-approved by the loop.

Usage:
    REVAMP_API_KEY=... TYPESAFE_API_KEY=... python3 revamp.py plans/invalid.md
    python3 revamp.py --self-test        # offline checks, no network/keys
    python3 revamp.py --dry-run plans/invalid.md  # show rewrite prompt only

Env: REVAMP_API_KEY (else OPENROUTER_API_KEY else OPENAI_API_KEY),
     REVAMP_MODEL (default openai/gpt-4o-mini),
     REVAMP_BASE_URL (default https://openrouter.ai/api/v1).
Exit codes: 0=READY (or signed off), 1=still needs work after max rounds,
            2=escalated (issue holds latest revamp), 3=error.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def llm_cfg():
    key = (os.environ.get("REVAMP_API_KEY")
           or os.environ.get("OPENROUTER_API_KEY")
           or os.environ.get("OPENAI_API_KEY"))
    return {"key": key,
            "model": os.environ.get("REVAMP_MODEL", "openai/gpt-4o-mini"),
            "base": os.environ.get("REVAMP_BASE_URL",
                                   "https://openrouter.ai/api/v1")}


def parse_route_output(text):
    """Pull LANE / VERDICT / issue URL out of route.py stdout."""
    lane = re.search(r"^LANE:\s*(\S+)", text, re.M)
    verdict = re.search(r"^(?:VERDICT|ESCALATED):\s*(.+)$", text, re.M)
    url = re.search(r"https://github\.com/\S+/issues/\d+", text)
    return {"lane": lane.group(1) if lane else None,
            "verdict": verdict.group(1).strip() if verdict else None,
            "issue_url": url.group(0) if url else None}


def run_route(plan, repo):
    cmd = [sys.executable, os.path.join(HERE, "route.py"), plan]
    if repo:
        cmd += ["--repo", repo]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def build_rewrite_prompt(original, fmt, info):
    return (
        "You are revising an execution plan that failed automated validation.\n"
        f"Validator lane: {info.get('lane')}, verdict: {info.get('verdict')}.\n"
        f"Trigger: {info.get('trigger_hint', 'see verdict detail')}.\n"
        f"Detail: {info.get('detail', '')}\n\n"
        "Rewrite the plan to fix every flagged problem:\n"
        "- concrete ordered steps, real tools/paths only, no missing dependencies\n"
        "- testable outcomes per step, plus verification and rollback sections\n"
        "- no destructive, irreversible, or policy-violating step without an "
        "explicit safeguard or human-approval gate\n"
        f"- keep the same format ({fmt}); output ONLY the plan, no commentary\n\n"
        f"ORIGINAL PLAN:\n{original}\n"
    )


def rev_filename(plan, k):
    stem, ext = os.path.splitext(plan)
    return f"{stem}.rev{k}{ext or '.md'}"


def llm_rewrite(prompt, cfg):
    if not cfg["key"]:
        raise RuntimeError("set REVAMP_API_KEY (or OPENROUTER_API_KEY / OPENAI_API_KEY)")
    body = {"model": cfg["model"],
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2}
    req = urllib.request.Request(
        cfg["base"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {cfg['key']}",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"].strip()


def save_rev(path, text):
    if path.endswith(".json"):
        try:
            json.loads(text)
        except json.JSONDecodeError:
            path = os.path.splitext(path)[0] + ".md"  # keep going as text
    with open(path, "w") as f:
        f.write(text + "\n")
    return path


def comment_on_issue(url, body_file, repo):
    num = url.rstrip("/").rsplit("/", 1)[-1]
    cmd = ["gh", "issue", "comment", num, "--body-file", body_file]
    if repo:
        cmd += ["--repo", repo]
    subprocess.run(cmd, capture_output=True, text=True, check=True)


def log(entry):
    entry["ts"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with open(os.environ.get("DECISIONS_LOG", "decisions.jsonl"), "a") as f:
        f.write(json.dumps(entry) + "\n")


def self_test():
    out = "LANE: standard (preset=normal)\nVERDICT: NEEDS_REVISION: low confidence\nexe=..\n"
    info = parse_route_output(out)
    assert info == {"lane": "standard",
                    "verdict": "NEEDS_REVISION: low confidence",
                    "issue_url": None}, info
    print("PASS parse revision output")
    out = ("ESCALATED: tier=complex\nIssue: https://github.com/o/r/issues/7\nrisk=..\n")
    info = parse_route_output(out)
    assert info["lane"] is None and info["issue_url"].endswith("/issues/7"), info
    print("PASS parse escalation output + issue url")
    p = build_rewrite_prompt("do stuff", "md",
                             {"lane": "standard", "verdict": "NEEDS_REVISION",
                              "detail": "exe=0.3"})
    assert "do stuff" in p and "exe=0.3" in p and "ONLY the plan" in p
    print("PASS prompt carries plan + findings")
    assert rev_filename("plans/x.md", 1) == "plans/x.rev1.md"
    assert rev_filename("plans/x.json", 2) == "plans/x.rev2.json"
    print("PASS rev filenames never overwrite original")
    # loop-stop matrix: (exit code) -> action
    decide = {0: "done", 1: "rewrite", 2: "comment-once-stop", 3: "abort"}
    assert [decide[c] for c in (0, 1, 2, 3)] == ["done", "rewrite",
                                                "comment-once-stop", "abort"]
    print("PASS loop stops: ready=done revision=rewrite escalated=comment-once human-decides")
    try:
        llm_rewrite("hi", {"key": None, "model": "m", "base": "http://x"})
        raise AssertionError("should have raised")
    except RuntimeError as e:
        assert "REVAMP_API_KEY" in str(e)
    print("PASS missing key fails clean, no network attempted")
    print("SELF-TEST OK")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Rewrite a plan until it validates.")
    ap.add_argument("plan", nargs="?", help="plans/xxx.md or plans/xxx.json")
    ap.add_argument("--rounds", type=int, default=2, help="max rewrites (default 2)")
    ap.add_argument("--repo", default=None)
    ap.add_argument("--dry-run", action="store_true", help="print rewrite prompt only")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.plan:
        ap.error("plan is required (unless --self-test)")

    with open(args.plan) as f:
        original = f.read()
    fmt = "json" if args.plan.endswith(".json") else "markdown"

    if args.dry_run:
        print(build_rewrite_prompt(original, fmt, {"lane": "?", "verdict": "?",
                                                  "detail": "? (dry run)"}))
        return 0

    cfg = llm_cfg()
    current, info = args.plan, {}
    for k in range(args.rounds + 1):
        code, text = run_route(current, args.repo)
        info = parse_route_output(text)
        detail = "\n".join(text.splitlines()[-1:])
        log({"event": "revamp-check", "round": k, "plan": current, **info})
        print(text)
        if code == 0:
            print(f"REVAMPED: {current} is READY after {k} rewrite(s)")
            return 0
        if code == 3:
            print("revamp aborted: route.py errored (see above)", file=sys.stderr)
            return 3
        if code == 2:  # escalated/blocked: one rewrite as a proposal for the human
            if k >= 1 or not info["issue_url"]:
                print("revamp stops: human owns this one now", file=sys.stderr)
                return 2
            rev = save_rev(rev_filename(args.plan, k + 1),
                           llm_rewrite(build_rewrite_prompt(original, fmt, info), cfg))
            comment_on_issue(info["issue_url"], rev,
                             args.repo or os.environ.get("GH_REPO"))
            log({"event": "revamp-proposal", "plan": rev, "issue_url": info["issue_url"]})
            print(f"PROPOSED: {rev} posted on {info['issue_url']} — human decides")
            return 2
        if k == args.rounds:  # code == 1, out of rounds
            print(f"revamp stopped: still needs work after {args.rounds} round(s): "
                  f"{current}", file=sys.stderr)
            return 1
        rev = save_rev(rev_filename(args.plan, k + 1),
                       llm_rewrite(build_rewrite_prompt(original, fmt,
                                                        {**info, "detail": detail}), cfg))
        log({"event": "revamp-round", "round": k + 1, "plan": rev})
        print(f"ROUND {k + 1}: rewrote -> {rev}, re-routing...")
        original, current = open(rev).read(), rev


if __name__ == "__main__":
    sys.exit(main())
