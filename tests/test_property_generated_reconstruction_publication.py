from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
import stat
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.routes import public_tour_payloads, public_tours
from app.product import property_tour_hosting
from app.product import service as product_service


_PUBLICATION_ARGS: dict[str, object] = {
    "principal_id": "publication-interruption-principal",
    "title": "Interruption-safe reconstruction",
    "listing_id": "publication-interruption-listing",
    "property_url": "https://example.test/property/publication-interruption-listing",
    "variant_key": "layout_first",
    "media_urls": ["https://img.example.test/living.jpg"],
    "floorplan_urls": ["https://img.example.test/floorplan.jpg"],
    "property_facts_json": {"rooms": 2},
    "source_host": "example.test",
}


def _publication_slug() -> str:
    return product_service._make_hosted_property_tour_slug(
        title=str(_PUBLICATION_ARGS["title"]),
        listing_id=str(_PUBLICATION_ARGS["listing_id"]),
        property_url=str(_PUBLICATION_ARGS["property_url"]),
        variant_key=str(_PUBLICATION_ARGS["variant_key"]),
        principal_id=str(_PUBLICATION_ARGS["principal_id"]),
    )


def _tree_snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}

    def _record(path: Path) -> None:
        relative = "." if path == root else path.relative_to(root).as_posix()
        metadata = path.lstat()
        permissions = stat.S_IMODE(metadata.st_mode)
        if path.is_symlink():
            snapshot[relative] = ("symlink", permissions, os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("directory", permissions)
        else:
            snapshot[relative] = ("file", permissions, path.read_bytes())

    _record(root)
    for current_root, directory_names, file_names in os.walk(root, followlinks=False):
        directory_names.sort()
        file_names.sort()
        current = Path(current_root)
        for name in directory_names:
            _record(current / name)
        for name in file_names:
            _record(current / name)
    return snapshot


def _write_previous_bundle(bundle_dir: Path) -> None:
    nested_dir = bundle_dir / "prior-assets" / "nested"
    nested_dir.mkdir(parents=True)
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": bundle_dir.name,
                "principal_id": _PUBLICATION_ARGS["principal_id"],
                "publication_status": "ready",
                "generation": "previous",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    prior_asset = nested_dir / "prior.bin"
    prior_asset.write_bytes(b"exact-prior-tree\x00\xff")
    prior_asset.chmod(0o640)
    nested_dir.chmod(0o750)


def _write_interrupted_working_bundle(bundle_dir: Path) -> None:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    private_render_temp = bundle_dir / "propertyquarry-reconstruction-source-private"
    private_render_temp.mkdir(exist_ok=True)
    (private_render_temp / "source.jpg").write_bytes(b"private-source")
    (bundle_dir / "new-bundle-only.bin").write_bytes(b"unpublished-new-tree")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": bundle_dir.name,
                "principal_id": _PUBLICATION_ARGS["principal_id"],
                "publication_status": "generating",
                "generation": "interrupted",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _hold_publication_lock_in_child(
    public_dir_text: str,
    slug: str,
    ready_connection: object,
    release_connection: object,
) -> None:
    public_dir = Path(public_dir_text)
    with product_service._property_reconstruction_publication_lock(
        public_dir=public_dir,
        slug=slug,
        timeout_seconds=5.0,
    ):
        ready_connection.send("locked")  # type: ignore[attr-defined]
        if not release_connection.poll(5.0):  # type: ignore[attr-defined]
            raise RuntimeError("publication_lock_test_release_timeout")
        release_connection.recv()  # type: ignore[attr-defined]


def test_generated_reconstruction_private_payload_carries_search_run_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_dir = tmp_path / "public-tours"
    public_dir.mkdir()
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_dir))
    monkeypatch.setattr(
        product_service,
        "_safe_live_property_tour_url",
        lambda value: str(value or "").strip(),
    )
    monkeypatch.setattr(
        product_service,
        "_hosted_property_tour_generated_reconstruction_bundle_ready",
        lambda _tour_url: False,
    )
    captured_payload: dict[str, object] = {}

    def _capture_private_payload(_bundle_dir: Path, payload: dict[str, object]) -> None:
        captured_payload.update(payload)
        raise RuntimeError("stop_after_private_payload")

    monkeypatch.setattr(
        product_service,
        "_write_hosted_property_tour_payload",
        _capture_private_payload,
    )

    with pytest.raises(RuntimeError, match="stop_after_private_payload"):
        product_service._write_generated_reconstruction_property_tour_bundle_unchecked(
            **_PUBLICATION_ARGS,
            search_run_id="  authoritative-run  ",
        )

    assert captured_payload["principal_id"] == _PUBLICATION_ARGS["principal_id"]
    assert captured_payload["search_run_id"] == "authoritative-run"


