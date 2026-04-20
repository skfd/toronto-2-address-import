"""Flag Land candidates that share (address_full, municipality_name) with
another active Land row. Non-canonical rows within 5 m are silently deduped
by the conflate stage; this check surfaces the canonical row in those pairs
(so the operator sees the dedup happened) and both rows in wider pairs
(where neither can be safely auto-dropped).
"""
from .base import Candidate, CheckContext, Verdict


class IntraSourceDuplicateCheck:
    id = "intra_source_duplicate"
    version = 1
    default_enabled = True
    description = (
        "Flags Land candidates that share (address_full, municipality) with "
        "another active Land row in the same run."
    )

    def applies(self, cand: Candidate, ctx: CheckContext) -> bool:
        return cand.dup_sibling_candidate_id is not None

    def evaluate(self, cand: Candidate, ctx: CheckContext) -> Verdict:
        return Verdict(
            status="FLAG",
            severity="info",
            reason_code="intra_source_duplicate",
            details={
                "sibling_candidate_id": cand.dup_sibling_candidate_id,
                "dist_m": round(cand.dup_sibling_dist_m, 2)
                if cand.dup_sibling_dist_m is not None
                else None,
            },
        )
