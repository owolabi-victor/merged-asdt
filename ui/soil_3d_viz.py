# ui/soil_3d_viz.py
"""
3D Soil Simulation for the ASDT Scientist Dashboard.

All parcel dimensions (size, depth, sensor positions, horizon depths) come from
the scientist's parcel geometry configuration stored in MongoDB — nothing is
hardcoded except scientifically-grounded depth-gradient models.
"""
import hashlib
import numpy as np
import plotly.graph_objects as go
import streamlit as st

NX = NY = 20   # grid columns (horizontal resolution)
NZ = 12        # grid layers  (depth resolution)

# ── Parameter configuration ──────────────────────────────────────────────────
# depth_fn: z goes 0 (surface) → -depth_m (bottom).  Output normalised to mean=1.
_P = {
    "nitrogen_ppm": {
        "name": "Nitrogen (N)", "unit": "ppm", "cat": "Chemical",
        "cs": "RdYlGn", "bad_low": True,
        "depth": lambda z, d: np.exp(3.0 * z / d),
    },
    "phosphorus_ppm": {
        "name": "Phosphorus (P)", "unit": "mg/kg", "cat": "Chemical",
        "cs": "RdYlGn", "bad_low": True,
        "depth": lambda z, d: np.exp(2.5 * z / d),
    },
    "potassium_ppm": {
        "name": "Potassium (K)", "unit": "mg/kg", "cat": "Chemical",
        "cs": "RdYlGn", "bad_low": True,
        "depth": lambda z, d: np.exp(1.8 * z / d),
    },
    "soil_ph": {
        "name": "Soil pH", "unit": "pH", "cat": "Chemical",
        "cs": "RdYlGn", "bad_low": True,
        "depth": lambda z, d: 1.0 + 0.06 * (z / d),
    },
    "organic_matter_pct": {
        "name": "Organic Matter", "unit": "%", "cat": "Chemical",
        "cs": "YlOrBr_r", "bad_low": True,
        "depth": lambda z, d: np.exp(4.0 * z / d),
    },
    "ec_ds_m": {
        "name": "Elect. Conductivity", "unit": "dS/m", "cat": "Chemical",
        "cs": "RdYlGn_r", "bad_low": False,
        "depth": lambda z, d: 1.0 - 0.2 * (z / d),
    },
    "soil_moisture_pct": {
        "name": "Soil Moisture", "unit": "%", "cat": "Physical",
        "cs": "Blues", "bad_low": True,
        "depth": lambda z, d: 0.75 - 0.25 * (z / d),
    },
    "bulk_density_g_cm3": {
        "name": "Bulk Density", "unit": "g/cm³", "cat": "Physical",
        "cs": "RdYlGn_r", "bad_low": False,
        "depth": lambda z, d: 1.0 - 0.3 * (z / d),
    },
    "soil_temp_c": {
        "name": "Soil Temperature", "unit": "°C", "cat": "Physical",
        "cs": "RdBu_r", "bad_low": False,
        "depth": lambda z, d: 1.0 + 0.04 * (z / d),
    },
    "microbial_biomass_mg_c_kg": {
        "name": "Microbial Biomass", "unit": "mg C/kg", "cat": "Biological",
        "cs": "YlGn", "bad_low": True,
        "depth": lambda z, d: np.exp(3.5 * z / d),
    },
    "soil_respiration_mg_co2_kg_day": {
        "name": "Soil Respiration", "unit": "mg CO₂/kg/day", "cat": "Biological",
        "cs": "YlGn", "bad_low": True,
        "depth": lambda z, d: np.exp(3.0 * z / d),
    },
}

PARAM_KEYS   = list(_P.keys())
_CAT_ORDER   = ["Physical", "Chemical", "Biological"]
_STATUS_COLOR = {
    "healthy": "#059669", "ok": "#059669",
    "warning": "#d97706", "critical": "#dc2626",
}

