import json
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path

import pytest
from ruamel.yaml.comments import TaggedScalar

from repotrial.compose.parser import load_compose
from repotrial.compose.risk import analyze_risk, risk_score
from repotrial.domain.models import RiskFinding


def _finding_data(findings: list[RiskFinding]) -> list[dict[str, object]]:
    return [finding.model_dump() for finding in findings]


def _load_compose(tmp_path: Path, content: str) -> dict[str, object]:
    compose_path = tmp_path / "compose.yml"
    compose_path.write_text(content, encoding="utf-8")
    return load_compose(compose_path)


def _assert_findings_are_json_safe(findings: list[RiskFinding]) -> None:
    for finding in findings:
        assert json.loads(finding.model_dump_json())["evidence"] == finding.evidence


def _nested_list(depth: int, leaf: object) -> list[object]:
    nested: object = leaf
    for _ in range(depth):
        nested = [nested]
    assert isinstance(nested, list)
    return nested


def _depth_limited_value(value: object) -> object:
    nested = value
    for _ in range(48):
        assert isinstance(nested, list)
        nested = nested[0]
    return nested


def _maximum_container_depth(value: object) -> int:
    if isinstance(value, dict):
        return 1 + max(
            (_maximum_container_depth(item) for item in value.values()), default=0
        )
    if isinstance(value, list):
        return 1 + max((_maximum_container_depth(item) for item in value), default=0)
    return 0


def _unsupported_marker_count(value: object) -> int:
    if isinstance(value, dict):
        return int(value == {"$evidence_unsupported": "depth_limit"}) + sum(
            _unsupported_marker_count(item) for item in value.values()
        )
    if isinstance(value, list):
        return sum(_unsupported_marker_count(item) for item in value)
    return 0


class _CountingList(list[object]):
    def __init__(self, values: list[object]) -> None:
        super().__init__(values)
        self.fetched_items = 0

    def __iter__(self) -> Iterator[object]:
        for value in super().__iter__():
            self.fetched_items += 1
            yield value

    def __getitem__(self, index: int) -> object:
        self.fetched_items += 1
        return super().__getitem__(index)


class _CountingDict(dict[str, object]):
    def __init__(self, values: dict[str, object]) -> None:
        super().__init__(values)
        self.key_enumerations = 0
        self.value_accesses = 0

    def __iter__(self) -> Iterator[str]:
        for key in super().__iter__():
            self.key_enumerations += 1
            yield key

    def __getitem__(self, key: str) -> object:
        self.value_accesses += 1
        return super().__getitem__(key)


def test_analyze_risk_emits_every_kind_with_exact_evidence() -> None:
    compose = {
        "services": {
            "app": {
                "privileged": True,
                "network_mode": "host",
                "pid": "host",
                "cap_add": ["NET_ADMIN"],
                "user": "root:staff",
                "read_only": False,
                "volumes": [
                    "/var/run/docker.sock:/var/run/docker.sock",
                    "./config:/etc/app:rw",
                    {
                        "type": "bind",
                        "source": "../cache",
                        "target": "/cache",
                    },
                ],
            },
            "possible": {},
        }
    }

    assert _finding_data(analyze_risk(compose)) == [
        {
            "finding_id": "privileged:app",
            "kind": "privileged",
            "service": "app",
            "severity": 100,
            "evidence": {"privileged": True},
        },
        {
            "finding_id": "docker_socket_rw:app",
            "kind": "docker_socket_rw",
            "service": "app",
            "severity": 90,
            "evidence": {"volumes": ["/var/run/docker.sock:/var/run/docker.sock"]},
        },
        {
            "finding_id": "host_network:app",
            "kind": "host_network",
            "service": "app",
            "severity": 70,
            "evidence": {"network_mode": "host"},
        },
        {
            "finding_id": "host_pid:app",
            "kind": "host_pid",
            "service": "app",
            "severity": 70,
            "evidence": {"pid": "host"},
        },
        {
            "finding_id": "cap_add:app",
            "kind": "cap_add",
            "service": "app",
            "severity": 50,
            "evidence": {"cap_add": ["NET_ADMIN"]},
        },
        {
            "finding_id": "root_user:app",
            "kind": "root_user",
            "service": "app",
            "severity": 30,
            "evidence": {"user": "root:staff"},
        },
        {
            "finding_id": "root_user_possible:possible",
            "kind": "root_user_possible",
            "service": "possible",
            "severity": 30,
            "evidence": {"user": None, "declared": False},
        },
        {
            "finding_id": "rw_host_bind:app",
            "kind": "rw_host_bind",
            "service": "app",
            "severity": 25,
            "evidence": {
                "volumes": [
                    "./config:/etc/app:rw",
                    {"type": "bind", "source": "../cache", "target": "/cache"},
                ]
            },
        },
        {
            "finding_id": "writable_rootfs:app",
            "kind": "writable_rootfs",
            "service": "app",
            "severity": 25,
            "evidence": {"read_only": False, "declared": True},
        },
        {
            "finding_id": "writable_rootfs:possible",
            "kind": "writable_rootfs",
            "service": "possible",
            "severity": 25,
            "evidence": {"read_only": None, "declared": False},
        },
    ]


