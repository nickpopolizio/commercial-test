"""
CREW — Facility Intake Engine
Given any subset of facility measurements and permit limits, selects the
highest-confidence calculation path and returns a GCC dose recommendation.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# ── Stoichiometric constants ───────────────────────────────────────────────────
ALK_CONSUMED_PER_NH3: float   = 7.14    # mg CaCO₃ per mg NH₃-N nitrified
ALK_RECOVERED_PER_NO3: float  = 3.57    # mg CaCO₃ per mg NO₃-N denitrified
TARGET_RESIDUAL_ALK: float    = 75.0    # mg/L default design residual
MIN_RESIDUAL_ALK: float       = 50.0    # mg/L hard safety floor
ENHANCED_RESIDUAL_ALK: float  = 120.0   # mg/L AEM™ full-optimization target

ASSUMED_INFLUENT_NH3: float   = 30.0    # mg/L — typical municipal
ASSUMED_INFLUENT_ALK: float   = 150.0   # mg/L as CaCO₃ — typical municipal

_MT_PER_DAY_FACTOR: float     = 3.785412e-3   # dose_mgl × flow_mgd → MT/day
_DAYS_PER_MONTH: float        = 30.44

# ── Alkalinity chemical equivalence ───────────────────────────────────────────
# kg CaCO₃-equivalent per kg of product (stoichiometric, 100 % purity basis)
ALK_CHEMICAL_EQUIVALENCE: dict[str, float] = {
    "Caustic Soda (NaOH)":            1.25,
    "Hydrated Lime (Ca(OH)₂)":        1.35,
    "Quicklime (CaO)":                1.79,
    "Sodium Bicarbonate (NaHCO₃)":    0.60,
    "Magnesium Hydroxide (Mg(OH)₂)":  1.72,
}


# ── Enums ──────────────────────────────────────────────────────────────────────

class Confidence(str, Enum):
    HIGH        = "High"
    MEDIUM      = "Medium"
    LOW         = "Low"
    PRELIMINARY = "Preliminary"


class CommercialScenario(str, Enum):
    ALKALINITY_REPLACEMENT = "Alkalinity Replacement"
    PROCESS_OPTIMIZATION   = "Process Optimization"


# ── Data containers ───────────────────────────────────────────────────────────

@dataclass
class FacilityInputs:
    """
    Intake form. Only flow_mgd and gcc_cost_per_mt are required.
    All water quality and operational fields are optional.
    """
    # Required
    flow_mgd: float
    gcc_cost_per_mt: float

    # Commercial scenario
    commercial_scenario: CommercialScenario = CommercialScenario.ALKALINITY_REPLACEMENT
    existing_chemical: str | None = None
    existing_chemical_spend_per_month: float | None = None   # $/month

    # Influent water quality (any combination)
    influent_nh3_mgl: float | None = None
    influent_no2_mgl: float | None = None
    influent_no3_mgl: float | None = None
    influent_ortho_p_mgl: float | None = None
    influent_ph: float | None = None
    influent_alkalinity_mgl: float | None = None

    # Effluent permit limits / targets (any combination)
    target_nh3_mgl: float | None = None
    target_no3_mgl: float | None = None
    target_tn_mgl: float | None = None
    target_tp_mgl: float | None = None

    # Operational
    current_svi_ml_g: float | None = None
    target_svi_reduction_pct: float | None = None

    # GCC product parameters
    dissolution_efficiency: float = 0.85
    target_residual_alk_mgl: float = TARGET_RESIDUAL_ALK


@dataclass
class DoseRecommendation:
    # Dose bands (mg/L)
    dose_mgl: float                      # recommended / baseline
    dose_min_mgl: float                  # stoichiometric floor (scenario-dependent)
    dose_enhanced_mgl: float             # AEM™ optimization ceiling
    dose_range_mgl: tuple[float, float]  # legacy ±uncertainty band

    # Mass (metric tons)
    mass_mt_per_day: float
    mass_mt_per_month: float

    # Cost
    cost_per_day_usd: float
    cost_per_month_usd: float
    cost_per_year_usd: float

    # Narrative
    confidence: Confidence
    method: str
    explanation: str
    enhanced_justification: str
    assumptions: list[str]
    data_score: int

    # Replacement scenario cost comparison (None when not applicable)
    cost_delta_per_month: float | None = None   # positive = GCC is cheaper


# ── Recommendation engine ─────────────────────────────────────────────────────

class IntakeRecommendationEngine:
    """
    Picks the highest-confidence calculation path, runs it, and returns a
    DoseRecommendation with scenario-aware dose bands and cost analysis.

    Path priority (descending confidence):
      A  Full stoichiometric balance    — influent NH₃ + alk + effluent target
      B  Alkalinity deficit             — influent alk known, N demand estimated
      C  pH proxy                       — alk estimated from pH
      D  Permit limits only             — N targets but no influent quality
      E  SVI-based empirical            — settling data only
      F  Conservative default           — flow & cost only
    """

    def __init__(self, inputs: FacilityInputs) -> None:
        self.inp = inputs

    # ── Public ────────────────────────────────────────────────────────────────

    def recommend(self) -> DoseRecommendation:
        nh3_removed, net_alk_demand = self._nitrogen_demand()
        influent_alk                = self._effective_influent_alkalinity()

        dose, confidence, method, explanation, assumptions = self._select_path(
            nh3_removed, net_alk_demand, influent_alk
        )
        dose = max(0.0, min(dose, 300.0))

        eff = self.inp.dissolution_efficiency

        # ── Dose bands ────────────────────────────────────────────────────────
        def _dose_at_res(residual: float) -> float:
            deficit = net_alk_demand - (influent_alk - residual)
            return max(0.0, min(300.0, deficit / eff))

        if self.inp.commercial_scenario == CommercialScenario.ALKALINITY_REPLACEMENT:
            # Min = stoichiometric floor (just above process failure threshold)
            dose_min = min(_dose_at_res(MIN_RESIDUAL_ALK), dose)
        else:
            # Process optimization: minimum IS the recommended dose (process goals)
            dose_min = dose

        dose_enhanced = max(dose, _dose_at_res(ENHANCED_RESIDUAL_ALK))

        dose_range = (
            round(max(0.0, dose * 0.85), 1),
            round(min(300.0, dose * 1.20), 1),
        )

        # ── Mass & cost at recommended dose ───────────────────────────────────
        def mt_day(d: float) -> float:
            return d * self.inp.flow_mgd * _MT_PER_DAY_FACTOR

        mass    = mt_day(dose)
        mass_mo = mass * _DAYS_PER_MONTH
        cpd     = mass * self.inp.gcc_cost_per_mt
        cpm     = mass_mo * self.inp.gcc_cost_per_mt
        cpy     = cpd * 365

        # ── Replacement cost delta ────────────────────────────────────────────
        cost_delta: float | None = None
        inp = self.inp
        if (inp.commercial_scenario == CommercialScenario.ALKALINITY_REPLACEMENT
                and inp.existing_chemical_spend_per_month is not None
                and inp.existing_chemical_spend_per_month > 0):
            cost_delta = round(inp.existing_chemical_spend_per_month - cpm, 0)

        # ── Enhanced justification text ───────────────────────────────────────
        enhanced_just = self._enhanced_justification(
            dose, dose_enhanced, mt_day(dose_enhanced)
        )

        return DoseRecommendation(
            dose_mgl              = round(dose, 1),
            dose_min_mgl          = round(dose_min, 1),
            dose_enhanced_mgl     = round(dose_enhanced, 1),
            dose_range_mgl        = dose_range,
            mass_mt_per_day       = round(mass, 3),
            mass_mt_per_month     = round(mass_mo, 2),
            cost_per_day_usd      = round(cpd, 2),
            cost_per_month_usd    = round(cpm, 0),
            cost_per_year_usd     = round(cpy, 0),
            confidence            = confidence,
            method                = method,
            explanation           = explanation,
            enhanced_justification= enhanced_just,
            assumptions           = assumptions,
            data_score            = self._data_score(),
            cost_delta_per_month  = cost_delta,
        )

    # ── Enhanced justification ────────────────────────────────────────────────

    def _enhanced_justification(
        self, dose: float, dose_enhanced: float, mt_enhanced_per_day: float
    ) -> str:
        scenario  = self.inp.commercial_scenario
        delta_mgl = dose_enhanced - dose

        if delta_mgl < 1.0:
            return (
                f"The recommended dose of {dose:.0f} mg/L already approaches the "
                f"AEM™ optimization threshold of {ENHANCED_RESIDUAL_ALK:.0f} mg/L "
                f"residual alkalinity. No meaningful additional product is required to "
                f"activate enhanced process benefits."
            )

        if scenario == CommercialScenario.ALKALINITY_REPLACEMENT:
            return (
                f"Dosing an additional {delta_mgl:.0f} mg/L above the baseline "
                f"replacement rate—targeting a bioreactor residual of "
                f"{ENHANCED_RESIDUAL_ALK:.0f} mg/L as CaCO₃ "
                f"({dose_enhanced:.0f} mg/L GCC, {mt_enhanced_per_day:.2f} MT/day)—"
                f"activates CREW’s Alkalinity-Enhanced Mode™ (AEM). At elevated "
                f"CaCO₃ concentrations, an improved monovalent:divalent cation ratio "
                f"increases floc stability and prevents sludge bulking, lowering SVI and "
                f"sludge blanket levels in secondary clarifiers. Enhanced pH buffering "
                f"reduces variability during peak loads, sustaining nitrification rates and "
                f"biological phosphorus removal (BioP) efficiency. These compounding "
                f"benefits can reduce aeration energy demand through blower turndown and "
                f"decrease reliance on coagulants and polymers—partially or fully "
                f"offsetting the incremental chemical cost. CREW has empirically observed "
                f"consistent process intensification outcomes at residual targets of "
                f"{ENHANCED_RESIDUAL_ALK:.0f} mg/L and above."
            )
        else:
            return (
                f"The baseline dose of {dose:.0f} mg/L meets the facility’s stated "
                f"process goals. Dosing to a bioreactor alkalinity target of "
                f"{ENHANCED_RESIDUAL_ALK:.0f} mg/L "
                f"({dose_enhanced:.0f} mg/L GCC, {mt_enhanced_per_day:.2f} MT/day) "
                f"unlocks the full AEM™ performance envelope. At this operating level, "
                f"the improved monovalent:divalent cation ratio from free calcium ions "
                f"enhances floc stability and prevents bulking—lowering SVI, TSS, and "
                f"sludge blanket levels. Increased alkalinity buffering capacity stabilizes "
                f"pH against diurnal and storm-driven load swings, protecting nitrifier "
                f"populations and sustaining biological nutrient removal (BNR) performance "
                f"under variable influent conditions. These compounding effects can increase "
                f"secondary treatment throughput within existing infrastructure, reduce "
                f"blower energy consumption through improved oxygen transfer efficiency, and "
                f"decrease coagulant and polymer demand. CREW has empirically observed "
                f"consistent process intensification outcomes at residual alkalinity targets "
                f"of {ENHANCED_RESIDUAL_ALK:.0f} mg/L and above."
            )

    # ── Nitrogen demand ───────────────────────────────────────────────────────

    def _nitrogen_demand(self) -> tuple[float, float]:
        inp = self.inp

        influent_nh3 = inp.influent_nh3_mgl if inp.influent_nh3_mgl is not None \
                       else ASSUMED_INFLUENT_NH3

        target_nh3 = inp.target_nh3_mgl
        if target_nh3 is None and inp.target_tn_mgl is not None:
            target_nh3 = min(influent_nh3 * 0.1, 3.0)
        if target_nh3 is None:
            target_nh3 = 3.0

        nh3_removed  = max(0.0, influent_nh3 - target_nh3)
        alk_consumed = nh3_removed * ALK_CONSUMED_PER_NH3

        target_no3 = inp.target_no3_mgl
        if target_no3 is None and inp.target_tn_mgl is not None:
            target_no3 = max(0.0, inp.target_tn_mgl - (target_nh3 or 0))

        alk_recovered = 0.0
        if target_no3 is not None:
            no3_denitrified = max(0.0, nh3_removed - target_no3)
            alk_recovered   = no3_denitrified * ALK_RECOVERED_PER_NO3

        return nh3_removed, max(0.0, alk_consumed - alk_recovered)

    # ── Effective influent alkalinity ─────────────────────────────────────────

    def _effective_influent_alkalinity(self) -> float:
        if self.inp.influent_alkalinity_mgl is not None:
            return self.inp.influent_alkalinity_mgl
        if self.inp.influent_ph is not None:
            return self._alk_from_ph(self.inp.influent_ph)
        return ASSUMED_INFLUENT_ALK

    @staticmethod
    def _alk_from_ph(ph: float) -> float:
        if ph < 6.5:  return 25.0
        if ph < 6.8:  return 50.0
        if ph < 7.0:  return 80.0
        if ph < 7.2:  return 120.0
        if ph < 7.5:  return 175.0
        if ph < 7.8:  return 250.0
        return 320.0

    # ── Path selection ────────────────────────────────────────────────────────

    def _select_path(
        self,
        nh3_removed: float,
        net_alk_demand: float,
        influent_alk: float,
    ) -> tuple[float, Confidence, str, str, list[str]]:

        inp         = self.inp
        assumptions: list[str] = []
        eff         = inp.dissolution_efficiency
        target_res  = inp.target_residual_alk_mgl

        def dose_from_balance(alk: float) -> float:
            deficit = net_alk_demand - (alk - target_res)
            return max(0.0, deficit / eff)

        nh3_known    = inp.influent_nh3_mgl is not None
        alk_measured = inp.influent_alkalinity_mgl is not None
        target_known = inp.target_nh3_mgl is not None or inp.target_tn_mgl is not None

        # ── Path A ────────────────────────────────────────────────────────────
        if nh3_known and alk_measured and target_known:
            dose = dose_from_balance(influent_alk)
            return (
                dose,
                Confidence.HIGH,
                "Full stoichiometric alkalinity mass balance",
                (
                    f"With {inp.influent_nh3_mgl:.0f} mg/L incoming ammonia and a target of "
                    f"{inp.target_nh3_mgl or '~3'} mg/L, nitrification will consume approximately "
                    f"{nh3_removed * ALK_CONSUMED_PER_NH3:.0f} mg/L of alkalinity. "
                    f"Measured influent alkalinity of {influent_alk:.0f} mg/L as CaCO₃ "
                    f"{'covers this demand — only a small maintenance dose is needed' if dose < 10 else 'is not sufficient on its own'}. "
                    f"A dose of {dose:.0f} mg/L GCC maintains a safe residual of {target_res:.0f} mg/L."
                ),
                assumptions,
            )

        # ── Path B ────────────────────────────────────────────────────────────
        if alk_measured:
            if not nh3_known:
                assumptions.append(
                    f"Incoming ammonia assumed {ASSUMED_INFLUENT_NH3:.0f} mg/L (typical municipal — measure for better accuracy)"
                )
            if not target_known:
                assumptions.append("Full nitrification to 3 mg/L assumed (conservative)")

            dose = dose_from_balance(influent_alk)
            return (
                dose,
                Confidence.MEDIUM,
                "Alkalinity deficit calculation with estimated nitrogen demand",
                (
                    f"Starting from the measured alkalinity of {influent_alk:.0f} mg/L as CaCO₃, "
                    f"the estimated nitrification demand of {net_alk_demand:.0f} mg/L "
                    f"{'leaves adequate headroom — a small dose maintains the safety buffer' if dose < 10 else 'creates a deficit that GCC needs to cover'}. "
                    f"A dose of {dose:.0f} mg/L GCC will maintain the {target_res:.0f} mg/L "
                    f"target residual. Entering effluent permit limits will refine this further."
                ),
                assumptions,
            )

        # ── Path C ────────────────────────────────────────────────────────────
        if inp.influent_ph is not None:
            est_alk = self._alk_from_ph(inp.influent_ph)
            assumptions.append(
                f"Alkalinity estimated from pH {inp.influent_ph:.1f} as ~{est_alk:.0f} mg/L as CaCO₃ "
                "(direct measurement will significantly improve accuracy)"
            )
            if not nh3_known:
                assumptions.append(f"Incoming ammonia assumed {ASSUMED_INFLUENT_NH3:.0f} mg/L")

            dose = dose_from_balance(est_alk)
            return (
                dose,
                Confidence.LOW,
                "pH-derived alkalinity estimate",
                (
                    f"A pH of {inp.influent_ph:.1f} suggests an influent alkalinity of roughly "
                    f"{est_alk:.0f} mg/L as CaCO₃. Based on this and an estimated nitrification "
                    f"demand of {net_alk_demand:.0f} mg/L, a dose of {dose:.0f} mg/L is indicated. "
                    "We recommend measuring alkalinity directly — it takes 5 minutes on-site "
                    "and will move this estimate into the Medium–High confidence range."
                ),
                assumptions,
            )

        # ── Path D ────────────────────────────────────────────────────────────
        if any(v is not None for v in [inp.target_nh3_mgl, inp.target_no3_mgl, inp.target_tn_mgl]):
            assumptions.append(
                f"Incoming ammonia assumed {ASSUMED_INFLUENT_NH3:.0f} mg/L (typical municipal)"
            )
            assumptions.append(
                f"Influent alkalinity assumed {ASSUMED_INFLUENT_ALK:.0f} mg/L (typical municipal)"
            )
            dose = dose_from_balance(ASSUMED_INFLUENT_ALK)
            return (
                dose,
                Confidence.LOW,
                "Permit-limit estimate using assumed influent quality",
                (
                    f"Using the plant’s effluent limits and typical municipal influent values, a starting "
                    f"dose of {dose:.0f} mg/L is estimated. This could vary significantly depending "
                    f"on actual water quality. Entering the plant’s measured alkalinity is the single "
                    "most impactful step to improve this recommendation."
                ),
                assumptions,
            )

        # ── Path E ────────────────────────────────────────────────────────────
        if inp.current_svi_ml_g is not None or inp.target_svi_reduction_pct is not None:
            svi = inp.current_svi_ml_g
            if svi is not None:
                dose = 20.0 if svi < 100 else \
                       40.0 if svi < 150 else \
                       65.0 if svi < 200 else \
                       85.0 if svi < 300 else 110.0
            else:
                dose = 60.0
            assumptions.append("Dose estimated from empirical SVI-response data")
            assumptions.append("Alkalinity balance not assessed — enter water quality readings for a full recommendation")
            return (
                dose,
                Confidence.LOW,
                "Empirical SVI-based estimate",
                (
                    f"Based on {'a current SVI of ' + str(int(svi)) + ' mL/g' if svi else 'a target SVI reduction'}, "
                    f"an empirical dose of {dose:.0f} mg/L is estimated from published settling "
                    "improvement data. This does not account for alkalinity balance. "
                    "Please provide alkalinity or pH readings for a more complete recommendation."
                ),
                assumptions,
            )

        # ── Path F ────────────────────────────────────────────────────────────
        assumptions.append(f"Incoming ammonia assumed {ASSUMED_INFLUENT_NH3:.0f} mg/L")
        assumptions.append(f"Influent alkalinity assumed {ASSUMED_INFLUENT_ALK:.0f} mg/L")
        assumptions.append("Conservative preliminary estimate — enter any water quality data to improve")
        return (
            50.0,
            Confidence.PRELIMINARY,
            "Conservative preliminary estimate — no water quality data provided",
            (
                "No water quality data has been entered yet. A conservative starting dose of "
                "50 mg/L is shown based on typical municipal wastewater conditions. "
                "Enter your measured alkalinity, pH, or ammonia in the sidebar to generate "
                "a site-specific recommendation."
            ),
            assumptions,
        )

    # ── Data completeness score ───────────────────────────────────────────────

    def _data_score(self) -> int:
        weights = {
            "influent_alkalinity_mgl":   30,
            "influent_nh3_mgl":          25,
            "target_nh3_mgl":            20,
            "influent_ph":               10,
            "target_no3_mgl":             8,
            "target_tn_mgl":              5,
            "current_svi_ml_g":           2,
        }
        return min(100, sum(
            w for attr, w in weights.items()
            if getattr(self.inp, attr) is not None
        ))
