"""Shared helpers for FlowOS training and evaluation."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from itertools import cycle, islice
from typing import Any, Callable

import requests

try:
    from .client import DeveloperControlRoomEnv
    from .models import ActionParameters, DeveloperControlRoomAction, DeveloperControlRoomObservation
    from .tasks import TASK_DEFINITIONS, scenario_count
except ImportError:
    from client import DeveloperControlRoomEnv
    from models import ActionParameters, DeveloperControlRoomAction, DeveloperControlRoomObservation
    from tasks import TASK_DEFINITIONS, scenario_count


DEFAULT_TRAIN_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
DEFAULT_MAX_TURNS = 12
DEFAULT_MAX_NEW_TOKENS = 200

TRAIN_SYSTEM_PROMPT = """You are operating the FlowOS benchmark as a production engineering agent.
Reply with exactly one JSON object with keys "action_type" and "parameters".
Do not include markdown, explanations, or code fences.

Allowed action shapes:
{"action_type":"search_workspace","parameters":{"query":"..."}}
{"action_type":"read_file","parameters":{"path":"..."}}
{"action_type":"inspect_schema","parameters":{"asset":"..."}}
{"action_type":"inspect_lineage","parameters":{"asset":"..."}}
{"action_type":"inspect_llm_draft","parameters":{"draft_id":"primary"}}
{"action_type":"edit_file","parameters":{"path":"...","content":"..."}}
{"action_type":"run_validator","parameters":{"validator":"..."}}
{"action_type":"submit_repair","parameters":{"root_cause":"...","fix_path":"...","summary":"..."}}
{"action_type":"submit_review","parameters":{"verdict":"approve|reject","issue_type":"...","summary":"..."}}
{"action_type":"submit_workspace","parameters":{"summary":"..."}}