def test_analyze_risk_recognizes_short_and_long_host_bind_forms() -> None:
    volumes = [
        "/absolute:/absolute",
        "./relative:/relative",
        "../parent:/parent",
        "~/home:/home",
        r".\backslash-relative:/backslash-relative",
        r"..\backslash-parent:/backslash-parent",
        r"C:\host:/windows",
        r"\\server\share:/unc",
        "named-volume:/named",
        {"type": "bind", "source": "/long", "target": "/long"},
        {
            "type": "bind",
            "source": "/readonly",
            "target": "/readonly",
            "read_only": True,
        },
        {"type": "volume", "source": "/not-a-bind", "target": "/ignored"},
    ]

    findings = analyze_risk({"services": {"app": {"volumes": volumes}}})

    assert _finding_data(
        [finding for finding in findings if finding.kind == "rw_host_bind"]
    ) == [
        {
            "finding_id": "rw_host_bind:app",
            "kind": "rw_host_bind",
            "service": "app",
            "severity": 25,
            "evidence": {
                "volumes": [
                    "/absolute:/absolute",
                    "./relative:/relative",
                    "../parent:/parent",
                    "~/home:/home",
                    r".\backslash-relative:/backslash-relative",
                    r"..\backslash-parent:/backslash-parent",
                    r"C:\host:/windows",
                    r"\\server\share:/unc",
                    {"type": "bind", "source": "/long", "target": "/long"},
                ]
            },
        }
    ]


@pytest.mark.parametrize(
    ("suffix", "read_only"),
    [("", None), (":rw", False), (":ro", True)],
)
def test_short_and_long_tilde_backslash_sources_are_not_host_paths(
    suffix: str, read_only: bool | None
) -> None:
    from repotrial.compose import risk as risk_module

    short_volume = rf"~\cache:/container{suffix}"
    long_volume: dict[str, object] = {
        "type": "bind",
        "source": r"~\cache",
        "target": "/container",
    }
    if read_only is not None:
        long_volume["read_only"] = read_only

    assert risk_module._recognized_host_bind(short_volume) is None
    assert risk_module._recognized_host_bind(long_volume) is None
    findings = analyze_risk(
        {"services": {"app": {"volumes": [short_volume, long_volume]}}}
    )
    assert [finding for finding in findings if finding.kind == "rw_host_bind"] == []


def test_analyze_risk_suppresses_read_only_binds_and_does_not_duplicate_sockets() -> (
    None
):
    compose = {
        "services": {
            "app": {
                "volumes": [
                    "/var/run/docker.sock:/var/run/docker.sock:ro",
                    {
                        "type": "bind",
                        "source": "/var/run/docker.sock",
                        "read_only": True,
                    },
                    "/var/run/docker.sock:/var/run/docker.sock:rw",
                    {"type": "bind", "source": "/var/run/docker.sock"},
                    "./data:/data:ro",
                    "./cache:/cache:delegated",
                ]
            }
        }
    }

    assert _finding_data(
        [
            finding
            for finding in analyze_risk(compose)
            if finding.kind in {"docker_socket_rw", "rw_host_bind"}
        ]
    ) == [
        {
            "finding_id": "docker_socket_rw:app",
            "kind": "docker_socket_rw",
            "service": "app",
            "severity": 90,
            "evidence": {
                "volumes": [
                    "/var/run/docker.sock:/var/run/docker.sock:rw",
                    {"type": "bind", "source": "/var/run/docker.sock"},
                ]
            },
        },
        {
            "finding_id": "rw_host_bind:app",
            "kind": "rw_host_bind",
            "service": "app",
            "severity": 25,
            "evidence": {"volumes": ["./cache:/cache:delegated"]},
        },
    ]


