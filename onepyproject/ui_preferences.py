"""Shared layout-density choices for desktop tables."""

DENSITY_HEIGHTS = {"comfortable": 24, "tight": 20, "dense": 18}
DEFAULT_DENSITY = "comfortable"
DENSITY_PREFERENCE_KEY = "display/density_v2"


def read_density_preference(preferences):
    """Retain old row spacing where possible without changing old settings."""
    current = preferences.value(DENSITY_PREFERENCE_KEY, "", type=str)
    if current:
        return current if current in DENSITY_HEIGHTS else DEFAULT_DENSITY
    previous = preferences.value("display/density", "", type=str)
    return {"comfortable": "comfortable", "tight": "comfortable", "dense": "tight"}.get(previous, DEFAULT_DENSITY)
