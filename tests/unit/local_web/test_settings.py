from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from repotrial.local_web.settings import (
    ModelSettingsStore,
    ModelSettingsUpdate,
    SettingsValidationError,
)


def test_missing_store_is_unconfigured_without_secret() -> None:
    store = ModelSettingsStore(Path("/tmp/repotrial-settings-missing.json"))

    public = store.load_public()

    assert public.provider is None
    assert public.endpoint is None
    assert public.model_name is None
    assert public.api_key_configured is False
    assert store.load_resolved() is None


def test_deepseek_defaults_and_owner_only_atomic_persistence(tmp_path: Path) -> None:
    path = tmp_path / "model-settings.json"
    store = ModelSettingsStore(path)

    public = store.save(ModelSettingsUpdate(provider="deepseek", api_key="secret"))

    assert public.provider == "deepseek"
    assert public.endpoint == "https://api.deepseek.com"
    assert public.model_name == "deepseek-v4-flash"
    assert public.api_key_configured is True
    assert path.stat().st_mode & 0o777 == 0o600
    assert "secret" not in repr(public)
    resolved = store.load_resolved()
    assert resolved is not None
    assert resolved.api_key == "secret"


def test_blank_key_retains_existing_key_and_clear_removes_it(tmp_path: Path) -> None:
    store = ModelSettingsStore(tmp_path / "settings.json")
    store.save(
        ModelSettingsUpdate(
            provider="custom",
            endpoint="http://localhost:8000/v1",
            model_name="qwen",
            api_key="first",
        )
    )

    retained = store.save(
        ModelSettingsUpdate(
            provider="custom",
            endpoint="http://localhost:8000/v1",
            model_name="qwen2",
            api_key="",
        )
    )
    assert retained.api_key_configured is True
    resolved = store.load_resolved()
    assert resolved is not None
    assert resolved.api_key == "first"

    changed = store.save(
        ModelSettingsUpdate(
            provider="custom",
            endpoint="http://localhost:9000/v1",
            model_name="qwen3",
            api_key="",
        )
    )
    assert changed.api_key_configured is False
    resolved = store.load_resolved()
    assert resolved is not None
    assert resolved.api_key is None

    cleared = store.clear_key()
    assert cleared.api_key_configured is False
    resolved = store.load_resolved()
    assert resolved is not None
    assert resolved.api_key is None


@pytest.mark.parametrize(
    "update",
    [
        ModelSettingsUpdate(provider="other"),
        ModelSettingsUpdate(provider="custom", model_name="missing-endpoint"),
        ModelSettingsUpdate(provider="custom", endpoint="http://example.com"),
        ModelSettingsUpdate(
            provider="custom",
            endpoint="https://user:secret@example.com/v1",
            model_name="model",
        ),
        ModelSettingsUpdate(
            provider="custom",
            endpoint="ftp://example.com/v1",
            model_name="model",
        ),
    ],
)
def test_save_rejects_invalid_provider_or_endpoint(
    tmp_path: Path, update: ModelSettingsUpdate
) -> None:
    with pytest.raises(SettingsValidationError):
        ModelSettingsStore(tmp_path / "settings.json").save(update)


def test_corrupt_or_symlinked_store_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("not-json", encoding="utf-8")
    store = ModelSettingsStore(path)
    assert store.load_public().api_key_configured is False
    assert store.load_resolved() is None

    target = tmp_path / "target.json"
    target.write_text(
        json.dumps(
            {
                "provider": "custom",
                "endpoint": "http://localhost:8000/v1",
                "model_name": "qwen",
                "api_key": "secret",
            }
        ),
        encoding="utf-8",
    )
    path.unlink()
    path.symlink_to(target)
    assert ModelSettingsStore(path).load_resolved() is None


def test_save_rejects_symlinked_settings_path(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    path = tmp_path / "settings.json"
    path.symlink_to(target)

    with pytest.raises(SettingsValidationError):
        ModelSettingsStore(path).save(ModelSettingsUpdate(provider="deepseek"))


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://model.example/v1?api_key=secret-value",
        "https://model.example/v1#secret-value",
    ],
)
def test_endpoint_rejects_query_and_fragment_credentials(
    tmp_path: Path, endpoint: str
) -> None:
    with pytest.raises(SettingsValidationError):
        ModelSettingsStore(tmp_path / "settings.json").save(
            ModelSettingsUpdate(
                provider="custom",
                endpoint=endpoint,
                model_name="model",
            )
        )