def test_analyze_risk_parses_short_volumes_from_right_and_honors_ro_mode_token() -> (
    None
):
    volumes = [
        "/var/run/docker.sock:backup:/container",
        "/var/run/docker.sock:backup:/container:rw,z",
        "/var/run/docker.sock:/container:ro,z",
        "/var/run/docker.sock:/container:z,ro",
        "/var/run/docker.sock:/container:ro,custom",
        "/var/run/docker.sock:/container:custom,ro",
        "/var/run/docker.sock:/container:rw,z",
        "/var/run/docker.sock:/container:rw,custom",
        r"C:\work:archive:/container:rw,z",
        r"\\server\share:archive:/container:rw,z",
    ]

    findings = analyze_risk({"services": {"app": {"volumes": volumes}}})

    assert _finding_data(
        [
            finding
            for finding in findings
            if finding.kind in {"docker_socket_rw", "rw_host_bind"}
        ]
    ) == [
        {
            "finding_id": "docker_socket_rw:app",
            "kind": "docker_socket_rw",
            "service": "app",
            "severity": 90,
            "evidence": {
                "volumes": [
                    "/var/run/docker.sock:/container:rw,z",
                    "/var/run/docker.sock:/container:rw,custom",
                ]
            },
        },
        {
            "finding_id": "rw_host_bind:app",
            "kind": "rw_host_bind",
            "service": "app",
            "severity": 25,
            "evidence": {
                "volumes": [
                    "/var/run/docker.sock:backup:/container",
                    "/var/run/docker.sock:backup:/container:rw,z",
                    r"C:\work:archive:/container:rw,z",
                    r"\\server\share:archive:/container:rw,z",
                ]
            },
        },
    ]


def test_analyze_risk_validates_short_volume_mode_candidates() -> None:
    volumes = [
        "/var/run/docker.sock:/container:custom",
        "/var/run/docker.sock:/container:rocustom",
        "/var/run/docker.sock:/container,archive",
    ]

    findings = analyze_risk({"services": {"app": {"volumes": volumes}}})

    assert _finding_data(
        [
            finding
            for finding in findings
            if finding.kind in {"docker_socket_rw", "rw_host_bind"}
        ]
    ) == [
        {
            "finding_id": "docker_socket_rw:app",
            "kind": "docker_socket_rw",
            "service": "app",
            "severity": 90,
            "evidence": {"volumes": volumes},
        }
    ]


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("/host", "/container"),
        ("/host", r"D:\container"),
        ("/host", r"\\server\share"),
        (r"C:\host", "/container"),
        (r"C:\host", r"D:\container"),
        (r"C:\host", r"\\server\share"),
        (r"\\server\share", "/container"),
        (r"\\server\share", r"D:\container"),
        (r"\\server\share", r"\\server\share"),
    ],
)
@pytest.mark.parametrize(
    ("suffix", "read_write"),
    [("", True), (":ro", False), (":rw", True), (":unknown", True)],
)
def test_analyze_risk_accepts_short_bind_source_target_matrix(
    source: str, target: str, suffix: str, read_write: bool
) -> None:
    volume = f"{source}:{target}{suffix}"

    findings = analyze_risk({"services": {"app": {"volumes": [volume]}}})

    host_binds = [finding for finding in findings if finding.kind == "rw_host_bind"]
    if not read_write:
        assert host_binds == []
        return
    assert _finding_data(host_binds) == [
        {
            "finding_id": "rw_host_bind:app",
            "kind": "rw_host_bind",
            "service": "app",
            "severity": 25,
            "evidence": {"volumes": [volume]},
        }
    ]


def test_analyze_risk_ignores_ambiguous_short_bind() -> None:
    volume = "/host:/container:/nested"

    findings = analyze_risk({"services": {"app": {"volumes": [volume]}}})

    assert [finding for finding in findings if finding.kind == "rw_host_bind"] == []


