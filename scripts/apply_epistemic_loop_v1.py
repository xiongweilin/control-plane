from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALERT_POLICY = ROOT / "src" / "control_plane" / "alert_policy.py"
PYPROJECT = ROOT / "pyproject.toml"

OLD_META_SHA = "b9de8d47801f979ec9af62cb80870329957b9335"
NEW_META_SHA = "68cb503e2850b06632c3d2cb2c1bdd56def3aa6b"


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def patch_alert_policy() -> None:
    text = ALERT_POLICY.read_text(encoding="utf-8")
    if "build_repair_epistemic_profile" in text:
        return

    text = replace_once(
        text,
        "from .kernel_bridge import PersonalKernelBridge\n",
        (
            "from .epistemic_profile import (\n"
            "    build_repair_epistemic_profile,\n"
            "    render_meta_control_directive,\n"
            ")\n"
            "from .kernel_bridge import PersonalKernelBridge\n"
        ),
        label="epistemic profile import",
    )

    text = replace_once(
        text,
        (
            "        if not retry_context and attempt > 1:\n"
            "            retry_context = _recent_reality_context(self.bridge, state.id)\n"
            "        instruction = (\n"
        ),
        (
            "        if not retry_context and attempt > 1:\n"
            "            retry_context = _recent_reality_context(self.bridge, state.id)\n"
            "        epistemic_profile = build_repair_epistemic_profile(\n"
            "            controller_ref=state.id,\n"
            "            state_version=state.version,\n"
            "            attempt=attempt,\n"
            "            attempt_limit=self.attempt_limit,\n"
            "            is_line_ending_cleanup=self._is_line_ending_cleanup(),\n"
            "            has_repo=bool(self.repo),\n"
            "            has_project=bool(self.project),\n"
            "            has_maintenance_capability=bool(self.maintenance_capability),\n"
            "            retry_context=retry_context,\n"
            "        )\n"
            "        meta_frame = self.meta_control_frame(\n"
            "            state,\n"
            "            issues=epistemic_profile.issues,\n"
            "            tensions=epistemic_profile.tensions,\n"
            "            candidates=epistemic_profile.candidates,\n"
            "            self_model=epistemic_profile.self_model,\n"
            "            budget=epistemic_profile.budget,\n"
            "            used_redundancy_keys=epistemic_profile.used_redundancy_keys,\n"
            "            basis_refs=epistemic_profile.basis_refs,\n"
            "        )\n"
            "        meta_directive = render_meta_control_directive(meta_frame)\n"
            "        instruction = (\n"
        ),
        label="diagnosis meta frame",
    )

    text = replace_once(
        text,
        (
            "        if retry_context:\n"
            "            instruction += f\"\\n\\nPrevious attempt evidence:\\n{retry_context}\"\n"
            "        if self._is_line_ending_cleanup():\n"
        ),
        (
            "        if retry_context:\n"
            "            instruction += f\"\\n\\nPrevious attempt evidence:\\n{retry_context}\"\n"
            "        if meta_directive:\n"
            "            instruction += f\"\\n\\nEpistemic control directive:\\n{meta_directive}\"\n"
            "        if self._is_line_ending_cleanup():\n"
        ),
        label="diagnosis directive",
    )

    text = replace_once(
        text,
        (
            "            \"attempt_index\": attempt,\n"
            "            \"timeout_seconds\": self.diagnosis_timeout_seconds,\n"
        ),
        (
            "            \"attempt_index\": attempt,\n"
            "            \"meta_intent\": meta_frame.intent.kind.value,\n"
            "            \"meta_candidate_ref\": meta_frame.intent.candidate_ref or \"\",\n"
            "            \"timeout_seconds\": self.diagnosis_timeout_seconds,\n"
        ),
        label="durable meta intent parameters",
    )

    ALERT_POLICY.write_text(text, encoding="utf-8")


def patch_pyproject() -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    if NEW_META_SHA in text:
        return
    if OLD_META_SHA not in text:
        raise RuntimeError("meta-controller dependency pin was not found")
    PYPROJECT.write_text(text.replace(OLD_META_SHA, NEW_META_SHA, 1), encoding="utf-8")


if __name__ == "__main__":
    patch_alert_policy()
    patch_pyproject()
