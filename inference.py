"""
Inference Script Example
===================================
MANDATORY
- Before submitting, ensure the following variables are defined in your environment configuration:
    API_BASE_URL   The API endpoint for the LLM.
    MODEL_NAME     The model identifier to use for inference.
    HF_TOKEN       Your Hugging Face / API key.
    LOCAL_IMAGE_NAME The name of the local image to use for the environment if you are using from_docker_image()
                     method

- Defaults are set only for API_BASE_URL and MODEL_NAME
    (and should reflect your active inference setup):
    API_BASE_URL = os.getenv("API_BASE_URL", "<your-active-endpoint>")
    MODEL_NAME = os.getenv("MODEL_NAME", "<your-active-model>")

- The inference script must be named `inference.py` and placed in the root directory of the project
- Participants must use OpenAI Client for all LLM calls using above variables

STDOUT FORMAT
- The script must emit exactly three line types to stdout, in this order:

    [START] task=<task_name> env=<benchmark> model=<model_name>
    [STEP]  step=<n> action=<action_str> reward=<0.00> done=<true|false> error=<msg|null>
    [END]   success=<true|false> steps=<n> score=<score> rewards=<r1,r2,...,rn>
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from typing import Any, List, Optional

from openai import OpenAI

from baseline import action_is_valid, build_action, fallback_action, fetch_grader_result, get_model_action
from client import DeveloperControlRoomEnv
from tasks import list_tasks


def load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return

    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(path)
        return
    except Exception:
        pass

    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


load_env_file()

LOCAL_IMAGE_NAME = os.getenv("LOCAL_IMAGE_NAME") or os.getenv("IMAGE_NAME")
ENV_URL = os.getenv("ENV_URL")
API_KEY = os.getenv("HF_TOKEN") or os.getenv("OPENAI_API_KEY") or os.getenv("API_KEY")

API_BASE_URL = os.getenv("API_BASE_URL") or "https://router.huggingface.co/v1"
MODEL_NAME = os.getenv("MODEL_NAME") or "Qwen/Qwen2.5-72B-Instruct"
TASK_NAME = os.getenv("DEVELOPER_CONTROL_ROOM_TASK")
BENCHMARK = os.getenv("DEVELOPER_CONTROL_ROOM_BENCHMARK", "developer_control_room")
MAX_STEPS = int(os.getenv("DEVELOPER_CONTROL_ROOM_MAX_STEPS", "14"))
DEFAULT_MODEL_STEPS = 2
MAX_MODEL_STEPS = int(
    os.getenv("DEVELOPER_CONTROL_ROOM_MAX_MODEL_STEPS", str(DEFAULT_MODEL_STEPS))
)
RUN_ALL_SCENARIOS = os.getenv("DEVELOPER_CONTROL_ROOM_RUN_ALL_SCENARIOS", "false").lower() == "true"
SCENARIO_INDEX_RAW = os.getenv("DEVELOPER_CONTROL_ROOM_SCENARIO_INDEX")
SCENARIO_SEED_RAW = os.getenv("DEVELOPER_CONTROL_ROOM_SCENARIO_SEED")
SCENARIO_INDEX = int(SCENARIO_INDEX_RAW) if SCENARIO_INDEX_RAW not in (None, "") else None
SCENARIO_SEED = int(SCENARIO_SEED_RAW) if SCENARIO_SEED_RAW not in (None, "") else None
TEMPERATURE = 0.2
MAX_TOKENS = 250
MODEL_TIMEOUT_SECONDS = float(os.getenv("DEVELOPER_CONTROL_ROOM_MODEL_TIMEOUT_SECONDS", "20"))
ENABLE_EPISODE_MEMORY = os.getenv("DEVELOPER_CONTROL_ROOM_ENABLE_EPISODE_MEMORY", "false").lower() == "true"
DEBUG_LOGGING = os.getenv("DEVELOPER_CONTROL_ROOM_DEBUG", "false").lower() == "true"
EPISODE_MEMORY_PATH = os.getenv(
    "DEVELOPER_CONTROL_ROOM_EPISODE_MEMORY_PATH",
    os.path.join(tempfile.gettempdir(), "developer_control_room_episode_memory.json"),
)
EPISODE_MEMORY_LIMIT = int(os.getenv("DEVELOPER_CONTROL_ROOM_EPISODE_MEMORY_LIMIT", "6"))


def log_start(task: str, env: str, model: str) -> None:
    print(f"[START] task={task} env={env} model={model}", flush=True)


def log_step(step: int, action: str, reward: float, done: bool, error: Optional[str]) -> None:
    error_val = _single_line(error) if error else "null"
    done_val = str(done).lower()
    print(
        f"[STEP] step={step} action={_single_line(action)} reward={reward:.2f} done={done_val} error={error_val}",
        flush=True,
    )


def log_end(success: bool, steps: int, score: float, rewards: List[float]) -> None:
    rewards_str = ",".join(f"{r:.2f}" for r in rewards)
    print(
        f"[END] success={str(success).lower()} steps={steps} score={max(0.0, min(score, 1.0)):.2f} rewards={rewards_str}",
        flush=True,
    )


def _single_line(value: str | None) -> str:
    return (value or "").replace("\n", "\\n").replace("\r", "\\r")


def debug_log(message: str) -> None:
    if DEBUG_LOGGING:
        print(message, file=sys.stderr, flush=True)


def format_action_str(action_dict: Optional[dict[str, Any]]) -> str:
    if not isinstance(action_dict, dict):
        return "null"
    action_type = str(action_dict.get("action_type") or "").strip() or "unknown"
    params = action_dict.get("parameters", {})
    if not isinstance(params, dict) or not params:
        return f"{action_type}()"

    parts: list[str] = []
    for key in sorted(params):
        value = params[key]
        if value is None:
            rendered = "null"
        elif isinstance(value, bool):
            rendered = str(value).lower()
        elif isinstance(value, (int, float)):
            rendered = str(value)
        else:
            text = str(value).replace("\\", "\\\\").replace("'", "\\'")
            text = text.replace("\n", "\\n").replace("\r", "\\r")
            rendered = f"'{text}'"
        parts.append(f"{key}={rendered}")
    return f"{action_type}({','.join(parts)})"


def load_episode_memory() -> list[dict[str, Any]]:
    if not ENABLE_EPISODE_MEMORY or not os.path.exists(EPISODE_MEMORY_PATH):
        return []
    try:
        with open(EPISODE_MEMORY_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
    except Exception as exc:
        debug_log(f"[DEBUG] episode memory load failed: {exc}")
    return []


def save_episode_memory(entries: list[dict[str, Any]]) -> None:
    if not ENABLE_EPISODE_MEMORY:
        return
    try:
        with open(EPISODE_MEMORY_PATH, "w", encoding="utf-8") as handle:
            json.dump(entries[-EPISODE_MEMORY_LIMIT:], handle, ensure_ascii=True, indent=2)
    except Exception as exc:
        debug_log(f"[DEBUG] episode memory save failed: {exc}")


def summarize_episode_memory(entries: list[dict[str, Any]]) -> str:
    if not ENABLE_EPISODE_MEMORY or not entries:
        return ""
    recent = entries[-min(3, len(entries)) :]
    lines: list[str] = []
    for item in recent:
        lines.append(
            f"- task={item.get('task_id')} scenario={item.get('scenario_id')} "
            f"solved={str(bool(item.get('solved', False))).lower()} "
            f"score={float(item.get('score', 0.0)):.2f} takeaway={item.get('takeaway', '')}"
        )
    return "\n".join(lines)


def record_episode_memory(
    entries: list[dict[str, Any]],
    task_name: str,
    scenario_id: str | None,
    solved: bool,
    score: float,
    history: list[str],
) -> list[dict[str, Any]]:
    if not ENABLE_EPISODE_MEMORY:
        return entries
    takeaway = ""
    if solved:
        takeaway = "Pattern solved successfully; repeat similar investigation and validation ordering."
    elif history:
        takeaway = "Recent attempt stayed partial; gather grounding evidence earlier and validate before submitting."
    entry = {
        "task_id": task_name,
        "scenario_id": scenario_id or "unknown",
        "solved": bool(solved),
        "score": max(0.0, min(score, 1.0)),
        "takeaway": takeaway,
        "actions": history[-4:],
    }
    updated = entries + [entry]
    return updated[-EPISODE_MEMORY_LIMIT:]


async def create_env() -> DeveloperControlRoomEnv:
    if ENV_URL:
        env = DeveloperControlRoomEnv(base_url=ENV_URL)
        await env.connect()
        return env
    if not LOCAL_IMAGE_NAME:
        raise ValueError("Set ENV_URL or LOCAL_IMAGE_NAME/IMAGE_NAME before running inference.py")
    return await DeveloperControlRoomEnv.from_docker_image(LOCAL_IMAGE_NAME)


async def run_task(
    client: OpenAI,
    task_name: str,
    memory_entries: list[dict[str, Any]],
    scenario_index: int | None = None,
    scenario_seed: int | None = None,
) -> list[dict[str, Any]]:
    env: DeveloperControlRoomEnv | None = None
    history: List[str] = []
    rewards: List[float] = []
    steps_taken = 0
    success = False
    score = 0.0
    scenario_id: str | None = None

    log_start(task=task_name, env=BENCHMARK, model=MODEL_NAME)

    try:
        env = await create_env()
        reset_kwargs: dict[str, Any] = {"task_id": task_name}
        if scenario_index is not None:
            reset_kwargs["scenario_index"] = scenario_index
        elif scenario_seed is not None:
            reset_kwargs["seed"] = scenario_seed
        result = await env.reset(**reset_kwargs)
        observation = result.observation
        scenario_id = observation.scenario_id
        episode_memory = summarize_episode_memory(memory_entries)

        for step in range(1, MAX_STEPS + 1):
            if result.done:
                break

            action_dict: Optional[dict[str, Any]] = None
            if step <= MAX_MODEL_STEPS:
                action_dict = get_model_action(
                    client,
                    MODEL_NAME,
                    TEMPERATURE,
                    MAX_TOKENS,
                    MODEL_TIMEOUT_SECONDS,
                    step,
                    observation,
                    history,
                    episode_memory,
                )
            if not action_is_valid(action_dict, observation):
                action_dict = fallback_action(task_name, observation)

            action = build_action(action_dict)
            result = await env.step(action)
            observation = result.observation

            reward = result.reward or 0.0
            done = result.done
            error = observation.last_action_error

            rewards.append(reward)
            steps_taken = step

            log_step(
                step=step,
                action=format_action_str(action_dict),
                reward=reward,
                done=done,
                error=error,
            )

            history.append(f"Step {step}: {format_action_str(action_dict)} -> reward {reward:+.2f}")

            if done:
                break

        grader_result = fetch_grader_result(env, ENV_URL)
        score = grader_result["total"]
        success = grader_result["solved"]
    except Exception as exc:
        debug_log(f"[DEBUG] task run failed: {exc}")
        success = False
        score = 0.0

    finally:
        try:
            if env is not None:
                await env.close()
        except Exception as exc:
            debug_log(f"[DEBUG] env.close() error (container cleanup): {exc}")
        log_end(success=success, steps=steps_taken, score=score, rewards=rewards)
    return record_episode_memory(memory_entries, task_name, scenario_id, success, score, history)


async def main() -> None:
    client = OpenAI(base_url=API_BASE_URL, api_key=API_KEY)
    memory_entries = load_episode_memory()
    tasks = list_tasks()
    selected_tasks = [task for task in tasks if task["id"] == TASK_NAME] if TASK_NAME else tasks

    for task_offset, task in enumerate(selected_tasks):
        task_name = task["id"]
        if RUN_ALL_SCENARIOS:
            for scenario_index in range(task["scenarios"]):
                scenario_seed = None if SCENARIO_SEED is None else SCENARIO_SEED + scenario_index + task_offset
                memory_entries = await run_task(
                    client,
                    task_name,
                    memory_entries,
                    scenario_index=scenario_index,
                    scenario_seed=scenario_seed,
                )
                save_episode_memory(memory_entries)
            continue

        scenario_seed = None
        if SCENARIO_INDEX is None and SCENARIO_SEED is not None:
            scenario_seed = SCENARIO_SEED + task_offset

        memory_entries = await run_task(
            client,
            task_name,
            memory_entries,
            scenario_index=SCENARIO_INDEX,
            scenario_seed=scenario_seed,
        )
        save_episode_memory(memory_entries)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        debug_log(f"[DEBUG] inference failed: {exc}")
        raise SystemExit(1)