def test_analyze_risk_bounds_colon_rich_short_volume_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from repotrial.compose import risk as risk_module

    host_checks: list[tuple[int, int]] = []
    target_checks: list[tuple[int, int, int]] = []
    candidate_constructions: list[object] = []
    materializations: list[object] = []
    scan_steps = 0
    is_host_path_prefix = risk_module._is_host_path_prefix
    is_container_path_range = risk_module._is_container_path_range
    short_volume_candidate = risk_module._ShortVolumeCandidate
    materialize_candidate = risk_module._materialize_short_volume_candidate

    def record_host_path_prefix(volume: str, end: int) -> bool:
        host_checks.append((len(volume), end))
        return is_host_path_prefix(volume, end)

    def record_container_path_range(volume: str, start: int, end: int) -> bool:
        target_checks.append((len(volume), start, end))
        return is_container_path_range(volume, start, end)

    def record_candidate(
        separator_index: int, target_end: int, mode_start: int | None
    ) -> object:
        candidate = short_volume_candidate(separator_index, target_end, mode_start)
        candidate_constructions.append(candidate)
        return candidate

    def record_materialization(volume: str, candidate: object) -> object:
        materializations.append(candidate)
        return materialize_candidate(volume, candidate)

    def record_range(*args: int) -> range | Iterator[int]:
        scanned_range = range(*args)
        if len(args) != 3 or args[1:] != (-1, -1):
            return scanned_range

        def count_scan_steps() -> Iterator[int]:
            nonlocal scan_steps
            for index in scanned_range:
                scan_steps += 1
                yield index

        return count_scan_steps()

    monkeypatch.setattr(risk_module, "_is_host_path_prefix", record_host_path_prefix)
    monkeypatch.setattr(
        risk_module, "_is_container_path_range", record_container_path_range
    )
    monkeypatch.setattr(risk_module, "_ShortVolumeCandidate", record_candidate)
    monkeypatch.setattr(
        risk_module, "_materialize_short_volume_candidate", record_materialization
    )
    monkeypatch.setattr(risk_module, "range", record_range, raising=False)

    ambiguous = "/a" + ":/b" * 4_000
    ambiguous_compose = _load_compose(
        tmp_path,
        f"services:\n  app:\n    volumes: ['{ambiguous}']\n",
    )

    ambiguous_findings = analyze_risk(ambiguous_compose)

    assert [
        finding for finding in ambiguous_findings if finding.kind == "rw_host_bind"
    ] == []
    assert len(host_checks) == 2
    assert len(target_checks) == 2
    assert scan_steps == 6
    assert len(candidate_constructions) == 1
    assert materializations == []

    host_checks.clear()
    target_checks.clear()
    candidate_constructions.clear()
    materializations.clear()
    scan_steps = 0
    unique = "/host" + ":archive" * 4_000 + ":/container"
    unique_compose = _load_compose(
        tmp_path,
        f"services:\n  app:\n    volumes: ['{unique}']\n",
    )

    unique_findings = analyze_risk(unique_compose)

    unique_host_binds = [
        finding for finding in unique_findings if finding.kind == "rw_host_bind"
    ]
    assert len(unique_host_binds) == 1
    assert unique_host_binds[0].evidence == {"volumes": [unique]}
    assert len(host_checks) == 4_001
    assert len(target_checks) == 4_001
    assert scan_steps == len(unique)
    assert len(candidate_constructions) == 1
    assert len(materializations) == 1


def test_analyze_risk_applies_override_tags_and_preserves_json_safe_evidence(
    tmp_path: Path,
) -> None:
    compose = _load_compose(
        tmp_path,
        "services:\n"
        "  app:\n"
        "    privileged: !override true\n"
        "    network_mode: !override host\n"
        "    pid: !override host\n"
        "    cap_add: !override [NET_ADMIN]\n"
        "    user: !override root\n"
        "    read_only: !override true\n"
        "    volumes: !override\n"
        "      - type: bind\n"
        "        source: !override /var/run/docker.sock\n"
        "        target: /socket\n",
    )

    findings = analyze_risk(compose)

    assert _finding_data(findings) == [
        {
            "finding_id": "privileged:app",
            "kind": "privileged",
            "service": "app",
            "severity": 100,
            "evidence": {"privileged": {"$yaml_tag": "!override", "value": True}},
        },
        {
            "finding_id": "docker_socket_rw:app",
            "kind": "docker_socket_rw",
            "service": "app",
            "severity": 90,
            "evidence": {
                "volumes": [
                    {
                        "type": "bind",
                        "source": {
                            "$yaml_tag": "!override",
                            "value": "/var/run/docker.sock",
                        },
                        "target": "/socket",
                    }
                ]
            },
        },
        {
            "finding_id": "host_network:app",
            "kind": "host_network",
            "service": "app",
            "severity": 70,
            "evidence": {"network_mode": {"$yaml_tag": "!override", "value": "host"}},
        },
        {
            "finding_id": "host_pid:app",
            "kind": "host_pid",
            "service": "app",
            "severity": 70,
            "evidence": {"pid": {"$yaml_tag": "!override", "value": "host"}},
        },
        {
            "finding_id": "cap_add:app",
            "kind": "cap_add",
            "service": "app",
            "severity": 50,
            "evidence": {
                "cap_add": {
                    "$yaml_tag": "!override",
                    "value": ["NET_ADMIN"],
                }
            },
        },
        {
            "finding_id": "root_user:app",
            "kind": "root_user",
            "service": "app",
            "severity": 30,
            "evidence": {"user": {"$yaml_tag": "!override", "value": "root"}},
        },
    ]
    _assert_findings_are_json_safe(findings)


