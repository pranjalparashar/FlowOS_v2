---
title: FlowOS
emoji: 🛠️
colorFrom: blue
colorTo: indigo
sdk: docker
pinned: false
app_port: 7860
base_path: /web
tags:
  - developer-tools
  - pipeline-ops
  - llm-evaluation
  - workflow-generation
---

# FlowOS

FlowOS is an end-to-end benchmark for AI agents that operate like real production teams. Instead of testing a narrow coding trick, it evaluates whether one agent can handle the full operating loop around modern software systems: diagnose failures, review risky AI-generated changes, and ship new production artifacts safely.

The benchmark runs inside a realistic repository with transforms, schemas, validators, lineage, deployment configs, policies, draft patches, and multi-file delivery tasks. That makes it useful for evaluating much more than “can the model write code?” It measures whether an agent can actually move work across the line.

FlowOS is designed around the work that burns the most money in real teams:

- failed pipelines, broken loads, and bad deploy drift that trigger retries, backfills, and missed refresh windows
- risky AI-generated patches that increase reviewer burden or create downstream incidents
- repetitive implementation work that bounces between developers, operators, analysts, and business owners before anything ships

One benchmark covers repair, review, and implementation in the same environment. Instead of training or evaluating separate agents for incident response, patch review, and artifact generation, FlowOS measures whether a single agent can operate across the entire workflow.

## Why FlowOS

Most agent benchmarks stop at chat help, log triage, or a single-file patch. FlowOS models the broader operating loop that real teams care about:

- inspect the repo and supporting docs
- understand contracts, dependencies, and operational constraints
- decide whether an AI-generated fix is safe and correct
- create or edit multiple artifacts across code, config, checks, and delivery metadata
- validate before shipping

That makes FlowOS useful well beyond a single developer persona. It is relevant anywhere business logic and production systems meet:

- engineers repairing and shipping production changes
- operators and platform teams responding to failures
- analysts and data stakeholders who know the business logic but are not full-time software developers
- domain experts who can describe the desired outcome in natural language and rely on the agent to assemble the right technical artifacts

In other words, the benchmark measures whether an agent can turn intent into safe operational progress, not just whether it can autocomplete code.

## Why this saves money

FlowOS is intentionally shaped around the highest-cost handoffs in production work:

- catching bad fixes before deployment reduces failed runs, emergency rollbacks, and compute-heavy recovery work
- repairing end-to-end loads and orchestration issues reduces backfill spend, on-call churn, and missed business reporting windows
- rejecting unsafe or incorrect AI patches reduces reviewer load and downstream incident cost
- generating production artifacts in the same environment reduces duplicated tooling across backend, platform, analytics, release, and operational workflows

The deeper value is leverage. When one agent can repair, review, and implement inside the same system, teams spend less money on repeated handoffs between people who understand the business problem and people who know how to encode it technically.

## Benchmark design

The benchmark is organized around **6 public task entries** and **3 internal objective families**:

- public tasks are what evaluators select and score
- scenarios are deterministic variants inside those tasks
- internal grader families keep the implementation compact:
  - `repair`
  - `review`
  - `workflow`

Difficulty is defined at the **task level**:

- Tasks `1-2` are easy
- Tasks `3-4` are medium
- Tasks `5-6` are hard

Variation is defined at the **scenario level**:

- each task contains one or more deterministic scenarios
- same `task_id + scenario_index + seed` always yields the same scenario
- selected scenarios also support light seeded literal variation in a few cases
- same action trajectory always yields the same rewards and score
- no randomness is used to create score spread

## Tasks

| Task ID | Difficulty | Objective | Submission |
|---|---|---|---|
| `repair_data_transform` | Easy | Repair broken transform logic or dependency wiring while preserving downstream contracts | `submit_repair` |
| `repair_pipeline_execution` | Easy | Repair ingestion and orchestration failures caused by path, runner, or deploy drift | `submit_repair` |
| `review_ai_patch_safety` | Medium | Review AI-generated changes for privacy, governance, and operational safety risk | `submit_review` |
| `review_ai_patch_correctness` | Medium | Review AI-generated changes for semantic correctness and contract compatibility | `submit_review` |
| `synthesize_reporting_asset` | Hard | Create reporting-facing views, contracts, and alert/config assets | `submit_workspace` |
| `synthesize_data_product` | Hard | Create production-grade marts and feature workflows with validation and governance | `submit_workspace` |

