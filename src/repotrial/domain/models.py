import re
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import ExperimentVerdict, MutationType, Verdict

_MAX_RECOVERY_ENV_KEYS = 32
_RECOVERY_ENV_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_RECOVERY_CONTROL_ENV_KEYS = frozenset(
    {"HOME", "PATH", "PYTHONHOME", "PYTHONPATH", "XDG_CONFIG_HOME"}
)
_RECOVERY_CONTROL_ENV_PREFIXES = ("COMPOSE_", "DOCKER_", "DYLD_", "LD_")
_SHA256_CONFIG_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SHA256_RAW_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class RepoRef(BaseModel):
    url: str
    owner: str
    repo: str
    requested_ref: str | None = None


class PinnedRepo(BaseModel):
    repo: RepoRef
    commit_sha: str = Field(min_length=40, max_length=40)
    local_path: Path


class RiskFinding(BaseModel):
    finding_id: str
    kind: str
    service: str
    severity: int = Field(ge=0, le=100)
    evidence: dict[str, Any]


class JourneyAssertion(BaseModel):
    kind: str
    target: str
    expected: Any


class JourneyStep(BaseModel):
    step_id: str
    tool: str
    action: str
    params: dict[str, Any]
    assertions: list[JourneyAssertion] = Field(default_factory=list)


class Journey(BaseModel):
    journey_id: str
    name: str
    steps: list[JourneyStep]


class JourneyResult(BaseModel):
    journey_id: str
    verdict: Verdict
    passed_steps: int
    total_steps: int
    evidence_paths: list[str] = Field(default_factory=list)
    failure_reason: str | None = None
    evidence_failure_reason: str | None = None


class Mutation(BaseModel):
    mutation_id: str
    type: MutationType
    service: str
    params: dict[str, Any] = Field(default_factory=dict)


class ObservationSnapshot(BaseModel):
    inspect: dict[str, Any] = Field(default_factory=dict)
    file_changes: list[dict[str, Any]] = Field(default_factory=list)
    process_events: list[dict[str, Any]] = Field(default_factory=list)
    network_events: list[dict[str, Any]] = Field(default_factory=list)
    unsupported_collectors: list[str] = Field(default_factory=list)


class ExperimentRecord(BaseModel):
    experiment_id: str
    parent_config_hash: str
    candidate_config_hash: str
    mutation: Mutation
    boot: Verdict
    journeys: list[JourneyResult]
    before: ObservationSnapshot | None = None
    after: ObservationSnapshot | None = None
    verdict: ExperimentVerdict
    reason: str


class HardenedOverlayProvenance(BaseModel):
    """Identity of a terminal, workload-conditioned hardened overlay."""

    baseline_reference: str
    baseline_config_hash: str
    final_reference: str
    final_config_hash: str
    overlay_relative_reference: str
    overlay_raw_sha256: str
    format: Literal["repotrial-hardened-overlay"]
    version: Literal[1]

    @field_validator(
        "baseline_reference", "final_reference", "overlay_relative_reference"
    )
    @classmethod
    def _validate_relative_reference(cls, value: str) -> str:
        path = Path(value)
        if (
            not value
            or path.is_absolute()
            or path.drive
            or ".." in path.parts
            or "\\" in value
            or any(part in {"", "."} for part in value.split("/"))
            or any(unicodedata.category(char).startswith("C") for char in value)
        ):
            raise ValueError("artifact reference must be a safe relative path")
        return value

    @field_validator("baseline_config_hash", "final_config_hash")
    @classmethod
    def _validate_config_hash(cls, value: str) -> str:
        if _SHA256_CONFIG_PATTERN.fullmatch(value) is None:
            raise ValueError("config hash must be a sha256 digest")
        return value

    @field_validator("overlay_raw_sha256")
    @classmethod
    def _validate_raw_hash(cls, value: str) -> str:
        if _SHA256_RAW_PATTERN.fullmatch(value) is None:
            raise ValueError("overlay raw hash must be a sha256 digest")
        return value


class RunState(BaseModel):
    model_config = ConfigDict(
        validate_assignment=True,
        revalidate_instances="always",
    )

    run_id: str
    repo_url: str
    commit_sha: str | None = None
    compose_path: str | None = None
    baseline_compose_path: str | None = None
    compatibility_overlay_path: str | None = None
    compatibility_overlay_sha256: str | None = None
    sandbox_id: str | None = None
    baseline_config_hash: str | None = None
    current_config_hash: str | None = None
    hardened_overlay_provenance: HardenedOverlayProvenance | None = None
    recovery_env_keys: list[str] = Field(
        default_factory=list, max_length=_MAX_RECOVERY_ENV_KEYS
    )
    risk_findings: list[RiskFinding] = Field(default_factory=list)
    journeys: list[Journey] = Field(default_factory=list)
    baseline_journey_results: list[JourneyResult] = Field(default_factory=list)
    baseline_observation: ObservationSnapshot | None = None
    experiments: list[ExperimentRecord] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    stop_reason: str | None = None

    def __setattr__(self, name: str, value: object) -> None:
        if name == "compatibility_overlay_path":
            current_sha256 = self.__dict__.get("compatibility_overlay_sha256")
            if (value is None) != (current_sha256 is None):
                raise ValueError(
                    "compatibility overlay path and sha256 must be provided together"
                )
        elif name == "compatibility_overlay_sha256":
            current_path = self.__dict__.get("compatibility_overlay_path")
            if (value is None) != (current_path is None):
                raise ValueError(
                    "compatibility overlay path and sha256 must be provided together"
                )
        super().__setattr__(name, value)

    @field_validator("recovery_env_keys", mode="before")
    @classmethod
    def _normalize_recovery_env_keys(cls, value: object) -> list[str]:
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise TypeError("recovery_env_keys must be a collection of key names")
        keys = list(value)
        if any(not isinstance(key, str) for key in keys):
            raise TypeError("recovery_env_keys must contain only strings")
        normalized = sorted(set(keys))
        if len(normalized) > _MAX_RECOVERY_ENV_KEYS:
            raise ValueError("recovery_env_keys exceeds the maximum size")
        for key in normalized:
            normalized_key = key.upper()
            if (
                _RECOVERY_ENV_KEY_PATTERN.fullmatch(key) is None
                or normalized_key in _RECOVERY_CONTROL_ENV_KEYS
                or normalized_key.startswith(_RECOVERY_CONTROL_ENV_PREFIXES)
            ):
                raise ValueError("recovery_env_keys contains an unsafe key name")
        return normalized

    @model_validator(mode="after")
    def _validate_compatibility_identity(self) -> Self:
        if (self.compatibility_overlay_path is None) != (
            self.compatibility_overlay_sha256 is None
        ):
            raise ValueError(
                "compatibility overlay path and sha256 must be provided together"
            )
        provenance = self.hardened_overlay_provenance
        if provenance is not None and (
            provenance.baseline_reference != self.baseline_compose_path
            or provenance.baseline_config_hash != self.baseline_config_hash
            or provenance.final_reference != self.compose_path
            or provenance.final_config_hash != self.current_config_hash
        ):
            raise ValueError("hardened overlay provenance does not match run identity")
        return self

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        copied = super().model_copy(update=update, deep=deep)
        return type(self).model_validate(copied)
