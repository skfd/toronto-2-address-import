import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    source_sqlite_path: str
    default_bbox: tuple[float, float, float, float]
    overpass_url: str
    match_radius_m: float
    match_near_m: float
    checks_enabled: dict[str, bool]
    checks_params: dict[str, dict]
    batch_size: int
    changesets_per_minute: float
    changeset_comment_template: str

    osm_source: str
    osm_pbf_url: str
    osm_toronto_bbox: tuple[float, float, float, float]
    osm_extract_dir: Path

    osm_api_base: str
    osm_client_id: str
    osm_client_secret: str
    osm_redirect_uri: str
    flask_secret_key: str
    fernet_key: str

    tool_db_path: Path = field(default=ROOT / "data" / "tool.db")
    migrations_dir: Path = field(default=ROOT / "migrations")
    data_dir: Path = field(default=ROOT / "data")


def _load_env():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def load() -> Config:
    _load_env()
    toml_path = ROOT / "config.toml"
    with open(toml_path, "rb") as f:
        cfg = tomllib.load(f)

    checks_enabled: dict[str, bool] = {k: bool(v) for k, v in cfg.get("checks", {}).items()}
    checks_params: dict[str, dict] = dict(cfg.get("check_params", {}))

    bbox = tuple(cfg["run_defaults"]["bbox"])
    assert len(bbox) == 4

    osm_section = cfg.get("osm", {})
    osm_source = str(osm_section.get("source", "local"))
    if osm_source not in ("local", "overpass"):
        raise ValueError(f"config.osm.source must be 'local' or 'overpass', got {osm_source!r}")
    osm_pbf_url = str(osm_section.get(
        "pbf_url",
        "https://download.geofabrik.de/north-america/canada/ontario-latest.osm.pbf",
    ))
    toronto_bbox = tuple(osm_section.get("toronto_bbox", [43.58, -79.64, 43.86, -79.11]))
    assert len(toronto_bbox) == 4
    extract_dir_raw = str(osm_section.get("extract_dir", "data/osm"))
    extract_dir = Path(extract_dir_raw)
    if not extract_dir.is_absolute():
        extract_dir = ROOT / extract_dir

    return Config(
        source_sqlite_path=cfg["source"]["sqlite_path"],
        default_bbox=bbox,  # type: ignore
        overpass_url=cfg["run_defaults"]["overpass_url"],
        match_radius_m=float(cfg["conflation"]["match_radius_m"]),
        match_near_m=float(cfg["conflation"]["match_near_m"]),
        checks_enabled=checks_enabled,
        checks_params=checks_params,
        batch_size=int(cfg["upload"]["batch_size"]),
        changesets_per_minute=float(cfg["upload"]["changesets_per_minute"]),
        changeset_comment_template=cfg["upload"]["changeset_comment_template"],
        osm_source=osm_source,
        osm_pbf_url=osm_pbf_url,
        osm_toronto_bbox=toronto_bbox,  # type: ignore
        osm_extract_dir=extract_dir,
        osm_api_base=os.environ.get("OSM_API_BASE", "https://master.apis.dev.openstreetmap.org"),
        osm_client_id=os.environ.get("OSM_CLIENT_ID", ""),
        osm_client_secret=os.environ.get("OSM_CLIENT_SECRET", ""),
        osm_redirect_uri=os.environ.get("OSM_REDIRECT_URI", "http://localhost:5000/oauth/callback"),
        flask_secret_key=os.environ.get("FLASK_SECRET_KEY", "dev-secret"),
        fernet_key=os.environ.get("FERNET_KEY", ""),
    )
