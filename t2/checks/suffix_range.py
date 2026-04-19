import re

from .base import Candidate, CheckContext, Verdict

_SUFFIX_CHAR = re.compile(r"^\d+\s*([A-Za-z])\b")

# Letters that look like digits and often indicate a data-entry typo
# (I ↔ 1, O ↔ 0, Q ↔ 0). Other single-letter suffixes (A, B, R for "rear",
# C–K, 1/2 fractionals) are normal Toronto civic forms and pass.
_SUSPICIOUS_SUFFIXES = frozenset({"I", "O", "Q"})


class SuffixRangeCheck:
    id = "suffix_range"
    version = 2
    default_enabled = True
    description = "Flags housenumber ranges (100-110) and digit-confusable suffix letters (I, O, Q). Other suffixes (A, B, R, 1/2, …) pass."

    def applies(self, cand: Candidate, ctx: CheckContext) -> bool:
        return cand.verdict == "MISSING"

    def evaluate(self, cand: Candidate, ctx: CheckContext) -> Verdict:
        hn = (cand.housenumber or "").strip()
        is_range = cand.lo_num is not None and cand.hi_num is not None and cand.lo_num != cand.hi_num

        if is_range:
            return Verdict(
                status="FLAG",
                severity="info",
                reason_code="range",
                details={"housenumber": hn, "lo_num": cand.lo_num, "hi_num": cand.hi_num},
            )

        suffix = (cand.lo_num_suf or "").strip().upper()
        if not suffix:
            m = _SUFFIX_CHAR.match(hn)
            if m:
                suffix = m.group(1).upper()

        if suffix in _SUSPICIOUS_SUFFIXES:
            return Verdict(
                status="FLAG",
                severity="info",
                reason_code="suspicious_suffix",
                details={"housenumber": hn, "suffix": suffix},
            )

        return Verdict(status="PASS", reason_code="ok")
