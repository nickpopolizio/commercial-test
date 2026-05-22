"""
CREW — Facility Intake & Evaluation
Run with: streamlit run crew_carbon_app.py
Requires: pip install streamlit reportlab
"""
from __future__ import annotations

from datetime import date
from io import BytesIO
from pathlib import Path

import streamlit as st
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable, Image as RLImage, Paragraph,
    SimpleDocTemplate, Spacer, Table, TableStyle,
)

# ── Register CREW brand fonts ─────────────────────────────────────────────────
_HERE = Path(__file__).parent
for _name, _file in [
    ("FunnelSans",          "FunnelSans-regular.ttf"),
    ("FunnelSans-Bold",     "FunnelSans-bold.ttf"),
    ("FunnelSans-Italic",   "FunnelSans-italic.ttf"),
    ("IBMPlexSans",         "IBMPlexSans-regular.ttf"),
    ("IBMPlexSans-Bold",    "IBMPlexSans-bold.ttf"),
    ("IBMPlexSans-Italic",  "IBMPlexSans-italic.ttf"),
]:
    _path = _HERE / _file
    if _path.exists():
        pdfmetrics.registerFont(TTFont(_name, str(_path)))

from crew_intake_engine import (
    Confidence,
    FacilityInputs,
    IntakeRecommendationEngine,
    MIN_RESIDUAL_ALK,
    TARGET_RESIDUAL_ALK,
)

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="CREW", page_icon="💧", layout="wide")

CONF_STYLE: dict[Confidence, tuple[str, str]] = {
    Confidence.HIGH:        ("#D1FAE5", "#065F46"),   # green
    Confidence.MEDIUM:      ("#DBEAFE", "#1E40AF"),   # blue
    Confidence.LOW:         ("#FEF3C7", "#92400E"),   # amber
    Confidence.PRELIMINARY: ("#F3F4F6", "#374151"),   # gray
}

st.markdown("""
<style>
[data-testid="stMetric"] {
    background: #ffffff;
    border-radius: 8px;
    padding: 1rem 1.2rem;
    border-left: 4px solid #0077B6;
    box-shadow: 0 1px 4px rgba(0,0,0,0.10);
}
[data-testid="stMetricLabel"] p {
    color: #6B7280 !important;
    font-size: 0.82rem !important;
    font-weight: 600 !important;
    text-transform: uppercase !important;
    letter-spacing: 0.04em !important;
}
[data-testid="stMetricValue"] {
    color: #0077B6 !important;
    font-weight: 700 !important;
}
.hero-card {
    background: #ffffff;
    border: 2px solid #0077B6;
    border-radius: 12px;
    padding: 2rem 2.5rem;
    text-align: center;
    box-shadow: 0 2px 8px rgba(0,119,182,0.12);
}
.hero-label {
    color: #6B7280;
    font-size: 0.85rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 0.4rem;
}
.hero-value {
    color: #0077B6;
    font-size: 3.8rem;
    font-weight: 800;
    line-height: 1.0;
}
.hero-sub {
    color: #9CA3AF;
    font-size: 0.82rem;
    margin-top: 0.5rem;
}
</style>
""", unsafe_allow_html=True)


# ── Helper: optional number input ─────────────────────────────────────────────

