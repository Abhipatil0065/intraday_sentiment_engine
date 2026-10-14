"""Verify session windows after the detect_session/next_session fix."""
from datetime import datetime

from app.market.session_detector import (
    detect_session, equity_session, equity_title, next_session, previous_session,
)
from app.utils import timeutils as T


def local(h, m=0):
    return datetime(2026, 10, 13, h, m, tzinfo=T.market_tz())


for h in [19, 20, 21, 1, 2, 3, 4, 6, 7, 8, 9, 11, 12, 14, 16, 17, 18, 19]:
    ref = local(h)
    s = detect_session(ref)
    print(f"{ref.hour:02d}:00 | detect={s.value:10s} | equity_sess={equity_session(ref).value:7s} "
          f"| prev={previous_session(ref).value:8s} | next={next_session(ref).value:8s} | {equity_title(s)}")
