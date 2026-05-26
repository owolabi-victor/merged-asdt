"""PDT Plant Sensor Simulator — physics-grounded drought stress simulation.

Drives PressureVolumeModel, CWSIModel, TranspirationModel, and SapFlowModel
forward in time and publishes synthetic sensor readings matching what field
sensors would measure on maize at V6-V8 stage under progressive drought stress.

Physical sensor connection:
    Set ``use_physical_sensors=True``. The simulator reads from MQTT topics
    produced by real sensors (leaf thermocouple, sap flow probes, pressure bomb).

Simulated sensor types and noise:
    Leaf thermocouple (type-T): ±0.5°C (Apogee SI-121)
    Stomatal conductance meter (LI-600): ±15 mmol/m²/s
    Sap flow (Granier TDP): ±8% (Smith & Allen 1996)
    Pressure bomb (PMS-1000): ±0.05 MPa
    Handheld SPAD (chlorophyll proxy): ±2 SPAD
"""

from __future__ import annotations

import logging
import math
import random
from typing import TYPE_CHECKING

from cross_domain.pdt.models import (
    PressureVolumeModel,
    CWSIModel,
    TranspirationModel,
    StomatalConductanceModel,
    SapFlowModel,
    AquaCropYieldModel,
    MAIZE_V6V8,
)

if TYPE_CHECKING:
    from dt_forge.core.config import TwinConfig
    from cross_domain.shared.weather_simulator import WeatherSimulator

log = logging.getLogger(__name__)

# Sensor noise standard deviations (1-sigma, from instrument specs)
_TEMP_NOISE     = 0.5     # leaf thermocouple ±0.5°C
_GS_NOISE       = 15.0    # LI-600 porometer ±15 mmol/m²/s
_SAP_NOISE_FRAC = 0.08    # Granier TDP ±8%
_PSI_NOISE      = 0.05    # pressure bomb ±0.05 MPa
_CWSI_NOISE     = 0.02    # CWSI index ±0.02


