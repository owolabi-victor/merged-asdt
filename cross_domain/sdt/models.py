"""SDT Simulation & Model Layer.

Physics-based soil-water models implementing the design specification.
All parameter values from literature (Carsel & Parrish 1988; van Genuchten 1980;
Mualem 1976; Allen et al. 1998 FAO-56; Richards 1931).

Models registered here:
    VanGenuchtenRetention     — soil water retention (algebraic)
    MualemConductivity        — unsaturated hydraulic conductivity (algebraic)
    RichardsEquationModel     — 3-layer 1D soil water dynamics (physics ODE)
    ETDepletionSurrogate      — ML surrogate for ET estimation (scikit-learn stub)
    ProphetMoistureForecast   — 24h VWC forecast (Prophet stub)

Design reference:
    SDT_Water_Depletion_Design.pdf §6 (Simulation & Model Layer Requirements)
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.integrate import solve_ivp

from dt_forge.core.types import ModelType
from dt_forge.simulation.base import TwinModel


# ---------------------------------------------------------------------------
# Soil Parameters — Loamy Sand calibrated to design spec (FC=0.10, PWP=0.05)
# Reference: Carsel & Parrish (1988) Water Resources Research
# Calibration: α, n fitted so that θ(−33kPa)=0.10 and θ(−1500kPa)≈0.050
# ---------------------------------------------------------------------------

@dataclass
class SoilParams:
    """Van Genuchten–Mualem soil hydraulic parameters."""

    theta_r: float = 0.050       # residual VWC [m³/m³]
    theta_s: float = 0.430       # saturated VWC [m³/m³]
    alpha_cm: float = 0.022      # van Genuchten α [1/cm]   → 0.22 [1/m]
    n: float = 2.00              # van Genuchten n [-]
    ks_m_day: float = 0.44       # saturated hydraulic conductivity [m/day]
    l: float = 0.5               # Mualem tortuosity/connectivity [-]

    # Derived
    @property
    def m(self) -> float:
        return 1.0 - 1.0 / self.n

    # Water content thresholds [m³/m³]
    fc: float = 0.100            # field capacity @ -33 kPa
    pwp: float = 0.051           # permanent wilting point @ -1500 kPa
    wilting_risk: float = 0.055  # PWP + 10% buffer (WILTING_RISK threshold)
    raw: float = 0.075           # readily available water (midpoint FC-PWP)
    mad_threshold: float = 0.045 # management allowable depletion threshold


SANDY_LOAM_PARAMS = SoilParams()


# ---------------------------------------------------------------------------
# Van Genuchten Retention Model
# ---------------------------------------------------------------------------

class VanGenuchtenRetention:
    """
    Soil water retention: θ(ψ) and ψ(θ) relationships.

    Implements van Genuchten (1980) model:
        θ(ψ) = θr + (θs − θr) / (1 + |α·ψ|^n)^m
        ψ(θ) = (1/α) · [(Se^(−1/m) − 1)^(1/n)]  [ψ in cm water]

    Note: ψ is matric potential, negative values indicate suction.
    """

    model_name = "van_genuchten_retention"
    model_type = ModelType.PHYSICS

    def __init__(self, params: SoilParams = SANDY_LOAM_PARAMS):
        self.p = params

    def theta_from_psi(self, psi_cm: float) -> float:
        """Volumetric water content [m³/m³] from matric potential [cm water]."""
        if psi_cm >= 0.0:
            return self.p.theta_s
        x = abs(self.p.alpha_cm * psi_cm)
        return self.p.theta_r + (self.p.theta_s - self.p.theta_r) / (1.0 + x ** self.p.n) ** self.p.m

    def psi_from_theta(self, theta: float) -> float:
        """Matric potential [cm water] from volumetric water content."""
        theta = max(self.p.theta_r + 1e-6, min(self.p.theta_s - 1e-6, theta))
        se = (theta - self.p.theta_r) / (self.p.theta_s - self.p.theta_r)
        se = max(1e-6, min(1.0 - 1e-6, se))
        psi = -(1.0 / self.p.alpha_cm) * (se ** (-1.0 / self.p.m) - 1.0) ** (1.0 / self.p.n)
        return psi  # negative cm water

    def psi_kpa_from_theta(self, theta: float) -> float:
        """Matric potential [kPa] from VWC."""
        psi_cm = self.psi_from_theta(theta)
        return psi_cm * 0.09806  # 1 cm H₂O ≈ 0.09806 kPa

    def step(self, dt: float, inputs: dict[str, float]) -> dict[str, float]:
        theta = inputs.get("vwc", self.p.fc)
        return {
            "sim_psi_kpa": round(self.psi_kpa_from_theta(theta), 3),
        }

    def reset(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Mualem–van Genuchten Conductivity Model
# ---------------------------------------------------------------------------

class MualemConductivity:
    """
    Unsaturated hydraulic conductivity K(θ) via Mualem–van Genuchten model.

    K(θ) = Ks · Se^l · [1 − (1 − Se^(1/m))^m]²

    Returns K in m/day.
    """

    model_name = "mualem_conductivity"
    model_type = ModelType.PHYSICS

    def __init__(self, params: SoilParams = SANDY_LOAM_PARAMS):
        self.p = params

    def k_from_theta(self, theta: float) -> float:
        """Unsaturated hydraulic conductivity [m/day]."""
        se = (theta - self.p.theta_r) / (self.p.theta_s - self.p.theta_r)
        se = max(0.0, min(1.0, se))
        if se < 1e-8:
            return 0.0
        inner = (1.0 - se ** (1.0 / self.p.m)) ** self.p.m
        k = self.p.ks_m_day * (se ** self.p.l) * (1.0 - inner) ** 2
        return max(0.0, k)

    def step(self, dt: float, inputs: dict[str, float]) -> dict[str, float]:
        theta = inputs.get("vwc", self.p.fc)
        return {
            "sim_k_unsat_m_day": round(self.k_from_theta(theta), 6),
        }

    def reset(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Richards Equation Model — 3-layer 1D soil water dynamics
# ---------------------------------------------------------------------------

class RichardsEquationModel:
    """
    Simplified 1D Richards equation for a 3-layer soil column.

    Layer geometry (aligned with sensor depths):
        Layer 0: 0–20 cm   (sensor at 10 cm)
        Layer 1: 20–40 cm  (sensor at 30 cm)
        Layer 2: 40–70 cm  (sensor at 60 cm)

    ODE per layer:
        dθᵢ/dt = (flux_in − flux_out) / Δzᵢ − Sᵢ(θᵢ, t)

    Flux between layers (Darcy + gravity):
        q₍ᵢ,ᵢ₊₁₎ = −K_avg(θᵢ, θᵢ₊₁) · (ψᵢ₊₁ − ψᵢ)/Δz + K_avg   [m/day]

    Sink term (root water uptake Feddes model, simplified):
        S₀ = 60% of ETc, S₁ = 30%, S₂ = 10%  (rooting depth ~ 50 cm)

    Rain input enters Layer 0 as an instantaneous addition (ponding ignored).

    References:
        Richards (1931); Allen et al. FAO-56 (1998); Šimůnek et al. HYDRUS.
    """

    model_name = "richards_equation"
    model_type = ModelType.PHYSICS

    # Layer thicknesses [m]
    _DZ = np.array([0.20, 0.20, 0.30])
    # Layer mid-depths [m] — matches sensor depths
    _Z_MID = np.array([0.10, 0.30, 0.60])
    # Root uptake fractions per layer (Feddes simplified, maize V6-V8 50cm root)
    _ROOT_FRAC = np.array([0.60, 0.30, 0.10])

    def __init__(
        self,
        params: SoilParams = SANDY_LOAM_PARAMS,
        initial_vwc: np.ndarray | None = None,
        dt_day: float = 15.0 / 1440.0,  # 15 min → days
    ):
        self.p = params
        self.retention = VanGenuchtenRetention(params)
        self.conductivity = MualemConductivity(params)
        self.dt = dt_day  # default step size in days

        if initial_vwc is None:
            # Start near field capacity (FC=0.10) so a fresh/reset run begins OPTIMAL.
            # The arid weather will drive depletion naturally from here.
            self.state = np.array([0.090, 0.095, 0.100])
        else:
            self.state = np.array(initial_vwc, dtype=float)
        self._initial_state = self.state.copy()

    def _flux(self, theta_up: float, theta_dn: float, dz: float) -> float:
        """Darcy flux from upper to lower layer [m/day]. Positive = downward."""
        k_avg = 0.5 * (self.conductivity.k_from_theta(theta_up)
                       + self.conductivity.k_from_theta(theta_dn))
        psi_up = self.retention.psi_from_theta(theta_up) / 100.0  # cm → m
        psi_dn = self.retention.psi_from_theta(theta_dn) / 100.0
        grad = (psi_dn - psi_up) / dz  # pressure gradient
        return -k_avg * (grad - 1.0)   # +1 for gravity component (downward positive)

    def _derivatives(self, t: float, theta: np.ndarray, etc_day: float, rain_day: float) -> list:
        """dθ/dt for each layer [m³/(m³·day)]."""
        theta = np.clip(theta, self.p.theta_r + 1e-6, self.p.theta_s - 1e-6)

        # Fluxes: q01 = layer0→1, q12 = layer1→2, q_bot = drainage from layer2
        dz_01 = 0.5 * (self._DZ[0] + self._DZ[1])
        dz_12 = 0.5 * (self._DZ[1] + self._DZ[2])
        q01 = self._flux(theta[0], theta[1], dz_01)
        q12 = self._flux(theta[1], theta[2], dz_12)
        q_bot = self.conductivity.k_from_theta(theta[2])  # free drainage

        # Rain infiltration into layer 0 (distributed over day)
        rain_flux = rain_day / self._DZ[0]

        # Sink: actual ETc distributed over root zone with Feddes stress
        def root_uptake(i: int) -> float:
            se = (theta[i] - self.p.theta_r) / (self.p.theta_s - self.p.theta_r)
            ks_stress = max(0.0, min(1.0, (theta[i] - self.p.pwp) / (self.p.raw - self.p.pwp)))
            et_i = etc_day * self._ROOT_FRAC[i] / self._DZ[i]
            return et_i * ks_stress

        s0, s1, s2 = root_uptake(0), root_uptake(1), root_uptake(2)

        dtheta0 = (rain_flux - q01) / self._DZ[0] - s0
        dtheta1 = (q01 - q12) / self._DZ[1] - s1
        dtheta2 = (q12 - q_bot) / self._DZ[2] - s2

        return [dtheta0, dtheta1, dtheta2]

    def step(self, dt: float, inputs: dict[str, float]) -> dict[str, float]:
        """
        Advance the soil column by dt seconds.

        Inputs expected:
            etc_mm_day  : actual crop evapotranspiration [mm/day]
            rain_mm_day : rainfall rate [mm/day]
            irrigation_mm_day: irrigation input [mm/day] (0 if no irrigation)
        """
        dt_day = dt / 86400.0  # seconds → days
        etc_day  = inputs.get("etc_mm_day", 5.0) / 1000.0   # mm/day → m/day
        rain_day = (inputs.get("rain_mm_day", 0.0) + inputs.get("irrigation_mm_day", 0.0)) / 1000.0

        try:
            sol = solve_ivp(
                fun=lambda t, y: self._derivatives(t, y, etc_day, rain_day),
                t_span=(0.0, max(dt_day, 1e-8)),
                y0=self.state,
                method="RK45",
                max_step=dt_day,
            )
            new_state = sol.y[:, -1]
        except Exception:
            new_state = self.state.copy()

        # Clamp to physical bounds
        self.state = np.clip(new_state, self.p.theta_r, self.p.theta_s)

        # Compute hydraulic properties
        psi_kpa = [
            self.retention.psi_kpa_from_theta(self.state[i])
            for i in range(3)
        ]
        drainage_flux = self.conductivity.k_from_theta(self.state[2]) * 1000.0  # m/day → mm/day

        return {
            "sim_vwc_10": round(float(self.state[0]), 4),
            "sim_vwc_30": round(float(self.state[1]), 4),
            "sim_vwc_60": round(float(self.state[2]), 4),
            "sim_potential_kpa": round(float(np.mean(psi_kpa[:2])), 2),
            "sim_drainage_mm_day": round(float(drainage_flux), 3),
            "sim_et_daily": round(etc_day * 1000.0, 3),  # m/day → mm/day
        }

    def reset(self) -> None:
        self.state = self._initial_state.copy()

    def get_vwc(self) -> tuple[float, float, float]:
        return tuple(float(x) for x in self.state)  # type: ignore[return-value]

    def get_potential_kpa(self) -> list[float]:
        return [self.retention.psi_kpa_from_theta(self.state[i]) for i in range(3)]


# ---------------------------------------------------------------------------
# ET Depletion Surrogate (SKLearn stub → trains from real data)
# ---------------------------------------------------------------------------

class ETDepletionSurrogate:
    """
    Scikit-learn surrogate for ET-driven VWC depletion prediction.

    Design spec: SKLearnSurrogate / ONNXSurrogate outputting ET_daily (mm/day).
    Inputs: T_air, u_wind, R_net, VPD, LAI (from Weather DT + PDT optional).

    Initially uses Penman-Monteith formula directly (physics fallback).
    When ``fit()`` is called with real data it replaces the formula with a
    gradient-boosting regressor and can be exported to ONNX.
    """

    model_name = "et_depletion_surrogate"
    model_type = ModelType.SURROGATE

    def __init__(self, kc: float = 1.10):
        self.kc = kc   # crop factor for maize V6-V8 (FAO-56)
        self._model = None  # SKLearn model, None → use PM formula
        self._gamma = 0.067  # kPa/°C

    def _pm_eto(self, t_air: float, vpd: float, u2: float, r_net: float) -> float:
        """Penman-Monteith reference ET (FAO-56)."""
        delta = 4098 * 0.6108 * math.exp(17.27 * t_air / (t_air + 237.3)) / (t_air + 237.3) ** 2
        num = 0.408 * delta * r_net + self._gamma * 900 / (t_air + 273) * u2 * vpd
        den = delta + self._gamma * (1 + 0.34 * u2)
        return max(0.0, num / den)

    def step(self, dt: float, inputs: dict[str, float]) -> dict[str, float]:
        t_air = inputs.get("t_air_c", 35.0)
        vpd   = inputs.get("vpd_kpa", 3.5)
        u2    = inputs.get("u_wind_ms", 1.5)
        r_net = inputs.get("r_net_mj", 18.0)
        lai   = inputs.get("leaf_area_index", 2.0)

        if self._model is None:
            eto = self._pm_eto(t_air, vpd, u2, r_net)
        else:
            x = np.array([[t_air, vpd, u2, r_net, lai]])
            eto = float(self._model.predict(x)[0])

        # Adjust Kc for partial ground cover (LAI-based Kcb)
        kc_adj = self.kc * min(1.0, lai / 3.0)
        etc = eto * kc_adj

        return {
            "sim_eto_mm_day": round(eto, 3),
            "sim_etc_mm_day": round(etc, 3),
        }

    def reset(self) -> None:
        pass

    def fit(self, X: Any, y: Any) -> None:
        """Train surrogate from sensor-ET pairs. X: feature matrix, y: ET values."""
        try:
            from sklearn.ensemble import GradientBoostingRegressor
            self._model = GradientBoostingRegressor(n_estimators=100, max_depth=3)
            self._model.fit(X, y)
        except ImportError:
            pass


# ---------------------------------------------------------------------------
# Prophet Moisture Forecast
# ---------------------------------------------------------------------------

class ProphetMoistureForecast:
    """
    24-hour VWC forecast using Facebook Prophet (or linear extrapolation fallback).

    Design spec: ProphetForecaster — daily refit + 15-min inference.
    """

    model_name = "prophet_moisture_forecast"
    model_type = ModelType.SURROGATE

    def __init__(self, asset_id: str = "sdt_water_depletion_001"):
        self.asset_id = asset_id
        self._model = None
        self._last_vwc: list[float] = []
        self._last_rate: float = 0.0

    def step(self, dt: float, inputs: dict[str, float]) -> dict[str, float]:
        vwc_now = inputs.get("vwc_10cm", 0.06)
        rate = inputs.get("depletion_rate_avg", 0.0)

        # Linear extrapolation fallback (Prophet not fitted yet or not installed)
        if self._model is None:
            # Simple linear forecast using depletion rate
            vwc_6h  = max(0.0, vwc_now - rate * 6.0)
            vwc_24h = max(0.0, vwc_now - rate * 24.0)
            ci_width = abs(rate) * 3.0 + 0.003   # uncertainty grows with time
        else:
            vwc_6h = vwc_24h = vwc_now  # placeholder
            ci_width = 0.005

        return {
            "sim_vwc_forecast_6h":  round(vwc_6h, 4),
            "sim_vwc_forecast_24h": round(vwc_24h, 4),
            "sim_forecast_ci_lower": round(vwc_24h - ci_width, 4),
            "sim_forecast_ci_upper": round(vwc_24h + ci_width, 4),
        }

    def reset(self) -> None:
        self._last_vwc = []

    def fit(self, vwc_series: list[tuple[float, float]]) -> None:
        """Fit Prophet model. vwc_series: list of (timestamp, vwc) tuples."""
        try:
            from prophet import Prophet
            import pandas as pd
            df = pd.DataFrame(vwc_series, columns=["ds", "y"])
            df["ds"] = pd.to_datetime(df["ds"], unit="s")
            m = Prophet(changepoint_prior_scale=0.05)
            m.fit(df)
            self._model = m
        except ImportError:
            pass


# ---------------------------------------------------------------------------
# Model registry for ModelRunner
# ---------------------------------------------------------------------------

def build_sdt_models(params: SoilParams = SANDY_LOAM_PARAMS) -> dict:
    """Build and return all SDT simulation models keyed by name."""
    return {
        "richards":     RichardsEquationModel(params),
        "et_surrogate": ETDepletionSurrogate(kc=1.10),
        "forecast":     ProphetMoistureForecast(),
    }
