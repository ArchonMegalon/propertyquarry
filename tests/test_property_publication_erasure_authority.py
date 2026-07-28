from __future__ import annotations

import contextlib
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Iterator

import pytest
from fastapi import HTTPException

from app.api.routes import public_tours
from app.product import service as product_service
from app.product import property_search_storage as storage
from app.product import property_search_schema as search_schema
from app.product import property_tour_hosting


class _AuthorityDatabase:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.fenced_run_ids: set[str] = set()
        self.eraser_attempting = threading.Event()
        self.fence_recorded = threading.Event()


class _AuthorityCursor:
    def __init__(self, connection: "_AuthorityConnection") -> None:
        self._connection = connection
        self._row: tuple[object, ...] | None = None

    def __enter__(self) -> "_AuthorityCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(
        self,
        sql: str,
        params: tuple[object, ...] | None = None,
    ) -> None:
        normalized_sql = " ".join(str(sql or "").split()).lower()
        if "pg_advisory_xact_lock" in normalized_sql:
            if threading.current_thread().name == "account-eraser":
                self._connection.database.eraser_attempting.set()
            self._connection.database.lock.acquire()
            self._connection.holds_lock = True
        elif "select exists" in normalized_sql:
            requested_run_id = str((params or ("", ""))[1] or "").strip()
            self._row = (
                "" in self._connection.database.fenced_run_ids
                or requested_run_id in self._connection.database.fenced_run_ids,
            )
        elif "insert into property_search_erasure_fences" in normalized_sql:
            fenced_run_id = str((params or ("", ""))[1] or "").strip()
            self._connection.database.fenced_run_ids.add(fenced_run_id)
            self._connection.database.fence_recorded.set()

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _AuthorityTransaction:
    def __init__(self, connection: "_AuthorityConnection") -> None:
        self._connection = connection

    def __enter__(self) -> None:
        self._connection.in_transaction = True

    def __exit__(self, *_args: object) -> None:
        self._connection.in_transaction = False
        if self._connection.holds_lock:
            self._connection.holds_lock = False
            self._connection.database.lock.release()


class _AuthorityConnection:
    def __init__(self, database: _AuthorityDatabase) -> None:
        self.database = database
        self.in_transaction = False
        self.holds_lock = False

    def __enter__(self) -> "_AuthorityConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def transaction(self) -> _AuthorityTransaction:
        return _AuthorityTransaction(self)

    def cursor(self) -> _AuthorityCursor:
        return _AuthorityCursor(self)


def _install_authority_database(
    monkeypatch: pytest.MonkeyPatch,
) -> _AuthorityDatabase:
    database = _AuthorityDatabase()

    @contextlib.contextmanager
    def _connect() -> Iterator[_AuthorityConnection]:
        yield _AuthorityConnection(database)

    monkeypatch.setenv(
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET",
        "publication-authority-test-secret",
    )
    monkeypatch.setattr(storage, "_property_search_run_database_url", lambda: "postgresql://isolated")
    monkeypatch.setattr(storage, "_require_property_search_run_schema", lambda: None)
    monkeypatch.setattr(storage, "_property_search_run_connect", _connect)
    return database


def test_publication_authority_serializes_with_account_erasure_without_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _install_authority_database(monkeypatch)
    publication_entered = threading.Event()
    release_publication = threading.Event()
    publication_exited = threading.Event()
    errors: list[BaseException] = []

    def _publish() -> None:
        try:
            with storage.property_account_publication_authority("tenant-publication-race"):
                publication_entered.set()
                assert release_publication.wait(timeout=5)
            publication_exited.set()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def _erase() -> None:
        try:
            principal_key = storage._property_search_principal_key(
                "tenant-publication-race"
            )
            with storage._property_search_run_connect() as connection:
                with storage._property_search_run_transaction(connection):
                    with connection.cursor() as cursor:
                        storage._set_property_search_writer_contract(cursor)
                        storage._record_property_search_erasure_fence(
                            cursor,
                            principal_key=principal_key,
                        )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    publisher = threading.Thread(target=_publish, name="artifact-publisher", daemon=True)
    eraser = threading.Thread(target=_erase, name="account-eraser", daemon=True)
    publisher.start()
    assert publication_entered.wait(timeout=5)
    eraser.start()
    assert database.eraser_attempting.wait(timeout=5)
    assert not database.fence_recorded.is_set()

    release_publication.set()
    publisher.join(timeout=5)
    eraser.join(timeout=5)

    assert not publisher.is_alive()
    assert not eraser.is_alive()
    assert errors == []
    assert publication_exited.is_set()
    assert database.fence_recorded.is_set()

    with pytest.raises(
        storage.PropertyAccountPublicationForbiddenError,
        match="property_account_publication_forbidden",
    ):
        with storage.property_account_publication_authority(
            "tenant-publication-race"
        ):
            pytest.fail("an erased account must not regain publication authority")


