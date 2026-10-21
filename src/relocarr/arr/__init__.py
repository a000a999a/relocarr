from .base import ArrApp, ConnectionTestResult, RootFolder
from .radarr import RadarrApp
from .sonarr import SonarrApp


def make_app(kind: str, base_url: str, api_key: str) -> ArrApp:
    if kind == "radarr":
        return RadarrApp(base_url, api_key)
    if kind == "sonarr":
        return SonarrApp(base_url, api_key)
    raise ValueError(f"unknown app kind: {kind!r}")


__all__ = [
    "ArrApp",
    "ConnectionTestResult",
    "RootFolder",
    "RadarrApp",
    "SonarrApp",
    "make_app",
]