def _opt(label: str, unit: str = "mg/L", help: str = "") -> float | None:
    """Text input that returns None when left blank."""
    raw = st.text_input(
        f"{label}" + (f" ({unit})" if unit else ""),
        value="",
        placeholder="—",
        help=help,
    )
    if not raw.strip():
        return None
    try:
        v = float(raw)
        return max(0.0, v)
    except ValueError:
        st.caption(f"⚠ Enter a number or leave blank")
        return None


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 💧 CREW")
    st.caption("Facility Intake & Evaluation")
    st.divider()

    facility_name = st.text_input("Facility name", value="", placeholder="e.g. Riverside WWTP")

    st.markdown("**Required**")
    flow_mgd     = st.number_input("Flow", min_value=0.1, max_value=500.0, value=5.0,
                                   step=0.5, format="%.1f", label_visibility="visible",
                                   help="Plant flow in million gallons per day (MGD)")
    st.caption("million gallons / day")
    gcc_cost     = st.number_input("GCC product cost ($ / metric ton)",
                                   min_value=10.0, max_value=1000.0, value=120.0,
                                   step=10.0, format="%.0f")

    st.divider()

    with st.expander("💧 Influent water quality readings", expanded=True):
        st.caption("Enter whatever values you have from a recent lab report.")
        influent_nh3  = _opt("Ammonia, NH₃-N",       help="Incoming ammonia nitrogen")
        influent_no2  = _opt("Nitrite, NO₂-N",        help="Incoming nitrite nitrogen")
        influent_no3  = _opt("Nitrate, NO₃-N",        help="Incoming nitrate nitrogen")
        influent_p    = _opt("Ortho-phosphorus, PO₄", help="Incoming orthophosphate")
        influent_ph   = _opt("pH",          unit="",  help="Incoming wastewater pH (0–14)")
        influent_alk  = _opt("Alkalinity",            help="Total alkalinity as CaCO₃")

    with st.expander("📋 Effluent permit limits / targets"):
        st.caption("Enter any regulatory limits from the plant's permit or design targets.")
        target_nh3  = _opt("Ammonia limit, NH₃-N",        help="Effluent NH₃-N target or permit limit")
        target_no3  = _opt("Nitrate limit, NO₃-N",        help="Effluent NO₃-N target or permit limit")
        target_tn   = _opt("Total nitrogen (TN) limit",   help="Combined N limit")
        target_tp   = _opt("Total phosphorus (TP) limit", help="Phosphorus limit")

    with st.expander("⚙️ Operational / settling data"):
        st.caption("From the plant's MLSS or settleability records.")
        current_svi     = _opt("Current SVI", unit="mL/g",
                               help="Sludge volume index — measures settling behaviour")
        target_svi_red  = _opt("Target SVI reduction", unit="%",
                               help="Desired percentage improvement in SVI")

    with st.expander("🔧 GCC product settings"):
        dissolution_pct = st.slider("Dissolution efficiency (%)", 50, 100, 85, 5,
                                    help="Fraction of dosed GCC that dissolves. Lower values reflect high aeration-basin CO₂ stripping.")
        residual_target = st.slider("Target residual alkalinity (mg/L as CaCO₃)",
                                    int(MIN_RESIDUAL_ALK), 100, int(TARGET_RESIDUAL_ALK), 5,
                                    help="Safety buffer to protect nitrifying bacteria from pH swings.")

    st.divider()
    st.caption("**Report options**")
    client_logo_file = st.file_uploader(
        "Client / utility logo (optional)",
        type=["png", "jpg", "jpeg"],
        help="Upload the facility or operator logo to include on the PDF report.",
    )
    client_logo_bytes: bytes | None = client_logo_file.read() if client_logo_file else None


# ── Run engine ────────────────────────────────────────────────────────────────

inputs = FacilityInputs(
    flow_mgd               = flow_mgd,
    gcc_cost_per_mt        = gcc_cost,
    influent_nh3_mgl       = influent_nh3,
    influent_no2_mgl       = influent_no2,
    influent_no3_mgl       = influent_no3,
    influent_ortho_p_mgl   = influent_p,
    influent_ph            = influent_ph,
    influent_alkalinity_mgl= influent_alk,
    target_nh3_mgl         = target_nh3,
    target_no3_mgl         = target_no3,
    target_tn_mgl          = target_tn,
    target_tp_mgl          = target_tp,
    current_svi_ml_g       = current_svi,
    target_svi_reduction_pct = target_svi_red,
    dissolution_efficiency = dissolution_pct / 100,
    target_residual_alk_mgl= float(residual_target),
)

rec = IntakeRecommendationEngine(inputs).recommend()
fname = facility_name.strip() or "Facility"


# ── Header ────────────────────────────────────────────────────────────────────

st.title("💧 CREW")
st.markdown(f"**{fname}** &nbsp;·&nbsp; {flow_mgd:.1f} MGD")
st.divider()


# ── Hero: dose ────────────────────────────────────────────────────────────────

col_hero, col_metrics = st.columns([2, 3], gap="large")

