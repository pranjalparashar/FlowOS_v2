"""Minimal GRPO training entrypoint for FlowOS."""

from __future__ import annotations

import argparse
import csv
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .baseline import action_is_valid, fallback_action
    from .training_utils import (
        DEFAULT_MAX_NEW_TOKENS,
        DEFAULT_MAX_TURNS,
        DEFAULT_TRAIN_MODEL,
        TRAIN_SYSTEM_PROMPT,
        build_episode_samples,
        build_turn_prompt,
        parse_action_json,
        parse_sample_prompt,
        persist_episode_artifacts,
        run_episode,
        samples_to_dataset_prompts,
    )
except ImportError:
    from baseline import action_is_valid, fallback_action
    from training_utils import (
        DEFAULT_MAX_NEW_TOKENS,
        DEFAULT_MAX_TURNS,
        DEFAULT_TRAIN_MODEL,
        TRAIN_SYSTEM_PROMPT,
        build_episode_samples,
        build_turn_prompt,
        parse_action_json,
        parse_sample_prompt,
        persist_episode_artifacts,
        run_episode,
        samples_to_dataset_prompts,
    )

logger = logging.getLogger(__name__)
DEBUG_GENERATION_LOG_LIMIT = 8


def _clean_preview(text: str, limit: int = 220) -> str:
    normalized = text.replace("\u00a0", " ")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return "<empty>"
    if len(normalized) > limit:
        return normalized[:limit] + "..."
    return normalized


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal Colab-first GRPO training for FlowOS")
    parser.add_argument("--model-id", default=DEFAULT_TRAIN_MODEL, help="Base instruct model to fine-tune")
    parser.add_argument("--env-url", default="http://localhost:7860", help="FlowOS OpenEnv server URL")
    parser.add_argument("--dataset-size", type=int, default=24, help="Number of training episode prompts")
    parser.add_argument("--num-generations", type=int, default=4, help="GRPO generations per prompt")
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS, help="Max environment turns per episode")
    parser.add_argument("--max-new-tokens", type=int, default=DEFAULT_MAX_NEW_TOKENS, help="Max tokens per action generation")
    parser.add_argument("--learning-rate", type=float, default=2e-6, help="Trainer learning rate")
    parser.add_argument("--output-dir", default=None, help="Checkpoint output directory")
    parser.add_argument("--task-scope", default="all", help="Task scope: all or comma-separated task ids")
    parser.add_argument("--report-to", default="none", choices=("none", "tensorboard", "wandb"), help="Logging backend")
    parser.add_argument("--num-epochs", type=int, default=1, help="Number of GRPO epochs")
    parser.add_argument("--max-steps", type=int, default=-1, help="Max trainer steps; -1 lets TRL infer from epochs")
    parser.add_argument("--gradient-accumulation-steps", type=int, default=4, help="Gradient accumulation")
    parser.add_argument("--per-device-train-batch-size", type=int, default=1, help="Per-device batch size")
    parser.add_argument("--save-steps", type=int, default=10, help="Checkpoint save interval")
    parser.add_argument("--logging-steps", type=int, default=1, help="Logging interval")
    parser.add_argument("--temperature", type=float, default=0.7, help="Sampling temperature")
    parser.add_argument("--lora-r", type=int, default=16, help="LoRA rank")
    parser.add_argument("--lora-alpha", type=int, default=32, help="LoRA alpha")
    parser.add_argument("--lora-dropout", type=float, default=0.05, help="LoRA dropout")
    parser.add_argument("--reward-log", default="reward_log.csv", help="CSV filename for episode rewards")
    parser.add_argument("--upload-repo-id", default=None, help="Optional Hugging Face repo id for uploading outputs")
    parser.add_argument(
        "--upload-repo-type",
        default="dataset",
        choices=("dataset", "model"),
        help="Repo type for --upload-repo-id",
    )
    parser.add_argument(
        "--upload-path-in-repo",
        default=None,
        help="Optional path prefix inside the upload repo (defaults to output dir name)",
    )
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


