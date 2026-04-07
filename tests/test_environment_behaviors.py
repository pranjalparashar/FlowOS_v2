from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from baseline import action_is_valid, build_action, fallback_action, get_model_action
from graders import grade
from server.developer_control_room_environment import DeveloperControlRoomEnvironment
from tasks import get_scenario


def run_episode_with_fallback(task_id: str, scenario_index: int) -> dict:
    env = DeveloperControlRoomEnvironment()
    observation = env.reset(task_id=task_id, scenario_index=scenario_index)
    while not env._state.done:
        action_dict = fallback_action(task_id, observation)
        observation = env.step(build_action(action_dict))
    return {
        "state": env._state.model_dump(),
        "scenario": env._scenario,
        "grade": grade(task_id, env._state.model_dump(), env._scenario),
    }


def test_repeated_file_reads_are_penalized() -> None:
    env = DeveloperControlRoomEnvironment()
    env.reset(task_id="repair_pipeline_execution", scenario_index=0)

    first = env.step(
        build_action(
            {
                "action_type": "read_file",
                "parameters": {"path": "pipelines/customer_events_ingest.yaml"},
            }
        )
    )
    second = env.step(
        build_action(
            {
                "action_type": "read_file",
                "parameters": {"path": "pipelines/customer_events_ingest.yaml"},
            }
        )
    )

    assert first.reward > 0
    assert second.reward < first.reward


def test_repeated_validator_spam_gets_worse() -> None:
    env = DeveloperControlRoomEnvironment()
    env.reset(task_id="repair_pipeline_execution", scenario_index=1)

    env.step(
        build_action(
            {
                "action_type": "read_file",
                "parameters": {"path": "ci/jenkins/orders_daily.groovy"},
            }
        )
    )
    env.step(
        build_action(
            {
                "action_type": "edit_file",
                "parameters": {
                    "path": "ci/jenkins/orders_daily.groovy",
                    "content": """pipeline {
  agent any
  stages {
    stage('Run') {
      steps {
        sh './scripts/run_orders_daily.sh'
      }
    }
  }
}
""",
                },
            }
        )
    )

    first = env.step(
        build_action(
            {
                "action_type": "run_validator",
                "parameters": {"validator": "jenkins_lint"},
            }
        )
    )
    second = env.step(
        build_action(
            {
                "action_type": "run_validator",
                "parameters": {"validator": "jenkins_lint"},
            }
        )
    )

    assert first.reward > 0
    assert second.reward <= 0
    assert second.reward < first.reward


def test_invalid_early_edits_and_validators_are_blocked_by_hybrid_guardrails() -> None:
    env = DeveloperControlRoomEnvironment()

    reporting_obs = env.reset(task_id="synthesize_data_product", scenario_index=1)
    assert not action_is_valid(
        {
            "action_type": "edit_file",
            "parameters": {"path": "pipelines/customer_margin_mart.yaml", "content": "x"},
        },
        reporting_obs,
    )

    repair_obs = env.reset(task_id="repair_pipeline_execution", scenario_index=4)
    assert not action_is_valid(
        {
            "action_type": "run_validator",
            "parameters": {"validator": "column_mapping_guard"},
        },
        repair_obs,
    )


def test_same_trajectory_gives_same_score() -> None:
    first = run_episode_with_fallback("repair_pipeline_execution", 0)
    second = run_episode_with_fallback("repair_pipeline_execution", 0)

    assert first["state"]["action_history"] == second["state"]["action_history"]
    assert first["state"]["cumulative_reward"] == second["state"]["cumulative_reward"]
    assert first["grade"]["total"] == second["grade"]["total"]
    assert first["grade"]["solved"] == second["grade"]["solved"]


def test_hard_tasks_mix_solved_and_unsolved_scenarios() -> None:
    solved_reporting = run_episode_with_fallback("synthesize_reporting_asset", 0)["grade"]
    unsolved_reporting = run_episode_with_fallback("synthesize_reporting_asset", 1)["grade"]
    unsolved_product = run_episode_with_fallback("synthesize_data_product", 0)["grade"]
    solved_product = run_episode_with_fallback("synthesize_data_product", 1)["grade"]

    assert solved_reporting["solved"] is True
    assert unsolved_reporting["solved"] is False
    assert unsolved_product["solved"] is False
    assert solved_product["solved"] is True
    assert solved_reporting["total"] > unsolved_reporting["total"]
    assert solved_product["total"] > unsolved_product["total"]


def test_reset_produces_clean_state() -> None:
    env = DeveloperControlRoomEnvironment()
    env.reset(task_id="repair_data_transform", scenario_index=0)
    env.step(
        build_action(
            {
                "action_type": "read_file",
                "parameters": {"path": "transforms/orders_daily.sql"},
            }
        )
    )
    env.step(
        build_action(
            {
                "action_type": "inspect_schema",
                "parameters": {"asset": "raw.orders_events"},
            }
        )
    )

    fresh = env.reset(task_id="review_ai_patch_safety", scenario_index=0)

    assert fresh.step_count == 0
    assert fresh.cumulative_reward == 0.0
    assert fresh.queried_data == {}
    assert fresh.edited_files == {}
    assert fresh.validator_status == {}
    assert fresh.last_action_error is None


def test_forced_model_timeout_returns_none(monkeypatch) -> None:
    env = DeveloperControlRoomEnvironment()
    observation = env.reset(task_id="repair_data_transform", scenario_index=0)

    monkeypatch.setenv("DEVELOPER_CONTROL_ROOM_FORCE_MODEL_TIMEOUT", "true")
    result = get_model_action(
        client=None,  # type: ignore[arg-type]
        model_name="demo-model",
        temperature=0.2,
        max_tokens=32,
        timeout_seconds=0.01,
        step=1,
        observation=observation,
        history=[],
    )

    assert result is None


def test_seeded_variants_are_deterministic_and_change_literals() -> None:
    first = get_scenario("review_ai_patch_correctness", 1, seed=1)
    second = get_scenario("review_ai_patch_correctness", 1, seed=1)
    third = get_scenario("review_ai_patch_correctness", 1, seed=2)

    assert first["developer_request"] == second["developer_request"]
    assert first["files"]["schemas/subscription_mrr_daily.json"] == second["files"]["schemas/subscription_mrr_daily.json"]
    assert first["developer_request"] != third["developer_request"]
