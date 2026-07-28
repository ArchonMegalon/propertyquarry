from __future__ import annotations

import hashlib
import re
import stat
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

from packaging.tags import Tag, parse_tag
from packaging.utils import canonicalize_name, parse_wheel_filename


ROOT = Path(__file__).resolve().parents[1]
BASE_LOCK = ROOT / "ea" / "requirements.lock"
HASH_LOCK = ROOT / "ea" / "requirements.wheelhouse.lock"
WHEELHOUSE = (
    ROOT / "vendor" / "propertyquarry-wheelhouse" / "cp312-linux-x86_64"
)
DOCKERFILE = ROOT / "ea" / "Dockerfile.property-web"
PIP_VERSION = "26.1.2"
HASH_LINE = re.compile(
    r"(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*)"
    r"==(?P<version>[^ \t]+)"
    r" --hash=sha256:(?P<digest>[0-9a-f]{64})\Z"
)


def _base_requirements() -> dict[str, str]:
    result: dict[str, str] = {}
    for line in BASE_LOCK.read_text(encoding="utf-8").splitlines():
        name, separator, version = line.partition("==")
        assert separator == "=="
        canonical = canonicalize_name(name)
        assert canonical not in result
        assert version
        result[canonical] = version
    return result


def _hash_requirements() -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for line in HASH_LOCK.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        match = HASH_LINE.fullmatch(line)
        assert match is not None
        name = match.group("name")
        assert name == canonicalize_name(name)
        assert name not in result
        result[name] = (match.group("version"), match.group("digest"))
    return result


def _target_compatible(tag: Tag) -> bool:
    if tag.interpreter == "py3" and tag.abi == "none":
        return tag.platform == "any" or (
            tag.platform.startswith("manylinux")
            and tag.platform.endswith("_x86_64")
        )
    if (
        tag.interpreter == "cp312"
        and tag.abi == "cp312"
        and tag.platform.startswith("manylinux")
        and tag.platform.endswith("_x86_64")
    ):
        return True
    return (
        tag.interpreter == "cp311"
        and tag.abi == "abi3"
        and tag.platform.startswith("manylinux")
        and tag.platform.endswith("_x86_64")
    )


def _wheel_metadata(path: Path) -> tuple[str, str, frozenset[Tag]]:
    seen: set[str] = set()
    metadata_entries: list[str] = []
    wheel_entries: list[str] = []
    aggregate_uncompressed = 0
    with zipfile.ZipFile(path) as archive:
        for entry in archive.infolist():
            assert not entry.flag_bits & 0x1
            assert entry.filename not in seen
            seen.add(entry.filename)
            candidate = PurePosixPath(entry.filename.removesuffix("/"))
            assert entry.filename
            assert "\\" not in entry.filename
            assert not candidate.is_absolute()
            assert all(part not in {"", ".", ".."} for part in candidate.parts)
            assert not stat.S_ISLNK(entry.external_attr >> 16)
            aggregate_uncompressed += entry.file_size
            assert aggregate_uncompressed <= 512 * 1024 * 1024
            if entry.filename.endswith(".dist-info/METADATA"):
                metadata_entries.append(entry.filename)
            if entry.filename.endswith(".dist-info/WHEEL"):
                wheel_entries.append(entry.filename)
        assert len(metadata_entries) == 1
        assert len(wheel_entries) == 1
        metadata_raw = archive.read(metadata_entries[0])
        wheel_raw = archive.read(wheel_entries[0])
    assert len(metadata_raw) <= 2 * 1024 * 1024
    assert len(wheel_raw) <= 64 * 1024
    metadata = BytesParser().parsebytes(metadata_raw)
    wheel = BytesParser().parsebytes(wheel_raw)
    names = metadata.get_all("Name", [])
    versions = metadata.get_all("Version", [])
    wheel_tags = wheel.get_all("Tag", [])
    assert len(names) == len(versions) == 1
    assert wheel_tags
    parsed_tags = frozenset(
        tag
        for value in wheel_tags
        for tag in parse_tag(value)
    )
    return canonicalize_name(names[0]), versions[0], parsed_tags


def test_wheelhouse_is_exact_hash_locked_and_target_compatible() -> None:
    expected = _base_requirements()
    expected["pip"] = PIP_VERSION
    locked = _hash_requirements()
    assert {name: version for name, (version, _digest) in locked.items()} == expected

    paths = sorted(WHEELHOUSE.iterdir())
    assert len(paths) == len(expected) == 37
    assert sum(path.stat().st_size for path in paths) < 128 * 1024 * 1024
    observed: dict[str, str] = {}
    for path in paths:
        metadata = path.lstat()
        assert path.suffix == ".whl"
        assert stat.S_ISREG(metadata.st_mode)
        assert not stat.S_ISLNK(metadata.st_mode)
        assert stat.S_IMODE(metadata.st_mode) == 0o644
        assert metadata.st_nlink == 1
        filename_name, filename_version, _build, filename_tags = (
            parse_wheel_filename(path.name)
        )
        metadata_name, metadata_version, metadata_tags = _wheel_metadata(path)
        name = canonicalize_name(filename_name)
        assert name == metadata_name
        assert str(filename_version) == metadata_version == expected[name]
        assert filename_tags
        assert all(_target_compatible(tag) for tag in filename_tags)
        assert metadata_tags
        assert all(_target_compatible(tag) for tag in metadata_tags)
        assert {
            (tag.interpreter, tag.abi) for tag in filename_tags
        } == {
            (tag.interpreter, tag.abi) for tag in metadata_tags
        }
        assert name not in observed
        observed[name] = hashlib.sha256(path.read_bytes()).hexdigest()

    assert observed == {
        name: digest
        for name, (_version, digest) in locked.items()
    }


def test_web_build_installs_only_the_offline_hash_locked_wheelhouse() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    install_step = dockerfile.split(
        "COPY vendor/propertyquarry-wheelhouse/cp312-linux-x86_64 /wheelhouse",
        1,
    )[1].split("RUN mkdir -p", 1)[0]

    assert "COPY ea/requirements.wheelhouse.lock" in dockerfile
    assert "--no-index" in install_step
    assert "--find-links=/wheelhouse" in install_step
    assert "--require-hashes" in install_step
    assert "--requirement /app/requirements.wheelhouse.lock" in install_step
    assert "pip install --no-cache-dir --upgrade" not in dockerfile
    assert "https://" not in install_step
    assert "http://" not in install_step
    assert "python -m pip uninstall --yes pip" in install_step
    assert "rm -rf /wheelhouse" in install_step
