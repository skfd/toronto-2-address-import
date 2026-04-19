# Future work

Design proposals that are **not implemented** and not scheduled. Each file
captures enough context (assumptions, guardrails, data-model sketch, phasing)
that a future implementer can pick it up without re-deriving the reasoning.

Before implementing anything here, re-read the proposal and verify the
assumptions still hold against the current code — these documents are frozen
at the date they were written.

## Index

- [postcode-enrichment.md](postcode-enrichment.md) — fill `addr:postcode` on
  matched OSM nodes that lack one, sourced from same-address POI nodes.
  First mutation flow in an otherwise create-only pipeline.
