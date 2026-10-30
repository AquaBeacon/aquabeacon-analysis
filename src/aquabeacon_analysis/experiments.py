"""Experiment registry and bounded, explicit selection of analysis inputs."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

EXPERIMENT_ID_RE = re.compile(r"\d{8}_[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
RAW_ROLES = ("C_MAIN", "P_MAIN", "Spresense", "P_MAIN_CAM", "C_MAIN_CAM")


def validate_experiment_id(value: str) -> str:
    if not isinstance(value, str) or not EXPERIMENT_ID_RE.fullmatch(value):
        raise ValueError("experiment ID must be YYYYMMDD_lowercase_name (for example 20261027_pool_001)")
    date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:8]}")
    return value


def repository_path(repository: Path, value: str) -> Path:
    """Resolve portable registry paths, including symlinks, within this checkout."""
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError(f"expected a repository-relative path using '/': {value!r}")
    relative = Path(value)
    root = repository.resolve()
    resolved = (root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not resolved.is_relative_to(root):
        raise ValueError(f"path escapes repository: {value!r}")
    return resolved


@dataclass(frozen=True)
class Experiment:
    repository: Path
    metadata_path: Path
    metadata: dict

    @property
    def experiment_id(self) -> str:
        return self.metadata["experiment_id"]

    @property
    def raw_root(self) -> Path:
        return repository_path(self.repository, self.metadata["raw_root"])

    def stage_path(self, stage: str) -> Path:
        if stage not in {"interim", "processed", "results"}:
            raise ValueError(f"unknown stage: {stage}")
        prefix = stage if stage == "results" else f"data/{stage}"
        return repository_path(self.repository, f"{prefix}/{self.experiment_id}")


def load_experiment(repository: Path, experiment_id: str) -> Experiment:
    validate_experiment_id(experiment_id)
    repository = repository.resolve()
    path = repository_path(repository, f"experiments/{experiment_id}/experiment.json")
    if path != repository / "experiments" / experiment_id / "experiment.json":
        raise ValueError(f"registry directory is redirected: {path}")
    metadata = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or metadata.get("schema_version") != 1:
        raise ValueError(f"unsupported experiment registry schema: {path}")
    if metadata.get("experiment_id") != experiment_id:
        raise ValueError(f"experiment ID differs from its registry directory: {path}")
    raw = repository_path(repository, metadata.get("raw_root"))
    raw_parent = repository_path(repository, "data/raw")
    if not raw.is_relative_to(raw_parent):
        raise ValueError("raw_root must be inside data/raw")
    for field in ("c_main_suffix", "p_main_suffix"):
        suffix = metadata.get(field)
        if suffix is not None and (not isinstance(suffix, str)
                                   or not re.fullmatch(r"\d{5}", suffix)):
            raise ValueError(f"{field} must be a five-digit string or null")
    for field in ("config_paths", "calibration_paths", "raw_sources"):
        references = metadata.get(field, [])
        if not isinstance(references, list):
            raise TypeError(f"{field} must be a list of repository-relative paths")
        for reference in references:
            resolved = repository_path(repository, reference)
            if field == "raw_sources" and not resolved.is_relative_to(raw_parent):
                raise ValueError("raw_sources must be inside data/raw")
    # Resolve output paths before any processing, rejecting junctions into raw or another run.
    experiment = Experiment(repository, path, metadata)
    for stage in ("interim", "processed", "results"):
        expected_parent = repository / (stage if stage == "results" else f"data/{stage}")
        output = experiment.stage_path(stage)
        if output.parent != expected_parent or output.is_relative_to(raw_parent):
            raise ValueError(f"output directory is redirected: {output}")
    return experiment


def initialize_experiment(repository: Path, experiment_id: str,
                          c_main_suffix: str | None = None,
                          p_main_suffix: str | None = None) -> Experiment:
    """Register a new experiment without touching raw data or existing outputs."""
    validate_experiment_id(experiment_id)
    suffixes = {}
    for name, suffix in (("c_main_suffix", c_main_suffix), ("p_main_suffix", p_main_suffix)):
        if suffix is not None:
            if not suffix.isascii() or not suffix.isdigit() or len(suffix) > 5:
                raise ValueError(f"{name} must contain one to five digits")
            suffix = suffix.zfill(5)
        suffixes[name] = suffix
    root = repository.resolve()
    metadata_dir = repository_path(root, f"experiments/{experiment_id}")
    metadata = {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "description": "",
        "raw_root": f"data/raw/{experiment_id}",
        "raw_sources": [f"data/raw/{experiment_id}/{role}" for role in RAW_ROLES],
        **suffixes,
        "config_paths": [],
        "calibration_paths": [],
        "notes": ["Register the actual settings and calibration before analysis."],
    }
    # Validate all destinations first. Existing run IDs must never be silently adopted.
    expected = [root / prefix / experiment_id
                for prefix in ("experiments", "data/interim", "data/processed", "results")]
    destinations = [repository_path(root, path.relative_to(root).as_posix()) for path in expected]
    for original, destination in zip(expected, destinations, strict=True):
        if destination != original:
            raise ValueError(f"experiment directory is redirected: {original}")
        if destination.exists():
            raise FileExistsError(f"experiment already has metadata or outputs: {destination}")
    for destination in destinations:
        destination.mkdir(parents=True, exist_ok=False)
    (metadata_dir / "experiment.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return load_experiment(root, experiment_id)


def select_experiment_directories(root: Path, requested: list[str] | None,
                                  required_subdirectory: str) -> list[Path]:
    """Ignore tool workspaces; reject missing or ambiguous requested experiments."""
    directories = sorted(path for path in root.glob("*")
                         if path.is_dir() and EXPERIMENT_ID_RE.fullmatch(path.name)
                         and (path / required_subdirectory).is_dir())
    if not requested:
        if not directories:
            raise FileNotFoundError(f"no experiments with {required_subdirectory} under {root}")
        return directories
    selected = set()
    for token in requested:
        matches = [path for path in directories if path.name == token or
                   (token.isascii() and token.isdigit() and
                    path.name.rsplit("_", 1)[-1].isdigit() and
                    int(path.name.rsplit("_", 1)[-1]) == int(token))]
        if not matches:
            raise FileNotFoundError(f"no matching experiment with {required_subdirectory}: {token}")
        if len(matches) > 1:
            raise ValueError(f"ambiguous session {token}; use a full experiment ID: "
                             + ", ".join(path.name for path in matches))
        selected.update(matches)
    return sorted(selected)


def describe_experiment(experiment: Experiment) -> dict:
    paths = {"raw": experiment.raw_root,
             **{stage: experiment.stage_path(stage)
                for stage in ("interim", "processed", "results")}}
    references = {field: [{"path": ref,
                           "exists": repository_path(experiment.repository, ref).exists()}
                          for ref in experiment.metadata.get(field, [])]
                  for field in ("raw_sources", "config_paths", "calibration_paths")}
    return {**experiment.metadata, "paths": {key: {"path": str(path), "exists": path.exists()}
                                            for key, path in paths.items()},
            "reference_status": references}


def experiment_context(experiment: Experiment, command: str) -> dict:
    """Snapshot declarations separately from the numerical pipeline's used parameters."""
    declared = {}
    for field in ("config_paths", "calibration_paths"):
        declared[field] = {}
        for reference in experiment.metadata.get(field, []):
            path = repository_path(experiment.repository, reference)
            if not path.is_file():
                raise FileNotFoundError(f"registered reference is missing: {reference}")
            declared[field][reference] = hashlib.sha256(path.read_bytes()).hexdigest()
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=experiment.repository,
                            capture_output=True, text=True, check=False, timeout=10)
    return {"experiment_id": experiment.experiment_id, "command": command,
            "experiment_metadata": str(experiment.metadata_path),
            "experiment_metadata_sha256": hashlib.sha256(
                experiment.metadata_path.read_bytes()).hexdigest(),
            "metadata_snapshot": experiment.metadata,
            "git_commit_sha": commit.stdout.strip() if commit.returncode == 0 else None,
            "registered_references_sha256": declared,
            "reference_policy": "Registry references are declarations, not automatically applied "
                                "parameters. See the analysis manifest for parameters actually used."}
