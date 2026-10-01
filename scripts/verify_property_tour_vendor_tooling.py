#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ea.property_render_ffmpeg_validator import (
    audit_ffmpeg_encoder as _ffmpeg_encoder_capability,
    capture_container_tool as _capture_container_tool,
    capture_local_tool as _capture_local_tool,
)
from scripts.property_magicfit_env import default_magicfit_env_files, discover_magicfit_env
from scripts.discover_property_tour_exports import build_discovery_receipt
from scripts.property_tour_runtime_paths import preferred_public_tour_root


INSTALLER_PATTERNS = (
    "Pano2VR*.exe",
    "Pano2VR*.msi",
    "pano2vr*.exe",
    "pano2vr*.msi",
    "3DVista*.exe",
    "3DVista*.msi",
    "3DVista*.dmg",
    "3DVVirtualTour*.exe",
    "3DVVirtualTour*.msi",
    "VirtualTour*.exe",
    "VirtualTour*.msi",
)
OFFICIAL_INSTALLER_SOURCES = {
    "3dvista": {
        "product": "3DVista VT Pro",
        "download_page": "https://www.3dvista.com/en/download/",
        "account_page": "https://cloud.3dvista.com",
        "operator_note": "Use the owned 3DVista account to download/export; keep private credentials out of tracked receipts.",
    },
    "pano2vr": {
        "product": "Pano2VR 8 Pro",
        "download_page": "https://ggnome.com/pano2vr-download/",
        "account_page": "https://ggnome.com/account/",
        "operator_note": "The Garden Gnome download may be Cloudflare-challenged for headless curl; download with a browser if the host fetch returns 403.",
    },
}
MAGICFIT_RENDER_SCRIPT = "scripts/render_magicfit_property_flythrough.py"
OMAGIC_ADAPTER_SCRIPT = "scripts/render_omagic_property_model_walkthrough.py"
OMAGIC_ADAPTER_RUNTIME_SCRIPT = "/app/scripts/render_omagic_property_model_walkthrough.py"
MAGICFIT_CREDENTIAL_ENV_PAIRS = (
    ("PROPERTYQUARRY_MAGICFIT_EMAIL", "PROPERTYQUARRY_MAGICFIT_PASSWORD"),
    ("MAGICFIT_EMAIL", "MAGICFIT_PASSWORD"),
    ("CHUMMER_EA_MAGICFIT_EMAIL", "CHUMMER_EA_MAGICFIT_PASSWORD"),
)
MAGICFIT_RENDER_PYTHON_MODULES = ("playwright", "requests")
RUNTIME_GENERATOR_MODULE = "scripts.generate_property_reconstruction"
RUNTIME_DIRECT_GLB_SYMBOL = "_write_glb"
OMAGIC_MODEL_UPLOAD_ENABLE_ENV = "PROPERTYQUARRY_OMAGIC_MODEL_UPLOAD_ENABLED"
OMAGIC_RENDER_ENDPOINT_ENV_NAMES = (
    "PROPERTYQUARRY_OMAGIC_RENDER_ENDPOINT",
    "OMAGIC_RENDER_ENDPOINT",
    "PROPERTYQUARRY_MAGIC_RENDER_ENDPOINT",
    "MAGIC_RENDER_ENDPOINT",
)
OMAGIC_RENDER_COMMAND_ENV_NAMES = (
    "PROPERTYQUARRY_OMAGIC_RENDER_COMMAND",
    "OMAGIC_RENDER_COMMAND",
    "PROPERTYQUARRY_MAGIC_RENDER_COMMAND",
    "MAGIC_RENDER_COMMAND",
)
OMAGIC_CREDENTIAL_ENV_NAMES = (
    "OMAGIC_API_KEY",
    "PROPERTYQUARRY_OMAGIC_API_KEY",
    "MAGIC_API_KEY",
    "PROPERTYQUARRY_MAGIC_API_KEY",
    "OMAGIC_ACCOUNTS_JSON",
    "PROPERTYQUARRY_OMAGIC_ACCOUNTS_JSON",
    "MAGIC_ACCOUNTS_JSON",
    "PROPERTYQUARRY_MAGIC_ACCOUNTS_JSON",
)