def generate_with_trainer(
    trainer: Any,
    tokenizer: Any,
    prompt_text: str,
    max_new_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    try:
        from trl.experimental.openenv import generate_rollout_completions

        episode = generate_rollout_completions(trainer, [prompt_text], max_new_tokens=max_new_tokens)[0]
        return {
            "prompt_ids": list(episode.get("prompt_ids", [])),
            "completion_ids": list(episode.get("completion_ids", [])),
            "logprobs": list(episode.get("logprobs", [])),
            "text": episode.get("text") or tokenizer.decode(episode.get("completion_ids", []), skip_special_tokens=True),
        }
    except Exception:
        pass

    import torch

    model = getattr(trainer, "model_wrapped", None) or getattr(trainer, "model", None)
    if model is None:
        raise RuntimeError("Could not access trainer model for fallback generation.")

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

    prompt_ids = inputs["input_ids"][0].tolist()
    full_ids = generated[0]
    completion_ids = full_ids[len(prompt_ids) :].tolist()

    with torch.no_grad():
        logits = model(full_ids.unsqueeze(0)).logits[0]
        token_logprobs: list[float] = []
        for offset, token_id in enumerate(completion_ids):
            logit_index = len(prompt_ids) - 1 + offset
            distribution = torch.log_softmax(logits[logit_index], dim=-1)
            token_logprobs.append(float(distribution[token_id].item()))

    return {
        "prompt_ids": prompt_ids,
        "completion_ids": completion_ids,
        "logprobs": token_logprobs,
        "text": tokenizer.decode(completion_ids, skip_special_tokens=True),
    }


def reward_total(completions: list[str], **kwargs: Any) -> list[float]:
    rewards = kwargs.get("total_reward", [])
    return [float(value) for value in rewards] if rewards else [0.0] * len(completions)


def reward_score(completions: list[str], **kwargs: Any) -> list[float]:
    rewards = kwargs.get("score_reward", [])
    return [float(value) for value in rewards] if rewards else [0.0] * len(completions)


def reward_solved(completions: list[str], **kwargs: Any) -> list[float]:
    solved = kwargs.get("solved_reward", [])
    return [float(value) for value in solved] if solved else [0.0] * len(completions)


def upload_outputs(output_dir: Path, repo_id: str, repo_type: str, path_in_repo: str | None) -> None:
    from huggingface_hub import HfApi

    api = HfApi()
    api.create_repo(repo_id=repo_id, repo_type=repo_type, exist_ok=True)
    api.upload_folder(
        repo_id=repo_id,
        repo_type=repo_type,
        folder_path=str(output_dir),
        path_in_repo=path_in_repo or output_dir.name,
    )


def main() -> None:
    args = parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("huggingface_hub").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        from datasets import Dataset
        from peft import LoraConfig
        from transformers import AutoTokenizer
        from trl import GRPOConfig, GRPOTrainer
    except ImportError as exc:
        raise SystemExit(
            "Training dependencies are missing. Install them with `pip install -e \".[train]\"`."
        ) from exc

    samples = build_episode_samples(args.task_scope, args.dataset_size)
    dataset = Dataset.from_dict({"prompt": samples_to_dataset_prompts(samples)})

    tokenizer = AutoTokenizer.from_pretrained(args.model_id)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    output_dir = Path(args.output_dir or Path("outputs") / f"flowos-grpo-{timestamp}")
    output_dir.mkdir(parents=True, exist_ok=True)

    reward_log_path = output_dir / args.reward_log
    artifact_output_dir = output_dir / "artifacts" / "sim_runs"
    with reward_log_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["episode", "task_id", "scenario_id", "total_reward", "score", "solved", "steps"])

    episode_counter = [0]
    generation_debug_counter = [0]

    def log_episode(metrics: Any) -> None:
        episode_counter[0] += 1
        with reward_log_path.open("a", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    episode_counter[0],
                    metrics.task_id,
                    metrics.scenario_id,
                    f"{metrics.total_reward:.4f}",
                    f"{metrics.score:.4f}",
                    str(bool(metrics.solved)).lower(),
                    metrics.steps,
                ]
            )
        logger.info(
            "Episode %s task=%s scenario=%s reward=%.3f score=%.3f solved=%s steps=%s",
            episode_counter[0],
            metrics.task_id,
            metrics.scenario_id,
            metrics.total_reward,
            metrics.score,
            metrics.solved,
            metrics.steps,
        )
        artifact_dir = persist_episode_artifacts(artifact_output_dir, metrics)
        if artifact_dir is not None:
            logger.info("Saved episode artifacts to %s", artifact_dir)

    def rollout_func(prompts: list[str], trainer: Any) -> dict[str, list]:
        prompt_batches: list[list[int]] = []
        completion_batches: list[list[int]] = []
        logprob_batches: list[list[float]] = []
        total_rewards: list[float] = []
        score_rewards: list[float] = []
        solved_rewards: list[float] = []

        for prompt in prompts:
            sample = parse_sample_prompt(prompt)

            def policy(observation: Any, transcript: list[dict[str, Any]]) -> dict[str, Any]:
                user_prompt = build_turn_prompt(observation, transcript)
                prompt_text = apply_chat_template(
                    tokenizer,
                    [
                        {"role": "system", "content": TRAIN_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                )
                generation = generate_with_trainer(
                    trainer=trainer,
                    tokenizer=tokenizer,
                    prompt_text=prompt_text,
                    max_new_tokens=args.max_new_tokens,
                    temperature=args.temperature,
                )
                parsed_action = parse_action_json(generation["text"])
                if generation_debug_counter[0] < DEBUG_GENERATION_LOG_LIMIT:
                    logger.info(
                        "Raw rollout completion %s task=%s step=%s text=%s parsed=%s",
                        generation_debug_counter[0] + 1,
                        sample.task_id,
                        observation.step_count + 1,
                        _clean_preview(generation["text"]),
                        parsed_action,
                    )
                    generation_debug_counter[0] += 1
                if not action_is_valid(parsed_action, observation):
                    fallback = fallback_action(sample.task_id, observation)
                    logger.info(
                        "Using fallback action task=%s step=%s fallback=%s",
                        sample.task_id,
                        observation.step_count + 1,
                        fallback,
                    )
                    parsed_action = fallback
                return {
                    "action": parsed_action,
                    "metadata": generation,
                }

            metrics = run_episode(
                base_url=args.env_url,
                sample=sample,
                max_turns=args.max_turns,
                policy=policy,
            )

            prompt_batches.append(metrics.prompt_ids or [])
            completion_batches.append(metrics.completion_ids or [])
            logprob_batches.append(metrics.logprobs or [])
            total_rewards.append(metrics.total_reward)
            score_rewards.append(metrics.score)
            solved_rewards.append(1.0 if metrics.solved else 0.0)
            log_episode(metrics)

        return {
            "prompt_ids": prompt_batches,
            "completion_ids": completion_batches,
            "logprobs": logprob_batches,
            "total_reward": total_rewards,
            "score_reward": score_rewards,
            "solved_reward": solved_rewards,
        }

    peft_config = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    )

    grpo_config = GRPOConfig(
        output_dir=str(output_dir),
        use_vllm=False,
        learning_rate=args.learning_rate,
        num_train_epochs=args.num_epochs,
        max_steps=args.max_steps,
        per_device_train_batch_size=args.per_device_train_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        num_generations=args.num_generations,
        generation_batch_size=args.num_generations,
        max_completion_length=args.max_new_tokens,
        logging_steps=args.logging_steps,
        save_strategy="steps",
        save_steps=args.save_steps,
        report_to=args.report_to,
        temperature=args.temperature,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
    )

    trainer = GRPOTrainer(
        model=args.model_id,
        processing_class=tokenizer,
        reward_funcs=[reward_total, reward_score, reward_solved],
        train_dataset=dataset,
        args=grpo_config,
        rollout_func=rollout_func,
        peft_config=peft_config,
    )

    logger.info("Starting FlowOS GRPO training")
    logger.info("model=%s env=%s dataset_size=%s task_scope=%s", args.model_id, args.env_url, args.dataset_size, args.task_scope)
    try:
        trainer.train()
    finally:
        try:
            try:
                from .plot_rewards import plot as plot_reward_curve
            except ImportError:
                from plot_rewards import plot as plot_reward_curve
            plot_reward_curve(reward_log_path, output_dir / "reward_curve.png")
            logger.info("Reward curve written to %s", output_dir / "reward_curve.png")
        except Exception as exc:  # pragma: no cover - plotting should be best effort
            logger.warning("Could not generate reward curve: %s", exc)

    trainer.save_model(str(output_dir))
    logger.info("Training finished. Model saved to %s", output_dir)
    logger.info("Reward log written to %s", reward_log_path)

    if args.upload_repo_id:
        try:
            upload_outputs(
                output_dir=output_dir,
                repo_id=args.upload_repo_id,
                repo_type=args.upload_repo_type,
                path_in_repo=args.upload_path_in_repo,
            )
            logger.info(
                "Uploaded outputs to https://huggingface.co/%s/%s",
                args.upload_repo_type + "s" if args.upload_repo_type != "model" else "",
                args.upload_repo_id,
            )
        except Exception as exc:  # pragma: no cover - depends on external auth/network
            logger.warning("Could not upload outputs: %s", exc)


if __name__ == "__main__":
    main()
