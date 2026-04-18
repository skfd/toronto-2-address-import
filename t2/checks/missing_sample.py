from .base import Candidate, CheckContext, Verdict


class MissingSampleCheck:
    id = "missing_sample"
    version = 1
    default_enabled = True
    description = "Flags every Nth MISSING candidate for spot-check review."

    def applies(self, cand: Candidate, ctx: CheckContext) -> bool:
        return cand.verdict == "MISSING"

    def evaluate(self, cand: Candidate, ctx: CheckContext) -> Verdict:
        every_nth = int(ctx.params.get("missing_sample", {}).get("every_nth", 50))
        if every_nth <= 0 or cand.candidate_id % every_nth != 0:
            return Verdict(status="PASS", reason_code="not_sampled")
        return Verdict(
            status="FLAG",
            severity="info",
            reason_code="spot_check",
            details={"every_nth": every_nth},
        )
