from .base import Candidate, CheckContext, Verdict


class PotentialAmenityCheck:
    id = "potential_amenity"
    version = 1
    default_enabled = True
    description = "Flags MATCH/MATCH_FAR where the matched OSM node carries non-address tags — hints the POI filter may need to grow."

    def applies(self, cand: Candidate, ctx: CheckContext) -> bool:
        if cand.verdict not in ("MATCH", "MATCH_FAR"):
            return False
        return cand.nearest_osm_type == "node" and cand.matched_osm_tags is not None

    def evaluate(self, cand: Candidate, ctx: CheckContext) -> Verdict:
        extra = sorted(k for k in (cand.matched_osm_tags or {}) if not k.startswith("addr:"))
        if not extra:
            return Verdict(status="PASS", reason_code="pure_address")
        return Verdict(
            status="FLAG",
            severity="info",
            reason_code="potential_amenity",
            details={"extra_tags": extra},
        )
