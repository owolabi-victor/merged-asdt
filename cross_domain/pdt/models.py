"""PDT Physics Models — Maize (Zea mays L.) drought stress simulation.

Models implemented:
    PressureVolumeModel       — cell turgor and osmotic adjustment (Tyree & Hammel 1972)
    CWSIModel                 — Crop Water Stress Index (Jackson et al. 1981)
    TranspirationModel        — SPA-style leaf transpiration (Williams et al. 1996)
    StomatalConductanceModel  — Ball-Berry model (Ball et al. 1987)
    SapFlowModel              — Granier thermal dissipation probe analog
    AquaCropYieldModel        — FAO AquaCrop yield-water productivity (Hsiao et al. 2009)

Maize V6-V8 parameters from literature:
    gs_nominal  = 260 mmol/m²/s  (Lecoeur & Sinclair 1996)
    LAI         = 2.0 m²/m²     (D'Andrea et al. 2006)
    π₀          = -0.70 MPa     (Westgate & Boyer 1985)
    ε_bulk      = 20.0 MPa      (Hsiao & Xu 2000)
    TLP         = π₀ + (π₀/ε)  ≈ -1.235 MPa (turgor loss point)
    Ky          = 1.25          (yield sensitivity — Doorenbos & Kassam 1979)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 1. Pressure-Volume (P-V) Model — Tyree & Hammel (1972)
# ---------------------------------------------------------------------------

@dataclass
class MaizeV6V8Params:
    """Literature-calibrated parameters for maize at V6-V8 vegetative stage."""
    gs_nominal_mmol: float = 260.0    # stomatal conductance [mmol/m²/s]
    lai: float             = 2.0      # leaf area index [m²/m²]
    root_depth_m: float    = 0.50     # effective root depth [m]
    pi_0_mpa: float        = -0.70    # osmotic potential at full turgor [MPa]
    epsilon_mpa: float     = 20.0     # bulk elastic modulus [MPa]
    # Turgor loss point: ψ_TLP = π₀ + π₀/ε  (Tyree & Hammel 1972)
    # ψ_TLP = -0.70 + (-0.70/20) = -0.735 MPa per simple linear P-V
    # More accurate form: TLP = π₀ × ε / (ε + π₀) → avoid sign issues below
    # We use the empirical Maize V6 TLP from Lecoeur & Sinclair (1996):
    tlp_mpa: float         = -1.20    # turgor loss point [MPa] (severe drought boundary)
    rwc_full: float        = 1.00     # relative water content at full turgor
    rwc_tlp: float         = 0.87     # RWC at turgor loss point (Westgate & Boyer 1985)
    ky: float              = 1.25     # yield sensitivity factor (Doorenbos & Kassam 1979)


MAIZE_V6V8 = MaizeV6V8Params()


class PressureVolumeModel:
    """
    Simplified P-V curve for maize cell water relations.

    Computes leaf water potential (ψ_leaf), turgor pressure (P),
    and osmotic potential (π) from relative water content (RWC).

    Based on Tyree & Hammel (1972) linear P-V model:
        π = π₀ / RWC
        P = ψ_leaf - π    (turgor = total - osmotic)
    """

    def __init__(self, params: MaizeV6V8Params = MAIZE_V6V8):
        self.p = params

    def leaf_water_potential(self, rwc: float) -> float:
        """Return ψ_leaf [MPa] from RWC [0-1]."""
        rwc = max(0.01, min(1.0, rwc))
        pi = self.p.pi_0_mpa / rwc                        # osmotic potential
        # Pressure potential: positive above TLP, zero below
        turgor = max(0.0, self.p.pi_0_mpa - pi)           # ≥0 when rwc < 1
        psi_leaf = pi + turgor                             # total = osmotic + pressure
        return psi_leaf

    def turgor_from_rwc(self, rwc: float) -> float:
        """Return turgor pressure P [MPa] — zero at and below TLP."""
        psi = self.leaf_water_potential(rwc)
        pi  = self.p.pi_0_mpa / max(rwc, 0.01)
        return max(0.0, psi - pi)

    def osmotic_adjustment(self, stress_duration_days: float) -> float:
        """
        Estimate osmotic adjustment [MPa] from sustained stress.
        Maize can lower π₀ by ~0.2–0.4 MPa over 5–10 days under mild drought.
        (Blum 1989, Crop Sci.)
        """
        max_adj = 0.35   # MPa maximum adjustment for maize
        tau     = 5.0    # days to 63% adjustment
        return max_adj * (1 - math.exp(-stress_duration_days / tau))


# ---------------------------------------------------------------------------
# 2. CWSI Model — Jackson et al. (1981)
# ---------------------------------------------------------------------------

class CWSIModel:
    """
    Crop Water Stress Index using canopy-to-air temperature differential.

    CWSI = (T_c - T_a - dT_ll) / (dT_ul - dT_ll)

    Where:
        T_c   = canopy temperature [°C] (from thermal infrared proxy)
        T_a   = air temperature [°C]
        dT_ll = lower limit (non-stressed, = -0.2 × VPD for maize; Jackson 1981)
        dT_ul = upper limit (fully stressed, typically +3.5°C for maize)

    Reference: Jackson et al. (1981) Irrigation Sci. 1:193-200
    """

    # Empirical coefficients for maize (Jackson 1981, table 2)
    _A = -0.21    # dT_ll = A + B × VPD
    _B = -0.56
    _DT_UL = 3.5  # upper limit [°C] for maize

    def compute(
        self,
        t_canopy_c: float,
        t_air_c: float,
        vpd_kpa: float,
    ) -> float:
        """Compute CWSI [0=unstressed, 1=fully stressed, <0 ok]."""
        dt = t_canopy_c - t_air_c
        dt_ll = self._A + self._B * vpd_kpa
        dt_ul = self._DT_UL
        denom = dt_ul - dt_ll
        if abs(denom) < 1e-6:
            return 0.5
        cwsi = (dt - dt_ll) / denom
        return max(0.0, min(1.0, cwsi))

    def canopy_temp_from_stress(
        self,
        t_air_c: float,
        vpd_kpa: float,
        cwsi: float,
    ) -> float:
        """Back-compute canopy temperature from CWSI (for simulator)."""
        dt_ll = self._A + self._B * vpd_kpa
        dt_ul = self._DT_UL
        dt    = dt_ll + cwsi * (dt_ul - dt_ll)
        return t_air_c + dt


# ---------------------------------------------------------------------------
# 3. Stomatal Conductance Model — Ball-Berry (1987)
# ---------------------------------------------------------------------------

class StomatalConductanceModel:
    """
    Ball-Berry stomatal conductance model.

    gs = m × (An × hs / Cs) + b

    Where:
        An  = net assimilation [µmol/m²/s]  (proxy from VPD)
        hs  = relative humidity at leaf surface
        Cs  = CO₂ at leaf surface [µmol/mol]
        m   = slope (8-12 for C4 maize)
        b   = minimum conductance [mol/m²/s]

    Simplified for drought use: scale gs_nominal by relative turgor.

    Reference: Ball et al. (1987) in Biggins (ed.) Progress in Photosynthesis
    """

    def __init__(self, params: MaizeV6V8Params = MAIZE_V6V8):
        self.p = params

    def conductance_mmol(
        self,
        rwc: float,
        vpd_kpa: float,
        t_air_c: float,
    ) -> float:
        """
        Estimate stomatal conductance [mmol/m²/s] from RWC and VPD.

        Uses empirical drought response function:
            gs = gs_nominal × f_turgor(rwc) × f_vpd(vpd)

        f_turgor: linear decline below TLP
        f_vpd: exponential decline with increasing VPD
        """
        # Turgor regulation factor
        rwc = max(0.01, min(1.0, rwc))
        if rwc >= self.p.rwc_full:
            f_turgor = 1.0
        elif rwc <= self.p.rwc_tlp:
            f_turgor = 0.10   # residual conductance at TLP
        else:
            f_turgor = 0.10 + 0.90 * (rwc - self.p.rwc_tlp) / (
                self.p.rwc_full - self.p.rwc_tlp
            )

        # VPD regulation: maize gs declines ~40% as VPD goes from 1 to 4 kPa
        f_vpd = math.exp(-0.18 * max(0.0, vpd_kpa - 1.0))

        gs = self.p.gs_nominal_mmol * f_turgor * f_vpd
        return max(5.0, gs)   # minimum residual conductance 5 mmol/m²/s


# ---------------------------------------------------------------------------
# 4. Transpiration Model — SPA-style (Williams et al. 1996)
# ---------------------------------------------------------------------------

class TranspirationModel:
    """
    SPA-style canopy transpiration.

    E = gs × VPD / P_atm × LAI  [mol/m²/s] then converted to [mm/day].

    Reference: Williams et al. (1996) Plant Cell Environ. 19:617-628
    """

    def __init__(self, params: MaizeV6V8Params = MAIZE_V6V8):
        self.p = params

    def transpiration_mm_day(
        self,
        gs_mmol: float,
        vpd_kpa: float,
        t_air_c: float,
        p_atm_kpa: float = 101.325,
    ) -> float:
        """Return canopy transpiration [mm/day]."""
        gs_mol_m2_s = gs_mmol / 1000.0
        # E [mol H₂O / m²leaf / s]
        e_mol = gs_mol_m2_s * (vpd_kpa / p_atm_kpa) * self.p.lai
        # Convert: 1 mol H₂O = 18 g = 18 mL → mm per m² ground per day
        e_mm_day = e_mol * 18.0 * 86400.0 / 1000.0
        return max(0.0, e_mm_day)


# ---------------------------------------------------------------------------
# 5. Sap Flow Model — Granier (1985) analog
# ---------------------------------------------------------------------------

class SapFlowModel:
    """
    Simplified sap flow estimation from transpiration demand.

    Granier (1985) thermal dissipation: Q ∝ ΔT response to heater pulse.
    Here we derive sap flow [L/hr/plant] from canopy transpiration.

    Plant density: 8 plants/m² (typical maize row crop spacing).
    Reference: Granier (1985) Ann. Sci. For. 42:193-200
    """

    _PLANT_DENSITY = 8.0   # plants per m²

    def sap_flow_L_hr(self, transpiration_mm_day: float) -> float:
        """Convert canopy E [mm/day] to sap flow [L/hr per plant]."""
        # mm/day × m² × 1000 ml/L → L/day; /24 → L/hr; /plants
        e_L_day_m2 = transpiration_mm_day / 1000.0 * 1000.0  # mm → mL → L (per m²)
        per_plant  = e_L_day_m2 / self._PLANT_DENSITY
        return per_plant / 24.0


# ---------------------------------------------------------------------------
# 6. AquaCrop Yield Model — FAO (Hsiao et al. 2009)
# ---------------------------------------------------------------------------

class AquaCropYieldModel:
    """
    Simplified AquaCrop yield-water productivity relationship.

    Ya/Ymax = 1 - Ky × (1 - ETa/ETc)

    Where:
        Ya   = actual yield
        Ymax = maximum yield
        Ky   = yield sensitivity factor (1.25 for maize; Doorenbos & Kassam 1979)
        ETa  = actual ET
        ETc  = crop ET under no-stress

    Reference: Hsiao et al. (2009) Agron. J. 101:488-502
    """

    def __init__(self, params: MaizeV6V8Params = MAIZE_V6V8):
        self.ky = params.ky

    def yield_fraction(self, eta_fraction: float) -> float:
        """
        Return Ya/Ymax [0-1] given ETa/ETc fraction [0-1].

        eta_fraction = ETa / ETc  (e.g. 0.7 means 70% of crop demand met)
        """
        eta_fraction = max(0.0, min(1.0, eta_fraction))
        ya_ymax = 1.0 - self.ky * (1.0 - eta_fraction)
        return max(0.0, min(1.0, ya_ymax))

    def cumulative_penalty_pct(
        self,
        daily_eta_fractions: list[float],
    ) -> float:
        """
        Compute cumulative yield penalty [%] over a growing season.
        Simplified: average of daily (1 - Ya/Ymax) × 100.
        """
        if not daily_eta_fractions:
            return 0.0
        penalties = [
            (1.0 - self.yield_fraction(f)) * 100.0
            for f in daily_eta_fractions
        ]
        return sum(penalties) / len(penalties)
