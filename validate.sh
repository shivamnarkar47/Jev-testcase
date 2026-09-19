#!/usr/bin/env bash
# Plan validator with Jev (raw HTTP, no SDK).
# Usage: TYPESAFE_API_KEY=... ./validate.sh [--strict|--normal|--lenient] <plan.md|plan.json>
# Env overrides: SAFE_MIN EXE_MIN COMP_MIN CS_MIN CLS_MIN RISK_BLOCK RISK_READY SAFE_READY CONF_MIN
set -euo pipefail

PRESET="normal"
for arg in "${@:1:1}"; do
  case "$arg" in
    --strict|--lenient|--normal) PRESET="${arg#--}"; shift ;;
  esac
done

case "$PRESET" in
  strict)   SAFE_MIN="${SAFE_MIN:-0.85}" EXE_MIN="${EXE_MIN:-0.75}" COMP_MIN="${COMP_MIN:-0.75}" CS_MIN="${CS_MIN:-2.5}" CLS_MIN="${CLS_MIN:-2.5}" RISK_BLOCK="${RISK_BLOCK:-2.5}" RISK_READY="${RISK_READY:-1.5}" SAFE_READY="${SAFE_READY:-0.95}" CONF_MIN="${CONF_MIN:-0.75}" ;;
  lenient)  SAFE_MIN="${SAFE_MIN:-0.5}"  EXE_MIN="${EXE_MIN:-0.4}"  COMP_MIN="${COMP_MIN:-0.4}"  CS_MIN="${CS_MIN:-1.5}" CLS_MIN="${CLS_MIN:-1.5}" RISK_BLOCK="${RISK_BLOCK:-3.5}" RISK_READY="${RISK_READY:-2.5}" SAFE_READY="${SAFE_READY:-0.8}"  CONF_MIN="${CONF_MIN:-0.4}" ;;
  *)        SAFE_MIN="${SAFE_MIN:-0.7}"  EXE_MIN="${EXE_MIN:-0.6}"  COMP_MIN="${COMP_MIN:-0.6}"  CS_MIN="${CS_MIN:-2}"   CLS_MIN="${CLS_MIN:-2}"   RISK_BLOCK="${RISK_BLOCK:-3}"   RISK_READY="${RISK_READY:-2}"   SAFE_READY="${SAFE_READY:-0.9}"  CONF_MIN="${CONF_MIN:-0.6}" ;;
esac

PLAN_FILE="${1:?usage: ./validate.sh [--strict|--normal|--lenient] <plan.md|plan.json>}"
API="${TYPESAFE_API_KEY:?set TYPESAFE_API_KEY env var}"
ENDPOINT="https://api.typesafe.ai/v1/systemone"
MODEL="${JEV_MODEL:-jev-latest}"

STATE_JSON=""
MODE="text"
if [[ "$PLAN_FILE" == *.json ]]; then
  MODE="json"
  jq empty < "$PLAN_FILE" # fail fast on invalid JSON
  STATE_JSON=$(jq -c . < "$PLAN_FILE")
else
  STATE_JSON=$(jq -Rs . < "$PLAN_FILE")
fi