def test_generated_reconstruction_propagates_search_run_id_to_renderer_and_final_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_dir = tmp_path / "public-tours"
    public_dir.mkdir()
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_dir))
    bundle_dir = public_dir / _publication_slug()
    captured_unchecked: dict[str, object] = {}
    captured_authority: dict[str, object] = {}

    def _fake_unchecked(**kwargs: object) -> dict[str, object]:
        captured_unchecked.update(kwargs)
        _write_interrupted_working_bundle(bundle_dir)
        return {"publication_status": "generating"}

    def _publication_authority(principal_id: object, *, run_id: object = "") -> object:
        captured_authority.update({"principal_id": principal_id, "run_id": run_id})
        return contextlib.nullcontext(None)

    def _load_finalized_payload(
        target_bundle_dir: Path,
        *,
        principal_id: str = "",
    ) -> dict[str, object]:
        assert principal_id == _PUBLICATION_ARGS["principal_id"]
        return dict(json.loads((target_bundle_dir / "tour.json").read_text(encoding="utf-8")))

    monkeypatch.setattr(
        product_service,
        "_write_generated_reconstruction_property_tour_bundle_unchecked",
        _fake_unchecked,
    )
    monkeypatch.setattr(
        product_service._property_search_storage,
        "property_account_publication_authority",
        _publication_authority,
    )
    monkeypatch.setattr(
        product_service,
        "_hosted_property_tour_generated_reconstruction_bundle_ready",
        lambda _tour_url: True,
    )
    monkeypatch.setattr(
        product_service,
        "_load_hosted_property_tour_payload",
        _load_finalized_payload,
    )

    result = product_service._write_generated_reconstruction_property_tour_bundle_with_lock_held(
        **_PUBLICATION_ARGS,
        search_run_id="  authoritative-run  ",
    )

    assert captured_unchecked["search_run_id"] == "authoritative-run"
    assert captured_authority == {
        "principal_id": _PUBLICATION_ARGS["principal_id"],
        "run_id": "authoritative-run",
    }
    assert result["publication_status"] == "ready"


def test_visual_request_passes_authoritative_run_id_to_tour_and_reconstruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_tour: dict[str, object] = {}
    captured_reconstruction: dict[str, object] = {}
    monkeypatch.setattr(
        product_service,
        "_existing_hosted_property_tour_url_for_identity",
        lambda **_kwargs: "",
    )
    monkeypatch.setattr(
        product_service,
        "_hosted_property_tour_verified_open_url",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        product_service,
        "_property_visual_ready_tour_url",
        lambda **_kwargs: "",
    )
    monkeypatch.setattr(
        product_service,
        "_hosted_property_tour_walkthrough_asset_url",
        lambda _tour_url: "",
    )
    monkeypatch.setattr(
        product_service,
        "_property_visual_generated_reconstruction_bundle_url",
        lambda _tour_url: "",
    )
    monkeypatch.setattr(
        product_service,
        "_hosted_property_tour_first_party_open_url",
        lambda *_args, **_kwargs: "",
    )

    class _Onboarding:
        @staticmethod
        def status(*, principal_id: str) -> dict[str, object]:
            assert principal_id == "visual-principal"
            return {"property_search_preferences": {}}

    class _Container:
        onboarding = _Onboarding()

    class _Service:
        _container = _Container()

        @staticmethod
        def _current_property_search_visual_state(**_kwargs: object) -> dict[str, object]:
            return {}

        @staticmethod
        def _resolve_browseract_property_tour_binding_id(**_kwargs: object) -> str:
            return ""

        @staticmethod
        def create_willhaben_property_tour(**kwargs: object) -> dict[str, object]:
            captured_tour.update(kwargs)
            return {
                "status": "blocked",
                "blocked_reason": "listing_360_media_missing",
                "tour_media_mode": "",
                "variant_key": "layout_first",
                "title": "Selected property",
                "tour_url": "",
                "vendor_tour_url": "",
            }

        @staticmethod
        def _materialize_property_generated_reconstruction_url(**kwargs: object) -> str:
            captured_reconstruction.update(kwargs)
            return ""

        @staticmethod
        def _persist_property_search_visual_state(**_kwargs: object) -> None:
            return None

    result = product_service.ProductService.request_property_visual_asset(
        _Service(),  # type: ignore[arg-type]
        principal_id="visual-principal",
        property_url="https://immobilien.derstandard.at/detail/15201500",
        run_id="  authoritative-run  ",
        candidate_ref="candidate-1",
        request_kind="tour",
        queue_async_request=False,
        suppress_human_followup=True,
    )

    assert captured_tour["search_run_id"] == "authoritative-run"
    assert captured_reconstruction["search_run_id"] == "authoritative-run"
    assert result["run_id"] == "authoritative-run"