def _shared_scene_video_env_path(repo_root: Path) -> Path:
    configured = str(os.getenv("PROPERTYQUARRY_SCENE_VIDEO_SHARED_ENV_FILE") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return repo_root / "state" / "runtime" / "property_scene_video_shared.env"


def _load_shared_scene_video_env(repo_root: Path) -> tuple[Path, dict[str, str]]:
    shared_env_path = _shared_scene_video_env_path(repo_root)
    if not shared_env_path.is_file():
        return shared_env_path, {}
    try:
        from scripts.property_scene_video_shared_env import load_shared_env
    except Exception:
        return shared_env_path, {}
    try:
        applied = load_shared_env(shared_env_path, override=False)
    except Exception:
        return shared_env_path, {}
    return shared_env_path, dict(applied)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_drop_dir() -> Path:
    return Path(
        os.getenv("PROPERTYQUARRY_TOUR_EXPORT_DROP_DIR")
        or os.getenv("PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR")
        or _repo_root() / "state" / "incoming_property_tours"
    ).expanduser()


def _default_tour_root() -> Path:
    return preferred_public_tour_root(
        configured_root=os.getenv("EA_PUBLIC_TOUR_DIR") or "",
        repo_root=_repo_root(),
        fallback_root=_repo_root() / "state" / "public_property_tours",
        runtime_container=os.getenv("PROPERTYQUARRY_RUNTIME_CONTAINER") or "",
    )


def _default_wine_prefix() -> Path:
    return Path(os.getenv("PROPERTYQUARRY_PANO2VR_WINEPREFIX") or _repo_root() / "state" / "wine-pano2vr").expanduser()


def _command_version(command: str, *args: str, env: dict[str, str] | None = None) -> dict[str, object]:
    executable = shutil.which(command)
    if not executable:
        return {"available": False, "path": "", "version": ""}
    version = ""
    try:
        command_env = os.environ.copy()
        if env:
            command_env.update(env)
        completed = subprocess.run(
            [executable, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
            env=command_env,
        )
        version = (completed.stdout or completed.stderr or "").strip().splitlines()[0][:160]
    except Exception:
        version = ""
    return {"available": True, "path": executable, "version": version}


def _container_command_available(container: str, command: str) -> dict[str, object]:
    if not container:
        return {"available": False, "container": "", "path": ""}
    docker = shutil.which("docker")
    if not docker:
        return {"available": False, "container": container, "path": "", "reason": "docker_missing"}
    try:
        completed = subprocess.run(
            [docker, "exec", container, "sh", "-lc", f"command -v {command}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception as exc:
        return {"available": False, "container": container, "path": "", "reason": type(exc).__name__}
    path = (completed.stdout or "").strip().splitlines()[0] if completed.stdout.strip() else ""
    return {
        "available": completed.returncode == 0 and bool(path),
        "container": container,
        "path": path,
        "returncode": int(completed.returncode),
    }


def _container_python_import_available(container: str, module: str) -> dict[str, object]:
    if not container:
        return {"available": False, "container": "", "module": module}
    docker = shutil.which("docker")
    if not docker:
        return {"available": False, "container": container, "module": module, "reason": "docker_missing"}
    script = (
        "import importlib; "
        f"candidate = importlib.import_module({module!r}); "
        "print(getattr(candidate, '__version__', 'available'))"
    )
    try:
        completed = subprocess.run(
            [docker, "exec", container, "/usr/local/bin/python", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception as exc:
        return {"available": False, "container": container, "module": module, "reason": type(exc).__name__}
    return {
        "available": completed.returncode == 0,
        "container": container,
        "module": module,
        "version": (completed.stdout or completed.stderr or "").strip().splitlines()[0][:120] if (completed.stdout or completed.stderr or "").strip() else "",
        "returncode": int(completed.returncode),
    }


def _python_symbol_status(module: str, symbol: str) -> dict[str, object]:
    script = (
        "import importlib; "
        f"candidate = importlib.import_module({module!r}); "
        f"raise SystemExit(0 if callable(getattr(candidate, {symbol!r}, None)) else 1)"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception as exc:
        return {
            "available": False,
            "path": sys.executable,
            "module": module,
            "symbol": symbol,
            "reason": type(exc).__name__,
        }
    return {
        "available": completed.returncode == 0,
        "path": sys.executable,
        "module": module,
        "symbol": symbol,
        "implementation": "direct_python_glb_writer",
        "returncode": int(completed.returncode),
    }


def _container_python_symbol_available(container: str, module: str, symbol: str) -> dict[str, object]:
    if not container:
        return {
            "available": False,
            "container": "",
            "module": module,
            "symbol": symbol,
        }
    docker = shutil.which("docker")
    if not docker:
        return {
            "available": False,
            "container": container,
            "module": module,
            "symbol": symbol,
            "reason": "docker_missing",
        }
    script = (
        "import importlib; "
        f"candidate = importlib.import_module({module!r}); "
        f"raise SystemExit(0 if callable(getattr(candidate, {symbol!r}, None)) else 1)"
    )
    try:
        completed = subprocess.run(
            [
                docker,
                "exec",
                container,
                "/usr/local/bin/python",
                "-c",
                script,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception as exc:
        return {
            "available": False,
            "container": container,
            "module": module,
            "symbol": symbol,
            "reason": type(exc).__name__,
        }
    return {
        "available": completed.returncode == 0,
        "container": container,
        "module": module,
        "symbol": symbol,
        "implementation": "direct_python_glb_writer",
        "returncode": int(completed.returncode),
    }


def _container_file_available(container: str, path: str) -> dict[str, object]:
    if not container:
        return {"available": False, "container": "", "path": path}
    docker = shutil.which("docker")
    if not docker:
        return {"available": False, "container": container, "path": path, "reason": "docker_missing"}
    script = (
        "from pathlib import Path; "
        f"raise SystemExit(0 if Path({path!r}).is_file() else 1)"
    )
    try:
        completed = subprocess.run(
            [
                docker,
                "exec",
                container,
                "/usr/local/bin/python",
                "-c",
                script,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception as exc:
        return {"available": False, "container": container, "path": path, "reason": type(exc).__name__}
    return {
        "available": completed.returncode == 0,
        "container": container,
        "path": path,
        "returncode": int(completed.returncode),
    }


def _python_module_status(module: str) -> dict[str, object]:
    try:
        completed = subprocess.run(
            [sys.executable, "-c", f"import {module}; print(getattr({module}, '__version__', 'available'))"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception as exc:
        return {"available": False, "path": sys.executable, "version": "", "reason": type(exc).__name__}
    return {
        "available": completed.returncode == 0,
        "path": sys.executable,
        "version": (completed.stdout or completed.stderr or "").strip().splitlines()[0][:120] if (completed.stdout or completed.stderr or "").strip() else "",
        "returncode": int(completed.returncode),
    }


def _python_module_available(module: str) -> bool:
    return bool(_python_module_status(module).get("available"))


def _credential_pair_present(values: dict[str, str]) -> bool:
    return any(values.get(email_key, "").strip() and values.get(password_key, "").strip() for email_key, password_key in MAGICFIT_CREDENTIAL_ENV_PAIRS)


def _magicfit_renderer_receipt(repo_root: Path, *, shared_env_path: Path | None = None) -> dict[str, object]:
    script_path = (repo_root / MAGICFIT_RENDER_SCRIPT).resolve()
    env_files = tuple(
        dict.fromkeys(
            str(path)
            for path in (
                repo_root / ".env",
                *((shared_env_path,) if shared_env_path is not None and shared_env_path.is_file() else ()),
                *default_magicfit_env_files(),
            )
        )
    )
    env_file_paths = tuple(Path(path).expanduser() for path in env_files)
    discovered_values, discovered_sources = discover_magicfit_env(env_file_paths)
    credential_sources: list[str] = []
    if _credential_pair_present(discovered_values):
        for email_key, password_key in MAGICFIT_CREDENTIAL_ENV_PAIRS:
            email_source = discovered_sources.get(email_key, "")
            password_source = discovered_sources.get(password_key, "")
            if email_source and password_source:
                credential_sources.extend([email_source, password_source])
                break
    elif _credential_pair_present({key: str(value) for key, value in os.environ.items()}):
        credential_sources.append("process_env")
    env_files_checked = [str(path.resolve()) for path in env_file_paths]
    deduped_sources = list(dict.fromkeys(credential_sources))
    python_modules = {
        module: _python_module_status(module)
        for module in MAGICFIT_RENDER_PYTHON_MODULES
    }
    python_modules_ready = all(bool(row.get("available")) for row in python_modules.values())
    credentials_configured = bool(deduped_sources)
    script_ready = script_path.is_file()
    ready = script_ready and credentials_configured and python_modules_ready
    missing_python_modules = [
        module
        for module, status in python_modules.items()
        if not bool(status.get("available"))
    ]
    if not script_ready:
        next_action = f"restore {MAGICFIT_RENDER_SCRIPT} before claiming MagicFit walkthrough readiness"
    elif not credentials_configured:
        next_action = (
            "configure the MagicFit login pair in /docker/property/.env "
            "or the current process environment before expecting MagicFit walkthrough renders"
        )
    elif missing_python_modules:
        next_action = (
            "install the missing Python modules for the MagicFit render lane: "
            + ", ".join(missing_python_modules)
        )
    else:
        next_action = ""
    return {
        "status": "pass" if ready else "blocked_configuration",
        "script_path": str(script_path),
        "script_ready": script_ready,
        "credentials_configured": credentials_configured,
        "credential_sources": deduped_sources,
        "env_files_checked": env_files_checked,
        "python_modules_ready": python_modules_ready,
        "python_modules": python_modules,
        "ready": ready,
        "next_action": next_action,
        "note": "Only readiness signals are recorded here; MagicFit sign-in material and session secrets are intentionally omitted.",
    }


def _configured_env_names(names: tuple[str, ...]) -> list[str]:
    return [name for name in names if str(os.getenv(name) or "").strip()]


def _truthy_env(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _omagic_adapter_receipt(
    repo_root: Path,
    *,
    runtime_container: str = "",
    runtime_only: bool = False,
) -> dict[str, object]:
    script_path = (repo_root / OMAGIC_ADAPTER_SCRIPT).resolve()
    script_ready = script_path.is_file()
    runtime_checked = bool(runtime_only or str(runtime_container or "").strip())
    if runtime_container:
        runtime_script = _container_file_available(runtime_container, OMAGIC_ADAPTER_RUNTIME_SCRIPT)
        runtime_script["checked_via"] = "docker_exec"
    elif runtime_only:
        runtime_script = {
            "available": script_ready,
            "container": "current_runtime",
            "path": str(script_path),
            "checked_via": "local_runtime_filesystem",
        }
    else:
        runtime_script = {
            "available": None,
            "container": "",
            "path": OMAGIC_ADAPTER_RUNTIME_SCRIPT,
            "checked_via": "not_checked",
        }
    endpoint_env_names = _configured_env_names(OMAGIC_RENDER_ENDPOINT_ENV_NAMES)
    command_env_names = _configured_env_names(OMAGIC_RENDER_COMMAND_ENV_NAMES)
    credential_env_names = _configured_env_names(OMAGIC_CREDENTIAL_ENV_NAMES)
    target_configured = bool(endpoint_env_names or command_env_names)
    adapter_enabled = _truthy_env(OMAGIC_MODEL_UPLOAD_ENABLE_ENV)
    runtime_script_ready = runtime_script.get("available")
    source_ready = script_ready
    deployed_ready = (not runtime_checked) or runtime_script_ready is True
    ready = bool(source_ready and deployed_ready)
    if not source_ready:
        status = "blocked_source_script_missing"
        next_action = f"restore {OMAGIC_ADAPTER_SCRIPT} before claiming OMagic model-upload adapter packaging"
    elif runtime_checked and runtime_script_ready is not True:
        status = "blocked_runtime_script_missing"
        next_action = (
            "rebuild/redeploy the PropertyQuarry runtime image so "
            f"{OMAGIC_ADAPTER_RUNTIME_SCRIPT} exists before claiming OMagic adapter availability"
        )
    elif not runtime_checked:
        status = "source_ready_runtime_not_checked"
        next_action = "run this verifier from the runtime container or pass --runtime-container before claiming deployed OMagic adapter availability"
    else:
        status = "pass"
        next_action = ""
    return {
        "status": status,
        "ready": ready,
        "script_path": str(script_path),
        "script_ready": script_ready,
        "runtime_checked": runtime_checked,
        "runtime_script_ready": runtime_script_ready,
        "runtime_script": runtime_script,
        "model_upload_enable_env": OMAGIC_MODEL_UPLOAD_ENABLE_ENV,
        "model_upload_adapter_enabled": adapter_enabled,
        "render_endpoint_env_names": endpoint_env_names,
        "render_command_env_names": command_env_names,
        "render_target_configured": target_configured,
        "credential_env_names": credential_env_names,
        "next_action": next_action,
        "note": "This is package/deploy evidence only; scene-video readiness owns credentials, endpoint/command, enablement, and proof-render truth.",
    }


def _installer_search_roots(extra_roots: list[str]) -> list[Path]:
    roots = [
        _repo_root() / "state" / "vendor_installers",
        _repo_root() / "state" / "incoming_property_tours",
        Path.home() / "Downloads",
    ]
    roots.extend(Path(value).expanduser() for value in extra_roots if str(value or "").strip())
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            deduped.append(root)
            seen.add(key)
    return deduped


def _find_installers(roots: list[Path]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in INSTALLER_PATTERNS:
            for path in sorted(root.rglob(pattern)):
                if not path.is_file():
                    continue
                lowered_name = path.name.lower()
                provider = "pano2vr" if "pano2vr" in lowered_name else "3dvista"
                rows.append(
                    {
                        "provider": provider,
                        "path": str(path.resolve()),
                        "size_bytes": path.stat().st_size,
                    }
                )
    return rows


def _installed_app_search_roots(wine_prefix: Path) -> list[Path]:
    roots = [
        _repo_root() / "state" / "vendor_apps" / "3dvista",
        _repo_root() / "state" / "vendor_apps" / "pano2vr",
        _repo_root() / "state" / "wine-3dvista",
        _repo_root() / "state" / "wine-pano2vr",
        wine_prefix,
    ]
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            deduped.append(root)
            seen.add(key)
    return deduped


def _find_installed_apps(roots: list[Path]) -> list[dict[str, object]]:
    provider_patterns = {
        "pano2vr": ("Pano2VR*.exe", "pano2vr*.exe"),
        "3dvista": ("3DVista*.exe", "VirtualTour*.exe", "3DVVirtualTour*.exe"),
    }
    rows: list[dict[str, object]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for provider, patterns in provider_patterns.items():
            for pattern in patterns:
                for path in sorted(root.glob(pattern)):
                    if not path.is_file():
                        continue
                    rows.append(
                        {
                            "provider": provider,
                            "path": str(path.resolve()),
                            "size_bytes": path.stat().st_size,
                            "layout": "portable_extract",
                        }
                    )
        for program_root_name in ("Program Files", "Program Files (x86)"):
            program_root = root / "drive_c" / program_root_name
            if not program_root.is_dir():
                continue
            for provider, patterns in provider_patterns.items():
                for pattern in patterns:
                    for path in sorted(program_root.rglob(pattern)):
                        if not path.is_file():
                            continue
                        rows.append(
                            {
                                "provider": provider,
                                "path": str(path.resolve()),
                                "size_bytes": path.stat().st_size,
                                "layout": "wine_program_files",
                            }
                        )
        direct_drive_c = root / "drive_c"
        if direct_drive_c.is_dir():
            for provider, patterns in provider_patterns.items():
                for pattern in patterns:
                    for path in sorted(direct_drive_c.glob(f"*/{pattern}")):
                        if not path.is_file():
                            continue
                        rows.append(
                            {
                                "provider": provider,
                                "path": str(path.resolve()),
                                "size_bytes": path.stat().st_size,
                                "layout": "wine_drive_c_app",
                            }
                        )
    return rows


def _provider_counts(rows: list[dict[str, object]], providers: tuple[str, ...]) -> dict[str, int]:
    counts = {provider: 0 for provider in providers}
    for row in rows:
        provider = str(row.get("provider") or "").strip().lower()
        if provider in counts:
            counts[provider] += 1
    return counts


def _installer_source_rows(providers: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for provider in providers:
        source = OFFICIAL_INSTALLER_SOURCES.get(provider)
        if not source:
            continue
        rows.append(
            {
                "provider": provider,
                "product": str(source["product"]),
                "download_page": str(source["download_page"]),
                "account_page": str(source["account_page"]),
                "operator_note": str(source["operator_note"]),
            }
        )
    return rows


def _provider_ready_counts(discovery: dict[str, Any]) -> dict[str, int]:
    counts = {"3dvista": 0, "pano2vr": 0}
    for row in list(discovery.get("imports") or []):
        if not isinstance(row, dict):
            continue
        provider = str(row.get("provider") or "").strip().lower()
        if provider in counts:
            counts[provider] += 1
    return counts


def _safe_bundle_relpath(value: object, default: str = "") -> str:
    relpath = str(value or default).strip().replace("\\", "/")
    while relpath.startswith("./"):
        relpath = relpath[2:]
    return relpath.strip("/")


def _resolve_under(root: Path, relpath: str) -> Path | None:
    if not relpath:
        return None
    candidate = (root / relpath).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _live_bundle_verified_export_counts(tour_root: Path) -> dict[str, int]:
    counts = {"3dvista": 0, "pano2vr": 0}
    if not tour_root.is_dir():
        return counts
    for bundle_dir in sorted(tour_root.iterdir()):
        if not bundle_dir.is_dir():
            continue
        manifest_path = bundle_dir / "tour.json"
        if not manifest_path.is_file():
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        three_d_vista_proof = payload.get("three_d_vista_white_label_proof")
        if isinstance(three_d_vista_proof, dict):
            export_root = _resolve_under(
                bundle_dir,
                _safe_bundle_relpath(payload.get("three_d_vista_export_root_relpath"), "3dvista"),
            )
            entry_path = _resolve_under(
                bundle_dir,
                _safe_bundle_relpath(payload.get("three_d_vista_entry_relpath")),
            )
            if (
                export_root
                and export_root.is_dir()
                and entry_path
                and entry_path.is_file()
                and bool(three_d_vista_proof.get("private_viewer_verified"))
                and bool(three_d_vista_proof.get("non_trial_export_verified"))
                and not bool(three_d_vista_proof.get("trial_branding_present"))
            ):
                counts["3dvista"] += 1

        pano2vr_import = payload.get("pano2vr_import")
        if isinstance(pano2vr_import, dict):
            entry_path = _resolve_under(
                bundle_dir,
                _safe_bundle_relpath(payload.get("pano2vr_entry_relpath")),
            )
            export_root = entry_path.parent if entry_path is not None else _resolve_under(bundle_dir, "pano2vr")
            runtime_markers = ("pano.xml", "pano2vr_player.js", "gginfo.json", "tour.js")
            if (
                export_root
                and export_root.is_dir()
                and entry_path
                and entry_path.is_file()
                and any((export_root / marker).is_file() for marker in runtime_markers)
            ):
                counts["pano2vr"] += 1
    return counts


def build_vendor_tooling_receipt(
    *,
    drop_dir: Path,
    tour_root: Path,
    wine_prefix: Path,
    installer_roots: list[Path],
    installed_app_roots: list[Path] | None = None,
    runtime_container: str = "",
    runtime_only: bool = False,
) -> dict[str, Any]:
    repo_root = _repo_root()
    shared_env_path = _shared_scene_video_env_path(repo_root)
    discovery = build_discovery_receipt(drop_dir=drop_dir, public_tour_dir=tour_root)
    discovery_provider_ready_counts = _provider_ready_counts(discovery)
    live_bundle_provider_ready_counts = _live_bundle_verified_export_counts(tour_root)
    provider_ready_counts = {
        provider: discovery_provider_ready_counts.get(provider, 0) + live_bundle_provider_ready_counts.get(provider, 0)
        for provider in ("3dvista", "pano2vr")
    }
    installers = [] if runtime_only else _find_installers(installer_roots)
    installed_apps = [] if runtime_only else _find_installed_apps(installed_app_roots if installed_app_roots is not None else _installed_app_search_roots(wine_prefix))
    installer_counts = _provider_counts(installers, ("3dvista", "pano2vr"))
    installed_app_counts = _provider_counts(installed_apps, ("3dvista", "pano2vr"))
    wine = {"available": False, "path": "", "version": "", "skipped": "runtime_only"} if runtime_only else _command_version("wine", "--version")
    wine64 = {"available": False, "path": "", "version": "", "skipped": "runtime_only"} if runtime_only else _command_version("wine64", "--version")
    xvfb = {"available": False, "path": "", "version": "", "skipped": "runtime_only"} if runtime_only else _command_version("xvfb-run", "--help")
    winetricks = {"available": False, "path": "", "version": "", "skipped": "runtime_only"} if runtime_only else _command_version("winetricks", "--version")
    krpano = _command_version("krpanotools", "version")
    blender = _command_version("blender", "--version")
    colmap = _command_version("colmap", "-h")
    meshlabserver = _command_version("meshlabserver", "-h", env={"QT_QPA_PLATFORM": "offscreen"})
    ffmpeg = _command_version("ffmpeg", "-version")
    exiftool = _command_version("exiftool", "-ver")
    imagemagick = _command_version("magick", "-version")
    if not imagemagick.get("available"):
        imagemagick = _command_version("convert", "-version")
    wine_prefix_ready = False if runtime_only else wine_prefix.is_dir() and (wine_prefix / "system.reg").is_file()
    wine_runtime_ready = bool(wine.get("available")) or bool(wine64.get("available"))
    host_ready = None if runtime_only else wine_runtime_ready and bool(xvfb.get("available")) and wine_prefix_ready
    host_generated_tour_tools = {
        "krpanotools": krpano,
        "blender": blender,
        "colmap": colmap,
        "meshlabserver": meshlabserver,
        "ffmpeg": ffmpeg,
        "exiftool": exiftool,
        "imagemagick": imagemagick,
    }
    local_ffmpeg_encoder = _ffmpeg_encoder_capability(
        _capture_local_tool,
        require_bounded_surface=runtime_only,
    )
    local_ffmpeg_capability_name = (
        "ffmpeg:bounded_encoder" if runtime_only else "ffmpeg:functional_encoder"
    )
    runtime_local_tools = {
        local_ffmpeg_capability_name: local_ffmpeg_encoder,
        "python:PIL": _python_module_status("PIL"),
        "python:playwright": _python_module_status("playwright"),
        "python:direct_glb": _python_symbol_status(
            RUNTIME_GENERATOR_MODULE,
            RUNTIME_DIRECT_GLB_SYMBOL,
        ),
    }
    generated_tour_readiness_capabilities = {
        name: {
            **row,
            "affects_runtime_readiness": True,
            "observation_posture": "retained_runtime_capability",
        }
        for name, row in runtime_local_tools.items()
    }
    legacy_generated_tour_observations = {
        name: {
            **row,
            "affects_runtime_readiness": False,
            "observation_posture": "legacy_host_observation_only",
        }
        for name, row in host_generated_tour_tools.items()
    }
    generated_tour_tools = {
        **generated_tour_readiness_capabilities,
        **legacy_generated_tour_observations,
    }
    generated_tour_ready = all(
        bool(row.get("available")) for row in runtime_local_tools.values()
    )
    runtime_generated_tour_tools: dict[str, dict[str, object]] = {}
    if runtime_container and not runtime_only:
        runtime_generated_tour_tools = {
            "ffmpeg:bounded_encoder": _ffmpeg_encoder_capability(
                lambda command, *args: _capture_container_tool(
                    runtime_container,
                    command,
                    *args,
                ),
                require_bounded_surface=True,
            ),
            "python:PIL": _container_python_import_available(runtime_container, "PIL"),
            "python:playwright": _container_python_import_available(runtime_container, "playwright"),
            "python:direct_glb": _container_python_symbol_available(
                runtime_container,
                RUNTIME_GENERATOR_MODULE,
                RUNTIME_DIRECT_GLB_SYMBOL,
            ),
        }
    if runtime_only:
        runtime_generated_tour_tools = runtime_local_tools
    runtime_generated_tour_ready = (
        all(bool(row.get("available")) for row in runtime_generated_tour_tools.values())
        if runtime_generated_tour_tools
        else None
    )
    magicfit_renderer = _magicfit_renderer_receipt(repo_root, shared_env_path=shared_env_path)
    _load_shared_scene_video_env(repo_root)
    omagic_adapter = _omagic_adapter_receipt(
        repo_root,
        runtime_container=runtime_container,
        runtime_only=runtime_only,
    )
    missing_exports = [
        provider
        for provider in ("3dvista", "pano2vr")
        if provider_ready_counts.get(provider, 0) <= 0
    ]
    next_actions: list[dict[str, object]] = []
    if not runtime_only and not host_ready:
        next_actions.append(
            {
                "area": "host_tooling",
                "action": "install wine64, wine32, winetricks, xvfb-run and initialize PROPERTYQUARRY_PANO2VR_WINEPREFIX",
            }
        )
    if not generated_tour_ready:
        missing_tools = [
            name
            for name, row in generated_tour_readiness_capabilities.items()
            if not row.get("available")
        ]
        next_actions.append(
            {
                "area": "runtime_generated_tour_tooling" if runtime_only else "generated_tour_tooling",
                "missing_tools": missing_tools,
                "action": "restore the missing retained runtime generation capabilities before claiming floorplan/photos-to-tour readiness" if runtime_only else "restore the missing local generation capabilities before claiming floorplan/photos-to-tour readiness",
            }
        )
    if runtime_generated_tour_ready is False and not runtime_only:
        missing_runtime_tools = [name for name, row in runtime_generated_tour_tools.items() if not row.get("available")]
        next_actions.append(
            {
                "area": "runtime_generated_tour_tooling",
                "container": runtime_container,
                "missing_tools": missing_runtime_tools,
                "action": "rebuild the render runtime with its bounded encoder, Python imaging/browser, and direct-GLB capabilities",
            }
        )
    if not bool(magicfit_renderer.get("ready")):
        next_actions.append(
            {
                "area": "magicfit_renderer",
                "script_ready": bool(magicfit_renderer.get("script_ready")),
                "credentials_configured": bool(magicfit_renderer.get("credentials_configured")),
                "python_modules_ready": bool(magicfit_renderer.get("python_modules_ready")),
                "credential_sources": list(magicfit_renderer.get("credential_sources") or []),
                "action": str(magicfit_renderer.get("next_action") or "configure the MagicFit render lane"),
            }
        )
    if bool(omagic_adapter.get("runtime_checked")) and not bool(omagic_adapter.get("ready")):
        next_actions.append(
            {
                "area": "omagic_model_upload_adapter_deploy",
                "status": str(omagic_adapter.get("status") or ""),
                "script_ready": bool(omagic_adapter.get("script_ready")),
                "runtime_script_ready": omagic_adapter.get("runtime_script_ready"),
                "runtime_script": dict(omagic_adapter.get("runtime_script") or {}),
                "action": str(omagic_adapter.get("next_action") or "deploy the OMagic model-upload adapter before claiming provider parity"),
            }
        )
    missing_installers = [
        provider
        for provider in ("3dvista", "pano2vr")
        if not runtime_only and installer_counts.get(provider, 0) <= 0 and provider_ready_counts.get(provider, 0) <= 0
    ]
    if missing_installers:
        next_actions.append(
            {
                "area": "vendor_installers",
                "missing_providers": missing_installers,
                "action": "download official desktop installers into state/vendor_installers or provide complete verified exports",
                "official_sources": _installer_source_rows(missing_installers),
            }
        )
    missing_installed_apps = [
        provider
        for provider in ("3dvista", "pano2vr")
        if not runtime_only
        and installer_counts.get(provider, 0) > 0
        and installed_app_counts.get(provider, 0) <= 0
        and provider_ready_counts.get(provider, 0) <= 0
    ]
    if missing_installed_apps:
        next_actions.append(
            {
                "area": "vendor_desktop_apps",
                "missing_providers": missing_installed_apps,
                "action": "install the cached official desktop app under Wine or provide a complete verified export",
            }
        )
    for provider in missing_exports:
        next_actions.append(
            {
                "area": "verified_export",
                "provider": provider,
                "action": f"place a complete verified {provider} export folder or zip into the prepared PropertyQuarry drop directory",
                "accepted_layouts": [
                    f"<drop>/<slug>/{provider}/",
                    f"<drop>/{provider}/<slug>/",
                    "or a zip file inside either folder",
                ],
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "mode": "runtime" if runtime_only else "operator_host",
        "status": "pass" if (runtime_only or host_ready) and generated_tour_ready and not missing_exports else "blocked_missing_verified_exports",
        "host_ready": host_ready,
        "generated_tour_ready": generated_tour_ready,
        "generated_tour_tools": generated_tour_tools,
        "generated_tour_readiness_capabilities": (
            generated_tour_readiness_capabilities
        ),
        "legacy_host_tool_observations": {
            "affects_runtime_readiness": False,
            "tools": legacy_generated_tour_observations,
        },
        "runtime_generated_tour_ready": runtime_generated_tour_ready,
        "runtime_generated_tour_tools": runtime_generated_tour_tools,
        "magicfit_renderer": magicfit_renderer,
        "omagic_adapter": omagic_adapter,
        "wine_runtime_ready": wine_runtime_ready,
        "wine": wine,
        "wine64": wine64,
        "xvfb_run": xvfb,
        "winetricks": winetricks,
        "wine_prefix": {
            "path": str(wine_prefix.resolve()),
            "ready": wine_prefix_ready,
        },
        "installer_count": len(installers),
        "installers": installers[:20],
        "installer_counts": installer_counts,
        "official_installer_sources": _installer_source_rows(["3dvista", "pano2vr"]),
        "installed_app_count": len(installed_apps),
        "installed_apps": installed_apps[:20],
        "installed_app_counts": installed_app_counts,
        "drop_dir": str(drop_dir.resolve()),
        "tour_root": str(tour_root.resolve()),
        "discovery_verified_export_ready_counts": discovery_provider_ready_counts,
        "live_bundle_verified_export_ready_counts": live_bundle_provider_ready_counts,
        "verified_export_ready_counts": provider_ready_counts,
        "missing_verified_exports": missing_exports,
        "discovery_status": str(discovery.get("status") or ""),
        "discovery_import_count": int(discovery.get("import_count") or 0),
        "discovery_rejected_count": int(discovery.get("rejected_count") or 0),
        "next_actions": next_actions,
        "note": (
            "Runtime readiness is based on the bounded FFmpeg encoder, Python "
            "imaging/browser modules, and the direct Python GLB writer. Operator-host "
            "FFmpeg is reported only as a functional encoder unless the runtime-only "
            "contract is selected. Legacy host tool identities are informational only; "
            "private credentials and invoice data are intentionally omitted."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify PropertyQuarry desktop vendor tooling readiness for 3DVista/Pano2VR.")
    parser.add_argument("--drop-dir", default="")
    parser.add_argument("--tour-root", default="")
    parser.add_argument("--wine-prefix", default="")
    parser.add_argument("--installer-root", action="append", default=[])
    parser.add_argument(
        "--runtime-container",
        default=(
            os.getenv("PROPERTYQUARRY_RENDER_CONTAINER_NAME")
            or os.getenv("PROPERTYQUARRY_RUNTIME_CONTAINER")
            or "propertyquarry-render-tools"
        ),
    )
    parser.add_argument("--runtime-only", action="store_true", help="Validate the current runtime container/process lane; skip desktop Wine/installers/apps.")
    parser.add_argument("--write", default="_completion/tours/property-tour-vendor-tooling-current.json")
    parser.add_argument("--fail-on-blocked", action="store_true")
    args = parser.parse_args()

    drop_dir = Path(args.drop_dir).expanduser() if str(args.drop_dir or "").strip() else _default_drop_dir()
    tour_root = Path(args.tour_root).expanduser() if str(args.tour_root or "").strip() else _default_tour_root()
    wine_prefix = Path(args.wine_prefix).expanduser() if str(args.wine_prefix or "").strip() else _default_wine_prefix()
    receipt = build_vendor_tooling_receipt(
        drop_dir=drop_dir,
        tour_root=tour_root,
        wine_prefix=wine_prefix,
        installer_roots=_installer_search_roots(args.installer_root),
        runtime_container=str(args.runtime_container or "").strip(),
        runtime_only=bool(args.runtime_only),
    )
    output = json.dumps(receipt, indent=2, sort_keys=True)
    if args.write:
        out_path = Path(args.write)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output + "\n", encoding="utf-8")
    print(output)
    if receipt.get("status") == "pass":
        return 0
    return 2 if args.fail_on_blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
