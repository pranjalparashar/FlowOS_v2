"""Custom Gradio UI for the FlowOS Space."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import gradio as gr

try:
    from ..tasks import get_scenario, get_task
except ImportError:
    from tasks import get_scenario, get_task


def _pretty_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _safe_json(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    return json.loads(text)


def _scenario_ids_for_task(task_id: str) -> list[str]:
    try:
        task = get_task(task_id)
    except Exception:
        return []
    scenario_ids = task.get("scenario_ids") or []
    return [str(sid) for sid in scenario_ids]


def _pipeline_task_choices() -> list[str]:
    candidate_ids = [
        "simulate_csv_report_workflow",
        "simulate_csv_report_curriculum_generate",
        "simulate_csv_report_curriculum_repair",
    ]
    valid: list[str] = []
    for task_id in candidate_ids:
        if _scenario_ids_for_task(task_id):
            valid.append(task_id)
    return valid


def _simulation_fields_for(task_id: str, scenario_id: str) -> dict[str, Any]:
    scenario_ids = _scenario_ids_for_task(task_id)
    if scenario_id not in scenario_ids:
        return {
            "source_csv": "",
            "raw_table": "",
            "final_view": "",
            "validators": [],
        }

    try:
        scenario_index = scenario_ids.index(scenario_id)
        scenario = get_scenario(task_id, scenario_index)
    except Exception:
        return {
            "source_csv": "",
            "raw_table": "",
            "final_view": "",
            "validators": [],
        }

    target = scenario.get("simulation_target", {})
    return {
        "source_csv": str(target.get("source_csv", "")),
        "raw_table": str(target.get("expected_raw_table", {}).get("name", "")),
        "final_view": str(target.get("expected_final_view", {}).get("name", "")),
        "validators": [str(v) for v in scenario.get("available_validators", [])],
    }


def _format_runtime_debug(observation: dict[str, Any]) -> tuple[str, str]:
    if not observation:
        return "Runtime status: idle", "No pipeline execution yet."

    runtime_status = observation.get("runtime_status") or {}
    logs = observation.get("execution_logs") or []
    step_count = observation.get("step_count", 0)
    last_error = observation.get("last_action_error")

    status_parts = [f"**Step:** {step_count}"]
    if runtime_status:
        status_parts.append(f"**Runtime:** `{json.dumps(runtime_status, ensure_ascii=False)}`")
    else:
        status_parts.append("**Runtime:** idle")
    if last_error:
        status_parts.append(f"**Error:** `{last_error}`")
    runtime_md = " | ".join(status_parts)

    if not logs:
        log_md = "No pipeline execution yet."
    else:
        rendered = [f"{idx + 1}. {line}" for idx, line in enumerate(logs)]
        log_md = "\n".join(rendered[-25:])
    return runtime_md, log_md


def _dropdown_update(value: str, choices: list[str]) -> dict[str, Any]:
    normalized_choices = [str(choice) for choice in (choices or []) if str(choice)]
    normalized_value = str(value or "")
    if normalized_value and normalized_value not in normalized_choices:
        normalized_choices = [normalized_value, *normalized_choices]
    if not normalized_choices:
        normalized_choices = [""]
        normalized_value = ""
    elif not normalized_value:
        normalized_value = normalized_choices[0]
    return gr.update(choices=normalized_choices, value=normalized_value)


def build_developer_control_room_ui(
    web_manager: Any,
    action_fields: List[Dict[str, Any]],
    metadata: Optional[Any],
    is_chat_env: bool,
    title: str = "FlowOS",
    quick_start_md: Optional[str] = None,
) -> gr.Blocks:
    del action_fields, is_chat_env

    display_title = title
    pipeline_task_ids = _pipeline_task_choices()
    if not pipeline_task_ids:
        pipeline_task_ids = ["simulate_csv_report_workflow"]
    default_task_id = (
        "simulate_csv_report_workflow"
        if "simulate_csv_report_workflow" in pipeline_task_ids
        else (pipeline_task_ids[0] if pipeline_task_ids else "simulate_csv_report_workflow")
    )
    default_scenarios = _scenario_ids_for_task(default_task_id)
    default_scenario_id = default_scenarios[0] if default_scenarios else ""
    default_fields = _simulation_fields_for(default_task_id, default_scenario_id)

    css = """
    :root {
      --bg: #09111f;
      --bg-2: #0f1728;
      --surface: rgba(14, 23, 39, 0.62);
      --surface-strong: rgba(17, 24, 39, 0.88);
      --line: rgba(148, 163, 184, 0.18);
      --text: #eef4ff;
      --muted: #9db0c7;
      --cyan: #6ee7f9;
      --blue: #60a5fa;
      --coral: #fb7185;
      --glow: rgba(110, 231, 249, 0.18);
    }
    body, .gradio-container {
      background:
        radial-gradient(circle at 15% 20%, rgba(96, 165, 250, 0.18), transparent 28%),
        radial-gradient(circle at 85% 12%, rgba(251, 113, 133, 0.16), transparent 24%),
        linear-gradient(180deg, var(--bg), var(--bg-2)) !important;
      color: var(--text);
    }
    .gradio-container { max-width: 1400px !important; padding-top: 18px !important; }
    .dcr-hero {
      padding: 28px 30px 18px 30px;
      border: 1px solid var(--line);
      border-radius: 28px;
      background: linear-gradient(135deg, rgba(15, 23, 40, 0.92), rgba(12, 18, 31, 0.72));
      box-shadow: 0 20px 80px rgba(0, 0, 0, 0.28);
      backdrop-filter: blur(14px);
      margin-bottom: 18px;
    }
    .dcr-kicker {
      display: inline-flex;
      gap: 10px;
      align-items: center;
      color: var(--cyan);
      font-size: 12px;
      letter-spacing: 0.22em;
      text-transform: uppercase;
      margin-bottom: 12px;
    }
    .dcr-title {
      font-size: 42px;
      line-height: 1.02;
      font-weight: 760;
      letter-spacing: -0.03em;
      margin: 0;
    }
    .dcr-subtitle {
      max-width: 760px;
      color: var(--muted);
      margin-top: 12px;
      font-size: 16px;
      line-height: 1.65;
    }
    .dcr-panel {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 18px 18px 10px 18px;
      backdrop-filter: blur(10px);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.03);
    }
    .dcr-panel-title {
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.16em;
      color: var(--cyan);
      margin-bottom: 8px;
    }
    .dcr-side-note {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.6;
      margin-top: 4px;
      margin-bottom: 10px;
    }
    .dcr-status {
      padding: 14px 16px;
      border-radius: 18px;
      background: rgba(96, 165, 250, 0.09);
      border: 1px solid rgba(96, 165, 250, 0.16);
    }
    .dcr-chip-row {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      justify-content: flex-end;
    }
    .dcr-chip {
      display: inline-block;
      padding: 6px 12px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.04);
      color: var(--text);
      font-size: 12px;
    }
    .dcr-chip strong { color: var(--cyan); font-weight: 600; }
    .dcr-btn-primary button {
      background: linear-gradient(135deg, var(--cyan), var(--blue));
      border: none !important;
      color: #06111f !important;
      font-weight: 700;
      box-shadow: 0 10px 30px var(--glow);
    }
    .dcr-btn-secondary button {
      background: rgba(255, 255, 255, 0.03) !important;
      border: 1px solid var(--line) !important;
      color: var(--text) !important;
    }
    .dcr-btn-secondary button:hover,
    .dcr-btn-primary button:hover {
      filter: brightness(1.05);
    }
    .dcr-json, .dcr-json textarea, .dcr-json pre {
      font-size: 12px !important;
    }
    .block, .gr-group, .gr-box, .gr-panel {
      border-radius: 20px !important;
    }
    """

    async def reset_env(task_id: str, scenario_index: int):
        try:
            data = await web_manager.reset_environment(
                {"task_id": task_id, "scenario_index": scenario_index}
            )
            return (
                _pretty_json(data.get("observation", {})),
                _pretty_json(data),
                "Reset complete.",
            )
        except Exception as exc:
            return ("", "", f"Reset error: {exc}")

    async def step_env(action_type: str, params_json: str):
        try:
            parameters = _safe_json(params_json)
        except Exception as exc:
            return ("", "", f"Invalid JSON: {exc}")

        payload = {"action_type": action_type, "parameters": parameters}
        try:
            data = await web_manager.step_environment(payload)
            return (
                _pretty_json(data.get("observation", {})),
                _pretty_json(data),
                "Step complete.",
            )
        except Exception as exc:
            return ("", "", f"Step error: {exc}")

    def get_state_sync():
        try:
            state = web_manager.get_state()
            runtime_md, log_md = _format_runtime_debug(state)
            return _pretty_json(state), runtime_md, log_md, "State refreshed."
        except Exception as exc:
            return f"State error: {exc}", "Runtime status: unavailable", f"State error: {exc}", f"State error: {exc}"

    def on_task_change(task_id: str):
        scenario_ids = _scenario_ids_for_task(task_id)
        scenario_value = scenario_ids[0] if scenario_ids else ""
        fields = _simulation_fields_for(task_id, scenario_value)
        validators = fields["validators"] or []
        return (
            _dropdown_update(scenario_value, scenario_ids),
            _dropdown_update(fields["source_csv"], [fields["source_csv"]]),
            _dropdown_update(fields["raw_table"], [fields["raw_table"]]),
            _dropdown_update(fields["final_view"], [fields["final_view"]]),
            _dropdown_update(validators[0] if validators else "", validators),
        )

    def on_scenario_change(task_id: str, scenario_id: str):
        fields = _simulation_fields_for(task_id, scenario_id)
        validators = fields["validators"] or []
        return (
            _dropdown_update(fields["source_csv"], [fields["source_csv"]]),
            _dropdown_update(fields["raw_table"], [fields["raw_table"]]),
            _dropdown_update(fields["final_view"], [fields["final_view"]]),
            _dropdown_update(validators[0] if validators else "", validators),
        )

    async def reset_from_preset(task_id: str, scenario_id: str):
        scenario_ids = _scenario_ids_for_task(task_id)
        scenario_index = scenario_ids.index(scenario_id) if scenario_id in scenario_ids else 0
        obs_json, raw, status_text = await reset_env(task_id, scenario_index)
        try:
            observation = _safe_json(obs_json)
        except Exception:
            observation = {}
        runtime_md, log_md = _format_runtime_debug(observation)
        return obs_json, raw, status_text, runtime_md, log_md

    async def step_with_debug(action_type: str, params_json: str):
        obs_json, raw, status_text = await step_env(action_type, params_json)
        try:
            observation = _safe_json(obs_json)
        except Exception:
            observation = {}
        runtime_md, log_md = _format_runtime_debug(observation)
        return obs_json, raw, status_text, runtime_md, log_md

    with gr.Blocks(title=display_title) as demo:
        gr.HTML(f"<style>{css}</style>")
        gr.HTML(
            f"""
            <section class="dcr-hero">
              <div class="dcr-kicker">FlowOS / Interactive Workspace / End-to-End Ops</div>
              <div class="dcr-title">flowOS</div>
              <div class="dcr-subtitle">
                Explore repair, review, and shipping in one live operating environment.
                FlowOS is built to evaluate how AI agents move real work from broken to shipped.
              </div>
            </section>
            """
        )

        with gr.Row():
            with gr.Column(scale=3):
                gr.HTML("<div class='dcr-chip-row'><span class='dcr-chip'><strong>Port</strong> 7860</span><span class='dcr-chip'><strong>Mode</strong> Single-page UI</span></div>")
                with gr.Group(elem_classes="dcr-panel"):
                    gr.HTML("<div class='dcr-panel-title'>Create Pipeline</div>")
                    gr.HTML("<div class='dcr-side-note'>Select a pipeline task and scenario. Downstream fields are preset from scenario metadata.</div>")
                    task_id = gr.Dropdown(
                        choices=pipeline_task_ids,
                        value=default_task_id,
                        label="Task",
                    )
                    scenario_id = gr.Dropdown(
                        choices=default_scenarios,
                        value=default_scenario_id,
                        label="Scenario",
                    )
                    source_csv = gr.Dropdown(
                        choices=[default_fields["source_csv"]] if default_fields["source_csv"] else [""],
                        value=default_fields["source_csv"] or "",
                        label="Source CSV (preset)",
                        interactive=False,
                    )
                    raw_table = gr.Dropdown(
                        choices=[default_fields["raw_table"]] if default_fields["raw_table"] else [""],
                        value=default_fields["raw_table"] or "",
                        label="Raw Table (preset)",
                        interactive=False,
                    )
                    final_view = gr.Dropdown(
                        choices=[default_fields["final_view"]] if default_fields["final_view"] else [""],
                        value=default_fields["final_view"] or "",
                        label="Final View (preset)",
                        interactive=False,
                    )
                    validator_profile = gr.Dropdown(
                        choices=default_fields["validators"] or [""],
                        value=(default_fields["validators"][0] if default_fields["validators"] else ""),
                        label="Validator Profile (preset)",
                        interactive=False,
                    )
                    reset_btn = gr.Button("Reset Episode", elem_classes="dcr-btn-secondary")

                with gr.Group(elem_classes="dcr-panel"):
                    gr.HTML("<div class='dcr-panel-title'>Action Composer</div>")
                    gr.HTML("<div class='dcr-side-note'>Use exact action names and JSON params. This is intentionally close to the raw FlowOS benchmark surface.</div>")
                    action_type = gr.Dropdown(
                        choices=[
                            "search_workspace",
                            "read_file",
                            "inspect_schema",
                            "inspect_lineage",
                            "inspect_llm_draft",
                            "edit_file",
                            "run_validator",
                            "submit_repair",
                            "submit_review",
                            "submit_workspace",
                        ],
                        value="read_file",
                        label="Action Type",
                    )
                    params_json = gr.Code(
                        value='{"path":"transforms/orders_daily.sql"}',
                        label="Action Parameters (JSON)",
                        language="json",
                        elem_classes="dcr-json",
                    )
                    with gr.Row():
                        step_btn = gr.Button("Send Action", elem_classes="dcr-btn-primary")
                        state_btn = gr.Button("Get State", elem_classes="dcr-btn-secondary")

                with gr.Group(elem_classes="dcr-status"):
                    status = gr.Textbox(label="Status", interactive=False, container=False)
                with gr.Group(elem_classes="dcr-panel"):
                    gr.HTML("<div class='dcr-panel-title'>Pipeline Status / Debug Steps</div>")
                    pipeline_runtime = gr.Markdown("Runtime status: idle")
                    pipeline_steps = gr.Markdown("No pipeline execution yet.")

            with gr.Column(scale=4):
                with gr.Group(elem_classes="dcr-panel"):
                    gr.HTML("<div class='dcr-panel-title'>Observation</div>")
                    obs_display = gr.Code(label="Observation", language="json", value="")
                with gr.Group(elem_classes="dcr-panel"):
                    gr.HTML("<div class='dcr-panel-title'>Raw JSON</div>")
                    raw_json = gr.Code(label="Raw Response", language="json", value="")
                if quick_start_md:
                    with gr.Group(elem_classes="dcr-panel"):
                        gr.HTML("<div class='dcr-panel-title'>Quick Start</div>")
                        gr.Markdown(quick_start_md)

        reset_btn.click(
            fn=reset_from_preset,
            inputs=[task_id, scenario_id],
            outputs=[obs_display, raw_json, status, pipeline_runtime, pipeline_steps],
        )
        step_btn.click(
            fn=step_with_debug,
            inputs=[action_type, params_json],
            outputs=[obs_display, raw_json, status, pipeline_runtime, pipeline_steps],
        )
        state_btn.click(
            fn=get_state_sync,
            outputs=[raw_json, pipeline_runtime, pipeline_steps, status],
        )
        task_id.change(
            fn=on_task_change,
            inputs=[task_id],
            outputs=[scenario_id, source_csv, raw_table, final_view, validator_profile],
        )
        scenario_id.change(
            fn=on_scenario_change,
            inputs=[task_id, scenario_id],
            outputs=[source_csv, raw_table, final_view, validator_profile],
        )

    return demo
