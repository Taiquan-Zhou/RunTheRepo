"""Tests for immutable Compose image preparation."""

import asyncio
import json
from pathlib import Path

import pytest

from repotrial.sandbox.base import (
    ExecResult,
    SandboxFailureEvidence,
    get_sandbox_failure_evidence,
)
from repotrial.sandbox.docker_sbx import DockerSbxError
from repotrial.sandbox.fake import FakeSandboxProvider
from repotrial.trial.image_template import (
    ImageTemplateError,
    parse_image_inventory,
    prepare_compose_image_template,
    verify_compose_image_identity,
)

_IMAGE_A = "sha256:" + "a" * 64
_IMAGE_B = "sha256:" + "b" * 64
_DIGEST_A = "sha256:" + "1" * 64
_IMAGE_LIST_FORMAT = (
    '{"ID":{{json .ID}},"Repository":{{json .Repository}},'
    '"Tag":{{json .Tag}},"Digest":{{json .Digest}}}'
)
_REAL_DOCKER_IMAGE_ROW = json.dumps(
    {
        "Containers": "N/A",
        "CreatedAt": "2026-09-06 04:00:00 +0000 UTC",
        "CreatedSince": "2 hours ago",
        "Digest": _DIGEST_A,
        "ID": _IMAGE_A,
        "Repository": "alpine",
        "SharedSize": "N/A",
        "Size": "7.8MB",
        "Tag": "latest",
        "UniqueSize": "N/A",
    },
    separators=(",", ":"),
)
COMPOSE_PATH = "compose.yml"
_ALLOWED_TEMPLATE_ENV_PREFIXES = (
    (),
    ("env", "REQUIRED_SECRET=repotrial-synthetic-value"),
)


def _is_allowed_template_command(
    call: tuple[str, ...], command: tuple[str, ...]
) -> bool:
    return any(call == (*prefix, *command) for prefix in _ALLOWED_TEMPLATE_ENV_PREFIXES)


def _line(
    image_id: str = _IMAGE_A,
    *,
    repository: str = "alpine",
    tag: str = "latest",
    digest: str = _DIGEST_A,
) -> str:
    return json.dumps(
        {
            "ID": image_id,
            "Repository": repository,
            "Tag": tag,
            "Digest": digest,
        },
        separators=(",", ":"),
    )


def test_parse_image_inventory_normalizes_sorts_and_hashes_records() -> None:
    output = "\n".join(
        [
            _line(
                _IMAGE_B,
                repository=" docker.io/library/zulu ",
                tag="release",
                digest=" SHA256:" + "2" * 64,
            ),
            _line(repository="ALPINE", tag=" latest "),
        ]
    )

    inventory = parse_image_inventory(output)

    assert inventory.records == tuple(sorted(inventory.records))
    assert inventory.records[0].repository == "docker.io/library/alpine"
    assert inventory.records[0].tag == "latest"
    assert inventory.records[0].digest == _DIGEST_A
    assert inventory.records[1].repository == "docker.io/library/zulu"
    assert len(inventory.sha256) == 64
    reordered = parse_image_inventory("\n".join(output.splitlines()[::-1]))
    assert reordered.sha256 == inventory.sha256


@pytest.mark.parametrize(
    "output",
    [
        b"\xff",
        "not-json",
        '{"ID":"' + _IMAGE_A + '","Repository":"alpine","Tag":"latest"}',
        _line()[:-1] + ',"unexpected":true}',
        '{"ID":"'
        + _IMAGE_A
        + '","ID":"'
        + _IMAGE_B
        + '","Repository":"alpine","Tag":"latest","Digest":"'
        + _DIGEST_A
        + '"}',
        _line("sha256:abc"),
        "\n".join([_line(), _line(repository=" DOCKER.IO/LIBRARY/ALPINE ")]),
        "",
    ],
)
def test_parse_image_inventory_rejects_malformed_or_ambiguous_output(
    output: str | bytes,
) -> None:
    with pytest.raises(ValueError):
        parse_image_inventory(output)


def test_parse_image_inventory_rejects_excessive_records() -> None:
    output = "\n".join(_line("sha256:" + format(index, "064x")) for index in range(300))

    with pytest.raises(ValueError):
        parse_image_inventory(output)


def test_parse_image_inventory_rejects_excessive_output() -> None:
    with pytest.raises(ValueError):
        parse_image_inventory("x" * 300_000)