def test_willhaben_entrypoint_forwards_search_run_id_to_generic_provider() -> None:
    captured_generic: dict[str, object] = {}

    class _Onboarding:
        @staticmethod
        def status(*, principal_id: str) -> dict[str, object]:
            assert principal_id == "visual-principal"
            return {"property_search_preferences": {}}

    class _Container:
        onboarding = _Onboarding()

    class _Service:
        _container = _Container()

        @staticmethod
        def _enforce_property_visual_quota(**_kwargs: object) -> None:
            return None

        @staticmethod
        def create_generic_property_tour(**kwargs: object) -> dict[str, object]:
            captured_generic.update(kwargs)
            return {"status": "blocked"}

    result = product_service.ProductService.create_willhaben_property_tour(
        _Service(),  # type: ignore[arg-type]
        principal_id="visual-principal",
        property_url="https://immobilien.derstandard.at/detail/15201500",
        search_run_id="  authoritative-run  ",
    )

    assert captured_generic["search_run_id"] == "authoritative-run"
    assert result == {"status": "blocked"}


@pytest.mark.parametrize("bundle_existed", [True, False], ids=["prior-bundle", "new-bundle"])
@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit], ids=["keyboard-interrupt", "system-exit"])
@pytest.mark.parametrize("interrupt_point", ["renderer", "manifest", "replace", "readiness"])
def test_generated_reconstruction_publication_restores_or_cleans_on_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bundle_existed: bool,
    interrupt_type: type[BaseException],
    interrupt_point: str,
) -> None:
    public_dir = tmp_path / "public-tours"
    public_dir.mkdir()
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_dir))
    slug = _publication_slug()
    bundle_dir = public_dir / slug
    previous_snapshot: dict[str, tuple[object, ...]] | None = None
    if bundle_existed:
        _write_previous_bundle(bundle_dir)
        previous_snapshot = _tree_snapshot(bundle_dir)

    def _interrupt() -> None:
        raise interrupt_type(f"forced-{interrupt_point}")

    def _fake_unchecked(**_kwargs: object) -> dict[str, object]:
        _write_interrupted_working_bundle(bundle_dir)
        if interrupt_point == "renderer":
            _interrupt()
        return {"publication_status": "generating"}

    monkeypatch.setattr(
        product_service,
        "_write_generated_reconstruction_property_tour_bundle_unchecked",
        _fake_unchecked,
    )

    if interrupt_point == "manifest":
        original_read_text = Path.read_text

        def _interrupted_manifest_read(path: Path, *args: object, **kwargs: object) -> str:
            if path == bundle_dir / "tour.json":
                _interrupt()
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _interrupted_manifest_read)
    elif interrupt_point == "replace":
        original_replace = os.replace

        def _interrupted_manifest_replace(source: object, target: object) -> None:
            source_path = Path(source)
            target_path = Path(target)
            if target_path == bundle_dir / "tour.json" and source_path.name.startswith(".tour.json."):
                original_replace(source, target)
                _interrupt()
            original_replace(source, target)

        monkeypatch.setattr(product_service.os, "replace", _interrupted_manifest_replace)
    elif interrupt_point == "readiness":
        monkeypatch.setattr(
            product_service,
            "_hosted_property_tour_generated_reconstruction_bundle_ready",
            lambda _tour_url: _interrupt(),
        )

    with pytest.raises(interrupt_type, match=f"forced-{interrupt_point}"):
        product_service._write_generated_reconstruction_property_tour_bundle(**_PUBLICATION_ARGS)

    if previous_snapshot is None:
        assert not bundle_dir.exists()
    else:
        assert bundle_dir.is_dir()
        assert _tree_snapshot(bundle_dir) == previous_snapshot
        assert not (bundle_dir / "new-bundle-only.bin").exists()
        assert not (bundle_dir / "propertyquarry-reconstruction-source-private").exists()
    assert sorted(path.name for path in public_dir.iterdir()) == ([slug] if bundle_existed else [])


