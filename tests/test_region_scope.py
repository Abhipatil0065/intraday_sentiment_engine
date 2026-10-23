from __future__ import annotations

from app.models.enums import NewsCategory, ReportKind


def test_region_scope_mexico_cpi_blocked():
    from app.analysis import region_scope
    ok, reason = region_scope.in_scope(
        "Mexico 1st half-month Inflation came in at 0.41%, above forecasts (0.37%) in October",
        ReportKind.ACTUAL,
        NewsCategory.INFLATION,
    )
    assert ok is False
    assert "Mexico" in reason or "whitelist" in reason


def test_region_scope_canada_ppi_blocked():
    from app.analysis import region_scope
    ok, reason = region_scope.in_scope(
        "Canadian PPI MoM Actual 2.0% (Forecast 0.9%, Previous 1.3%)",
        ReportKind.ACTUAL,
        NewsCategory.INFLATION,
    )
    assert ok is False


def test_region_scope_turkish_rate_cut_blocked():
    from app.analysis import region_scope
    ok, reason = region_scope.in_scope(
        "Turkish Lira: Rate cuts raise depreciation risks - Commerzbank",
        ReportKind.GENERAL,
        NewsCategory.CENTRAL_BANK,
    )
    assert ok is False
    assert "central bank" in reason.lower() or "Turkey" in reason


def test_region_scope_us_cpi_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "US CPI comes in hotter than expected at 3.8%",
        ReportKind.ACTUAL,
        NewsCategory.INFLATION,
    )
    assert ok is True


def test_region_scope_eurozone_hicp_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "Eurozone HICP inflation rises to 2.9% YoY",
        ReportKind.ACTUAL,
        NewsCategory.INFLATION,
    )
    assert ok is True


def test_region_scope_german_ifo_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "German IFO Business Climate Index comes in at 86.0",
        ReportKind.ACTUAL,
        NewsCategory.OTHER,
    )
    assert ok is True


def test_region_scope_china_pmi_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "China Caixin Manufacturing PMI comes in at 50.2",
        ReportKind.ACTUAL,
        NewsCategory.OTHER,
    )
    assert ok is True


def test_region_scope_japan_cpi_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "Japan CPI comes in at 3.0% YoY",
        ReportKind.ACTUAL,
        NewsCategory.INFLATION,
    )
    assert ok is True


def test_region_scope_uk_cpi_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "UK CPI comes in at 3.1% YoY",
        ReportKind.ACTUAL,
        NewsCategory.INFLATION,
    )
    assert ok is True


def test_region_scope_fed_cuts_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "Fed cuts rates by 25bp, signals more easing ahead",
        ReportKind.GENERAL,
        NewsCategory.CENTRAL_BANK,
    )
    assert ok is True


def test_region_scope_ecb_decision_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "ECB holds interest rates steady as expected",
        ReportKind.GENERAL,
        NewsCategory.CENTRAL_BANK,
    )
    assert ok is True


def test_region_scope_boj_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "BoJ holds policy rate unchanged",
        ReportKind.GENERAL,
        NewsCategory.CENTRAL_BANK,
    )
    assert ok is True


def test_region_scope_boe_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "BoE holds rates at 5.25%",
        ReportKind.GENERAL,
        NewsCategory.CENTRAL_BANK,
    )
    assert ok is True


def test_region_scope_pboc_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "PBOC keeps loan prime rate unchanged",
        ReportKind.GENERAL,
        NewsCategory.CENTRAL_BANK,
    )
    assert ok is True


def test_region_scope_rba_blocked():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "RBA holds cash rate at 4.35%",
        ReportKind.GENERAL,
        NewsCategory.CENTRAL_BANK,
    )
    assert ok is False


def test_region_scope_boc_blocked():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "BoC holds overnight rate at 2.75%",
        ReportKind.GENERAL,
        NewsCategory.CENTRAL_BANK,
    )
    assert ok is False


def test_region_scope_banxico_blocked():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "Banco de México holds interest rate at 11.00%",
        ReportKind.GENERAL,
        NewsCategory.CENTRAL_BANK,
    )
    assert ok is False


def test_region_scope_non_macro_geopolitics_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "Yemen's Houthis say they launched an attack on Saudi Defence Ministry in Riyadh",
        ReportKind.GENERAL,
        NewsCategory.GEOPOLITICS,
    )
    assert ok is True


def test_region_scope_non_macro_risk_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "Swiss Franc: Funding role faces safe-haven risks - Societe Generale",
        ReportKind.GENERAL,
        NewsCategory.RISK_SENTIMENT,
    )
    assert ok is True



def test_region_scope_fed_cuts_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "Fed cuts rates by 25bp, signals more easing ahead",
        ReportKind.GENERAL,
        NewsCategory.CENTRAL_BANK,
    )
    assert ok is True


def test_region_scope_ecb_decision_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "ECB holds interest rates steady as expected",
        ReportKind.GENERAL,
        NewsCategory.CENTRAL_BANK,
    )
    assert ok is True


def test_region_scope_boj_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "BoJ holds policy rate unchanged",
        ReportKind.GENERAL,
        NewsCategory.CENTRAL_BANK,
    )
    assert ok is True


def test_region_scope_boe_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "BoE holds rates at 5.25%",
        ReportKind.GENERAL,
        NewsCategory.CENTRAL_BANK,
    )
    assert ok is True


def test_region_scope_pboc_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "PBOC keeps loan prime rate unchanged",
        ReportKind.GENERAL,
        NewsCategory.CENTRAL_BANK,
    )
    assert ok is True


def test_region_scope_rba_blocked():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "RBA holds cash rate at 4.35%",
        ReportKind.GENERAL,
        NewsCategory.CENTRAL_BANK,
    )
    assert ok is False


def test_region_scope_boc_blocked():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "BoC holds overnight rate at 2.75%",
        ReportKind.GENERAL,
        NewsCategory.CENTRAL_BANK,
    )
    assert ok is False


def test_region_scope_banxico_blocked():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "Banco de México holds interest rate at 11.00%",
        ReportKind.GENERAL,
        NewsCategory.CENTRAL_BANK,
    )
    assert ok is False


def test_region_scope_non_macro_geopolitics_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "Yemen's Houthis say they launched an attack on Saudi Defence Ministry in Riyadh",
        ReportKind.GENERAL,
        NewsCategory.GEOPOLITICS,
    )
    assert ok is True


def test_region_scope_non_macro_risk_allowed():
    from app.analysis import region_scope
    ok, _ = region_scope.in_scope(
        "Swiss Franc: Funding role faces safe-haven risks - Societe Generale",
        ReportKind.GENERAL,
        NewsCategory.RISK_SENTIMENT,
    )
    assert ok is True