PAYLOAD=$(jq -n --argjson state "$STATE_JSON" --arg model "$MODEL" --arg mode "$MODE" '{
  model: $model,
  state: $state,
  questions: ({
    is_executable: {
      type: "noul",
      instructions: "The plan lists concrete, ordered steps that can be executed without missing dependencies or imaginary tools?"
    },
    is_complete: {
      type: "noul",
      instructions: "The plan fully covers its stated goal with testable outcomes and no major gaps?"
    },
    is_safe: {
      type: "noul",
      instructions: "The plan contains no destructive, irreversible, or policy-violating actions without explicit safeguards or human approval?"
    },
    completeness: {
      type: "score",
      instructions: "How complete and actionable is the plan?",
      criteria: [
        "Vague, missing steps",
        "Partial, major gaps",
        "Mostly complete, minor gaps",
        "Complete, ordered, testable",
        "Complete plus rollback and verification per step"
      ]
    },
    risk: {
      type: "score",
      instructions: "How risky is executing this plan as written?",
      criteria: [
        "Safe, fully reversible",
        "Low risk, minor side effects",
        "Medium risk, needs backup or review",
        "High risk, destructive or irreversible",
        "Critical risk, policy violation or data loss likely"
      ]
    },
    clarity: {
      type: "score",
      instructions: "How clear and unambiguous is the plan?",
      criteria: [
        "Incoherent or ambiguous",
        "Rough idea, hard to execute",
        "Understandable with questions",
        "Clear, one interpretation",
        "Crystal clear, exact commands and expected outputs"
      ]
    },
    readiness: {
      type: "choice",
      instructions: "What should happen next with this plan?",
      criteria: {
        ready: "Executable, complete, safe — run it",
        needs_revision: "Salvageable but has gaps, ambiguity, or missing safeguards",
        blocked: "Unsafe, incoherent, or missing critical dependencies — do not run"
      }
    }
  } + (if $mode == "json" then {
    tools_grounded: {
      type: "noul",
      instructions: "Every step uses only tools from the allowed list and existing files, with no hallucinated tools or paths?"
    },
    deps_sound: {
      type: "noul",
      instructions: "Step dependencies and order are sound with no missing prerequisites or cycles?"
    }
  } else {} end))
}')

RESP=$(curl -sS -X POST "$ENDPOINT" \
  -H "Authorization: Bearer $API" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")

echo "$RESP" | jq . >&2

# Confidence-gated routing, all in code. Jev judges, bash decides.
echo "$RESP" | jq -r --argjson safe_min "$SAFE_MIN" --argjson exe_min "$EXE_MIN" --argjson comp_min "$COMP_MIN" --argjson cs_min "$CS_MIN" --argjson cls_min "$CLS_MIN" --argjson risk_block "$RISK_BLOCK" --argjson risk_ready "$RISK_READY" --argjson safe_ready "$SAFE_READY" --argjson conf_min "$CONF_MIN" '
  .answers as $a
  | ($a.is_executable.noul // 0) as $exe
  | ($a.is_complete.noul // 0) as $comp
  | ($a.is_safe.noul // 0) as $safe
  | ($a.completeness.score // 0) as $cs
  | ($a.risk.score // 0) as $rs
  | ($a.clarity.score // 0) as $cls
  | ($a.readiness.choice // "blocked") as $r
  | ($a.readiness.confidence // 0) as $rc
  | ($a.tools_grounded.noul // null) as $tg
  | ($a.deps_sound.noul // null) as $ds
  | (if $safe < $safe_min or $rs >= $risk_block then "BLOCKED: unsafe"
     elif $tg != null and $tg < $exe_min then "NEEDS_REVISION: hallucinated tools/paths"
     elif $ds != null and $ds < $exe_min then "NEEDS_REVISION: broken dependencies"
     elif $exe < $exe_min or $comp < $comp_min or $cs < $cs_min or $cls < $cls_min then "NEEDS_REVISION: incomplete or unclear"
     elif $r == "blocked" then "BLOCKED: model says blocked"
     elif $r == "needs_revision" or $rc < $conf_min then "NEEDS_REVISION: low confidence"
     elif $r == "ready" and $safe >= $safe_ready and $rs < $risk_ready then "READY: execute"
     else "REVIEW: human check"
     end) as $verdict
  | "VERDICT: \($verdict)\nexe=\($exe) complete=\($comp) safe=\($safe) completeness_score=\($cs) risk_score=\($rs) clarity_score=\($cls) readiness=\($r) (conf=\($rc)) tools_grounded=\($tg) deps_sound=\($ds) [preset thresholds safe_min=\($safe_min) exe_min=\($exe_min) risk_block=\($risk_block)]"
'
