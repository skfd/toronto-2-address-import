from .base import Candidate, CheckContext, Verdict


class ConflictCheck:
    id = "match_far"
    version = 2
    default_enabled = True
    description = "Flags candidates where the matching OSM address exists but sits far from the candidate coordinates."

    def applies(self, cand: Candidate, ctx: CheckContext) -> bool:
        return cand.verdict == "MATCH_FAR"

    def evaluate(self, cand: Candidate, ctx: CheckContext) -> Verdict:
        return Verdict(
            status="FLAG",
            severity="warn",
            reason_code="match_far",
            details={
                "nearest_osm_id": cand.nearest_osm_id,
                "nearest_osm_type": cand.nearest_osm_type,
                "nearest_dist_m": cand.nearest_dist_m,
            },
        )
