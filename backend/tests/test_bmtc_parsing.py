"""BMTC collector parsing: response shapes and feed quirks observed in the
real API (18 Oct 2026). Pure functions - no database or network."""
from datetime import date, datetime, timezone

from app.bmtc.collector import parse_envelope, parse_static_csv, parse_vehicles

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
IST = 19800
RET = 72 * 3600


def _veh(vid, ts_utc, **trip):
    """A vehicle entity as BMTC sends it: timestamp is IST-as-UTC."""
    return {"id": "5436", "vehicle": {
        "trip": {"trip_id": "", "start_time": "", "start_date": "",
                 "schedule_relationship": "UNSCHEDULED", "route_id": "",
                 "sch_no": "N/A", **trip},
        "position": {"latitude": 12.9276, "longitude": 77.6004, "bearing": 0.0},
        "current_status": 1, "timestamp": str(int(ts_utc) + IST),
        "stop_id": "", "vehicle": {"id": vid, "label": "KA51AK0984"}}}


def test_envelope_success_and_both_error_shapes():
    assert parse_envelope({"Data": {"x": 1}, "IsSuccess": True, "RowCount": 0}) == \
        (True, {"x": 1}, None)
    ok, _, err = parse_envelope({"Data": None, "IsSuccess": False, "Message": "bad"})
    assert not ok and err == "bad"
    ok, _, err = parse_envelope({"status": "Error", "errorMessage": "Error in authorization"})
    assert not ok and err == "Error in authorization"
    assert parse_envelope("<html>")[0] is False


def test_vehicle_timestamp_corrected_and_placeholders_nulled():
    t = NOW.timestamp() - 30
    rows, header_ts, stale = parse_vehicles(
        {"header": {"timestamp": str(int(NOW.timestamp()))}, "entity": [_veh("27079", t)]},
        NOW, IST, RET)
    assert stale == 0 and header_ts == NOW
    r = rows[0]
    assert r["ts"].timestamp() == int(t)          # IST offset removed
    assert r["trip_id"] is None and r["sch_no"] is None and r["stop_id"] is None
    assert r["bearing"] is None                   # 0.0 is "unknown"
    assert r["scheduled"] is False and r["label"] == "KA51AK0984"


def test_scheduled_trip_fields():
    rows, _, _ = parse_vehicles({"entity": [_veh(
        "1", NOW.timestamp(), trip_id="102948441", route_id="6951",
        start_time="14:01:00", start_date="20261018",
        schedule_relationship="SCHEDULED", sch_no="EV-366R/2")]}, NOW, IST, RET)
    r = rows[0]
    assert (r["trip_id"], r["route_id"], r["sch_no"]) == ("102948441", "6951", "EV-366R/2")
    assert r["start_date"] == date(2026, 10, 18) and r["scheduled"] is True


def test_stale_dropped_and_duplicate_vehicle_keeps_newest():
    old = NOW.timestamp() - RET - 60
    rows, _, stale = parse_vehicles({"entity": [
        _veh("1", old), _veh("2", NOW.timestamp() - 20), _veh("2", NOW.timestamp() - 5),
        {"vehicle": {"position": {}}}]}, NOW, IST, RET)
    assert stale == 1
    assert [r["vehicle_id"] for r in rows] == ["2"]
    assert rows[0]["ts"].timestamp() == int(NOW.timestamp() - 5)


def test_agency_concatenated_records_are_split():
    lines = ["agency_id,agency_name,agency_url,agency_timezone,agency_phone,agency_lang",
             "323,MPi,https://bmtcitms.karnataka.gov.in,Asia/Kolkata,,en"
             "322,Aargee,https://bmtcitms.karnataka.gov.in,Asia/Kolkata,,en"
             "38,BMTC,https://bmtcitms.karnataka.gov.in,Asia/Kolkata,,en"]
    _, rows, warns = parse_static_csv(0, lines)
    assert [r["agency_id"] for r in rows] == ["323", "322", "38"]
    assert rows[2]["agency_name"] == "BMTC" and rows[2]["agency_phone"] is None
    assert any("re-split" in w for w in warns)


def test_calendar_zero_dates_become_null_and_days_bool():
    _, rows, _ = parse_static_csv(5, [
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date",
        "weekday_service,1,1,1,1,1,0,0,000000,000000"])
    r = rows[0]
    assert r["monday"] is True and r["sunday"] is False
    assert r["start_date"] is None and r["end_date"] is None


def test_static_quoted_commas_header_only_and_unknown_columns():
    _, rows, _ = parse_static_csv(1, ['stop_id,stop_name,stop_lat,stop_lon',
                                      '1,"Majestic, Platform 3",12.97,77.57'])
    assert rows[0]["stop_name"] == "Majestic, Platform 3" and rows[0]["stop_lat"] == 12.97
    _, rows, _ = parse_static_csv(4, ["trip_id,arrival_time,departure_time,stop_id,stop_sequence"])
    assert rows == []
    _, rows, warns = parse_static_csv(3, ["route_id,service_id,trip_id,shape_id",
                                          "1,weekday_service,9,S1"])
    assert rows[0]["trip_id"] == "9" and any("shape_id" in w for w in warns)
