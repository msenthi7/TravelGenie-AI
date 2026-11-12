import streamlit as st
import sys
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
import json

try:
    from anthropic import Anthropic  # type: ignore
    CLAUDE_AVAILABLE = True
except ImportError:
    CLAUDE_AVAILABLE = False

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT / "servers" / "flight_server"))
sys.path.insert(0, str(PROJECT_ROOT / "servers" / "hotel_server"))
sys.path.insert(0, str(PROJECT_ROOT / "servers" / "event_server"))
sys.path.insert(0, str(PROJECT_ROOT / "servers" / "geocoder_server"))
sys.path.insert(0, str(PROJECT_ROOT / "servers" / "weather_server"))
sys.path.insert(0, str(PROJECT_ROOT / "servers" / "finance_server"))
sys.path.insert(0, str(PROJECT_ROOT / "servers" / "traffic_crowd_server"))

try:
    import mcp.server.fastmcp
    import geopy
    import requests
except ImportError as e:
    print(f"ERROR: Missing required module: {e}")
    print("Please ensure you're running Streamlit with the virtual environment activated:")
    print("  source .venv/bin/activate")
    print("  streamlit run streamlit_app.py")
    print("\nOr use the provided script:")
    print("  ./run_streamlit.sh")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from claude_orchestrator import load_search_file, summarize_flights, summarize_hotels, summarize_weather

st.set_page_config(
    page_title="Travel Assistant - MCP Orchestration",
    layout="wide",
    initial_sidebar_state="expanded"
)

def safe_error(e) -> str:
    """Short, secret-free error text for the UI."""
    from claude_orchestrator import redact_secrets
    return redact_secrets(str(e))[:300]


# Common cities → main airport, so the Plan Trip form works without codes for typical demos.
CITY_AIRPORTS = {
    "reston": "IAD", "washington": "IAD", "dulles": "IAD", "arlington": "DCA", "banff": "YYC", "calgary": "YYC",
    "new york": "JFK", "nyc": "JFK", "boston": "BOS", "chicago": "ORD", "los angeles": "LAX", "san francisco": "SFO",
    "seattle": "SEA", "denver": "DEN", "miami": "MIA", "atlanta": "ATL", "dallas": "DFW", "austin": "AUS",
    "las vegas": "LAS", "orlando": "MCO", "vancouver": "YVR", "toronto": "YYZ", "montreal": "YUL",
    "london": "LHR", "paris": "CDG", "tokyo": "HND", "cancun": "CUN", "honolulu": "HNL",
}


def resolve_airport(code: str, city: str):
    """Return a valid IATA code from the form, or look up the city; None if unknown."""
    code = (code or "").strip().upper()
    if code:
        return code if len(code) == 3 and code.isalpha() else None
    city_l = (city or "").lower()
    for name, airport in CITY_AIRPORTS.items():
        if name in city_l:
            return airport
    return None


def show_debug_details(e) -> None:
    """Stack traces reveal code paths and data; show them only when debugging locally."""
    if os.getenv("TRAVELGENIE_DEBUG") == "1":
        import traceback
        from claude_orchestrator import redact_secrets
        st.code(redact_secrets(traceback.format_exc()))


@st.cache_resource
def get_server_keys():
    # Snapshot keys set by the host (env vars / Streamlit secrets) once per process,
    # before any visitor-entered key is written into os.environ.
    return {
        "SERPAPI_KEY": os.getenv("SERPAPI_KEY", ""),
        "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),
    }

SERVER_KEYS = get_server_keys()

if 'results' not in st.session_state:
    st.session_state.results = {}
if 'api_keys_set' not in st.session_state:
    st.session_state.api_keys_set = False

st.title("Travel Assistant - MCP Server Orchestration")
st.markdown("**Orchestrate 7 MCP servers** to create comprehensive travel plans")

with st.sidebar:
    st.header("API Configuration")
    
    # Never pass a server-side key to a widget: its value is sent to the browser.
    if SERVER_KEYS["SERPAPI_KEY"]:
        serpapi_key = SERVER_KEYS["SERPAPI_KEY"]
    else:
        serpapi_key = st.text_input(
            "SerpAPI Key",
            type="password",
            help="Required for flights, hotels, events, and finance servers"
        )

    st.info("Weather Server uses Open-Meteo API (free, no API key required)")

    if serpapi_key:
        os.environ["SERPAPI_KEY"] = serpapi_key
        st.session_state.api_keys_set = True
        st.success("SerpAPI key configured" if SERVER_KEYS["SERPAPI_KEY"] else "SerpAPI key set")
    else:
        st.warning("SerpAPI key required")
        st.session_state.api_keys_set = False
    
    st.divider()
    
    st.header("Server Status")
    server_status = {}
    
    servers_to_check = {
        "Flight Server": "servers.flight_server.flight_server",
        "Hotel Server": "servers.hotel_server.hotel_server",
        "Event Server": "servers.event_server.event_server",
        "Geocoder Server": "servers.geocoder_server.geocoder_server",
        "Weather Server": "servers.weather_server.weather_server",
        "Finance Server": "servers.finance_server.finance_server",
        "Traffic & Crowd Server": "servers.traffic_crowd_server.traffic_crowd_server"
    }
    
    for server_name, module_path in servers_to_check.items():
        try:
            __import__(module_path)
            server_status[server_name] = "Available"
        except Exception as e:
            server_status[server_name] = f"Error: {str(e)[:30]}"
    
    for server, status in server_status.items():
        st.text(f"{server}: {status}")

