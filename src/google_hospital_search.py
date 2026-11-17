"""
AmbuPriority — Google Places API (New) Hospital-Only Search
============================================================
Strictly searches for hospitals using Google Places API (New) with:
  - includedType: "hospital" + strictTypeFiltering: True  (Text Search)
  - includedTypes: ["hospital"]                           (Nearby Search)

This module is ISOLATED — it only powers the hospital search/autocomplete
endpoints. All other OSM integrations, insurance APIs, Gemini AI,
dispatch logic, and triage remain completely untouched.

API key is loaded from .env:  VITE_GOOGLE_PLACES_API_KEY
"""

import json
import math
import time
import logging
import os
import urllib.request
import urllib.error
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------
def _get_places_api_key() -> str:
    key = os.getenv("VITE_GOOGLE_PLACES_API_KEY", "").strip()
    if not key:
        # fallback: read .env manually in case dotenv wasn't loaded
        try:
            env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
            with open(env_path) as f:
                for line in f:
                    if line.startswith("VITE_GOOGLE_PLACES_API_KEY="):
                        key = line.strip().split("=", 1)[1]
                        break
        except Exception:
            pass
    return key

GOOGLE_PLACES_API_KEY = _get_places_api_key()

# ---------------------------------------------------------------------------
# Field mask used for all Place Detail lookups
# ---------------------------------------------------------------------------
_FIELD_MASK = "places.id,places.displayName,places.formattedAddress,places.location,places.types,places.internationalPhoneNumber"
_DETAIL_FIELD_MASK = "id,displayName,formattedAddress,location,types,internationalPhoneNumber"

# ---------------------------------------------------------------------------
# Simple TTL cache (10-minute)
# ---------------------------------------------------------------------------
_cache: Dict[str, Tuple[float, Any]] = {}
_CACHE_TTL = 600  # 10 minutes


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < _CACHE_TTL:
        return entry[1]
    _cache.pop(key, None)
    return None


def _cache_set(key: str, value: Any):
    _cache[key] = (time.time(), value)


# ---------------------------------------------------------------------------
# Haversine distance helper (self-contained to avoid import cycles)
# ---------------------------------------------------------------------------
def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Core: Google Places API (New) — Text Search  (hospital type enforced)
# ---------------------------------------------------------------------------
def _places_text_search(query: str, lat: float, lon: float, page_size: int = 20) -> List[Dict]:
    """
    Call Places API (New) searchText with:
      - includedType: 'hospital'
      - strictTypeFiltering: True
    Returns raw list of place dicts from Google.
    """
    if not GOOGLE_PLACES_API_KEY:
        logger.warning("VITE_GOOGLE_PLACES_API_KEY not set — Google hospital search disabled")
        return []

    url = "https://places.googleapis.com/v1/places:searchText"
    body = {
        "textQuery": query,
        "includedType": "hospital",
        "strictTypeFiltering": True,
        "pageSize": page_size,
        "locationBias": {
            "circle": {
                "center": {"latitude": lat, "longitude": lon},
                "radius": 50000.0   # 50km bias (API max for locationBias)
            }
        },
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
                "X-Goog-FieldMask": _FIELD_MASK,
            }
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("places", [])
    except Exception as e:
        logger.warning("Google Places text search failed for %r: %s", query, e)
        return []