class TemplateProvider(FakeSandboxProvider):
    def __init__(
        self,
        *,
        guest_root: str = "/workspace/repo",
        git_root: str | None = None,
        fail_stage: str | None = None,
        inventory_output: str | bytes | None = None,
        proof_output: str | bytes = "",
        success_stderr: str = "",
        provider_error: DockerSbxError | None = None,
        provider_error_stage: str | None = None,
        activation_error: DockerSbxError | None = None,
    ) -> None:
        super().__init__()
        self.guest_root = guest_root
        self.git_root = guest_root if git_root is None else git_root
        self.fail_stage = fail_stage
        self.inventory_output = inventory_output or _line()
        self.proof_output = proof_output
        self.success_stderr = success_stderr
        self.provider_error = provider_error
        self.provider_error_stage = provider_error_stage
        self.activation_error = activation_error
        self.exec_calls: list[tuple[str, ...]] = []
        self.lifecycle: list[str] = []
        self.activated_identity: str | None = None
        self.staged_bundle: tuple[tuple[str, ...], tuple[str, ...]] | None = None
        self.staged_plan = None

    @property
    def supports_runtime_templates(self) -> bool:
        return True

    async def activate_runtime_template(
        self, sandbox_id: str, image_identity_sha256: str
    ) -> None:
        self._require_active(sandbox_id)
        if self.activation_error is not None:
            raise self.activation_error
        self.lifecycle.append("activate")
        self.activated_identity = image_identity_sha256

    async def stage_runtime_image_bundle(
        self,
        sandbox_id: str,
        image_references: tuple[str, ...],
        image_ids: tuple[str, ...],
        *,
        runtime_image_plan=None,
    ) -> None:
        self._require_active(sandbox_id)
        if self.provider_error_stage == "stage" and self.provider_error is not None:
            raise self.provider_error
        self.lifecycle.append("stage")
        self.staged_bundle = (image_references, image_ids)
        self.staged_plan = runtime_image_plan

    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 60
    ) -> ExecResult:
        self._require_active(sandbox_id)
        call = tuple(argv)
        self.exec_calls.append(call)
        if call[-2:] == ("config", "--quiet"):
            if (
                self.provider_error_stage == "config"
                and self.provider_error is not None
            ):
                raise self.provider_error
            return self._result("config")
        if call[-2:] == ("config", "--services"):
            records = [json.loads(line) for line in self.inventory_output.splitlines()]
            return self._result(
                "config-services",
                stdout="".join(f"service{index}\n" for index in range(len(records))),
            )
        if call[-3:-1] == ("config", "--images"):
            index = int(call[-1].removeprefix("service"))
            record = json.loads(self.inventory_output.splitlines()[index])
            repository = record["Repository"].strip().lower()
            if repository in {"<none>", ""}:
                repository = "service"
            if "." not in repository and ":" not in repository.split("/")[0]:
                repository = f"docker.io/library/{repository}"
            tag = record["Tag"].strip()
            digest = record["Digest"].strip()
            reference = (
                f"{repository}:{tag}" if tag != "<none>" else f"{repository}@{digest}"
            )
            return self._result("config-images", stdout=f"{reference}\n")
        if call[-2:] == ("pull", "--ignore-buildable"):
            if self.provider_error_stage == "pull" and self.provider_error is not None:
                raise self.provider_error
            return self._result("pull")
        if call[-1:] == ("build",):
            if self.provider_error_stage == "build" and self.provider_error is not None:
                raise self.provider_error
            return self._result("build")
        if call[:2] == ("docker", "image"):
            if (
                self.provider_error_stage == "inventory"
                and self.provider_error is not None
            ):
                raise self.provider_error
            if call[-4:-2] == ("inspect", "--format"):
                reference = call[-1]
                records = [
                    json.loads(line) for line in self.inventory_output.splitlines()
                ]
                for record in records:
                    repository = record["Repository"].strip().lower()
                    if repository in {"<none>", ""}:
                        repository = "service"
                    if "." not in repository and ":" not in repository.split("/")[0]:
                        repository = f"docker.io/library/{repository}"
                    tag = record["Tag"].strip()
                    digest = record["Digest"].strip()
                    expected = (
                        f"{repository}:{tag}"
                        if tag != "<none>"
                        else f"{repository}@{digest}"
                    )
                    if expected == reference:
                        return self._result("inventory", stdout=record["ID"] + "\n")
                raise AssertionError(f"unexpected inspect reference: {reference!r}")
            if call[-1] == "{{json .}}":
                return self._result("inventory", stdout=_REAL_DOCKER_IMAGE_ROW)
            assert call[-1] == _IMAGE_LIST_FORMAT
            return self._result("inventory", stdout=self.inventory_output)
        if call[-2:] == ("pwd", "-P"):
            command = call[-2:]
            if not _is_allowed_template_command(call, command):
                raise AssertionError(f"unexpected guest-root command: {call!r}")
            if self.provider_error_stage == "pwd" and self.provider_error is not None:
                raise self.provider_error
            return self._result("pwd", stdout=f"{self.guest_root}\n")
        if call[-3:] == ("git", "rev-parse", "--show-toplevel"):
            command = call[-3:]
            if not _is_allowed_template_command(call, command):
                raise AssertionError(f"unexpected guest-root command: {call!r}")
            if (
                self.provider_error_stage == "git-root"
                and self.provider_error is not None
            ):
                raise self.provider_error
            return self._result("git-root", stdout=f"{self.git_root}\n")
        if call[-5:] in {
            ("rm", "--recursive", "--force", "--", self.guest_root),
            ("rm", "--recursive", "--force", "--", self.git_root),
        }:
            command = call[-5:]
            if not _is_allowed_template_command(call, command):
                raise AssertionError(f"unexpected guest-root command: {call!r}")
            return ExecResult(
                exit_code=1,
                stdout="",
                stderr="clone root is an active mountpoint",
            )
        if call[-5:] in {
            ("find", self.guest_root, "-mindepth", "1", "-delete"),
            ("find", self.git_root, "-mindepth", "1", "-delete"),
        }:
            command = call[-5:]
            if not _is_allowed_template_command(call, command):
                raise AssertionError(f"unexpected guest-root command: {call!r}")
            if (
                self.provider_error_stage == "remove"
                and self.provider_error is not None
            ):
                raise self.provider_error
            self.lifecycle.append("clear")
            return self._result("remove")
        if call[-6:] in {
            ("find", self.guest_root, "-mindepth", "1", "-print", "-quit"),
            ("find", self.git_root, "-mindepth", "1", "-print", "-quit"),
        }:
            command = call[-6:]
            if not _is_allowed_template_command(call, command):
                raise AssertionError(f"unexpected guest-root command: {call!r}")
            if (
                self.provider_error_stage == "remove-proof"
                and self.provider_error is not None
            ):
                raise self.provider_error
            return self._result("remove-proof", stdout=self.proof_output)
        raise AssertionError(f"unexpected provider command: {call!r}")

    def _result(self, stage: str, *, stdout: str | bytes = "") -> ExecResult:
        if self.fail_stage == stage:
            return ExecResult(exit_code=1, stdout="", stderr=f"{stage} failed")
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8")
        return ExecResult(exit_code=0, stdout=stdout, stderr=self.success_stderr)


