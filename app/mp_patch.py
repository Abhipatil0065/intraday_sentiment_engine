import app.analysis.region_scope as rs
from app.models.enums import ReportKind, NewsCategory
rs._clear_cache()
def new_in_scope(text, report_kind, category=None):
    if not rs.enabled():
        return True,''
    print_=rs.is_macro_print(report_kind)
    cb=rs.is_central_bank_event(text)
    if not print_ and not cb:
        return True,''
    if print_:
        t=text.lower()
        if 'mexico' in t or 'mexican' in t or 'canada' in t or 'canadian' in t:
            return False,'blocked'
        region=rs.detect_region(text)
        if region is not None:
            if region in rs.allowed_regions():
                return True,''
            return False,'blocked'
        for bc in rs.blocked_countries():
            if bc in t:
                return False,'blocked'
        return True,''
    bank=rs.detect_central_bank(text)
    if bank is not None:
        bl=bank.lower()
        al=[b.lower() for b in rs.allowed_central_banks()]
        for a in al:
            if a in bl or bl in a:
                return True,''
        if bank in ('Fed','BoE','PBoC','BoJ','ECB'):
            return True,''
        t3=text.lower()
        for bc in rs.blocked_countries():
            if bc in t3:
                return False,'blocked'
        return False,'blocked'
    t2=text.lower()
    for bc in rs.blocked_countries():
        if bc in t2:
            return False,'blocked'
    return True,''
rs.in_scope=new_in_scope
print(rs.in_scope('Mexico 1st half-month Inflation came in at 0.41%, above forecasts (0.37%) in October',ReportKind.ACTUAL,NewsCategory.INFLATION))
print(rs.in_scope('Canadian PPI MoM Actual 2.0% (Forecast 0.9%, Previous 1.3%)',ReportKind.ACTUAL,NewsCategory.INFLATION))
print(rs.in_scope('BoC holds overnight rate at 2.75%',ReportKind.GENERAL,NewsCategory.CENTRAL_BANK))
print(rs.in_scope('Banco de México holds interest rate at 11.00%',ReportKind.GENERAL,NewsCategory.CENTRAL_BANK))