def test_analyze_risk_treats_reset_tags_as_unset_and_preserves_evidence(
    tmp_path: Path,
) -> None:
    compose = _load_compose(
        tmp_path,
        "services:\n"
        "  app:\n"
        "    privileged: !reset true\n"
        "    network_mode: !reset host\n"
        "    pid: !reset host\n"
        "    cap_add: !reset [NET_ADMIN]\n"
        "    user: !reset root\n"
        "    read_only: !reset true\n"
        "    volumes: !reset\n"
        "      - /var/run/docker.sock:/socket\n",
    )

    findings = analyze_risk(compose)

    assert _finding_data(findings) == [
        {
            "finding_id": "root_user_possible:app",
            "kind": "root_user_possible",
            "service": "app",
            "severity": 30,
            "evidence": {
                "user": {"$yaml_tag": "!reset", "value": "root"},
                "declared": True,
            },
        },
        {
            "finding_id": "writable_rootfs:app",
            "kind": "writable_rootfs",
            "service": "app",
            "severity": 25,
            "evidence": {
                "read_only": {"$yaml_tag": "!reset", "value": True},
                "declared": True,
            },
        },
    ]
    _assert_findings_are_json_safe(findings)


def test_analyze_risk_keeps_quoted_override_scalars_as_strings(tmp_path: Path) -> None:
    compose = _load_compose(
        tmp_path,
        "services:\n"
        "  app:\n"
        '    privileged: !override "true"\n'
        '    network_mode: !override "host"\n'
        '    user: !override "root"\n'
        '    read_only: !override "true"\n',
    )

    findings = analyze_risk(compose)

    assert _finding_data(findings) == [
        {
            "finding_id": "host_network:app",
            "kind": "host_network",
            "service": "app",
            "severity": 70,
            "evidence": {"network_mode": {"$yaml_tag": "!override", "value": "host"}},
        },
        {
            "finding_id": "root_user:app",
            "kind": "root_user",
            "service": "app",
            "severity": 30,
            "evidence": {"user": {"$yaml_tag": "!override", "value": "root"}},
        },
        {
            "finding_id": "writable_rootfs:app",
            "kind": "writable_rootfs",
            "service": "app",
            "severity": 25,
            "evidence": {
                "read_only": {"$yaml_tag": "!override", "value": "true"},
                "declared": True,
            },
        },
    ]
    _assert_findings_are_json_safe(findings)


def test_analyze_risk_excludes_unsupported_manual_evidence_values() -> None:
    unsupported = object()
    compose = {
        "services": {
            "app": {
                "cap_add": [unsupported],
                "read_only": unsupported,
                "volumes": [{"type": "bind", "source": "/host", 1: unsupported}],
            }
        }
    }

    findings = analyze_risk(compose)

    assert _finding_data(findings) == [
        {
            "finding_id": "cap_add:app",
            "kind": "cap_add",
            "service": "app",
            "severity": 50,
            "evidence": {"cap_add": [None]},
        },
        {
            "finding_id": "root_user_possible:app",
            "kind": "root_user_possible",
            "service": "app",
            "severity": 30,
            "evidence": {"user": None, "declared": False},
        },
        {
            "finding_id": "rw_host_bind:app",
            "kind": "rw_host_bind",
            "service": "app",
            "severity": 25,
            "evidence": {"volumes": [{"source": "/host", "type": "bind"}]},
        },
        {
            "finding_id": "writable_rootfs:app",
            "kind": "writable_rootfs",
            "service": "app",
            "severity": 25,
            "evidence": {"read_only": None, "declared": True},
        },
    ]
    _assert_findings_are_json_safe(findings)


