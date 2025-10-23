# /// script
# dependencies = [
#   "requests",
#   "fr24sdk"
# ]
# ///


from fr24sdk.client import Client
import requests
import re

API_TOKEN = xxx

TILES = [
    (30.0, 15.0, -85.0, -70.0),  # Florida - Western Caribbean
    (30.0, 15.0, -70.0, -55.0),  # Eastern Caribbean
    (15.0, 0.0, -85.0, -70.0),   # Colombia - Venezuela West
    (15.0, 0.0, -70.0, -55.0),   # Venezuela East - Trinidad
]

MIL_CALLSIGN_PREFIXES = ("RCH", "K35", "KOW", "AE",
                         "RFF", "FORTE", "PAT", "CNV", "MAZ")
MIL_TYPES = {
    "Q4", "C30J", "C17", "K35R", "E6", "P8", "E3CF", "P3", "E8", "U2",
    "C5M", "C130", "C130H", "C130J", "K35T", "K35E", "K10", "E3", "RC135",
}
CIVIL_BIZJET_TYPES = {
    "C750", "C56X", "C25A", "C25B", "C25C", "C550", "C680", "C68A",
    "GLF2", "GLF3", "GLF4", "GLF5", "GLF6", "FA50", "FA7X", "FA8X",
    "E55P", "E50P", "CL60", "LJ45", "LJ35", "LJ40", "LJ55", "LJ60",
}

CIVILIAN_KEYWORDS = [
    "aviation", "airlines", "airways", "jet", "charter", "aero", "private",
    "bizjet", "leasing", "helicopter", "avionics", "executive", "flight"
]
MILITARY_KEYWORDS = [
    "usaf", "navy", "air force", "raf", "marines", "military",
    "us navy", "us army", "royal air force", "canadian forces",
    "us coast guard", "usmc"
]


def fr24_link(f):
    cs, hx = f.get("callsign"), f.get("hex")
    if cs:
        return f"https://www.flightradar24.com/{cs}"
    if hx:
        return f"https://www.flightradar24.com/data/aircraft/{hx}"
    return "N/A"


def classify_operator_name(name: str) -> str:
    if not name:
        return "unknown"
    low = name.lower()
    if any(kw in low for kw in MILITARY_KEYWORDS):
        return "military"
    if any(kw in low for kw in CIVILIAN_KEYWORDS):
        return "civilian"
    return "unknown"


def strong_military_signals(f):
    """Return (is_strong, reasons)."""
    reasons = []
    hexcode = (f.get("hex") or "").upper()
    ac_type = (f.get("type") or "").upper()
    reg = f.get("reg") or ""
    painted = (f.get("painted_as") or "").upper()
    operating = (f.get("operating_as") or "").upper()

    if hexcode.startswith("AE"):
        reasons.append("hex AE**** (US mil)")
    if ac_type in MIL_TYPES:
        reasons.append(f"type {ac_type} (mil)")
    if reg and re.match(r"^\d{2}-\d{4}$", reg):
        reasons.append(f"military-style USAF reg {reg}")
    if painted in MIL_CALLSIGN_PREFIXES:
        reasons.append(f"painted_as {painted} (mil)")
    if operating in MIL_CALLSIGN_PREFIXES:
        reasons.append(f"operating_as {operating} (mil)")

    return len(reasons) > 0, reasons


def get_operator_with_fallbacks(client: Client, hexcode: str, reg: str | None):
    """Try multiple ways to fetch operator. Returns (operator_name, debug_reason)."""
    # 1) Direct by ICAO24
    if hexcode:
        try:
            a = client.aircraft.get(icao24=hexcode)
            op = a.model_dump().get("operator")
            if op:
                return op, "aircraft.get(icao24)"
        except Exception:
            pass

    # 2) Search by registration (most reliable for US civ)
    if reg:
        try:
            res = client.aircraft.search(query=reg).model_dump()
            items = res.get("items") or []
            if items:
                best = next((x for x in items if (
                    x.get("registration") or "").upper() == reg.upper()), items[0])
                op = best.get("operator") or best.get("owner")
                if op:
                    return op, "aircraft.search(reg)"
        except Exception:
            pass

    # 3) Search by hex
    if hexcode:
        try:
            res = client.aircraft.search(query=hexcode).model_dump()
            items = res.get("items") or []
            if items:
                op = items[0].get("operator") or items[0].get("owner")
                if op:
                    return op, "aircraft.search(hex)"
        except Exception:
            pass

    return None, "operator not found"


def should_keep_as_military(f, operator_name: str | None, strong_reasons: list):
    """
    Final decision:
    - Keep if strong military signals (hex/type/painted/operating/numeric reg)
    - Else, if operator explicitly civilian → drop
    - Else, check callsign BUT ONLY if paired with non-civil airframe (not in common bizjet list)
    """
    ac_type = (f.get("type") or "").upper()
    callsign = (f.get("callsign") or "").upper()

    if strong_reasons:
        return True, "strong signals: " + ", ".join(strong_reasons)

    # If we couldn’t prove mil, use operator to veto
    op_class = classify_operator_name(operator_name)
    if op_class == "civilian":
        return False, f"operator civilian: {operator_name}"

    # If callsign looks mil but bizjet type → drop
    if callsign.startswith(MIL_CALLSIGN_PREFIXES) and ac_type in CIVIL_BIZJET_TYPES:
        return False, f"callsign {callsign} but bizjet type {ac_type}"

    # If nothing else, treat as non-military
    return False, "no strong signals and no military operator"


def fetch_all_flights(client: Client):
    """Fetch and deduplicate flights across all tiles."""
    all_flights = {}
    for north, south, west, east in TILES:
        b = f"{north},{south},{west},{east}"
        resp = client.live.flight_positions.get_full(bounds=b)
        flights = resp.model_dump().get("data", [])
        for f in flights:
            key = f.get("hex") or f.get("fr24_id")
            if key:
                all_flights[key] = f
    return list(all_flights.values())


with Client(api_token=API_TOKEN) as client:
    flights = fetch_all_flights(client)
    print(f"Total flights in area after tiling: {len(flights)}")

    kept, dropped = [], []

    for f in flights:
        is_strong, reasons = strong_military_signals(f)

        op_name, op_src = (None, "skipped")
        if not is_strong:
            op_name, op_src = get_operator_with_fallbacks(
                client, f.get("hex") or "", f.get("reg"))

        keep, why = should_keep_as_military(f, op_name, reasons)

        if keep:
            kept.append((f, op_name, op_src, why))
        else:
            dropped.append((f, op_name, op_src, why))

    print(f"\n✅ Likely Military Flights: {len(kept)}")
    for f, op, src, why in kept:
        print(
            f"- {f.get('callsign')} | HEX {f.get('hex')} | "
            f"type {f.get('type')} | reg {f.get('reg')} | "
            f"op {op or 'N/A'} ({src}) | {why} | {fr24_link(f)}"
        )

    print(f"\n🚫 Excluded as Civil/Unknown: {len(dropped)}")
    for f, op, src, why in dropped:
        print(
            f"  ❌ {f.get('callsign')} {f.get('reg')} | op {op or 'N/A'} ({src}) | {why}"
        )