tab1, tab2, tab3, tab4 = st.tabs(["Plan Trip", "Chat with Claude", "Individual Searches", "View Results"])

with tab1:
    st.header("Complete Travel Planning")
    st.markdown("Fill out the form below to get a comprehensive travel plan")
    
    with st.form("travel_plan_form"):
        col1, col2 = st.columns(2)
        
        with col1:
            origin = st.text_input("Origin", placeholder="e.g., Reston, Virginia", key="origin")
            destination = st.text_input("Destination", placeholder="e.g., Banff, Alberta", key="dest")
            ac1, ac2 = st.columns(2)
            with ac1:
                origin_airport = st.text_input("From airport", placeholder="e.g., IAD", max_chars=3, key="origin_airport",
                                               help="3-letter IATA code. Leave blank to look up common cities.")
            with ac2:
                dest_airport = st.text_input("To airport", placeholder="e.g., YYC", max_chars=3, key="dest_airport",
                                             help="3-letter IATA code. Leave blank to look up common cities.")
            start_date = st.date_input("Start Date", value=datetime.now().date() + timedelta(days=7), key="start")
            end_date = st.date_input("End Date", value=datetime.now().date() + timedelta(days=14), key="end")
        
        with col2:
            adults = st.number_input("Adults", min_value=1, max_value=10, value=2, key="adults")
            children = st.number_input("Children", min_value=0, max_value=10, value=0, key="children")
            budget = st.number_input("Budget (USD)", min_value=0, value=5000, key="budget")
            currency = st.selectbox("Currency", ["USD", "CAD", "EUR", "GBP", "JPY"], key="currency")
        
        interests = st.multiselect(
            "Interests",
            ["Hiking", "Sight-seeing", "Dining", "Museums", "Nightlife", "Shopping", "Beaches", "Adventure Sports"],
            default=["Hiking", "Sight-seeing", "Dining", "Museums"],
            key="interests"
        )
        
        st.subheader("Select Servers to Use")
        col_flight, col_hotel, col_event, col_weather, col_finance = st.columns(5)
        
        with col_flight:
            use_flights = st.checkbox("Flights", value=True)
        with col_hotel:
            use_hotels = st.checkbox("Hotels", value=True)
        with col_event:
            use_events = st.checkbox("Events", value=True)
        with col_weather:
            use_weather = st.checkbox("Weather", value=True)
        with col_finance:
            use_finance = st.checkbox("Finance", value=True)
        
        submitted = st.form_submit_button("Plan My Trip", type="primary", use_container_width=True)
    
    if submitted:
        if not st.session_state.api_keys_set:
            st.error("Please enter your SerpAPI key in the sidebar")
            st.stop()
        
        if not origin or not destination:
            st.error("Please fill in origin and destination")
            st.stop()
        
        if end_date <= start_date:
            st.error("End date must be after start date")
            st.stop()

        if start_date < datetime.now().date():
            st.error("Start date is in the past")
            st.stop()

        origin_code = resolve_airport(origin_airport, origin)
        dest_code = resolve_airport(dest_airport, destination)
        if use_flights and not (origin_code and dest_code):
            st.warning("Flights skipped: enter 3-letter airport codes (e.g. IAD, YYC) for cities I can't look up.")
            use_flights = False
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        results_container = st.container()
        
        total_steps = sum([
            use_flights, use_hotels, use_events, use_weather, use_finance, True
        ])
        current_step = 0
        
        results = {}
        
        status_text.text("Step 1: Getting location coordinates...")
        progress_bar.progress(current_step / total_steps)
        try:
            from servers.geocoder_server.geocoder_server import geocode_location
            
            origin_result = geocode_location(origin)
            dest_result = geocode_location(destination)
            
            results["geocoding"] = {
                "origin": origin_result,
                "destination": dest_result
            }
            
            origin_location_data = origin_result.get("location_data", {})
            dest_location_data = dest_result.get("location_data", {})
            
            origin_lat = origin_location_data.get("latitude", "N/A")
            origin_lon = origin_location_data.get("longitude", "N/A")
            dest_lat = dest_location_data.get("latitude", "N/A")
            dest_lon = dest_location_data.get("longitude", "N/A")
            
            if origin_lat != "N/A" and dest_lat != "N/A":
                st.success(f"Geocoded: {origin} → {destination} | ({origin_lat}, {origin_lon}) → ({dest_lat}, {dest_lon})")
            else:
                st.warning(f"Geocoding incomplete: {origin} → {destination} | Check coordinates")
            current_step += 1
        except Exception as e:
            st.error(f"Geocoding error: {safe_error(e)}")
            results["geocoding"] = None
        
        if use_flights:
            status_text.text(f"Step {current_step + 1}/{total_steps}: Searching for flights...")
            progress_bar.progress(current_step / total_steps)
            try:
                from servers.flight_server.flight_server import search_flights
                
                flight_results = search_flights(
                    departure_id=origin_code,
                    arrival_id=dest_code,
                    outbound_date=start_date.strftime("%Y-%m-%d"),
                    return_date=end_date.strftime("%Y-%m-%d"),
                    adults=adults,
                    children=children,
                    currency=currency,
                    max_results=10
                )
                
                search_id = flight_results.get("search_id", "")
                if search_id:
                    try:
                        flight_file_path = os.path.join("flights", f"{search_id}.json")
                        if os.path.exists(flight_file_path):
                            with open(flight_file_path, 'r') as f:
                                full_flight_data = json.load(f)
                                flight_results["best_flights"] = full_flight_data.get("best_flights", [])
                                flight_results["other_flights"] = full_flight_data.get("other_flights", [])
                    except Exception as e:
                        st.warning(f"Could not load full flight data: {safe_error(e)}")
                
                results["flights"] = flight_results
                best_flights = flight_results.get("best_flights", [])
                other_flights = flight_results.get("other_flights", [])
                flight_count = len(best_flights) + len(other_flights)
                if flight_count > 0:
                    st.success(f"Found {flight_count} flight options ({len(best_flights)} best, {len(other_flights)} other)")
                else:
                    total_best = flight_results.get("total_best_flights", 0)
                    total_other = flight_results.get("total_other_flights", 0)
                    if total_best > 0 or total_other > 0:
                        st.success(f"Found {total_best + total_other} flight options ({total_best} best, {total_other} other)")
                    else:
                        st.warning(f"No flights found. Try using airport codes (e.g., 'IAD' for Reston area, 'YYC' for Calgary/Banff area)")
                current_step += 1
            except Exception as e:
                st.error(f"Flight search error: {safe_error(e)}")
                results["flights"] = None
        
        if use_hotels:
            status_text.text(f"Step {current_step + 1}/{total_steps}: Searching for hotels...")
            progress_bar.progress(current_step / total_steps)
            try:
                from servers.hotel_server.hotel_server import search_hotels
                
                hotel_results = search_hotels(
                    location=destination,
                    check_in_date=start_date.strftime("%Y-%m-%d"),
                    check_out_date=end_date.strftime("%Y-%m-%d"),
                    adults=adults,
                    children=children,
                    currency=currency,
                    max_results=10
                )
                
                search_id = hotel_results.get("search_id", "")
                if search_id:
                    try:
                        hotel_file_path = os.path.join("hotels", f"{search_id}.json")
                        if os.path.exists(hotel_file_path):
                            with open(hotel_file_path, 'r') as f:
                                full_hotel_data = json.load(f)
                                hotel_results["properties"] = full_hotel_data.get("properties", [])
                    except Exception as e:
                        st.warning(f"Could not load full hotel data: {safe_error(e)}")
                
                results["hotels"] = hotel_results
                hotel_count = hotel_results.get("total_properties", 0)
                if hotel_count > 0:
                    st.success(f"Found {hotel_count} hotel options")
                else:
                    st.warning(f"No hotels found for {destination}")
                current_step += 1
            except Exception as e:
                st.error(f"Hotel search error: {safe_error(e)}")
                results["hotels"] = None
        
        if use_weather:
            status_text.text(f"Step {current_step + 1}/{total_steps}: Getting weather forecast...")
            progress_bar.progress(current_step / total_steps)
            try:
                from servers.weather_server.weather_server import get_weather_forecast
                
                weather_results = get_weather_forecast(
                    location=destination,
                    forecast_days=5,
                    hourly=False
                )
                
                if results.get("geocoding") and results["geocoding"].get("destination"):
                    dest_result = results["geocoding"]["destination"]
                    dest_location_data = dest_result.get("location_data", {})
                    display_name = dest_location_data.get("display_name", destination)
                    
                    if "location" in weather_results:
                        weather_results["location"]["name"] = display_name
                        lat = dest_location_data.get("latitude")
                        lon = dest_location_data.get("longitude")
                        if lat and lon:
                            weather_results["location"]["coordinates"] = f"{lat}, {lon}"
                
                results["weather"] = weather_results
                st.success("Weather forecast retrieved")
                current_step += 1
            except Exception as e:
                st.error(f"Weather error: {safe_error(e)}")
                results["weather"] = None
        
        if use_events:
            status_text.text(f"Step {current_step + 1}/{total_steps}: Finding local events...")
            progress_bar.progress(current_step / total_steps)
            try:
                from servers.event_server.event_server import search_events
                
                # Google Events finds scheduled events (concerts, festivals...), not activities like hiking.
                event_query = "events"
                event_results = search_events(
                    query=event_query,
                    location=destination,
                    date_filter="month",
                    max_results=20
                )
                
                full_events = load_search_file("events", event_results.get("search_id")) or {}
                event_results["events"] = full_events.get("events_results", [])
                results["events"] = event_results
                event_count = len(event_results["events"])
                if event_count:
                    st.success(f"Found {event_count} events")
                else:
                    st.info(f"No scheduled events listed for {destination} in this period")
                current_step += 1
            except Exception as e:
                st.error(f"Event search error: {safe_error(e)}")
                results["events"] = None
        
        if use_finance:
            status_text.text(f"Step {current_step + 1}/{total_steps}: Analyzing budget...")
            progress_bar.progress(current_step / total_steps)
            try:
                from servers.finance_server.finance_server import convert_currency
                
                conversion_rate = 1.0
                if currency != "USD":
                    conversion_result = convert_currency(
                        amount=1,
                        from_currency=currency,
                        to_currency="USD"
                    )
                    if conversion_result and "converted_amount" in conversion_result:
                        conversion_rate = conversion_result["converted_amount"]
                
                results["currency_conversion"] = {
                    "rate": conversion_rate,
                    "from": currency,
                    "to": "USD"
                }
                
                total_cost = 0
                cost_breakdown = {}
                
                if results.get("flights"):
                    best_flights = results["flights"].get("best_flights", [])
                    other_flights = results["flights"].get("other_flights", [])
                    flights = best_flights + other_flights
                    prices = [f.get("price") for f in flights if isinstance(f.get("price"), (int, float))]
                    if prices:
                        flight_price = min(prices)
                        if flight_price:
                            total_cost += flight_price
                            cost_breakdown["flights"] = flight_price
                
                if results.get("hotels"):
                    hotels = results["hotels"].get("properties", [])
                    if hotels:
                        cheapest = min(hotels, key=lambda h: (h.get("rate_per_night") or {}).get("extracted_lowest") or float("inf"))
                        rate_info = cheapest.get("rate_per_night") or {}
                        price_per_night = rate_info.get("extracted_lowest") or 0
                        if price_per_night:
                            nights = (end_date - start_date).days
                            # Hotel rates are per room, not per guest; prefer the listed stay total.
                            hotel_total = (cheapest.get("total_rate") or {}).get("extracted_lowest") or price_per_night * nights
                            total_cost += hotel_total
                            cost_breakdown["hotels"] = hotel_total
                            cost_breakdown["hotels_per_night"] = price_per_night
                
                results["budget_analysis"] = {
                    "total_budget_usd": budget,
                    "estimated_cost_usd": total_cost * conversion_rate,
                    "remaining_budget": budget - (total_cost * conversion_rate),
                    "cost_breakdown": cost_breakdown,
                    "currency": currency
                }
                
                st.success("Budget analysis complete")
                current_step += 1
            except Exception as e:
                st.error(f"Finance error: {safe_error(e)}")
                results["currency_conversion"] = None
        
        progress_bar.progress(1.0)
        status_text.text("Complete!")
        
        st.session_state.results = results
        
        with results_container:
            st.header("Travel Plan Results")
            flight_rows = summarize_flights(results.get("flights") or {}, limit=10)
            hotel_rows = summarize_hotels(results.get("hotels") or {}, limit=10)
            weather_rows = []
            if results.get("weather") and not results["weather"].get("error"):
                weather_rows = summarize_weather(load_search_file("weather_data", results["weather"].get("search_id")) or {})
            events = (results.get("events") or {}).get("events", [])
            budget_info = results.get("budget_analysis")

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Flight options", len(flight_rows))
            m2.metric("Hotel options", len(hotel_rows))
            m3.metric("Events found", len(events))
            if budget_info:
                remaining = budget_info.get("remaining_budget", 0)
                m4.metric("Budget left" if remaining >= 0 else "Over budget", f"${abs(remaining):,.0f}",
                          delta=None if remaining >= 0 else "over", delta_color="inverse")

            if use_flights:
                st.subheader("✈️ Flights")
                if flight_rows:
                    st.dataframe([{
                        "Airline": r["airlines"], "Departs": r["depart"], "Arrives": r["arrive"],
                        "Stops": r["stops"], "Duration": f"{(r['duration_min'] or 0) // 60}h {(r['duration_min'] or 0) % 60}m",
                        "Price": f"${r['price']:,}" if isinstance(r["price"], (int, float)) else "n/a",
                    } for r in flight_rows], hide_index=True, use_container_width=True)
                else:
                    st.info(f"No flights found for {origin_code} → {dest_code} on these dates.")

            if use_hotels:
                st.subheader("🏨 Hotels")
                if hotel_rows:
                    nights = (end_date - start_date).days
                    st.dataframe([{
                        "Name": r["name"], "Type": r["type"],
                        "Rating": f"⭐ {r['rating']:.1f} ({r['reviews'] or 0})" if r["rating"] else "-",
                        "Per night": f"${r['price_per_night']:,}" if r["price_per_night"] else "-",
                        f"Total ({nights} nights)": f"${r['total_price']:,}" if r["total_price"] else "-",
                    } for r in hotel_rows], hide_index=True, use_container_width=True)
                else:
                    st.info(f"No hotels found for {destination} on these dates.")

            if use_weather:
                st.subheader("🌤️ Weather (next days)")
                if weather_rows:
                    st.dataframe([{
                        "Date": r["date"], "Conditions": r["conditions"],
                        "High": f"{r['high_c']}°C", "Low": f"{r['low_c']}°C", "Precip.": f"{r['precip_mm']} mm",
                    } for r in weather_rows], hide_index=True, use_container_width=True)
                    st.caption("Forecasts cover the next few days only, not necessarily your travel dates.")
                else:
                    st.info("Weather forecast unavailable.")

            if use_events:
                st.subheader("🎉 Events")
                if events:
                    for event in events[:8]:
                        when = (event.get("date") or {}).get("when", "")
                        venue = (event.get("venue") or {}).get("name") or ", ".join(event.get("address") or [])
                        link = event.get("link")
                        title = f"[{event.get('title', 'Event')}]({link})" if link else event.get("title", "Event")
                        st.markdown(f"- **{title}** · {when} · {venue}")
                else:
                    st.info(f"No scheduled events listed for {destination} in this period.")

            if budget_info:
                st.subheader("💰 Budget")
                breakdown = budget_info.get("cost_breakdown", {})
                b1, b2, b3 = st.columns(3)
                b1.metric("Your budget", f"${budget_info.get('total_budget_usd', 0):,.0f}")
                b2.metric("Cheapest flight + hotel", f"${budget_info.get('estimated_cost_usd', 0):,.0f}")
                b3.metric("Flight / hotel", f"${breakdown.get('flights', 0):,.0f} / ${breakdown.get('hotels', 0):,.0f}")
                remaining = budget_info.get("remaining_budget", 0)
                if remaining < 0:
                    st.warning(f"Over budget by ${-remaining:,.0f} before food and activities. Try fewer nights, other dates, or a cheaper area.")
                else:
                    st.success(f"${remaining:,.0f} left for food, activities and transport.")
                if not breakdown.get("flights") and use_flights:
                    st.caption("Flight cost not included: no flight prices were found.")

            with st.expander("Raw data (JSON)"):
                st.json(results, expanded=False)

            st.download_button(
                label="Download Full Results (JSON)",
                data=json.dumps(results, indent=2, default=str),
                file_name=f"travel_plan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                mime="application/json"
            )