class PlantSensorSimulator:
    """
    Physics-grounded plant stress sensor simulator.

    Integrates soil VWC inputs with plant hydraulics to compute:
    - Leaf water potential (ψ_leaf) via P-V model
    - CWSI via canopy temperature differential
    - Stomatal conductance (gs) via Ball-Berry drought response
    - Sap flow [L/hr/plant] via Granier analog
    - Relative water content (RWC) from turgor model
    - Cumulative yield penalty via AquaCrop

    Parameters
    ----------
    config : TwinConfig
    publish_interval_s : float
        Sensor reading interval (default 1800s = 30 min).
    use_physical_sensors : bool
        When True, reads from MQTT instead of physics models.
    weather : WeatherSimulator
        Shared weather generator (arid ~25°N, same as SDT).
    """

    def __init__(
        self,
        config: "TwinConfig",
        publish_interval_s: float = 1800.0,
        use_physical_sensors: bool = False,
        weather: "WeatherSimulator | None" = None,
    ):
        self._config   = config
        self._interval = publish_interval_s
        self._use_physical = use_physical_sensors
        self._rng      = random.Random()

        self._pv_model    = PressureVolumeModel(MAIZE_V6V8)
        self._cwsi_model  = CWSIModel()
        self._gs_model    = StomatalConductanceModel(MAIZE_V6V8)
        self._transp_model = TranspirationModel(MAIZE_V6V8)
        self._sap_model   = SapFlowModel()
        self._yield_model = AquaCropYieldModel(MAIZE_V6V8)

        # Weather
        if weather is None:
            from cross_domain.shared.weather_simulator import WeatherSimulator
            weather = WeatherSimulator()
        self._weather = weather

        # External soil inputs (set by BPDT boundary conditions).
        # Start near field capacity to match the SDT initial state so both
        # twins begin in an unstressed condition after a reset/fresh start.
        self._vwc_root_zone: float = 0.09   # near FC (0.10), well above RAW (0.075)
        self._root_zone_potential_mpa: float = -0.10  # light tension, non-stressed

        # Internal state
        self._rwc: float      = 0.98        # relative water content [0-1] — fully hydrated
        self._stress_days: float = 0.0      # cumulative stress duration [days]
        self._daily_eta_fractions: list[float] = []
        self._last_readings: dict[str, float] = {}

    # ------------------------------------------------------------------
    # External inputs from SDT (via BPDT BoundaryConditions)
    # ------------------------------------------------------------------

    def set_soil_inputs(
        self,
        vwc_10cm: float | None = None,
        vwc_30cm: float | None = None,
        root_zone_potential_kpa: float | None = None,
    ) -> None:
        """Update soil moisture inputs from SDT boundary conditions."""
        if vwc_10cm is not None and vwc_30cm is not None:
            self._vwc_root_zone = 0.6 * vwc_10cm + 0.4 * vwc_30cm
        if root_zone_potential_kpa is not None:
            self._root_zone_potential_mpa = root_zone_potential_kpa / 1000.0

    # ------------------------------------------------------------------
    # Step
    # ------------------------------------------------------------------

    def step_once(self) -> dict[str, float]:
        """Synchronous single step — returns sensor reading dict."""
        if self._use_physical:
            return self._last_readings

        weather = self._weather.step(self._interval / 3600.0)
        readings = self._step_physics(weather)
        self._last_readings = readings
        return readings

    def get_latest(self) -> dict[str, float]:
        return dict(self._last_readings)

    # ------------------------------------------------------------------
    # Physics integration
    # ------------------------------------------------------------------

    def _step_physics(self, weather) -> dict[str, float]:
        """Compute plant state from VWC and weather, return sensor readings."""
        dt_days = self._interval / 86400.0

        # 1. RWC from root-zone VWC (linear mapping: FC→1.0, PWP→0.80)
        _FC, _PWP = 0.100, 0.050
        rwc_from_soil = 0.80 + 0.20 * max(0.0, min(1.0,
            (self._vwc_root_zone - _PWP) / max(_FC - _PWP, 0.001)
        ))
        # Lag: RWC adjusts toward soil supply with τ = 1.5 hr
        tau = max(1.5 / (self._interval / 3600.0), 0.05)
        self._rwc = self._rwc + (rwc_from_soil - self._rwc) / tau
        self._rwc = max(0.70, min(1.0, self._rwc))

        # 2. Leaf water potential (P-V model)
        psi_leaf = self._pv_model.leaf_water_potential(self._rwc)

        # 3. Stomatal conductance
        gs_mmol = self._gs_model.conductance_mmol(
            self._rwc, weather.vpd_kpa, weather.t_air_c
        )

        # 4. Transpiration
        transp_mm_day = self._transp_model.transpiration_mm_day(
            gs_mmol, weather.vpd_kpa, weather.t_air_c
        )

        # 5. Sap flow
        sap_flow_L_hr = self._sap_model.sap_flow_L_hr(transp_mm_day)

        # 6. CWSI — canopy temp from gs deficit
        gs_frac   = gs_mmol / MAIZE_V6V8.gs_nominal_mmol
        cwsi_true = max(0.0, min(1.0, 1.0 - gs_frac))
        t_canopy  = self._cwsi_model.canopy_temp_from_stress(
            weather.t_air_c, weather.vpd_kpa, cwsi_true
        )
        cwsi_meas = self._cwsi_model.compute(t_canopy, weather.t_air_c, weather.vpd_kpa)

        # 7. Stress accumulation
        if self._rwc < MAIZE_V6V8.rwc_tlp:
            self._stress_days += dt_days
        else:
            self._stress_days = max(0.0, self._stress_days - dt_days * 0.1)

        # ETa/ETc fraction for yield model
        etc_max = self._transp_model.transpiration_mm_day(
            MAIZE_V6V8.gs_nominal_mmol, weather.vpd_kpa, weather.t_air_c
        )
        eta_frac = min(1.0, transp_mm_day / max(etc_max, 0.1))
        self._daily_eta_fractions.append(eta_frac)
        if len(self._daily_eta_fractions) > 120:   # keep ~60 days rolling
            self._daily_eta_fractions.pop(0)

        yield_penalty = self._yield_model.cumulative_penalty_pct(
            self._daily_eta_fractions
        )

        def _noisy(val: float, std: float, lo: float, hi: float) -> float:
            return round(min(hi, max(lo, val + self._rng.gauss(0, std))), 4)

        # Sap flow noise is proportional (8%)
        sap_noisy = _noisy(sap_flow_L_hr, sap_flow_L_hr * _SAP_NOISE_FRAC, 0.0, 5.0)

        return {
            # CWSI [0=unstressed, 1=fully stressed]
            "cwsi": _noisy(cwsi_meas, _CWSI_NOISE, 0.0, 1.0),
            # Leaf water potential [MPa]
            "leaf_water_potential_mpa": _noisy(psi_leaf, _PSI_NOISE, -5.0, 0.0),
            # Stomatal conductance [mmol/m²/s]
            "stomatal_conductance_mmol": _noisy(gs_mmol, _GS_NOISE, 0.0, 600.0),
            # Canopy-air temperature differential [°C]
            "canopy_temp_c": _noisy(t_canopy, _TEMP_NOISE, 10.0, 60.0),
            "canopy_air_delta_c": round(t_canopy - weather.t_air_c, 3),
            # Sap flow [L/hr/plant]
            "sap_flow_L_hr": sap_noisy,
            # Relative water content [0-1]
            "relative_water_content": round(self._rwc, 4),
            # Cumulative yield penalty [%]
            "yield_penalty_pct": round(yield_penalty, 2),
            # Stress duration [days]
            "stress_duration_days": round(self._stress_days, 1),
            # ETa fraction
            "eta_fraction": round(eta_frac, 4),
            # Weather pass-through (for MAS agents)
            "t_air_c": round(weather.t_air_c, 2),
            "vpd_kpa":  round(weather.vpd_kpa, 3),
            # Soil inputs (for reference)
            "vwc_root_zone": round(self._vwc_root_zone, 4),
        }
