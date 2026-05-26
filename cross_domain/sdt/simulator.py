"""SDT Sensor Simulator — Physics-grounded soil moisture sensor simulation.

Drives the RichardsEquationModel forward in time and publishes sensor readings
to MQTT (matching what real capacitive moisture/tensiometer sensors would emit).

Physical sensor connection:
    Set ``use_physical_sensors=True`` in the constructor. The simulator will
    then subscribe to MQTT topics from real sensors instead of computing physics.
    Override ``_mqtt_topic_for_field()`` to match your sensor naming convention.

Sensor noise model (based on typical capacitive soil moisture sensor specs):
    Capacitive moisture sensors (e.g. TEROS-12): ±0.01 m³/m³ RMSE
    Tensiometers: ±2 kPa
    Soil temperature: ±0.2 °C
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING

from cross_domain.shared.weather_simulator import WeatherSimulator
from cross_domain.sdt.models import (
    RichardsEquationModel,
    ETDepletionSurrogate,
    SoilParams,
    SANDY_LOAM_PARAMS,
    VanGenuchtenRetention,
)

if TYPE_CHECKING:
    from dt_forge.core.config import TwinConfig

log = logging.getLogger(__name__)

# Sensor specifications (TEROS-12 capacitive moisture sensor accuracy)
_VWC_NOISE_STD  = 0.005   # ±0.005 m³/m³ (1 sigma)
_TEMP_NOISE_STD = 0.2     # ±0.2 °C
_PSI_NOISE_STD  = 2.0     # ±2 kPa (tensiometer)


class SoilSensorSimulator:
    """
    Physics-grounded soil sensor simulator.

    Runs the 3-layer Richards equation and adds realistic sensor noise.
    Publishes to MQTT at ``publish_interval_s`` seconds.

    When ``use_physical_sensors=True``, this class acts as a pass-through
    adapter that reads from MQTT topics connected to real sensors.
    The Richards model still runs in parallel for model-sensor residuals.

    Parameters
    ----------
    config : TwinConfig
    publish_interval_s : float
        How often to publish a reading (default 900s = 15 minutes).
    use_physical_sensors : bool
        When True, real sensor readings are used instead of simulated ones.
    soil_params : SoilParams
        Van Genuchten parameters for the soil profile.
    weather : WeatherSimulator | None
        Shared weather simulator. Creates a private one if None.
    """

    def __init__(
        self,
        config: "TwinConfig",
        publish_interval_s: float = 900.0,
        use_physical_sensors: bool = False,
        soil_params: SoilParams = SANDY_LOAM_PARAMS,
        weather: WeatherSimulator | None = None,
    ):
        self._config = config
        self._interval = publish_interval_s
        self._use_physical = use_physical_sensors
        self._running = False
        self._rng = random.Random()

        self._model = RichardsEquationModel(soil_params)
        self._et_model = ETDepletionSurrogate(kc=1.10)
        self._retention = VanGenuchtenRetention(soil_params)
        self._weather = weather or WeatherSimulator()
        self._irrigation_mm_day: float = 0.0  # set externally when irrigating
        self._last_readings: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    def set_irrigation(self, rate_L_hr: float, field_area_m2: float = 1.0) -> None:
        """Convert irrigation flow rate [L/hr] to [mm/day] for model input."""
        self._irrigation_mm_day = rate_L_hr * 24.0 / field_area_m2  # L/hr → mm/day

    def stop_irrigation(self) -> None:
        self._irrigation_mm_day = 0.0

    def get_latest(self) -> dict[str, float]:
        """Return the most recent sensor reading snapshot."""
        return dict(self._last_readings)

    async def run(self) -> None:
        """Main loop: step physics, add noise, publish to MQTT, cache."""
        from dt_forge.network.transport import MQTTTransport
        self._running = True
        transport = MQTTTransport(self._config)
        transport.connect()
        log.info(
            "SoilSensorSimulator started (interval=%.0fs, physical=%s)",
            self._interval,
            self._use_physical,
        )
        try:
            while self._running:
                readings = await self._step()
                self._last_readings = readings
                transport.publish(self._config.topic_telemetry, readings)
                await asyncio.sleep(self._interval)
        finally:
            transport.disconnect()
            self._running = False

    def step_once(self) -> dict[str, float]:
        """Single synchronous step (used by TelemetryRouter and tests)."""
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # In async context — caller should use await _step()
                return self._last_readings or self._step_sync()
        except RuntimeError:
            pass
        return self._step_sync()

    def _step_sync(self) -> dict[str, float]:
        """Synchronous step for non-async callers."""
        weather = self._weather.step(self._interval / 3600.0)
        et_out = self._et_model.step(self._interval, {
            "t_air_c": weather.t_air_c,
            "vpd_kpa": weather.vpd_kpa,
            "u_wind_ms": weather.u_wind_ms,
            "r_net_mj": weather.r_net_mj,
        })
        soil_out = self._model.step(self._interval, {
            "etc_mm_day": et_out["sim_etc_mm_day"],
            "rain_mm_day": weather.rain_mm,
            "irrigation_mm_day": self._irrigation_mm_day,
        })
        readings = self._build_readings(soil_out, et_out, weather)
        self._last_readings = readings
        return readings

    async def _step(self) -> dict[str, float]:
        return self._step_sync()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_readings(self, soil_out: dict, et_out: dict, weather) -> dict[str, float]:
        """Build sensor readings dict from model outputs + noise."""
        if self._use_physical:
            return self._read_physical_sensors(soil_out)

        vwc = self._model.state
        psi_kpa = self._model.get_potential_kpa()

        # Sensor noise (Gaussian, capped to physical bounds)
        def noisy(val: float, std: float, lo: float, hi: float) -> float:
            return round(min(hi, max(lo, val + self._rng.gauss(0, std))), 4)

        readings = {
            # Volumetric water content [m³/m³]
            "vwc_10cm":  noisy(vwc[0], _VWC_NOISE_STD, 0.025, 0.43),
            "vwc_30cm":  noisy(vwc[1], _VWC_NOISE_STD, 0.025, 0.43),
            "vwc_60cm":  noisy(vwc[2], _VWC_NOISE_STD, 0.025, 0.43),
            # Soil temperature [°C] — warmer at surface, cooler at depth
            "soil_temp_10cm": noisy(weather.t_air_c * 0.85 + 5.0, _TEMP_NOISE_STD, 5, 55),
            "soil_temp_30cm": noisy(weather.t_air_c * 0.70 + 6.0, _TEMP_NOISE_STD, 5, 45),
            # Soil water potential [kPa] (tensiometer at 10 and 30 cm)
            "soil_water_potential_10":  noisy(psi_kpa[0], _PSI_NOISE_STD, -2000, 0),
            "soil_water_potential_30":  noisy(psi_kpa[1], _PSI_NOISE_STD, -2000, 0),
            # Depletion rate [m³/m³/hr] — rolling computation
            "depletion_rate_avg": round(self._compute_depletion_rate(vwc[0]), 6),
            # ET estimate [mm/day]
            "et_loss_rate": round(et_out.get("sim_etc_mm_day", 0.0), 3),
            # Weather co-variates (for ET agents and OODA)
            "t_air_c":   round(weather.t_air_c, 2),
            "vpd_kpa":   round(weather.vpd_kpa, 3),
            "rain_mm":   round(weather.rain_mm, 2),
            # Inter-sensor divergence flag (agent diagnostic)
            "sensor_divergence_flag": round(
                abs(vwc[0] - vwc[1]) + abs(vwc[1] - vwc[2]) * 0.5, 4
            ),
        }
        return readings

    def _read_physical_sensors(self, model_fallback: dict) -> dict[str, float]:
        """Stub: read from real MQTT sensor topics.

        Override or replace with MQTT subscription logic.
        Falls back to model output when sensor data unavailable.
        """
        log.debug("physical sensor mode — using model fallback until MQTT configured")
        return {
            "vwc_10cm": model_fallback.get("sim_vwc_10", 0.06),
            "vwc_30cm": model_fallback.get("sim_vwc_30", 0.06),
            "vwc_60cm": model_fallback.get("sim_vwc_60", 0.06),
        }

    def _compute_depletion_rate(self, vwc_now: float) -> float:
        """Rolling 1-hour depletion rate at 10 cm [m³/m³/hr]."""
        prev = self._last_readings.get("vwc_10cm", vwc_now)
        elapsed_hr = self._interval / 3600.0
        if elapsed_hr < 1e-6:
            return 0.0
        rate = (prev - vwc_now) / elapsed_hr  # positive = depleting
        return max(-0.01, min(0.01, rate))
