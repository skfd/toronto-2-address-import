import re

from .base import Candidate, CheckContext, Verdict

_SUFFIXED_NUMBER = re.compile(r"^\d+[A-Z/\-].*$", re.IGNORECASE)


class SuffixRangeCheck:
    id = "suffix_range"
    version = 1
    default_enabled = True
    description = "Flags suffixed or ranged housenumbers (10A, 10-14) that often duplicate a plain base number in OSM."

    def applies(self, cand: Candidate, ctx: CheckContext) -> bool:
        # Only bother checking MISSING — CONFLICTs are already flagged elsewhere
        return cand.verdict in ("MISSING", "CONFLICT")

    def evaluate(self, cand: Candidate, ctx: CheckContext) -> Verdict:
        hn = (cand.housenumber or "").strip()
        is_range = cand.lo_num is not None and cand.hi_num is not None and cand.lo_num != cand.hi_num
        has_suffix = bool(_SUFFIXED_NUMBER.match(hn)) or bool(cand.lo_num_suf)
        if not (is_range or has_suffix):
            return Verdict(status="PASS", reason_code="plain_number")
        return Verdict(
            status="FLAG",
            severity="info",
            reason_code="range" if is_range else "suffix",
            details={
                "housenumber": hn,
                "lo_num": cand.lo_num,
                "hi_num": cand.hi_num,
                "lo_num_suf": cand.lo_num_suf,
            },
        )