@pytest.mark.parametrize("value", ["9" * 5_000, "2026-02-30"])
def test_analyze_risk_sanitizes_invalid_allowed_tagged_scalars(value: str) -> None:
    tagged_value = TaggedScalar(value=value, tag="!override")
    findings = analyze_risk(
        {"services": {"app": {"user": tagged_value, "read_only": tagged_value}}}
    )

    assert _finding_data(findings) == [
        {
            "finding_id": "writable_rootfs:app",
            "kind": "writable_rootfs",
            "service": "app",
            "severity": 25,
            "evidence": {
                "read_only": {"$yaml_tag": "!override", "value": None},
                "declared": True,
            },
        }
    ]
    _assert_findings_are_json_safe(findings)


def test_analyze_risk_limits_real_parser_evidence_depth(tmp_path: Path) -> None:
    nested_flow = "[" * 120 + "NET_ADMIN" + "]" * 120
    compose = _load_compose(
        tmp_path,
        f"services:\n  app:\n    cap_add: {nested_flow}\n",
    )

    findings = analyze_risk(compose)

    _assert_findings_are_json_safe(findings)
    cap_add = next(finding for finding in findings if finding.kind == "cap_add")
    assert _maximum_container_depth(cap_add.evidence) <= 48
    assert _unsupported_marker_count(cap_add.evidence) == 1


def test_analyze_risk_preserves_outer_sequence_around_depth_marker() -> None:
    deep_subtree = _nested_list(120, "TOO_DEEP")
    cap_add = ["KEEP_BEFORE", deep_subtree, "KEEP_AFTER"]
    compose = {"services": {"app": {"cap_add": cap_add}}}
    original = deepcopy(compose)

    findings = analyze_risk(compose)

    finding = next(finding for finding in findings if finding.kind == "cap_add")
    safe_cap_add = finding.evidence["cap_add"]
    assert isinstance(safe_cap_add, list)
    assert safe_cap_add[0] == "KEEP_BEFORE"
    assert safe_cap_add[2] == "KEEP_AFTER"
    nested = safe_cap_add[1]
    for level in range(45):
        assert isinstance(nested, list), level
        assert len(nested) == 1
        nested = nested[0]
    assert nested == {"$evidence_unsupported": "depth_limit"}
    assert _maximum_container_depth(finding.evidence) == 48
    assert _unsupported_marker_count(finding.evidence) == 1
    _assert_findings_are_json_safe(findings)
    assert compose == original
    assert compose["services"]["app"]["cap_add"] is cap_add


def test_analyze_risk_preserves_outer_sequence_around_overwide_mapping() -> None:
    overwide = {f"key-{index}": None for index in range(10_000)}
    cap_add = ["KEEP_BEFORE", overwide, "KEEP_AFTER"]

    findings = analyze_risk({"services": {"app": {"cap_add": cap_add}}})

    finding = next(finding for finding in findings if finding.kind == "cap_add")
    assert finding.evidence["cap_add"] == [
        "KEEP_BEFORE",
        {"$evidence_unsupported": "node_limit"},
        "KEEP_AFTER",
    ]
    _assert_findings_are_json_safe(findings)
    assert cap_add[1] is overwide
    assert len(overwide) == 10_000


def test_analyze_risk_preserves_tagged_outer_sequence_around_depth_marker(
    tmp_path: Path,
) -> None:
    nested_flow = "[" * 120 + "TOO_DEEP" + "]" * 120
    compose = _load_compose(
        tmp_path,
        "services:\n"
        "  app:\n"
        f"    cap_add: !override [KEEP_BEFORE, {nested_flow}, KEEP_AFTER]\n",
    )
    services = compose["services"]
    assert isinstance(services, dict)
    app = services["app"]
    assert isinstance(app, dict)
    raw_cap_add = app["cap_add"]

    findings = analyze_risk(compose)

    finding = next(finding for finding in findings if finding.kind == "cap_add")
    tagged = finding.evidence["cap_add"]
    assert isinstance(tagged, dict)
    assert tagged["$yaml_tag"] == "!override"
    safe_cap_add = tagged["value"]
    assert isinstance(safe_cap_add, list)
    assert safe_cap_add[0] == "KEEP_BEFORE"
    assert safe_cap_add[2] == "KEEP_AFTER"
    nested = safe_cap_add[1]
    for level in range(44):
        assert isinstance(nested, list), level
        assert len(nested) == 1
        nested = nested[0]
    assert nested == {"$evidence_unsupported": "depth_limit"}
    assert _maximum_container_depth(finding.evidence) == 48
    assert _unsupported_marker_count(finding.evidence) == 1
    _assert_findings_are_json_safe(findings)
    assert app["cap_add"] is raw_cap_add


