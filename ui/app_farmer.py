# ui/app_farmer.py
"""
Farmer Dashboard — Production-Grade Soil Advisory.

Fixes implemented:
  1. Profile page (name, email, farm name editable)
  2. Strict no-assumption agent (uses real user context)
  3. Quick questions actually call the agent
  4. Clear error reporting (no cryptic Errno -2)
  5. Consistent agent behavior (verifies data via tools)
  6. High-contrast indicator boxes (readable)
  7. Onboarding agent for plain-language data collection
  8. Dynamic "What's working for you" (based on real source status)
  9. Choose data source: connect sensor OR enter manually with agent
  10. Never assume — every value comes from the user's database
  11. Production sensor connection (test, validate, monitor)

Run:  streamlit run ui/app_farmer.py --server.port 8502
"""
import os
import json
import requests
import streamlit as st
import plotly.graph_objects as go
from datetime import datetime

API_BASE = os.getenv("ASDT_API_URL", "http://localhost:8080")
API_V1 = f"{API_BASE}/api/v1"

st.set_page_config(
    page_title="ASDT — Farmer Advisory",
    page_icon="🌱",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Custom CSS — HIGH CONTRAST ──────────────────────────────────────────────
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800&display=swap');

    .stApp { font-family: 'Nunito', sans-serif; }
    .main .block-container { padding-top: 1rem; max-width: 900px; margin: 0 auto; }

    .farmer-header {
        background: linear-gradient(135deg, #052e16 0%, #065f46 50%, #047857 100%);
        padding: 24px 30px; border-radius: 16px; margin-bottom: 24px; text-align: center;
        border: 1px solid #064e3b;
    }
    .farmer-header h1 { color: #ecfdf5; margin: 0; font-size: 28px; font-weight: 800; }
    .farmer-header p { color: #6ee7b7; margin: 6px 0 0 0; font-size: 15px; }

    /* Indicator boxes — dark-theme-compatible */
    .indicator-box {
        padding: 16px 20px;
        border-radius: 12px;
        margin: 8px 0;
        display: flex;
        justify-content: space-between;
        align-items: center;
        border: 2px solid;
    }
    .indicator-box strong { font-size: 17px; }
    .indicator-box .sub { font-size: 14px; opacity: 0.8; }

    .ind-good       { background: #052e16; border-color: #16a34a; color: #bbf7d0; }
    .ind-good *     { color: #bbf7d0 !important; }
    .ind-moderate   { background: #451a03; border-color: #d97706; color: #fde68a; }
    .ind-moderate * { color: #fde68a !important; }
    .ind-poor       { background: #450a0a; border-color: #dc2626; color: #fecaca; }
    .ind-poor *     { color: #fecaca !important; }
    .ind-nodata     { background: #161b22; border-color: #30363d; color: #8b949e; }
    .ind-nodata *   { color: #8b949e !important; }

    /* Alert cards */
    .alert-card { padding: 14px 18px; border-radius: 12px; margin: 8px 0; font-weight: 600; border-left: 4px solid; }
    .alert-warning { background: #451a03; border-color: #f59e0b; color: #fde68a; }
    .alert-warning * { color: #fde68a !important; }
    .alert-critical { background: #450a0a; border-color: #ef4444; color: #fecaca; }
    .alert-critical * { color: #fecaca !important; }

    /* Recommendation card */
    .rec-card {
        background: #161b22; border: 1px solid #21262d; border-radius: 14px;
        padding: 20px; margin: 12px 0;
    }
    .rec-card h4 { margin: 0 0 8px 0; color: #e6edf3; font-size: 16px; }
    .rec-card .detail { color: #8b949e; font-size: 14px; margin: 4px 0; }
    .rec-card .detail strong { color: #c9d1d9; }

    /* Layer abstraction icons */
    .layer-icon {
        display: inline-flex; align-items: center; gap: 6px;
        padding: 8px 16px; border-radius: 20px;
        font-size: 13px; margin: 4px; font-weight: 600;
        border: 2px solid;
    }
    .layer-active   { background: #052e16; color: #6ee7b7; border-color: #059669; }
    .layer-active * { color: #6ee7b7 !important; }
    .layer-inactive { background: #161b22; color: #6b7280; border-color: #30363d; }
    .layer-inactive * { color: #6b7280 !important; }

    /* Onboarding question card */
    .onboard-card {
        background: #161b22; border: 2px solid #059669; border-radius: 16px;
        padding: 24px; margin: 12px 0;
    }
    .onboard-card h3 { margin: 0 0 8px 0; color: #6ee7b7; }
    .onboard-card .hint { color: #8b949e; font-size: 14px; margin: 8px 0 16px 0; }

    /* Setup choice cards */
    .setup-card {
        background: #161b22; border: 2px solid #21262d; border-radius: 14px;
        padding: 24px; margin: 12px 0; cursor: pointer; transition: border-color 0.2s;
    }
    .setup-card:hover { border-color: #059669; }
    .setup-card h3 { color: #e6edf3; margin: 0 0 8px 0; }
    .setup-card p { color: #8b949e; margin: 0; }

    /* Profile box */
    .profile-box {
        background: #161b22; border: 1px solid #21262d;
        border-radius: 12px; padding: 20px; margin: 12px 0;
    }
    .profile-box h4 { color: #e6edf3; margin: 0 0 12px 0; }
    .profile-box p { color: #8b949e; }
    .profile-box strong { color: #c9d1d9; }

    div[data-testid="stMetric"] {
        background: #161b22; border: 1px solid #21262d;
        padding: 16px; border-radius: 12px;
    }
</style>
""", unsafe_allow_html=True)


# ── Session State ────────────────────────────────────────────────────────────

def init_session():
    defaults = {
        "token": None,
        "user": None,
        "chat_history": [],
        "current_parcel": None,
        "onboarding_session": None,
        "active_view": "dashboard",
        "pending_question": None,  # for quick question buttons
        "show_inline_onboarding": False,  # for inline data entry from dashboard
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def api_headers():
    return {"Authorization": f"Bearer {st.session_state.token}"} if st.session_state.token else {}


def api_get(path, params=None):
    try:
        r = requests.get(f"{API_V1}{path}", headers=api_headers(), params=params, timeout=10)
        if r.status_code == 401:
            st.session_state.token = None
            st.rerun()
        if r.status_code == 200:
            return r.json()
        return {"_error": r.text, "_status": r.status_code}
    except requests.ConnectionError:
        return {"_error": "Cannot connect to the soil advisory system. Please try again later."}
    except requests.Timeout:
        return {"_error": "Request timed out. Please try again."}


def api_post(path, data=None):
    try:
        r = requests.post(f"{API_V1}{path}", headers=api_headers(), json=data, timeout=30)
        if r.status_code in (200, 201):
            return r.json()
        return {"error": r.text, "_status": r.status_code}
    except requests.ConnectionError:
        return {"error": "Connection failed"}
    except requests.Timeout:
        return {"error": "Request timed out"}


def api_put(path, data=None):
    try:
        r = requests.put(f"{API_V1}{path}", headers=api_headers(), json=data, timeout=15)
        if r.status_code == 200:
            return r.json()
        return {"error": r.text, "_status": r.status_code}
    except requests.ConnectionError:
        return {"error": "Connection failed"}


# ── Login Page ───────────────────────────────────────────────────────────────

def login_page():
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown("""
        <div class="farmer-header">
            <h1>🌱 Soil Health Advisory</h1>
            <p>Your soil, your success — powered by ASDT</p>
        </div>
        """, unsafe_allow_html=True)

        tab1, tab2 = st.tabs(["Sign In", "Create Account"])

        with tab1:
            with st.form("login_form"):
                email = st.text_input("Email")
                password = st.text_input("Password", type="password")
                if st.form_submit_button("Sign In", use_container_width=True):
                    result = api_post("/auth/login", {"email": email, "password": password})
                    if result and "token" in result:
                        st.session_state.token = result["token"]
                        st.session_state.user = result
                        st.rerun()
                    else:
                        st.error(result.get("error", "Login failed"))

        with tab2:
            with st.form("register_form"):
                r_name = st.text_input("Your full name")
                r_email = st.text_input("Email address")
                r_password = st.text_input("Password (min 6 characters)", type="password")
                r_farm = st.text_input("Farm/Parcel name (optional)", placeholder="e.g., Owusu Family Farm")
                if st.form_submit_button("Create Account", use_container_width=True):
                    if not r_name or not r_email or len(r_password) < 6:
                        st.error("Please fill all required fields. Password needs 6+ characters.")
                    else:
                        result = api_post("/auth/register", {
                            "email": r_email, "password": r_password,
                            "name": r_name, "role": "farmer",
                            "parcels": [r_farm or f"farm_{r_name.lower().replace(' ', '_')}"],
                        })
                        if result and "user_id" in result:
                            # Sign in immediately rather than sending the user to another tab.
                            login = api_post("/auth/login", {"email": r_email, "password": r_password})
                            if login and "access_token" in login:
                                st.session_state.token = login["access_token"]
                                st.session_state.user = login.get("user", {})
                                st.rerun()
                            else:
                                st.success("✅ Account created successfully! Switch to the **Sign In** tab to log in.")
                        else:
                            st.error(result.get("error", "Registration failed"))


# ── Health Gauge ─────────────────────────────────────────────────────────────

def farmer_gauge(score):
    if score is None:
        return None
    try:
        score = float(score)
    except (ValueError, TypeError):
        return None

    color = "#059669" if score >= 75 else "#d97706" if score >= 50 else "#dc2626"
    label = "Healthy" if score >= 75 else "Needs Attention" if score >= 50 else "Urgent"

    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=score,
        title={"text": label, "font": {"size": 18, "color": "#8b949e", "family": "Nunito"}},
        gauge={
            "axis": {"range": [0, 100], "tickwidth": 1, "tickcolor": "#30363d"},
            "bar": {"color": color, "thickness": 0.3},
            "bgcolor": "#161b22", "borderwidth": 0,
            "steps": [
                {"range": [0, 40], "color": "#450a0a"},
                {"range": [40, 70], "color": "#451a03"},
                {"range": [70, 100], "color": "#052e16"},
            ],
        },
        number={"suffix": "", "font": {"size": 52, "color": "#e6edf3", "family": "Nunito"}},
    ))
    fig.update_layout(
        height=250, margin=dict(l=30, r=30, t=50, b=10),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font={"color": "#8b949e", "family": "Nunito"},
    )
    return fig


# ── Main Dashboard Router ───────────────────────────────────────────────────

def dashboard():
    user = st.session_state.user

    # Fetch profile to ensure we have latest name
    profile = api_get("/auth/profile")
    if profile and not profile.get("_error"):
        display_name = profile.get("name", user["name"])
    else:
        display_name = user["name"]

    # Header
    st.markdown(f"""
    <div class="farmer-header">
        <h1>🌱 Hello, {display_name}!</h1>
        <p>Here's how your soil is doing today</p>
    </div>
    """, unsafe_allow_html=True)

    # Get current parcel
    if not st.session_state.current_parcel:
        parcels_data = api_get("/parcels")
        if parcels_data and parcels_data.get("parcels"):
            st.session_state.current_parcel = parcels_data["parcels"][0]["parcel_id"]

    # Tabs
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "🏠 Dashboard", "📊 My Soil Data", "💬 Ask Advisor",
        "📋 History", "👤 Profile",
    ])

    with tab1:
        render_dashboard()
    with tab2:
        render_soil_data()
    with tab3:
        render_chat()
    with tab4:
        render_history()
    with tab5:
        render_profile()

    # Sign out
    st.divider()
    col1, col2, col3 = st.columns([1, 1, 1])
    with col2:
        if st.button("Sign Out", use_container_width=True):
            for key in ["token", "user", "chat_history", "current_parcel",
                        "onboarding_session", "pending_question"]:
                st.session_state[key] = None if key != "chat_history" else []
            st.rerun()


# ── Dashboard Tab ───────────────────────────────────────────────────────────

def render_dashboard():
    parcel_id = st.session_state.current_parcel
    if not parcel_id:
        st.warning("No parcel found. Please add a parcel in your Profile.")
        return

    data = api_get("/ui/farmer/dashboard", {"parcel_id": parcel_id})

    if not data or data.get("_error"):
        err = data.get("_error", "Cannot load dashboard") if data else "Cannot connect to server"
        st.error(f"Dashboard error: {err}")
        st.caption("Try refreshing the page. If this persists, check `docker logs asdt_twin_api`.")
        return

    # Onboarding flow if not configured
    if not data.get("configured"):
        render_setup_choice(parcel_id, ctx="dashboard")
        return

    # If configured but no data yet
    if not data.get("has_data"):
        mode = data.get("mode")
        st.info(f"Your data source is set to **{mode}** mode, but no readings have arrived yet.")
        if mode == "manual":
            if st.button("📝 Enter your soil data now", use_container_width=True):
                st.session_state.onboarding_session = None
                st.rerun()
            render_onboarding_flow(parcel_id, ctx="dashboard")
        elif mode == "sensor":
            sensor_status = data.get("sensor_status", {})
            st.markdown("### Sensor Connection Status")
            status = sensor_status.get("status", "unknown")
            if status == "active":
                st.success(f"✅ Sensor connected. Waiting for first message...")
            elif status == "connected":
                st.info("✅ Connected to broker. Waiting for sensor messages...")
            elif status == "disconnected":
                st.warning("⚠️ Sensor disconnected. Will auto-reconnect.")
            elif status == "error":
                st.error(f"❌ Sensor error: {sensor_status.get('last_error', 'Unknown')}")
            else:
                st.info("Sensor connection status: " + status)

            if st.button("🔄 Refresh status"):
                st.rerun()
            if st.button("⚙️ Reconfigure sensor"):
                api_post(f"/sensor/stop/{parcel_id}")
                # Reset to setup
                set_result = api_post("/data-source", {
                    "parcel_id": parcel_id, "mode": "manual", "config": {}
                })
                st.rerun()
        return

    # === We have actual data ===

    # ── Inline onboarding (triggered by "Add missing data" / "Add more measurements") ──
    # Shows INSTEAD of dashboard content so user doesn't have to scroll
    if st.session_state.get("show_inline_onboarding"):
        st.markdown("### 📝 Enter More Soil Data")
        st.caption("Your answers are saved as you go. You can stop anytime and continue later.")
        col_back, _ = st.columns([1, 3])
        with col_back:
            if st.button("← Back to dashboard", key="back_from_inline_onboard"):
                # Save partial progress before going back
                session = st.session_state.get("onboarding_session")
                if session and session.get("answers"):
                    api_post("/onboarding/commit", {
                        "parcel_id": parcel_id,
                        "session_state": session,
                    })
                st.session_state.show_inline_onboarding = False
                # Keep the session so user can resume later
                st.rerun()
        render_onboarding_flow(parcel_id, ctx="inline_dash")
        return  # Don't render the rest of the dashboard

    # Handle sensor switch request from dashboard
    if st.session_state.get("setup_mode") == "sensor" and data.get("mode") == "manual":
        st.info("You can connect a sensor to automatically collect soil data. Your existing manual readings will be preserved.")
        render_sensor_setup(parcel_id, ctx="dash_switch")
        if st.button("← Back to dashboard", key="back_from_sensor_switch"):
            st.session_state.setup_mode = None
            st.rerun()
        return

    # Health gauge + indicators
    col1, col2 = st.columns([1, 1])

    with col1:
        score = data.get("health_score")
        if score is not None:
            fig = farmer_gauge(score)
            if fig:
                st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Add more measurements to see your health score")

    with col2:
        st.markdown("### How your soil is doing")
        completion = data.get("completion_pct", 0)
        st.progress(completion / 100, text=f"Data completeness: {completion:.0f}%")

        for key, ind in data.get("indicators", {}).items():
            status = ind.get("status", "no_data")
            label = ind.get("label", key)
            emoji = {
                "good": "✅", "adequate": "✅", "optimal": "✅", "active": "✅",
                "moderate": "⚠️", "dry": "⚠️", "needs_lime": "⚠️",
                "poor": "❌", "waterlogged": "🌊", "slow": "❌",
                "no_data": "❓",
            }.get(status, "❓")
            friendly = "No data yet" if status == "no_data" else status.replace("_", " ").title()
            st.markdown(f'{emoji} **{label}:** {friendly}')

    st.divider()

    # Alerts
    alerts = data.get("alerts", [])
    if alerts:
        st.markdown("### ⚠️ Alerts")
        for a in alerts:
            sev = a.get("severity", "warning")
            cls = "alert-critical" if sev == "critical" else "alert-warning"
            icon = "🚨" if sev == "critical" else "⚠️"
            st.markdown(
                f'<div class="alert-card {cls}">{icon} {a.get("message", "")}</div>',
                unsafe_allow_html=True,
            )
    else:
        st.success("No alerts — your soil looks good!")

    # ── Physical status cards ────────────────────────────────────────────────
    phys = api_get("/physical/status")
    if phys:
        _render_physical_cards(phys, parcel_id)

    # Missing fields callout
    if data.get("fields_missing"):
        with st.expander(f"📋 {len(data['fields_missing'])} measurements still needed"):
            st.caption("Add these to get more accurate recommendations:")
            for f in data["fields_missing"]:
                st.text(f"  • {f.replace('_', ' ').title()}")
            if st.button("Add missing data now", use_container_width=True):
                st.session_state.show_inline_onboarding = True
                st.rerun()

    st.divider()

    # Latest recommendation
    rec = data.get("latest_recommendation")
    if rec:
        st.markdown("### 📋 Latest Recommendation")
        st.markdown(f"""
        <div class="rec-card">
            <h4>🧪 {rec.get('product', 'Recommendation')}</h4>
            <div class="detail">📏 <strong>How much:</strong> {rec.get('rate_kg_ha', '—')} kg per hectare</div>
            <div class="detail">📅 <strong>When:</strong> {rec.get('timing', '—')}</div>
            <div class="detail">🔧 <strong>How:</strong> {rec.get('method', '—')}</div>
            <div class="detail">💡 <strong>Why:</strong> {rec.get('rationale', '—')}</div>
        </div>
        """, unsafe_allow_html=True)

        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("✅ I applied this", use_container_width=True, key="dash_apply"):
                api_post("/ui/farmer/feedback", {
                    "recommendation_id": rec.get("recommendation_id", "unknown"),
                    "accepted": True, "outcome": "applied", "rating": 5,
                })
                st.success("Recorded!")
        with col2:
            if st.button("ℹ️ Need more info", use_container_width=True, key="dash_info"):
                st.info("Switch to the 'Ask Advisor' tab to discuss with the AI.")
        with col3:
            if st.button("🚨 Report a problem", use_container_width=True, key="dash_problem"):
                api_post("/ui/farmer/escalate")
                st.success("A scientist will review your parcel.")
    else:
        st.info("No recommendations yet. As you add more data, you'll get tailored advice.")

    # Dynamic "What's working for you" (fix #8 — now truly dynamic)
    st.divider()
    st.markdown("### 🔧 What's working for you")

    mode = data.get("mode")
    completion = data.get("completion_pct", 0)
    fields_with = data.get("fields_with_data", [])
    fields_missing = data.get("fields_missing", [])

    layers = []

    # Data source layer — reflects actual completeness
    if mode == "sensor":
        sensor_status = data.get("sensor_status", {})
        if sensor_status.get("status") == "active":
            msg_count = sensor_status.get("message_count", 0)
            layers.append(("📡 Real-time sensors", True, f"{msg_count} messages received"))
        else:
            layers.append(("📡 Real-time sensors", False, "Connecting..."))
    else:
        if completion >= 100:
            layers.append(("📝 Manual data entry", True, f"All {len(fields_with)} fields complete"))
        elif completion > 0:
            layers.append(("📝 Manual data entry", True, f"{len(fields_with)}/{len(fields_with) + len(fields_missing)} fields"))
        else:
            layers.append(("📝 Manual data entry", False, "No data yet"))

    # Alerts — active only if data exists to alert on
    if completion > 0:
        layers.append(("🔔 Smart alerts", True, f"{len(alerts)} active"))
    else:
        layers.append(("🔔 Smart alerts", False, "Waiting for data"))

    # Recommendations — active only if we have enough data AND a recommendation exists
    if rec:
        layers.append(("🩺 Soil doctor", True, "Recommendation ready"))
    elif completion >= 50:
        layers.append(("🩺 Soil doctor", True, "Analyzing your data..."))
    else:
        layers.append(("🩺 Soil doctor", False, f"Need {50 - completion:.0f}% more data"))

    # Cross-domain sync
    cd_active = data.get("cross_domain_active", False)
    cd_info = data.get("cross_domain_info") or {}
    cd_sub = "Connected" if cd_active else "Not connected"
    if cd_active and cd_info.get("last_received"):
        cd_sub = f"Last sync: {cd_info['last_received'][:16]}"
    layers.append(("🌐 Crop advisor sync", cd_active, cd_sub))

    html_layers = ""
    for name, active, sub in layers:
        cls = "layer-active" if active else "layer-inactive"
        html_layers += f'<div class="layer-icon {cls}" title="{sub}">{name}</div>'

    st.markdown(f'<div>{html_layers}</div>', unsafe_allow_html=True)

    # ── Data completeness encouragement ──────────────────────────────────────
    if completion < 100:
        st.divider()
        st.markdown("### 📊 Complete your soil profile")

        if completion >= 70:
            st.success(
                f"🎉 Great progress! You're at **{completion:.0f}%** — your dashboard is already "
                f"working with the data you've provided. Adding the remaining "
                f"**{len(fields_missing)} field(s)** will make your recommendations even more accurate."
            )
        elif completion >= 30:
            st.info(
                f"You're at **{completion:.0f}%** data completeness. The more fields you fill in, "
                f"the better your soil health score and recommendations will be."
            )
        else:
            st.warning(
                f"You've only entered **{completion:.0f}%** of your soil data. "
                f"Add more measurements to unlock recommendations and alerts."
            )

        col_add, col_sensor = st.columns(2)
        with col_add:
            if st.button("📝 Add more measurements", use_container_width=True, key="dash_add_more"):
                st.session_state.show_inline_onboarding = True
                st.rerun()
        with col_sensor:
            if mode == "manual":
                if st.button("📡 Switch to sensor instead", use_container_width=True, key="dash_switch_sensor"):
                    st.session_state.setup_mode = "sensor"
                    st.session_state.onboarding_session = None
                    st.rerun()


# ── Setup Choice (Sensor vs Manual) ─────────────────────────────────────────

def render_setup_choice(parcel_id: str, ctx: str = "default"):
    st.markdown("### 🌱 Welcome! Let's get your soil data set up")
    st.caption("Choose how you want to provide soil information for this parcel.")

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("""
        <div class="setup-card">
            <h3>📡 Connect a Sensor</h3>
            <p>If you have an IoT soil sensor, connect it here for automatic real-time data.</p>
        </div>
        """, unsafe_allow_html=True)
        if st.button("Connect my sensor", key=f"choose_sensor_{ctx}", use_container_width=True, type="primary"):
            st.session_state.setup_mode = "sensor"
            st.rerun()

    with col2:
        st.markdown("""
        <div class="setup-card">
            <h3>📝 Enter Data Manually</h3>
            <p>Don't have a sensor? Our advisor will walk you through entering your data step by step.</p>
        </div>
        """, unsafe_allow_html=True)
        if st.button("Enter manually with help", key=f"choose_manual_{ctx}", use_container_width=True, type="primary"):
            st.session_state.setup_mode = "manual"
            st.rerun()

    # Show appropriate flow
    if st.session_state.get("setup_mode") == "sensor":
        st.divider()
        render_sensor_setup(parcel_id, ctx=f"setup_{ctx}")
    elif st.session_state.get("setup_mode") == "manual":
        st.divider()
        render_onboarding_flow(parcel_id, ctx=f"setup_{ctx}")


# ── Sensor Setup Flow (production-grade) ───────────────────────────────────

def render_sensor_setup(parcel_id: str, ctx: str = "default"):
    st.markdown("### 📡 Connect Your IoT Sensor")
    st.caption("Enter your sensor's MQTT broker details. We'll test the connection before saving.")

    with st.form(f"sensor_setup_form_{ctx}"):
        st.markdown("**Connection Details**")
        broker = st.text_input("MQTT Broker (host)", placeholder="e.g., broker.hivemq.com")
        port = st.number_input("Port", value=1883, step=1)
        topic = st.text_input("Topic to subscribe to", placeholder="e.g., farm/owusu/soil")

        st.markdown("**Authentication (optional)**")
        username = st.text_input("Username (leave blank if none)")
        password = st.text_input("Password", type="password")
        use_tls = st.checkbox("Use TLS/SSL (recommended for production)")

        with st.expander("Advanced: Field name mapping"):
            st.caption("If your sensor uses different field names, map them to our standard fields.")
            st.text("Example: {'moisture': 'soil_moisture_pct', 'ph': 'soil_ph'}")
            mapping_str = st.text_area(
                "Field mapping (JSON)", value="{}", height=100,
                help="Leave as {} if your sensor already uses our standard field names",
            )

        col1, col2 = st.columns(2)
        with col1:
            test_btn = st.form_submit_button("🧪 Test Connection", use_container_width=True)
        with col2:
            save_btn = st.form_submit_button("💾 Save & Connect", use_container_width=True, type="primary")

        if test_btn:
            if not broker:
                st.error("Please enter a broker host")
            else:
                with st.spinner("Testing connection..."):
                    result = api_post("/sensor/test", {
                        "broker": broker, "port": int(port),
                        "username": username, "password": password,
                        "topic": topic, "use_tls": use_tls,
                    })
                if result.get("success"):
                    st.success(f"✅ {result.get('message')} (latency: {result.get('latency_ms')}ms)")
                else:
                    st.error(f"❌ {result.get('message')}")

        if save_btn:
            if not broker or not topic:
                st.error("Broker and topic are required")
            else:
                try:
                    field_mapping = json.loads(mapping_str)
                except json.JSONDecodeError:
                    st.error("Field mapping must be valid JSON")
                    return

                # Test first
                with st.spinner("Validating connection..."):
                    test = api_post("/sensor/test", {
                        "broker": broker, "port": int(port),
                        "username": username, "password": password,
                        "topic": topic, "use_tls": use_tls,
                    })

                if not test.get("success"):
                    st.error(f"Cannot save — connection test failed: {test.get('message')}")
                    return

                # Save config
                config = {
                    "broker": broker, "port": int(port), "topic": topic,
                    "username": username, "password": password,
                    "use_tls": use_tls, "field_mapping": field_mapping,
                }
                save_result = api_post("/data-source", {
                    "parcel_id": parcel_id, "mode": "sensor", "config": config,
                })

                if save_result.get("error"):
                    st.error(f"Save failed: {save_result['error']}")
                else:
                    st.success("✅ Sensor connected! Data will start arriving shortly.")
                    st.session_state.setup_mode = None
                    st.rerun()


# ── Onboarding Flow (Q&A agent) ─────────────────────────────────────────────

def render_onboarding_flow(parcel_id: str, ctx: str = "default"):
    st.markdown("### 📝 Let's enter your soil data")
    st.caption("I'll ask you a few questions about your soil. Answer what you know — skip the rest.")

    # Start session if needed
    if st.session_state.onboarding_session is None:
        result = api_post(f"/onboarding/start?parcel_id={parcel_id}")
        if result.get("error"):
            st.error(result["error"])
            return
        st.session_state.onboarding_session = result["session"]

    session = st.session_state.onboarding_session

    # If complete
    if session.get("complete"):
        saved = session.get("_commit_saved", len(session.get("answers", {})))
        st.success(f"✅ Thanks! Saved {saved} measurements.")
        st.balloons()
        if st.button("Go to my dashboard", key=f"goto_dash_{ctx}", use_container_width=True):
            st.session_state.onboarding_session = None
            st.session_state.setup_mode = None
            st.session_state.show_inline_onboarding = False
            st.rerun()
        return

    # Get questions
    questions_data = api_get("/onboarding/questions")
    if not questions_data:
        st.error("Cannot load questions")
        return

    questions = questions_data["questions"]
    total = questions_data["total"]
    idx = session["current_index"]

    if idx >= total:
        session["complete"] = True
        st.session_state.onboarding_session = session
        st.rerun()
        return

    q = questions[idx]

    # Progress
    st.progress((idx) / total, text=f"Question {idx + 1} of {total}")

    # Show previously answered fields as a summary (collapsible)
    answers = session.get("answers", {})
    soil_type_choice = session.get("soil_type")
    answered_count = len(answers) + (1 if soil_type_choice else 0)
    if answered_count > 0:
        with st.expander(f"✅ {answered_count} answer(s) saved so far — click to review"):
            if soil_type_choice:
                st.markdown(f"🌍 **Soil Type:** {soil_type_choice.title()}")
            for field, val in answers.items():
                label = field.replace("_", " ").title()
                # Find the unit from questions list
                q_info = next((qq for qq in questions if qq["field"] == field), {})
                unit = q_info.get("unit", "")
                st.markdown(f"**{label}:** {val} {unit}")

    # Question card
    st.markdown(f"""
    <div class="onboard-card">
        <h3>{q['icon']} {q['question']}</h3>
        <div class="hint">💡 {q['help']}</div>
    </div>
    """, unsafe_allow_html=True)

    # Quick choices if available
    if q.get("preset_choices"):
        st.caption("Quick options:")
        choices = q["preset_choices"]
        num_cols = min(len(choices), 2)
        for row_start in range(0, len(choices), num_cols):
            cols = st.columns(num_cols)
            for i, choice in enumerate(choices[row_start:row_start + num_cols]):
                with cols[i]:
                    if st.button(choice["label"], key=f"preset_{ctx}_{idx}_{row_start + i}", use_container_width=True):
                        submit_onboarding(parcel_id, session, str(choice["value"]))
                        return

    # For choice-only questions (like soil_type), show skip + back only
    if q["field"] == "soil_type":
        col_prev, col_skip = st.columns(2)
        with col_prev:
            if idx > 0:
                if st.button("← Previous", key=f"prev_{ctx}_{idx}", use_container_width=True):
                    session["current_index"] = idx - 1
                    st.session_state.onboarding_session = session
                    st.rerun()
        with col_skip:
            if st.button(f"⏭ {q['skip_label']}", key=f"skip_{ctx}_{idx}", use_container_width=True):
                submit_onboarding(parcel_id, session, "", skip=True)
                return
        return

    # Number input (for all other questions)
    # Pre-fill with existing answer if going back to review
    existing_value = str(answers.get(q["field"], ""))

    with st.form(f"q_{ctx}_{idx}_form"):
        col1, col2 = st.columns([3, 1])
        with col1:
            q_min = q.get('min', 0)
            q_max = q.get('max', 999)
            value = st.text_input(
                f"Your answer ({q['unit']})",
                value=existing_value,
                placeholder=f"Enter a number between {q_min} and {q_max}",
                key=f"answer_{ctx}_{idx}",
            )
        with col2:
            st.markdown("")
            st.markdown("")
            submit = st.form_submit_button("Next →", use_container_width=True, type="primary")

        # Navigation row: Previous + Skip
        col_prev, col_skip = st.columns(2)
        with col_prev:
            prev_btn = st.form_submit_button("← Previous", use_container_width=True) if idx > 0 else False
        with col_skip:
            skip = st.form_submit_button(f"⏭ {q['skip_label']}", use_container_width=True)

        if submit:
            if not value:
                st.warning("Please enter a value or click skip.")
            else:
                submit_onboarding(parcel_id, session, value)
                return

        if prev_btn and idx > 0:
            session["current_index"] = idx - 1
            st.session_state.onboarding_session = session
            st.rerun()
            return

        if skip:
            submit_onboarding(parcel_id, session, "", skip=True)
            return


def submit_onboarding(parcel_id: str, session: dict, answer: str, skip: bool = False):
    # Get the current question BEFORE submitting (so we know which field was answered)
    idx = session.get("current_index", 0)
    questions_data = api_get("/onboarding/questions")
    questions = questions_data.get("questions", []) if questions_data else []
    current_field = questions[idx]["field"] if idx < len(questions) else None

    result = api_post("/onboarding/answer", {
        "parcel_id": parcel_id,
        "answer": answer if not skip else None,
        "skip": skip,
        "session_state": session,
    })

    if result.get("error"):
        st.error(result["error"])
        return

    # Incrementally save this answer to the database right away,
    # so progress is preserved even if the user leaves mid-flow.
    if not skip and answer and current_field and current_field != "soil_type":
        try:
            api_post("/readings/manual", {
                "parcel_id": parcel_id,
                "field": current_field,
                "value": float(answer),
                "notes": "from onboarding",
            })
        except (ValueError, TypeError):
            pass  # Non-numeric answers (like soil_type) handled separately

    st.session_state.onboarding_session = result["session"]
    st.rerun()


# ── Soil Data Tab ───────────────────────────────────────────────────────────

def _compute_reading_status(field: str, value: float, soil_type: str = None) -> str:
    """Status check using the same soil-type-specific thresholds as the backend."""
    from shared.config import get_thresholds_for_soil_type
    t = get_thresholds_for_soil_type(soil_type or "loamy").get(field, {})
    if not t:
        return "healthy"
    low = t.get("low", True)
    if low:
        if value < t.get("crit", float("-inf")): return "critical"
        if value < t.get("warn", float("-inf")): return "warning"
    else:
        if value > t.get("crit", float("inf")): return "critical"
        if value > t.get("warn", float("inf")): return "warning"
    return "healthy"

def render_soil_data():
    parcel_id = st.session_state.current_parcel
    data = api_get("/ui/farmer/dashboard", {"parcel_id": parcel_id})

    if not data or data.get("_error"):
        st.error("Cannot load data")
        return

    if not data.get("configured"):
        render_setup_choice(parcel_id, ctx="soildata")
        return

    st.markdown("### 📊 Your Soil Measurements")
    st.caption("All values are exactly what you've provided — nothing assumed. Click ✏️ to edit any value.")

    if not data.get("has_data"):
        st.info("No measurements yet.")
        if data.get("mode") == "manual":
            render_onboarding_flow(parcel_id, ctx="soildata")
        return

    readings = data.get("readings", {})

    # Track which field is being edited
    editing_field = st.session_state.get("editing_field")

    # Show fields with data
    if readings:
        st.markdown("#### Current Readings")
        for field, info in readings.items():
            label = field.replace("_", " ").title()
            value = info["value"]
            age = info["age_minutes"]
            source = info.get("source", "manual")

            age_str = f"{int(age)} min ago" if age < 60 else f"{int(age/60)} hr ago" if age < 1440 else f"{int(age/1440)} days ago"

            # Determine status color
            status = _compute_reading_status(field, value, data.get("soil_type"))
            cls = {"healthy": "ind-good", "warning": "ind-moderate", "critical": "ind-poor"}.get(status, "ind-good")

            if editing_field == field:
                # Show edit form inline
                with st.form(f"edit_{field}_form"):
                    st.markdown(f"**✏️ Editing: {label}**")
                    new_val = st.number_input(
                        f"New value", value=float(value), step=0.1, format="%.2f",
                        key=f"edit_val_{field}",
                    )
                    col_save, col_cancel = st.columns(2)
                    with col_save:
                        save = st.form_submit_button("💾 Save", use_container_width=True, type="primary")
                    with col_cancel:
                        cancel = st.form_submit_button("Cancel", use_container_width=True)

                    if save:
                        result = api_post("/readings/manual", {
                            "parcel_id": parcel_id, "field": field,
                            "value": float(new_val), "notes": "edited",
                        })
                        if result.get("status") == "saved" or result.get("reading_id"):
                            st.session_state.editing_field = None
                            st.rerun()
                        else:
                            st.error(result.get("error", "Failed to save"))
                    if cancel:
                        st.session_state.editing_field = None
                        st.rerun()
            else:
                # Show reading with edit button
                col_display, col_edit = st.columns([6, 1])
                with col_display:
                    st.markdown(f"""
                    <div class="indicator-box {cls}">
                        <div>
                            <strong>{label}</strong><br>
                            <span class="sub">Source: {source} • Updated {age_str}</span>
                        </div>
                        <div style="font-size:24px; font-weight:700;">{value:.2f}</div>
                    </div>
                    """, unsafe_allow_html=True)
                with col_edit:
                    st.markdown("")  # vertical spacer
                    if st.button("✏️", key=f"edit_btn_{field}", help=f"Edit {label}"):
                        st.session_state.editing_field = field
                        st.rerun()

    # Add/update measurement
    st.divider()
    st.markdown("#### ➕ Add a Measurement")
    with st.form("add_reading_form"):
        all_fields = [
            ("soil_ph", "Soil pH"),
            ("soil_moisture_pct", "Soil Moisture (%)"),
            ("nitrogen_ppm", "Nitrogen (ppm)"),
            ("phosphorus_ppm", "Phosphorus (mg/kg)"),
            ("potassium_ppm", "Potassium (mg/kg)"),
            ("organic_matter_pct", "Organic Matter (%)"),
            ("ec_ds_m", "EC (dS/m)"),
            ("soil_temp_c", "Soil Temperature (°C)"),
            ("bulk_density_g_cm3", "Bulk Density (g/cm³)"),
            ("microbial_biomass_mg_c_kg", "Microbial Biomass (mg C/kg)"),
            ("soil_respiration_mg_co2_kg_day", "Respiration (mg CO₂/kg/day)"),
        ]
        field_choice = st.selectbox("Measurement", [f"{lbl} ({k})" for k, lbl in all_fields])
        value = st.number_input("Value", step=0.1, format="%.2f")
        notes = st.text_input("Notes (optional)")

        if st.form_submit_button("Save measurement", use_container_width=True):
            field_key = field_choice.split("(")[-1].rstrip(")")
            result = api_post("/readings/manual", {
                "parcel_id": parcel_id, "field": field_key,
                "value": float(value), "notes": notes,
            })
            if result.get("status") == "saved":
                st.success("✅ Saved!")
                st.rerun()
            else:
                st.error(result.get("error", "Failed to save"))


# ── Physical Status Cards ────────────────────────────────────────────────────

def _render_physical_cards(phys: dict, parcel_id: str):
    """
    Render farmer-friendly physical status cards for moisture, compaction,
    and erosion. Shows action buttons when S4 or S5 depletion states are active.
    """
    phys_states = phys.get("physical_depletion_states", [])
    phys_data   = phys.get("physical", {})
    if not phys_data:
        return

    vwc  = phys_data.get("VWC_percent")
    bd   = phys_data.get("bulk_density_g_cm3")
    temp = phys_data.get("soil_temperature_c")
    avail_water = phys_data.get("available_water_mm")
    s4_active = "S4" in phys_states
    s5_active = "S5" in phys_states

    if not (vwc or bd or temp):
        return

    st.divider()
    st.markdown("### 🌱 Physical Soil Status")

    col_m, col_c, col_e = st.columns(3)

    # Moisture card
    with col_m:
        s5_info = phys.get("s5_water_stress") or {}
        urgency = s5_info.get("urgency", "normal")
        if s5_active:
            triggers = s5_info.get("triggers", [])
            stress_type = triggers[0].get("stress_type", "dry") if triggers else "dry"
            if stress_type == "dry":
                card_cls = "alert-critical" if urgency == "high" else "alert-warning"
                icon = "🔴" if urgency == "high" else "🟡"
                status_txt = "Too Dry"
            else:
                card_cls = "alert-warning"
                icon = "🌊"
                status_txt = "Waterlogged"
        else:
            card_cls = ""
            icon = "💧"
            status_txt = "Good"

        vwc_str = f"{vwc:.1f}%" if vwc is not None else "—"
        avail_str = f"{avail_water:.0f} mm available" if avail_water is not None else ""
        st.markdown(
            f'<div class="alert-card {card_cls}" style="min-height:90px">'
            f'<strong>{icon} Moisture: {status_txt}</strong><br>'
            f'{vwc_str}{(" · " + avail_str) if avail_str else ""}'
            f'</div>',
            unsafe_allow_html=True,
        )
        if urgency == "high":
            st.caption("High plant water demand — urgent")

    # Compaction card
    with col_c:
        if s4_active:
            bd_str = f"{bd:.2f} g/cm³" if bd is not None else "—"
            st.markdown(
                f'<div class="alert-card alert-warning" style="min-height:90px">'
                f'<strong>⚖️ Compaction: High</strong><br>'
                f'{bd_str} — deep rip needed</div>',
                unsafe_allow_html=True,
            )
        else:
            bd_str = f"{bd:.2f} g/cm³" if bd is not None else "—"
            st.markdown(
                f'<div class="alert-card" style="min-height:90px">'
                f'<strong>✅ Compaction: Normal</strong><br>'
                f'{bd_str}</div>',
                unsafe_allow_html=True,
            )

    # Temperature card
    with col_e:
        temp_str = f"{temp:.1f} °C" if temp is not None else "—"
        too_hot  = temp is not None and temp > 35
        too_cold = temp is not None and temp < 10
        if too_hot or too_cold:
            st.markdown(
                f'<div class="alert-card alert-warning" style="min-height:90px">'
                f'<strong>🌡️ Temperature: Stress</strong><br>'
                f'{temp_str} — {"too hot" if too_hot else "too cold"} for roots</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f'<div class="alert-card" style="min-height:90px">'
                f'<strong>✅ Temperature: Good</strong><br>'
                f'{temp_str}</div>',
                unsafe_allow_html=True,
            )

    # ── Action buttons when physical depletion is active ────────────────────
    if s4_active or s5_active:
        st.markdown("**What would you like to do?**")
        col_i, col_r, col_a = st.columns(3)

        with col_i:
            if st.button("💧 I will irrigate", use_container_width=True,
                          key="phys_action_irrigate", disabled=not s5_active):
                api_post("/ui/farmer/observation", {
                    "text": "Farmer committed to irrigation in response to S5 water stress.",
                    "structured_fields": {"action": "irrigate", "depletion_state": "S5"},
                })
                st.success("Logged. Irrigate soon — check back tomorrow.")

        with col_r:
            if st.button("⛏️ I will deep rip", use_container_width=True,
                          key="phys_action_deeprip", disabled=not s4_active):
                api_post("/ui/farmer/observation", {
                    "text": "Farmer committed to deep ripping in response to S4 compaction.",
                    "structured_fields": {"action": "deep_rip", "depletion_state": "S4"},
                })
                st.success("Logged. Rip to 30–50 cm when soil is dry enough.")

        with col_a:
            if st.button("💬 I need advice", use_container_width=True,
                          key="phys_action_advice"):
                codes = " and ".join(sorted(phys_states))
                question = (
                    f"My soil has active depletion: {codes}. "
                    f"Moisture is {vwc:.1f}%, bulk density is {bd:.2f} g/cm³. "
                    f"What should I do first?"
                )
                st.session_state.chat_history.append({"role": "user", "content": question})
                result = api_post("/ui/farmer/chat", {
                    "question": question,
                    "parcel_id": parcel_id,
                    "history": [],
                })
                answer = (result or {}).get("answer", "Unable to get advice right now.")
                st.session_state.chat_history.append({"role": "assistant", "content": answer})
                st.info(f"**Advisor:** {answer}")


# ── Chat Tab ────────────────────────────────────────────────────────────────

def render_chat():
    st.markdown("### 💬 Ask the Soil Advisor")
    st.caption("Ask anything about your soil. The advisor will only use YOUR actual data — never guesses.")

    # Display chat history
    for msg in st.session_state.chat_history:
        role = msg["role"]
        if role == "user":
            st.chat_message("user").write(msg["content"])
        else:
            st.chat_message("assistant", avatar="🌱").write(msg["content"])

    # Process pending question (from quick buttons)
    if st.session_state.get("pending_question"):
        question = st.session_state.pending_question
        st.session_state.pending_question = None
        process_chat_message(question)
        st.rerun()

    # Chat input
    user_input = st.chat_input("Ask about your soil...")
    if user_input:
        process_chat_message(user_input)
        st.rerun()

    # Quick question buttons — general + physical
    st.divider()
    st.caption("Quick questions:")
    quick_qs = [
        "What is my soil health score?",
        "What fertiliser should I apply?",
        "Is my soil too acidic?",
        "What should I do first to fix my soil?",
    ]
    cols = st.columns(2)
    for i, q in enumerate(quick_qs):
        with cols[i % 2]:
            if st.button(q, key=f"quick_{i}", use_container_width=True):
                st.session_state.pending_question = q
                st.rerun()

    # Physical quick questions
    st.caption("Physical soil questions:")
    phys_qs = [
        "Is my soil moisture level safe for my crops?",
        "My soil feels hard — what should I do about compaction?",
        "How much should I irrigate today?",
        "When is the best time to deep rip compacted soil?",
    ]
    cols2 = st.columns(2)
    for i, q in enumerate(phys_qs):
        with cols2[i % 2]:
            if st.button(q, key=f"phys_quick_{i}", use_container_width=True):
                st.session_state.pending_question = q
                st.rerun()

    # Observation input
    st.divider()
    st.markdown("### 📝 Record an Observation")
    with st.form("observation_form"):
        obs_text = st.text_area(
            "Your observation",
            placeholder="e.g., 'Applied urea 2 weeks ago', 'Saw water pooling'",
        )
        if st.form_submit_button("Submit", use_container_width=True):
            if obs_text.strip():
                result = api_post("/ui/farmer/observation", {"text": obs_text.strip()})
                if result.get("status") == "ok":
                    st.success("Recorded!")
                else:
                    st.error("Failed to record")


def process_chat_message(question: str):
    """Send a chat message — used by both input box and quick buttons."""
    st.session_state.chat_history.append({"role": "user", "content": question})

    parcel_id = st.session_state.current_parcel

    with st.spinner("Thinking..."):
        result = api_post("/ui/farmer/chat", {
            "question": question,
            "parcel_id": parcel_id,
        })

    answer = result.get("answer", "Sorry, I couldn't process that.")
    st.session_state.chat_history.append({"role": "assistant", "content": answer})


# ── History Tab ─────────────────────────────────────────────────────────────

def render_history():
    parcel_id = st.session_state.current_parcel
    st.markdown("### 📋 Recommendation History")

    data = api_get("/ui/farmer/dashboard", {"parcel_id": parcel_id})
    if not data or data.get("_error"):
        st.error("Cannot load history")
        return

    if not data.get("configured"):
        st.info("Complete your setup to see recommendation history.")
        return

    recs = data.get("recent_recommendations", [])
    if not recs:
        st.info("No recommendations yet. As you add data, you'll get tailored advice.")
        return

    for rec in recs:
        product = rec.get("product", "Recommendation")
        state = rec.get("state", "?")
        rate = rec.get("rate_kg_ha", "—")

        with st.expander(f"[{state}] {product} — {rate} kg/ha"):
            st.write(f"**When:** {rec.get('timing', '—')}")
            st.write(f"**How:** {rec.get('method', '—')}")
            st.write(f"**Why:** {rec.get('rationale', '—')}")

            col1, col2 = st.columns(2)
            with col1:
                if st.button("👍 Worked", key=f"up_{hash(str(rec))}"):
                    api_post("/ui/farmer/feedback", {
                        "recommendation_id": rec.get("recommendation_id", "unknown"),
                        "accepted": True, "outcome": "worked", "rating": 5,
                    })
                    st.success("Thanks!")
            with col2:
                if st.button("👎 Didn't help", key=f"down_{hash(str(rec))}"):
                    api_post("/ui/farmer/feedback", {
                        "recommendation_id": rec.get("recommendation_id", "unknown"),
                        "accepted": True, "outcome": "no_improvement", "rating": 1,
                    })
                    st.warning("Recorded.")


# ── Profile Tab (NEW — fix #1) ──────────────────────────────────────────────

def render_profile():
    st.markdown("### 👤 Your Profile")

    profile = api_get("/auth/profile")
    if not profile or profile.get("_error"):
        st.error("Cannot load profile")
        return

    # Display info
    st.markdown(f"""
    <div class="profile-box">
        <h4>Account Details</h4>
        <p><strong>Member since:</strong> {profile.get('created_at', 'Unknown')}</p>
        <p><strong>Last login:</strong> {profile.get('last_login', 'Unknown')}</p>
        <p><strong>Account type:</strong> {profile.get('role', '').title()}</p>
    </div>
    """, unsafe_allow_html=True)

    # Edit form
    st.markdown("#### ✏️ Edit Profile")
    with st.form("profile_edit"):
        new_name = st.text_input("Full name", value=profile.get("name", ""))
        new_email = st.text_input("Email", value=profile.get("email", ""))
        new_phone = st.text_input("Phone (optional)", value=profile.get("phone", ""))
        new_location = st.text_input("Location (optional)", value=profile.get("location", ""))
        new_farm = st.text_input("Farm name (optional)", value=profile.get("farm_name", ""))

        if st.form_submit_button("💾 Save changes", use_container_width=True, type="primary"):
            updates = {
                "name": new_name, "email": new_email,
                "phone": new_phone, "location": new_location,
                "farm_name": new_farm,
            }
            result = api_put("/auth/profile", updates)
            if result.get("error"):
                st.error(result["error"])
            else:
                st.success("✅ Profile updated!")
                st.rerun()

    # Change password
    st.divider()
    st.markdown("#### 🔒 Change Password")
    with st.form("password_change"):
        old_pw = st.text_input("Current password", type="password")
        new_pw = st.text_input("New password (min 6 chars)", type="password")
        confirm_pw = st.text_input("Confirm new password", type="password")

        if st.form_submit_button("Update password", use_container_width=True):
            if new_pw != confirm_pw:
                st.error("Passwords don't match")
            elif len(new_pw) < 6:
                st.error("Password must be at least 6 characters")
            else:
                result = api_post("/auth/change-password", {
                    "old_password": old_pw, "new_password": new_pw,
                })
                if result.get("error"):
                    st.error(result["error"])
                else:
                    st.success("✅ Password changed")

    # Manage parcels
    st.divider()
    st.markdown("#### 🌾 Your Parcels")
    parcels = api_get("/parcels")
    if parcels and parcels.get("parcels"):
        for p in parcels["parcels"]:
            mode_emoji = {"sensor": "📡", "manual": "📝", "none": "❓"}.get(p["data_source_mode"], "❓")
            st.markdown(f"- {mode_emoji} **{p['parcel_name']}** (mode: {p['data_source_mode']})")

    with st.expander("Add another parcel"):
        with st.form("add_parcel"):
            new_pid = st.text_input("Parcel ID (unique identifier)",
                                     placeholder="e.g., field_north_2")
            new_pname = st.text_input("Parcel name (display)",
                                       placeholder="e.g., North Field")
            if st.form_submit_button("Add parcel"):
                result = api_post("/parcels", {
                    "parcel_id": new_pid, "parcel_name": new_pname,
                })
                if result.get("error"):
                    st.error(result["error"])
                else:
                    st.success("✅ Parcel added")
                    st.rerun()


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    init_session()
    if st.session_state.token is None:
        login_page()
    else:
        dashboard()


if __name__ == "__main__":
    main()