"""Supervised fine-tuning entrypoint for FlowOS."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import torch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LoRA SFT training for FlowOS traces")
    parser.add_argument("--dataset-path", default="outputs/sft_traces/traces.jsonl", help="JSONL trace file")
    parser.add_argument("--model-id", default="Qwen/Qwen2.5-1.5B-Instruct", help="Base instruct model")
    parser.add_argument("--output-dir", default="outputs/flowos-sft", help="Checkpoint output directory")
    parser.add_argument("--learning-rate", type=float, default=2e-5, help="Learning rate")
    parser.add_argument("--num-epochs", type=int, default=1, help="Training epochs")
    parser.add_argument("--per-device-train-batch-size", type=int, default=1, help="Per-device batch size")
    parser.add_argument("--gradient-accumulation-steps", type=int, default=4, help="Gradient accumulation")
    parser.add_argument("--logging-steps", type=int, default=1, help="Logging interval")
    parser.add_argument("--save-steps", type=int, default=25, help="Checkpoint save interval")
    parser.add_argument("--max-seq-length", type=int, default=2048, help="Sequence truncation length")
    parser.add_argument("--lora-r", type=int, default=16, help="LoRA rank")
    parser.add_argument("--lora-alpha", type=int, default=32, help="LoRA alpha")
    parser.add_argument("--lora-dropout", type=float, default=0.05, help="LoRA dropout")
    parser.add_argument("--report-to", default="none", choices=("none", "tensorboard", "wandb"), help="Logging backend")
    return parser.parse_args()


def apply_chat_template(tokenizer: Any, messages: list[dict[str, str]], add_generation_prompt: bool) -> str:
    try:
        return tokenizer.apply_chat_template(
            messages,
            add_generation_prompt=add_generation_prompt,
            tokenize=False,
            enable_thinking=False,
        )
    except TypeError:
        return tokenizer.apply_chat_template(
            messages,
            add_generation_prompt=add_generation_prompt,
            tokenize=False,
        )


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


@dataclass
class FlowOSSFTDataset:
    samples: list[dict[str, Any]]
    tokenizer: Any
    max_seq_length: int

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int) -> dict[str, torch.Tensor]:
        sample = self.samples[idx]
        prompt_text = apply_chat_template(
            self.tokenizer,
            [
                {"role": "system", "content": sample["system_prompt"]},
                {"role": "user", "content": sample["user_prompt"]},
            ],
            add_generation_prompt=True,
        )
        full_text = prompt_text + sample["target_action"]

        prompt_ids = self.tokenizer(
            prompt_text,
            truncation=True,
            max_length=self.max_seq_length,
            add_special_tokens=False,
        )["input_ids"]
        full_encoding = self.tokenizer(
            full_text,
            truncation=True,
            max_length=self.max_seq_length,
            add_special_tokens=False,
        )
        input_ids = full_encoding["input_ids"]
        attention_mask = full_encoding["attention_mask"]
        prompt_len = min(len(prompt_ids), len(input_ids))
        labels = [-100] * prompt_len + input_ids[prompt_len:]

        return {
            "input_ids": torch.tensor(input_ids, dtype=torch.long),
            "attention_mask": torch.tensor(attention_mask, dtype=torch.long),
            "labels": torch.tensor(labels, dtype=torch.long),
        }


class SFTCollator:
    def __init__(self, tokenizer: Any) -> None:
        self.tokenizer = tokenizer

    def __call__(self, features: list[dict[str, torch.Tensor]]) -> dict[str, torch.Tensor]:
        pad_token_id = self.tokenizer.pad_token_id
        max_len = max(feature["input_ids"].shape[0] for feature in features)
        batch: dict[str, list[torch.Tensor]] = {"input_ids": [], "attention_mask": [], "labels": []}
        for feature in features:
            pad_len = max_len - feature["input_ids"].shape[0]
            batch["input_ids"].append(
                torch.cat([feature["input_ids"], torch.full((pad_len,), pad_token_id, dtype=torch.long)])
            )
            batch["attention_mask"].append(
                torch.cat([feature["attention_mask"], torch.zeros(pad_len, dtype=torch.long)])
            )
            batch["labels"].append(
                torch.cat([feature["labels"], torch.full((pad_len,), -100, dtype=torch.long)])
            )
        return {key: torch.stack(value) for key, value in batch.items()}


def main() -> None:
    args = parse_args()

    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments

    dataset_path = Path(args.dataset_path)
    rows = load_jsonl(dataset_path)
    if not rows:
        raise SystemExit(f"No traces found at {dataset_path}")

    tokenizer = AutoTokenizer.from_pretrained(args.model_id)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(args.model_id)
    model.config.use_cache = False

    peft_config = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    )
    model = get_peft_model(model, peft_config)

    train_dataset = FlowOSSFTDataset(rows, tokenizer, args.max_seq_length)
    collator = SFTCollator(tokenizer)

    training_args = TrainingArguments(
        output_dir=args.output_dir,
        learning_rate=args.learning_rate,
        num_train_epochs=args.num_epochs,
        per_device_train_batch_size=args.per_device_train_batch_size,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        logging_steps=args.logging_steps,
        save_steps=args.save_steps,
        report_to=[] if args.report_to == "none" else [args.report_to],
        remove_unused_columns=False,
        bf16=torch.cuda.is_available(),
        fp16=False,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        data_collator=collator,
    )

    trainer.train()
    trainer.save_model(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    print(f"Saved SFT model to {args.output_dir}")


if __name__ == "__main__":
    main()