def _prepare(provider: TemplateProvider) -> object:
    async def exercise() -> object:
        sandbox_id = await provider.create(Path("missing-workspace"), "warmup")
        return await prepare_compose_image_template(
            provider,
            sandbox_id,
            COMPOSE_PATH,
            {},
            guest_workspace=provider.guest_root,
        )

    return asyncio.run(exercise())


def _stage(call: tuple[str, ...]) -> str:
    if call[-2:] == ("config", "--quiet"):
        return "config"
    if call[-2:] == ("config", "--services"):
        return "config-services"
    if call[-3:-1] == ("config", "--images"):
        return "config-images"
    if call[-2:] == ("pull", "--ignore-buildable"):
        return "pull"
    if call[-1:] == ("build",):
        return "build"
    if call[:2] == ("docker", "image"):
        return "inventory"
    if call[-2:] == ("pwd", "-P"):
        return "pwd"
    if call[-3:] == ("git", "rev-parse", "--show-toplevel"):
        return "git-root"
    if call[-5:] == ("find", call[-4], "-mindepth", "1", "-delete"):
        return "remove"
    if call[-6:] == (
        "find",
        call[-5],
        "-mindepth",
        "1",
        "-print",
        "-quit",
    ):
        return "remove-proof"
    return "remove"


def test_prepare_compose_image_template_clears_root_then_activates() -> None:
    provider = TemplateProvider()

    inventory = _prepare(provider)

    assert inventory.records
    assert provider.activated_identity == inventory.sha256
    assert provider.exec_calls[0][-2:] == ("config", "--quiet")
    pull = next(
        call
        for call in provider.exec_calls
        if call[-2:] == ("pull", "--ignore-buildable")
    )
    build = next(call for call in provider.exec_calls if call[-1:] == ("build",))
    clear = next(
        call
        for call in provider.exec_calls
        if call[-5:] == ("find", provider.guest_root, "-mindepth", "1", "-delete")
    )
    proof = next(
        call
        for call in provider.exec_calls
        if call[-6:]
        == ("find", provider.guest_root, "-mindepth", "1", "-print", "-quit")
    )
    assert pull[-2:] == ("pull", "--ignore-buildable")
    assert build[-1] == "build"
    assert clear[-5:] == (
        "find",
        provider.guest_root,
        "-mindepth",
        "1",
        "-delete",
    )
    assert proof[-6:] == (
        "find",
        provider.guest_root,
        "-mindepth",
        "1",
        "-print",
        "-quit",
    )
    assert not any(
        call[-5:] == ("rm", "--recursive", "--force", "--", provider.guest_root)
        for call in provider.exec_calls
    )
    assert all("up" not in call for call in provider.exec_calls)
    assert [_stage(call) for call in provider.exec_calls] == [
        "config",
        "pull",
        "build",
        "config-services",
        "config-images",
        "inventory",
        "inventory",
        "pwd",
        "git-root",
        "remove",
        "remove-proof",
    ]


