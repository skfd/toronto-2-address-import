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
        osm_api_base=os.environ.get("OSM_API_BASE", "https://master.apis.dev.openstreetmap.org"),
        osm_client_id=os.environ.get("OSM_CLIENT_ID", ""),
        osm_client_secret=os.environ.get("OSM_CLIENT_SECRET", ""),
        osm_redirect_uri=os.environ.get("OSM_REDIRECT_URI", "http://localhost:5000/oauth/callback"),
        flask_secret_key=os.environ.get("FLASK_SECRET_KEY", "dev-secret"),
        fernet_key=os.environ.get("FERNET_KEY", ""),
    )
