"""Application settings. Everything env-driven so the three tiers stay decoupled."""
import os
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolved from this file so the same defaults work whether the app runs from
# a checkout (repo/backend/app) or from /app in the container image.
APP_DIR = Path(__file__).resolve().parent        # .../backend/app
BACKEND_DIR = APP_DIR.parent                     # .../backend   (/app in image)
REPO_ROOT = BACKEND_DIR.parent                   # repo root     (/     in image)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Local runs read backend/.env. The image never ships one - secrets
        # arrive as environment variables - and a missing file is not an error.
        env_file=str(BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- data plane -------------------------------------------------------
    # Overridden by DB_PATH / GTFS_STATIC_DIR in the container.
    db_path: Path = REPO_ROOT / "data" / "gtfs.db"
    gtfs_static_dir: Path = REPO_ROOT / "delhi_buses_static_gtfs_v1-2"

    # Which Repository implementation the data plane uses. "sqlite" is the
    # default for local dev and the offline demo; "postgres" selects the
    # PostgreSQL + PostGIS adapter, which needs DATABASE_URL.
    db_backend: str = "sqlite"
    database_url: str = "postgresql://gtfs:gtfs@localhost:5432/gtfs"
    pg_pool_max: int = 10

    # ---- GTFS-realtime feed ----------------------------------------------
    # Delhi Open Transit Data. Override either piece in backend/.env
    rt_vehicle_positions_url: str = "https://otd.delhi.gov.in/api/realtime/VehiclePositions.pb"
    rt_trip_updates_url: Optional[str] = None
    rt_api_key: Optional[str] = None
    # Feeds differ in how they want the key. If a header name is set it wins;
    # otherwise the key goes on the query string as `rt_api_key_param`.
    rt_api_key_header: Optional[str] = "x-api-key"
    rt_api_key_param: str = "key"
    rt_poll_seconds: int = 30
    rt_timeout_seconds: float = 20.0

    # When no API key is configured the poller replays synthetic vehicles built
    # from the static feed so the full stack is demonstrable offline.
    rt_mock_when_unconfigured: bool = True

    # Baseline for the quality tier's fleet coverage ratio (active vehicles /
    # expected fleet). No feed we've seen publishes this, so it stays unset
    # (metric reports null) until an operator supplies it out of band.
    expected_fleet_size: Optional[int] = None

    # ---- retention --------------------------------------------------------
    # How much position history the data plane keeps for the analytics tier.
    # The live Delhi feed carries ~5,700 vehicles, so a 30s cadence writes on
    # the order of 16M rows/day (~2.5 GB in SQLite). Keep this modest until the
    # history moves to a columnar store.
    history_retention_hours: int = 12

    # ---- service ----------------------------------------------------------
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origin_list(self):
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def rt_configured(self) -> bool:
        return bool(self.rt_api_key)


settings = Settings()
os.makedirs(settings.db_path.parent, exist_ok=True)