def test_generated_reconstruction_publication_rejects_generating_manifest_even_if_readiness_claims_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_dir = tmp_path / "public-tours"
    public_dir.mkdir()
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_dir))
    slug = _publication_slug()
    bundle_dir = public_dir / slug
    _write_previous_bundle(bundle_dir)
    previous_snapshot = _tree_snapshot(bundle_dir)

    def _fake_unchecked(**_kwargs: object) -> dict[str, object]:
        _write_interrupted_working_bundle(bundle_dir)
        return {"publication_status": "generating"}

    original_replace = os.replace

    def _skip_manifest_replace(source: object, target: object) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if target_path == bundle_dir / "tour.json" and source_path.name.startswith(".tour.json."):
            return
        original_replace(source, target)

    monkeypatch.setattr(
        product_service,
        "_write_generated_reconstruction_property_tour_bundle_unchecked",
        _fake_unchecked,
    )
    monkeypatch.setattr(product_service.os, "replace", _skip_manifest_replace)
    monkeypatch.setattr(
        product_service,
        "_hosted_property_tour_generated_reconstruction_bundle_ready",
        lambda _tour_url: True,
    )
    monkeypatch.setattr(
        product_service,
        "_load_hosted_property_tour_payload",
        lambda path, *, principal_id: json.loads((Path(path) / "tour.json").read_text(encoding="utf-8")),
    )

    with pytest.raises(RuntimeError, match="property_reconstruction_publication_status_invalid"):
        product_service._write_generated_reconstruction_property_tour_bundle(**_PUBLICATION_ARGS)

    assert _tree_snapshot(bundle_dir) == previous_snapshot
    assert sorted(path.name for path in public_dir.iterdir()) == [slug]


def test_generated_reconstruction_readiness_rejects_generating_before_asset_validation(
    tmp_path: Path,
) -> None:
    class _GeneratingPayload(dict[str, object]):
        def __contains__(self, key: object) -> bool:
            return key == "publication_status"

        def get(self, key: str, default: object = None) -> object:
            if key == "publication_status":
                return "generating"
            raise AssertionError(f"generating publication reached asset validation: {key}")

    assert property_tour_hosting._hosted_property_tour_generated_reconstruction_contract(
        bundle_dir=tmp_path,
        payload=_GeneratingPayload(),
    ) == {"ready": False}


@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit], ids=["keyboard-interrupt", "system-exit"])
def test_generated_reconstruction_publication_restores_when_initial_backup_rename_interrupts_after_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_type: type[BaseException],
) -> None:
    public_dir = tmp_path / "public-tours"
    public_dir.mkdir()
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_dir))
    slug = _publication_slug()
    bundle_dir = public_dir / slug
    _write_previous_bundle(bundle_dir)
    previous_snapshot = _tree_snapshot(bundle_dir)
    original_replace = os.replace
    interrupted = False

    def _interrupting_initial_replace(source: object, target: object) -> None:
        nonlocal interrupted
        source_path = Path(source)
        target_path = Path(target)
        if not interrupted and source_path == bundle_dir and target_path.name == "previous-bundle":
            interrupted = True
            original_replace(source, target)
            raise interrupt_type("forced-initial-backup-replace")
        original_replace(source, target)

    monkeypatch.setattr(product_service.os, "replace", _interrupting_initial_replace)
    monkeypatch.setattr(
        product_service,
        "_write_generated_reconstruction_property_tour_bundle_unchecked",
        lambda **_kwargs: pytest.fail("renderer must not run after interrupted backup rename"),
    )

    with pytest.raises(interrupt_type, match="forced-initial-backup-replace"):
        product_service._write_generated_reconstruction_property_tour_bundle(**_PUBLICATION_ARGS)

    assert _tree_snapshot(bundle_dir) == previous_snapshot
    assert sorted(path.name for path in public_dir.iterdir()) == [slug]


