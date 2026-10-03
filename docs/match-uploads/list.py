"""Toronto nodes uploaded although conflation said MATCH: OSM already had the
address, and a review Approve (which uploads whatever the verdict) added a
second copy. Reads the tool DB, checks both objects against the live API,
writes match_uploads.csv. Deletes nothing.
"""
import csv
import json
import sqlite3
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE.parents[1] / "data" / "toronto" / "tool.db"
API = "https://api.openstreetmap.org/api/0.6/"
UA = "toronto-address-import/1.0 (match-upload audit; skfd)"


def fetch(kind, ids):
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 100):
        req = urllib.request.Request(f"{API}{kind}s.json?{kind}s=" + ",".join(map(str, ids[i:i + 100])),
                                     headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                for el in json.load(r)["elements"]:
                    out[el["id"]] = el
        except urllib.error.HTTPError as e:   # a deleted id 404s/410s the batch: go one by one
            if len(ids[i:i + 100]) == 1:
                continue
            out.update(fetch(kind, [x for x in ids[i:i + 100]][:50]))
            out.update(fetch(kind, [x for x in ids[i:i + 100]][50:]))
        time.sleep(0.5)
    return out


db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = db.execute("""
    SELECT c.run_id, c.candidate_id, c.housenumber, c.street_raw, c.osm_node_id,
           f.nearest_osm_type, f.nearest_osm_id, f.nearest_dist_m, r.reason_code, r.note,
           f.matched_osm_tags_json
      FROM candidates c JOIN conflation f USING (run_id, candidate_id)
      JOIN review_items r USING (run_id, candidate_id)
     WHERE c.stage = 'UPLOADED' AND f.verdict = 'MATCH'
""").fetchall()

ours = fetch("node", {r[4] for r in rows if r[4]})
theirs = {}
for kind in ("node", "way", "relation"):
    ids = {r[6] for r in rows if r[5] == kind and r[6]}
    if ids:
        theirs.update({(kind, k): v for k, v in fetch(kind, ids).items()})

out = []
for (run, cid, num, street, our_id, mtype, mid, dist, reason, note, mtags) in rows:
    o = ours.get(our_id)
    t = theirs.get((mtype, mid))
    ttags = (t or {}).get("tags", {})
    other = sorted(k for k in json.loads(mtags or "{}") if not k.startswith("addr:") and k != "source")
    same = bool(o and t and ttags.get("addr:housenumber") == num)
    kind = ("gone: our node deleted" if not o else
            "gone: matched object deleted or changed" if not same else
            "plain duplicate" if not other else
            "beside an addressed building/POI")
    out.append({
        "kind": kind, "address": f"{num} {street}", "ours": f"https://www.openstreetmap.org/node/{our_id}",
        "matched": f"https://www.openstreetmap.org/{mtype}/{mid}", "dist_m": round(dist or 0, 1),
        "matched_has": " ".join(other[:4]), "review_reason": reason, "note": note or "",
        "run": run, "candidate": cid,
    })
order = ["plain duplicate", "beside an addressed building/POI",
         "gone: matched object deleted or changed", "gone: our node deleted"]
out.sort(key=lambda r: (order.index(r["kind"]), r["address"]))
with open(HERE / "match_uploads.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(out[0]))
    w.writeheader(); w.writerows(out)
import collections
print(len(out), collections.Counter(r["kind"] for r in out))