def test_publication_authority_checks_run_and_account_erasure_fences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _install_authority_database(monkeypatch)
    principal_id = "tenant-run-publication-authority"
    principal_key = storage._property_search_principal_key(principal_id)

    with storage._property_search_run_connect() as connection:
        with storage._property_search_run_transaction(connection):
            with connection.cursor() as cursor:
                storage._set_property_search_writer_contract(cursor)
                storage._record_property_search_erasure_fence(
                    cursor,
                    principal_key=principal_key,
                    run_id="erased-run",
                )

    assert database.fenced_run_ids == {"erased-run"}
    with storage.property_account_publication_authority(
        principal_id,
        run_id="surviving-run",
    ) as authority_connection:
        assert isinstance(authority_connection, _AuthorityConnection)
    with storage.property_account_publication_authority(principal_id):
        pass
    with pytest.raises(
        storage.PropertyAccountPublicationForbiddenError,
        match="property_account_publication_forbidden",
    ):
        with storage.property_account_publication_authority(
            principal_id,
            run_id="erased-run",
        ):
            pytest.fail("a deleted search run must not republish its artifacts")

    with storage._property_search_run_connect() as connection:
        with storage._property_search_run_transaction(connection):
            with connection.cursor() as cursor:
                storage._set_property_search_writer_contract(cursor)
                storage._record_property_search_erasure_fence(
                    cursor,
                    principal_key=principal_key,
                )

    assert database.fenced_run_ids == {"", "erased-run"}
    for requested_run_id in ("", "surviving-run"):
        with pytest.raises(
            storage.PropertyAccountPublicationForbiddenError,
            match="property_account_publication_forbidden",
        ):
            with storage.property_account_publication_authority(
                principal_id,
                run_id=requested_run_id,
            ):
                pytest.fail("an account fence must reject every publication")