@pytest.mark.parametrize("bundle_existed", [True, False], ids=["prior-bundle", "new-bundle"])
def test_generated_reconstruction_publication_lock_timeout_precedes_all_bundle_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bundle_existed: bool,
) -> None:
    public_dir = tmp_path / "public-tours"
    public_dir.mkdir()
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_dir))
    monkeypatch.setenv("PROPERTYQUARRY_RECONSTRUCTION_PUBLICATION_LOCK_TIMEOUT_SECONDS", "0.05")
    slug = _publication_slug()
    bundle_dir = public_dir / slug
    previous_snapshot: dict[str, tuple[object, ...]] | None = None
    if bundle_existed:
        _write_previous_bundle(bundle_dir)
        previous_snapshot = _tree_snapshot(bundle_dir)

    process_context = multiprocessing.get_context("fork")
    ready_reader, ready_writer = process_context.Pipe(duplex=False)
    release_reader, release_writer = process_context.Pipe(duplex=False)
    holder = process_context.Process(
        target=_hold_publication_lock_in_child,
        args=(str(public_dir), slug, ready_writer, release_reader),
    )
    holder.start()
    try:
        assert ready_reader.poll(5.0)
        assert ready_reader.recv() == "locked"
        lock_dir = product_service._property_reconstruction_publication_lock_directory(public_dir)
        lock_path = lock_dir / product_service._property_reconstruction_publication_lock_name(slug=slug)
        assert lock_dir.is_dir() and not lock_dir.is_symlink()
        assert stat.S_IMODE(lock_dir.stat().st_mode) == 0o700
        assert lock_dir.stat().st_uid == os.geteuid()
        assert lock_path.is_file() and not lock_path.is_symlink()
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
        assert lock_path.stat().st_uid == os.geteuid()
        assert lock_path.stat().st_nlink == 1
        monkeypatch.setattr(
            product_service,
            "_write_generated_reconstruction_property_tour_bundle_unchecked",
            lambda **_kwargs: pytest.fail("timed-out publisher must not reach the renderer"),
        )

        with pytest.raises(RuntimeError, match="property_reconstruction_publication_lock_timeout"):
            product_service._write_generated_reconstruction_property_tour_bundle(**_PUBLICATION_ARGS)

        if previous_snapshot is None:
            assert not bundle_dir.exists()
        else:
            assert _tree_snapshot(bundle_dir) == previous_snapshot
        assert not any(path.name.startswith(f".{slug}.publication-transaction-") for path in public_dir.iterdir())
    finally:
        if holder.is_alive():
            release_writer.send("release")
        holder.join(timeout=5.0)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5.0)
    assert holder.exitcode == 0


