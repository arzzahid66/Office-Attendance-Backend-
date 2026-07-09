import math

# Great-circle distance via the Haversine formula. Pure stdlib — no Google Maps, no API
# key, no billing, works offline. Mean Earth radius (IUGG) in metres.
EARTH_RADIUS_M = 6_371_008.8


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in metres between two WGS-84 lat/long points."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))