@pytest.mark.skipif(
    os.environ.get("EA_RUN_PROPERTY_SEARCH_POSTGRES_INTEGRATION") != "1",
    reason="explicit isolated PostgreSQL integration lane only",
)
def test_postgres_publication_authority_and_erasure_are_transactionally_ordered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = str(os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        pytest.skip("DATABASE_URL is required for the explicit integration lane")

    import psycopg
    from psycopg import sql

    schema_name = f"publication_authority_{uuid.uuid4().hex}"
    principal_id = "tenant-postgres-publication-race"
    monkeypatch.setenv(
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET",
        "postgres-publication-authority-test-secret",
    )
    admin = psycopg.connect(database_url, autocommit=True, connect_timeout=5)
    try:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        setup = psycopg.connect(database_url, autocommit=True, connect_timeout=5)
        try:
            with setup.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET search_path TO {}").format(
                        sql.Identifier(schema_name)
                    )
                )
                for migration in search_schema.PROPERTY_SEARCH_MIGRATIONS:
                    cursor.execute(migration.sql)
        finally:
            setup.close()

        @contextlib.contextmanager
        def _connect() -> Iterator[object]:
            connection = psycopg.connect(
                database_url,
                autocommit=True,
                connect_timeout=5,
                options=f"-csearch_path={schema_name}",
            )
            try:
                yield connection
            finally:
                connection.close()

        monkeypatch.setattr(
            storage,
            "_property_search_run_database_url",
            lambda: database_url,
        )
        monkeypatch.setattr(
            storage,
            "_require_property_search_run_schema",
            lambda: None,
        )
        monkeypatch.setattr(storage, "_property_search_run_connect", _connect)

        publication_entered = threading.Event()
        release_publication = threading.Event()
        eraser_attempting = threading.Event()
        results: list[dict[str, int]] = []
        errors: list[BaseException] = []
        original_record_fence = storage._record_property_search_erasure_fence

        def _observed_record_fence(*args: object, **kwargs: object) -> None:
            eraser_attempting.set()
            original_record_fence(*args, **kwargs)

        monkeypatch.setattr(
            storage,
            "_record_property_search_erasure_fence",
            _observed_record_fence,
        )

        def _publish() -> None:
            try:
                with storage.property_account_publication_authority(principal_id):
                    publication_entered.set()
                    assert release_publication.wait(timeout=5)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        def _erase() -> None:
            try:
                results.append(
                    storage._erase_property_search_account_data(
                        principal_ids=(principal_id,)
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        publisher = threading.Thread(
            target=_publish,
            name="postgres-artifact-publisher",
            daemon=True,
        )
        eraser = threading.Thread(
            target=_erase,
            name="postgres-account-eraser",
            daemon=True,
        )
        publisher.start()
        assert publication_entered.wait(timeout=5)
        eraser.start()
        assert eraser_attempting.wait(timeout=5)
        assert results == []

        release_publication.set()
        publisher.join(timeout=5)
        eraser.join(timeout=5)

        assert not publisher.is_alive()
        assert not eraser.is_alive()
        assert errors == []
        assert results == [
            {
                "runs_deleted": 0,
                "work_jobs_deleted": 0,
                "packet_links_deleted": 0,
                "packet_links_legal_hold_retained": 0,
            }
        ]
        with pytest.raises(
            storage.PropertyAccountPublicationForbiddenError,
            match="property_account_publication_forbidden",
        ):
            with storage.property_account_publication_authority(principal_id):
                pytest.fail("the committed erasure fence must reject late publication")
    finally:
        try:
            with admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema_name)
                    )
                )
        finally:
            admin.close()


def test_publication_authority_fails_closed_in_production_without_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_RUNTIME_MODE", "production")
    monkeypatch.setattr(storage, "_property_search_run_database_url", lambda: "")

    with pytest.raises(
        RuntimeError,
        match="property_account_publication_authority_unavailable",
    ):
        with storage.property_account_publication_authority("tenant-production"):
            pytest.fail("production publication must require durable authority")


def test_hosted_tour_commit_holds_authority_through_both_manifests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "authority-guarded-tour"
    observed: list[tuple[str, str]] = []

    @contextlib.contextmanager
    def _authority(
        principal_id: object,
        *,
        run_id: object = "",
    ) -> Iterator[None]:
        assert principal_id == "tenant-tour-owner"
        assert run_id == "search-run-42"
        observed.append(("entered", str(run_id)))
        yield
        assert (bundle_dir / "tour.private.json").is_file()
        assert (bundle_dir / "tour.json").is_file()
        observed.append(("released", str(run_id)))

    monkeypatch.setattr(
        property_tour_hosting,
        "property_account_publication_authority",
        _authority,
    )

    property_tour_hosting._write_hosted_property_tour_payload(
        bundle_dir,
        {
            "slug": bundle_dir.name,
            "principal_id": "tenant-tour-owner",
            "search_run_id": "search-run-42",
            "publication_status": "ready",
            "title": "Authority guarded tour",
        },
    )

    private_payload = json.loads((bundle_dir / "tour.private.json").read_text(encoding="utf-8"))
    public_payload = json.loads((bundle_dir / "tour.json").read_text(encoding="utf-8"))
    assert private_payload["search_run_id"] == "search-run-42"
    assert "search_run_id" not in public_payload
    assert observed == [
        ("entered", "search-run-42"),
        ("released", "search-run-42"),
    ]


def test_hosted_tour_authority_rejection_precedes_all_bundle_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "forbidden-tour"

    @contextlib.contextmanager
    def _reject(
        _principal_id: object,
        *,
        run_id: object = "",
    ) -> Iterator[None]:
        assert run_id == "erased-search-run"
        raise storage.PropertyAccountPublicationForbiddenError(
            "property_account_publication_forbidden"
        )
        yield  # pragma: no cover

    monkeypatch.setattr(
        property_tour_hosting,
        "property_account_publication_authority",
        _reject,
    )

    with pytest.raises(
        storage.PropertyAccountPublicationForbiddenError,
        match="property_account_publication_forbidden",
    ):
        property_tour_hosting._write_hosted_property_tour_payload(
            bundle_dir,
            {
                "slug": bundle_dir.name,
                "principal_id": "tenant-erased-owner",
                "search_run_id": "erased-search-run",
                "publication_status": "generating",
                "title": "Forbidden tour",
            },
        )

    assert not bundle_dir.exists()