with tab2:
    st.header("Chat with Claude - Travel Planning Assistant")
    st.markdown("**Have a conversation with Claude to plan your trip!** Claude will use all available travel services to help you.")
    
    if not CLAUDE_AVAILABLE:
        st.error("Anthropic SDK not installed. Please install it: `pip install anthropic`")
        st.stop()
    
    if SERVER_KEYS["ANTHROPIC_API_KEY"]:
        anthropic_api_key = SERVER_KEYS["ANTHROPIC_API_KEY"]
    else:
        anthropic_api_key = st.sidebar.text_input(
            "Anthropic API Key",
            type="password",
            help="Get your API key from https://console.anthropic.com/"
        )
    
    if not anthropic_api_key:
        st.warning("Please enter your Anthropic API key in the sidebar to use Claude chat.")
        st.info("You can also set it as an environment variable: `ANTHROPIC_API_KEY`")
        st.stop()
    
    if 'claude_messages' not in st.session_state:
        st.session_state.claude_messages = []
    
    if 'claude_client' not in st.session_state or st.session_state.get('claude_api_key') != anthropic_api_key:
        try:
            st.session_state.claude_client = Anthropic(api_key=anthropic_api_key)
            st.session_state.claude_api_key = anthropic_api_key
        except Exception as e:
            st.error(f"Error initializing Claude client: {safe_error(e)}")
            st.stop()
    
    try:
        from claude_orchestrator import get_tool_definitions, execute_tool, get_system_prompt, redact_secrets, tool_error_message
    except ImportError as e:
        st.error(f"Could not import orchestrator: {safe_error(e)}")
        st.stop()
    

    def run_chat_tool(tool_name, tool_input, tool_id, tool_status, failed_tools):
        """Run one tool for the chat tab: redact secrets, flag errors for Claude and the user."""
        try:
            result = execute_tool(tool_name, **tool_input)
            error = tool_error_message(result)
            text = json.dumps(result, indent=2, default=str)
        except Exception as e:  # execute_tool normally returns errors instead of raising
            error, text = str(e), json.dumps({"error": str(e)})
        block = {"type": "tool_result", "tool_use_id": tool_id, "content": redact_secrets(text)[:10000]}
        if error:
            block["is_error"] = True
            failed_tools.append(tool_name)
            tool_status.warning(f"**Failed:** `{tool_name}`: {redact_secrets(error)[:200]}")
        else:
            tool_status.success(f"**Completed:** `{tool_name}`")
        return block

    chat_container = st.container()
    with chat_container:
        for message in st.session_state.claude_messages:
            with st.chat_message(message["role"]):
                if message.get("content"):
                    st.markdown(message["content"])
                
                if "tool_calls" in message and message["tool_calls"]:
                    st.markdown("---")
                    st.markdown("**Tools Used:**")
                    for i, tool_call in enumerate(message["tool_calls"], 1):
                        tool_name = tool_call.get("name", "Unknown")
                        tool_input = tool_call.get("input", {})
                        
                        with st.expander(f"**{i}. {tool_name}**", expanded=False):
                            st.json(tool_input)
                
                if "status_messages" in message:
                    for status in message["status_messages"]:
                        st.info(f"{status}")
    
    user_input = st.chat_input("Ask me about travel planning... (e.g., 'Plan a trip to Banff from Reston, Virginia for June 7-14, 2025')")
    
    if user_input:
        st.session_state.claude_messages.append({"role": "user", "content": user_input})
        
        with st.chat_message("user"):
            st.markdown(user_input)
        
        with st.chat_message("assistant"):
            status_placeholder = st.empty()
            thinking_placeholder = st.empty()
            tool_execution_container = st.container()
            tool_calls_placeholder = st.empty()
            response_placeholder = st.empty()
            
            try:
                api_messages = []
                for msg in st.session_state.claude_messages[:-1]:
                    if msg["role"] == "user":
                        api_messages.append({
                            "role": "user",
                            "content": msg["content"]
                        })
                    elif msg["role"] == "assistant":
                        content = []
                        if msg.get("content"):
                            content.append({"type": "text", "text": msg["content"]})
                        if content:
                            api_messages.append({
                                "role": "assistant",
                                "content": content
                            })
                
                api_messages.append({
                    "role": "user",
                    "content": user_input
                })
                
                model_name = os.getenv("CLAUDE_MODEL", "claude-opus-5-5")
                thinking_placeholder.info("**Thinking...** Analyzing your request and planning the best approach.")

                response = st.session_state.claude_client.messages.create(
                    model=model_name,
                    max_tokens=16000,
                    system=get_system_prompt(),
                    messages=api_messages,
                    tools=get_tool_definitions()
                )

                thinking_placeholder.empty()
                
                assistant_message = {"role": "assistant", "content": "", "tool_calls": [], "status_messages": []}
                full_response = ""
                tool_calls_info = []
                failed_tools = []
                
                if response.stop_reason == "tool_use":
                    tool_results = []
                    tool_status_messages = []
                    
                    with tool_execution_container:
                        for idx, content_block in enumerate(response.content):
                            if content_block.type == "tool_use":
                                tool_name = content_block.name
                                tool_input = content_block.input
                                tool_id = content_block.id
                                
                                tool_call_info = {
                                    "name": tool_name,
                                    "input": tool_input
                                }
                                tool_calls_info.append(tool_call_info)
                                
                                tool_status = st.empty()
                                tool_status.info(f"**Executing:** `{tool_name}`")
                                
                                tool_results.append(run_chat_tool(tool_name, tool_input, tool_id, tool_status, failed_tools))
                    
                    if tool_calls_info:
                        tool_calls_placeholder.markdown("---")
                        tool_calls_placeholder.markdown("**Tools Executed:**")
                        for i, tool_call in enumerate(tool_calls_info, 1):
                            tool_name = tool_call.get("name", "Unknown")
                            tool_input = tool_call.get("input", {})
                            with tool_calls_placeholder.expander(f"**{i}. {tool_name}**", expanded=False):
                                st.json(tool_input)
                    
                    api_messages.append({
                        "role": "assistant",
                        "content": response.content
                    })
                    api_messages.append({
                        "role": "user",
                        "content": tool_results
                    })
                    
                    max_iterations = 5
                    iteration = 0
                    # Round 1's tools already ran above; get Claude's next turn before looping.
                    thinking_placeholder.info("**Analyzing results...** Synthesizing information.")
                    current_response = st.session_state.claude_client.messages.create(
                        model=model_name,
                        max_tokens=16000,
                        system=get_system_prompt(),
                        messages=api_messages,
                        tools=get_tool_definitions()
                    )
                    thinking_placeholder.empty()

                    while current_response.stop_reason == "tool_use" and iteration < max_iterations:
                        iteration += 1
                        tool_results = []
                        round_tool_calls = []
                        
                        if iteration > 1:
                            thinking_placeholder.info(f"**Analyzing results...** (Round {iteration})")
                        
                        with tool_execution_container:
                            for content_block in current_response.content:
                                if content_block.type == "tool_use":
                                    tool_name = content_block.name
                                    tool_input = content_block.input
                                    tool_id = content_block.id
                                    
                                    tool_call_info = {
                                        "name": tool_name,
                                        "input": tool_input
                                    }
                                    tool_calls_info.append(tool_call_info)
                                    round_tool_calls.append(tool_call_info)
                                    
                                    tool_status = st.empty()
                                    tool_status.info(f"**Executing:** `{tool_name}`")
                                    
                                    tool_results.append(run_chat_tool(tool_name, tool_input, tool_id, tool_status, failed_tools))
                        
                        if round_tool_calls:
                            tool_calls_placeholder.markdown("---")
                            tool_calls_placeholder.markdown(f"**Additional Tools Executed (Round {iteration}):**")
                            start_idx = len(tool_calls_info) - len(round_tool_calls) + 1
                            for i, tool_call in enumerate(round_tool_calls, start_idx):
                                tool_name = tool_call.get("name", "Unknown")
                                tool_input = tool_call.get("input", {})
                                with tool_calls_placeholder.expander(f"**{i}. {tool_name}**", expanded=False):
                                    st.json(tool_input)
                        
                        api_messages.append({
                            "role": "assistant",
                            "content": current_response.content
                        })
                        api_messages.append({
                            "role": "user",
                            "content": tool_results
                        })
                        
                        thinking_placeholder.info("**Analyzing results...** Synthesizing information.")
                        current_response = st.session_state.claude_client.messages.create(
                            model=model_name,
                            max_tokens=16000,
                            system=get_system_prompt(),
                            messages=api_messages,
                            tools=get_tool_definitions()
                        )
                        thinking_placeholder.empty()
                    
                    for content_block in current_response.content:
                        if content_block.type == "text":
                            full_response += content_block.text
                    
                    assistant_message["tool_calls"] = tool_calls_info
                
                else:
                    for content_block in response.content:
                        if content_block.type == "text":
                            full_response += content_block.text
                
                if full_response:
                    thinking_placeholder.empty()
                    status_placeholder.empty()
                    
                    if failed_tools:
                        names = ", ".join(sorted(set(failed_tools)))
                        full_response += f"\n\n> ⚠️ Some live lookups failed ({names}), so parts of this answer may be incomplete."
                    response_placeholder.markdown(full_response)
                    assistant_message["content"] = full_response
                else:
                    if tool_calls_info:
                        response_placeholder.info("Tools executed successfully. Processing results...")
                
                st.session_state.claude_messages.append(assistant_message)
                
            except Exception as e:
                error_msg = "Sorry, something went wrong while planning that. Please try again."
                response_placeholder.error(error_msg)
                show_debug_details(e)
                st.session_state.claude_messages.append({
                    "role": "assistant",
                    "content": error_msg,
                    "tool_calls": [],
                    "status_messages": []
                })
        
        st.rerun()
    
    if st.button("Clear Chat History"):
        st.session_state.claude_messages = []
        st.rerun()

