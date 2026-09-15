"""Service-layer regression coverage for the quality tier.

The Repository contract suite (test_repository_contract.py) asserts what
each adapter's SQL returns, but not what QualityService then does with it in
Python - which is exactly where a cross-database type mismatch bites: an
aggregate over a bigint column comes back as `Decimal` from psycopg but
`float` from sqlite3, and Python raises on float/Decimal arithmetic rather
than coercing. That combination only exists once the two are near each
other in the same expression, which the contract suite - one adapter's
return value asserted in isolation - structurally cannot exercise. These
tests run the actual service methods against both adapters so that class of
bug fails a test instead of a production request.
"""
from conftest import NOW

from app.services import quality as quality_module
from app.services.aggregator import GAP_THRESHOLD_S
from app.services.live_analytics import LiveAnalytics
from app.services.quality import QualityService


def test_continuity_report_continuity_pct_across_adapters(repo, monkeypatch):
    """Regression test for a real bug: on Postgres, AVG(bigint) returns
    Decimal, and `avg_obs / expected_slots` mixing that with the float from
    AVG(x::float) raised TypeError - reproduced live against real feed data,
    never caught by the contract suite alone (it asserts the repository
    layer's return values, not what the service layer's arithmetic then does
    with them). Must not raise, on either adapter, and must actually compute
    a ratio when there is span to compute one from.

    QualityService.continuity() windows off the real wall clock, so the
    fixture's fixed NOW (the only time its OBSERVATIONS data lines up with a
    query window) has to be patched in as "now" for this call.
    """
    monkeypatch.setattr(quality_module.time, "time", lambda: NOW)
    repo.fold_continuity_gaps(0, NOW, GAP_THRESHOLD_S)
    quality = QualityService(repo, LiveAnalytics(repo))

    result = quality.continuity(hours=4.0)

    assert result["report_continuity_pct"] is not None
    assert 0 <= result["report_continuity_pct"] <= 100