### Task coverage

- `repair_data_transform`: schema drift and stale downstream dependency repairs
- `repair_pipeline_execution`: ADLS path migration, Jenkins runner repair, deploy config drift, CSV/type mismatch loads, column-count mismatches, and primary-key-safe merge repair
- `review_ai_patch_safety`: PII leakage and unsafe operational advice
- `review_ai_patch_correctness`: incorrect joins and contract-breaking schema changes
- `synthesize_reporting_asset`: reporting view delivery and reporting view + alert config delivery
- `synthesize_data_product`: feature workflow and curated mart delivery

### Current scenario set

- `repair_data_transform`
  - `PR-001`: schema drift in a warehouse transform
  - `PR-004`: stale downstream dependency on a deprecated upstream alias
- `repair_pipeline_execution`
  - `PR-002`: ADLS path and partition migration
  - `PR-003`: Jenkins runner-path breakage
  - `PR-005`: deploy config env-var drift
  - `PR-006`: CSV identifier type mismatch during warehouse load
  - `PR-007`: 6-column extract into a 12-column archive target
  - `PR-008`: primary-key violation from duplicate dimension rows
- `review_ai_patch_safety`
  - `LR-001`: PII leakage in an analytics patch
  - `LR-004`: unsafe warehouse recovery recommendation
- `review_ai_patch_correctness`
  - `LR-002`: technically incorrect enrichment join
  - `LR-003`: published-contract-breaking rename
- `synthesize_reporting_asset`
  - `WS-002`: reporting view + schema contract
  - `WS-004`: reporting view + alert config
- `synthesize_data_product`
  - `WS-001`: feature workflow for downstream model scoring
  - `WS-003`: curated finance mart / data product

## Action space

```json
{"action_type":"search_workspace","parameters":{"query":"orders_daily schema drift"}}
{"action_type":"read_file","parameters":{"path":"transforms/orders_daily.sql"}}
{"action_type":"inspect_schema","parameters":{"asset":"raw.orders_events"}}
{"action_type":"inspect_lineage","parameters":{"asset":"risk_model_scoring"}}
{"action_type":"inspect_llm_draft","parameters":{"draft_id":"primary"}}
{"action_type":"edit_file","parameters":{"path":"sql/merchant_risk_features.sql","content":"..."}}
{"action_type":"run_validator","parameters":{"validator":"governance_check"}}
{"action_type":"submit_repair","parameters":{"root_cause":"schema rename event_ts -> event_time","fix_path":"transforms/orders_daily.sql","summary":"..."}}
{"action_type":"submit_review","parameters":{"verdict":"reject","issue_type":"pii_exposure","summary":"..."}}
{"action_type":"submit_workspace","parameters":{"summary":"..."}}
```

## Observation space

The observation is typed in [models.py](/Users/pranjalparashar/Desktop/hackz/cw-apr1/control-room/developer_control_room/models.py) and includes:

- `developer_request`
- `workspace_summary`
- `known_files`
- `editable_targets`
- `known_assets`
- `available_validators`
- `queried_data`
- `edited_files`
- `validator_status`
- `cumulative_reward`
- `feedback`
- `last_action_error`

The agent only sees operational context and its own interaction history. Hidden grader targets stay in the scenario fixtures and are not exposed directly in observations.

## Reward shaping

Rewards are dense across the episode:

- small positive reward for first-time useful investigation actions
- negative reward for repeated, invalid, or no-op actions
- a progress delta from the deterministic grader after each step
- additional signal when validators are run and begin passing
- late-episode and max-step penalties for inefficient trajectories

This means the agent gets feedback for real progress, not just a binary terminal signal.

## Deterministic graders

Each task has a deterministic grader implemented in [graders.py](/Users/pranjalparashar/Desktop/hackz/cw-apr1/control-room/developer_control_room/graders.py):

- `repair`: investigation + correct file edit + validator passes + root-cause submission
- `review`: investigation + verdict + issue type + summary evidence
- `workflow`: reference inspection + artifact quality + governance + validator passes + summary

The public task list contains 6 tasks, but these 6 tasks intentionally map to 3 internal grader families because there are only 3 objective types. All task scores are constrained to `0.0–1.0`.

## Anti-gaming and reproducibility

The environment is designed so that reward comes from useful work, not cheap action farming:

