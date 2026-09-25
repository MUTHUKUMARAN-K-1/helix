---
name: helix
description: Hand long-running jobs to the Helix orchestration engine and collect verified results. Use when a task will outlast this session, needs several independent workstreams run in parallel, needs human approval gates, or needs a recorded token/cost trail.
---

# Helix

Helix is a standalone orchestration engine running on this machine (or reachable over HTTP). It plans a typed DAG for a goal, runs independent nodes in parallel, verifies every output, holds at approval gates, and records tokens and cost. You stay the interface; Helix does the long work.

## When to hand a job to Helix

Hand off when ANY of these hold:
- The job will plausibly outlast this session (hours of builds, sweeps, multi-part research).
- The job splits into independent workstreams that can run in parallel.
- The user wants approval gates, a cost ceiling, or a replayable event log.
- The user explicitly asks for Helix.

Do NOT hand off single-file edits, quick questions, or anything faster done inline.

## How to hand off (CLI)

```bash
# 1. submit and run to completion (mock provider works offline; set HELIX_PROVIDER for real models)
helix run "<clear goal statement>" --provider "$HELIX_PROVIDER" --auto-approve

# 2. or submit without auto-approve, then drive the gates yourself
helix jobs                          # find the job id
helix status <job_id> --full        # plan, state, result
helix events <job_id>               # node-by-node log
helix approve <job_id> <node_id>    # pass a human gate (add --reject to stop it)
helix export <job_id> out.json      # full trajectory for the user
```

If `helix` is not on PATH, install once with `pip install -e .` from the Helix checkout, or use the HTTP API below.

## How to hand off (HTTP API)

```bash
curl -X POST localhost:8741/api/jobs -H 'Content-Type: application/json' \
  -d '{"goal": "<goal>", "provider": "mock", "budget": 60000}'
curl localhost:8741/api/jobs/<job_id>
curl -X POST localhost:8741/api/jobs/<job_id>/approve -H 'Content-Type: application/json' \
  -d '{"node_id": "<node>", "approved": true}'
```

## Reuse and automation

```bash
helix playbook save <name> --goal "<goal>"   # plan once, keep the DAG
helix playbook run <name>                     # run it again with no re-planning
helix templates                               # prebuilt plans to adopt
helix schedule add <name> --cron "30 8 * * 1-5" --playbook <name>
helix schedule daemon                         # fires due schedules
helix webhook add <name> --playbook <name>    # tokenized POST trigger
helix memory --recall "<topic>"               # what Helix learned on past runs
```

## Rules

1. Write the goal as a complete brief: context, constraints, deliverable. Node prompts are built from it.
2. Never auto-approve a gate the user placed unless the user told you to.
3. Report the job id, status, token count and cost back to the user, not just the result text.
4. If a job fails, read `helix events <job_id>` before retrying - the log says which node and why.