Use exact file paths, asset names, and validator names from the observation.
Good examples:
{"action_type":"read_file","parameters":{"path":"docs/runtime_contract.md"}}
{"action_type":"inspect_schema","parameters":{"asset":"raw.sales_orders_csv"}}
{"action_type":"edit_file","parameters":{"path":"pipelines/report_job.yaml","content":"name: customer_daily_report_job\\nstorage_path: mock_s3/staged/sales_orders.csv\\nraw_table: raw_sales_orders\\nload_sql: sql/load_raw.sql\\nbuild_sql: sql/build_table.sql\\nreport_sql: sql/report_view.sql\\nfinal_view: customer_daily_report\\n"}}
For simulate_csv_report_workflow, prefer starting with read_file on docs/runtime_contract.md or templates/report_pipeline_template.yaml.
Return only one action per turn."""


@dataclass
class EpisodeSample:
    task_id: str
    scenario_index: int
    seed: int


@dataclass
class EpisodeMetrics:
    task_id: str
    scenario_id: str
    rewards: list[float]
    total_reward: float
    score: float
    solved: bool
    steps: int
    transcript: list[dict[str, Any]]
    prompt_ids: list[int] | None = None
    completion_ids: list[int] | None = None
    logprobs: list[float] | None = None


def resolve_task_scope(task_scope: str) -> list[str]:
    if not task_scope or task_scope == "all":
        return list(TASK_DEFINITIONS)
    task_ids = [task.strip() for task in task_scope.split(",") if task.strip()]
    unknown = [task for task in task_ids if task not in TASK_DEFINITIONS]
    if unknown:
        raise ValueError(f"Unknown task ids in --task-scope: {', '.join(unknown)}")
    return task_ids


def build_episode_samples(task_scope: str, dataset_size: int) -> list[EpisodeSample]:
    task_ids = resolve_task_scope(task_scope)
    universe: list[EpisodeSample] = []
    seed = 0
    for task_id in task_ids:
        for scenario_index in range(scenario_count(task_id)):
            universe.append(EpisodeSample(task_id=task_id, scenario_index=scenario_index, seed=seed))
            seed += 1
    if not universe:
        return []
    return list(islice(cycle(universe), dataset_size))


def samples_to_dataset_prompts(samples: list[EpisodeSample]) -> list[str]:
    return [
        json.dumps(
            {
                "task_id": sample.task_id,
                "scenario_index": sample.scenario_index,
                "seed": sample.seed,
            },
            separators=(",", ":"),
        )
        for sample in samples
    ]


def parse_sample_prompt(prompt: str) -> EpisodeSample:
    payload = json.loads(prompt)
    return EpisodeSample(
        task_id=str(payload["task_id"]),
        scenario_index=int(payload["scenario_index"]),
        seed=int(payload.get("seed", 0)),
    )


def parse_action_json(response_text: str) -> dict[str, Any] | None:
    text = (response_text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("```")
        ).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for idx, char in enumerate(text):
            if char != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(text[idx:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def coerce_action(action_dict: dict[str, Any] | None) -> DeveloperControlRoomAction:
    if not isinstance(action_dict, dict):
        return DeveloperControlRoomAction(
            action_type="invalid_action",
            parameters=ActionParameters(),
        )
    action_type = str(action_dict.get("action_type") or "").strip() or "invalid_action"
    params = action_dict.get("parameters", {})
    if not isinstance(params, dict):
        params = {}
    return DeveloperControlRoomAction(
        action_type=action_type,
        parameters=ActionParameters(**params),
    )


def format_history(transcript: list[dict[str, Any]]) -> str:
    if not transcript:
        return "None"
    lines: list[str] = []
    for entry in transcript[-4:]:
        lines.append(
            "Action: {action}\nReward: {reward:.2f}\nFeedback: {feedback}\nError: {error}".format(
                action=entry.get("action_text", "invalid_action()"),
                reward=float(entry.get("reward", 0.0)),
                feedback=entry.get("feedback") or "none",
                error=entry.get("error") or "null",
            )
        )
    return "\n\n".join(lines)


def format_observation(observation: DeveloperControlRoomObservation) -> str:
    return (
        f"Task: {observation.task_id}\n"
        f"Scenario: {observation.scenario_id}\n"
        f"Step: {observation.step_count}/{observation.max_steps}\n"
        f"Active role: {observation.active_role}\n"
        f"Request: {observation.developer_request}\n"
        f"Workspace summary: {observation.workspace_summary}\n"
        f"Known files: {json.dumps(observation.known_files)}\n"
        f"Editable targets: {json.dumps(observation.editable_targets)}\n"
        f"Known assets: {json.dumps(observation.known_assets)}\n"
        f"Validators: {json.dumps(observation.available_validators)}\n"
        f"Queried data keys: {json.dumps(sorted(observation.queried_data.keys()))}\n"
        f"Edited files: {json.dumps(sorted(observation.edited_files.keys()))}\n"
        f"Validator status: {json.dumps(observation.validator_status)}\n"
        f"Runtime status: {json.dumps(observation.runtime_status)}\n"
        f"Execution logs: {json.dumps(observation.execution_logs)}\n"
        f"Output schema: {json.dumps(observation.output_schema)}\n"
        f"Feedback: {observation.feedback or 'none'}\n"
        f"Last action error: {observation.last_action_error or 'null'}"
    )


def build_turn_prompt(
    observation: DeveloperControlRoomObservation,
    transcript: list[dict[str, Any]],
) -> str:
    return (
        "Current observation:\n"
        f"{format_observation(observation)}\n\n"
        "Recent action history:\n"
        f"{format_history(transcript)}\n\n"
        "Choose exactly one next JSON action."
    )


def format_action_text(action: DeveloperControlRoomAction) -> str:
    payload = action.parameters.model_dump(exclude_none=True)
    if not payload:
        return f"{action.action_type}()"
    parts = [f"{key}={payload[key]!r}" for key in sorted(payload)]
    return f"{action.action_type}({','.join(parts)})"


def fetch_grader_result(base_url: str) -> dict[str, Any]:
    response = requests.get(f"{base_url.rstrip('/')}/grader", timeout=20)
    response.raise_for_status()
    payload = response.json()
    return {
        "total": float(payload.get("total", 0.0)),
        "solved": bool(payload.get("solved", False)),
    }


async def run_episode_async(
    base_url: str,
    sample: EpisodeSample,
    max_turns: int,
    policy: Callable[[DeveloperControlRoomObservation, list[dict[str, Any]]], dict[str, Any]],
) -> EpisodeMetrics:
    env = DeveloperControlRoomEnv(base_url=base_url)
    await env.connect()
    transcript: list[dict[str, Any]] = []
    rewards: list[float] = []
    prompt_ids: list[int] = []
    completion_ids: list[int] = []
    logprobs: list[float] = []
    try:
        result = await env.reset(
            task_id=sample.task_id,
            scenario_index=sample.scenario_index,
            seed=sample.seed,
        )
        observation = result.observation
        for _ in range(max_turns):
            if result.done:
                break
            action_payload = policy(observation, transcript)
            action = coerce_action(action_payload.get("action"))
            metadata = action_payload.get("metadata", {})
            prompt_ids.extend(metadata.get("prompt_ids", []))
            completion_ids.extend(metadata.get("completion_ids", []))
            logprobs.extend(metadata.get("logprobs", []))

            result = await env.step(action)
            observation = result.observation
            reward = float(result.reward or 0.0)
            rewards.append(reward)
            transcript.append(
                {
                    "action_text": format_action_text(action),
                    "reward": reward,
                    "feedback": observation.feedback,
                    "error": observation.last_action_error,
                    "raw_model_text": metadata.get("text", ""),
                }
            )
            if result.done:
                break

        grader = fetch_grader_result(base_url)
        return EpisodeMetrics(
            task_id=sample.task_id,
            scenario_id=observation.scenario_id,
            rewards=rewards,
            total_reward=sum(rewards),
            score=float(grader["total"]),
            solved=bool(grader["solved"]),
            steps=len(rewards),
            transcript=transcript,
            prompt_ids=prompt_ids,
            completion_ids=completion_ids,
            logprobs=logprobs,
        )
    finally:
        await env.close()


def run_episode(
    base_url: str,
    sample: EpisodeSample,
    max_turns: int,
    policy: Callable[[DeveloperControlRoomObservation, list[dict[str, Any]]], dict[str, Any]],
) -> EpisodeMetrics:
    return asyncio.run(run_episode_async(base_url, sample, max_turns, policy))
