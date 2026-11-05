import pandas as pd
from pyproj import Transformer
import folium
import requests
import streamlit as st
from streamlit_folium import st_folium
from streamlit_searchbox import st_searchbox
from streamlit_geolocation import streamlit_geolocation
import os
import psycopg2
from zoneinfo import ZoneInfo


def run_query(sql, params=(), fetch=False):
    conn = psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"),
        dbname=os.getenv("PGDATABASE", "postgres"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", ""),
    )
    try:
        with conn, conn.cursor() as cur:   # "with conn" commits on success
            cur.execute(sql, params)
            return cur.fetchall() if fetch else None
    finally:
        conn.close()
        
def add_hazard(street, description, lat, lon, hours, radius_m=50):
    run_query(
        "INSERT INTO obstacles (street, description, lat, lon, radius_m, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, now() + make_interval(hours => %s))",
        (street, description, lat, lon, radius_m, hours),
    )

@st.cache_data(ttl=30)
def get_active_obstacles():
    return run_query(
        "SELECT id, street, description, lat, lon, radius_m, expires_at "
        "FROM obstacles WHERE expires_at > now() ORDER BY created_at DESC",
        fetch=True,
    )

# ==============================STYLING============================== #
st.set_page_config(
    page_title="Roleo Bike Nav", 
    layout="wide",
    initial_sidebar_state="expanded"
)
st.markdown(
    """
    <style>
        /* pełnoekranowa mapa */
        .block-container { padding: 0 !important; margin: 0 !important; max-width: 100vw !important; }
        [data-testid="stVerticalBlock"] { gap: 0 !important; }
        footer, [data-testid="stToolbar"], [data-testid="stSidebar"],
        [data-testid="stSidebarCollapsedControl"] { display: none !important; }
        header[data-testid="stHeader"] { display: none !important; }
 
        /* tylko iframe mapy (nie wyszukiwarek!) */
        iframe[title="streamlit_folium.st_folium"] {
            height: 100vh !important; width: 100% !important;
            border: none !important; display: block !important;
        }
 
        /* mały, jasny panel nad mapą */
        .st-key-panel {
            position: fixed !important; top: 8px; left: 12px; width: 310px;
            z-index: 1000000; background: #ffffff;
            border-radius: 12px; padding: 2px 6px;
            box-shadow: 0 4px 18px rgba(0,0,0,0.22);
            border: 1px solid rgba(0,0,0,0.08);
        }
        .st-key-panel [data-testid="stVerticalBlock"] { gap: 0.3rem !important; }
        .st-key-panel [data-testid="stExpander"] details { border: none !important; }
        .st-key-panel [data-testid="stExpander"] summary { padding: 0.35rem 0.3rem !important; font-weight: 600; }
        .st-key-panel [data-testid="stExpanderDetails"] { padding: 0 0.3rem 0.4rem 0.3rem !important; }
 
        /* karta ze statystykami trasy */
        .route-hud {
            position: fixed; top: 12px; left: 50%; transform: translateX(-50%);
            z-index: 9998; background: rgba(255,255,255,0.96);
            padding: 8px 20px; border-radius: 999px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.18);
            font-family: sans-serif; font-size: 14px; font-weight: 600; color: #1e293b;
            white-space: nowrap;
        }
    </style>
    """,
    unsafe_allow_html=True,
)
# ==============================LOADING DATA============================== #
@st.cache_data()
def load_data() -> pd.DataFrame:
    df = pd.read_csv("Infrastruktura_rowerowa_ZTP.csv")
    transformer = Transformer.from_crs("EPSG:2178", "EPSG:4326", always_xy=True)
    df["lon"], df["lat"] = transformer.transform(df["X"].values, df["Y"].values)
    df[["nazwa","typ","opis"]] = df[["nazwa","typ","opis"]].fillna("")
    return df
df = load_data()
COLOR_krakow_map = {
    "stacja naprawy rowerów": "green", 
    "podpórka": "gray", "wiata": "blue", 
    "licznik rowerowy": "orange", 
    "znak": "red", 
    "boks": "white", 
    "wielopoziomowy parking": "purple"
}
DEFAULT_LAYERS = ["stacja naprawy rowerów"]
max_lat = df["lat"].max() + 0.02
min_lat = df["lat"].min() - 0.02
max_lon = df["lon"].max() + 0.02
min_lon = df["lon"].min() - 0.02
krakow_map = folium.Map(
    location=[50.0614, 19.9366], 
    zoom_start=14, 
    max_bounds=True,
    min_zoom=12,
    min_lat=min_lat,
    max_lat=max_lat,
    min_lon=min_lon,
    max_lon=max_lon,
    tiles="OpenStreetMap"
)

# ==============================MAPPING OBJECTS============================== #
def add_icons(selected_layers):
    for typ in selected_layers:
        group = folium.FeatureGroup(name=typ).add_to(krakow_map)
        for row in df[df["typ"] == typ].itertuples(index=False):
            folium.Marker(
                location=(row.lat, row.lon),
                tooltip=row.nazwa,
                popup=f"{row.typ}<br>{row.opis}",
                icon=folium.Icon(color=COLOR_krakow_map.get(typ, "blue")),
            ).add_to(group)
