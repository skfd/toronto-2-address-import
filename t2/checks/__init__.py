"""Import-time registry of all available checks."""
from .base import Candidate, Check, CheckContext, Verdict
from .city_duplicate import CityDuplicateCheck
from .conflict import ConflictCheck
from .missing_sample import MissingSampleCheck
from .potential_amenity import PotentialAmenityCheck
from .suffix_range import SuffixRangeCheck

REGISTRY: dict[str, Check] = {
    c.id: c
    for c in (
        ConflictCheck(),
        SuffixRangeCheck(),
        CityDuplicateCheck(),
        MissingSampleCheck(),
        PotentialAmenityCheck(),
    )
}

__all__ = ["REGISTRY", "Check", "Candidate", "CheckContext", "Verdict"]
