"""Shared Periodic Hill grid-generation parameters.

This module is the single source of truth for the elliptic Poisson grid
settings used by both the solver-side generator and the Phase 1 checkpoint
grid generator.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Optional


GRID_PARAM_SCHEMA = 1
GRID_GENERATOR = "periodic_hill_steger_sorenson"

# Poisson smoother parameters.  These values affect the generated coordinates.
POISSON_MAX_ITER = 100000
POISSON_TOL = 1.0e-12
POISSON_OMEGA = 1.0
POISSON_PRINT_EVERY = 2000
POISSON_REQUIRE_CONVERGED = True

# Stretching and geometry defaults.
DEFAULT_ALPHA = 0.5
PHASE1_OLD_GAMMA = 2.0
HILL_LY = 9.0
HILL_SCALE = 54.0 / 28.0
HILL_REFERENCE_HEIGHT = 1.0

# These are part of the grid-generation algorithm, not merely diagnostics.
VERTICAL_REDISTRIBUTION = "physical_z_vinokur_tanh"
POISSON_CONTROL_FUNCTIONS = "reverse_computed_PQ"
BOUNDARY_CONDITION = "fixed_resampled_boundaries"

GRID_PARAMS_SHA256_KEY = "GRID_PARAMS_SHA256"
GRID_PARAMS_JSON_KEY = "GRID_PARAMS_JSON"


def _json_ready(value: Any) -> Any:
    """Normalize values for stable JSON hashing."""
    if isinstance(value, Path):
        return value.name
    if isinstance(value, float):
        return float(format(value, ".17g"))
    if isinstance(value, (list, tuple)):
        return [_json_ready(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_ready(value[k]) for k in sorted(value)}
    return value


def build_grid_param_payload(
    *,
    ni: int,
    nj: int,
    gamma: float,
    alpha: float,
    reference_grid: str,
    ly: float = HILL_LY,
    lz: Optional[float] = None,
    mode: str = "adaptive_poisson",
    pq_interpolation: str = "unknown",
    boundary_interpolation: str = "unknown",
    poisson_iter: int = POISSON_MAX_ITER,
    poisson_tol: float = POISSON_TOL,
    poisson_omega: float = POISSON_OMEGA,
    poisson_print_every: int = POISSON_PRINT_EVERY,
    require_converged: bool = POISSON_REQUIRE_CONVERGED,
) -> Dict[str, Any]:
    """Return the canonical parameter payload that defines a grid."""
    payload = {
        "schema": GRID_PARAM_SCHEMA,
        "generator": GRID_GENERATOR,
        "mode": mode,
        "ni": int(ni),
        "nj": int(nj),
        "gamma": float(gamma),
        "alpha": float(alpha),
        "reference_grid": Path(reference_grid).name,
        "ly": float(ly),
        "lz": None if lz is None else float(lz),
        "hill_shape": {
            "profile": "Mellen-Frohlich-Rodi periodic hill polynomial",
            "ly": float(ly),
            "scale": HILL_SCALE,
            "reference_height": HILL_REFERENCE_HEIGHT,
        },
        "poisson": {
            "max_iter": int(poisson_iter),
            "tol": float(poisson_tol),
            "omega": float(poisson_omega),
            "print_every": int(poisson_print_every),
            "require_converged": bool(require_converged),
            "control_functions": POISSON_CONTROL_FUNCTIONS,
        },
        "interpolation": {
            "pq": pq_interpolation,
            "boundary": boundary_interpolation,
        },
        "boundary_condition": BOUNDARY_CONDITION,
        "vertical_redistribution": VERTICAL_REDISTRIBUTION,
    }
    return _json_ready(payload)


def canonical_grid_param_json(payload: Dict[str, Any]) -> str:
    return json.dumps(_json_ready(payload), sort_keys=True, separators=(",", ":"))


def grid_param_sha256(payload: Dict[str, Any]) -> str:
    encoded = canonical_grid_param_json(payload).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_grid_metadata(**kwargs: Any) -> Dict[str, str]:
    payload = build_grid_param_payload(**kwargs)
    payload_json = canonical_grid_param_json(payload)
    return {
        GRID_PARAMS_SHA256_KEY: grid_param_sha256(payload),
        GRID_PARAMS_JSON_KEY: payload_json,
    }


def grid_header_comment_lines(metadata: Optional[Dict[str, str]]) -> list[str]:
    """Return Tecplot-safe comment lines placed before TITLE."""
    if not metadata:
        return []
    lines = []
    for key in (GRID_PARAMS_SHA256_KEY, GRID_PARAMS_JSON_KEY):
        value = metadata.get(key)
        if value:
            lines.append(f"# {key}={value}\n")
    return lines


def read_grid_params_sha256(path: str | Path) -> Optional[str]:
    """Read GRID_PARAMS_SHA256 from a generated Tecplot file if present."""
    prefix = f"{GRID_PARAMS_SHA256_KEY}="
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("DT="):
                break
            if prefix in stripped:
                return stripped.split(prefix, 1)[1].strip().split()[0]
    return None