def test_prepare_stages_sorted_unique_image_references_and_ids_before_activation() -> (
    None
):
    provider = TemplateProvider(
        inventory_output="\n".join(
            [
                _line(
                    _IMAGE_B,
                    repository="zulu",
                    tag="release",
                    digest="<none>",
                ),
                _line(repository="alpine", tag="latest"),
            ]
        )
    )

    inventory = _prepare(provider)

    assert provider.staged_bundle == (
        ("docker.io/library/alpine:latest", "docker.io/library/zulu:release"),
        (_IMAGE_A, _IMAGE_B),
    )
    assert provider.activated_identity == inventory.sha256


def test_prepare_stages_digest_only_reference_before_workspace_clear() -> None:
    provider = TemplateProvider(
        inventory_output="\n".join(
            [
                _line(
                    _IMAGE_B,
                    repository="docker.io/library/zulu",
                    tag="<none>",
                    digest=_DIGEST_A,
                ),
                _line(
                    _IMAGE_A,
                    repository="ALPINE",
                    tag="latest",
                    digest="<none>",
                ),
            ]
        )
    )

    _prepare(provider)

    assert provider.staged_bundle == (
        (
            "docker.io/library/alpine:latest",
            "docker.io/library/zulu@" + _DIGEST_A,
        ),
        (_IMAGE_A, _IMAGE_B),
    )
    assert provider.lifecycle == ["stage", "clear", "activate"]


def test_prepare_stage_failure_prevents_workspace_clear_and_activation() -> None:
    provider_error = DockerSbxError("image_bundle_stage", "image_bundle_empty")
    provider = TemplateProvider(
        provider_error=provider_error,
        provider_error_stage="stage",
    )

    with pytest.raises(DockerSbxError) as raised:
        _prepare(provider)

    assert raised.value is provider_error
    assert provider.staged_bundle is None
    assert provider.activated_identity is None
    assert not any(
        call[-5:] == ("find", provider.guest_root, "-mindepth", "1", "-delete")
        for call in provider.exec_calls
    )


def test_prepare_rejects_nonempty_clear_proof_before_activation() -> None:
    provider = TemplateProvider(proof_output="/workspace/repo/leftover\n")

    with pytest.raises(ImageTemplateError) as error:
        _prepare(provider)

    assert error.value.reason == "guest_workspace_removal_failed"
    assert provider.activated_identity is None
    assert [_stage(call) for call in provider.exec_calls] == [
        "config",
        "pull",
        "build",
        "config-services",
        "config-images",
        "inventory",
        "inventory",
        "pwd",
        "git-root",
        "remove",
        "remove-proof",
    ]


