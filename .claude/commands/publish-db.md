---
description: Pointer — the canonical /publish-db lives in the engine checkout and works for any city
argument-hint: [YYYYMMDD]
---

The DB-snapshot command is **engine-level** now: one definition serving every
city, in `address-importer-friend/.claude/commands/publish-db.md`. This file is
a pointer so the Toronto checkout does not carry a second copy that drifts.

Run it from the engine checkout with this repo as the city dir:

```
/publish-db ../toronto-2-address-import [YYYYMMDD]
```

Or drive the script directly:

```
cd ../address-importer-friend
T2_CITY_DIR=../toronto-2-address-import python -m scripts.publish_db [--date YYYYMMDD]
```

Either way the release lands on **this** repo (the script resolves `--repo` from
this checkout's `origin`), and the scrub-verification rules in the engine
command still apply — never upload an artifact you have not verified is
credential-free.