def test_insecure_existing_store_is_not_loaded(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = ModelSettingsStore(path)
    store.save(
        ModelSettingsUpdate(
            provider="custom",
            endpoint="https://model.example/v1",
            model_name="model",
            api_key="secret-value",
        )
    )
    path.chmod(0o644)
    assert store.load_public().api_key_configured is False
    assert store.load_resolved() is None


def test_wrong_owner_existing_store_is_not_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    import repotrial.local_web.settings as settings_module

    path = tmp_path / "settings.json"
    store = ModelSettingsStore(path)
    store.save(ModelSettingsUpdate(provider="deepseek", api_key="secret-value"))
    real_fstat = settings_module.os.fstat

    def fake_fstat(fd: int) -> object:
        result = real_fstat(fd)
        try:
            target = Path(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            return result
        if target == path:
            return type(
                "Stat",
                (),
                {
                    "st_mode": result.st_mode,
                    "st_uid": os.geteuid() + 1,
                    "st_size": result.st_size,
                },
            )()
        return result

    monkeypatch.setattr(settings_module.os, "fstat", fake_fstat)
    assert store.load_public().api_key_configured is False
    assert store.load_resolved() is None


def test_existing_ancestor_symlink_is_rejected(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    (real_root / "nested").mkdir(parents=True)
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(SettingsValidationError):
        ModelSettingsStore(linked_root / "nested" / "settings.json").save(
            ModelSettingsUpdate(provider="deepseek")
        )


def test_failed_target_chmod_preserves_previous_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import repotrial.local_web.settings as settings_module

    path = tmp_path / "settings.json"
    store = ModelSettingsStore(path)
    store.save(ModelSettingsUpdate(provider="deepseek", api_key="old-secret"))
    previous = path.read_bytes()

    def fail_temp(fd: int, mode: int) -> None:
        raise PermissionError("temporary chmod failed")

    monkeypatch.setattr(settings_module.os, "fchmod", fail_temp)
    with pytest.raises(SettingsValidationError):
        store.save(ModelSettingsUpdate(provider="deepseek", api_key="new-secret"))
    assert path.read_bytes() == previous


def test_load_rejects_target_replaced_after_path_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import repotrial.local_web.settings as settings_module

    path = tmp_path / "settings.json"
    store = ModelSettingsStore(path)
    store.save(
        ModelSettingsUpdate(
            provider="custom",
            endpoint="http://localhost:8000/v1",
            model_name="qwen",
            api_key="original",
        )
    )
    replacement = tmp_path / "replacement.json"
    replacement.write_text(
        json.dumps(
            {
                "provider": "custom",
                "endpoint": "http://localhost:8000/v1",
                "model_name": "qwen",
                "api_key": "replacement",
            }
        ),
        encoding="utf-8",
    )
    real_read = settings_module.os.read
    swapped = False

    def swap_after_open(fd: int, count: int) -> bytes:
        nonlocal swapped
        if not swapped:
            swapped = True
            path.unlink()
            path.symlink_to(replacement)
        return real_read(fd, count)

    monkeypatch.setattr(settings_module.os, "read", swap_after_open)
    resolved = store.load_resolved()
    assert swapped is True
    assert resolved is not None
    assert resolved.api_key == "original"


def test_load_rejects_ancestor_replaced_after_path_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import repotrial.local_web.settings as settings_module

    real_root = tmp_path / "real"
    real_root.mkdir()
    path = real_root / "settings.json"
    store = ModelSettingsStore(path)
    store.save(ModelSettingsUpdate(provider="deepseek", api_key="original"))
    replacement_root = tmp_path / "replacement"
    replacement_root.mkdir()
    (replacement_root / "settings.json").write_text(
        json.dumps(
            {
                "provider": "deepseek",
                "endpoint": "https://api.deepseek.com",
                "model_name": "deepseek-v4-flash",
                "api_key": "replacement",
            }
        ),
        encoding="utf-8",
    )
    real_read = settings_module.os.read
    swapped = False

    def swap_ancestor_after_open(fd: int, count: int) -> bytes:
        nonlocal swapped
        if not swapped:
            swapped = True
            path.unlink()
            real_root.rmdir()
            real_root.symlink_to(replacement_root, target_is_directory=True)
        return real_read(fd, count)

    monkeypatch.setattr(settings_module.os, "read", swap_ancestor_after_open)
    resolved = store.load_resolved()
    assert swapped is True
    assert resolved is not None
    assert resolved.api_key == "original"


def test_oversized_encoded_key_preserves_previous_bytes(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = ModelSettingsStore(path)
    store.save(ModelSettingsUpdate(provider="deepseek", api_key="old"))
    previous = path.read_bytes()

    with pytest.raises(SettingsValidationError):
        store.save(ModelSettingsUpdate(provider="deepseek", api_key="😀" * 4096))

    assert path.read_bytes() == previous
    assert store.load_resolved() is not None
    assert store.load_resolved().api_key == "old"


@pytest.mark.parametrize("api_key", ["bad\x00key", "x" * 4097])
def test_invalid_disk_key_fails_closed(tmp_path: Path, api_key: str) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "provider": "deepseek",
                "endpoint": "https://api.deepseek.com",
                "model_name": "deepseek-v4-flash",
                "api_key": api_key,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    store = ModelSettingsStore(path)

    assert store.load_public().api_key_configured is False
    assert store.load_resolved() is None


@pytest.mark.parametrize(
    "endpoint",
    ["https://[broken", "https://:443/v1", "https://example.com:bad/v1"],
)
def test_save_rejects_malformed_hostname_or_port(tmp_path: Path, endpoint: str) -> None:
    with pytest.raises(SettingsValidationError):
        ModelSettingsStore(tmp_path / "settings.json").save(
            ModelSettingsUpdate(
                provider="custom",
                endpoint=endpoint,
                model_name="model",
            )
        )


def test_fifo_load_rejects_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    os.mkfifo(path, 0o600)
    script = """
import sys
from pathlib import Path
from repotrial.local_web.settings import ModelSettingsStore
assert ModelSettingsStore(Path(sys.argv[1])).load_resolved() is None
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        timeout=2,
        check=False,
    )
    assert result.returncode == 0


def test_fifo_save_existing_rejects_without_blocking(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    os.mkfifo(path, 0o600)
    script = """
import sys
from pathlib import Path
from repotrial.local_web.settings import (
    ModelSettingsStore,
    ModelSettingsUpdate,
    SettingsValidationError,
)
try:
    ModelSettingsStore(Path(sys.argv[1])).save(
        ModelSettingsUpdate(provider="deepseek", api_key="secret")
    )
except SettingsValidationError:
    pass
else:
    raise SystemExit(1)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        timeout=2,
        check=False,
    )
    assert result.returncode == 0


def test_existing_parent_with_untrusted_writable_mode_is_rejected(
    tmp_path: Path,
) -> None:
    unsafe_parent = tmp_path / "unsafe"
    unsafe_parent.mkdir()
    unsafe_parent.chmod(0o777)
    with pytest.raises(SettingsValidationError):
        ModelSettingsStore(unsafe_parent / "settings.json").save(
            ModelSettingsUpdate(provider="deepseek")
        )


@pytest.mark.parametrize("failure", ["write", "fsync", "replace"])
def test_write_failures_preserve_previous_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import repotrial.local_web.settings as settings_module

    path = tmp_path / "settings.json"
    store = ModelSettingsStore(path)
    store.save(ModelSettingsUpdate(provider="deepseek", api_key="old"))
    previous = path.read_bytes()

    if failure == "write":

        def fail_write(fd: int, data: bytes) -> int:
            raise OSError("write failed")

        monkeypatch.setattr(settings_module.os, "write", fail_write)
    elif failure == "fsync":

        def fail_fsync(fd: int) -> None:
            raise OSError("fsync failed")

        monkeypatch.setattr(settings_module.os, "fsync", fail_fsync)
    else:

        def fail_replace(
            source: str, destination: str, *, src_dir_fd: int, dst_dir_fd: int
        ) -> None:
            raise OSError("replace failed")

        monkeypatch.setattr(settings_module.os, "replace", fail_replace)

    with pytest.raises(SettingsValidationError):
        store.save(ModelSettingsUpdate(provider="deepseek", api_key="new"))
    assert path.read_bytes() == previous


def test_load_rejects_fstat_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import repotrial.local_web.settings as settings_module

    path = tmp_path / "settings.json"
    store = ModelSettingsStore(path)
    store.save(ModelSettingsUpdate(provider="deepseek", api_key="secret"))

    def fail_fstat(fd: int) -> object:
        raise OSError("fstat failed")

    monkeypatch.setattr(settings_module.os, "fstat", fail_fstat)
    assert store.load_resolved() is None


def test_load_rejects_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import repotrial.local_web.settings as settings_module

    path = tmp_path / "settings.json"
    store = ModelSettingsStore(path)
    store.save(ModelSettingsUpdate(provider="deepseek", api_key="secret"))

    def fail_read(fd: int, count: int) -> bytes:
        raise OSError("read failed")

    monkeypatch.setattr(settings_module.os, "read", fail_read)
    assert store.load_resolved() is None


@pytest.mark.parametrize(
    "payload",
    [
        {"provider": "deepseek", "endpoint": None, "model_name": None, "api_key": ""},
        {
            "provider": "deepseek",
            "endpoint": "https://api.deepseek.com",
            "model_name": "deepseek-v4-flash",
            "api_key": 7,
        },
        {
            "provider": "deepseek",
            "endpoint": "https://api.deepseek.com",
            "model_name": "deepseek-v4-flash",
            "api_key": "secret",
            "extra": True,
        },
    ],
)
def test_disk_schema_rejects_noncanonical_records(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    assert ModelSettingsStore(path).load_resolved() is None


def test_multibyte_key_roundtrips_before_byte_limit(tmp_path: Path) -> None:
    key = "密" * 1000
    store = ModelSettingsStore(tmp_path / "settings.json")
    store.save(ModelSettingsUpdate(provider="deepseek", api_key=key))
    resolved = store.load_resolved()
    assert resolved is not None
    assert resolved.api_key == key


def test_malformed_utf8_and_json_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_bytes(b"\xff")
    path.chmod(0o600)
    store = ModelSettingsStore(path)
    assert store.load_public().api_key_configured is False

    path.write_bytes(b"{not-json")
    assert store.load_resolved() is None


def test_invalid_root_settings_path_is_rejected() -> None:
    with pytest.raises(SettingsValidationError):
        ModelSettingsStore(Path("/")).save(ModelSettingsUpdate(provider="deepseek"))


def test_endpoint_change_with_blank_key_does_not_retain_old_key(tmp_path: Path) -> None:
    store = ModelSettingsStore(tmp_path / "settings.json")
    store.save(
        ModelSettingsUpdate(
            provider="custom",
            endpoint="https://old.example/v1",
            model_name="old-model",
            api_key="old-secret",
        )
    )

    store.save(
        ModelSettingsUpdate(
            provider="custom",
            endpoint="https://new.example/v1",
            model_name="new-model",
            api_key="",
        )
    )

    resolved = store.load_resolved()
    assert resolved is not None
    assert resolved.endpoint == "https://new.example/v1"
    assert resolved.api_key is None


def test_clear_removes_all_model_settings_and_survives_reload(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = ModelSettingsStore(path)
    store.save(
        ModelSettingsUpdate(
            provider="custom",
            endpoint="https://model.example/v1",
            model_name="model",
            api_key="secret",
        )
    )
    cleared = store.clear()
    assert cleared.as_dict() == {
        "provider": None,
        "endpoint": None,
        "model_name": None,
        "api_key_configured": False,
    }
    assert ModelSettingsStore(path).load_resolved() is None
    assert ModelSettingsStore(path).load_public().as_dict() == cleared.as_dict()


def test_clear_failure_preserves_previous_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.json"
    store = ModelSettingsStore(path)
    store.save(ModelSettingsUpdate(provider="deepseek", api_key="secret"))
    previous = path.read_bytes()

    def fail(_: bytes) -> None:
        raise SettingsValidationError("settings could not be saved")

    monkeypatch.setattr(store, "_atomic_write", fail)
    with pytest.raises(SettingsValidationError):
        store.clear()
    assert path.read_bytes() == previous