def test_generated_reconstruction_tail_cannot_republish_erased_search_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database = _install_authority_database(monkeypatch)
    principal_id = "tenant-render-tail-erased"
    search_run_id = "search-run-render-tail-erased"
    title = "Render tail apartment"
    listing_id = "render-tail-1"
    property_url = "https://example.test/render-tail-1"
    variant_key = "layout_first"
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    monkeypatch.setenv("EA_PUBLIC_TOUR_BASE_URL", "https://propertyquarry.test/tours")

    slug = product_service._make_hosted_property_tour_slug(
        title=title,
        listing_id=listing_id,
        property_url=property_url,
        variant_key=variant_key,
        principal_id=principal_id,
    )
    bundle_dir = tmp_path / slug
    render_waiting = threading.Event()
    allow_render_tail = threading.Event()
    errors: list[BaseException] = []

    def _write_manifest_and_asset(*, marker: str) -> None:
        generated_dir = bundle_dir / "generated-reconstruction"
        generated_dir.mkdir(parents=True, exist_ok=True)
        asset_path = generated_dir / f"{marker}.glb"
        asset_path.write_bytes(b"post-erasure-render-bytes")
        asset_path.chmod(0o644)
        (bundle_dir / "tour.private.json").write_text(
            json.dumps(
                {
                    "principal_id": principal_id,
                    "search_run_id": search_run_id,
                }
            ),
            encoding="utf-8",
        )
        (bundle_dir / "tour.json").write_text(
            json.dumps(
                {
                    "slug": slug,
                    "title": title,
                    "display_title": title,
                    "publication_status": "generating",
                    "generated_reconstruction": {
                        "viewer_relpath": "generated-reconstruction/viewer.html",
                    },
                }
            ),
            encoding="utf-8",
        )

    def _render_tail(**kwargs: object) -> dict[str, object]:
        assert kwargs["search_run_id"] == search_run_id
        # The first tree is visible to the run-erasure tour sweep. The renderer
        # remains alive and recreates the canonical path after that sweep,
        # matching a bridge/subprocess that outlives revocation.
        _write_manifest_and_asset(marker="before-erasure")
        render_waiting.set()
        assert allow_render_tail.wait(timeout=5)
        assert not bundle_dir.exists()
        _write_manifest_and_asset(marker="after-erasure")
        return {}

    monkeypatch.setattr(
        product_service,
        "_write_generated_reconstruction_property_tour_bundle_unchecked",
        _render_tail,
    )

    def _publish() -> None:
        try:
            product_service._write_generated_reconstruction_property_tour_bundle_with_lock_held(
                principal_id=principal_id,
                search_run_id=search_run_id,
                title=title,
                listing_id=listing_id,
                property_url=property_url,
                variant_key=variant_key,
                media_urls=("https://example.test/photo.jpg",),
                floorplan_urls=(),
                property_facts_json={},
                source_host="example.test",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    publisher = threading.Thread(
        target=_publish,
        name="reconstruction-tail-publisher",
        daemon=True,
    )
    publisher.start()
    assert render_waiting.wait(timeout=5)

    principal_key = storage._property_search_principal_key(principal_id)
    with storage._property_search_run_connect() as connection:
        with storage._property_search_run_transaction(connection):
            with connection.cursor() as cursor:
                storage._set_property_search_writer_contract(cursor)
                storage._record_property_search_erasure_fence(
                    cursor,
                    principal_key=principal_key,
                    run_id=search_run_id,
                )
    assert database.fence_recorded.is_set()
    revocation = property_tour_hosting.revoke_hosted_property_tour_bundle(
        slug=slug,
        principal_id=principal_id,
        actor="run_erasure",
    )
    assert revocation["status"] == "revoked"
    assert not bundle_dir.exists()

    allow_render_tail.set()
    publisher.join(timeout=5)

    assert not publisher.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], storage.PropertyAccountPublicationForbiddenError)
    assert str(errors[0]) == "property_account_publication_forbidden"
    assert not bundle_dir.exists()
    assert property_tour_hosting.hosted_property_tour_revocation_receipt(slug)[
        "status"
    ] == "revoked"
    assert not any(
        path.name.startswith(f".{slug}.publication-transaction-")
        for path in tmp_path.iterdir()
    )

    with pytest.raises(HTTPException) as payload_error:
        public_tours.public_tour_payload(slug)
    assert payload_error.value.status_code == 410
    with pytest.raises(HTTPException) as asset_error:
        public_tours.public_tour_file(
            slug,
            "generated-reconstruction/after-erasure.glb",
            None,  # type: ignore[arg-type]
        )
    assert asset_error.value.status_code == 410
    assert public_tours._load_private_tour_receipt(slug) == {}


