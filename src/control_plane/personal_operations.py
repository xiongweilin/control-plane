"""Typed personal Git/Docker operations for the private profile.

These operations are deliberately separate from the Codex provider. Codex may
recommend a merge, push, or runtime restart, but only this provider can perform
the corresponding side effect after the portable Runtime has evaluated a
scoped request and AuthorizationGrant.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from portable_runtime.core.capabilities import (
    CapabilityRequest,
    CapabilityResult,
    InvocationContext,
    ProviderDescriptor,
    ProviderHealth,
)

from .config import ControlPlaneConfig
from .gitpush import push_with_ssh_fallback
from .reconciliation import (
    BaselineSnapshot,
    DockerObservationCoordinates,
    DockerOperation,
    DockerPostcondition,
    GitMergeObservationCoordinates,
    GitMergeOperation,
    GitMergePostcondition,
    GitMergeReality,
    GitPushObservationCoordinates,
    GitPushOperation,
    GitPushPostcondition,
    ReconciliationDescriptor,
    ReconciliationDescriptorStore,
    ReconciliationObservation,
    ReconciliationVerdict,
    classify_docker_state,
    classify_git_merge_ancestry,
    classify_git_push_remote_ref,
)
from .tools import CommandExecutor, ToolError


@dataclass(slots=True)
class _OperationJournalEntry:
    """Process-local recovery journal for provider invocations.

    The portable runtime persists the request/attempt identity.  This small
    provider journal keeps the operation-specific parameters needed to query
    reality again.  It is intentionally non-authoritative: reconciliation
    always reads Git/Docker state rather than trusting this record.
    """

    request: CapabilityRequest
    state: str = "running"
    metadata: dict[str, Any] = field(default_factory=dict)


class PersonalOperationsProvider:
    """Execute only explicitly named Git and Docker profile operations."""

    def __init__(
        self,
        config: ControlPlaneConfig,
        executor: CommandExecutor,
        reconciliation_store: ReconciliationDescriptorStore | None = None,
    ) -> None:
        self.config = config
        self.executor = executor
        self.reconciliation_store = reconciliation_store
        self._journal: dict[str, _OperationJournalEntry] = {}
        self._descriptor = ProviderDescriptor(
            id="personal-operations",
            name="Personal Git/Docker Operations",
            version="1.0.0",
            capabilities=["git.merge", "git.push", "git.rollback", "docker.restart", "docker.compose.up"],
            priority=20,
            tags={"personal-profile", "side-effect"},
            effect_semantics="reconcilable",
            side_effect_class="reconcilable",
            reversibility="compensatable",
            provider_family="personal-operations",
            execution_domain="windows-local",
            network_domain="github-docker",
            trust_boundary="control-plane-authorized",
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider_id=self.descriptor.id, available=True, detail="personal operations ready")

    async def invoke(self, request: CapabilityRequest, context: InvocationContext) -> CapabilityResult:
        self._journal[request.id] = _OperationJournalEntry(request=request.model_copy(deep=True))
        try:
            await self._prepare_reconciliation_descriptor(request)
            if request.capability == "git.merge":
                output = await self._git_merge(request)
            elif request.capability == "git.push":
                output = await self._git_push(request)
            elif request.capability == "git.rollback":
                output = await self._git_rollback(request)
            elif request.capability in {"docker.restart", "docker.compose.up"}:
                output = await self._docker(request)
            else:
                return CapabilityResult(
                    request_id=request.id,
                    provider_id=self.descriptor.id,
                    status="unavailable",
                    message=f"unsupported personal operation {request.capability}",
                    error={"code": "UnsupportedCapability"},
                )
            descriptor_result = await self._reconcile_descriptor(request.id)
            if descriptor_result is not None and descriptor_result.status != "succeeded":
                return descriptor_result
            # A restart command plus a healthy postcondition does not prove
            # that this particular restart event occurred.  Keep this
            # capability non-terminal even when no durable reconciliation
            # store is configured (for example, in an isolated provider
            # harness).  Callers must not promote desired-state evidence to
            # event attribution.
            if request.capability == "docker.restart" and self.reconciliation_store is None:
                entry = self._journal[request.id]
                entry.state = "unknown"
                entry.metadata.update(
                    {
                        "event_attribution": "unknown",
                        "event_verified": False,
                        "event_verification_basis": "not-observable",
                    }
                )
                return CapabilityResult(
                    request_id=request.id,
                    provider_id=self.descriptor.id,
                    status="unknown",
                    message="Docker desired state confirmed; restart event attribution remains unknown",
                    metadata={
                        "operation": request.capability,
                        "resource_ref": request.resource_ref or "",
                        **entry.metadata,
                    },
                    reconciled=False,
                )
        except ToolError as exc:
            entry = self._journal[request.id]
            ambiguous = self._is_ambiguous_error(exc)
            entry.state = "unknown" if ambiguous else "failed"
            entry.metadata.setdefault("error", str(exc))
            return CapabilityResult(
                request_id=request.id,
                provider_id=self.descriptor.id,
                status="unknown" if ambiguous else "failed",
                message=str(exc),
                error={
                    "code": "PersonalOperationUncertain" if ambiguous else "PersonalOperationFailed",
                    "reason": str(exc),
                },
                metadata={
                    "operation": request.capability,
                    "resource_ref": request.resource_ref or "",
                    **entry.metadata,
                },
            )
        entry = self._journal[request.id]
        entry.state = "succeeded"
        return CapabilityResult(
            request_id=request.id,
            provider_id=self.descriptor.id,
            status="succeeded",
            message=output,
            metadata={
                "operation": request.capability,
                "resource_ref": request.resource_ref or "",
                **entry.metadata,
            },
        )

    async def _prepare_reconciliation_descriptor(self, request: CapabilityRequest) -> None:
        """Persist operation coordinates before a host or remote side effect."""

        store = self.reconciliation_store
        if store is None or request.capability == "git.rollback":
            return
        descriptor: ReconciliationDescriptor | None = None
        if request.capability == "git.merge":
            repo = self._required(request, "repo")
            candidate_ref = self._required(request, "branch")
            target_ref = str(request.parameters.get("target", "main"))
            baseline_merge = (
                await self.executor.run(["git", "-C", repo, "rev-parse", target_ref], timeout=60)
            ).strip()
            candidate = (await self.executor.run(["git", "-C", repo, "rev-parse", candidate_ref], timeout=60)).strip()
            merge_operation = GitMergeOperation(
                repo=repo,
                target_ref=target_ref,
                candidate_ref=candidate_ref,
                candidate_commit=candidate,
                target_baseline_commit=baseline_merge,
            )
            descriptor = ReconciliationDescriptor.from_request(
                descriptor_id=f"recon_{request.id}",
                request=request,
                provider_id=self.descriptor.id,
                provider_version=self.descriptor.version,
                operation=merge_operation,
                pre_effect_baseline=BaselineSnapshot(values={"target_tip": baseline_merge, "merge_head": None}),
                expected_postcondition=GitMergePostcondition(
                    target_ref=target_ref,
                    candidate_commit=candidate,
                    target_baseline_commit=baseline_merge,
                ),
                observation_coordinates=GitMergeObservationCoordinates(
                    repo=repo,
                    target_ref=target_ref,
                    candidate_ref=candidate_ref,
                    merge_head_path=f"{repo}\\.git\\MERGE_HEAD",
                ),
            )
        elif request.capability == "git.push":
            repo = self._required(request, "repo")
            remote = str(request.parameters.get("remote", "origin"))
            branch = str(request.parameters.get("branch", "main"))
            expected = (await self.executor.run(["git", "-C", repo, "rev-parse", branch], timeout=60)).strip()
            baseline_remote = await self._remote_ref(repo, remote, branch)
            push_operation = GitPushOperation(repo=repo, remote=remote, branch=branch, expected_commit=expected)
            descriptor = ReconciliationDescriptor.from_request(
                descriptor_id=f"recon_{request.id}",
                request=request,
                provider_id=self.descriptor.id,
                provider_version=self.descriptor.version,
                operation=push_operation,
                pre_effect_baseline=BaselineSnapshot(values={"remote_commit": baseline_remote}),
                expected_postcondition=GitPushPostcondition(
                    remote=remote, branch=branch, expected_commit=expected
                ),
                observation_coordinates=GitPushObservationCoordinates(
                    repo=repo,
                    remote=remote,
                    branch=branch,
                    remote_ref=f"refs/heads/{branch}",
                ),
            )
        elif request.capability in {"docker.restart", "docker.compose.up"}:
            project = str(request.parameters.get("project", ""))
            if project not in self.config.allowed_auto_projects:
                raise ToolError(f"docker project is not allowlisted: {project}")
            project_dir = self.config.project_dirs.get(project, f"D:\\infrastructure\\compose\\{project}")
            baseline = await self._docker_status(project)
            docker_operation = DockerOperation(
                kind=cast(Literal["docker.restart", "docker.compose.up"], request.capability),
                project=project,
                project_dir=str(project_dir),
                desired_state="healthy",
            )
            descriptor = ReconciliationDescriptor.from_request(
                descriptor_id=f"recon_{request.id}",
                request=request,
                provider_id=self.descriptor.id,
                provider_version=self.descriptor.version,
                operation=docker_operation,
                pre_effect_baseline=BaselineSnapshot(values={"containers": baseline}),
                expected_postcondition=DockerPostcondition(project=project, desired_state="healthy"),
                observation_coordinates=DockerObservationCoordinates(
                    project=project,
                    project_dir=str(project_dir),
                    compose_project_label=project,
                ),
            )
        else:
            return
        if descriptor is None:
            return
        store.save(descriptor)
        self._journal[request.id].metadata["reconciliation_descriptor_id"] = descriptor.id

    async def _reconcile_descriptor(self, request_id: str) -> CapabilityResult | None:
        """Re-observe reality from the durable descriptor, including after restart."""

        store = self.reconciliation_store
        if store is None:
            return None
        descriptor = store.get_by_request(request_id)
        if descriptor is None:
            return None
        details: dict[str, Any] = {}
        if isinstance(descriptor.operation, GitMergeOperation):
            operation = descriptor.operation
            status = await self._git_status(operation.repo)
            merge_head = self._merge_in_progress(status)
            target_tip: str | None
            try:
                target_tip = (
                    await self.executor.run(
                        ["git", "-C", operation.repo, "rev-parse", operation.target_ref], timeout=60
                    )
                ).strip()
            except ToolError:
                target_tip = None
            candidate_is_ancestor: bool | None = None
            if target_tip:
                try:
                    await self.executor.run(
                        [
                            "git",
                            "-C",
                            operation.repo,
                            "merge-base",
                            "--is-ancestor",
                            operation.candidate_commit,
                            operation.target_ref,
                        ],
                        timeout=60,
                    )
                    candidate_is_ancestor = True
                except ToolError:
                    candidate_is_ancestor = False
            verdict = classify_git_merge_ancestry(
                GitMergeReality(
                    target_tip=target_tip,
                    target_baseline_commit=operation.target_baseline_commit,
                    candidate_commit=operation.candidate_commit,
                    candidate_is_ancestor=candidate_is_ancestor,
                    merge_head="MERGE_HEAD" if merge_head else None,
                    conflicts=merge_head,
                )
            )
            details.update(
                {
                    "target_tip": target_tip or "",
                    "candidate_is_ancestor": candidate_is_ancestor,
                    "merge_status": status,
                }
            )
        elif isinstance(descriptor.operation, GitPushOperation):
            push_operation = descriptor.operation
            observed = await self._remote_ref(push_operation.repo, push_operation.remote, push_operation.branch)
            verdict = classify_git_push_remote_ref(
                expected_commit=push_operation.expected_commit,
                observed_commit=observed,
            )
            details.update({"expected_commit": push_operation.expected_commit, "remote_commit": observed or ""})
        elif isinstance(descriptor.operation, DockerOperation):
            docker_operation = descriptor.operation
            status = await self._docker_status(docker_operation.project)
            healthy = self._containers_healthy(status)
            desired_state_verdict = classify_docker_state(
                healthy=healthy, desired_state=docker_operation.desired_state
            )
            # ``classify_docker_state`` intentionally answers only whether
            # the desired state is true.  A restart additionally requires
            # attribution to this request; container health alone cannot
            # distinguish our restart from a pre-existing/recovered state.
            # Until a restart identity (generation/start-time/restart-count)
            # is observed, keep the reconciliation UNKNOWN and therefore
            # non-terminal.
            verdict = (
                ReconciliationVerdict.UNKNOWN
                if docker_operation.kind == "docker.restart"
                else desired_state_verdict
            )
            details.update(
                {
                    "project": docker_operation.project,
                    "container_status": status,
                    "desired_state": docker_operation.desired_state,
                    "desired_state_verified": healthy,
                    "event_attribution": "unknown"
                    if docker_operation.kind == "docker.restart"
                    else "not-applicable",
                    "event_verified": False,
                    "event_verification_basis": "not-observable"
                    if docker_operation.kind == "docker.restart"
                    else "desired-state-operation",
                }
            )
        else:
            return None

        observation = ReconciliationObservation(
            verdict=verdict,
            message=f"{descriptor.capability} reconciliation: {verdict.value}",
            details=details,
        )
        store.record_observation(descriptor.id, observation)
        entry = self._journal.get(request_id)
        if entry is not None:
            entry.state = (
                "succeeded"
                if verdict is ReconciliationVerdict.APPLIED
                else "failed"
                if verdict in {ReconciliationVerdict.NOT_APPLIED, ReconciliationVerdict.MISMATCH}
                else "unknown"
            )
            entry.metadata.update({"reconciliation_verdict": verdict.value, **details})
        result_status = (
            "succeeded"
            if verdict is ReconciliationVerdict.APPLIED
            else "failed"
            if verdict in {ReconciliationVerdict.NOT_APPLIED, ReconciliationVerdict.MISMATCH}
            else "unknown"
        )
        return CapabilityResult(
            request_id=request_id,
            provider_id=self.descriptor.id,
            status=result_status,  # type: ignore[arg-type]
            message=observation.message,
            metadata={"operation": descriptor.capability, "reconciliation_descriptor_id": descriptor.id, **details},
            reconciled=True,
        )

    async def _git_merge(self, request: CapabilityRequest) -> str:
        repo = self._required(request, "repo")
        branch = self._required(request, "branch")
        target = str(request.parameters.get("target", "main"))
        await self.executor.run(["git", "-C", repo, "checkout", "-q", target], timeout=120)
        try:
            output = await self.executor.run(["git", "-C", repo, "merge", "--ff-only", branch], timeout=120)
        except ToolError:
            try:
                output = await self.executor.run(["git", "-C", repo, "merge", "-q", "--no-edit", branch], timeout=120)
            except ToolError as exc:
                # A failed non-FF merge may leave MERGE_HEAD/index state.  An
                # abort is best-effort, but the post-abort status is always
                # captured so callers can distinguish a clean failure from an
                # operation that still needs recovery.
                abort_error = ""
                try:
                    await self.executor.run(["git", "-C", repo, "merge", "--abort"], timeout=120)
                except ToolError as abort_exc:
                    abort_error = str(abort_exc)
                status = await self._git_status(repo)
                self._journal[request.id].metadata.update(
                    {
                        "merge_aborted": not bool(abort_error),
                        "merge_status": status,
                        "merge_abort_error": abort_error,
                    }
                )
                raise ToolError(f"git merge failed and was aborted; status={status}") from exc
        status = await self._git_status(repo)
        if self._merge_in_progress(status):
            self._journal[request.id].metadata.update({"merge_status": status, "merge_in_progress": True})
            raise ToolError(f"git merge returned but merge is still in progress; status={status}")
        self._journal[request.id].metadata.update({"merge_status": status, "merge_in_progress": False})
        return output

    async def _git_push(self, request: CapabilityRequest) -> str:
        repo = self._required(request, "repo")
        remote = str(request.parameters.get("remote", "origin"))
        branch = str(request.parameters.get("branch", "main"))
        expected_commit = await self.executor.run(["git", "-C", repo, "rev-parse", branch], timeout=60)
        expected_commit = expected_commit.strip()
        self._journal[request.id].metadata["expected_commit"] = expected_commit
        self._journal[request.id].metadata.update({"remote": remote, "branch": branch, "repo": repo})
        pushed, detail = await push_with_ssh_fallback(
            self.executor,
            repo,
            remote=remote,
            branch=branch,
            timeout=self.config.git_push_timeout_seconds,
            fallback_enabled=self.config.github_ssh_fallback,
            fallback_host=self.config.github_ssh_host_port,
        )
        if not pushed:
            raise ToolError(detail)
        remote_commit = await self._remote_ref(repo, remote, branch)
        if remote_commit is None:
            raise ToolError("git push completed but remote ref could not be confirmed")
        self._journal[request.id].metadata["remote_commit"] = remote_commit
        if remote_commit != expected_commit:
            raise ToolError(
                f"git push remote ref mismatch: expected {expected_commit}, observed {remote_commit}"
            )
        return detail

    async def _git_rollback(self, request: CapabilityRequest) -> str:
        repo = self._required(request, "repo")
        branch = self._required(request, "branch")
        self._journal[request.id].metadata.update({"repo": repo, "branch": branch})
        await self.executor.run(["git", "-C", repo, "checkout", "-q", "main"], timeout=120)
        return await self.executor.run(["git", "-C", repo, "branch", "-D", branch], timeout=120)

    async def _docker(self, request: CapabilityRequest) -> str:
        project = str(request.parameters.get("project", ""))
        if project not in self.config.allowed_auto_projects:
            raise ToolError(f"docker project is not allowlisted: {project}")
        project_dir = self.config.project_dirs.get(project, f"D:\\infrastructure\\compose\\{project}")
        # A successful post-command health probe proves the desired state, not
        # that a particular restart event was observed.  Keep those facts
        # separate in the operation journal so callers cannot accidentally use
        # healthy containers as restart attribution evidence.
        is_restart = request.capability == "docker.restart"
        self._journal[request.id].metadata.update(
            {
                "project": project,
                "project_dir": project_dir,
                "desired_state": "running",
                "desired_state_verified": False,
                "event_attribution": "unknown" if is_restart else "not-applicable",
                "event_verified": False,
                "event_verification_basis": "not-observable" if is_restart else "desired-state-operation",
            }
        )
        command = (
            ["docker", "compose", "restart"]
            if is_restart
            else ["docker", "compose", "up", "-d"]
        )
        output = await self.executor.run(command, cwd=project_dir, timeout=180)
        status = await self._docker_status(project)
        self._journal[request.id].metadata.update(
            {"container_status": status, "desired_state_verified": self._containers_healthy(status)}
        )
        if not self._containers_healthy(status):
            raise ToolError(f"docker operation completed but containers are not healthy: {status or '(none)'}")
        return output or status

    @staticmethod
    def _required(request: CapabilityRequest, name: str) -> str:
        value = request.parameters.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ToolError(f"missing required operation parameter: {name}")
        return value

    async def cancel(self, request_id: str) -> None:
        return None

    async def reconcile(self, request_id: str) -> CapabilityResult | None:
        if self.reconciliation_store is not None:
            durable = await self._reconcile_descriptor(request_id)
            if durable is not None:
                return durable
        entry = self._journal.get(request_id)
        if entry is None:
            return None
        request = entry.request
        try:
            if request.capability == "git.merge":
                status = await self._git_status(self._required(request, "repo"))
                if self._merge_in_progress(status):
                    with contextlib.suppress(ToolError):
                        await self.executor.run(
                            ["git", "-C", self._required(request, "repo"), "merge", "--abort"],
                            timeout=120,
                        )
                    status = await self._git_status(self._required(request, "repo"))
                    entry.state = "failed"
                    aborted = not self._merge_in_progress(status)
                    entry.metadata.update(
                        {"merge_status": status, "merge_aborted": aborted}
                    )
                    return self._reconciled(
                        request,
                        "failed",
                        "merge conflict aborted; merge was not applied"
                        if aborted
                        else "merge conflict remains unresolved; abort attempted",
                        entry.metadata,
                    )
                entry.state = "succeeded" if not self._merge_in_progress(status) else "unknown"
                entry.metadata["merge_status"] = status
                return self._reconciled(request, "succeeded", "git merge state confirmed", entry.metadata)

            if request.capability == "git.push":
                repo = self._required(request, "repo")
                remote = str(request.parameters.get("remote", "origin"))
                branch = str(request.parameters.get("branch", "main"))
                expected = str(entry.metadata.get("expected_commit") or "").strip()
                if not expected:
                    expected = (await self.executor.run(["git", "-C", repo, "rev-parse", branch], timeout=60)).strip()
                observed = await self._remote_ref(repo, remote, branch)
                entry.metadata.update({"expected_commit": expected, "remote_commit": observed or ""})
                if observed is None:
                    entry.state = "unknown"
                    return self._reconciled(request, "unknown", "remote ref is not observable", entry.metadata)
                if observed != expected:
                    entry.state = "failed"
                    return self._reconciled(
                        request,
                        "failed",
                        f"remote ref mismatch: expected {expected}, observed {observed}",
                        entry.metadata,
                    )
                entry.state = "succeeded"
                return self._reconciled(request, "succeeded", "remote ref matches expected commit", entry.metadata)

            if request.capability in {"docker.restart", "docker.compose.up"}:
                project = str(request.parameters.get("project", ""))
                status = await self._docker_status(project)
                entry.metadata["container_status"] = status
                desired_state_verified = self._containers_healthy(status)
                entry.metadata["desired_state"] = "running"
                entry.metadata["desired_state_verified"] = desired_state_verified
                is_restart = request.capability == "docker.restart"
                entry.metadata.setdefault(
                    "event_attribution", "unknown" if is_restart else "not-applicable"
                )
                entry.metadata.setdefault("event_verified", False)
                entry.metadata.setdefault(
                    "event_verification_basis",
                    "not-observable" if is_restart else "desired-state-operation",
                )
                if desired_state_verified:
                    # Healthy containers prove only the desired state.  A
                    # restart event is not attributable without an observed
                    # restart identity, so preserve UNKNOWN and require a
                    # later, effect-specific observation.
                    if is_restart:
                        entry.state = "unknown"
                        return self._reconciled(
                            request,
                            "unknown",
                            "Docker desired state confirmed; restart event attribution remains unknown",
                            entry.metadata,
                        )
                    entry.state = "succeeded"
                    return self._reconciled(request, "succeeded", "Docker desired state confirmed", entry.metadata)
                entry.state = "unknown"
                return self._reconciled(
                    request,
                    "unknown",
                    "Docker desired state is not healthy or not yet observable",
                    entry.metadata,
                )

            if request.capability == "git.rollback":
                repo = self._required(request, "repo")
                branch = self._required(request, "branch")
                branch_listing = await self.executor.run(
                    ["git", "-C", repo, "branch", "--list", branch],
                    timeout=60,
                )
                exists = bool(branch_listing.strip())
                entry.metadata.update({"repo": repo, "branch": branch, "branch_exists": exists})
                entry.state = "failed" if exists else "succeeded"
                return self._reconciled(
                    request,
                    "failed" if exists else "succeeded",
                    "rollback branch still exists" if exists else "rollback branch absent",
                    entry.metadata,
                )
        except ToolError as exc:
            entry.state = "unknown"
            entry.metadata["reconcile_error"] = str(exc)
            return self._reconciled(request, "unknown", str(exc), entry.metadata)
        return None

    async def _git_status(self, repo: str) -> str:
        return await self.executor.run(["git", "-C", repo, "status", "--short", "--branch"], timeout=60)

    async def _remote_ref(self, repo: str, remote: str, branch: str) -> str | None:
        output = await self.executor.run(
            ["git", "-C", repo, "ls-remote", remote, f"refs/heads/{branch}"],
            timeout=self.config.git_push_timeout_seconds,
        )
        for line in output.splitlines():
            fields = line.strip().split()
            if fields and fields[0] and fields[0] != "-":
                return fields[0]
        return None

    async def _docker_status(self, project: str) -> str:
        if project not in self.config.allowed_auto_projects:
            raise ToolError(f"docker project is not allowlisted: {project}")
        return await self.executor.run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                "{{.Names}}\t{{.Status}}",
            ],
            timeout=60,
        )

    @staticmethod
    def _containers_healthy(status: str) -> bool:
        lines = [line.strip() for line in status.splitlines() if line.strip()]
        if not lines:
            return False
        return all(
            len(line.split("\t", 1)) == 2
            and line.split("\t", 1)[1].startswith("Up")
            and "unhealthy" not in line.lower()
            and "restarting" not in line.lower()
            for line in lines
        )

    @staticmethod
    def _merge_in_progress(status: str) -> bool:
        lowered = status.lower()
        if "merge_head" in lowered or "you are in the middle of a merge" in lowered or "unmerged paths" in lowered:
            return True
        return any(
            line[:2] in {"uu", "aa", "dd", "au", "ua", "du", "ud"}
            for line in (part.strip().lower() for part in status.splitlines())
        )

    @staticmethod
    def _is_ambiguous_error(exc: ToolError) -> bool:
        lowered = str(exc).lower()
        return any(
            marker in lowered
            for marker in (
                "timed out",
                "timeout",
                "connection refused",
                "connection reset",
                "remote end hung up",
                "network is unreachable",
                "no route to host",
                "could not resolve host",
                "could not be confirmed",
                "not healthy",
                "not observable",
                "not yet observable",
            )
        )

    def _reconciled(
        self,
        request: CapabilityRequest,
        status: str,
        message: str,
        metadata: dict[str, Any],
    ) -> CapabilityResult:
        return CapabilityResult(
            request_id=request.id,
            provider_id=self.descriptor.id,
            status=status,  # type: ignore[arg-type]
            message=message,
            metadata={"operation": request.capability, **metadata},
            reconciled=True,
        )