_DEFAULT_GEO = {
    "width_m": 10.0, "length_m": 10.0, "depth_m": 1.0,
    "sensor_count": 4, "sensor_depth_cm": 10.0,
    "topsoil_depth_cm": 20.0, "subsoil_depth_cm": 50.0,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parcel_seed(parcel_id: str) -> int:
    return int(hashlib.md5(parcel_id.encode()).hexdigest()[:8], 16) % (2 ** 31)


def _sensor_positions(geo: dict) -> list[tuple[float, float]]:
    """
    Return (x, y) positions for each sensor node, evenly distributed
    inside the parcel according to sensor_count.
    Supports 1, 2, 4, or 9 sensors.
    """
    w, l, n = geo["width_m"], geo["length_m"], geo["sensor_count"]
    if n <= 1:
        return [(w / 2, l / 2)]
    if n == 2:
        return [(w * 0.33, l / 2), (w * 0.67, l / 2)]
    if n <= 4:
        return [(w * 0.25, l * 0.25), (w * 0.75, l * 0.25),
                (w * 0.25, l * 0.75), (w * 0.75, l * 0.75)]
    # 9-sensor grid
    pts = []
    for row in [0.25, 0.50, 0.75]:
        for col in [0.25, 0.50, 0.75]:
            pts.append((w * col, l * row))
    return pts[:n]


def _build_volume(base_val: float, field: str, parcel_id: str, geo: dict):
    """Generate NX × NY × NZ values for a parameter using the parcel's dimensions."""
    meta = _P[field]
    w, l, d = geo["width_m"], geo["length_m"], geo["depth_m"]

    x = np.linspace(0, w, NX)
    y = np.linspace(0, l, NY)
    z = np.linspace(0.0, -d, NZ)     # 0 = surface, -d = max depth
    X, Y, Z = np.meshgrid(x, y, z, indexing="ij")

    depth = meta["depth"](Z, d)
    depth = depth / depth.mean()

    rng = np.random.default_rng(_parcel_seed(parcel_id))
    scale_x = 10.0 / w   # keep spatial frequency consistent regardless of size
    scale_y = 10.0 / l
    scale_z = 1.0 / d
    noise = (
        0.07 * np.sin(X * 0.9 * scale_x) * np.cos(Y * 1.2 * scale_y) +
        0.05 * np.cos(X * 2.1 * scale_x + 0.5) * np.sin(Y * 2.8 * scale_y) +
        0.03 * np.sin((X * scale_x + Y * scale_y) * 1.7) * np.cos(Z * 5 * scale_z) +
        0.02 * rng.standard_normal(X.shape)
    )

    values = base_val * depth * (1.0 + noise)
    return X.flatten(), Y.flatten(), Z.flatten(), values.flatten()


# ── Figure builder ────────────────────────────────────────────────────────────

def build_figure(sensors: dict, field: str, parcel_id: str,
                 soil_type: str, geo: dict) -> go.Figure:
    meta   = _P[field]
    sdata  = sensors.get(field, {})
    val    = sdata.get("value")
    status = sdata.get("status", "no_data")
    unit   = sdata.get("unit", meta["unit"])

    col = _STATUS_COLOR.get(status, "#6b7280")
    if val is not None:
        badge = {"healthy": "✅ OK", "ok": "✅ OK",
                 "warning": "⚠ WARNING", "critical": "🔴 CRITICAL"}.get(status, "⬜")
        alert = f"{badge} — {val:.3g} {unit}"
    else:
        alert = "No sensor data yet"

    w  = geo["width_m"]
    l  = geo["length_m"]
    d  = geo["depth_m"]
    sd = geo["sensor_depth_cm"] / 100.0          # convert cm → m
    t1 = -(geo["topsoil_depth_cm"]  / 100.0)     # topsoil boundary (negative = below surface)
    t2 = -(geo["subsoil_depth_cm"]  / 100.0)

    fig = go.Figure()

    # ── Volumetric render ────────────────────────────────────────────────────
    if val is not None and val > 0:
        xf, yf, zf, vf = _build_volume(val, field, parcel_id, geo)
        p2  = float(np.percentile(vf, 2))
        p98 = float(np.percentile(vf, 98))
        if p2 >= p98:
            p98 = p2 + max(0.01, abs(p2) * 0.1)

        # Depth-slice positions: surface, topsoil boundary, subsoil boundary, max depth
        slice_z = [0.0, t1, t2, -d]

        fig.add_trace(go.Volume(
            x=xf, y=yf, z=zf, value=vf,
            isomin=p2, isomax=p98,
            opacity=0.14,
            surface_count=20,
            colorscale=meta["cs"],
            colorbar=dict(
                title=dict(text=unit, font=dict(color="#8b949e", size=11)),
                tickfont=dict(color="#8b949e", size=10),
                x=1.01, thickness=14, len=0.65, y=0.5,
            ),
            caps=dict(x_show=False, y_show=False, z_show=True),
            slices=dict(z=dict(show=True, locations=slice_z)),
            name=meta["name"],
            hovertemplate=(
                f"<b>{meta['name']}</b><br>"
                "x: %{x:.1f} m  y: %{y:.1f} m<br>"
                "Depth: %{z:.2f} m<br>"
                f"Value: %{{value:.4g}} {unit}<extra></extra>"
            ),
        ))

    # ── Soil horizon boundary planes ─────────────────────────────────────────
    bx = np.array([[0, w], [0, w]], dtype=float)
    by = np.array([[0, 0], [l, l]], dtype=float)
    for depth_m, label, rgba in [
        (t1, f"Topsoil / Subsoil  ({geo['topsoil_depth_cm']:.0f} cm)",
             "rgba(150,100,45,0.22)"),
        (t2, f"Subsoil / Substratum  ({geo['subsoil_depth_cm']:.0f} cm)",
             "rgba(100,65,25,0.22)"),
    ]:
        if abs(depth_m) < d:   # only draw if boundary is within monitoring depth
            fig.add_trace(go.Surface(
                x=bx, y=by, z=np.full((2, 2), depth_m),
                surfacecolor=np.zeros((2, 2)),
                colorscale=[[0, rgba], [1, rgba]],
                showscale=False, opacity=0.38,
                name=label,
                hovertemplate=f"<b>{label}</b><extra></extra>",
            ))

    # ── Sensor nodes and cables ───────────────────────────────────────────────
    positions = _sensor_positions(geo)
    sx_list = [p[0] for p in positions]
    sy_list = [p[1] for p in positions]
    sz_list = [-sd] * len(positions)

    fig.add_trace(go.Scatter3d(
        x=sx_list, y=sy_list, z=sz_list,
        mode="markers+text",
        marker=dict(size=7, color="#3b82f6", symbol="diamond",
                    line=dict(color="#93c5fd", width=1)),
        text=[f"S{i+1}" for i in range(len(positions))],
        textfont=dict(color="#93c5fd", size=10),
        textposition="top center",
        name=f"Sensor nodes ({geo['sensor_depth_cm']:.0f} cm depth)",
        hovertemplate=(
            "<b>%{text}</b><br>"
            f"Depth: {geo['sensor_depth_cm']:.0f} cm<extra></extra>"
        ),
    ))

    for sx, sy in positions:
        fig.add_trace(go.Scatter3d(
            x=[sx, sx], y=[sy, sy], z=[0.0, -sd],
            mode="lines",
            line=dict(color="#3b82f6", width=2, dash="dot"),
            showlegend=False, hoverinfo="skip",
        ))

    # ── z-axis ticks driven by parcel geometry ───────────────────────────────
    tick_vals  = sorted({0.0, t1, t2, -d}, reverse=True)
    tick_texts = []
    for tv in tick_vals:
        cm = abs(tv) * 100
        tick_texts.append("Surface" if tv == 0.0 else f"{cm:.0f} cm")

    # ── Layout ───────────────────────────────────────────────────────────────
    fig.update_layout(
        title=dict(
            text=(
                f"<b>{parcel_id}</b>  ·  {soil_type} soil  ·  {meta['name']}<br>"
                f"<span style='font-size:13px;color:{col}'>{alert}</span>"
            ),
            font=dict(size=15, color="#e6edf3"),
            x=0.01, xanchor="left", y=0.98,
        ),
        scene=dict(
            xaxis=dict(
                title=dict(text=f"East (m)  [0–{w:.0f} m]",
                           font=dict(color="#8b949e", size=11)),
                range=[0, w], backgroundcolor="#0d1117", gridcolor="#21262d",
                showbackground=True, tickfont=dict(color="#8b949e", size=10),
            ),
            yaxis=dict(
                title=dict(text=f"North (m)  [0–{l:.0f} m]",
                           font=dict(color="#8b949e", size=11)),
                range=[0, l], backgroundcolor="#0d1117", gridcolor="#21262d",
                showbackground=True, tickfont=dict(color="#8b949e", size=10),
            ),
            zaxis=dict(
                title=dict(text=f"Depth  [0–{d*100:.0f} cm]",
                           font=dict(color="#8b949e", size=11)),
                range=[-(d + 0.05), 0.1],
                backgroundcolor="#0d1117", gridcolor="#21262d",
                showbackground=True, tickfont=dict(color="#8b949e", size=10),
                tickvals=tick_vals, ticktext=tick_texts,
            ),
            bgcolor="#0d1117",
            camera=dict(
                eye=dict(x=1.75, y=-1.85, z=1.35),
                center=dict(x=0, y=0, z=-0.25),
            ),
            aspectmode="manual",
            aspectratio=dict(x=max(0.8, w / l), y=max(0.8, l / w), z=0.65),
        ),
        paper_bgcolor="#0d1117",
        font=dict(color="#e6edf3"),
        height=650,
        margin=dict(l=0, r=20, t=70, b=0),
        legend=dict(
            bgcolor="rgba(13,17,23,0.88)", bordercolor="#30363d", borderwidth=1,
            font=dict(color="#8b949e", size=10), x=0.01, y=0.01,
        ),
    )
    return fig


# ── Status grid ───────────────────────────────────────────────────────────────

def _status_grid(sensors: dict):
    for cat in _CAT_ORDER:
        fields = [k for k in PARAM_KEYS if _P[k]["cat"] == cat]
        st.markdown(f"**{cat}**")
        cols = st.columns(len(fields))
        for col, field in zip(cols, fields):
            s      = sensors.get(field, {})
            val    = s.get("value")
            stat   = s.get("status", "no_data")
            unit   = s.get("unit", _P[field]["unit"])
            hmin   = s.get("healthy_min")
            hmax   = s.get("healthy_max")
            border = _STATUS_COLOR.get(stat, "#6b7280")
            badge  = {"healthy": "✅", "ok": "✅",
                      "warning": "⚠️", "critical": "🔴"}.get(stat, "⬜")
            val_str   = f"{val:.3g}" if val is not None else "—"
            range_str = (f"{hmin}–{hmax} {unit}"
                         if hmin is not None and hmax is not None else "")
            col.markdown(
                f"""<div style='background:#161b22;border:1px solid #30363d;
                border-left:3px solid {border};border-radius:6px;
                padding:10px 12px;margin:3px 0;min-height:90px'>
                <div style='font-size:10px;color:#8b949e;margin-bottom:2px'>
                    {_P[field]['name']}</div>
                <div style='font-size:22px;font-weight:700;color:#e6edf3;
                    font-family:JetBrains Mono,monospace'>{val_str}</div>
                <div style='font-size:10px;color:#8b949e'>{unit}  {badge}</div>
                <div style='font-size:9px;color:#484f58;margin-top:2px'>
                    healthy: {range_str}</div>
                </div>""",
                unsafe_allow_html=True,
            )


# ── Geometry config panel ─────────────────────────────────────────────────────

def _geometry_panel(api_get, api_patch, parcel_id: str, geo: dict) -> dict:
    """
    Expandable panel where the scientist enters their parcel's physical dimensions.
    Returns the (possibly updated) geometry dict.
    """
    with st.expander("⚙️ Parcel Setup — dimensions & sensors", expanded=(geo == _DEFAULT_GEO)):
        st.caption(
            "Set the actual dimensions of your field plot, "
            "sensor deployment depth, and soil horizon boundaries."
        )
        c1, c2, c3 = st.columns(3)
        with c1:
            w = st.number_input("Width East-West (m)",
                                min_value=1.0, max_value=10000.0,
                                value=float(geo["width_m"]), step=1.0,
                                key="geo_width")
            l = st.number_input("Length North-South (m)",
                                min_value=1.0, max_value=10000.0,
                                value=float(geo["length_m"]), step=1.0,
                                key="geo_length")
        with c2:
            d = st.number_input("Monitoring depth (m)",
                                min_value=0.1, max_value=5.0,
                                value=float(geo["depth_m"]), step=0.1,
                                key="geo_depth")
            sensor_depth = st.number_input("Sensor depth (cm)",
                                           min_value=1.0, max_value=200.0,
                                           value=float(geo["sensor_depth_cm"]),
                                           step=1.0, key="geo_sdepth")
        with c3:
            n_sensors = st.selectbox("Number of sensors",
                                     [1, 2, 4, 9],
                                     index=[1, 2, 4, 9].index(
                                         geo["sensor_count"]
                                         if geo["sensor_count"] in [1, 2, 4, 9] else 4
                                     ),
                                     key="geo_nsensors")
            topsoil = st.number_input("Topsoil depth (cm)",
                                      min_value=1.0, max_value=100.0,
                                      value=float(geo["topsoil_depth_cm"]),
                                      step=1.0, key="geo_topsoil")
            subsoil = st.number_input("Subsoil depth (cm)",
                                      min_value=1.0, max_value=200.0,
                                      value=float(geo["subsoil_depth_cm"]),
                                      step=1.0, key="geo_subsoil")

        if st.button("Save parcel setup", key="geo_save"):
            new_geo = {
                "width_m": w, "length_m": l, "depth_m": d,
                "sensor_count": n_sensors,
                "sensor_depth_cm": sensor_depth,
                "topsoil_depth_cm": topsoil,
                "subsoil_depth_cm": subsoil,
            }
            result = api_patch(f"/ui/scientist/parcel_geometry?parcel_id={parcel_id}",
                               new_geo)
            if result and "geometry" in result:
                st.success("Parcel setup saved.")
                return result["geometry"]
            else:
                st.error(f"Save failed: {result}")

        return {
            "width_m": w, "length_m": l, "depth_m": d,
            "sensor_count": n_sensors,
            "sensor_depth_cm": sensor_depth,
            "topsoil_depth_cm": topsoil,
            "subsoil_depth_cm": subsoil,
        }


# ── Readings entry form ───────────────────────────────────────────────────────

# field → (category, label, unit, min_val, max_val, step, default)
# Final tuple element is the form default. It is 0.0 for every field: a
# pre-filled plausible number is indistinguishable from a measurement once
# saved, and the form treats 0 as "skipped", so nothing is recorded unless the
# user actually types it.
_FIELD_META = {
    "soil_moisture_pct":              ("Physical", "Soil Moisture (%)",         "%",             0.0,  100.0, 0.1,   25.0),
    "bulk_density_g_cm3":             ("Physical", "Bulk Density (g/cm³)",      "g/cm³",         0.5,    2.5, 0.01,   1.2),
    "soil_temp_c":                    ("Physical", "Soil Temperature (°C)",     "°C",            0.0,   60.0, 0.1,   22.0),
    "soil_ph":                        ("Chemical", "Soil pH", "pH", 3.0, 9.0, 0.1, 0.0),
    "nitrogen_ppm":                   ("Chemical", "Nitrogen (ppm)",            "ppm",           0.0, 1000.0, 1.0,  100.0),
    "phosphorus_ppm":                 ("Chemical", "Phosphorus (mg/kg)",        "mg/kg",         0.0,  500.0, 1.0,   30.0),
    "potassium_ppm":                  ("Chemical", "Potassium (mg/kg)",         "mg/kg",         0.0, 1000.0, 1.0,  150.0),
    "ec_ds_m":                        ("Chemical", "Elect. Conductivity (dS/m)","dS/m",          0.0,   10.0, 0.01,   0.4),
    "organic_matter_pct":             ("Chemical", "Organic Matter (%)",        "%",             0.0,   30.0, 0.1,    2.5),
    "microbial_biomass_mg_c_kg":      ("Biological", "Microbial Biomass (mg C/kg)","mg C/kg",     0.0, 2000.0, 1.0,  200.0),
    "soil_respiration_mg_co2_kg_day": ("Biological", "Soil Respiration (mg CO₂/kg/day)","mg CO₂/kg/day",0.0,500.0,0.1,25.0),
}


def _render_readings_entry_form(api_post, parcel_id: str, summary: dict,
                                 current_sensors: dict, *, expanded: bool = True):
    """
    Manual entry for the physical soil parameters.

    Restricted to the physical domain because that is what the deployed node
    instruments. Offering chemical and biological fields invited values that
    nothing had measured, and the dashboard then presented them alongside
    genuine sensor readings with no way to tell them apart. Manual entry is the
    fallback for a parcel with no sensor attached, not a substitute for one.
    """
    soil_type = summary.get("soil_type", "")
    avail     = summary.get("data_availability", {})
    pct       = avail.get("completion_pct", 0)

    with st.expander(
        f"📝 Enter / Update Soil Readings  "
        f"({'%.0f' % pct}% complete — {len(avail.get('fields_with_data', []))} / "
        f"{len([m for m in _FIELD_META.values() if m[0] == 'Physical'])} physical fields)",
        expanded=expanded,
    ):
        if not api_post:
            st.warning("Post API not available — cannot save readings from this context.")
            return

        st.caption(
            f"Parcel: `{parcel_id}` · Soil type: **{soil_type or 'not set'}** · "
            "Enter values you have measured yourself. Leave a field at 0 to skip "
            "it — a skipped field is recorded as absent, not as zero."
        )

        new_vals: dict[str, float] = {}

        for cat in ("Physical",):
            cat_fields = [(k, v) for k, v in _FIELD_META.items() if v[0] == cat]
            st.markdown(f"**{cat}**")
            cols = st.columns(len(cat_fields))
            for col, (field, (_, label, unit, fmin, fmax, step, default)) in zip(cols, cat_fields):
                existing = current_sensors.get(field, {})
                existing_val = existing.get("value")
                prefill = float(existing_val) if existing_val is not None else default
                new_vals[field] = col.number_input(
                    label, min_value=fmin, max_value=fmax,
                    value=prefill, step=step,
                    key=f"entry_{parcel_id}_{field}",
                )

        st.divider()
        col_save, col_info = st.columns([2, 3])
        with col_info:
            if avail.get("fields_with_data"):
                st.caption(
                    f"Last data: {', '.join(avail['fields_with_data'][:4])}"
                    + (" …" if len(avail.get("fields_with_data", [])) > 4 else "")
                )

        with col_save:
            if st.button("💾 Save readings", type="primary",
                          key=f"save_readings_{parcel_id}", use_container_width=True):
                # Only send fields the user actually set (> 0 or differs from default)
                to_save = {f: v for f, v in new_vals.items() if v > 0}
                if not to_save:
                    st.warning("All values are 0 — set at least one field to save.")
                else:
                    result = api_post("/readings/manual/batch", {
                        "parcel_id": parcel_id,
                        "readings": to_save,
                    })
                    if result and result.get("status") == "ok":
                        st.success(f"Saved {result['saved']} readings.")
                        st.rerun()
                    else:
                        st.error(f"Save failed: {result}")


# ── Main render entry-point ───────────────────────────────────────────────────

def render_3d_simulation(api_get, api_patch=None, api_post=None):
    """Called from app_scientist.py."""
    user      = st.session_state.get("user", {})
    parcels   = user.get("parcels") or ["unknown"]

    st.markdown("### 3D Soil Simulation")
    st.caption(
        "Volumetric render synthesised from your sensor readings · "
        "all dimensions from your parcel setup · unique spatial structure per parcel"
    )

    # ── Parcel selector (if user has more than one) ───────────────────────────
    if len(parcels) > 1:
        parcel_id = st.selectbox("Parcel", parcels, key="sim3d_parcel")
    else:
        parcel_id = parcels[0]

    # ── Load current data state ───────────────────────────────────────────────
    init_summary = api_get(f"/ui/scientist/summary?parcel_id={parcel_id}") or {}
    no_data      = init_summary.get("no_data", True)
    current_sensors = init_summary.get("sensors", {})

    # ── Data entry form (always accessible; expanded when no data) ────────────
    if api_post:
        _render_readings_entry_form(
            api_post, parcel_id, init_summary, current_sensors,
            expanded=no_data,
        )

    if no_data:
        st.info(
            "Enter your soil measurements above and click **Save readings** — "
            "the 3D model will render automatically once data is saved."
        )
        return

    # ── Load geometry from API ────────────────────────────────────────────────
    geo_resp  = api_get(f"/ui/scientist/parcel_geometry?parcel_id={parcel_id}") or {}
    geo       = {**_DEFAULT_GEO, **(geo_resp.get("geometry") or {})}

    # ── Parcel setup panel ────────────────────────────────────────────────────
    if api_patch:
        geo = _geometry_panel(api_get, api_patch, parcel_id, geo)

    # ── Parameter + refresh controls ─────────────────────────────────────────
    c1, c2, c3 = st.columns([5, 2, 2])
    with c1:
        idx = st.selectbox(
            "Parameter",
            range(len(PARAM_KEYS)),
            format_func=lambda i: (
                f"{_P[PARAM_KEYS[i]]['cat']} — {_P[PARAM_KEYS[i]]['name']}"
            ),
            key="sim3d_field_idx",
        )
    with c2:
        interval = st.selectbox("Refresh (s)", [5, 10, 30, 60],
                                index=1, key="sim3d_interval")
    with c3:
        live = st.toggle("Live updates", value=True, key="sim3d_live")

    selected_field = PARAM_KEYS[idx]
    run_every      = f"{interval}s" if live else None

    # ── Live fragment — only the chart re-runs on each tick ───────────────────
    @st.fragment(run_every=run_every)
    def _live():
        summary   = api_get(f"/ui/scientist/summary?parcel_id={parcel_id}") or {}
        sensors   = summary.get("sensors", {})
        soil_type = summary.get("soil_type", "loamy")
        last_ts   = summary.get("last_update", "")

        fig = build_figure(sensors, selected_field, parcel_id, soil_type, geo)
        st.plotly_chart(fig, use_container_width=True,
                        key=f"sim3d_{selected_field}_{parcel_id}")

        st.markdown("---")
        st.markdown("#### Live Readings — all parameters")
        _status_grid(sensors)

        # Cross-domain signals
        xd   = api_get("/ui/scientist/cross_domain") or {}
        msgs = xd.get("recent_messages", [])
        if msgs:
            with st.expander(
                "Connected system signals (Plant DT · Biotic Pod DT)", expanded=False
            ):
                for m in msgs[:6]:
                    src  = m.get("source_domain", "?")
                    ev   = m.get("event_type") or m.get("message_type", "")
                    ts   = (m.get("sent_at") or m.get("timestamp", ""))[:19].replace("T", " ")
                    sev  = m.get("severity", "info")
                    c    = {"critical": "#dc2626", "warning": "#d97706"}.get(sev, "#8b949e")
                    st.markdown(
                        f"<span style='color:{c};font-size:12px'>"
                        f"[{ts}] <b>{src}</b> → {ev}</span>",
                        unsafe_allow_html=True,
                    )

        if last_ts:
            st.caption(f"Sensor data as of: {last_ts}")

    _live()