with tab3:
    st.header("Individual Server Searches")
    st.markdown("Test individual MCP servers")
    
    server_choice = st.selectbox(
        "Select Server",
        ["Flight", "Hotel", "Event", "Geocoder", "Weather", "Finance"]
    )
    
    if server_choice == "Flight":
        st.subheader("Flight Search")
        with st.form("flight_form"):
            col1, col2 = st.columns(2)
            with col1:
                dep = st.text_input("Departure", "LAX")
                arr = st.text_input("Arrival", "JFK")
                out_date = st.date_input("Outbound Date", datetime.now().date() + timedelta(days=7))
            with col2:
                ret_date = st.date_input("Return Date", datetime.now().date() + timedelta(days=14))
                adults_count = st.number_input("Adults", 1, 10, 1)
            
            if st.form_submit_button("Search Flights"):
                try:
                    from servers.flight_server.flight_server import search_flights
                    flight_result = search_flights(
                        departure_id=dep,
                        arrival_id=arr,
                        outbound_date=out_date.strftime("%Y-%m-%d"),
                        return_date=ret_date.strftime("%Y-%m-%d"),
                        adults=adults_count
                    )
                    st.json(flight_result)
                except Exception as e:
                    st.error(f"Error: {safe_error(e)}")
    
    elif server_choice == "Hotel":
        st.subheader("Hotel Search")
        with st.form("hotel_form"):
            location = st.text_input("Location", "New York")
            check_in = st.date_input("Check-in", datetime.now().date() + timedelta(days=7))
            check_out = st.date_input("Check-out", datetime.now().date() + timedelta(days=14))
            adults_count = st.number_input("Adults", 1, 10, 2)
            
            if st.form_submit_button("Search Hotels"):
                try:
                    from servers.hotel_server.hotel_server import search_hotels
                    hotel_result = search_hotels(
                        location=location,
                        check_in_date=check_in.strftime("%Y-%m-%d"),
                        check_out_date=check_out.strftime("%Y-%m-%d"),
                        adults=adults_count
                    )
                    st.json(hotel_result)
                except Exception as e:
                    st.error(f"Error: {safe_error(e)}")
    
    elif server_choice == "Event":
        st.subheader("Event Search")
        with st.form("event_form"):
            query = st.text_input("Query", "concerts")
            location = st.text_input("Location", "San Francisco")
            date_filter = st.selectbox("Date Filter", ["today", "tomorrow", "week", "weekend", "month"])
            
            if st.form_submit_button("Search Events"):
                try:
                    from servers.event_server.event_server import search_events
                    event_result = search_events(
                        query=query,
                        location=location,
                        date_filter=date_filter
                    )
                    st.json(event_result)
                except Exception as e:
                    st.error(f"Error: {safe_error(e)}")
    
    elif server_choice == "Geocoder":
        st.subheader("Geocoder")
        with st.form("geocoder_form"):
            location = st.text_input("Location", "New York, NY")
            
            if st.form_submit_button("Geocode"):
                try:
                    from servers.geocoder_server.geocoder_server import geocode_location
                    geo_result = geocode_location(location)
                    st.json(geo_result)
                except Exception as e:
                    st.error(f"Error: {safe_error(e)}")
    
    elif server_choice == "Weather":
        st.subheader("Weather")
        st.info("Using Open-Meteo API (free, no API key required)")
        
        with st.form("weather_form"):
            input_type = st.radio(
                "Input Type",
                ["Location Name", "Coordinates"],
                horizontal=True
            )
            
            if input_type == "Location Name":
                location = st.text_input("Location", "New York, NY", help="City name, address, or location")
                forecast_days = st.slider("Forecast Days", 1, 16, 5)
                hourly = st.checkbox("Include Hourly Data", False)
            else:
                col1, col2 = st.columns(2)
                with col1:
                    lat = st.number_input("Latitude", value=40.7128, format="%.4f")
                with col2:
                    lon = st.number_input("Longitude", value=-74.0060, format="%.4f")
                forecast_days = st.slider("Forecast Days", 1, 16, 5)
                hourly = st.checkbox("Include Hourly Data", False)
                location = f"{lat},{lon}"
            
            col1, col2 = st.columns(2)
            with col1:
                if st.form_submit_button("Get Current Weather"):
                    try:
                        from servers.weather_server.weather_server import get_current_weather
                        weather_result = get_current_weather(location=location)
                        st.success("Current weather retrieved!")
                        st.json(weather_result)
                    except Exception as e:
                        st.error(f"Error: {safe_error(e)}")
                        show_debug_details(e)
            
            with col2:
                if st.form_submit_button("Get Forecast"):
                    try:
                        from servers.weather_server.weather_server import get_weather_forecast
                        weather_result = get_weather_forecast(
                            location=location,
                            forecast_days=forecast_days,
                            hourly=hourly
                        )
                        st.success("Weather forecast retrieved!")
                        st.json(weather_result)
                    except Exception as e:
                        st.error(f"Error: {safe_error(e)}")
                        show_debug_details(e)
    
    elif server_choice == "Finance":
        st.subheader("Finance")
        with st.form("finance_form"):
            amount = st.number_input("Amount", value=100.0)
            from_curr = st.selectbox("From Currency", ["USD", "CAD", "EUR", "GBP", "JPY"])
            to_curr = st.selectbox("To Currency", ["USD", "CAD", "EUR", "GBP", "JPY"])
            
            if st.form_submit_button("Convert Currency"):
                try:
                    from servers.finance_server.finance_server import convert_currency
                    finance_result = convert_currency(
                        amount=amount,
                        from_currency=from_curr,
                        to_currency=to_curr
                    )
                    st.json(finance_result)
                except Exception as e:
                    st.error(f"Error: {safe_error(e)}")

with tab4:
    st.header("Saved Results")
    
    if st.session_state.results:
        st.json(st.session_state.results)
        
        st.download_button(
            label="Download Results (JSON)",
            data=json.dumps(st.session_state.results, indent=2, default=str),
            file_name=f"travel_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            mime="application/json"
        )
    else:
        st.info("No results yet. Plan a trip in the 'Plan Trip' tab!")