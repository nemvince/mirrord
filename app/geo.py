import logging
import os

logger = logging.getLogger("mirrord.geo")

_COMMON_PATHS = [
    "/usr/share/GeoIP/GeoLite2-Country.mmdb",
    "/var/lib/GeoIP/GeoLite2-Country.mmdb",
    "/usr/local/share/GeoIP/GeoLite2-Country.mmdb",
]


class GeoIP:
    def __init__(self):
        self._reader = None
        path = self._find_db()
        if path:
            try:
                import geoip2.database

                self._reader = geoip2.database.Reader(path)
                logger.info("GeoIP loaded: %s", path)
            except Exception as exc:
                logger.warning("Failed to open GeoIP DB at %s: %s", path, exc)
        else:
            logger.info("No GeoIP database found — geolocation disabled")

    @staticmethod
    def _find_db() -> str | None:
        env_path = os.environ.get("MIRRORD_GEOIP_DB")
        if env_path and os.path.isfile(env_path):
            return env_path
        for path in _COMMON_PATHS:
            if os.path.isfile(path):
                return path
        return None

    def lookup(self, ip: str) -> str | None:
        if self._reader is None:
            return None
        try:
            return self._reader.country(ip).country.iso_code
        except Exception:
            return None