rows = get_active_obstacles() or []
obstacles = tuple((lon, lat, r) for _, _, _, lat, lon, r, _ in rows)   # (lon, lat, promień)

for hid, street, desc, lat, lon, radius, expires in rows:
    until = expires.astimezone(ZoneInfo("Europe/Warsaw")).strftime("%d.%m %H:%M")
    tip = f"🚧 {street}: {desc} (do {until})"
    folium.Circle((lat, lon), radius=radius, color="#dc2626", weight=2,
                  fill=True, fill_opacity=0.3, tooltip=tip).add_to(krakow_map)
    folium.CircleMarker((lat, lon), radius=4, color="#dc2626",
                        fill=True, fill_opacity=1, tooltip=tip).add_to(krakow_map)
# ==============================SEARCHING ADRESS============================== #
@st.cache_data(ttl=3600, show_spinner=False)
def finding_streets(street_name: str) -> list[tuple[str, str]]:
    if not street_name.strip():
        return []
    url = "https://photon.komoot.io/api/"
    params = {
        "q": street_name,
        "bbox": "19.78,49.96,20.22,50.13",
        "limit": 5,
    }
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36"}
    response = requests.get(url, params=params, headers=headers, timeout=5)
    
    features = response.json().get("features", [])
    results = []
    for feature in features:
        lon, lat = feature["geometry"]["coordinates"]

        props = feature["properties"]
        parts = [
            props.get("name"),
            props.get("street"),
            props.get("housenumber"),
            props.get("district"),
        ]
        label = ", ".join(str(p) for p in parts if p)

        if label:
            results.append((label, f"{lat},{lon}"))

    return results

def search_address(query: str) -> list[tuple[str, str]]:
    if len(query.strip()) < 3:
        return []
    try:
        return finding_streets(query.strip())
    except requests.RequestException:
        return []

def parse_point(value: str | None) -> tuple[float, float] | None:
    if not value:
        return None
    lat, lon = value.split(",")
    return float(lat), float(lon)

# ==============================SEARCHING FOR A NEAREST OBJECT============================== #
# nearest_obg = st.button("Znajdz najblirzszą stacje rowerową")
# if nearest_obg:

    

# ==============================BUILDING A ROUTE============================== #
@st.cache_data(ttl=600, show_spinner=False)
def build_a_route(lon1, lat1, lon2, lat2, obstacles: list[tuple[float, float, int]] | None = None):
    params = {
        "lonlats": f"{lon1},{lat1}|{lon2},{lat2}",
        "profile": "safety",
        "alternativeidx": 0,
        "format": "geojson"
    }
    if obstacles:
        params["nogos"] = "|".join(f"{lon},{lat},{r}" for lon, lat, r in obstacles)
    response = requests.get("https://brouter.de/brouter", params=params, timeout=10)
    return response.json() if response.ok else None

def draw_routes(a, b, obstacles, color, label):
    """a, b = (lat, lon). Rysuje trasę(-y) i zwraca tekst na pasek HUD (albo None)."""
    normal = build_a_route(a[1], a[0], b[1], b[0])                  # bez przeszkód
    if not (normal and normal.get("features")):
        return None
    m_n, s_n = route_stats(normal)

    detour = build_a_route(a[1], a[0], b[1], b[0], obstacles) if obstacles else None
    has_detour = bool(detour and detour.get("features"))
    if has_detour:
        m_d, s_d = route_stats(detour)
    blocked = has_detour and abs(m_d - m_n) > 5     # trasy się różnią -> przeszkoda leży na drodze

    if blocked:
        folium.GeoJson(
            normal, tooltip=folium.Tooltip(f"Bez objazdu: {s_n / 60:.0f} min"),
            style_function=lambda _: {"color": "#6b7280", "weight": 4, "opacity": 0.8, "dashArray": "8 8"},
        ).add_to(krakow_map)
        folium.GeoJson(
            detour,
            style_function=lambda _: {"color": color, "weight": 6, "opacity": 0.9},
        ).add_to(krakow_map)
        extra = max(1, round((s_d - s_n) / 60))
        return f"{label}: {s_d / 60:.0f} min • {m_d / 1000:.2f} km • ⚠️ objazd +{extra} min"

    folium.GeoJson(
        normal,
        style_function=lambda _: {"color": color, "weight": 6, "opacity": 0.9},
    ).add_to(krakow_map)
    text = f"{label}: {s_n / 60:.0f} min • {m_n / 1000:.2f} km"
    if obstacles and not has_detour:
        text += " • ⚠️ nie znaleziono objazdu"
    return text
