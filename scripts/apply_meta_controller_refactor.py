from __future__ import annotations

from pathlib import Path


POLICY = Path("src/control_plane/alert_policy.py")
INIT = Path("src/control_plane/__init__.py")
TEST = Path("tests/test_meta_policy_integration.py")
META_POLICY = Path("src/control_plane/meta_policy.py")


def remove_select_method(text: str, class_name: str, end_marker: str) -> str:
    class_start = text.index(f"class {class_name}")
    method_start = text.index(
        "\n    async def select(self, state: ControllerState) -> ControllerDecision:\n",
        class_start,
    )
    method_end = text.index(end_marker, method_start)
    return text[:method_start] + text[method_end:]


def main() -> None:
    text = POLICY.read_text(encoding="utf-8")

    if "from meta_controller import StagedMetaPolicy\n" not in text:
        text = text.replace(
            "from portable_runtime.controller import (\n",
            "from meta_controller import StagedMetaPolicy\nfrom portable_runtime.controller import (\n",
            1,
        )

    text = text.replace(
        "class AutonomousRepairPolicy:\n",
        "class AutonomousRepairPolicy(StagedMetaPolicy):\n",
        1,
    )
    text = text.replace(
        "class ManualTaskPolicy:\n",
        "class ManualTaskPolicy(StagedMetaPolicy):\n",
        1,
    )

    helper_anchor = """def _latest_run(runtime: Any, work_id: str) -> Any | None:\n    runs = runtime.store.list_runs(work_id)\n    if not runs:\n        return None\n    return max(runs, key=lambda run: (run.started_at or run.created_at, run.created_at))\n"""
    helper = helper_anchor + """\n\ndef _recent_reality_context(bridge: PersonalKernelBridge, controller_id: str) -> str:\n    \"\"\"Project prior Work reality into a renewed diagnosis without claiming truth.\"\"\"\n\n    events = bridge.result_events(controller_id)\n    if not events:\n        return \"\"\n    parts: list[str] = []\n    for event in events[-6:]:\n        stage = str(event.payload.get(\"stage\", \"observation\"))\n        result = _event_result(event)\n        status = str(result.get(\"status\", \"unknown\"))\n        message = str(result.get(\"message\", \"\"))[-1500:]\n        parts.append(f\"[{stage}] status={status}\\n{message}\")\n        metadata = result.get(\"metadata\")\n        if isinstance(metadata, dict):\n            bounded = {\n                key: value\n                for key, value in metadata.items()\n                if key in {\"active\", \"blocker\", \"attempt_index\", \"capability\"}\n            }\n            if bounded:\n                parts.append(f\"[{stage}] metadata={bounded}\")\n    return \"\\n\\n\".join(parts)[-6000:]\n"""
    if "def _recent_reality_context(" not in text:
        text = text.replace(helper_anchor, helper, 1)

    attempt_anchor = """    @property\n    def attempt_limit(self) -> int:\n        \"\"\"Hard ceiling for alert repair: at most two diagnosis/execution rounds.\"\"\"\n\n        return _AUTONOMOUS_ATTEMPT_LIMIT\n"""
    accept_method = attempt_anchor + """\n    def _accept_failed_diagnosis_as_unknown(self) -> bool:\n        # Preserve the profile's fail-closed behavior: a malformed/failed diagnosis\n        # may close only as UNKNOWN/read-only and can never open effect authority.\n        return True\n"""
    if "def _accept_failed_diagnosis_as_unknown" not in text:
        text = text.replace(attempt_anchor, accept_method, 1)

    diagnosis_start = text.index(
        "    def _diagnosis(self, state: ControllerState, *, retry_context: str = \"\")"
    )
    attempt_line = "        attempt = self._diagnosis_count(state) + 1\n"
    attempt_pos = text.index(attempt_line, diagnosis_start)
    if "_recent_reality_context(self.bridge, state.id)" not in text[diagnosis_start:attempt_pos + 500]:
        replacement = attempt_line + """        if not retry_context and attempt > 1:\n            retry_context = _recent_reality_context(self.bridge, state.id)\n"""
        text = text[:attempt_pos] + text[attempt_pos:].replace(attempt_line, replacement, 1)

    diagnosis_start = text.index(
        "    def _diagnosis(self, state: ControllerState, *, retry_context: str = \"\")"
    )
    parameters_pos = text.index("        parameters: dict[str, Any] = {\n", diagnosis_start)
    hint_block = """        tags = [\"failure-localization\", \"proxy-observation\"]\n        if attempt > 1:\n            tags.append(\"retry\")\n        if self._is_line_ending_cleanup():\n            tags.append(\"representation-mismatch\")\n        hints = self.experience_hints(state, *tags)\n        if hints:\n            instruction += f\"\\n\\n{hints}\"\n"""
    if "tags = [\"failure-localization\", \"proxy-observation\"]" not in text[diagnosis_start:parameters_pos]:
        text = text[:parameters_pos] + hint_block + text[parameters_pos:]

    manual_start = text.index("class ManualTaskPolicy(StagedMetaPolicy):")
    manual_diagnosis = text.index(
        "    def _diagnosis(self, state: ControllerState) -> ControllerDecision:\n",
        manual_start,
    )
    manual_parameters = text.index("        parameters: dict[str, Any] = {", manual_diagnosis)
    manual_hint = """        hints = self.experience_hints(state, \"failure-localization\")\n        if hints:\n            instruction += f\"\\n\\n{hints}\"\n"""
    if "self.experience_hints(state, \"failure-localization\")" not in text[
        manual_diagnosis:manual_parameters
    ]:
        text = text[:manual_parameters] + manual_hint + text[manual_parameters:]

    text = remove_select_method(
        text,
        "AutonomousRepairPolicy(StagedMetaPolicy):",
        "\n\n@dataclass(frozen=True, slots=True)\nclass ManualTaskPolicy",
    )
    text = remove_select_method(
        text,
        "ManualTaskPolicy(StagedMetaPolicy):",
        "\n\nasync def drive_policy",
    )

    POLICY.write_text(text, encoding="utf-8")

    INIT.write_text(
        '"""Autonomous personal operations deployment/profile for Agent Kernel."""\n\n'
        '__version__ = "0.5.0"\n',
        encoding="utf-8",
    )

    TEST.write_text(
        """from __future__ import annotations\n\nfrom meta_controller import StagedMetaPolicy\n\nfrom control_plane.alert_policy import AutonomousRepairPolicy, ManualTaskPolicy\n\n\ndef test_control_plane_policies_use_meta_controller_directly() -> None:\n    assert issubclass(AutonomousRepairPolicy, StagedMetaPolicy)\n    assert issubclass(ManualTaskPolicy, StagedMetaPolicy)\n    assert AutonomousRepairPolicy.select is StagedMetaPolicy.select\n    assert ManualTaskPolicy.select is StagedMetaPolicy.select\n""",
        encoding="utf-8",
    )

    if META_POLICY.exists():
        META_POLICY.unlink()


if __name__ == "__main__":
    main()