def test_analyze_risk_limits_tagged_output_evidence_depth(tmp_path: Path) -> None:
    nested_flow = "!override [" * 60 + "NET_ADMIN" + "]" * 60
    compose = _load_compose(
        tmp_path,
        f"services:\n  app:\n    cap_add: {nested_flow}\n",
    )
    services = compose["services"]
    assert isinstance(services, dict)
    app = services["app"]
    assert isinstance(app, dict)
    raw_cap_add = app["cap_add"]

    findings = analyze_risk(compose)

    _assert_findings_are_json_safe(findings)
    finding = next(finding for finding in findings if finding.kind == "cap_add")
    assert _maximum_container_depth(finding.evidence) <= 48
    assert _unsupported_marker_count(finding.evidence) == 1
    assert app["cap_add"] is raw_cap_add


def test_analyze_risk_limits_manual_acyclic_evidence_depth() -> None:
    cap_add = _nested_list(500, "NET_ADMIN")
    compose = {"services": {"app": {"cap_add": cap_add}}}

    findings = analyze_risk(compose)

    _assert_findings_are_json_safe(findings)
    finding = next(finding for finding in findings if finding.kind == "cap_add")
    assert _maximum_container_depth(finding.evidence) <= 48
    assert _unsupported_marker_count(finding.evidence) == 1
    assert _depth_limited_value(cap_add) != {"$evidence_unsupported": "depth_limit"}


def test_analyze_risk_truncates_overwide_evidence_before_iteration() -> None:
    cap_add = _CountingList([None] * 20_000)
    findings = analyze_risk({"services": {"app": {"cap_add": cap_add}}})

    finding = next(finding for finding in findings if finding.kind == "cap_add")

    assert cap_add.fetched_items == 0
    assert finding.evidence["cap_add"] == {"$evidence_unsupported": "node_limit"}
    assert len(finding.model_dump_json()) < 500


def test_analyze_risk_marks_overwide_evidence_once() -> None:
    cap_add = [None] * 10_001
    findings = analyze_risk({"services": {"app": {"cap_add": cap_add}}})

    _assert_findings_are_json_safe(findings)
    finding = next(finding for finding in findings if finding.kind == "cap_add")
    assert finding.evidence["cap_add"] == {"$evidence_unsupported": "node_limit"}


def test_analyze_risk_reserves_nested_mapping_keys_before_enumeration() -> None:
    child: object = None
    mappings: list[_CountingDict] = []
    for _ in range(5):
        mapping = _CountingDict({f"key-{index}": child for index in range(9_000)})
        mappings.append(mapping)
        child = mapping
    cap_add = [child]

    findings = analyze_risk({"services": {"app": {"cap_add": cap_add}}})

    observed_work = sum(
        mapping.key_enumerations + mapping.value_accesses for mapping in mappings
    )
    finding = next(finding for finding in findings if finding.kind == "cap_add")
    assert observed_work <= 10_000
    assert finding.evidence["cap_add"] == [{"$evidence_unsupported": "node_limit"}]
    assert len(finding.model_dump_json()) < 500
    _assert_findings_are_json_safe(findings)
    for index, mapping in enumerate(mappings):
        assert len(mapping) == 9_000
        assert mapping["key-0"] is (None if index == 0 else mappings[index - 1])


def test_analyze_risk_stops_before_fetching_sibling_after_node_exhaustion() -> None:
    children = [_CountingList([None]) for _ in range(6_000)]
    cap_add = _CountingList(children)

    findings = analyze_risk({"services": {"app": {"cap_add": cap_add}}})

    finding = next(finding for finding in findings if finding.kind == "cap_add")
    assert cap_add.fetched_items == 4_999
    assert sum(child.fetched_items for child in children) == 4_998
    assert finding.evidence["cap_add"] == {"$evidence_unsupported": "node_limit"}
    assert len(finding.model_dump_json()) < 500
    _assert_findings_are_json_safe(findings)
    assert cap_add[0] is children[0]


@pytest.mark.parametrize(
    ("user", "kind"),
    [
        (0, "root_user"),
        ("0:123", "root_user"),
        ("root", "root_user"),
        ("root:staff", "root_user"),
        (True, None),
        (False, None),
        ("1000:0", None),
        ("${UID}", None),
        ([], None),
    ],
)
def test_analyze_risk_distinguishes_explicit_root_and_unresolved_users(
    user: object, kind: str | None
) -> None:
    findings = analyze_risk({"services": {"app": {"user": user}}})
    user_findings = [finding for finding in findings if "user" in finding.kind]

    if kind is None:
        assert user_findings == []
    else:
        assert _finding_data(user_findings) == [
            {
                "finding_id": f"{kind}:app",
                "kind": kind,
                "service": "app",
                "severity": 30,
                "evidence": {"user": user},
            }
        ]


