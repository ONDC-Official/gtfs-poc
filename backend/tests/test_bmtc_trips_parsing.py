"""BMTC trip collector parsing, against the shapes the zone-wise APIs returned
on 19 Oct 2026. Pure functions - no database or network."""
from datetime import date

from app.bmtc.collector import parse_static_csv
from app.bmtc.trips import parse_service_date, parse_trip_feed


def _entity(trip_id, start_date="19102026", status="SCHEDULED", stops=()):
    return {"id": "3543", "trip_update": {
        "trip": {"trip_id": trip_id, "start_time": "08:25:00", "start_date": start_date,
                 "schedule_relationship": status, "route_id": "11241"},
        "stop_time_update": [
            {"stop_sequence": seq, "arrival": {"delay": arr}, "departure": {"delay": dep},
             "stop_id": sid, "schedule_relationship": st}
            for seq, sid, arr, dep, st in stops]}}


def test_service_date_both_formats():
    assert parse_service_date("19102026") == date(2026, 10, 19)   # trip feed
    assert parse_service_date("20261019") == date(2026, 10, 19)   # vehicle feed
    assert parse_service_date("000000") is None
    assert parse_service_date("") is None


def test_trip_and_stop_rows():
    data = {"header": {"timestamp": "1792426253"}, "entity": [_entity("102930009", stops=[
        (2444805176, "37648", 120, 150, "SCHEDULED"),
        (2444805177, "38003", None, None, "SKIPPED"),
        (2444805178, "38001", 9000, 9000, "SCHEDULED"),     # > 2h: flagged
        (2444805179, "37999", -60, -30, "SCHEDULED")])]}
    trips, stops = parse_trip_feed(data, zone=1)
    t = trips[0]
    assert t["service_date"] == date(2026, 10, 19) and t["zone_id"] == 1
    assert (t["stops_total"], t["stops_skipped"], t["stops_with_delay"]) == (4, 1, 3)
    assert t["max_delay_s"] == 120                          # implausible excluded
    by_seq = {s["stop_sequence"]: s for s in stops}
    assert by_seq[2444805176]["arrival_delay_s"] == 120     # bigint sequence kept
    assert by_seq[2444805177]["arrival_delay_s"] is None    # null stays null, not 0
    assert by_seq[2444805177]["stop_status"] == "SKIPPED"
    assert by_seq[2444805178]["implausible"] is True
    assert by_seq[2444805179]["implausible"] is False


def test_canceled_trip_and_dedup():
    data = {"entity": [_entity("1", status="CANCELED", stops=[(5, "a", None, None, "SKIPPED")]),
                       _entity("1", status="CANCELED", stops=[(5, "a", None, None, "SKIPPED")]),
                       _entity("", stops=[(1, "x", 0, 0, "SCHEDULED")]),
                       _entity("2", start_date="bad")]}
    trips, stops = parse_trip_feed(data, zone=3)
    assert [t["trip_id"] for t in trips] == ["1"] and trips[0]["status"] == "CANCELED"
    assert trips[0]["max_delay_s"] is None and len(stops) == 1


def test_stop_times_large_sequences():
    _, rows, _ = parse_static_csv(4, [
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence",
        "102958632,16:10:00,16:11:00,24847,2445505881",
        "102958632,16:13:00,16:14:00,20748,2445505882"])
    assert [r["stop_sequence"] for r in rows] == [2445505881, 2445505882]
    assert rows[0]["arrival_time"] == "16:10:00"
