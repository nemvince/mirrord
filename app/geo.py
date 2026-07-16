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
        if env_path:
            if os.path.isfile(env_path):
                logger.debug("Using GeoIP DB from MIRRORD_GEOIP_DB: %s", env_path)
                return env_path
            logger.debug(
                "MIRRORD_GEOIP_DB set to %s but file not found", env_path
            )
        for path in _COMMON_PATHS:
            if os.path.isfile(path):
                logger.debug("Found GeoIP DB at common path: %s", path)
                return path
        logger.debug("No GeoIP DB found in env or common paths: %s", _COMMON_PATHS)
        return None

    def lookup(self, ip: str) -> str | None:
        if self._reader is None:
            return None
        try:
            code = self._reader.country(ip).country.iso_code
            logger.debug("GeoIP lookup %s -> %s", ip, code)
            return code
        except Exception as exc:
            logger.debug("GeoIP lookup failed for %s: %s", ip, exc)
            return None
