"""
Named string constants for closed value sets used across connectors.

Plain str assignments (not Enum subclass) so callers can compare with ==
and use the values as dict keys or DataFrame cell values without .value.

Modules:
    enums      -- this file; segment, enrich-status, and merge-confidence constants
    normalize  -- text normalization helpers
    arcgis     -- ArcGIS REST query helpers
"""

# ---------------------------------------------------------------------------
# Segment values
# ---------------------------------------------------------------------------
SEGMENT_RELIGIOUS = "religious"
SEGMENT_MUNICIPAL = "municipal"
SEGMENT_FEDERAL = "federal"
SEGMENT_COMMERCIAL = "commercial"

# ---------------------------------------------------------------------------
# Enrich status values (irs_990_enrich)
# ---------------------------------------------------------------------------
ENRICH_OK = "ok"
ENRICH_NOT_FOUND = "not_found"
ENRICH_ERROR = "error"
ENRICH_SKIPPED = "skipped"

# ---------------------------------------------------------------------------
# Merge confidence values (deathcare_merge)
# ---------------------------------------------------------------------------
CONFIDENCE_HIGH = "high"
CONFIDENCE_SPATIAL_ONLY = "spatial_only"
CONFIDENCE_NONE = "none"
