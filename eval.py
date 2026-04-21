"""Evaluate base and fine-tuned FlowOS policies against the OpenEnv server."""

from __future__ import annotations

import argparse
import statistics
from typing import Any

try:
    from .training_utils import (
        DEFAULT_MAX_NEW_TOKENS,
        DEFAULT_MAX_TURNS,
        DEFAULT_TRAIN_MODEL,
        TRAIN_SYSTEM_PROMPT,
        build_episode_samples,
        build_turn_prompt,
        parse_action_json,
        run_episode,
    )
except ImportError:
    from training_utils import (
        DEFAULT_MAX_NEW_TOKENS,
        DEFAULT_MAX_TURNS,
        DEFAULT_TRAIN_MODEL,
        TRAIN_SYSTEM_PROMPT,
        build_episode_samples,
        build_turn_prompt,
        parse_action_json,
        run_episode,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate FlowOS policies before and after GRPO fine-tuning")
    parser.add_argument("--model-id", default=DEFAULT_TRAIN_MODEL, help="Base model id")
    parser.add_argument("--checkpoint-path", default=None, help="Optional fine-tuned checkpoint or LoRA adapter path")
    parser.add_argument("--env-url", default="http://localhost:7860", help="FlowOS OpenEnv server URL")
    parser.add_argument("--task-scope", default="all", help="Task scope: all or comma-separated task ids")
    parser.add_argument("--episodes", type=int, default=12, help="Total evaluation episodes per policy")
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS, help="Max environment turns per episode")
    parser.add_argument("--max-new-tokens", type=int, default=DEFAULT_MAX_NEW_TOKENS, help="Max tokens per action generation")
    parser.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature for evaluation")
    return parser.parse_args()


def apply_chat_template(tokenizer: Any, messages: list[dict[str, str]]) -> str:
    try:
        return tokenizer.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=False,
            enable_thinking=False,
        )
    except TypeError:
        return tokenizer.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=False,
        )


def load_policy(model_id: str, checkpoint_path: str | None) -> tuple[Any, Any, str]:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    try:
        tokenizer = AutoTokenizer.from_pretrained(checkpoint_path or model_id)
    except Exception:
        tokenizer = AutoTokenizer.from_pretrained(model_id)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    label = "base"
    if checkpoint_path:
        try:
            from peft import AutoPeftModelForCausalLM

            model = AutoPeftModelForCausalLM.from_pretrained(checkpoint_path)
            label = "tuned"
        except Exception:
            model = AutoModelForCausalLM.from_pretrained(checkpoint_path)
            label = "tuned"
    else:
        model = AutoModelForCausalLM.from_pretrained(model_id)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    model.eval()
    return model, tokenizer, label


def generate_action(
    model: Any,
    tokenizer: Any,
    user_prompt: str,
    max_new_tokens: int,
    temperature: float,
) -> dict[str, Any] | None:
    import torch

    prompt_text = apply_chat_template(
        tokenizer,
        [
            {"role": "system", "content": TRAIN_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    device = next(model.parameters()).device
    inputs = tokenizer(prompt_text, return_tensors="pt").to(device)
    with torch.no_grad():
        generated = model.generate(
            **inputs,
            do_sample=temperature > 0,
            temperature=max(temperature, 1e-5),
            max_new_tokens=max_new_tokens,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )
    completion_ids = generated[0][inputs["input_ids"].shape[1] :]
    text = tokenizer.decode(completion_ids, skip_special_tokens=True)
    return parse_action_json(text)


def summarize(results: list[dict[str, Any]]) -> dict[str, float]:
    return {
        "avg_reward": statistics.fmean(result["total_reward"] for result in results) if results else 0.0,
        "avg_score": statistics.fmean(result["score"] for result in results) if results else 0.0,
        "solved_rate": (
            sum(1 for result in results if result["solved"]) / len(results) if results else 0.0
        ),
        "avg_steps": statistics.fmean(result["steps"] for result in results) if results else 0.0,
    }


def run_policy(
    label: str,
    model: Any,
    tokenizer: Any,
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    samples = build_episode_samples(args.task_scope, args.episodes)
    results: list[dict[str, Any]] = []
    for sample in samples:
        def policy(observation: Any, transcript: list[dict[str, Any]]) -> dict[str, Any]:
            user_prompt = build_turn_prompt(observation, transcript)
            action = generate_action(
                model=model,
                tokenizer=tokenizer,
                user_prompt=user_prompt,
                max_new_tokens=args.max_new_tokens,
                temperature=args.temperature,
            )
            return {"action": action, "metadata": {"text": ""}}

        metrics = run_episode(
            base_url=args.env_url,
            sample=sample,
            max_turns=args.max_turns,
            policy=policy,
        )
        results.append(
            {
                "label": label,
                "task_id": metrics.task_id,
                "scenario_id": metrics.scenario_id,
                "total_reward": metrics.total_reward,
                "score": metrics.score,
                "solved": metrics.solved,
                "steps": metrics.steps,
            }
        )
    return results


def print_table(rows: list[dict[str, Any]]) -> None:
    headers = ("policy", "avg_reward", "avg_score", "solved_rate", "avg_steps")
    line = "{:<10} {:>11} {:>10} {:>12} {:>10}"
    print(line.format(*headers))
    print(line.format(*("-" * len(header) for header in headers)))
    for row in rows:
        print(
            line.format(
                row["policy"],
                f"{row['avg_reward']:.3f}",
                f"{row['avg_score']:.3f}",
                f"{row['solved_rate']:.2%}",
                f"{row['avg_steps']:.2f}",
            )
        )


def main() -> None:
    args = parse_args()

    base_model, base_tokenizer, base_label = load_policy(args.model_id, None)
    rows: list[dict[str, Any]] = []
    base_results = run_policy(base_label, base_model, base_tokenizer, args)
    rows.append({"policy": base_label, **summarize(base_results)})

    if args.checkpoint_path:
        tuned_model, tuned_tokenizer, tuned_label = load_policy(args.model_id, args.checkpoint_path)
        tuned_results = run_policy(tuned_label, tuned_model, tuned_tokenizer, args)
        rows.append({"policy": tuned_label, **summarize(tuned_results)})

    print_table(rows)


if __name__ == "__main__":
    main()
