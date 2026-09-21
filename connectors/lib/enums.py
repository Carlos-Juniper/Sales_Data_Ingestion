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

# ---------------------------------------------------------------------------
# Healthcare pipeline scope (D10)
# ---------------------------------------------------------------------------
# Single source of truth for the 5 states in scope for healthcare ingestion.
# All three healthcare connectors (va_facilities, cms_provider_data,
# nppes_practice_locations) import this constant and apply it as a .isin()
# filter as early as possible in row-building — before geocoding and before
# upsert_staging() — so that out-of-state rows never reach downstream steps.
HEALTHCARE_TARGET_STATES: frozenset[str] = frozenset({"FL", "NC", "TX", "PA", "SC"})

# ---------------------------------------------------------------------------
# Parks pipeline scope (D10)
# ---------------------------------------------------------------------------
# Same 5-state footprint as healthcare. All parks connectors import this constant
# and apply it as an early .isin() filter on the state column.
PARKS_TARGET_STATES: frozenset[str] = frozenset({"FL", "NC", "TX", "PA", "SC"})

# ---------------------------------------------------------------------------
# Segment values — parks extension
# ---------------------------------------------------------------------------
SEGMENT_STATE = "state"
