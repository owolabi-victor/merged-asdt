"""WeatherSimulator — physics-grounded synthetic weather generator for an arid region.

Produces consistent weather streams shared by all three twins (SDT, PDT, BPDT).
Based on FAO-56 climatology for a semi-arid to arid environment.

Typical output ranges:
  T_air       : 28 – 42 °C (peak midday ~40 °C)
  VPD         : 2.0 – 5.5 kPa
  u_wind      : 0.5 – 3.5 m/s
  R_net       : 10 – 25 MJ/m²/day (0 at night)
  ETo         : 7 – 12 mm/day (FAO-56 Penman-Monteith)
  rain_prob   : 0.02 – 0.05 (arid: rare rainfall events)

To connect real sensors later: subclass WeatherSimulator and override
``_observe()`` to pull from an MQTT topic or weather station API.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class WeatherState:
    """Snapshot of current weather conditions."""

    timestamp: float = field(default_factory=time.time)
    t_air_c: float = 35.0          # air temperature [°C]
    t_air_min_c: float = 22.0      # daily min [°C]
    t_air_max_c: float = 42.0      # daily max [°C]
    rh_pct: float = 25.0           # relative humidity [%]
    vpd_kpa: float = 3.5           # vapour-pressure deficit [kPa]
    u_wind_ms: float = 1.5         # wind speed at 2 m [m/s]
    r_net_mj: float = 18.0         # net radiation [MJ/m²/day]
    r_solar_mj: float = 22.0       # incoming solar [MJ/m²/day]
    eto_mm: float = 9.0            # reference ET (FAO-56 P-M) [mm/day]
    rain_mm: float = 0.0           # rainfall this time-step [mm]
    rain_prob: float = 0.03        # probability of rain today [-]
    leaf_area_index: float = 2.0   # LAI for ET crop factor
    cloud_cover: float = 0.1       # fraction cloud cover [-]


class WeatherSimulator:
    """
    Physics-grounded synthetic weather generator for an arid agricultural region.

    Weather follows a realistic diurnal cycle driven by solar position.
    Adds structured day-to-day variability with random perturbations.
    Monthly means match FAO-56 climatology for an arid environment (~latitude 25°N).

    Parameters
    ----------
    latitude_deg : float
        Field latitude in degrees (affects solar geometry).
    elevation_m : float
        Field elevation [m] (affects atmospheric pressure and γ).
    seed : int | None
        Random seed for reproducibility.
    """

    # Monthly mean daily max temperature [°C] for arid region (~25°N latitude)
    _T_MAX_MONTHLY = [25, 28, 32, 37, 42, 43, 42, 41, 39, 35, 30, 26]
    _T_MIN_MONTHLY = [10, 12, 16, 20, 25, 27, 27, 26, 24, 20, 15, 11]
    _RH_MONTHLY    = [35, 32, 28, 22, 18, 16, 18, 20, 23, 28, 33, 37]  # %
    _WIND_MONTHLY  = [1.5, 1.7, 2.0, 2.2, 2.0, 1.8, 1.7, 1.6, 1.5, 1.4, 1.3, 1.4]
    # Monthly rain probability (arid: ~3-8 rainy days/month)
    _RAIN_PROB     = [0.04, 0.03, 0.03, 0.02, 0.01, 0.01, 0.02, 0.03, 0.03, 0.04, 0.04, 0.05]

    def __init__(
        self,
        latitude_deg: float = 25.0,
        elevation_m: float = 200.0,
        seed: int | None = 42,
    ):
        self._lat_rad = math.radians(latitude_deg)
        self._elev = elevation_m
        self._gamma = 0.067 * (101.3 * ((293 - 0.0065 * elevation_m) / 293) ** 5.26 / 101.3)
        self._rng = random.Random(seed)
        self._state = WeatherState()
        self._day_of_year = datetime.now(tz=timezone.utc).timetuple().tm_yday
        self._daily_rain_event = False
        self._update_daily_means()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def step(self, dt_hours: float = 0.25) -> WeatherState:
        """
        Advance simulation by dt_hours and return the new weather state.

        Call this every 15 or 30 minutes to match the twin refresh rate.
        """
        now = datetime.now(tz=timezone.utc)
        hour_of_day = now.hour + now.minute / 60.0

        # New day: update means and decide daily rain event
        if hour_of_day < dt_hours:
            self._day_of_year = (self._day_of_year % 365) + 1
            self._update_daily_means()
            month = self._day_to_month(self._day_of_year)
            rain_p = self._RAIN_PROB[month - 1]
            self._daily_rain_event = self._rng.random() < rain_p

        t_air = self._diurnal_temp(hour_of_day)
        rh = self._diurnal_rh(hour_of_day, t_air)
        vpd = self._compute_vpd(t_air, rh)
        u_wind = max(0.1, self._mean_wind + self._rng.gauss(0, 0.2))
        r_net = self._solar_net_radiation(self._day_of_year, hour_of_day)
        rain = self._sample_rain(hour_of_day) if self._daily_rain_event else 0.0
        eto = self._penman_monteith(t_air, vpd, u_wind, r_net)

        self._state = WeatherState(
            timestamp=time.time(),
            t_air_c=round(t_air, 2),
            t_air_min_c=round(self._mean_t_min, 2),
            t_air_max_c=round(self._mean_t_max, 2),
            rh_pct=round(rh, 1),
            vpd_kpa=round(vpd, 3),
            u_wind_ms=round(u_wind, 2),
            r_net_mj=round(r_net, 2),
            r_solar_mj=round(r_net * 1.25, 2),
            eto_mm=round(eto, 3),
            rain_mm=round(rain, 2),
            rain_prob=self._RAIN_PROB[self._day_to_month(self._day_of_year) - 1],
            cloud_cover=max(0.0, 0.05 + self._rng.gauss(0, 0.03)),
        )
        return self._state

    @property
    def current(self) -> WeatherState:
        return self._state

    def as_dict(self) -> dict:
        s = self._state
        return {
            "t_air_c":      s.t_air_c,
            "t_air_min_c":  s.t_air_min_c,
            "t_air_max_c":  s.t_air_max_c,
            "rh_pct":       s.rh_pct,
            "vpd_kpa":      s.vpd_kpa,
            "u_wind_ms":    s.u_wind_ms,
            "r_net_mj":     s.r_net_mj,
            "eto_mm":       s.eto_mm,
            "rain_mm":      s.rain_mm,
            "rain_prob":    s.rain_prob,
        }

    # ------------------------------------------------------------------
    # Internal physics helpers
    # ------------------------------------------------------------------

    def _update_daily_means(self) -> None:
        month = self._day_to_month(self._day_of_year)
        self._mean_t_max = self._T_MAX_MONTHLY[month - 1] + self._rng.gauss(0, 1.5)
        self._mean_t_min = self._T_MIN_MONTHLY[month - 1] + self._rng.gauss(0, 1.5)
        self._mean_rh    = self._RH_MONTHLY[month - 1]    + self._rng.gauss(0, 2.0)
        self._mean_wind  = self._WIND_MONTHLY[month - 1]

    def _diurnal_temp(self, hour: float) -> float:
        """Sinusoidal diurnal temperature with peak at 14:00."""
        amplitude = (self._mean_t_max - self._mean_t_min) / 2.0
        t_mean = (self._mean_t_max + self._mean_t_min) / 2.0
        # Peak at hour=14, trough at hour=4  →  phase = (hour-14)*π/12
        phase = (hour - 14.0) * math.pi / 12.0
        return t_mean + amplitude * math.cos(phase) + self._rng.gauss(0, 0.5)

    def _diurnal_rh(self, hour: float, t_air: float) -> float:
        """RH inversely related to temperature (higher at night)."""
        t_frac = (t_air - self._mean_t_min) / max(1.0, self._mean_t_max - self._mean_t_min)
        rh = self._mean_rh * (1.0 - 0.3 * t_frac) + self._rng.gauss(0, 1.5)
        return max(5.0, min(95.0, rh))

    def _compute_vpd(self, t_air: float, rh: float) -> float:
        """Saturation vapour pressure deficit [kPa]."""
        es = 0.6108 * math.exp(17.27 * t_air / (t_air + 237.3))
        ea = es * rh / 100.0
        return max(0.0, es - ea)

    def _solar_net_radiation(self, doy: int, hour: float) -> float:
        """Approximate net radiation [MJ/m²/day equivalent at current hour]."""
        # Daily extraterrestrial radiation (Ra) using FAO-56 formula
        dr = 1 + 0.033 * math.cos(2 * math.pi * doy / 365)
        decl = 0.409 * math.sin(2 * math.pi * doy / 365 - 1.39)
        ws = math.acos(-math.tan(self._lat_rad) * math.tan(decl))
        Ra = (24 * 60 / math.pi) * 0.0820 * dr * (
            ws * math.sin(self._lat_rad) * math.sin(decl)
            + math.cos(self._lat_rad) * math.cos(decl) * math.sin(ws)
        )  # MJ/m²/day
        # Fraction of daytime: simple sinusoidal approximation
        sunrise = 12 - ws * 12 / math.pi
        sunset  = 12 + ws * 12 / math.pi
        if hour < sunrise or hour > sunset:
            return 0.0
        sun_frac = math.sin(math.pi * (hour - sunrise) / (sunset - sunrise))
        Rs = Ra * 0.75 * sun_frac  # assuming Angstrom coefficients a=0.25, b=0.50
        Rns = 0.77 * Rs            # net short-wave (albedo=0.23)
        # Approximate net long-wave (simplified Brunt equation)
        Rnl = 4.903e-9 * ((273.3 + self._mean_t_max) ** 4 + (273.3 + self._mean_t_min) ** 4) / 2 * 0.40
        return max(0.0, Rns - Rnl)

    def _penman_monteith(self, t_air: float, vpd: float, u2: float, r_net: float) -> float:
        """FAO-56 Penman-Monteith reference ET [mm/day]."""
        delta = 4098 * 0.6108 * math.exp(17.27 * t_air / (t_air + 237.3)) / (t_air + 237.3) ** 2
        gamma = self._gamma
        numerator = (0.408 * delta * r_net) + (gamma * 900 / (t_air + 273) * u2 * vpd)
        denominator = delta + gamma * (1 + 0.34 * u2)
        eto = numerator / denominator if denominator > 0 else 0.0
        return max(0.0, eto)

    def _sample_rain(self, hour: float) -> float:
        """Generate a rain event: heavy burst possible in arid regions."""
        if 14 < hour < 18:  # afternoon convective storms typical in arid regions
            return self._rng.uniform(5, 30) * self._rng.random()
        return 0.0

    @staticmethod
    def _day_to_month(doy: int) -> int:
        month_days = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        total = 0
        for m, days in enumerate(month_days, 1):
            total += days
            if doy <= total:
                return m
        return 12