with col_hero:
    range_text = f"Typical range: {rec.dose_range_mgl[0]:.0f} – {rec.dose_range_mgl[1]:.0f} mg/L"
    st.markdown(f"""
<div class="hero-card">
  <div class="hero-label">Recommended GCC Dose</div>
  <div class="hero-value">{rec.dose_mgl:.0f} mg/L</div>
  <div class="hero-sub">{range_text}</div>
</div>
""", unsafe_allow_html=True)

    # Confidence badge
    bg, fg = CONF_STYLE[rec.confidence]
    st.markdown(f"""
<div style="margin-top:1rem; text-align:center;">
  <span style="background:{bg}; color:{fg}; border-radius:20px; padding:0.3rem 1rem;
               font-size:0.82rem; font-weight:700; letter-spacing:0.04em;">
    {rec.confidence.value} confidence
  </span>
</div>
<div style="margin-top:0.5rem; text-align:center; color:#9CA3AF; font-size:0.78rem;">
  {rec.method}
</div>
""", unsafe_allow_html=True)

    # Data quality bar
    st.markdown("<div style='margin-top:1.2rem;'>", unsafe_allow_html=True)
    st.caption(f"Data completeness — {rec.data_score}%")
    st.progress(rec.data_score / 100)
    st.markdown("</div>", unsafe_allow_html=True)

with col_metrics:
    m1, m2 = st.columns(2)
    m1.metric("Tons per day",  f"{rec.mass_mt_per_day:.2f} MT")
    m2.metric("Cost per day",  f"${rec.cost_per_day_usd:,.0f}")

    m3, m4 = st.columns(2)
    m3.metric("Tons per year", f"{rec.mass_mt_per_day * 365:,.0f} MT")
    m4.metric("Cost per year", f"${rec.cost_per_year_usd:,.0f}")

    st.markdown("<br>", unsafe_allow_html=True)
    st.info(f"**How we calculated this:**\n\n{rec.explanation}")


# ── Assumptions (if any) ──────────────────────────────────────────────────────

if rec.assumptions:
    with st.expander(f"ℹ️ {len(rec.assumptions)} assumption(s) made — click to review"):
        for a in rec.assumptions:
            st.markdown(f"- {a}")
        st.caption(
            "Fill in more values in the sidebar to replace these assumptions "
            "with measured data and improve recommendation confidence."
        )

st.divider()


# ── PDF builder ───────────────────────────────────────────────────────────────

# Brand palette (extracted from CREW identity)
_NAVY   = colors.HexColor("#1B3548")
_CORAL  = colors.HexColor("#FF6969")
_LNAVY  = colors.HexColor("#EBF0F4")   # light navy tint for alt rows
_WHITE  = colors.white
_LGRAY  = colors.HexColor("#DEE2E6")
_MGRAY  = colors.HexColor("#6C757D")
_GREEN  = colors.HexColor("#2DC653")
_AMBER  = colors.HexColor("#D97706")

# Font names (registered at module load; fall back to Helvetica if TTF absent)
_FB     = "FunnelSans-Bold"  if (_HERE / "FunnelSans-bold.ttf").exists()    else "Helvetica-Bold"
_B      = "IBMPlexSans"      if (_HERE / "IBMPlexSans-regular.ttf").exists() else "Helvetica"
_BB     = "IBMPlexSans-Bold" if (_HERE / "IBMPlexSans-bold.ttf").exists()   else "Helvetica-Bold"

_LOGO_PATH = str(_HERE / "crew_logo.png")


def _logo_image(path_or_bytes, max_w: float, max_h: float) -> RLImage | None:
    """Return a scaled RLImage, or None if the source is unavailable."""
    try:
        src = BytesIO(path_or_bytes) if isinstance(path_or_bytes, bytes) else path_or_bytes
        img = RLImage(src)
        w, h = img.imageWidth, img.imageHeight
        scale = min(max_w / w, max_h / h)
        img.drawWidth, img.drawHeight = w * scale, h * scale
        return img
    except Exception:
        return None


def _alt_rows(n: int, c1: object, c2: object) -> list:
    return [("BACKGROUND", (0, i), (-1, i), c1 if i % 2 == 1 else c2)
            for i in range(1, n + 1)]


def _section_bar(label: str, uw: float) -> Table:
    """Full-width navy label bar used as a section header."""
    t = Table([[label]], colWidths=[uw])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), _NAVY),
        ("TEXTCOLOR",     (0, 0), (-1, -1), _WHITE),
        ("FONTNAME",      (0, 0), (-1, -1), _FB),
        ("FONTSIZE",      (0, 0), (-1, -1), 8),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 8),
    ]))
    return t


