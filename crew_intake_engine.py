"""
CREW — Facility Intake Engine
Given any subset of facility measurements and permit limits, selects the
highest-confidence calculation path and returns a GCC dose recommendation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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

# ── Ca²⁺ floc stability thresholds (Grady et al.; Biggs et al. 2001) ──────────
# 1 mg/L CaCO₃ dissolved → 1/100 mmol/L Ca²⁺ → 0.02 meq/L Ca²⁺ (divalent)
CA2_MEQL_PER_MGL_CACO3: float = 0.02   # meq/L Ca²⁺ per mg/L CaCO₃ dissolved
CA2_MEQL_FLOC_MIN: float      = 0.7    # meq/L — minimum for stable biofloc
CA2_MEQL_FLOC_OPTIMAL: float  = 2.0    # meq/L — optimal settling range (upper)

# ── Nitrification kinetics for temperature assessment ──────────────────────────
# Conservative fixed SRT assumption — most municipal BNR plants exceed this
ASSUMED_SRT_DAYS: float       = 12.0   # days — conservative BNR design reference
THETA_NITRIFICATION: float    = 1.072  # Arrhenius θ for AOB (Grady et al.)
MU_MAX_AOB_20C: float         = 0.75   # d⁻¹ — max growth rate at 20°C
B_DECAY_AOB: float            = 0.05   # d⁻¹ — endogenous decay
SRT_SAFETY_FACTOR: float      = 2.5    # design safety factor on minimum SRT

# ── Alkalinity chemical equivalence ───────────────────────────────────────────
# kg CaCO₃-equivalent per kg of product (stoichiometric, 100 % purity basis)
ALK_CHEMICAL_EQUIVALENCE: dict[str, float] = {
    "Caustic Soda (NaOH)":            1.25,
    "Hydrated Lime (Ca(OH)₂)":        1.35,
    "Quicklime (CaO)":                1.79,
    "Sodium Bicarbonate (NaHCO₃)":    0.60,
    "Magnesium Hydroxide (Mg(OH)₂)":  1.72,
}

# Which chemicals provide no Ca²⁺ and risk localized pH overshoot
_CAUSTIC_RISK_CHEMICALS = {"Caustic Soda (NaOH)", "Hydrated Lime (Ca(OH)₂)", "Quicklime (CaO)"}


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
    wastewater_temp_c: float | None = None   # °C — for nitrification risk assessment

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

    # Ca²⁺ contribution (meq/L) at each dose band
    ca2_meq_recommended: float = 0.0
    ca2_meq_enhanced: float    = 0.0

    # Temperature / nitrification risk
    temp_risk_level: str | None = None   # "Low" / "Moderate" / "High" / "Critical"
    temp_risk_note: str = ""

    # Detailed calculation walkthrough (shown on demand)
    calculation_walkthrough: str = ""

    # Replacement scenario cost comparison (None when not applicable)
    cost_delta_per_month: float | None = None   # positive = GCC is cheaper
    existing_chem_has_overdose_risk: bool = False


# ── Recommendation engine ─────────────────────────────────────────────────────

class IntakeRecommendationEngine:
    """
    Picks the highest-confidence calculation path, runs it, and returns a
    DoseRecommendation with scenario-aware dose bands, Ca²⁺ analysis,
    temperature risk assessment, and a full calculation walkthrough.

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
            dose_min = min(_dose_at_res(MIN_RESIDUAL_ALK), dose)
        else:
            dose_min = dose

        dose_enhanced = max(dose, _dose_at_res(ENHANCED_RESIDUAL_ALK))
        dose_range    = (
            round(max(0.0, dose * 0.85), 1),
            round(min(300.0, dose * 1.20), 1),
        )

        # ── Mass & cost ───────────────────────────────────────────────────────
        def mt_day(d: float) -> float:
            return d * self.inp.flow_mgd * _MT_PER_DAY_FACTOR

        mass    = mt_day(dose)
        mass_mo = mass * _DAYS_PER_MONTH
        cpd     = mass * self.inp.gcc_cost_per_mt
        cpm     = mass_mo * self.inp.gcc_cost_per_mt
        cpy     = cpd * 365

        # ── Ca²⁺ contribution ─────────────────────────────────────────────────
        ca2_rec = round(dose     * eff * CA2_MEQL_PER_MGL_CACO3, 3)
        ca2_enh = round(dose_enhanced * eff * CA2_MEQL_PER_MGL_CACO3, 3)

        # ── Temperature risk ──────────────────────────────────────────────────
        t_risk_level: str | None = None
        t_risk_note: str = ""
        if self.inp.wastewater_temp_c is not None:
            t_risk_level, t_risk_note = self._temperature_risk(self.inp.wastewater_temp_c)

        # ── Replacement cost delta ────────────────────────────────────────────
        cost_delta: float | None = None
        inp = self.inp
        if (inp.commercial_scenario == CommercialScenario.ALKALINITY_REPLACEMENT
                and inp.existing_chemical_spend_per_month is not None
                and inp.existing_chemical_spend_per_month > 0):
            cost_delta = round(inp.existing_chemical_spend_per_month - cpm, 0)

        overdose_risk = (
            inp.existing_chemical in _CAUSTIC_RISK_CHEMICALS
            if inp.existing_chemical else False
        )

        # ── Narratives ────────────────────────────────────────────────────────
        enhanced_just = self._enhanced_justification(dose, dose_enhanced, mt_day(dose_enhanced), ca2_enh)
        walkthrough   = self._build_walkthrough(
            nh3_removed, net_alk_demand, influent_alk, dose, dose_min,
            dose_enhanced, mass, mass_mo, cpm, cpy, ca2_rec, ca2_enh,
            t_risk_level, t_risk_note, assumptions,
        )

        return DoseRecommendation(
            dose_mgl                 = round(dose, 1),
            dose_min_mgl             = round(dose_min, 1),
            dose_enhanced_mgl        = round(dose_enhanced, 1),
            dose_range_mgl           = dose_range,
            mass_mt_per_day          = round(mass, 3),
            mass_mt_per_month        = round(mass_mo, 2),
            cost_per_day_usd         = round(cpd, 2),
            cost_per_month_usd       = round(cpm, 0),
            cost_per_year_usd        = round(cpy, 0),
            confidence               = confidence,
            method                   = method,
            explanation              = explanation,
            enhanced_justification   = enhanced_just,
            assumptions              = assumptions,
            data_score               = self._data_score(),
            ca2_meq_recommended      = ca2_rec,
            ca2_meq_enhanced         = ca2_enh,
            temp_risk_level          = t_risk_level,
            temp_risk_note           = t_risk_note,
            calculation_walkthrough  = walkthrough,
            cost_delta_per_month     = cost_delta,
            existing_chem_has_overdose_risk = overdose_risk,
        )

    # ── Temperature / nitrification risk assessment ───────────────────────────

    def _temperature_risk(self, temp_c: float) -> tuple[str, str]:
        mu_T = MU_MAX_AOB_20C * (THETA_NITRIFICATION ** (temp_c - 20.0))
        net  = mu_T - B_DECAY_AOB

        if net <= 0:
            return (
                "Critical",
                f"At {temp_c:.0f}°C, net AOB growth approaches zero — nitrification is "
                f"unlikely regardless of SRT or alkalinity. Alkalinity supplementation "
                f"provides no benefit until temperature rises above ~{20 + (B_DECAY_AOB / (MU_MAX_AOB_20C * 0.1)):.0f}°C.",
            )

        srt_min    = 1.0 / net
        srt_design = srt_min * SRT_SAFETY_FACTOR

        if ASSUMED_SRT_DAYS >= srt_design:
            return (
                "Low",
                f"At {temp_c:.0f}°C, nitrification is reliable at the assumed SRT of "
                f"{ASSUMED_SRT_DAYS:.0f} d (minimum SRT: {srt_min:.1f} d; "
                f"design SRT with {SRT_SAFETY_FACTOR:.1f}× safety: {srt_design:.1f} d). "
                f"Alkalinity supplementation at the recommended dose is appropriate.",
            )
        elif ASSUMED_SRT_DAYS >= srt_min:
            return (
                "Moderate",
                f"At {temp_c:.0f}°C, nitrification is possible but operating with limited "
                f"safety margin (SRT {ASSUMED_SRT_DAYS:.0f} d is above minimum {srt_min:.1f} d "
                f"but below the {SRT_SAFETY_FACTOR:.1f}× design SRT of {srt_design:.1f} d). "
                f"Raising the target residual alkalinity slider to 100+ mg/L provides additional "
                f"pH buffering headroom against nitrification instability.",
            )
        else:
            return (
                "High",
                f"At {temp_c:.0f}°C, nitrification reliability is at risk — the assumed SRT of "
                f"{ASSUMED_SRT_DAYS:.0f} d is below the theoretical minimum of {srt_min:.1f} d. "
                f"A higher residual alkalinity target (120+ mg/L) is strongly recommended as a "
                f"conservative buffer. Confirm actual SRT with the facility.",
            )

    # ── Enhanced justification ────────────────────────────────────────────────

    def _enhanced_justification(
        self, dose: float, dose_enhanced: float,
        mt_enhanced_per_day: float, ca2_enh: float,
    ) -> str:
        scenario  = self.inp.commercial_scenario
        delta_mgl = dose_enhanced - dose

        ca2_note = (
            f"At this dose, GCC dissolution introduces approximately {ca2_enh:.2f} meq/L of "
            f"free Ca²⁺ ions — "
            + ("exceeding" if ca2_enh >= CA2_MEQL_FLOC_MIN else "approaching")
            + f" the peer-reviewed minimum of {CA2_MEQL_FLOC_MIN:.1f} meq/L required for stable "
            f"biofloc formation (Grady, Daigger & Love; Biggs et al. 2001). "
        )

        dic_note = (
            "Unlike caustic soda or magnesium hydroxide, CREW's soluble CaCO₃ simultaneously "
            "provides bicarbonate as a dissolved inorganic carbon (DIC) source for autotrophic "
            "nitrifier growth — the carbon substrate AOB and NOB require to build biomass. "
            "This mechanistic advantage means equivalent alkalinity from CaCO₃ delivers more "
            "stable nitrification than equivalent alkalinity from hydroxide-only sources "
            "(Metcalf & Eddy 5e; WEF Treatment Fundamentals)."
        )

        if delta_mgl < 1.0:
            return (
                f"The recommended dose of {dose:.0f} mg/L already approaches the AEM™ "
                f"optimization threshold of {ENHANCED_RESIDUAL_ALK:.0f} mg/L residual alkalinity. "
                f"No meaningful additional product is required to activate enhanced process benefits. "
                f"{dic_note}"
            )

        if scenario == CommercialScenario.ALKALINITY_REPLACEMENT:
            return (
                f"Dosing an additional {delta_mgl:.0f} mg/L above the baseline replacement "
                f"rate — targeting a bioreactor residual of {ENHANCED_RESIDUAL_ALK:.0f} mg/L as "
                f"CaCO₃ ({dose_enhanced:.0f} mg/L GCC, {mt_enhanced_per_day:.2f} MT/day) — "
                f"activates CREW's Alkalinity-Enhanced Mode™ (AEM). "
                f"{ca2_note}"
                f"The improved monovalent:divalent cation ratio increases floc stability and "
                f"prevents sludge bulking, lowering SVI and sludge blanket levels in secondary "
                f"clarifiers. Enhanced pH buffering reduces variability during peak loads, "
                f"sustaining nitrification rates and biological phosphorus removal (BioP). "
                f"These compounding benefits can reduce aeration energy demand and decrease "
                f"reliance on coagulants and polymers — partially or fully offsetting the "
                f"incremental chemical cost. "
                f"{dic_note} "
                f"CREW has observed consistent process intensification outcomes at residual "
                f"targets of {ENHANCED_RESIDUAL_ALK:.0f} mg/L and above across full-scale facilities."
            )
        else:
            return (
                f"The baseline dose of {dose:.0f} mg/L meets the facility's stated process "
                f"goals. Dosing to a bioreactor alkalinity target of "
                f"{ENHANCED_RESIDUAL_ALK:.0f} mg/L ({dose_enhanced:.0f} mg/L GCC, "
                f"{mt_enhanced_per_day:.2f} MT/day) unlocks the full AEM™ performance envelope. "
                f"{ca2_note}"
                f"The improved monovalent:divalent cation ratio from free Ca²⁺ ions enhances "
                f"floc stability and prevents bulking — lowering SVI, TSS, and sludge blanket "
                f"levels. Increased alkalinity buffering capacity stabilizes pH against diurnal "
                f"and storm-driven load swings, protecting nitrifier populations and sustaining "
                f"BNR performance under variable influent conditions. These compounding effects "
                f"can increase secondary treatment throughput within existing infrastructure and "
                f"reduce blower energy consumption. "
                f"{dic_note} "
                f"CREW has observed consistent process intensification outcomes at residual "
                f"targets of {ENHANCED_RESIDUAL_ALK:.0f} mg/L and above across full-scale facilities."
            )

    # ── Calculation walkthrough ───────────────────────────────────────────────

    def _build_walkthrough(
        self,
        nh3_removed: float, net_alk_demand: float, influent_alk: float,
        dose: float, dose_min: float, dose_enhanced: float,
        mass: float, mass_mo: float, cpm: float, cpy: float,
        ca2_rec: float, ca2_enh: float,
        t_risk_level: str | None, t_risk_note: str,
        assumptions: list[str],
    ) -> str:
        inp = self.inp
        eff = inp.dissolution_efficiency

        lines: list[str] = []
        w = lines.append

        w("## CREW GCC Dose Calculation — Step-by-Step Walkthrough")
        w("")
        w("### 1. Inputs Used")
        w(f"- Plant flow: **{inp.flow_mgd:.1f} MGD**")
        w(f"- GCC product cost: **${inp.gcc_cost_per_mt:,.0f} / MT**")
        w(f"- Dissolution efficiency: **{eff*100:.0f}%**")
        w(f"- Target residual alkalinity: **{inp.target_residual_alk_mgl:.0f} mg/L as CaCO₃**")
        if inp.influent_alkalinity_mgl is not None:
            w(f"- Measured influent alkalinity: **{inp.influent_alkalinity_mgl:.0f} mg/L as CaCO₃**")
        else:
            w(f"- Influent alkalinity: **assumed {influent_alk:.0f} mg/L as CaCO₃** (not measured)")
        if inp.influent_nh3_mgl is not None:
            w(f"- Measured influent NH₃-N: **{inp.influent_nh3_mgl:.1f} mg/L**")
        else:
            w(f"- Influent NH₃-N: **assumed {ASSUMED_INFLUENT_NH3:.0f} mg/L** (not measured)")
        if inp.wastewater_temp_c is not None:
            w(f"- Wastewater temperature: **{inp.wastewater_temp_c:.1f}°C**")
        if assumptions:
            w("")
            w("**Assumptions applied:**")
            for a in assumptions:
                w(f"  - {a}")

        w("")
        w("### 2. Nitrogen Demand Calculation")
        w(f"Every mg/L of NH₃-N nitrified consumes **{ALK_CONSUMED_PER_NH3} mg/L CaCO₃** alkalinity (stoichiometric constant, WEF / Metcalf & Eddy 5e).")
        w("")

        nh3_in  = inp.influent_nh3_mgl or ASSUMED_INFLUENT_NH3
        nh3_out = inp.target_nh3_mgl or 3.0
        alk_consumed = nh3_removed * ALK_CONSUMED_PER_NH3
        w(f"- Influent NH₃-N: {nh3_in:.1f} mg/L")
        w(f"- Effluent NH₃-N target: {nh3_out:.1f} mg/L")
        w(f"- NH₃-N to be removed: {nh3_in:.1f} − {nh3_out:.1f} = **{nh3_removed:.1f} mg/L**")
        w(f"- Alkalinity consumed by nitrification: {nh3_removed:.1f} × {ALK_CONSUMED_PER_NH3} = **{alk_consumed:.1f} mg/L as CaCO₃**")

        alk_recovered = alk_consumed - net_alk_demand
        if alk_recovered > 0.01:
            w("")
            w(f"Denitrification recovers **{ALK_RECOVERED_PER_NO3} mg/L CaCO₃** per mg/L NO₃-N reduced.")
            w(f"- Alkalinity recovered by denitrification: **{alk_recovered:.1f} mg/L as CaCO₃**")
            w(f"- Net alkalinity demand: {alk_consumed:.1f} − {alk_recovered:.1f} = **{net_alk_demand:.1f} mg/L as CaCO₃**")
        else:
            w(f"- No denitrification credit applied (conservative — no NO₃ target provided).")
            w(f"- Net alkalinity demand: **{net_alk_demand:.1f} mg/L as CaCO₃**")

        w("")
        w("### 3. Alkalinity Balance")
        w(f"- Influent alkalinity available: **{influent_alk:.0f} mg/L**")
        w(f"- Net alkalinity demanded by process: **{net_alk_demand:.1f} mg/L**")
        w(f"- Target safety residual to maintain: **{inp.target_residual_alk_mgl:.0f} mg/L**")
        deficit = net_alk_demand - (influent_alk - inp.target_residual_alk_mgl)
        w(f"- Alkalinity deficit = demand − (available − residual target)")
        w(f"  = {net_alk_demand:.1f} − ({influent_alk:.0f} − {inp.target_residual_alk_mgl:.0f})")
        w(f"  = **{deficit:.1f} mg/L as CaCO₃** must be supplemented")

        w("")
        w("### 4. GCC Dose Derivation")
        w(f"GCC dose accounts for dissolution efficiency ({eff*100:.0f}%) — not all dosed product dissolves.")
        w(f"- Recommended dose = deficit ÷ dissolution efficiency")
        w(f"  = {deficit:.1f} ÷ {eff:.2f} = **{dose:.1f} mg/L GCC**")
        w("")
        deficit_min = net_alk_demand - (influent_alk - MIN_RESIDUAL_ALK)
        deficit_enh = net_alk_demand - (influent_alk - ENHANCED_RESIDUAL_ALK)
        w(f"**Dose bands:**")
        w(f"| Band | Residual Target | Deficit | GCC Dose |")
        w(f"|------|----------------|---------|----------|")
        w(f"| Minimum | {MIN_RESIDUAL_ALK:.0f} mg/L | {max(0,deficit_min):.1f} mg/L | **{dose_min:.1f} mg/L** |")
        w(f"| Recommended | {inp.target_residual_alk_mgl:.0f} mg/L | {max(0,deficit):.1f} mg/L | **{dose:.1f} mg/L** |")
        w(f"| Enhanced (AEM™) | {ENHANCED_RESIDUAL_ALK:.0f} mg/L | {max(0,deficit_enh):.1f} mg/L | **{dose_enhanced:.1f} mg/L** |")

        w("")
        w("### 5. Mass and Cost Conversion")
        w(f"Unit factor: dose (mg/L) × flow (MGD) × 3.785412×10⁻³ = MT/day")
        w(f"- MT/day = {dose:.1f} × {inp.flow_mgd:.1f} × 3.785412×10⁻³ = **{mass:.3f} MT/day**")
        w(f"- MT/month ({_DAYS_PER_MONTH:.0f}-day average): {mass:.3f} × {_DAYS_PER_MONTH:.2f} = **{mass_mo:.2f} MT/month**")
        w(f"- Cost/month = {mass_mo:.2f} MT × ${inp.gcc_cost_per_mt:,.0f}/MT = **${cpm:,.0f}/month**")
        w(f"- Cost/year = **${cpy:,.0f}/year**")

        w("")
        w("### 6. Ca²⁺ Ion Contribution")
        w("CaCO₃ is the only common alkalinity source that releases free Ca²⁺ ions on dissolution.")
        w("Basis: MW CaCO₃ = 100 g/mol, Ca²⁺ is divalent → 1 mg/L CaCO₃ = 0.02 meq/L Ca²⁺")
        w(f"- At recommended dose ({dose:.0f} mg/L, {eff*100:.0f}% dissolution):")
        w(f"  {dose:.1f} × {eff:.2f} × {CA2_MEQL_PER_MGL_CACO3} = **{ca2_rec:.3f} meq/L Ca²⁺**")
        w(f"- At enhanced dose ({dose_enhanced:.0f} mg/L):")
        w(f"  {dose_enhanced:.1f} × {eff:.2f} × {CA2_MEQL_PER_MGL_CACO3} = **{ca2_enh:.3f} meq/L Ca²⁺**")
        w(f"- Literature minimum for stable biofloc: **{CA2_MEQL_FLOC_MIN:.1f} meq/L** (Grady, Daigger & Love; Biggs et al. 2001)")
        status = "✓ Exceeds" if ca2_enh >= CA2_MEQL_FLOC_MIN else "⚠ Below"
        w(f"- Enhanced dose Ca²⁺ vs. threshold: **{status} minimum** ({ca2_enh:.3f} vs {CA2_MEQL_FLOC_MIN:.1f} meq/L)")

        if inp.wastewater_temp_c is not None and t_risk_level:
            w("")
            w("### 7. Temperature / Nitrification Risk Assessment")
            w(f"Arrhenius correction: µ_max(T) = {MU_MAX_AOB_20C:.2f} × {THETA_NITRIFICATION}^(T−20)")
            mu_T = MU_MAX_AOB_20C * (THETA_NITRIFICATION ** (inp.wastewater_temp_c - 20))
            w(f"- At {inp.wastewater_temp_c:.1f}°C: µ_max = {MU_MAX_AOB_20C:.2f} × {THETA_NITRIFICATION}^({inp.wastewater_temp_c:.1f}−20) = **{mu_T:.3f} d⁻¹**")
            net_growth = mu_T - B_DECAY_AOB
            if net_growth > 0:
                srt_min = 1.0 / net_growth
                w(f"- Net growth = {mu_T:.3f} − {B_DECAY_AOB:.2f} = {net_growth:.3f} d⁻¹")
                w(f"- Minimum SRT = 1 ÷ {net_growth:.3f} = **{srt_min:.1f} days**")
                w(f"- Design SRT ({SRT_SAFETY_FACTOR:.1f}× safety) = **{srt_min * SRT_SAFETY_FACTOR:.1f} days**")
                w(f"- Assumed facility SRT: **{ASSUMED_SRT_DAYS:.0f} days** (conservative reference)")
            w(f"- Risk level: **{t_risk_level}**")
            w(f"- {t_risk_note}")

        w("")
        w("---")
        w(f"*Prepared using CREW Facility Intake Engine. All stoichiometric constants per*")
        w(f"*WEF Basic Laboratory Procedures (2011), Metcalf & Eddy Wastewater Engineering 5e,*")
        w(f"*and WEF MOP 37 — Operation of Nutrient Removal Facilities.*")
        w(f"*Assumed SRT: {ASSUMED_SRT_DAYS:.0f} days (conservative BNR reference; confirm with facility).*")
        w(f"*Site-specific sampling is recommended before implementation.*")

        return "\n".join(lines)

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
                dose, Confidence.HIGH,
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
                dose, Confidence.MEDIUM,
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
                dose, Confidence.LOW,
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
            assumptions.append(f"Incoming ammonia assumed {ASSUMED_INFLUENT_NH3:.0f} mg/L (typical municipal)")
            assumptions.append(f"Influent alkalinity assumed {ASSUMED_INFLUENT_ALK:.0f} mg/L (typical municipal)")
            dose = dose_from_balance(ASSUMED_INFLUENT_ALK)
            return (
                dose, Confidence.LOW,
                "Permit-limit estimate using assumed influent quality",
                (
                    f"Using the plant's effluent limits and typical municipal influent values, a starting "
                    f"dose of {dose:.0f} mg/L is estimated. This could vary significantly depending "
                    f"on actual water quality. Entering the plant's measured alkalinity is the single "
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
                dose, Confidence.LOW,
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
            50.0, Confidence.PRELIMINARY,
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
