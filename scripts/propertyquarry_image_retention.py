from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCHEMA = "propertyquarry-image-retention-v1"
RUNTIME_REPOSITORIES = (
    "propertyquarry-standalone-web-runtime",
    "propertyquarry-standalone-render-runtime",
)
LOCAL_TAG_PATTERN = re.compile(r"^local-[0-9a-f]{12}$")
MANAGED_TAG_PATTERNS = {
    RUNTIME_REPOSITORIES[0]: re.compile(
        r"^(?:local-[0-9a-f]{12}|flagship-[0-9a-f]{12}(?:-[0-9a-f]{12}|-build)?)$"
    ),
    RUNTIME_REPOSITORIES[1]: LOCAL_TAG_PATTERN,
    "propertyquarry-local-candidate": re.compile(r"^[0-9a-f]{12}$"),
    "propertyquarry-playwright": re.compile(r"^local$"),
    "propertyquarry-browseract-operator": re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$"),
}
TARGET_REPOSITORIES = tuple(MANAGED_TAG_PATTERNS)
IMAGE_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _run(command: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=check,
        capture_output=True,
        text=True,
    )


def _json_output(command: Sequence[str]) -> Any:
    output = _run(command).stdout.strip()
    return json.loads(output) if output else []


def _active_image_ids() -> set[str]:
    container_ids = [
        value
        for value in _run(
            ["/usr/bin/docker", "container", "ls", "--all", "--quiet"]
        ).stdout.splitlines()
        if value
    ]
    if not container_ids:
        return set()
    inspections = _json_output(
        ["/usr/bin/docker", "container", "inspect", *container_ids]
    )
    return {
        str(item.get("Image", ""))
        for item in inspections
        if IMAGE_ID_PATTERN.fullmatch(str(item.get("Image", "")))
    }


def _local_image_rows() -> list[dict[str, object]]:
    rows: list[dict[str, str]] = []
    output = _run(
        [
            "/usr/bin/docker",
            "image",
            "ls",
            "--no-trunc",
            "--format",
            "{{json .}}",
        ]
    ).stdout
    for line in output.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        repository = str(row.get("Repository", ""))
        tag = str(row.get("Tag", ""))
        image_id = str(row.get("ID", ""))
        if (
            repository in MANAGED_TAG_PATTERNS
            and MANAGED_TAG_PATTERNS[repository].fullmatch(tag)
            and IMAGE_ID_PATTERN.fullmatch(image_id)
        ):
            rows.append(
                {"repository": repository, "tag": tag, "image_id": image_id}
            )

    image_ids = sorted({str(row["image_id"]) for row in rows})
    if not image_ids:
        return []
    details = _json_output(["/usr/bin/docker", "image", "inspect", *image_ids])
    details_by_id = {
        str(item.get("Id", "")): item
        for item in details
        if IMAGE_ID_PATTERN.fullmatch(str(item.get("Id", "")))
    }
    result: list[dict[str, object]] = []
    for row in rows:
        detail = details_by_id.get(str(row["image_id"]), {})
        result.append(
            {
                **row,
                "created": str(detail.get("Created", "")),
                "size_bytes": int(detail.get("Size", 0)),
            }
        )
    return result


def _expected_images(
    *, expected_web_image: str | None, expected_render_image: str | None
) -> dict[str, str]:
    expected: dict[str, str] = {}
    if expected_web_image:
        expected[RUNTIME_REPOSITORIES[0]] = expected_web_image
    if expected_render_image:
        expected[RUNTIME_REPOSITORIES[1]] = expected_render_image
    for repository, image_id in expected.items():
        if not IMAGE_ID_PATTERN.fullmatch(image_id):
            raise ValueError(f"invalid expected image id for {repository}")
    return expected


