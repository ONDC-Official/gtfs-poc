// Copied from transit-hub-collector/internal/fakefeed (2026-11-26) and owned
// here from now on; the collector keeps its own copy for its unit tests.
// Package fakefeed generates synthetic BMTC-like GTFS-RT vehicle feeds for
// tests and the local fake BMTC server. It reproduces the spec §6.4 quirks:
// timestamps shifted ahead, and entity.id values that are not unique.
//
// Replace with real captured payloads in testdata/ once BMTC samples are
// available (plan task: capture fixtures).
package fakefeed

import (
	"fmt"
	"math/rand/v2"
	"time"

	"github.com/MobilityData/gtfs-realtime-bindings/golang/gtfs"
	"google.golang.org/protobuf/proto"
)

// Bengaluru bounding box, roughly.
const (
	minLat, maxLat = 12.83, 13.14
	minLon, maxLon = 77.46, 77.78
)

// Options describes the feed to build.
type Options struct {
	Now      time.Time
	Vehicles int           // distinct, fresh vehicles
	Shift    time.Duration // added to every timestamp (real BMTC: +5 h 30 m)
	Dupes    int           // extra entities repeating an existing vehicle id (older copy)
	Stale    int           // extra vehicles older than Retention
	Invalid  int           // extra entities with no vehicle id
	Seed     uint64
}

// Build returns a FeedMessage per o. Entity ids deliberately collide
// ("1", "2", ... restarting), as BMTC's do.
func Build(o Options) *gtfs.FeedMessage {
	r := rand.New(rand.NewPCG(o.Seed, o.Seed^0x9e3779b97f4a7c15))
	shifted := func(t time.Time) *uint64 { return proto.Uint64(uint64(t.Add(o.Shift).Unix())) }

	feed := &gtfs.FeedMessage{
		Header: &gtfs.FeedHeader{
			GtfsRealtimeVersion: proto.String("2.0"),
			Incrementality:      gtfs.FeedHeader_FULL_DATASET.Enum(),
			Timestamp:           shifted(o.Now),
		},
	}
	entityID := 0
	add := func(vp *gtfs.VehiclePosition) {
		entityID++
		feed.Entity = append(feed.Entity, &gtfs.FeedEntity{
			Id:      proto.String(fmt.Sprint(entityID%1000 + 1)), // not unique
			Vehicle: vp,
		})
	}
	vehicle := func(i int, ts time.Time) *gtfs.VehiclePosition {
		return &gtfs.VehiclePosition{
			Trip: &gtfs.TripDescriptor{
				TripId:  proto.String(fmt.Sprintf("T%05d", i)),
				RouteId: proto.String(fmt.Sprintf("R%03d", i%400)),
			},
			Vehicle: &gtfs.VehicleDescriptor{Id: proto.String(fmt.Sprintf("KA01F%04d", i))},
			Position: &gtfs.Position{
				Latitude:  proto.Float32(float32(minLat + r.Float64()*(maxLat-minLat))),
				Longitude: proto.Float32(float32(minLon + r.Float64()*(maxLon-minLon))),
				Bearing:   proto.Float32(float32(r.IntN(360))),
				Speed:     proto.Float32(float32(r.IntN(15))),
			},
			Timestamp: shifted(ts.Add(-time.Duration(r.IntN(20)) * time.Second)),
		}
	}

	for i := range o.Vehicles {
		add(vehicle(i, o.Now))
	}
	for i := range min(o.Dupes, o.Vehicles) {
		add(vehicle(i, o.Now.Add(-time.Minute))) // older duplicate of vehicle i
	}
	for i := range o.Stale {
		add(vehicle(o.Vehicles+i, o.Now.Add(-time.Hour)))
	}
	for range o.Invalid {
		vp := vehicle(0, o.Now)
		vp.Vehicle = nil
		add(vp)
	}
	return feed
}

// Marshal encodes a feed as protobuf.
func Marshal(feed *gtfs.FeedMessage) []byte {
	b, err := proto.Marshal(feed)
	if err != nil {
		panic(err) // only fails on programmer error
	}
	return b
}
