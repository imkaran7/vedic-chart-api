from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from datetime import datetime, timezone
from dateutil import tz
from geopy.geocoders import Nominatim
import swisseph as swe
import os
import traceback

app = FastAPI(
    title="Vedic Chart Engine",
    version="1.0.0",
    servers=[{"url": "https://vedic-chart-api-ncha.onrender.com"}]
)

# Lahiri sidereal
swe.set_sid_mode(swe.SIDM_LAHIRI, 0, 0)
# Set ephemeris path (pyswisseph includes built-in minimal data;
# this also supports adding .se1 files later)
swe.set_ephe_path(os.environ.get("SE_EPHE_PATH", ""))

GEO = Nominatim(user_agent="vedic-chart-api")

PLANETS = {
    "Sun": swe.SUN,
    "Moon": swe.MOON,
    "Mars": swe.MARS,
    "Mercury": swe.MERCURY,
    "Jupiter": swe.JUPITER,
    "Venus": swe.VENUS,
    "Saturn": swe.SATURN,
    "Rahu": swe.TRUE_NODE,   # True node
    "Ketu": None,            # derived
}

SIGNS = [
    "Aries","Taurus","Gemini","Cancer","Leo","Virgo",
    "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"
]

def lon_to_sign_deg(lon: float):
    lon = lon % 360.0
    sign_index = int(lon // 30)
    deg = lon - sign_index * 30
    return SIGNS[sign_index], deg

class GeocodeReq(BaseModel):
    place: str = Field(..., examples=["Delhi, India"])

class GeocodeResp(BaseModel):
    lat: float
    lon: float
    tzid: str
    normalized_place: str

@app.post("/geocode", response_model=GeocodeResp)
def geocode_place(req: GeocodeReq):
    loc = GEO.geocode(req.place, timeout=20)
    if not loc:
        raise HTTPException(404, "Place not found")
    lat, lon = float(loc.latitude), float(loc.longitude)
    # Timezone detection removed for compatibility.
    # User/GPT must provide tzid (IANA) separately.
    return GeocodeResp(lat=lat, lon=lon, tzid="UNKNOWN", normalized_place=str(loc))


class NatalReq(BaseModel):
    date: str  # YYYY-MM-DD
    time: str  # HH:MM:SS
    tzid: str
    lat: float
    lon: float
    ayanamsha: str = "lahiri"

class PlanetOut(BaseModel):
    name: str
    lon: float
    sign: str
    degree: float

class NatalResp(BaseModel):
    birth_utc: str
    ascendant: PlanetOut
    planets: list[PlanetOut]

def to_utc(d: str, t: str, tzid: str) -> datetime:
    dt_local = datetime.fromisoformat(f"{d}T{t}")
    local_tz = tz.gettz(tzid)
    if local_tz is None:
        raise ValueError("Invalid tzid")
    dt_local = dt_local.replace(tzinfo=local_tz)
    return dt_local.astimezone(timezone.utc)

@app.post("/chart/natal", response_model=NatalResp)
def compute_natal(req: NatalReq):
    try:
        if req.ayanamsha.lower() != "lahiri":
            raise HTTPException(400, "Only lahiri supported")

        dt_utc = to_utc(req.date, req.time, req.tzid)

        jd_ut = swe.julday(
            dt_utc.year, dt_utc.month, dt_utc.day,
            dt_utc.hour + dt_utc.minute / 60 + dt_utc.second / 3600
        )

        # Use sidereal flags for planets
        flags = swe.FLG_SWIEPH | swe.FLG_SIDEREAL

        # Ascendant: use tropical houses() for stability, then subtract ayanamsa
        # (Ascendant is a point; sidereal = tropical - ayanamsa)
        hsys = b'P'
        cusps, ascmc = swe.houses(jd_ut, req.lat, req.lon, hsys)
        asc_trop = ascmc[0] % 360.0

        # Swiss Ephemeris gives ayanamsa for this JD
        ay = swe.get_ayanamsa_ut(jd_ut)
        asc_lon = (asc_trop - ay) % 360.0

        asc_sign, asc_deg = lon_to_sign_deg(asc_lon)

        planets_out: list[PlanetOut] = []

        for name, pid in PLANETS.items():
            if name == "Ketu":
                continue
            lon, _ = swe.calc_ut(jd_ut, pid, flags)[:2]
            lon = lon % 360.0
            sign, deg = lon_to_sign_deg(lon)
            planets_out.append(PlanetOut(name=name, lon=lon, sign=sign, degree=deg))

        rahu_lon = next(p.lon for p in planets_out if p.name == "Rahu")
        ketu_lon = (rahu_lon + 180.0) % 360.0
        ksign, kdeg = lon_to_sign_deg(ketu_lon)
        planets_out.append(PlanetOut(name="Ketu", lon=ketu_lon, sign=ksign, degree=kdeg))

        return NatalResp(
            birth_utc=dt_utc.isoformat(),
            ascendant=PlanetOut(name="Ascendant", lon=asc_lon, sign=asc_sign, degree=asc_deg),
            planets=sorted(planets_out, key=lambda x: x.name)
        )

    except HTTPException:
        raise
    except Exception as e:
        # Force full traceback into Render logs
        print("NATAL_ERROR:", repr(e))
        traceback.print_exc()
        raise HTTPException(500, "Internal error computing chart")