def build_plan(
    rows: Iterable[Mapping[str, object]],
    *,
    active_image_ids: set[str],
    expected_images: Mapping[str, str],
    keep_previous: int,
) -> dict[str, object]:
    if keep_previous < 1:
        raise ValueError("keep_previous must be at least 1")

    grouped: dict[str, dict[str, dict[str, object]]] = {
        repository: {} for repository in TARGET_REPOSITORIES
    }
    for row in rows:
        repository = str(row.get("repository", ""))
        tag = str(row.get("tag", ""))
        image_id = str(row.get("image_id", ""))
        if (
            repository not in grouped
            or not MANAGED_TAG_PATTERNS[repository].fullmatch(tag)
            or not IMAGE_ID_PATTERN.fullmatch(image_id)
        ):
            continue
        image = grouped[repository].setdefault(
            image_id,
            {
                "image_id": image_id,
                "created": str(row.get("created", "")),
                "size_bytes": int(row.get("size_bytes", 0)),
                "tags": [],
            },
        )
        image["tags"].append(tag)  # type: ignore[union-attr]

    missing_expected: list[str] = []
    for repository, image_id in expected_images.items():
        if repository not in grouped or image_id not in grouped[repository]:
            missing_expected.append(repository)
    if missing_expected:
        raise ValueError(
            "expected images are not locally tagged for: "
            + ", ".join(sorted(missing_expected))
        )

    protected_reasons: dict[tuple[str, str], set[str]] = {}
    for repository, images in grouped.items():
        for image_id in images:
            if image_id in active_image_ids:
                protected_reasons.setdefault((repository, image_id), set()).add(
                    "container_reference"
                )
            if expected_images.get(repository) == image_id:
                protected_reasons.setdefault((repository, image_id), set()).add(
                    "expected_live"
                )

        if repository in RUNTIME_REPOSITORIES:
            rollback_candidates = sorted(
                (
                    image
                    for image_id, image in images.items()
                    if (repository, image_id) not in protected_reasons
                    and any(
                        LOCAL_TAG_PATTERN.fullmatch(str(tag))
                        for tag in image["tags"]  # type: ignore[union-attr]
                    )
                ),
                key=lambda image: (
                    str(image["created"]),
                    str(image["image_id"]),
                ),
                reverse=True,
            )
            for image in rollback_candidates[:keep_previous]:
                protected_reasons.setdefault(
                    (repository, str(image["image_id"])), set()
                ).add("rollback")

    protected: list[dict[str, object]] = []
    removable: list[dict[str, object]] = []
    for repository, images in grouped.items():
        for image_id, image in images.items():
            protection_key = (repository, image_id)
            target = protected if protection_key in protected_reasons else removable
            for tag in sorted(set(image["tags"])):  # type: ignore[arg-type]
                entry: dict[str, object] = {
                    "repository": repository,
                    "tag": tag,
                    "reference": f"{repository}:{tag}",
                    "image_id": image_id,
                    "created": image["created"],
                    "size_bytes": image["size_bytes"],
                }
                if target is protected:
                    entry["reasons"] = sorted(protected_reasons[protection_key])
                target.append(entry)

    protected.sort(key=lambda item: str(item["reference"]))
    removable.sort(key=lambda item: str(item["reference"]))
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "plan",
        "status": "pass",
        "policy": {
            "repositories": list(TARGET_REPOSITORIES),
            "tag_pattern": LOCAL_TAG_PATTERN.pattern,
            "tag_patterns": {
                repository: pattern.pattern
                for repository, pattern in MANAGED_TAG_PATTERNS.items()
            },
            "keep_previous_distinct_images_per_repository": keep_previous,
            "keep_previous_distinct_images_per_runtime_repository": keep_previous,
            "ephemeral_images_have_no_rollback_retention": True,
            "protect_all_container_references": True,
            "protect_expected_live_images": True,
        },
        "counts": {
            "protected_tags": len(protected),
            "removable_tags": len(removable),
        },
        "protected": protected,
        "removable": removable,
        "secret_values_recorded": False,
    }


def _image_id_for_reference(reference: str) -> str | None:
    result = _run(
        [
            "/usr/bin/docker",
            "image",
            "inspect",
            reference,
            "--format",
            "{{.Id}}",
        ],
        check=False,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value if IMAGE_ID_PATTERN.fullmatch(value) else None


def apply_plan(plan: Mapping[str, object]) -> dict[str, object]:
    active_image_ids = _active_image_ids()
    removed: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    for entry in plan.get("removable", []):
        reference = str(entry["reference"])
        planned_image_id = str(entry["image_id"])
        current_image_id = _image_id_for_reference(reference)
        if current_image_id is None:
            skipped.append({"reference": reference, "reason": "already_absent"})
            continue
        if current_image_id != planned_image_id:
            skipped.append({"reference": reference, "reason": "tag_changed"})
            continue
        active_image_ids.update(_active_image_ids())
        if current_image_id in active_image_ids:
            skipped.append({"reference": reference, "reason": "container_reference"})
            continue
        result = _run(
            ["/usr/bin/docker", "image", "rm", reference],
            check=False,
        )
        if result.returncode == 0:
            removed.append({"reference": reference, "image_id": current_image_id})
        else:
            failures.append({"reference": reference, "reason": "docker_remove_failed"})

    return {
        **plan,
        "mode": "apply",
        "status": "pass" if not failures else "fail",
        "application": {
            "removed": removed,
            "skipped": skipped,
            "failures": failures,
        },
        "secret_values_recorded": False,
    }


def _write_receipt(path: Path, result: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Retain bounded PropertyQuarry local runtime image history."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-web-image")
    parser.add_argument("--expected-render-image")
    parser.add_argument("--keep-previous", type=int, default=1)
    parser.add_argument("--write", type=Path)
    args = parser.parse_args(argv)

    if args.apply and (
        not args.expected_web_image or not args.expected_render_image
    ):
        parser.error("--apply requires both expected live image ids")

    expected = _expected_images(
        expected_web_image=args.expected_web_image,
        expected_render_image=args.expected_render_image,
    )
    try:
        plan = build_plan(
            _local_image_rows(),
            active_image_ids=_active_image_ids(),
            expected_images=expected,
            keep_previous=args.keep_previous,
        )
    except ValueError as error:
        parser.error(str(error))
    result = apply_plan(plan) if args.apply else plan
    if args.write:
        _write_receipt(args.write, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