@pytest.mark.parametrize("bundle_existed", [True, False], ids=["prior-bundle", "new-bundle"])
def test_generated_reconstruction_same_slug_publishers_serialize_restore_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bundle_existed: bool,
) -> None:
    public_dir = tmp_path / "public-tours"
    public_dir.mkdir()
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_dir))
    monkeypatch.setenv("PROPERTYQUARRY_RECONSTRUCTION_PUBLICATION_LOCK_TIMEOUT_SECONDS", "2")
    slug = _publication_slug()
    bundle_dir = public_dir / slug
    previous_snapshot: dict[str, tuple[object, ...]] | None = None
    if bundle_existed:
        _write_previous_bundle(bundle_dir)
        previous_snapshot = _tree_snapshot(bundle_dir)

    process_context = multiprocessing.get_context("fork")
    first_entered = process_context.Event()
    release_first = process_context.Event()
    second_lock_attempted = process_context.Event()
    second_entered = process_context.Event()
    results = process_context.Queue()
    original_flock = product_service.fcntl.flock

    def _observed_flock(file_descriptor: int, operation: int) -> object:
        if (
            multiprocessing.current_process().name == "publisher-two"
            and operation & product_service.fcntl.LOCK_EX
            and operation & product_service.fcntl.LOCK_NB
        ):
            second_lock_attempted.set()
        return original_flock(file_descriptor, operation)

    def _fake_unchecked(**_kwargs: object) -> dict[str, object]:
        publisher_name = multiprocessing.current_process().name
        if previous_snapshot is None:
            assert not bundle_dir.exists()
        else:
            assert _tree_snapshot(bundle_dir) == previous_snapshot
        _write_interrupted_working_bundle(bundle_dir)
        if publisher_name == "publisher-one":
            first_entered.set()
            assert release_first.wait(5.0)
        else:
            second_entered.set()
        raise RuntimeError(f"{publisher_name}-render-failed")

    def _publish() -> None:
        publisher_name = multiprocessing.current_process().name
        try:
            product_service._write_generated_reconstruction_property_tour_bundle(**_PUBLICATION_ARGS)
        except BaseException as exc:
            results.put((publisher_name, type(exc).__name__, str(exc)))

    monkeypatch.setattr(product_service.fcntl, "flock", _observed_flock)
    monkeypatch.setattr(
        product_service,
        "_write_generated_reconstruction_property_tour_bundle_unchecked",
        _fake_unchecked,
    )
    first = process_context.Process(target=_publish, name="publisher-one")
    second = process_context.Process(target=_publish, name="publisher-two")
    first.start()
    assert first_entered.wait(5.0)
    second.start()
    assert second_lock_attempted.wait(5.0)
    assert not second_entered.is_set()
    release_first.set()
    first.join(timeout=5.0)
    second.join(timeout=5.0)

    if first.is_alive():
        first.terminate()
        first.join(timeout=5.0)
    if second.is_alive():
        second.terminate()
        second.join(timeout=5.0)
    assert first.exitcode == 0
    assert second.exitcode == 0
    assert second_entered.is_set()
    result_rows = sorted((results.get(timeout=2.0), results.get(timeout=2.0)))
    assert result_rows == [
        ("publisher-one", "RuntimeError", "publisher-one-render-failed"),
        ("publisher-two", "RuntimeError", "publisher-two-render-failed"),
    ]
    if previous_snapshot is None:
        assert not bundle_dir.exists()
    else:
        assert _tree_snapshot(bundle_dir) == previous_snapshot
    assert not any(path.name.startswith(f".{slug}.publication-transaction-") for path in public_dir.iterdir())


@pytest.mark.parametrize("unsafe_kind", ["symlink", "permissions", "owner"])
def test_generated_reconstruction_publication_lock_rejects_unsafe_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_kind: str,
) -> None:
    public_dir = tmp_path / "public-tours"
    public_dir.mkdir()
    lock_dir = product_service._property_reconstruction_publication_lock_directory(public_dir)
    if unsafe_kind == "symlink":
        target_dir = tmp_path / "attacker-locks"
        target_dir.mkdir(mode=0o700)
        lock_dir.symlink_to(target_dir, target_is_directory=True)
    else:
        lock_dir.mkdir(mode=0o700)
        lock_dir.chmod(0o755 if unsafe_kind == "permissions" else 0o700)
        if unsafe_kind == "owner":
            actual_uid = os.geteuid()
            monkeypatch.setattr(product_service.os, "geteuid", lambda: actual_uid + 1)

    with pytest.raises(RuntimeError, match="property_reconstruction_publication_lock_directory_unsafe"):
        with product_service._property_reconstruction_publication_lock(
            public_dir=public_dir,
            slug=_publication_slug(),
            timeout_seconds=0.05,
        ):
            pytest.fail("unsafe lock directory must never be acquired")


