# ui/app_scientist.py
"""
Soil Scientist Dashboard — Professional Research Environment.
Streamlit app with full observability and control over the ASDT.

Run:  streamlit run ui/app_scientist.py --server.port 8501
"""
import os
import json
import time
import requests
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime
from soil_3d_viz import render_3d_simulation

# ── Configuration ────────────────────────────────────────────────────────────
API_BASE = os.getenv("ASDT_API_URL", "http://localhost:8080")
API_V1 = f"{API_BASE}/api/v1"

# ── Page Config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ASDT — Soil Scientist",
    page_icon="🔬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ───────────────────────────────────────────────────────────────
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=DM+Sans:wght@400;500;700&display=swap');

    .stApp {
        font-family: 'DM Sans', sans-serif;
    }
    code, .stCode, pre {
        font-family: 'JetBrains Mono', monospace !important;
    }
    .main .block-container {
        padding-top: 1.5rem;
        max-width: 100%;
    }

    /* Status badges */
    .badge-healthy { background: #059669; color: white; padding: 2px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; }
    .badge-warning { background: #d97706; color: white; padding: 2px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; }
    .badge-critical { background: #dc2626; color: white; padding: 2px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; }
    .badge-nodata { background: #6b7280; color: white; padding: 2px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; }

    /* Header */
    .sci-header {
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
        padding: 20px 30px;
        border-radius: 12px;
        margin-bottom: 20px;
        border: 1px solid #1e3a5f;
    }
    .sci-header h1 { color: #4ecdc4; margin: 0; font-size: 26px; }
    .sci-header p { color: #8eafc0; margin: 4px 0 0 0; font-size: 14px; }

    /* Metric cards */
    .metric-card {
        background: #111827;
        border: 1px solid #1f2937;
        border-radius: 10px;
        padding: 16px;
        margin: 4px 0;
    }
    .metric-card h4 { color: #9ca3af; font-size: 12px; margin: 0 0 6px 0; text-transform: uppercase; letter-spacing: 0.5px; }
    .metric-card .value { color: #f9fafb; font-size: 28px; font-weight: 700; font-family: 'JetBrains Mono', monospace; }
    .metric-card .unit { color: #6b7280; font-size: 14px; }

    /* Section headers */
    .section-physical { border-left: 4px solid #3b82f6; padding-left: 12px; }
    .section-chemical { border-left: 4px solid #f59e0b; padding-left: 12px; }
    .section-biological { border-left: 4px solid #10b981; padding-left: 12px; }

    div[data-testid="stMetric"] {
        background: #111827;
        border: 1px solid #1f2937;
        padding: 12px;
        border-radius: 8px;
    }

    .stTabs [data-baseweb="tab-list"] { gap: 4px; }
    .stTabs [data-baseweb="tab"] {
        background: #1f2937;
        border-radius: 6px;
        color: #9ca3af;
        padding: 8px 16px;
    }
    .stTabs [aria-selected="true"] {
        background: #0f3460 !important;
        color: #4ecdc4 !important;
    }
</style>
""", unsafe_allow_html=True)


# ── Session State ────────────────────────────────────────────────────────────

def init_session():
    if "token" not in st.session_state:
        st.session_state.token = None
    if "user" not in st.session_state:
        st.session_state.user = None


def api_headers():
    return {"Authorization": f"Bearer {st.session_state.token}"} if st.session_state.token else {}


def api_get(path, params=None):
    try:
        r = requests.get(f"{API_V1}{path}", headers=api_headers(), params=params, timeout=10)
        if r.status_code == 401:
            st.session_state.token = None
            st.rerun()
        return r.json() if r.status_code == 200 else None
    except requests.ConnectionError:
        st.error("Cannot connect to ASDT API. Make sure the backend is running.")
        return None
    except requests.Timeout:
        st.error(f"Request timed out: {path}")
        return None


def api_post(path, data=None):
    try:
        r = requests.post(f"{API_V1}{path}", headers=api_headers(), json=data, timeout=15)
        return r.json() if r.status_code in (200, 201) else {"error": r.text}
    except requests.ConnectionError:
        return {"error": "Cannot connect to API"}
    except requests.Timeout:
        return {"error": "Request timed out"}


def api_put(path, data=None):
    try:
        r = requests.put(f"{API_V1}{path}", headers=api_headers(), json=data, timeout=15)
        return r.json() if r.status_code == 200 else {"error": r.text}
    except requests.ConnectionError:
        return {"error": "Cannot connect to API"}
    except requests.Timeout:
        return {"error": "Request timed out"}


def api_patch(path, data=None):
    try:
        r = requests.patch(f"{API_V1}{path}", headers=api_headers(), json=data, timeout=15)
        return r.json() if r.status_code == 200 else {"error": r.text}
    except requests.ConnectionError:
        return {"error": "Cannot connect to API"}
    except requests.Timeout:
        return {"error": "Request timed out"}


# ── Login Page ───────────────────────────────────────────────────────────────

def login_page():
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown("""
        <div class="sci-header" style="text-align:center;">
            <h1>🔬 ASDT — Soil Scientist Portal</h1>
            <p>Agentic Soil Digital Twin — Research Environment</p>
        </div>
        """, unsafe_allow_html=True)

        tab1, tab2 = st.tabs(["Sign In", "Create Account"])

        with tab1:
            with st.form("login_form"):
                email = st.text_input("Email")
                password = st.text_input("Password", type="password")
                submitted = st.form_submit_button("Sign In", use_container_width=True)

                if submitted:
                    result = api_post("/auth/login", {"email": email, "password": password})
                    if "token" in result:
                        st.session_state.token = result["token"]
                        st.session_state.user = result
                        st.rerun()
                    else:
                        st.error(result.get("error", "Login failed"))

        with tab2:
            with st.form("register_form"):
                r_name = st.text_input("Full name")
                r_email = st.text_input("Email address")
                r_password = st.text_input("Password (min 6 characters)", type="password")
                r_lab = st.text_input("Research site / parcel name (optional)",
                                      placeholder="e.g., Kumasi Field Station Plot A")
                if st.form_submit_button("Create Account", use_container_width=True):
                    if not r_name or not r_email or len(r_password) < 6:
                        st.error("Please fill all required fields. Password needs 6+ characters.")
                    else:
                        result = api_post("/auth/register", {
                            "email": r_email, "password": r_password,
                            "name": r_name, "role": "scientist",
                            "parcels": [r_lab or f"site_{r_name.lower().replace(' ', '_')}"],
                        })
                        if result and "user_id" in result:
                            # Sign the user straight in. Making them retype the
                            # credentials they just chose is friction with no
                            # purpose, and the previous message left them on a
                            # tab that looked like a dead end.
                            login = api_post("/auth/login",
                                             {"email": r_email, "password": r_password})
                            if login and "access_token" in login:
                                st.session_state.token = login["access_token"]
                                st.session_state.user = login.get("user", {})
                                st.rerun()
                            else:
                                st.success("Account created. Switch to the "
                                           "**Sign In** tab to log in.")
                        else:
                            st.error(result.get("error", "Registration failed"))


# ── Status Badge Helper ─────────────────────────────────────────────────────

def status_badge(status):
    cls = {
        "healthy": "badge-healthy", "ok": "badge-healthy",
        "warning": "badge-warning",
        "critical": "badge-critical",
        "no_data": "badge-nodata",
    }.get(status, "badge-nodata")
    return f'<span class="{cls}">{status.upper()}</span>'


# ── Gauge Chart ─────────────────────────────────────────────────────────────

def health_gauge(score):
    if score is None:
        score = 0
    try:
        score = float(score)
    except (ValueError, TypeError):
        score = 0

    color = "#059669" if score >= 75 else "#d97706" if score >= 50 else "#dc2626"

    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=score,
        domain={"x": [0, 1], "y": [0, 1]},
        gauge={
            "axis": {"range": [0, 100], "tickwidth": 1, "tickcolor": "#374151"},
            "bar": {"color": color},
            "bgcolor": "#1f2937",
            "borderwidth": 0,
            "steps": [
                {"range": [0, 40], "color": "#450a0a"},
                {"range": [40, 70], "color": "#451a03"},
                {"range": [70, 100], "color": "#052e16"},
            ],
        },
        number={"suffix": "/100", "font": {"size": 36, "color": "#f9fafb", "family": "JetBrains Mono"}},
    ))
    fig.update_layout(
        height=200, margin=dict(l=20, r=20, t=20, b=20),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font={"color": "#9ca3af"},
    )
    return fig


# ── Time Series Chart ──────────────────────────────────────────────────────

def time_series_chart(field, minutes=60):
    data = api_get("/ui/scientist/sensor_history", {"field": field, "minutes": minutes})
    if not data or not data.get("data"):
        st.caption(f"No data for {field}")
        return

    df = pd.DataFrame(data["data"])
    df["time"] = pd.to_datetime(df["time"])

    fig = px.line(df, x="time", y="value", title=field.replace("_", " ").title())
    fig.update_layout(
        height=250,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(17,24,39,0.8)",
        font={"color": "#9ca3af", "family": "DM Sans"},
        xaxis={"gridcolor": "#1f2937"}, yaxis={"gridcolor": "#1f2937"},
        margin=dict(l=40, r=20, t=40, b=30),
    )
    fig.update_traces(line_color="#4ecdc4")
    st.plotly_chart(fig, use_container_width=True)


# ── Radar Chart for Nutrients ───────────────────────────────────────────────

def nutrient_radar(sensors):
    fields = ["nitrogen_ppm", "phosphorus_ppm", "potassium_ppm", "soil_ph", "organic_matter_pct", "ec_ds_m"]
    labels = ["N (ppm)", "P (mg/kg)", "K (mg/kg)", "pH", "OM (%)", "EC (dS/m)"]

    values = []
    for f in fields:
        s = sensors.get(f, {})
        val = s.get("value")
        hmin = s.get("healthy_min", 1)
        hmax = s.get("healthy_max", 100)
        if val is not None and hmax and hmin:
            mid = (hmin + hmax) / 2
            norm = min(val / mid * 100, 150) if mid > 0 else 50
        else:
            norm = 0
        values.append(round(norm, 1))

    values.append(values[0])  # close the polygon
    labels.append(labels[0])

    fig = go.Figure(go.Scatterpolar(r=values, theta=labels, fill="toself",
                                      fillcolor="rgba(78,205,196,0.2)",
                                      line={"color": "#4ecdc4"}))
    fig.update_layout(
        polar={"radialaxis": {"visible": True, "range": [0, 150], "gridcolor": "#1f2937"},
               "bgcolor": "rgba(17,24,39,0.8)", "angularaxis": {"gridcolor": "#1f2937"}},
        showlegend=False, height=350,
        paper_bgcolor="rgba(0,0,0,0)", font={"color": "#9ca3af"},
        margin=dict(l=60, r=60, t=30, b=30),
    )
    st.plotly_chart(fig, use_container_width=True)


# ── Main Dashboard ──────────────────────────────────────────────────────────

def dashboard():
    user = st.session_state.user

    # Sidebar
    with st.sidebar:
        st.markdown(f"### 🔬 {user['name']}")
        st.caption(f"{user['email']} — Scientist")
        st.divider()

        view = st.radio("Navigation", [
            "Dashboard",
            "3D Simulation",
            "Physical Segment",
            "Chemical Segment",
            "Biological Segment",
            "Soil Parameters",
            "Layer Observability",
            "Cross-Domain",
            "Knowledge Graph",
            "Audit Log",
            "Profile",
        ], label_visibility="collapsed")

        st.divider()
        if st.button("Refresh Data", use_container_width=True):
            st.rerun()
        if st.button("Run Diagnosis Now", use_container_width=True):
            with st.spinner("Running 6-step diagnostic pipeline..."):
                result = api_post("/ui/scientist/run_diagnosis")
                if result and "error" not in result:
                    st.success(f"Diagnosis complete: {result.get('primary_state', {}).get('code', 'N/A')}")
                else:
                    st.error(str(result.get("error", "Failed")))
        st.divider()
        if st.button("Sign Out", use_container_width=True):
            st.session_state.token = None
            st.session_state.user = None
            st.rerun()

    # Main content
    if view == "Dashboard":
        render_integrated_view()
    elif view == "3D Simulation":
        render_3d_simulation(api_get, api_patch, api_post)
    elif view == "Physical Segment":
        render_physical()
    elif view == "Chemical Segment":
        render_chemical()
    elif view == "Biological Segment":
        render_biological()
    elif view == "Soil Parameters":
        render_soil_parameters()
    elif view == "Layer Observability":
        render_layers()
    elif view == "Cross-Domain":
        render_cross_domain()
    elif view == "Knowledge Graph":
        render_knowledge_graph()
    elif view == "Audit Log":
        render_audit_log()
    elif view == "Profile":
        render_scientist_profile()


# ── Integrated View ─────────────────────────────────────────────────────────

def render_integrated_view():
    st.markdown("""
    <div class="sci-header">
        <h1>🔬 Integrated Soil Dashboard</h1>
        <p>Complete observability across Physical, Chemical, and Biological domains</p>
    </div>
    """, unsafe_allow_html=True)

    summary = api_get("/ui/scientist/summary")
    if not summary:
        st.error("Failed to load dashboard data")
        return

    # ── No-data welcome state for new users ───────────────────────────────────
    if summary.get("no_data"):
        st.info(
            "**Welcome!** Your parcel has no soil readings yet.  \n"
            "Enter your first measurements below — the full dashboard will unlock once "
            "data is saved."
        )
        from soil_3d_viz import _render_readings_entry_form
        parcel_id = summary.get("parcel_id")
        if parcel_id:
            _render_readings_entry_form(
                api_post, parcel_id, summary,
                summary.get("sensors", {}), expanded=True,
            )
        return

    # Top metrics row
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        hs = summary.get("health_score")
        if hs is not None:
            st.plotly_chart(health_gauge(hs), use_container_width=True)
        else:
            st.metric("Soil Health Score", "—")
            st.caption("No data yet — run the simulator")
        st.caption("Soil Health Score")
    with c2:
        depletion = summary.get("depletion_states", {})
        st.metric("Active Depletion States", len(depletion))
        if depletion:
            for code in depletion:
                st.markdown(f"**{code}**: {depletion[code].get('name', '')}")
    with c3:
        diag = summary.get("active_diagnosis", {})
        primary = diag.get("primary_state", "—")
        confidence = diag.get("confidence", 0)
        st.metric("Primary State", primary)
        st.progress(confidence, text=f"Confidence: {confidence:.0%}")
    with c4:
        st.metric("System State", summary.get("system_state", "unknown").upper())
        st.metric("Soil Type", summary.get("soil_type", "—").title())

    st.divider()

    # Sensor overview table
    st.subheader("All Sensor Parameters")
    sensors = summary.get("sensors", {})
    def _fmt(v, precision=2):
        return f"{v:.{precision}f}" if v is not None else "—"

    rows = []
    for field, info in sensors.items():
        rows.append({
            "Parameter": field.replace("_", " ").title(),
            "Value": _fmt(info.get("value")),
            "Unit": info.get("unit") or "",
            "Status": info.get("status", "no_data"),
            "Healthy Min": _fmt(info.get("healthy_min")),
            "Healthy Max": _fmt(info.get("healthy_max")),
            "Warn": _fmt(info.get("threshold_warn")),
            "Critical": _fmt(info.get("threshold_crit")),
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True, hide_index=True)

    # Recommendations
    st.divider()
    st.subheader("Recent Recommendations")
    recs_data = api_get("/ui/scientist/recommendations")
    if recs_data and recs_data.get("recommendations"):
        for rec in recs_data["recommendations"][:5]:
            with st.expander(f"[{rec.get('state', '?')}] {rec.get('product', 'Unknown')} — {rec.get('rate_kg_ha', 0)} kg/ha"):
                st.write(f"**Timing:** {rec.get('timing', '—')}")
                st.write(f"**Method:** {rec.get('method', '—')}")
                st.write(f"**Rationale:** {rec.get('rationale', '—')}")
                st.write(f"**Confidence:** {rec.get('confidence', 0):.0%}")

    # Recent events
    st.divider()
    st.subheader("Recent Events")
    events = summary.get("recent_events", [])
    if events:
        for ev in events[:10]:
            sev = ev.get("severity", "info")
            icon = "🔴" if sev == "critical" else "🟡" if sev == "warning" else "🔵"
            ts = ev.get("timestamp", "")
            if isinstance(ts, dict):
                ts = ts.get("$date", "")
            st.text(f"{icon} [{sev.upper()}] {ev.get('event_type', '?')} — {ts}")


# ── Physical Segment ────────────────────────────────────────────────────────

def render_physical():
    import pandas as pd
    st.markdown('<h2 class="section-physical">Physical Segment</h2>', unsafe_allow_html=True)
    st.caption("Soil Moisture · Bulk Density · Temperature — S4/S5 depletion states")

    summary  = api_get("/ui/scientist/summary")
    phys_api = api_get("/physical/status")
    if not summary:
        return

    sensors = summary.get("sensors", {})
    physical_fields = ["soil_moisture_pct", "bulk_density_g_cm3", "soil_temp_c"]
    field_labels = {
        "soil_moisture_pct":  ("Soil Moisture",   "%"),
        "bulk_density_g_cm3": ("Bulk Density",    "g/cm³"),
        "soil_temp_c":        ("Soil Temperature","°C"),
    }

    # ── Live gauges ──────────────────────────────────────────────────────────
    st.subheader("Current Readings")
    cols = st.columns(3)
    for i, field in enumerate(physical_fields):
        with cols[i]:
            info  = sensors.get(field, {})
            val   = info.get("value")
            unit  = field_labels[field][1]
            label = field_labels[field][0]
            status = info.get("status", "no_data")
            age    = info.get("age_minutes")
            age_str = f"{age:.0f} min ago" if age is not None else ""
            st.metric(
                label,
                f"{val:.2f} {unit}" if val is not None else "—",
                delta=age_str if age_str else None,
                delta_color="off",
            )
            st.markdown(status_badge(status), unsafe_allow_html=True)
            warn = info.get("threshold_warn")
            crit = info.get("threshold_crit")
            if warn is not None and crit is not None:
                st.caption(f"warn < {warn} · crit < {crit} {unit}")

    # ── S4 / S5 depletion state alerts ──────────────────────────────────────
    if phys_api:
        phys_states = phys_api.get("physical_depletion_states", [])
        if phys_states:
            st.divider()
            label_map = {"S4": "Compacted", "S5": "Water-Stressed"}
            for code in phys_states:
                detail = phys_api.get(f"s{code[1].lower()}_compaction" if code == "S4"
                                      else "s5_water_stress") or {}
                st.warning(f"**{code} — {label_map.get(code, code)}** active")
                if detail.get("triggers"):
                    for t in detail["triggers"]:
                        st.write(f"  → {t.get('field', '')}: {t.get('value', ''):.2f} "
                                 f"(threshold {t.get('threshold', ''):.2f})")
                if detail.get("urgency") == "high":
                    st.error("Cross-domain: plant water demand is HIGH — irrigation urgency elevated")

            # Run physical fast path
            if st.button("⚡ Run Physical Fast Diagnose", key="phys_fast_diagnose"):
                result = api_post("/physical/fast_diagnose", {})
                if result:
                    if result.get("fast_path_taken"):
                        r = result["result"]
                        st.success(f"**{r.get('action', 'Recommendation')}**")
                        st.write(r.get("recommendation", ""))
                        st.write(f"Confidence: {r.get('confidence', 0):.0%} | Severity: {r.get('severity', '—')}")
                    else:
                        st.info("Non-physical states detected — full pipeline needed.")
        else:
            st.success("No physical depletion states active.")

    # ── Time-series selector ─────────────────────────────────────────────────
    st.divider()
    st.subheader("Time Series")
    time_opt = st.radio(
        "Range",
        ["1 hour", "6 hours", "24 hours", "7 days"],
        horizontal=True,
        key="phys_time_range",
    )
    minutes_map = {"1 hour": 60, "6 hours": 360, "24 hours": 1440, "7 days": 10080}
    minutes = minutes_map[time_opt]
    for field in physical_fields:
        time_series_chart(field, minutes)

    # ── Water balance forecast chart ─────────────────────────────────────────
    st.divider()
    st.subheader("Water Balance Forecast (FAO-56)")
    col_wb1, col_wb2 = st.columns([3, 1])
    with col_wb2:
        horizon = st.selectbox("Horizon", [3, 5, 7, 10, 14], index=2, key="wb_horizon")
    wb = api_get(f"/physical/water_balance?horizon_days={horizon}")
    if wb and wb.get("forecast"):
        fc = wb["forecast"]
        df_wb = pd.DataFrame(fc)
        if not df_wb.empty:
            import plotly.graph_objects as go
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=df_wb["day"], y=df_wb["vwc_pct"],
                mode="lines+markers", name="VWC%",
                line=dict(color="#3b82f6", width=2),
            ))
            if "field_capacity_pct" in df_wb.columns:
                fig.add_hline(
                    y=df_wb["field_capacity_pct"].iloc[0],
                    line_dash="dot", line_color="#22c55e",
                    annotation_text="FC",
                )
            if "wilting_point_pct" in df_wb.columns:
                fig.add_hline(
                    y=df_wb["wilting_point_pct"].iloc[0],
                    line_dash="dot", line_color="#ef4444",
                    annotation_text="WP",
                )
            # Highlight stressed days
            stress_days = [d for d in fc if d.get("stressed")]
            if stress_days:
                sx = [d["day"] for d in stress_days]
                sy = [d["vwc_pct"] for d in stress_days]
                fig.add_trace(go.Scatter(
                    x=sx, y=sy, mode="markers",
                    marker=dict(color="red", size=10, symbol="x"),
                    name="Stress",
                ))
            fig.update_layout(
                height=280,
                margin=dict(l=0, r=0, t=24, b=0),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(color="#e2e8f0"),
                xaxis=dict(title="Day", gridcolor="#334155"),
                yaxis=dict(title="VWC (%)", gridcolor="#334155"),
                legend=dict(orientation="h"),
            )
            st.plotly_chart(fig, use_container_width=True)

            # Summary table
            with st.expander("Daily forecast table"):
                disp = df_wb[["day", "vwc_pct", "et0_mm", "available_water_mm"]
                             if "available_water_mm" in df_wb.columns
                             else ["day", "vwc_pct", "et0_mm"]].copy()
                st.dataframe(disp, use_container_width=True)
    else:
        st.info("Water balance forecast unavailable — check that model_runner is running.")

    # ── Compaction risk meter ────────────────────────────────────────────────
    st.divider()
    st.subheader("Compaction Risk")
    col_cr1, col_cr2 = st.columns([3, 1])
    with col_cr2:
        passes = st.number_input("Machinery passes (last 30d)", 0, 20, 0, key="cr_passes")
    cr = api_get(f"/physical/compaction_risk?machinery_passes={passes}")
    if cr:
        risk_score = cr.get("risk_score", 0)
        risk_level = cr.get("risk_level", "unknown")
        risk_colors = {"low": "#22c55e", "moderate": "#f59e0b",
                       "high": "#ef4444", "critical": "#dc2626", "unknown": "#64748b"}
        col_a, col_b = st.columns(2)
        with col_a:
            color = risk_colors.get(risk_level, "#64748b")
            st.markdown(
                f"<div style='background:{color}22;border:2px solid {color};"
                f"border-radius:8px;padding:16px;text-align:center;'>"
                f"<div style='font-size:2.5rem;font-weight:700;color:{color}'>{risk_score:.0f}</div>"
                f"<div style='color:{color};text-transform:uppercase;font-size:0.75rem'>{risk_level} risk</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        with col_b:
            st.write(f"**BD:** {cr.get('bulk_density', '—')} g/cm³")
            st.write(f"**Moisture:** {cr.get('moisture_pct', '—'):.1f}%")
            st.write(f"**Action:** {cr.get('action', '—')}")
            if cr.get("note"):
                st.caption(cr["note"])
    else:
        st.info("Compaction risk data unavailable.")

    # ── Erosion risk ─────────────────────────────────────────────────────────
    st.divider()
    st.subheader("Erosion Risk (RUSLE)")
    with st.expander("Configure site parameters", expanded=False):
        col_e1, col_e2, col_e3, col_e4 = st.columns(4)
        with col_e1:
            slope = st.number_input("Slope (%)", 0.0, 100.0, 2.0, 0.5, key="er_slope")
        with col_e2:
            slope_len = st.number_input("Slope length (m)", 1.0, 500.0, 50.0, 5.0, key="er_len")
        with col_e3:
            cover = st.number_input("Cover factor C (0–1)", 0.0, 1.0, 0.3, 0.05, key="er_cover")
        with col_e4:
            support = st.number_input("Practice P (0–1)", 0.1, 1.0, 1.0, 0.1, key="er_support")

    er = api_get(
        f"/physical/erosion_risk?slope_pct={slope}&slope_length_m={slope_len}"
        f"&cover_factor={cover}&support_practice={support}"
    )
    if er:
        soil_loss  = er.get("soil_loss_t_ha_yr", 0)
        risk_class = er.get("risk_class", "unknown")
        dominant   = er.get("dominant_factor", "—")
        er_colors  = {
            "Very Low": "#22c55e", "Low": "#84cc16",
            "Moderate": "#f59e0b", "High": "#ef4444",
            "Very High": "#dc2626", "unknown": "#64748b",
        }
        col_ea, col_eb = st.columns(2)
        with col_ea:
            color = er_colors.get(risk_class, "#64748b")
            st.markdown(
                f"<div style='background:{color}22;border:2px solid {color};"
                f"border-radius:8px;padding:16px;text-align:center;'>"
                f"<div style='font-size:2rem;font-weight:700;color:{color}'>{soil_loss:.1f} t/ha/yr</div>"
                f"<div style='color:{color};text-transform:uppercase;font-size:0.75rem'>{risk_class}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        with col_eb:
            factors = er.get("factors", {})
            st.write(f"**Dominant factor:** {dominant}")
            st.write(f"R = {factors.get('R', '—')} · K = {factors.get('K', '—')}")
            st.write(f"LS = {factors.get('LS', '—')} · C = {factors.get('C', '—')} · P = {factors.get('P', '—')}")
            if er.get("recommendation"):
                st.info(er["recommendation"])
    else:
        st.info("Erosion risk data unavailable.")

    # ── Override controls ────────────────────────────────────────────────────
    st.divider()
    st.subheader("Manual Override")
    with st.form("physical_override"):
        field = st.selectbox("Parameter", physical_fields,
                             format_func=lambda f: field_labels[f][0], key="po_field")
        value = st.number_input("New Value", step=0.01, key="po_value")
        reason = st.text_input("Reason for override", key="po_reason")
        if st.form_submit_button("Apply Physical Override"):
            result = api_post("/physical/override", {"field": field, "value": value, "reason": reason})
            if result and result.get("status") == "ok":
                st.success(f"Override applied: {field_labels[field][0]} = {value} {field_labels[field][1]}")
            else:
                st.error(str(result))

    # ── Threshold editing ────────────────────────────────────────────────────
    st.divider()
    st.subheader("Threshold Configuration")
    thresholds = summary.get("thresholds", {})
    for field in physical_fields:
        t = summary.get("thresholds", {}).get(field, {})
        if t:
            with st.expander(f"{label} — warn: {t.get('warn')} {unit} · crit: {t.get('crit')} {unit}"):
                col_t1, col_t2 = st.columns(2)
                with col_t1:
                    new_warn = st.number_input(
                        "Warning threshold", value=float(t.get("warn", 0)),
                        step=0.01, key=f"tw_{field}",
                    )
                with col_t2:
                    new_crit = st.number_input(
                        "Critical threshold", value=float(t.get("crit", 0)),
                        step=0.01, key=f"tc_{field}",
                    )
                if st.button(f"Update {label} thresholds", key=f"tb_{field}"):
                    result = api_post("/ui/scientist/threshold",
                                      {"field": field, "warn": new_warn, "crit": new_crit})
                    if result and "error" not in result:
                        st.success("Threshold updated")
                    else:
                        st.error(str(result))


# ── Chemical Segment ────────────────────────────────────────────────────────

def render_chemical():
    st.markdown('<h2 class="section-chemical">Chemical Segment</h2>', unsafe_allow_html=True)
    st.caption("pH, Nitrogen, Phosphorus, Potassium, EC, Organic Matter")

    summary = api_get("/ui/scientist/summary")
    if not summary:
        return

    sensors = summary.get("sensors", {})
    chemical_fields = ["soil_ph", "nitrogen_ppm", "phosphorus_ppm", "potassium_ppm", "ec_ds_m", "organic_matter_pct"]

    # Current values
    cols = st.columns(3)
    for i, field in enumerate(chemical_fields):
        with cols[i % 3]:
            info = sensors.get(field, {})
            val = info.get("value")
            unit = info.get("unit", "")
            status = info.get("status", "no_data")
            st.metric(
                field.replace("_", " ").title(),
                f"{val:.2f} {unit}" if val is not None else "—",
            )
            st.markdown(status_badge(status), unsafe_allow_html=True)

    st.divider()

    # Nutrient radar
    st.subheader("Nutrient Balance (Radar)")
    nutrient_radar(sensors)

    # Chemical depletion states
    depletion = summary.get("depletion_states", {})
    chem_states = {k: v for k, v in depletion.items() if k in ("S1", "S2", "S3", "S6")}
    if chem_states:
        st.warning(f"Active Chemical Depletion: {', '.join(chem_states.keys())}")
        for code, info in chem_states.items():
            st.write(f"**{code} — {info.get('name', '')}**")

    # Time series
    st.subheader("Time Series")
    minutes = st.slider("Time range (minutes)", 10, 360, 60, key="chem_minutes")
    c1, c2 = st.columns(2)
    for i, field in enumerate(chemical_fields):
        with c1 if i % 2 == 0 else c2:
            time_series_chart(field, minutes)

    # Override controls
    st.divider()
    st.subheader("Manual Override / Simulate Intervention")
    with st.form("chemical_override"):
        field = st.selectbox("Parameter", chemical_fields)
        value = st.number_input("New Value", step=0.1)
        reason = st.text_input("Reason (e.g., 'Applied 100 kg/ha CAN')")
        if st.form_submit_button("Apply Override"):
            result = api_post("/ui/scientist/override", {"field": field, "value": value, "reason": reason})
            if result and "error" not in result:
                st.success(f"Override applied: {field} = {value}")
            else:
                st.error(str(result))


# ── Biological Segment ──────────────────────────────────────────────────────

def render_biological():
    st.markdown('<h2 class="section-biological">Biological Segment</h2>', unsafe_allow_html=True)
    st.caption("Microbial Biomass, Soil Respiration")

    summary = api_get("/ui/scientist/summary")
    if not summary:
        return

    sensors = summary.get("sensors", {})
    bio_fields = ["microbial_biomass_mg_c_kg", "soil_respiration_mg_co2_kg_day"]

    c1, c2 = st.columns(2)
    for i, field in enumerate(bio_fields):
        with c1 if i == 0 else c2:
            info = sensors.get(field, {})
            val = info.get("value")
            unit = info.get("unit", "")
            status = info.get("status", "no_data")
            st.metric(
                field.replace("_", " ").title(),
                f"{val:.1f} {unit}" if val is not None else "—",
            )
            st.markdown(status_badge(status), unsafe_allow_html=True)

    # Biological activity index
    mb = sensors.get("microbial_biomass_mg_c_kg", {}).get("value")
    resp = sensors.get("soil_respiration_mg_co2_kg_day", {}).get("value")
    if mb is not None and resp is not None:
        activity_index = min((mb / 300 + resp / 50) / 2 * 100, 100)
        st.metric("Biological Activity Index", f"{activity_index:.0f}/100")
        st.progress(activity_index / 100)

    # Depletion
    depletion = summary.get("depletion_states", {})
    if "S7" in depletion:
        st.error("S7 — Biologically Inactive detected")

    # Time series
    st.subheader("Time Series")
    minutes = st.slider("Time range (minutes)", 10, 360, 60, key="bio_minutes")
    for field in bio_fields:
        time_series_chart(field, minutes)

    # Override
    st.divider()
    st.subheader("Simulate Organic Amendment")
    with st.form("bio_override"):
        field = st.selectbox("Parameter", bio_fields)
        value = st.number_input("New Value", step=1.0)
        reason = st.text_input("Reason")
        if st.form_submit_button("Apply Override"):
            result = api_post("/ui/scientist/override", {"field": field, "value": value, "reason": reason})
            if result and "error" not in result:
                st.success(f"Override applied: {field} = {value}")
            else:
                st.error(str(result))


# ── Soil Parameters ─────────────────────────────────────────────────────────

_PARAM_META = {
    # field:  (display_name, category)
    "soil_moisture_pct":              ("Soil Moisture",             "Physical"),
    "bulk_density_g_cm3":             ("Bulk Density",              "Physical"),
    "soil_temp_c":                    ("Soil Temperature",          "Physical"),
    "soil_ph":                        ("Soil pH",                   "Chemical"),
    "nitrogen_ppm":                   ("Nitrogen",                  "Chemical"),
    "phosphorus_ppm":                 ("Phosphorus",                "Chemical"),
    "potassium_ppm":                  ("Potassium",                 "Chemical"),
    "ec_ds_m":                        ("Electrical Conductivity",   "Chemical"),
    "organic_matter_pct":             ("Organic Matter",            "Chemical"),
    "microbial_biomass_mg_c_kg":      ("Microbial Biomass",         "Biological"),
    "soil_respiration_mg_co2_kg_day": ("Soil Respiration",          "Biological"),
}

def render_soil_parameters():
    st.subheader("Soil Parameters")
    st.caption(
        "Set the warning/critical thresholds and healthy ranges for your parcel. "
        "These replace the soil-type defaults and are used across all analysis, "
        "health scoring, and alerts."
    )

    data = api_get("/ui/scientist/soil_parameters")
    if not data:
        st.error("Cannot load soil parameters")
        return

    params = data.get("parameters", {})
    soil_type = data.get("soil_type", "—")
    parcel_id = data.get("parcel_id", "")

    st.info(f"Parcel: `{parcel_id}` · Soil type: **{soil_type.title()}** · "
            f"Custom fields are marked ✏️ · Defaults are marked 📋")

    new_thresholds = {}
    new_ranges = {}

    for category in ("Physical", "Chemical", "Biological"):
        fields_in_cat = [f for f, (_, cat) in _PARAM_META.items() if cat == category]
        st.markdown(f"#### {category} Parameters")

        for field in fields_in_cat:
            p = params.get(field)
            if p is None:
                continue

            label, _ = _PARAM_META[field]
            unit = p.get("unit", "")
            t_custom = p.get("threshold_custom", False)
            r_custom = p.get("range_custom", False)
            t_icon = "✏️" if t_custom else "📋"
            r_icon = "✏️" if r_custom else "📋"

            with st.expander(
                f"{t_icon if t_custom or r_custom else '📋'} **{label}** "
                f"({unit}) — warn: {p.get('threshold_warn', '—')} · "
                f"crit: {p.get('threshold_crit', '—')} · "
                f"healthy: {p.get('healthy_min', '—')}–{p.get('healthy_max', '—')}"
            ):
                c1, c2, c3, c4 = st.columns(4)
                with c1:
                    tw = st.number_input(
                        f"{t_icon} Warn threshold",
                        value=float(p["threshold_warn"]) if p.get("threshold_warn") is not None else 0.0,
                        step=0.1, key=f"tw_{field}",
                        help="Alert fires when value crosses this level"
                    )
                with c2:
                    tc = st.number_input(
                        f"{t_icon} Crit threshold",
                        value=float(p["threshold_crit"]) if p.get("threshold_crit") is not None else 0.0,
                        step=0.1, key=f"tc_{field}"
                    )
                with c3:
                    rmin = st.number_input(
                        f"{r_icon} Healthy min",
                        value=float(p["healthy_min"]) if p.get("healthy_min") is not None else 0.0,
                        step=0.1, key=f"rmin_{field}"
                    )
                with c4:
                    rmax = st.number_input(
                        f"{r_icon} Healthy max",
                        value=float(p["healthy_max"]) if p.get("healthy_max") is not None else 0.0,
                        step=0.1, key=f"rmax_{field}"
                    )

                new_thresholds[field] = {"warn": tw, "crit": tc}
                new_ranges[field] = {"min": rmin, "max": rmax,
                                      "unit": unit}

        st.divider()

    if st.button("💾 Save all parameters", type="primary", use_container_width=True):
        result = api_post("/ui/scientist/soil_parameters", {
            "parcel_id": parcel_id,
            "thresholds": new_thresholds,
            "healthy_ranges": new_ranges,
        })
        if result and "error" not in result:
            st.success(
                f"Saved {result.get('thresholds_saved', 0)} thresholds and "
                f"{result.get('ranges_saved', 0)} ranges for parcel `{parcel_id}`."
            )
            st.rerun()
        else:
            st.error(str(result))

    st.divider()
    if st.button("↩️ Reset to soil-type defaults", use_container_width=True):
        try:
            r = requests.delete(
                f"{API_V1}/ui/scientist/soil_parameters/{parcel_id}",
                headers=api_headers(), timeout=10,
            )
            if r.status_code == 200:
                st.success("All custom parameters cleared — using soil-type defaults.")
                st.rerun()
            else:
                st.error(f"Reset failed: {r.text}")
        except Exception as e:
            st.error(f"Reset failed: {e}")


# ── Layer Observability ─────────────────────────────────────────────────────

def render_layers():
    st.subheader("Layer Observability & Interaction")

    status_data = api_get("/ui/scientist/layer_status")
    if not status_data:
        st.error("Cannot fetch layer status")
        return

    layers = status_data.get("layers", [])
    for layer in layers:
        name = layer["name"]
        status = layer.get("status", "unknown")
        icon = "🟢" if status == "running" else "🔴" if status == "stopped" else "🟡"

        with st.expander(f"{icon} {name} — {status.upper()}"):
            st.text(f"Module: {layer['module']}")
            st.text(f"Status: {status}")
            hb = layer.get("last_heartbeat")
            st.text(f"Last heartbeat: {hb or 'No heartbeat recorded'}")

    st.divider()
    st.subheader("System State")
    st.metric("Current State", status_data.get("system_state", "unknown").upper())


# ── Cross-Domain Dashboard ──────────────────────────────────────────────────

def render_cross_domain():
    st.subheader("Cross-Domain Integration")

    cd_data = api_get("/ui/scientist/cross_domain")
    if not cd_data:
        st.error("Cannot fetch cross-domain data")
        return

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Plant DT (Consumed)**")
        pd_data = cd_data.get("plant_demand")
        if pd_data:
            st.json(pd_data)
        else:
            st.info("No data received from Plant DT yet")

    with c2:
        st.markdown("**Biotic Pod DT (Consumed)**")
        bd_data = cd_data.get("biotic_data")
        if bd_data:
            st.json(bd_data)
        else:
            st.info("No data received from Biotic Pod DT yet")

    st.divider()
    st.subheader("Recent Cross-Domain Messages")
    messages = cd_data.get("recent_messages", [])
    if messages:
        for msg in messages[:15]:
            direction = "→" if msg.get("source_domain") == "soil_dt" else "←"
            target = msg.get("target_domain", msg.get("source_domain", "?"))
            st.text(f"{direction} {target} | {msg.get('message_type', '?')} | {msg.get('sent_at', '?')}")
    else:
        st.info("No cross-domain messages yet")

    # Data source management
    st.divider()
    st.subheader("External Data Sources")
    sources = cd_data.get("data_sources", [])
    if sources:
        st.dataframe(pd.DataFrame(sources), use_container_width=True, hide_index=True)

    with st.expander("Add New Data Source"):
        with st.form("add_datasource"):
            name = st.text_input("Name", placeholder="Weather Station API")
            stype = st.selectbox("Type", ["mqtt", "rest_api", "database"])
            uri = st.text_input("Connection URI")
            topic = st.text_input("MQTT Topic (if applicable)")
            if st.form_submit_button("Add Source"):
                result = api_post("/ui/scientist/data_source", {
                    "name": name, "source_type": stype,
                    "connection_uri": uri, "mqtt_topic": topic,
                })
                if result and "error" not in result:
                    st.success(f"Data source added: {name}")
                else:
                    st.error(str(result))


# ── Knowledge Graph ─────────────────────────────────────────────────────────

def render_knowledge_graph():
    st.subheader("Soil Depletion Knowledge Graph")

    kg_data = api_get("/ui/scientist/knowledge_graph")
    if not kg_data or kg_data.get("error"):
        error_msg = kg_data.get("error", "No data") if kg_data else "Cannot connect to API"
        st.warning(f"Knowledge graph not available: {error_msg}")
        st.info("Make sure Neo4j is running and the KG has been initialized with `python main.py --setup`")
        return

    nodes = kg_data.get("nodes", [])
    edges = kg_data.get("edges", [])

    st.metric("Nodes", len(nodes))
    st.metric("Relationships", len(edges))

    # Display nodes by type
    if nodes:
        node_types = {}
        for n in nodes:
            ntype = n.get("type", "Unknown")
            if ntype not in node_types:
                node_types[ntype] = []
            node_types[ntype].append(n.get("name", n.get("id", "?")))

        for ntype, names in node_types.items():
            with st.expander(f"{ntype} ({len(names)} nodes)"):
                for name in sorted(names):
                    st.text(f"  • {name}")

    # Display edges
    if edges:
        with st.expander(f"Relationships ({len(edges)})"):
            for e in edges[:50]:
                st.text(f"  {e.get('source', '?')} —[{e.get('type', '?')}]→ {e.get('target', '?')}")


# ── Audit Log ───────────────────────────────────────────────────────────────

def render_audit_log():
    st.subheader("Audit Log")

    audit_data = api_get("/ui/scientist/audit_log", {"limit": 50})
    if not audit_data:
        st.info("No audit log entries yet")
        return

    entries = audit_data.get("entries", [])
    if entries:
        for entry in entries:
            ts = entry.get("timestamp", "")
            if isinstance(ts, dict):
                ts = ts.get("$date", "")
            action = entry.get("action", "?")
            user_id = entry.get("user_id", "?")[:8]
            st.text(f"[{ts}] {action} (user: {user_id}...)")
            if entry.get("details"):
                st.json(entry["details"])
    else:
        st.info("No audit log entries")


# ── Scientist Profile ──────────────────────────────────────────────────────

def render_scientist_profile():
    st.subheader("👤 Your Profile")

    profile = api_get("/auth/profile")
    if not profile:
        st.error("Cannot load profile")
        return

    st.markdown(f"**User ID:** `{profile.get('user_id', '?')[:12]}...`")
    st.markdown(f"**Role:** {profile.get('role', '?').title()}")
    st.markdown(f"**Member since:** {profile.get('created_at', 'Unknown')}")

    st.divider()

    with st.form("scientist_profile_edit"):
        new_name = st.text_input("Full name", value=profile.get("name", ""))
        new_email = st.text_input("Email", value=profile.get("email", ""))
        new_phone = st.text_input("Phone", value=profile.get("phone", ""))
        new_location = st.text_input("Institution / Location", value=profile.get("location", ""))

        if st.form_submit_button("💾 Save changes", type="primary"):
            result = api_put("/auth/profile", {
                "name": new_name, "email": new_email,
                "phone": new_phone, "location": new_location,
            })
            if result and "error" not in result:
                st.success("Profile updated")
                st.rerun()
            else:
                st.error(result.get("error", "Update failed"))

    st.divider()
    st.markdown("#### Change Password")
    with st.form("scientist_pw_change"):
        old_pw = st.text_input("Current password", type="password")
        new_pw = st.text_input("New password", type="password")
        confirm = st.text_input("Confirm new password", type="password")
        if st.form_submit_button("Update password"):
            if new_pw != confirm:
                st.error("Passwords don't match")
            elif len(new_pw) < 6:
                st.error("Password must be at least 6 characters")
            else:
                result = api_post("/auth/change-password", {
                    "old_password": old_pw, "new_password": new_pw,
                })
                if result and "error" not in result:
                    st.success("Password changed")
                else:
                    st.error(result.get("error", "Change failed"))


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