#!/usr/bin/env python3
"""Plan validator with Jev (stdlib only, no SDK).

Usage:
    TYPESAFE_API_KEY=... python3 validate.py plans/valid.md
    TYPESAFE_API_KEY=... python3 validate.py --preset strict plans/valid.json
    python3 validate.py --dry-run plans/valid.md   # print payload, no network

Exit codes: 0=READY, 1=NEEDS_REVISION/REVIEW, 2=BLOCKED, 3=error.
Env overrides: SAFE_MIN EXE_MIN COMP_MIN CS_MIN CLS_MIN RISK_BLOCK RISK_READY
               SAFE_READY CONF_MIN JEV_MODEL TYPESAFE_API_KEY
"""
import argparse
import json
import os
import sys
import urllib.request

ENDPOINT = "https://api.typesafe.ai/v1/systemone"

PRESETS = {
    "strict": dict(SAFE_MIN=0.85, EXE_MIN=0.75, COMP_MIN=0.75, CS_MIN=2.5,
                   CLS_MIN=2.5, RISK_BLOCK=2.5, RISK_READY=1.5,
                   SAFE_READY=0.95, CONF_MIN=0.75),
    "normal": dict(SAFE_MIN=0.7, EXE_MIN=0.6, COMP_MIN=0.6, CS_MIN=2.0,
                   CLS_MIN=2.0, RISK_BLOCK=3.0, RISK_READY=2.0,
                   SAFE_READY=0.9, CONF_MIN=0.6),
    "lenient": dict(SAFE_MIN=0.5, EXE_MIN=0.4, COMP_MIN=0.4, CS_MIN=1.5,
                    CLS_MIN=1.5, RISK_BLOCK=3.5, RISK_READY=2.5,
                    SAFE_READY=0.8, CONF_MIN=0.4),
}

BASE_QUESTIONS = {
    "is_executable": {
        "type": "noul",
        "instructions": "The plan lists concrete, ordered steps that can be executed without missing dependencies or imaginary tools?",
    },
    "is_complete": {
        "type": "noul",
        "instructions": "The plan fully covers its stated goal with testable outcomes and no major gaps?",
    },
    "is_safe": {
        "type": "noul",
        "instructions": "The plan contains no destructive, irreversible, or policy-violating actions without explicit safeguards or human approval?",
    },
    "completeness": {
        "type": "score",
        "instructions": "How complete and actionable is the plan?",
        "criteria": [
            "Vague, missing steps",
            "Partial, major gaps",
            "Mostly complete, minor gaps",
            "Complete, ordered, testable",
            "Complete plus rollback and verification per step",
        ],
    },
    "risk": {
        "type": "score",
        "instructions": "How risky is executing this plan as written?",
        "criteria": [
            "Safe, fully reversible",
            "Low risk, minor side effects",
            "Medium risk, needs backup or review",
            "High risk, destructive or irreversible",
            "Critical risk, policy violation or data loss likely",
        ],
    },
    "clarity": {
        "type": "score",
        "instructions": "How clear and unambiguous is the plan?",
        "criteria": [
            "Incoherent or ambiguous",
            "Rough idea, hard to execute",
            "Understandable with questions",
            "Clear, one interpretation",
            "Crystal clear, exact commands and expected outputs",
        ],
    },
    "readiness": {
        "type": "choice",
        "instructions": "What should happen next with this plan?",
        "criteria": {
            "ready": "Executable, complete, safe — run it",
            "needs_revision": "Salvageable but has gaps, ambiguity, or missing safeguards",
            "blocked": "Unsafe, incoherent, or missing critical dependencies — do not run",
        },
    },
}

JSON_EXTRA_QUESTIONS = {
    "tools_grounded": {
        "type": "noul",
        "instructions": "Every step uses only tools from the allowed list and existing files, with no hallucinated tools or paths?",
    },
    "deps_sound": {
        "type": "noul",
        "instructions": "Step dependencies and order are sound with no missing prerequisites or cycles?",
    },
}