@pytest.mark.parametrize("unsafe_kind", ["symlink", "permissions"])
def test_generated_reconstruction_publication_lock_rejects_unsafe_file(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    public_dir = tmp_path / "public-tours"
    public_dir.mkdir()
    lock_dir = product_service._property_reconstruction_publication_lock_directory(public_dir)
    lock_dir.mkdir(mode=0o700)
    lock_dir.chmod(0o700)
    lock_name = product_service._property_reconstruction_publication_lock_name(
        slug=_publication_slug(),
    )
    lock_path = lock_dir / lock_name
    if unsafe_kind == "symlink":
        attacker_file = tmp_path / "attacker.lock"
        attacker_file.write_bytes(b"")
        lock_path.symlink_to(attacker_file)
    else:
        lock_path.write_bytes(b"")
        lock_path.chmod(0o644)

    with pytest.raises(RuntimeError, match="property_reconstruction_publication_lock_file_unsafe"):
        with product_service._property_reconstruction_publication_lock(
            public_dir=public_dir,
            slug=_publication_slug(),
            timeout_seconds=0.05,
        ):
            pytest.fail("unsafe lock file must never be acquired")


def test_generated_reconstruction_publication_retains_private_backup_if_rollback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_dir = tmp_path / "public-tours"
    public_dir.mkdir()
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_dir))
    slug = _publication_slug()
    bundle_dir = public_dir / slug
    _write_previous_bundle(bundle_dir)
    previous_snapshot = _tree_snapshot(bundle_dir)

    def _fake_unchecked(**_kwargs: object) -> dict[str, object]:
        _write_interrupted_working_bundle(bundle_dir)
        raise RuntimeError("forced-render-failure")

    original_replace = os.replace

    def _fail_restore_replace(source: object, target: object) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if source_path.name == "previous-bundle" and target_path == bundle_dir:
            raise OSError("forced-restore-failure")
        original_replace(source, target)

    monkeypatch.setattr(
        product_service,
        "_write_generated_reconstruction_property_tour_bundle_unchecked",
        _fake_unchecked,
    )
    monkeypatch.setattr(product_service.os, "replace", _fail_restore_replace)

    with pytest.raises(OSError, match="forced-restore-failure"):
        product_service._write_generated_reconstruction_property_tour_bundle(**_PUBLICATION_ARGS)

    transaction_dirs = [
        path
        for path in public_dir.iterdir()
        if path.name.startswith(f".{slug}.publication-transaction-")
    ]
    assert len(transaction_dirs) == 1
    transaction_dir = transaction_dirs[0]
    assert stat.S_IMODE(transaction_dir.stat().st_mode) == 0o700
    assert _tree_snapshot(transaction_dir / "previous-bundle") == previous_snapshot
    assert (transaction_dir / "failed-bundle" / "new-bundle-only.bin").read_bytes() == b"unpublished-new-tree"
    assert not bundle_dir.exists()

    with product_service._property_reconstruction_publication_lock(
        public_dir=public_dir,
        slug=slug,
        timeout_seconds=0.05,
    ):
        pass


@pytest.mark.parametrize(
    "publication_status",
    ["generating", "pending", "staging", "failed", "cancelled", "unknown", "", "   ", "READY", " ready ", None],
)
def test_public_tour_page_and_asset_guards_reject_every_explicit_nonready_status(
    monkeypatch: pytest.MonkeyPatch,
    publication_status: object,
) -> None:
    monkeypatch.setattr(
        public_tours,
        "_load_tour",
        lambda _slug: {"publication_status": publication_status},
    )
    monkeypatch.setattr(
        public_tours,
        "_load_tour_with_private_receipt",
        lambda _slug: {"publication_status": publication_status},
    )

    page_response = public_tours.public_tour_page(
        "explicit-nonready",
        Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/tours/explicit-nonready",
                "query_string": b"",
                "headers": [],
                "scheme": "https",
                "server": ("propertyquarry.test", 443),
            }
        ),
        container=object(),  # The status guard runs before container access.
    )
    assert page_response.status_code == 404

    with pytest.raises(HTTPException) as payload_error:
        public_tours.public_tour_payload("explicit-nonready")
    assert payload_error.value.status_code == 404
    assert payload_error.value.detail == "tour_not_found"

    with pytest.raises(HTTPException) as asset_error:
        public_tours._asset_file("explicit-nonready", "scene.jpg")
    assert asset_error.value.status_code == 404
    assert asset_error.value.detail == "tour_not_found"


def test_public_tour_viewability_keeps_missing_legacy_publication_status_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_tour_payloads.require_public_tour_viewable({})
    monkeypatch.setattr(public_tours, "_load_tour", lambda _slug: {})

    response = public_tours.public_tour_payload("legacy-statusless-tour")

    assert response.status_code == 200


def test_public_tour_page_guard_accepts_exact_ready_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"publication_status": "ready"}
    public_tour_payloads.require_public_tour_viewable(payload)
    monkeypatch.setattr(public_tours, "_load_tour", lambda _slug: payload)

    response = public_tours.public_tour_payload("ready-tour")

    assert response.status_code == 200
