"""Tests for immutable Compose image preparation."""

import asyncio
import json
from pathlib import Path

import pytest

from repotrial.sandbox.base import ExecResult
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
COMPOSE_PATH = "compose.yml"


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
        fail_stage: str | None = None,
        inventory_output: str | bytes | None = None,
        success_stderr: str = "",
    ) -> None:
        super().__init__()
        self.guest_root = guest_root
        self.fail_stage = fail_stage
        self.inventory_output = inventory_output or _line()
        self.success_stderr = success_stderr
        self.exec_calls: list[tuple[str, ...]] = []
        self.activated_identity: str | None = None

    @property
    def supports_runtime_templates(self) -> bool:
        return True

    async def activate_runtime_template(
        self, sandbox_id: str, image_identity_sha256: str
    ) -> None:
        self._require_active(sandbox_id)
        self.activated_identity = image_identity_sha256

    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 60
    ) -> ExecResult:
        self._require_active(sandbox_id)
        call = tuple(argv)
        self.exec_calls.append(call)
        if call[-2:] == ("config", "--quiet"):
            return self._result("config")
        if call[-2:] == ("pull", "--ignore-buildable"):
            return self._result("pull")
        if call[-1:] == ("build",):
            return self._result("build")
        if call[-8:-6] == ("docker", "image"):
            return self._result("inventory", stdout=self.inventory_output)
        if call[-1:] == ("pwd",):
            return self._result("pwd", stdout=f"{self.guest_root}\n")
        if call[-5:] == ("rm", "--recursive", "--force", "--", self.guest_root):
            return self._result("remove")
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
    if call[-2:] == ("pull", "--ignore-buildable"):
        return "pull"
    if call[-1:] == ("build",):
        return "build"
    if call[-8:-6] == ("docker", "image"):
        return "inventory"
    if call[-1:] == ("pwd",):
        return "pwd"
    return "remove"


def test_prepare_compose_image_template_pulls_builds_removes_then_activates() -> None:
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
    remove = next(
        call
        for call in provider.exec_calls
        if call[-4:] == ("--recursive", "--force", "--", provider.guest_root)
    )
    assert pull[-2:] == ("pull", "--ignore-buildable")
    assert build[-1] == "build"
    assert remove[-4:] == ("--recursive", "--force", "--", provider.guest_root)
    assert all("up" not in call for call in provider.exec_calls)
    assert [_stage(call) for call in provider.exec_calls] == [
        "config",
        "pull",
        "build",
        "inventory",
        "pwd",
        "remove",
    ]


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
    remove = next(
        call
        for call in provider.exec_calls
        if call[-4:] == ("--recursive", "--force", "--", provider.guest_root)
    )
    assert remove[-4:] == ("--recursive", "--force", "--", provider.guest_root)


def test_prepare_compose_image_template_allows_bounded_success_stderr() -> None:
    provider = TemplateProvider(success_stderr="compose warning")

    inventory = _prepare(provider)

    assert provider.activated_identity == inventory.sha256


@pytest.mark.parametrize(
    "fail_stage", ["config", "pull", "build", "inventory", "remove"]
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
            "inventory": ("config", "pull", "build", "inventory"),
            "remove": ("config", "pull", "build", "inventory", "pwd", "remove"),
        }
        observed = [_stage(call) for call in provider.exec_calls]
        assert tuple(observed) == expected_stages[fail_stage]


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

    assert not any(call[-1:] == ("rm",) for call in provider.exec_calls)
    assert provider.activated_identity is None


def test_prepare_compose_image_template_rejects_guest_root_control_characters() -> None:
    guest_root = "/workspace/repo\n--unexpected"
    provider = TemplateProvider(guest_root=guest_root)

    with pytest.raises(ImageTemplateError):
        _prepare(provider)

    assert not any(
        call[-5:] == ("rm", "--recursive", "--force", "--", guest_root)
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
