from ..conflate import haversine
from .base import Candidate, CheckContext, Verdict


class CityDuplicateCheck:
    id = "city_duplicate"
    version = 1
    default_enabled = True
    description = "Flags candidates that are within a few metres of another City point in the same run."

    def applies(self, cand: Candidate, ctx: CheckContext) -> bool:
        return cand.lat is not None and cand.lon is not None

    def evaluate(self, cand: Candidate, ctx: CheckContext) -> Verdict:
        radius = float(ctx.params.get("city_duplicate", {}).get("radius_m", 3.0))
        neighbors = []
        for lat, lon, other in ctx.city_index.query(cand.lat, cand.lon):
            if other["candidate_id"] == cand.candidate_id:
                continue
            dist = haversine(cand.lat, cand.lon, lat, lon)
            if dist <= radius:
                neighbors.append({"candidate_id": other["candidate_id"], "dist_m": round(dist, 2)})
        if not neighbors:
            return Verdict(status="PASS", reason_code="unique_location")
        return Verdict(
            status="FLAG",
            severity="info",
            reason_code="city_duplicate",
            details={"neighbors": neighbors[:5], "count": len(neighbors)},
        )