# ---------------------------------------------------------------------------
# Core: Google Places API (New) — Nearby Search  (hospital type enforced)
# ---------------------------------------------------------------------------
def _places_nearby_search(lat: float, lon: float, radius_m: float = 50000.0, max_results: int = 20) -> List[Dict]:
    """
    Call Places API (New) searchNearby with includedTypes: ['hospital'].
    Returns raw list of place dicts from Google.
    """
    if not GOOGLE_PLACES_API_KEY:
        return []

    url = "https://places.googleapis.com/v1/places:searchNearby"
    body = {
        "includedTypes": ["hospital"],
        "maxResultCount": min(max_results, 20),
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lon},
                "radius": radius_m
            }
        }
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
                "X-Goog-FieldMask": _FIELD_MASK,
            }
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("places", [])
    except Exception as e:
        logger.warning("Google Places nearby search failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Normalise a raw Google place dict → internal hospital dict
# ---------------------------------------------------------------------------
def _normalise_place(place: Dict, patient_lat: float, patient_lon: float) -> Optional[Dict]:
    """Convert a raw Google Places API place dict to our internal hospital format."""
    loc = place.get("location", {})
    lat = loc.get("latitude")
    lon = loc.get("longitude")
    if lat is None or lon is None:
        return None

    name = place.get("displayName", {}).get("text", "").strip()
    if not name:
        return None

    address = place.get("formattedAddress", "").strip()
    phone = place.get("internationalPhoneNumber", "").strip()
    place_id = place.get("id", "")
    dist_km = round(_haversine(patient_lat, patient_lon, lat, lon), 2)

    return {
        "name": name,
        "address": address,
        "latitude": lat,
        "longitude": lon,
        "distance_km": dist_km,
        "phone": phone,
        "google_place_id": place_id,
        # Downstream code expects these keys
        "source": "google_places",
    }


# ---------------------------------------------------------------------------
# Public: search hospitals (text search, hospital-only)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Strict Hospital-Only Fallback (used when Google Places API quota is 429)
# ---------------------------------------------------------------------------
_STRICT_HOSPITAL_KEYWORDS = [
    "hospital", "clinic", "nursing home", "medical centre", "medical center",
    "health centre", "health center", "super speciality", "multispeciality",
    "sanatorium", "infirmary", "charitable hospital", "institute of medical sciences",
    "cancer institute", "heart centre", "heart center", "eye care", "eye hospital",
    "ganga ram", "safdarjung", "aiims", "apollo", "fortis", "max", "medanta"
]


def _strict_hospital_fallback_search(query: str, patient_lat: float, patient_lon: float) -> List[Dict]:
    """
    Strict hospital-only fallback query executing ONLY when Google Places is exhausted (HTTP 429).
    Enforces that:
    - Never returns PIN codes, restaurants, shops, residences, or non-hospital amenities.
    - Result MUST be an authentic healthcare/hospital amenity or explicitly titled medical hospital.
    """
    clean_q = query.strip()
    if not clean_q or len(clean_q) < 2:
        return []

    # Pure numbers (e.g. PIN codes 110001) are strictly rejected
    if clean_q.isdigit():
        return []

    queries = [clean_q]
    if not any(k in clean_q.lower() for k in ["hospital", "clinic", "nursing", "medical", "centre", "center"]):
        queries.append(f"{clean_q} hospital")

    results = []
    seen = set()

    for query_str in queries:
        url = (
            f"https://nominatim.openstreetmap.org/search?q={urllib.parse.quote(query_str)}"
            f"&format=json&limit=15&countrycodes=in&addressdetails=1"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "AmbuPriority-HospitalSearch/2.0"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for item in data:
                    p_type = (item.get("type") or "").lower()
                    p_class = (item.get("class") or "").lower()
                    d_name = item.get("display_name") or ""
                    raw_name = item.get("name") or d_name.split(",")[0].strip()

                    # 1. Reject non-medical classes
                    if p_class not in ("amenity", "healthcare", "office", "building"):
                        continue
                    if p_type in (
                        "postcode", "restaurant", "cafe", "fast_food", "bar",
                        "hotel", "shop", "commercial", "road", "residential",
                        "reservoir", "water", "village", "suburb"
                    ):
                        continue

                    # 2. Must be verified hospital amenity or explicit hospital keyword in name
                    is_hosp_amenity = (p_class in ("amenity", "healthcare") and p_type in ("hospital", "clinic", "doctors"))
                    has_hosp_in_name = any(k in raw_name.lower() for k in _STRICT_HOSPITAL_KEYWORDS)
                    if not (is_hosp_amenity or has_hosp_in_name):
                        continue

                    # 3. Must be relevant to user's search query terms
                    q_words = [w.lower() for w in clean_q.split() if len(w) > 1 and w.lower() not in ("delhi", "in", "near", "the", "hospital")]
                    if q_words and not any(w in raw_name.lower() or w in d_name.lower() for w in q_words):
                        continue

                    if raw_name in seen:
                        continue
                    seen.add(raw_name)

                    lat = float(item["lat"])
                    lon = float(item["lon"])
                    dist = round(_haversine(patient_lat, patient_lon, lat, lon), 2)
                    results.append({
                        "name": raw_name,
                        "address": d_name,
                        "latitude": lat,
                        "longitude": lon,
                        "distance_km": dist,
                        "phone": "",
                        "google_place_id": "osm_" + str(item.get("place_id", "")),
                        "source": "verified_hospital_registry"
                    })
        except Exception as e:
            logger.info("Strict hospital search fallback error: %s", e)
        if results:
            break

    results.sort(key=lambda x: x["distance_km"])
    return results


def google_search_hospitals(query: str, patient_lat: float, patient_lon: float) -> List[Dict]:
    """
    Hospital-only search using Google Places API (New) Text Search.
    Returns list of normalised hospital dicts, nearest-first.
    Never returns non-hospital places (enforced by includedType + strictTypeFiltering).
    Falls back to strict hospital search if Google Places quota is exceeded (429).
    """
    cache_key = f"gsearch:{query.lower().strip()}:{round(patient_lat, 2)}:{round(patient_lon, 2)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    raw = _places_text_search(query, patient_lat, patient_lon)
    hospitals = []
    seen_ids = set()
    for place in raw:
        pid = place.get("id", "")
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        h = _normalise_place(place, patient_lat, patient_lon)
        if h:
            hospitals.append(h)

    # If Google Places returned 0 because of API quota or network failure,
    # use strict hospital-only search (only matches authentic hospitals)
    if not hospitals:
        fallback = _strict_hospital_fallback_search(query, patient_lat, patient_lon)
        if fallback:
            hospitals = fallback

    hospitals.sort(key=lambda x: x["distance_km"])
    _cache_set(cache_key, hospitals)
    return hospitals


# ---------------------------------------------------------------------------
# Public: nearby hospitals (location-based, hospital-only)
# ---------------------------------------------------------------------------
def google_nearby_hospitals(patient_lat: float, patient_lon: float, radius_km: float = 15.0) -> List[Dict]:
    """
    Hospital-only nearby search using Google Places API (New).
    Multi-ring searchNearby (e.g. 3km, 7km, 15km, 25km) plus locationBias text search
    to gather a comprehensive candidate pool of nearby hospitals.
    All candidates are deduplicated and sorted strictly nearest-first.
    """
    cache_key = f"gnearby:{round(patient_lat, 3)}:{round(patient_lon, 3)}:{radius_km}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    raw_places: List[Dict] = []
    
    # 1. Multi-ring Nearby Search for granular local radius coverage
    rings = [3000.0, 7000.0, 15000.0, min(radius_km * 1000.0, 50000.0)]
    rings = sorted(list(set(rings)))
    for r_m in rings:
        ring_results = _places_nearby_search(patient_lat, patient_lon, radius_m=r_m, max_results=20)
        raw_places.extend(ring_results)

    # 2. Text Search with locationBias to capture prominent tertiary/apex hospitals in the zone
    text_results = _places_text_search("hospital", patient_lat, patient_lon, page_size=20)
    raw_places.extend(text_results)

    hospitals = []
    seen_ids = set()
    for place in raw_places:
        pid = place.get("id", "")
        if pid and pid in seen_ids:
            continue
        if pid:
            seen_ids.add(pid)
        h = _normalise_place(place, patient_lat, patient_lon)
        if h:
            hospitals.append(h)

    # Calculate actual distance and sort ascending
    hospitals.sort(key=lambda x: x["distance_km"])
    _cache_set(cache_key, hospitals)
    return hospitals


# ---------------------------------------------------------------------------
# Public: autocomplete suggestions (hospital-only, fast)
# ---------------------------------------------------------------------------
def google_hospital_autocomplete(query: str, patient_lat: float, patient_lon: float) -> List[Dict]:
    """
    Hospital-only autocomplete using Google Places API (New) Text Search.
    Returns lightweight suggestion dicts: {kind, name, address, latitude, longitude, hospital_id}.
    """
    if len(query.strip()) < 2:
        return []

    cache_key = f"gac:{query.lower().strip()}:{round(patient_lat, 2)}:{round(patient_lon, 2)}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # Use a tighter page_size for autocomplete speed
    raw = _places_text_search(query, patient_lat, patient_lon, page_size=8)
    suggestions = []
    seen_ids = set()
    for place in raw:
        pid = place.get("id", "")
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        h = _normalise_place(place, patient_lat, patient_lon)
        if not h:
            continue
        # Build a concise address for autocomplete display
        addr_parts = h["address"].split(",")
        concise = ", ".join(p.strip() for p in addr_parts[:3] if p.strip())
        suggestions.append({
            "kind": "hospital",
            "name": h["name"],
            "address": concise,
            "latitude": h["latitude"],
            "longitude": h["longitude"],
            "hospital_id": None,  # will be enriched downstream if needed
            "distance_km": h["distance_km"],
            "google_place_id": h.get("google_place_id", ""),
        })

    # If Google Places returned 0 because of API quota or network failure,
    # use strict hospital-only search for autocomplete
    if not suggestions:
        fallback = _strict_hospital_fallback_search(query, patient_lat, patient_lon)
        for h in fallback[:8]:
            addr_parts = h["address"].split(",")
            concise = ", ".join(p.strip() for p in addr_parts[:3] if p.strip())
            suggestions.append({
                "kind": "hospital",
                "name": h["name"],
                "address": concise,
                "latitude": h["latitude"],
                "longitude": h["longitude"],
                "hospital_id": None,
                "distance_km": h["distance_km"],
                "google_place_id": h.get("google_place_id", ""),
            })

    _cache_set(cache_key, suggestions)
    return suggestions


# ---------------------------------------------------------------------------
# Utility: is Google Places available?
# ---------------------------------------------------------------------------
def is_google_places_available() -> bool:
    return bool(GOOGLE_PLACES_API_KEY)