def build_pdf(
    fname: str,
    inp: FacilityInputs,
    r,
    client_logo_bytes: bytes | None = None,
) -> bytes:
    buf = BytesIO()
    W, _ = letter
    mg = 0.65 * inch
    uw = W - 2 * mg

    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        topMargin=0.5 * inch, bottomMargin=0.75 * inch,
        leftMargin=mg, rightMargin=mg,
    )

    # Canvas callback: draws coral rule + footer text pinned to page bottom
    _today = date.today().strftime("%B %d, %Y")
    def _draw_footer(canvas, _):
        canvas.saveState()
        y_rule = 0.55 * inch
        canvas.setStrokeColor(_CORAL)
        canvas.setLineWidth(3)
        canvas.line(mg, y_rule, mg + uw, y_rule)
        canvas.setFont(_B, 7)
        canvas.setFillColor(_MGRAY)
        canvas.drawCentredString(
            W / 2, 0.35 * inch,
            f"crewcarbon.com  ·  Prepared {_today}  ·  "
            "Preliminary estimate based on stoichiometric mass balance and empirical data. "
            "Site-specific sampling recommended before implementation.",
        )
        canvas.restoreState()

    base = getSampleStyleSheet()
    S = lambda name, **kw: ParagraphStyle(name, parent=base["Normal"], **kw)

    body_s    = S("bd",  fontName=_B,  fontSize=8.5, leading=12, spaceAfter=4)
    caption_s = S("cp",  fontName=_B,  fontSize=7.5, textColor=_MGRAY,
                  leading=10, spaceAfter=3)
    prep_lbl  = S("pl",  fontName=_FB, fontSize=7,   textColor=_CORAL,
                  leading=9, spaceAfter=1)
    prep_val  = S("pv",  fontName=_BB, fontSize=10,  textColor=_NAVY,
                  leading=12)
    story = []

    # ── Header: CREW logo  |  spacer  |  client logo ─────────────────────────
    crew_img   = _logo_image(_LOGO_PATH, max_w=1.4*inch, max_h=0.45*inch)
    client_img = _logo_image(client_logo_bytes, max_w=1.6*inch, max_h=0.45*inch) \
                 if client_logo_bytes else None

    left_cell  = crew_img  or Paragraph("CREW", S("cl", fontName=_FB, fontSize=16, textColor=_NAVY))
    right_cell = client_img or Paragraph("")

    hdr_tbl = Table([[left_cell, Paragraph(""), right_cell]],
                    colWidths=[1.6*inch, uw - 3.4*inch, 1.8*inch])
    hdr_tbl.setStyle(TableStyle([
        ("VALIGN",  (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",   (2, 0), (2,  0),  "RIGHT"),
    ]))
    story.append(hdr_tbl)
    story.append(Spacer(1, 4))

    # coral rule
    story.append(HRFlowable(width="100%", thickness=3, color=_CORAL, spaceAfter=8))

    # ── Title block ───────────────────────────────────────────────────────────
    story.append(Paragraph(
        "Alkalinity-Enhanced Mode™ &nbsp;|&nbsp; Facility Evaluation",
        S("ti", fontName=_FB, fontSize=14, textColor=_NAVY, spaceAfter=2),
    ))
    story.append(HRFlowable(width="100%", thickness=0.5, color=_LGRAY, spaceAfter=8))

    # ── Prepared-for block ───────────────────────────────────────────────────
    conf_color = _GREEN if r.confidence == Confidence.HIGH  else \
                 _NAVY  if r.confidence == Confidence.MEDIUM else \
                 _AMBER if r.confidence == Confidence.LOW    else _MGRAY

    pf_data = [[
        Paragraph("PREPARED FOR", prep_lbl),
        Paragraph("PLANT FLOW",   prep_lbl),
        Paragraph("DATE",         prep_lbl),
        Paragraph("ESTIMATE QUALITY", prep_lbl),
    ], [
        Paragraph(fname or "—",              prep_val),
        Paragraph(f"{inp.flow_mgd:.1f} MGD", prep_val),
        Paragraph(date.today().strftime("%B %d, %Y"), prep_val),
        Paragraph(r.confidence.value,        S("cv", fontName=_BB, fontSize=10,
                                               textColor=conf_color, leading=12)),
    ]]
    pf_tbl = Table(pf_data, colWidths=[uw*0.32, uw*0.18, uw*0.26, uw*0.24])
    pf_tbl.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW",     (0, 1), (-1, 1),  0.5, _LGRAY),
    ]))
    story.append(pf_tbl)
    story.append(Spacer(1, 10))

    # ── Recommendation metrics ────────────────────────────────────────────────
    story.append(_section_bar("RECOMMENDED ALKALINITY ADDITION", uw))
    story.append(Spacer(1, 4))

    met_data = [
        ["GCC DOSE",              "TONS / DAY",                    "COST / DAY",                   "COST / YEAR"],
        [f"{r.dose_mgl:.0f} mg/L", f"{r.mass_mt_per_day:.2f} MT", f"${r.cost_per_day_usd:,.0f}",  f"${r.cost_per_year_usd:,.0f}"],
        [f"Range {r.dose_range_mgl[0]:.0f}–{r.dose_range_mgl[1]:.0f} mg/L",
         f"{r.mass_mt_per_day * 365:,.0f} MT/yr",
         f"@ ${inp.gcc_cost_per_mt:,.0f}/MT",
         f"{r.confidence.value} confidence  ·  {r.data_score}% data"],
    ]
    bw = uw / 4
    met_tbl = Table(met_data, colWidths=[bw]*4, rowHeights=[14, 28, 11])
    met_tbl.setStyle(TableStyle([
        # Label row
        ("BACKGROUND",    (0, 0), (-1, 0), _LNAVY),
        ("TEXTCOLOR",     (0, 0), (-1, 0), _NAVY),
        ("FONTNAME",      (0, 0), (-1, 0), _FB),
        ("FONTSIZE",      (0, 0), (-1, 0), 7),
        ("ALIGN",         (0, 0), (-1, 0), "CENTER"),
        # Value row
        ("TEXTCOLOR",     (0, 1), (-1, 1), _NAVY),
        ("FONTNAME",      (0, 1), (-1, 1), _FB),
        ("FONTSIZE",      (0, 1), (-1, 1), 18),
        # Coral accent on the dose cell
        ("TEXTCOLOR",     (0, 1), (0,  1), _CORAL),
        # Sub-caption row
        ("TEXTCOLOR",     (0, 2), (-1, 2), _MGRAY),
        ("FONTNAME",      (0, 2), (-1, 2), _B),
        ("FONTSIZE",      (0, 2), (-1, 2), 7),
        # All
        ("ALIGN",         (0, 1), (-1, -1), "CENTER"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("BOX",           (0, 0), (-1, -1), 1.5, _NAVY),
        ("INNERGRID",     (0, 0), (-1, -1), 0.4, _LGRAY),
        ("LINEBELOW",     (0, 0), (-1, 0),  1,   _NAVY),
        ("LINEBELOW",     (0, 1), (-1, 1),  0.5, _LGRAY),
    ]))
    story.append(met_tbl)
    story.append(Spacer(1, 10))

    # ── Calculation basis ─────────────────────────────────────────────────────
    story.append(_section_bar("HOW WE CALCULATED THIS", uw))
    story.append(Spacer(1, 4))
    story.append(Paragraph(r.explanation, body_s))
    story.append(Paragraph(
        f"<i>Method: {r.method}</i>", caption_s
    ))
    story.append(Spacer(1, 8))

    # ── Inputs provided ───────────────────────────────────────────────────────
    def irow(label, val): return [label, val] if val is not None else None

    raw_rows = [
        irow("Plant flow",            f"{inp.flow_mgd:.1f} MGD"),
        irow("Influent alkalinity",   f"{inp.influent_alkalinity_mgl:.0f} mg/L as CaCO₃" if inp.influent_alkalinity_mgl else None),
        irow("Influent pH",           f"{inp.influent_ph:.1f}" if inp.influent_ph else None),
        irow("Influent NH₃-N",        f"{inp.influent_nh3_mgl:.1f} mg/L" if inp.influent_nh3_mgl else None),
        irow("Influent NO₂-N",        f"{inp.influent_no2_mgl:.1f} mg/L" if inp.influent_no2_mgl else None),
        irow("Influent NO₃-N",        f"{inp.influent_no3_mgl:.1f} mg/L" if inp.influent_no3_mgl else None),
        irow("Influent ortho-P",      f"{inp.influent_ortho_p_mgl:.1f} mg/L" if inp.influent_ortho_p_mgl else None),
        irow("Effluent NH₃-N limit",  f"{inp.target_nh3_mgl:.1f} mg/L" if inp.target_nh3_mgl else None),
        irow("Effluent NO₃-N limit",  f"{inp.target_no3_mgl:.1f} mg/L" if inp.target_no3_mgl else None),
        irow("Total nitrogen limit",  f"{inp.target_tn_mgl:.1f} mg/L" if inp.target_tn_mgl else None),
        irow("Total phosphorus limit",f"{inp.target_tp_mgl:.1f} mg/L" if inp.target_tp_mgl else None),
        irow("Current SVI",           f"{inp.current_svi_ml_g:.0f} mL/g" if inp.current_svi_ml_g else None),
        irow("GCC product cost",      f"${inp.gcc_cost_per_mt:,.0f} / metric ton"),
        irow("Dissolution efficiency",f"{inp.dissolution_efficiency*100:.0f}%"),
        irow("Target residual alk.",  f"{inp.target_residual_alk_mgl:.0f} mg/L as CaCO₃"),
    ]
    inp_rows = [ro for ro in raw_rows if ro is not None]

    story.append(_section_bar("INPUTS PROVIDED", uw))
    story.append(Spacer(1, 4))

    # Use two-column layout only when there are enough rows to justify it
    if len(inp_rows) > 5:
        mid       = (len(inp_rows) + 1) // 2
        left_col  = inp_rows[:mid]
        right_col = inp_rows[mid:]
        while len(right_col) < len(left_col):
            right_col.append(["", ""])
        combined = [[lc[0], lc[1], rc[0], rc[1]] for lc, rc in zip(left_col, right_col)]
        it = Table(combined, colWidths=[uw*0.28, uw*0.22, uw*0.28, uw*0.22])
        it.setStyle(TableStyle(
            _alt_rows(len(combined), _WHITE, _LNAVY) + [
                ("FONTNAME",      (0, 0), (-1, -1), _B),
                ("FONTSIZE",      (0, 0), (-1, -1), 8),
                ("FONTNAME",      (0, 0), (0, -1),  _BB),
                ("FONTNAME",      (2, 0), (2, -1),  _BB),
                ("TEXTCOLOR",     (0, 0), (0, -1),  _NAVY),
                ("TEXTCOLOR",     (2, 0), (2, -1),  _NAVY),
                ("TEXTCOLOR",     (1, 0), (1, -1),  _MGRAY),
                ("TEXTCOLOR",     (3, 0), (3, -1),  _MGRAY),
                ("TOPPADDING",    (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("LEFTPADDING",   (0, 0), (-1, -1), 5),
                ("LINEAFTER",     (1, 0), (1, -1),  0.5, _LGRAY),
                ("BOX",           (0, 0), (-1, -1), 0.5, _LGRAY),
            ]
        ))
    else:
        # Single-column layout for sparse data
        it = Table(inp_rows, colWidths=[uw * 0.45, uw * 0.55])
        it.setStyle(TableStyle(
            _alt_rows(len(inp_rows), _WHITE, _LNAVY) + [
                ("FONTNAME",      (0, 0), (-1, -1), _B),
                ("FONTSIZE",      (0, 0), (-1, -1), 8),
                ("FONTNAME",      (0, 0), (0, -1),  _BB),
                ("TEXTCOLOR",     (0, 0), (0, -1),  _NAVY),
                ("TEXTCOLOR",     (1, 0), (1, -1),  _MGRAY),
                ("TOPPADDING",    (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING",   (0, 0), (-1, -1), 5),
                ("BOX",           (0, 0), (-1, -1), 0.5, _LGRAY),
            ]
        ))
    story.append(it)

    # ── Assumptions ───────────────────────────────────────────────────────────
    if r.assumptions:
        story.append(Spacer(1, 8))
        story.append(_section_bar("ASSUMPTIONS", uw))
        story.append(Spacer(1, 4))
        for a in r.assumptions:
            story.append(Paragraph(f"• {a}", caption_s))

    doc.build(story, onFirstPage=_draw_footer, onLaterPages=_draw_footer)
    return buf.getvalue()


# ── Download ──────────────────────────────────────────────────────────────────

pdf  = build_pdf(fname, inputs, rec, client_logo_bytes)
safe = fname.replace(" ", "_")
st.download_button(
    label               = "📄 Download Summary Report",
    data                = pdf,
    file_name           = f"CREW_{safe}_{date.today()}.pdf",
    mime                = "application/pdf",
    type                = "primary",
    use_container_width = True,
)