@pytest.mark.parametrize(
    ("service", "evidence"),
    [
        ("missing", {"user": None, "declared": False}),
        ("null", {"user": None, "declared": True}),
        ("empty", {"user": "", "declared": True}),
    ],
)
def test_analyze_risk_marks_only_missing_null_or_empty_user_as_possible_root(
    service: str, evidence: dict[str, object]
) -> None:
    definitions: dict[str, object] = {
        "missing": {},
        "null": {"user": None},
        "empty": {"user": ""},
    }

    findings = analyze_risk({"services": {service: definitions[service]}})

    assert _finding_data(
        [finding for finding in findings if finding.kind == "root_user_possible"]
    ) == [
        {
            "finding_id": f"root_user_possible:{service}",
            "kind": "root_user_possible",
            "service": service,
            "severity": 30,
            "evidence": evidence,
        }
    ]


def test_analyze_risk_treats_everything_except_true_as_writable_rootfs() -> None:
    compose = {
        "services": {
            "true": {"read_only": True},
            "false": {"read_only": False},
            "integer": {"read_only": 1},
            "missing": {},
        }
    }

    assert _finding_data(
        [
            finding
            for finding in analyze_risk(compose)
            if finding.kind == "writable_rootfs"
        ]
    ) == [
        {
            "finding_id": "writable_rootfs:false",
            "kind": "writable_rootfs",
            "service": "false",
            "severity": 25,
            "evidence": {"read_only": False, "declared": True},
        },
        {
            "finding_id": "writable_rootfs:integer",
            "kind": "writable_rootfs",
            "service": "integer",
            "severity": 25,
            "evidence": {"read_only": 1, "declared": True},
        },
        {
            "finding_id": "writable_rootfs:missing",
            "kind": "writable_rootfs",
            "service": "missing",
            "severity": 25,
            "evidence": {"read_only": None, "declared": False},
        },
    ]


def test_analyze_risk_is_stable_across_service_mapping_order() -> None:
    first = {
        "services": {
            "zeta": {"network_mode": "host"},
            "alpha": {"network_mode": "host", "pid": "host"},
        }
    }
    second = {
        "services": {
            "alpha": {"pid": "host", "network_mode": "host"},
            "zeta": {"network_mode": "host"},
        }
    }

    assert _finding_data(analyze_risk(first)) == _finding_data(analyze_risk(second))
    assert [finding.finding_id for finding in analyze_risk(first)] == [
        "host_network:alpha",
        "host_network:zeta",
        "host_pid:alpha",
        "root_user_possible:alpha",
        "root_user_possible:zeta",
        "writable_rootfs:alpha",
        "writable_rootfs:zeta",
    ]


def test_analyze_risk_ignores_missing_or_invalid_service_and_volume_shapes() -> None:
    assert analyze_risk({}) == []
    assert analyze_risk({"services": []}) == []
    assert analyze_risk({"services": {1: {}, "list": [], "string": "nginx"}}) == []

    findings = analyze_risk({"services": {"app": {"volumes": "./data:/data"}}})

    assert [finding.finding_id for finding in findings] == [
        "root_user_possible:app",
        "writable_rootfs:app",
    ]


def test_analyze_risk_does_not_mutate_its_input() -> None:
    compose = {
        "services": {
            "app": {
                "cap_add": ["NET_ADMIN"],
                "volumes": ["./config:/config", {"type": "bind", "source": "/tmp"}],
            }
        }
    }
    original = deepcopy(compose)

    analyze_risk(compose)

    assert compose == original


def test_risk_score_returns_unbounded_sum_of_severities() -> None:
    findings = [
        RiskFinding(
            finding_id="privileged:one",
            kind="privileged",
            service="one",
            severity=100,
            evidence={},
        ),
        RiskFinding(
            finding_id="privileged:two",
            kind="privileged",
            service="two",
            severity=100,
            evidence={},
        ),
        RiskFinding(
            finding_id="docker_socket_rw:three",
            kind="docker_socket_rw",
            service="three",
            severity=90,
            evidence={},
        ),
    ]

    assert risk_score(findings) == 290
