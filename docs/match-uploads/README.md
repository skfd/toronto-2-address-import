# Uploaded although OSM already had the address

**Found 2026-10-03, from the Guelph review.** The review page's **Approve**
uploads a candidate whatever its conflation verdict. On a MATCH, "Approve"
read as "yes, that is the match", and it created a second copy instead. The
buttons now say **Upload new node** / **Don't upload**, and MATCH items carry
a warning (address-importer-friend `6c8bc766`).

Toronto rejected 2,094 MATCH review items and approved 109; 101 of those
uploaded. `list.py` checks each one against the live API (2026-10-03) and
writes `match_uploads.csv`:

| Kind | Count | What to do |
|---|---|---|
| plain duplicate | 8 | Another address-only node with the same number, 2–10 m away. Delete ours, or merge. |
| beside an addressed building/POI | 64 | The building or POI carries the same address. Tolerated by some mappers; judge each. |
| matched object deleted or changed | 29 | Someone already resolved it; probably nothing to do. |

Nothing has been deleted. MATCH_FAR (210 uploaded) is not in this list: far
matches are often genuinely different points on a long street.
