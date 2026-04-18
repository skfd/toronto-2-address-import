"""Import-time registry of all available checks."""
from .base import Candidate, Check, CheckContext, Verdict
from .city_duplicate import CityDuplicateCheck
from .conflict import ConflictCheck
from .missing_sample import MissingSampleCheck
from .suffix_range import SuffixRangeCheck

REGISTRY: dict[str, Check] = {
    c.id: c
    for c in (
        ConflictCheck(),
        SuffixRangeCheck(),
        CityDuplicateCheck(),
        MissingSampleCheck(),
    )
}

__all__ = ["REGISTRY", "Check", "Candidate", "CheckContext", "Verdict"]