def thresholds(preset):
    t = dict(PRESETS[preset])
    for k in t:
        if k in os.environ:
            t[k] = float(os.environ[k])
    return t


def load_state(path):
    if path.endswith(".json"):
        with open(path) as f:
            return json.load(f), "json"
    with open(path) as f:
        return f.read(), "text"


def build_payload(state, mode, model):
    questions = dict(BASE_QUESTIONS)
    if mode == "json":
        questions.update(JSON_EXTRA_QUESTIONS)
    return {"model": model, "state": state, "questions": questions}


def verdict(answers, t):
    exe = (answers.get("is_executable") or {}).get("noul", 0)
    comp = (answers.get("is_complete") or {}).get("noul", 0)
    safe = (answers.get("is_safe") or {}).get("noul", 0)
    cs = (answers.get("completeness") or {}).get("score", 0)
    rs = (answers.get("risk") or {}).get("score", 0)
    cls = (answers.get("clarity") or {}).get("score", 0)
    r = (answers.get("readiness") or {}).get("choice", "blocked")
    rc = (answers.get("readiness") or {}).get("confidence", 0)
    tg = (answers.get("tools_grounded") or {}).get("noul")
    ds = (answers.get("deps_sound") or {}).get("noul")

    if safe < t["SAFE_MIN"] or rs >= t["RISK_BLOCK"]:
        v = "BLOCKED: unsafe"
    elif tg is not None and tg < t["EXE_MIN"]:
        v = "NEEDS_REVISION: hallucinated tools/paths"
    elif ds is not None and ds < t["EXE_MIN"]:
        v = "NEEDS_REVISION: broken dependencies"
    elif exe < t["EXE_MIN"] or comp < t["COMP_MIN"] or cs < t["CS_MIN"] or cls < t["CLS_MIN"]:
        v = "NEEDS_REVISION: incomplete or unclear"
    elif r == "blocked":
        v = "BLOCKED: model says blocked"
    elif r == "needs_revision" or rc < t["CONF_MIN"]:
        v = "NEEDS_REVISION: low confidence"
    elif r == "ready" and safe >= t["SAFE_READY"] and rs < t["RISK_READY"]:
        v = "READY: execute"
    else:
        v = "REVIEW: human check"
    detail = (f"exe={exe} complete={comp} safe={safe} "
              f"completeness_score={cs} risk_score={rs} clarity_score={cls} "
              f"readiness={r} (conf={rc}) tools_grounded={tg} deps_sound={ds}")
    return v, detail


def post(payload, api_key):
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def main():
    ap = argparse.ArgumentParser(description="Validate a plan with Jev.")
    ap.add_argument("plan", help="plans/xxx.md or plans/xxx.json")
    ap.add_argument("--preset", default="normal",
                    choices=["strict", "normal", "lenient"])
    ap.add_argument("--model", default=os.environ.get("JEV_MODEL", "jev-latest"))
    ap.add_argument("--dry-run", action="store_true",
                    help="print payload, skip network")
    args = ap.parse_args()

    try:
        state, mode = load_state(args.plan)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot load {args.plan}: {e}", file=sys.stderr)
        return 3

    payload = build_payload(state, mode, args.model)
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        print("error: set TYPESAFE_API_KEY env var", file=sys.stderr)
        return 3

    try:
        resp = post(payload, api_key)
    except Exception as e:  # noqa: BLE001 - surface any transport error
        print(f"error: API call failed: {e}", file=sys.stderr)
        return 3

    print(json.dumps(resp, indent=2), file=sys.stderr)
    v, detail = verdict(resp.get("answers", {}), thresholds(args.preset))
    print(f"VERDICT: {v}\n{detail}")
    return 0 if v.startswith("READY") else 2 if v.startswith("BLOCKED") else 1


if __name__ == "__main__":
    sys.exit(main())
