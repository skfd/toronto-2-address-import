from .base import Candidate, CheckContext, Verdict


class ConflictCheck:
    id = "conflict_30m"
    version = 1
    default_enabled = True
    description = "Flags candidates whose conflation verdict is CONFLICT (near an existing OSM addr with different attributes)."

    def applies(self, cand: Candidate, ctx: CheckContext) -> bool:
        return cand.verdict == "CONFLICT"

    def evaluate(self, cand: Candidate, ctx: CheckContext) -> Verdict:
        return Verdict(
            status="FLAG",
            severity="warn",
            reason_code="conflict_30m",
            details={
                "nearest_osm_id": cand.nearest_osm_id,
                "nearest_osm_type": cand.nearest_osm_type,
                "nearest_dist_m": cand.nearest_dist_m,
            },
        )