# ==============================GET NAME BY LOCATION============================== #
@st.cache_data(ttl=3600, show_spinner=False)
def reverse_geocode(lat: float, lon: float) -> str:
    try:
        response = requests.get(
            "https://photon.komoot.io/reverse",
            params={"lat": lat, "lon": lon},
            headers={"User-Agent": "RoleoBikeNav/1.0"},
            timeout=5,
        )
        response.raise_for_status()
        features = response.json().get("features", [])
        if features:
            p = features[0]["properties"]
            label = " ".join(str(x) for x in (p.get("street") or p.get("name"), p.get("housenumber")) if x)
            if label:
                return label
    except requests.RequestException:
        pass
    return "Moja lokalizacja"


# ==============================REPORT AN OBSTACLE============================== #

DURATIONS = {"1 godzina": 1, "6 godzin": 6, "24 godziny": 24, "7 dni": 168}

@st.dialog("Zgłoś przeszkodę")
def report_dialog():
    query = st.text_input("Ulica / miejsce", placeholder="np. Karmelicka 10 (zatwierdź Enterem)")
    options = dict(search_address(query))            # etykieta -> "lat,lon"
    choice = st.selectbox("Wybierz adres", options=list(options.keys()))
    description = st.text_area("Opis", placeholder="np. rozkopana ścieżka rowerowa", max_chars=200)
    duration = st.selectbox("Jak długo?", options=list(DURATIONS.keys()))
    if st.button("Zgłoś", type="primary"):
        if not choice:
            st.error("Wpisz ulicę i wybierz adres z listy.")
            return
        point = parse_point(options[choice])
        if point is None:
            st.error("Nie udało się odczytać współrzędnych adresu.")
            return
        lat, lon = point
        add_hazard(choice, description, lat, lon, DURATIONS[duration])
        get_active_obstacles.clear()                 # odśwież listę zgłoszeń
        st.rerun()                                   # zamyka okno i odświeża mapę
# ==============================SIDE PANEL============================== #
with st.container(key="panel"):
    if st.button("Zgłoś przeszkodę", use_container_width=True):
        report_dialog()
    with st.expander("🚲 Roleo Bike Nav", expanded=True):
        c1, c2 = st.columns([1, 5])
        with c1:
            loc = streamlit_geolocation()
        with c2:
            st.caption("Użyj mojej lokalizacji jako startu")
        if loc and loc.get("latitude") is not None and loc != st.session_state.get("last_loc"):
            st.session_state["last_loc"] = loc
            lat, lon = round(loc["latitude"], 5), round(loc["longitude"], 5)
            st.session_state["my_location"] = (lat, lon, reverse_geocode(lat, lon))
        my_loc = st.session_state.get("my_location")
        if my_loc:
            f1, f2 = st.columns([6, 1])
            with f1:
                st.text_input("Start", value=f"{my_loc[2]}", disabled=True, label_visibility="collapsed")
            with f2:
                if st.button("✕", help="Wpisz inny start"):
                    del st.session_state["my_location"]
                    st.rerun()
            start_value = None
        else:
            start_value = st_searchbox(search_address, key="start", placeholder="Punkt startowy")
        finish_value = st_searchbox(
            search_address, key="finish", placeholder="Cel podróży"
        )
        selected_layers = st.multiselect(
            "Warstwy",
            options=list(COLOR_krakow_map.keys()),
            default=DEFAULT_LAYERS,
            placeholder="Warstwy na mapie",
            label_visibility="collapsed",
        )
        route_slot = st.empty()

    start = (my_loc[0], my_loc[1]) if my_loc else parse_point(start_value)
    finish = parse_point(finish_value)

# ==============================BUILD A ROUTE============================== #
hud_html=""
if start and finish:
    route = build_a_route(start[1], start[0], finish[1], finish[0])
    # route_geoJSON = route_geo.json()
    
    if route and route.get("features"):
        props = route["features"][0]["properties"]
        distance_km = float(props.get("track-length", 0)) / 1000
        time_min = float(props.get("total-time", 0)) / 60
        ascend_m = float(props.get("filtered ascend", 0))
    
        folium.GeoJson(
            route, 
            name="Trasa rowerowa",
            style_function=lambda _: {"color": "#2563eb", "weight": 6, "opacity": 0.85}
        ).add_to(krakow_map)
        
        folium.Marker(start, tooltip="Start", icon=folium.Icon(color="green", icon="play")).add_to(krakow_map)
        folium.Marker(finish, tooltip="Meta", icon=folium.Icon(color="red", icon="flag")).add_to(krakow_map)
        krakow_map.fit_bounds([start, finish], padding=(80, 80))
        hud_html = (
            f'<div class="route-hud">📏 {distance_km:.2f} km &nbsp;•&nbsp; '
            f'⏱️ {time_min:.0f} min &nbsp;•&nbsp; ⛰️ {ascend_m:.0f} m</div>'
        )
    else:
        route_slot.warning("Nie udało się wyznaczyć trasy.")
    
if hud_html:
    st.markdown(hud_html, unsafe_allow_html=True)

# ==============================RUNNING PROGRAM============================== #
add_icons(selected_layers)
st_folium(krakow_map, height=900, use_container_width=True, returned_objects=[])

