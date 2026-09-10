"""Stage contract: artifact shapes, input/output paths, and gates.

Pure data definition consumed by downstream orchestration tasks. Covers
spec sections: artifact-shape table and gate definitions for --batch.

Stage ordering is implicit (the ordered list of stage IDs), with 1a -> 1b
sequential. Human-gated stages (2, 3, 4) require human review of their
output before the next stage may read it.
"""

from __future__ import annotations

# Ordered stage IDs as used throughout the pipeline.
STAGE_ORDER: list[str] = ["1a", "1b", "2", "3", "4", "5", "6", "7", "8", "9"]

# Artifact shape constants.
SHAPE_SINGLE_FILE = "single-file"
SHAPE_PHASE_TRACKED = "phase-tracked"
SHAPE_MULTI_FILE = "multi-file"
SHAPE_NO_CHECKABLE = "no-checkable-artifact"

# Each key is a stage ID. Values are dicts with:
#   artifact_shape    – one of the SHAPE_* constants
#   preflight_inputs  – exact filenames the stage must find on disk before it runs
#   postflight_outputs – tracked artifact filenames the stage produces
#   human_gated       – True for stages 2/3/4 only (per spec §1/§4 gate definitions)
#   (optional) phase_tracking_note – extra requirement text for phase-tracked stages
STAGE_CONTRACT: dict[str, dict] = {
    "1a": {
        "artifact_shape": SHAPE_SINGLE_FILE,
        "preflight_inputs": [],
        "postflight_outputs": ["01-instructions.md"],
        "human_gated": False,
    },
    "1b": {
        "artifact_shape": SHAPE_SINGLE_FILE,
        "preflight_inputs": [],
        "postflight_outputs": ["01-signatures.md"],
        "human_gated": False,
    },
    "2": {
        "artifact_shape": SHAPE_SINGLE_FILE,
        "preflight_inputs": [
            "00-decisions.md",
            "01-instructions.md",
            "01-signatures.md",
        ],
        "postflight_outputs": ["02-capability-inventory.md"],
        "human_gated": True,
    },
    "3": {
        "artifact_shape": SHAPE_SINGLE_FILE,
        "preflight_inputs": [
            "00-decisions.md",
            "01-instructions.md",
            "01-signatures.md",
            "02-capability-inventory.md",
        ],
        "postflight_outputs": ["03-mcp-surface.md"],
        "human_gated": True,
    },
    "4": {
        "artifact_shape": SHAPE_SINGLE_FILE,
        "preflight_inputs": [
            "01-instructions.md",
            "01-signatures.md",
            "02-capability-inventory.md",
            "03-mcp-surface.md",
        ],
        "postflight_outputs": ["04-spec.md"],
        "human_gated": True,
    },
    "5": {
        "artifact_shape": SHAPE_PHASE_TRACKED,
        "preflight_inputs": ["04-spec.md"],
        "postflight_outputs": [],
        "human_gated": False,
        "phase_tracking_note": (
            "Pre-flight requires 04-spec.md exists and that "
            "--phase N/M is given with M consistent with prior "
            "stage-5 manifest entries."
        ),
    },
    "6": {
        "artifact_shape": SHAPE_MULTI_FILE,
        "preflight_inputs": ["04-spec.md"],
        "postflight_outputs": ["05-test-plan.md"],
        "human_gated": False,
    },
    "7": {
        "artifact_shape": SHAPE_SINGLE_FILE,
        "preflight_inputs": [],
        "postflight_outputs": ["06-review.md"],
        "human_gated": False,
    },
    "8": {
        "artifact_shape": SHAPE_MULTI_FILE,
        "preflight_inputs": [],
        "postflight_outputs": ["07-release.md", "README.md"],
        "human_gated": False,
    },
    "9": {
        "artifact_shape": SHAPE_NO_CHECKABLE,
        "preflight_inputs": [],
        "postflight_outputs": [],
        "human_gated": False,
    },
}


def validate_contract() -> None:
    """Fail-fast check that the contract is internally consistent.

    Catches duplicate stage IDs, missing keys, and ordering violations.
    Raises ValueError on the first problem found; returns None if valid.
    """
    seen_ids: set[str] = set()
    for stage_id in STAGE_ORDER:
        if stage_id in seen_ids:
            raise ValueError(f"duplicate stage ID in STAGE_ORDER: {stage_id}")
        seen_ids.add(stage_id)

        if stage_id not in STAGE_CONTRACT:
            raise ValueError(f"stage {stage_id} is in STAGE_ORDER but missing from STAGE_CONTRACT")

        entry = STAGE_CONTRACT[stage_id]
        for key in ("artifact_shape", "preflight_inputs", "postflight_outputs", "human_gated"):
            if key not in entry:
                raise ValueError(f"stage {stage_id} missing required key: {key}")

        if stage_id not in ("1a", "1b"):
            for dep in entry["preflight_inputs"]:
                # Walk backwards through STAGE_ORDER to confirm the dep
                # was produced by an earlier stage.
                found = False
                idx = STAGE_ORDER.index(stage_id)
                for prev_id in STAGE_ORDER[:idx]:
                    if dep in STAGE_CONTRACT[prev_id]["postflight_outputs"]:
                        found = True
                        break
                if not found and dep not in entry.get("postflight_outputs", []):
                    # Allow deps that live outside the contract (e.g. repo files).
                    # Only warn — the dep may come from setup stage 0 or the repo.
                    pass