def test_revocation_waits_for_same_slug_publisher_rollback_before_deleting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    public_dir = tmp_path / "public-tours"
    public_dir.mkdir()
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_dir))
    monkeypatch.setenv("EA_PUBLIC_TOUR_BASE_URL", "https://propertyquarry.test/tours")
    principal_id = "tenant-revoke-publisher-race"
    search_run_id = "search-run-revoke-publisher-race"
    title = "Publisher rollback race"
    listing_id = "publisher-rollback-race-1"
    property_url = "https://example.test/publisher-rollback-race-1"
    variant_key = "layout_first"
    slug = product_service._make_hosted_property_tour_slug(
        title=title,
        listing_id=listing_id,
        property_url=property_url,
        variant_key=variant_key,
        principal_id=principal_id,
    )
    bundle_dir = public_dir / slug
    bundle_dir.mkdir()
    (bundle_dir / "tour.private.json").write_text(
        json.dumps(
            {
                "principal_id": principal_id,
                "search_run_id": search_run_id,
            }
        ),
        encoding="utf-8",
    )
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": title,
                "display_title": title,
                "publication_status": "ready",
            }
        ),
        encoding="utf-8",
    )
    (bundle_dir / "prior-tree.bin").write_bytes(b"prior-tree")

    backup_renamed = threading.Event()
    allow_working_copy = threading.Event()
    revoker_lock_attempting = threading.Event()
    revocation_finished = threading.Event()
    publisher_errors: list[BaseException] = []
    revoker_errors: list[BaseException] = []
    revocation_results: list[dict[str, object]] = []
    observed_lock_inodes: list[tuple[int, int]] = []
    original_copytree = product_service.shutil.copytree
    original_hosting_lock = property_tour_hosting._hosted_property_tour_publication_lock

    def _paused_copytree(source: object, target: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if Path(source).name == "previous-bundle" and Path(target) == bundle_dir:
            assert not bundle_dir.exists()
            backup_renamed.set()
            assert allow_working_copy.wait(timeout=5)
        return original_copytree(source, target, *args, **kwargs)

    def _fail_after_backup(**kwargs: object) -> dict[str, object]:
        assert kwargs["search_run_id"] == search_run_id
        raise RuntimeError("forced-render-failure-after-backup")

    @contextlib.contextmanager
    def _observed_hosting_lock(
        *,
        public_dir: Path,
        slug: str,
    ) -> Iterator[None]:
        assert public_dir == bundle_dir.parent
        assert slug == bundle_dir.name
        lock_path = product_service._property_reconstruction_publication_lock_directory(
            public_dir
        ) / product_service._property_reconstruction_publication_lock_name(slug=slug)
        lock_stat = lock_path.stat()
        observed_lock_inodes.append((lock_stat.st_dev, lock_stat.st_ino))
        revoker_lock_attempting.set()
        with original_hosting_lock(public_dir=public_dir, slug=slug):
            acquired_stat = lock_path.stat()
            observed_lock_inodes.append((acquired_stat.st_dev, acquired_stat.st_ino))
            yield

    monkeypatch.setattr(product_service.shutil, "copytree", _paused_copytree)
    monkeypatch.setattr(
        product_service,
        "_write_generated_reconstruction_property_tour_bundle_unchecked",
        _fail_after_backup,
    )
    monkeypatch.setattr(
        property_tour_hosting,
        "_hosted_property_tour_publication_lock",
        _observed_hosting_lock,
    )

    def _publish() -> None:
        try:
            product_service._write_generated_reconstruction_property_tour_bundle(
                principal_id=principal_id,
                search_run_id=search_run_id,
                title=title,
                listing_id=listing_id,
                property_url=property_url,
                variant_key=variant_key,
                media_urls=("https://example.test/photo.jpg",),
                floorplan_urls=(),
                property_facts_json={},
                source_host="example.test",
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            publisher_errors.append(exc)

    def _revoke() -> None:
        try:
            revocation_results.append(
                property_tour_hosting.revoke_hosted_property_tour_bundle(
                    slug=slug,
                    principal_id=principal_id,
                    actor="account_erasure",
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            revoker_errors.append(exc)
        finally:
            revocation_finished.set()

    publisher = threading.Thread(target=_publish, name="paused-publisher", daemon=True)
    revoker = threading.Thread(target=_revoke, name="serialized-revoker", daemon=True)
    publisher.start()
    try:
        assert backup_renamed.wait(timeout=5)
        assert not bundle_dir.exists()
        lock_path = product_service._property_reconstruction_publication_lock_directory(
            public_dir
        ) / product_service._property_reconstruction_publication_lock_name(slug=slug)
        publisher_lock_stat = lock_path.stat()
        publisher_lock_inode = (publisher_lock_stat.st_dev, publisher_lock_stat.st_ino)
        revoker.start()
        assert revoker_lock_attempting.wait(timeout=5)
        assert not revocation_finished.wait(timeout=0.1)
    finally:
        allow_working_copy.set()
        publisher.join(timeout=5)
        revoker.join(timeout=5)

    assert not publisher.is_alive()
    assert not revoker.is_alive()
    assert len(publisher_errors) == 1
    assert str(publisher_errors[0]) == "forced-render-failure-after-backup"
    assert revoker_errors == []
    assert len(revocation_results) == 1
    assert revocation_results[0]["status"] == "revoked"
    assert revocation_results[0]["already_revoked"] is False
    assert observed_lock_inodes == [publisher_lock_inode, publisher_lock_inode]
    assert not bundle_dir.exists()
    assert property_tour_hosting.hosted_property_tour_revocation_receipt(slug)[
        "status"
    ] == "revoked"


def test_matching_revocation_retry_deletes_resurrected_canonical_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    public_dir = tmp_path / "public-tours"
    public_dir.mkdir()
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(public_dir))
    principal_id = "tenant-idempotent-revocation"
    slug = "idempotent-revocation-tour"
    bundle_dir = public_dir / slug
    bundle_dir.mkdir()
    (bundle_dir / "tour.private.json").write_text(
        json.dumps({"principal_id": principal_id}),
        encoding="utf-8",
    )
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Idempotent revocation",
                "publication_status": "ready",
            }
        ),
        encoding="utf-8",
    )

    first = property_tour_hosting.revoke_hosted_property_tour_bundle(
        slug=slug,
        principal_id=principal_id,
        actor="account_erasure",
    )
    assert first["status"] == "revoked"
    assert first["already_revoked"] is False
    assert not bundle_dir.exists()

    bundle_dir.mkdir()
    (bundle_dir / "resurrected-after-revocation.bin").write_bytes(b"must-be-deleted")
    retry = property_tour_hosting.revoke_hosted_property_tour_bundle(
        slug=slug,
        principal_id=principal_id,
        actor="account_erasure_retry",
    )

    assert retry["status"] == "revoked"
    assert retry["already_revoked"] is True
    assert retry["revoked_at"] == first["revoked_at"]
    assert not bundle_dir.exists()

    bundle_dir.mkdir()
    (bundle_dir / "undeletable-resurrection.bin").write_bytes(b"verify-absence")
    monkeypatch.setattr(property_tour_hosting.shutil, "rmtree", lambda _path: None)
    with pytest.raises(
        RuntimeError,
        match="hosted_property_tour_revocation_removal_failed",
    ):
        property_tour_hosting.revoke_hosted_property_tour_bundle(
            slug=slug,
            principal_id=principal_id,
            actor="account_erasure_retry",
        )
    assert bundle_dir.exists()
