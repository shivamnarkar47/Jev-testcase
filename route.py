#!/usr/bin/env python3
"""Tiered plan validation: Jev router picks the lane, validate.py judges.

    plan -> router call (tier/difficulty/risk/review) -> validator call
    with lane preset -> READY/REVISION, or ESCALATE with a GitHub issue
    upserted via gh CLI for a human.

Usage:
    TYPESAFE_API_KEY=... python3 route.py plans/trivial.md
    python3 route.py --dry-run plans/complex.md   # print router payload only
    python3 route.py --self-test                  # offline logic checks
    python3 route.py --signoff <sha12> plans/complex.md  # human approves reviewed plan

Exit codes: 0=READY-or-SIGNED-OFF, 1=NEEDS_REVISION/REVIEW, 2=ESCALATED-or-BLOCKED, 3=error.
Env: TYPESAFE_API_KEY, JEV_MODEL, GH_REPO (owner/name), ROUTER_CONF_MIN,
     RISK_ESCALATE, REVIEW_NOUL_MIN, DECISIONS_LOG.
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import validate

ROUTER_QUESTIONS = {
    "tier": {
        "type": "choice",
        "instructions": "Which validation lane does this plan belong in?",
        "criteria": {
            "trivial": "Single file, reversible, tested — e.g. typo fix, copy change",
            "standard": "Multi-file, no migrations, no prod data, has tests",
            "complex": "Migrations, auth, prod data, multi-service, or irreversible steps",
        },
    },
    "difficulty": {
        "type": "score",
        "instructions": "How hard is this plan to get right?",
        "criteria": [
            "Trivial, mechanical",
            "Easy, one judgment call",
            "Moderate, several interacting steps",
            "Hard, subtle failure modes",
            "Expert-only, novel or high-blast-radius work",
        ],
    },
    "risk": {
        "type": "score",
        "instructions": "What is the blast radius if this plan goes wrong?",
        "criteria": [
            "None, fully reversible",
            "Low, dev-only side effects",
            "Medium, staging or reviewable prod change",
            "High, prod data or user-facing with rollback",
            "Critical, irreversible or wide prod impact",
        ],
    },
    "needs_deep_review": {
        "type": "noul",
        "instructions": "Would a senior reviewer likely catch something a checklist misses in this plan?",
    },
}

PRESET_BY_TIER = {"trivial": "lenient", "standard": "normal", "complex": "strict"}
LABEL = "plan-review"


def router_cfg():
    return {
        "conf_min": float(os.environ.get("ROUTER_CONF_MIN", "0.6")),
        "risk_esc": float(os.environ.get("RISK_ESCALATE", "3.0")),
        "review_min": float(os.environ.get("REVIEW_NOUL_MIN", "0.7")),
    }


def decide_lane(answers, cfg):
    """Return (tier, preset, escalate, trigger). Missing/unreadable router output escalates."""
    t = (answers.get("tier") or {})
    tier = t.get("choice")
    conf = t.get("confidence", 0)
    risk = (answers.get("risk") or {}).get("score", 0)
    review = (answers.get("needs_deep_review") or {}).get("noul", 0)
    diff = (answers.get("difficulty") or {}).get("score", 0)
    if tier not in PRESET_BY_TIER:
        return "complex", "strict", True, "router output unreadable", diff, risk, review, conf
    if tier == "complex":
        trigger = "tier=complex"
    elif risk >= cfg["risk_esc"]:
        trigger = f"risk veto (risk={risk} >= {cfg['risk_esc']})"
    elif conf < cfg["conf_min"]:
        trigger = f"low router confidence ({conf} < {cfg['conf_min']})"
    elif review >= cfg["review_min"]:
        trigger = f"needs_deep_review={review} >= {cfg['review_min']}"
    else:
        return tier, PRESET_BY_TIER[tier], False, None, diff, risk, review, conf
    return tier, "strict", True, trigger, diff, risk, review, conf


def file_sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def build_packet(plan, sha, tier, diff, risk, review, conf, trigger, preset, verdict, detail, state):
    body = (f"# Plan review: `{plan}`\n\n"
            f"- sha: `{sha}` (`{sha[:12]}`)\n"
            f"- tier: `{tier}` (router conf {conf}), difficulty {diff}, risk {risk}, needs_deep_review {review}\n"
            f"- trigger: **{trigger}**\n"
            f"- validator preset: `{preset}` -> **{verdict}**\n"
            f"- detail: `{detail}`\n\n"
            f"## Plan\n\n```\n{state if isinstance(state, str) else json.dumps(state, indent=2)}\n```\n")
    if len(body) > 60000:  # gh body cap is 65536; keep head + note
        body = body[:60000] + "\n...[truncated, see repo file]...\n"
    return body


def issue_title(plan, sha, tier, risk):
    return f"[plan-review] {os.path.basename(plan)} {sha[:12]} tier={tier} risk={risk}"


def gh_repo(args):
    if args.repo:
        return args.repo
    if os.environ.get("GH_REPO"):
        return os.environ["GH_REPO"]
    try:
        out = subprocess.run(["git", "config", "--get", "remote.origin.url"],
                             capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    if out.startswith("git@github.com:"):
        return out.split(":", 1)[1].removesuffix(".git")
    if "github.com/" in out:
        return out.split("github.com/", 1)[1].removesuffix(".git")
    return None


def gh_run(cmd):
    """Run gh, returning stdout. Raises CalledProcessError on failure."""
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


CACHE_FILE = os.environ.get("ROUTE_CACHE", ".route-cache.json")


def load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache):
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, CACHE_FILE)


def issue_url(repo, num):
    cmd = ["gh", "issue", "view", str(num), "--json", "url,state", "--jq",
           'select(.state == "OPEN") | .url']
    if repo:
        cmd += ["--repo", repo]
    return gh_run(cmd).strip() or None


def comment_issue(num, body_file, repo):
    cmd = ["gh", "issue", "comment", str(num), "--body-file", body_file]
    if repo:
        cmd += ["--repo", repo]
    gh_run(cmd)
    return issue_url(repo, num)


def ensure_label(repo):
    cmd = ["gh", "label", "create", LABEL,
           "--description", "Escalated plan validations needing human review",
           "--color", "B60205"]
    if repo:
        cmd += ["--repo", repo]
    subprocess.run(cmd, capture_output=True, text=True)  # exists already -> fine


def upsert_issue(title, body, short, repo):
    """Comment on the open issue for this plan hash, or create it. Returns URL or None."""
    if shutil.which("gh") is None:
        print("warn: gh CLI not found, skipping issue upsert", file=sys.stderr)
        return None
    tmp = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                      dir="/tmp/opencode")
    try:
        tmp.write(body)
        tmp.close()
        ensure_label(repo)
        cache = load_cache()
        # 1. Local cache: lag-proof for same-machine re-runs. Closed/missing
        #    entries are dropped so a re-review opens a fresh issue.
        if short in cache:
            url = issue_url(repo, cache[short])
            if url:
                comment_issue(cache[short], tmp.name, repo)
                return url
            del cache[short]
        # 2. Remote list + client-side match (--search lags; list is fresher).
        #    Retried: GitHub read-after-write can lag a few seconds.
        found = []
        for _ in range(3):
            cmd = ["gh", "issue", "list", "--label", LABEL, "--state", "open",
                   "--json", "number,title", "--limit", "100"]
            if repo:
                cmd += ["--repo", repo]
            found = [i for i in json.loads(gh_run(cmd) or "[]")
                     if short in (i.get("title") or "")]
            if found:
                break
            time.sleep(3)
        if found:
            num = found[0]["number"]
            url = comment_issue(num, tmp.name, repo)
            if url:
                cache[short] = num
                save_cache(cache)
                return url
        cmd = ["gh", "issue", "create", "--title", title, "--label", LABEL,
               "--body-file", tmp.name]
        if repo:
            cmd += ["--repo", repo]
        url = gh_run(cmd).strip()
        try:
            num = int(url.rstrip("/").rsplit("/", 1)[-1])
        except ValueError:
            num = None
        if num:
            cache[short] = num
            save_cache(cache)
        return url or None
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        err = getattr(e, "stderr", "") or ""
        print(f"warn: issue upsert failed ({e}); packet printed instead\n{err}",
              file=sys.stderr)
        return None
    finally:
        os.unlink(tmp.name)


def log_decision(entry):
    path = os.environ.get("DECISIONS_LOG", "decisions.jsonl")
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def self_test():
    cfg = {"conf_min": 0.6, "risk_esc": 3.0, "review_min": 0.7}
    base = {"tier": {"choice": "trivial", "confidence": 0.9},
            "difficulty": {"score": 0.5}, "risk": {"score": 1.0},
            "needs_deep_review": {"noul": 0.1}}
    cases = [
        ("trivial fast lane", base, ("trivial", "lenient", False)),
        ("risk veto", {**base, "risk": {"score": 3.5}}, ("trivial", "strict", True)),
        ("low conf up", {**base, "tier": {"choice": "standard", "confidence": 0.4}},
         ("standard", "strict", True)),
        ("deep review flag", {**base, "needs_deep_review": {"noul": 0.9}},
         ("trivial", "strict", True)),
        ("complex", {**base, "tier": {"choice": "complex", "confidence": 0.9}},
         ("complex", "strict", True)),
        ("unreadable escalates", {}, ("complex", "strict", True)),
    ]
    for name, ans, (tier, preset, esc) in cases:
        got = decide_lane(ans, cfg)[:3]
        assert got == (tier, preset, esc), f"{name}: got {got}"
        print(f"PASS {name} -> tier={tier} preset={preset} escalate={esc}")
    sha = hashlib.sha256(b"x").hexdigest()
    title = issue_title("plans/complex.md", sha, "complex", 3.5)
    assert sha[:12] in title and LABEL in title, title
    print(f"PASS upsert title pins content hash -> {title}")
    pkt = build_packet("p.md", sha, "complex", 3, 3.5, 0.9, 0.8, "tier=complex",
                       "strict", "BLOCKED: unsafe", "d", "body")
    assert sha in pkt and "tier=complex" in pkt
    print("PASS packet carries sha + trigger")
    print("SELF-TEST OK")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Route a plan to its validation lane.")
    ap.add_argument("plan", nargs="?", help="plans/xxx.md or plans/xxx.json")
    ap.add_argument("--model", default=os.environ.get("JEV_MODEL", "jev-latest"))
    ap.add_argument("--repo", default=None, help="owner/name, default from git remote or GH_REPO")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--signoff", default=None, metavar="SHA12",
                    help="human approval for the reviewed plan hash")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.plan:
        ap.error("plan is required (unless --self-test)")

    try:
        state, mode = validate.load_state(args.plan)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot load {args.plan}: {e}", file=sys.stderr)
        return 3
    sha = file_sha(args.plan)

    if args.signoff:
        if not (sha.startswith(args.signoff) or sha == args.signoff):
            print(f"error: plan hash {sha[:12]} != signoff {args.signoff} "
                  f"(plan changed since review)", file=sys.stderr)
            return 3
        log_decision({"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      "plan": args.plan, "sha": sha[:12], "event": "human-signoff"})
        print(f"SIGNED-OFF: {args.plan} {sha[:12]} by human")
        return 0

    router_payload = {"model": args.model, "state": state, "questions": ROUTER_QUESTIONS}
    if args.dry_run:
        print(json.dumps(router_payload, indent=2))
        return 0

    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        print("error: set TYPESAFE_API_KEY env var", file=sys.stderr)
        return 3
    try:
        r_resp = validate.post(router_payload, api_key)
    except Exception as e:  # noqa: BLE001
        print(f"error: router call failed: {e}", file=sys.stderr)
        return 3
    print(json.dumps(r_resp, indent=2), file=sys.stderr)

    cfg = router_cfg()
    tier, preset, escalate, trigger, diff, risk, review, conf = decide_lane(
        r_resp.get("answers", {}), cfg)

    try:
        v_resp = validate.post(validate.build_payload(state, mode, args.model), api_key)
    except Exception as e:  # noqa: BLE001
        print(f"error: validator call failed: {e}", file=sys.stderr)
        return 3
    print(json.dumps(v_resp, indent=2), file=sys.stderr)
    v, detail = validate.verdict(v_resp.get("answers", {}), validate.thresholds(preset))

    entry = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
             "plan": args.plan, "sha": sha[:12], "tier": tier, "difficulty": diff,
             "risk": risk, "needs_deep_review": review, "router_conf": conf,
             "preset": preset, "verdict": v, "trigger": trigger, "issue_url": None}

    if not escalate:
        log_decision(entry)
        print(f"LANE: {tier} (preset={preset})\nVERDICT: {v}\n{detail}")
        return 0 if v.startswith("READY") else 2 if v.startswith("BLOCKED") else 1

    packet = build_packet(args.plan, sha, tier, diff, risk, review, conf,
                          trigger, preset, v, detail, state)
    url = upsert_issue(issue_title(args.plan, sha, tier, risk), packet, sha[:12],
                       gh_repo(args))
    entry["issue_url"] = url
    log_decision(entry)
    if url:
        print(f"ESCALATED: {trigger}\nIssue: {url}\n{detail}")
    else:
        print(f"ESCALATED: {trigger} (gh unavailable, packet below)\n{packet}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