- repeated reads and repeated validators degrade in reward
- invalid or poorly grounded actions lose reward
- hard tasks intentionally require more investigation and broader validator coverage
- same task, scenario, and action trajectory always produce the same score

The repo now includes explicit tests for:

- repeated validator spam
- repeated file reads
- invalid early edits in the hybrid baseline
- reset-state cleanliness
- deterministic replay scoring
- solved versus unsolved hard-scenario separation

### Score and success semantics

- `score` is a normalized quality signal in `0.0–1.0`
- `success` is not a simple threshold; it reflects whether the scenario’s core solve criteria were met
- this allows a scenario to score highly without being marked fully solved, especially in the hard tasks

### Intended calibration

- easy tasks should cluster highest
- medium tasks should cluster below easy
- hard tasks should cluster lowest on average
- hard tasks intentionally mix solved and partial scenarios

Latest all-scenario baseline run:

| Public task | Typical band |
|---|---|
| `repair_data_transform` | `0.88-0.98` |
| `repair_pipeline_execution` | `0.90-0.97` |
| `review_ai_patch_safety` | `0.82-0.86` |
| `review_ai_patch_correctness` | `0.78-0.83` |
| `synthesize_reporting_asset` | `0.65-0.71` |
| `synthesize_data_product` | `0.61-0.70` |

Overall, the current calibrated profile is:

- easy tasks: about `0.93-0.95`
- medium tasks: about `0.82-0.84`
- hard tasks: about `0.65-0.68`

## Baseline inference

The root [inference.py](/Users/pranjalparashar/Desktop/hackz/cw-apr1/control-room/developer_control_room/inference.py) is the submission-facing entrypoint. It uses the OpenAI client with:

- `API_BASE_URL`
- `MODEL_NAME`
- `HF_TOKEN`

The benchmark-specific helper logic lives in [baseline.py](/Users/pranjalparashar/Desktop/hackz/cw-apr1/control-room/developer_control_room/baseline.py):

- prompt construction
- model response parsing
- deterministic fallback policy
- baseline artifact templates
- grader fetch helpers

By default, the baseline is a bounded hybrid policy: it allows `2` model-led steps, then switches to deterministic fallback behavior for the rest of the episode. You can change that budget with `DEVELOPER_CONTROL_ROOM_MAX_MODEL_STEPS` if you want a more or less model-led baseline for local experiments.

The baseline also supports two optional experimental knobs for local research:

- light seeded scenario variation through `DEVELOPER_CONTROL_ROOM_SCENARIO_SEED`
- cross-episode baseline memory through `DEVELOPER_CONTROL_ROOM_ENABLE_EPISODE_MEMORY=true`

That memory is policy-side only: it writes compact episode summaries to a temp JSON file and reuses them in later prompts, but it does not change environment state, reset behavior, or grading.

### Inference behavior

- `python inference.py` runs all 6 public tasks by default
- `DEVELOPER_CONTROL_ROOM_TASK=<task_id> python inference.py` runs one task
- `DEVELOPER_CONTROL_ROOM_RUN_ALL_SCENARIOS=true python inference.py` runs every scenario inside every selected task
- `DEVELOPER_CONTROL_ROOM_SCENARIO_INDEX=<n>` selects a specific scenario index inside each selected task
- `DEVELOPER_CONTROL_ROOM_SCENARIO_SEED=<n>` selects scenarios deterministically by seed

Example:

```bash
DEVELOPER_CONTROL_ROOM_RUN_ALL_SCENARIOS=true python inference.py
```

## Local usage

```bash
uv sync
uv run server
```

Or with Docker:

```bash
docker build -t developer-control-room-env .
docker run -p 7860:7860 developer-control-room-env
```

Example local container run:

```bash
docker build -t developer_control_room:latest .
docker stop developer_control_room 2>/dev/null
docker rm developer_control_room 2>/dev/null
docker run -d --name developer_control_room -p 7860:7860 developer_control_room:latest
python inference.py
```

## Validation

The benchmark has also been validated against the submission-style checks:

- the deployed environment responds to `/reset`
- Docker build succeeds
- `inference.py` emits the required `[START]`, `[STEP]`, and `[END]` lines

## Project structure

```text
developer_control_room/
├── Dockerfile
├── README.md
├── openenv.yaml
├── pyproject.toml
├── inference.py
├── baseline.py
├── tasks.py
├── graders.py
├── models.py
├── client.py
└── server/
    ├── app.py
    └── developer_control_room_environment.py
```