def test_prepare_uses_explicit_four_field_docker_image_format() -> None:
    provider = TemplateProvider()

    inventory = _prepare(provider)

    inventory_call = next(
        call for call in provider.exec_calls if call[-8:-6] == ("docker", "image")
    )
    assert inventory_call == (
        "docker",
        "image",
        "ls",
        "--all",
        "--no-trunc",
        "--digests",
        "--format",
        _IMAGE_LIST_FORMAT,
    )
    assert "{{json .}}" not in inventory_call[-1]
    assert inventory.records
    with pytest.raises(ValueError):
        parse_image_inventory(_REAL_DOCKER_IMAGE_ROW)


def test_prepare_compose_image_template_can_derive_proven_guest_root() -> None:
    provider = TemplateProvider()

    async def exercise() -> object:
        sandbox_id = await provider.create(Path("missing-workspace"), "warmup")
        return await prepare_compose_image_template(
            provider,
            sandbox_id,
            COMPOSE_PATH,
            {},
        )

    inventory = asyncio.run(exercise())

    assert provider.activated_identity == inventory.sha256
    clear = next(
        call
        for call in provider.exec_calls
        if call[-5:] == ("find", provider.guest_root, "-mindepth", "1", "-delete")
    )
    proof = next(
        call
        for call in provider.exec_calls
        if call[-6:]
        == ("find", provider.guest_root, "-mindepth", "1", "-print", "-quit")
    )
    assert clear[-5:] == (
        "find",
        provider.guest_root,
        "-mindepth",
        "1",
        "-delete",
    )
    assert proof[-6:] == (
        "find",
        provider.guest_root,
        "-mindepth",
        "1",
        "-print",
        "-quit",
    )


def test_prepare_compose_image_template_allows_bounded_success_stderr() -> None:
    provider = TemplateProvider(success_stderr="compose warning")

    inventory = _prepare(provider)

    assert provider.activated_identity == inventory.sha256


@pytest.mark.parametrize(
    "fail_stage", ["config", "pull", "build", "inventory", "remove", "remove-proof"]
)
def test_prepare_compose_image_template_fails_closed_before_activation(
    fail_stage: str,
) -> None:
    provider = TemplateProvider(fail_stage=fail_stage)

    with pytest.raises(ImageTemplateError):
        _prepare(provider)

    assert provider.activated_identity is None
    assert all("up" not in call for call in provider.exec_calls)
    if fail_stage == "config":
        assert len(provider.exec_calls) == 1
    else:
        expected_stages = {
            "pull": ("config", "pull"),
            "build": ("config", "pull", "build"),
            "inventory": (
                "config",
                "pull",
                "build",
                "config-services",
                "config-images",
                "inventory",
            ),
            "remove": (
                "config",
                "pull",
                "build",
                "config-services",
                "config-images",
                "inventory",
                "inventory",
                "pwd",
                "git-root",
                "remove",
            ),
            "remove-proof": (
                "config",
                "pull",
                "build",
                "config-services",
                "config-images",
                "inventory",
                "inventory",
                "pwd",
                "git-root",
                "remove",
                "remove-proof",
            ),
        }
        observed = [_stage(call) for call in provider.exec_calls]
        assert tuple(observed) == expected_stages[fail_stage]


@pytest.mark.parametrize(
    "reason", ["total_duration_exhausted", "process_cleanup_unconfirmed"]
)
def test_prepare_preserves_docker_sbx_error_from_exec(reason: str) -> None:
    failure_evidence = SandboxFailureEvidence(
        operation="exec",
        reason=reason,
        sandbox_id="sandbox-1",
    )
    provider_error = DockerSbxError(
        "exec",
        reason,
        stderr="provider-detail",
        failure_evidence=failure_evidence,
    )
    provider = TemplateProvider(
        provider_error=provider_error,
        provider_error_stage="pull",
    )

    with pytest.raises(DockerSbxError) as raised:
        _prepare(provider)

    assert raised.value is provider_error
    assert raised.value.reason == reason
    assert get_sandbox_failure_evidence(raised.value) is failure_evidence


def test_prepare_preserves_docker_sbx_error_from_compose_preflight() -> None:
    provider_error = DockerSbxError(
        "exec", "total_duration_exhausted", stderr="provider-detail"
    )
    provider = TemplateProvider(
        provider_error=provider_error,
        provider_error_stage="config",
    )

    async def exercise() -> object:
        sandbox_id = await provider.create(Path("missing-workspace"), "warmup")
        return await prepare_compose_image_template(
            provider,
            sandbox_id,
            COMPOSE_PATH,
            {},
            guest_workspace=provider.guest_root,
            declared_secret_env_keys={"REQUIRED_SECRET"},
        )

    with pytest.raises(DockerSbxError) as raised:
        asyncio.run(exercise())

    assert raised.value is provider_error
    assert raised.value.reason == "total_duration_exhausted"


