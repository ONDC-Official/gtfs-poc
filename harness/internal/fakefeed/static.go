package fakefeed

import (
	"fmt"
	"hash/fnv"
	"math/rand/v2"
	"strings"
)

// StaticOptions describes a synthetic static GTFS feed.
type StaticOptions struct {
	Stops         int
	Routes        int
	TripsPerRoute int
	StopsPerTrip  int
	// Version changes trip headsigns, so a different Version gives a
	// different fingerprint for the same network.
	Version int
	Seed    uint64
}

// DefaultStatic is a small BMTC-like network: 60k stop_times.
var DefaultStatic = StaticOptions{Stops: 2000, Routes: 300, TripsPerRoute: 10, StopsPerTrip: 20, Version: 1, Seed: 42}

// StaticFiles returns GTFS file name → CSV content, with BMTC's quirks
// (spec §6.4): 10-digit stop_sequence values and one invalid calendar date.
func StaticFiles(o StaticOptions) map[string]string {
	r := rand.New(rand.NewPCG(o.Seed, o.Seed^0x51ed270b))
	var b strings.Builder
	files := map[string]string{}
	flush := func(name string) { files[name] = b.String(); b.Reset() }

	b.WriteString("agency_id,agency_name,agency_url,agency_timezone,agency_lang\n")
	b.WriteString("BMTC,Bangalore Metropolitan Transport Corporation,https://mybmtc.karnataka.gov.in,Asia/Kolkata,en\n")
	flush("agency.txt")

	b.WriteString("stop_id,stop_name,stop_lat,stop_lon,zone_id\n")
	for i := range o.Stops {
		fmt.Fprintf(&b, "S%05d,Stop %d,%.6f,%.6f,Z%d\n", i, i,
			minLat+r.Float64()*(maxLat-minLat), minLon+r.Float64()*(maxLon-minLon), i%7+1)
	}
	flush("stops.txt")

	b.WriteString("route_id,agency_id,route_short_name,route_long_name,route_type\n")
	for i := range o.Routes {
		fmt.Fprintf(&b, "R%03d,BMTC,%d,Route %d,3\n", i, 100+i, i)
	}
	flush("routes.txt")

	b.WriteString("route_id,service_id,trip_id,trip_headsign,shape_id\n")
	var trips []string
	for rt := range o.Routes {
		for t := range o.TripsPerRoute {
			id := fmt.Sprintf("T%03d_%02d", rt, t)
			trips = append(trips, id)
			fmt.Fprintf(&b, "R%03d,%s,%s,Headsign %d v%d,SH%03d\n", rt, []string{"WK", "WE"}[t%2], id, rt, o.Version, rt)
		}
	}
	flush("trips.txt")

	b.WriteString("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n")
	for _, id := range trips {
		start := 5*3600 + r.IntN(16*3600)
		for s := range o.StopsPerTrip {
			t := start + s*120
			hms := fmt.Sprintf("%02d:%02d:%02d", t/3600, t/60%60, t%60)
			// BMTC sends 10-digit stop_sequence values.
			fmt.Fprintf(&b, "%s,%s,%s,S%05d,%d\n", id, hms, hms, r.IntN(o.Stops), 1000000000+s)
		}
	}
	flush("stop_times.txt")

	b.WriteString("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n")
	b.WriteString("WK,1,1,1,1,1,0,0,20260101,20261231\n")
	b.WriteString("WE,0,0,0,0,0,1,1,20260101,20261231\n")
	flush("calendar.txt")

	b.WriteString("service_id,date,exception_type\n")
	b.WriteString("WK,20261225,2\n")
	b.WriteString("WK,20263045,2\n") // invalid date, as BMTC sends (spec §6.4)
	flush("calendar_dates.txt")

	b.WriteString("shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n")
	for rt := range o.Routes {
		for p := range 5 {
			fmt.Fprintf(&b, "SH%03d,%.6f,%.6f,%d\n", rt,
				minLat+r.Float64()*(maxLat-minLat), minLon+r.Float64()*(maxLon-minLon), p)
		}
	}
	flush("shapes.txt")
	return files
}

// StaticCheckOrder maps BMTC's check 0..7 to files for the fake server.
// Deliberately NOT alphabetical or GTFS-spec order: the collector must
// identify files by header, not by check number (plan Q2).
var StaticCheckOrder = []string{
	"stop_times.txt", "agency.txt", "calendar_dates.txt", "routes.txt",
	"shapes.txt", "stops.txt", "calendar.txt", "trips.txt",
}

// StaticZones is how many zones BMTC splits stop_times.txt into.
const StaticZones = 7

// StaticZone returns the CSV header plus the rows whose trip (first column)
// belongs to zone 1..StaticZones, by hash, as BMTC serves stop_times.txt
// per {"zoneid": z}. A nil or out-of-range zone gives the header alone, as
// the real API does without a zoneid.
func StaticZone(csv string, zone *int) string {
	lines := strings.Split(strings.TrimRight(csv, "\n"), "\n")
	var b strings.Builder
	b.WriteString(lines[0] + "\n")
	if zone == nil || *zone < 1 || *zone > StaticZones {
		return b.String()
	}
	for _, l := range lines[1:] {
		trip, _, _ := strings.Cut(l, ",")
		h := fnv.New32a()
		_, _ = h.Write([]byte(trip))
		if int(h.Sum32()%StaticZones)+1 == *zone {
			b.WriteString(l + "\n")
		}
	}
	return b.String()
}
