import json
from copy import deepcopy
from pathlib import Path

import pytest

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
