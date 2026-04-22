"""Collect supervised traces for FlowOS without GRPO."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

try:
    from .baseline import fallback_action
    from .training_utils import (
        TRAIN_SYSTEM_PROMPT,
        build_episode_samples,
        build_turn_prompt,
        coerce_action,
        format_action_text,
        parse_sample_prompt,
        persist_episode_artifacts,
        samples_to_dataset_prompts,
    )
    from .client import DeveloperControlRoomEnv
except ImportError:
    from baseline import fallback_action
    from training_utils import (
        TRAIN_SYSTEM_PROMPT,
        build_episode_samples,
        build_turn_prompt,
        coerce_action,
        format_action_text,
        parse_sample_prompt,
        persist_episode_artifacts,
        samples_to_dataset_prompts,
    )
    from client import DeveloperControlRoomEnv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect fallback/teacher traces for FlowOS SFT")
    parser.add_argument("--env-url", default="http://localhost:7860", help="FlowOS OpenEnv server URL")
    parser.add_argument("--task-scope", default="all", help="Task scope: all or comma-separated task ids")
    parser.add_argument("--dataset-size", type=int, default=8, help="Number of episodes to collect")
    parser.add_argument("--max-turns", type=int, default=12, help="Max environment turns per episode")
    parser.add_argument("--output-dir", default="outputs/sft_traces", help="Directory for JSONL traces")
    parser.add_argument("--policy", default="fallback", choices=("fallback",), help="Teacher policy to use")
    return parser.parse_args()


async def collect_episode(
    env_url: str,
    sample_prompt: str,
    max_turns: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    sample = parse_sample_prompt(sample_prompt)
    env = DeveloperControlRoomEnv(base_url=env_url)
    await env.connect()
    transcript: list[dict[str, Any]] = []
    examples: list[dict[str, Any]] = []
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
            prompt = build_turn_prompt(observation, transcript)
            action_dict = fallback_action(sample.task_id, observation)
            action = coerce_action(action_dict)
            action_json = json.dumps(action_dict, separators=(",", ":"), ensure_ascii=False)
            examples.append(
                {
                    "task_id": sample.task_id,
                    "scenario_id": observation.scenario_id,
                    "episode_id": observation.episode_id,
                    "step": observation.step_count + 1,
                    "system_prompt": TRAIN_SYSTEM_PROMPT,
                    "user_prompt": prompt,
                    "target_action": action_json,
                    "action_type": action.action_type,
                    "editable_targets": observation.editable_targets,
                    "available_validators": observation.available_validators,
                }
            )

            result = await env.step(action)
            observation = result.observation
            transcript.append(
                {
                    "action_text": format_action_text(action),
                    "reward": float(result.reward or 0.0),
                    "feedback": observation.feedback,
                    "error": observation.last_action_error,
                    "raw_model_text": "",
                }
            )
            if result.done:
                break

        metrics = {
            "task_id": sample.task_id,
            "scenario_id": observation.scenario_id,
            "steps": len(transcript),
            "materialized_artifacts": dict(getattr(observation, "materialized_artifacts", {})),
            "runtime_status": dict(getattr(observation, "runtime_status", {})),
            "output_schema": list(getattr(observation, "output_schema", [])),
            "report_preview": list(getattr(observation, "report_preview", [])),
        }
        return examples, metrics
    finally:
        await env.close()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    traces_path = output_dir / "traces.jsonl"
    artifact_root = output_dir / "artifacts"

    prompts = samples_to_dataset_prompts(build_episode_samples(args.task_scope, args.dataset_size))

    all_examples: list[dict[str, Any]] = []
    for prompt in prompts:
        examples, metrics = asyncio.run(collect_episode(args.env_url, prompt, args.max_turns))
        all_examples.extend(examples)
        if metrics["materialized_artifacts"] or metrics["runtime_status"]:
            class _Metrics:
                def __init__(self, payload: dict[str, Any]) -> None:
                    self.task_id = payload["task_id"]
                    self.scenario_id = payload["scenario_id"]
                    self.steps = payload["steps"]
                    self.materialized_artifacts = payload["materialized_artifacts"]
                    self.runtime_status = payload["runtime_status"]
                    self.output_schema = payload["output_schema"]
                    self.report_preview = payload["report_preview"]

            persist_episode_artifacts(artifact_root, _Metrics(metrics))

    with traces_path.open("w", encoding="utf-8") as handle:
        for row in all_examples:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"Wrote {len(all_examples)} supervised examples to {traces_path}")
    print(f"Artifacts stored under {artifact_root}")


if __name__ == "__main__":
    main()