@pytest.mark.parametrize(
    "reason", ["total_duration_exhausted", "process_cleanup_unconfirmed"]
)
def test_prepare_preserves_docker_sbx_error_from_activation(reason: str) -> None:
    provider_error = DockerSbxError("template_save", reason, stderr="provider-detail")
    provider = TemplateProvider(activation_error=provider_error)

    with pytest.raises(DockerSbxError) as raised:
        _prepare(provider)

    assert raised.value is provider_error
    assert raised.value.reason == reason


def test_prepare_compose_image_template_rejects_unproven_guest_root() -> None:
    provider = TemplateProvider(guest_root="/workspace/other")

    with pytest.raises(ImageTemplateError):

        async def exercise() -> object:
            sandbox_id = await provider.create(Path("missing-workspace"), "warmup")
            return await prepare_compose_image_template(
                provider,
                sandbox_id,
                COMPOSE_PATH,
                {},
                guest_workspace="/workspace/repo",
            )

        asyncio.run(exercise())

    assert not any(
        call[-5:]
        in {
            ("find", provider.guest_root, "-mindepth", "1", "-delete"),
        }
        or call[-6:]
        == ("find", provider.guest_root, "-mindepth", "1", "-print", "-quit")
        for call in provider.exec_calls
    )
    assert provider.activated_identity is None


def test_prepare_compose_image_template_rejects_mismatched_git_root() -> None:
    provider = TemplateProvider(
        guest_root="/workspace/repo",
        git_root="/workspace/other",
    )

    with pytest.raises(ImageTemplateError) as error:

        async def exercise() -> object:
            sandbox_id = await provider.create(Path("missing-workspace"), "warmup")
            return await prepare_compose_image_template(
                provider,
                sandbox_id,
                COMPOSE_PATH,
                {},
            )

        asyncio.run(exercise())

    assert error.value.reason == "guest_workspace_verification_failed"
    assert any(call[-2:] == ("pwd", "-P") for call in provider.exec_calls)
    assert any(
        call[-3:] == ("git", "rev-parse", "--show-toplevel")
        for call in provider.exec_calls
    )
    assert not any(
        call[-5:]
        in {
            ("find", provider.guest_root, "-mindepth", "1", "-delete"),
            ("find", provider.git_root, "-mindepth", "1", "-delete"),
        }
        or call[-6:]
        in {
            ("find", provider.guest_root, "-mindepth", "1", "-print", "-quit"),
            ("find", provider.git_root, "-mindepth", "1", "-print", "-quit"),
        }
        for call in provider.exec_calls
    )
    assert provider.activated_identity is None


def test_prepare_compose_image_template_rejects_guest_root_control_characters() -> None:
    guest_root = "/workspace/repo\n--unexpected"
    provider = TemplateProvider(guest_root=guest_root)

    with pytest.raises(ImageTemplateError):
        _prepare(provider)

    assert not any(
        call[-5:] == ("find", guest_root, "-mindepth", "1", "-delete")
        or call[-6:] == ("find", guest_root, "-mindepth", "1", "-print", "-quit")
        for call in provider.exec_calls
    )
    assert provider.activated_identity is None


def _verify(provider: TemplateProvider, expected: str) -> None:
    async def exercise() -> None:
        sandbox_id = await provider.create(Path("missing-workspace"), "candidate")
        await verify_compose_image_identity(
            provider,
            sandbox_id,
            expected,
            compose_path=COMPOSE_PATH,
            env={},
        )

    asyncio.run(exercise())


def test_verify_compose_image_identity_accepts_matching_inventory() -> None:
    provider = TemplateProvider()
    expected = parse_image_inventory(_line()).sha256

    _verify(provider, expected)

    assert len(provider.exec_calls) == 1
    assert provider.exec_calls[0][-8:-6] == ("docker", "image")


def test_verify_compose_image_identity_rejects_mismatch_before_startup() -> None:
    provider = TemplateProvider()

    with pytest.raises(ImageTemplateError) as error:
        _verify(provider, "f" * 64)

    assert error.value.reason == "image_identity_mismatch"
    assert len(provider.exec_calls) == 1
    assert all("up" not in call for call in provider.exec_calls)
