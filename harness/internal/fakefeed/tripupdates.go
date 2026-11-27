package fakefeed

import (
	"fmt"
	"math/rand/v2"
	"time"

	"github.com/MobilityData/gtfs-realtime-bindings/golang/gtfs"
	"google.golang.org/protobuf/proto"
)

// TripOptions describes one zone's synthetic trip-update feed.
type TripOptions struct {
	Now          time.Time
	Zone         int
	Trips        int // trips in this zone
	StopsPerTrip int // stop_time_updates per trip
	// CrossZone repeats this many trips of the previous zone (BMTC's zones
	// overlap); Invalid adds entities without a trip_id.
	CrossZone int
	Invalid   int
	Seed      uint64
}

// TripUpdates builds one zone's feed with BMTC's DDMMYYYY start_date
// (spec §6.4) and 10-digit stop_sequence values.
func TripUpdates(o TripOptions) *gtfs.FeedMessage {
	r := rand.New(rand.NewPCG(o.Seed+uint64(o.Zone), uint64(o.Zone)))
	feed := &gtfs.FeedMessage{Header: &gtfs.FeedHeader{
		GtfsRealtimeVersion: proto.String("2.0"),
		Timestamp:           proto.Uint64(uint64(o.Now.Unix())),
	}}
	day := o.Now.Format("02012006") // DDMMYYYY, as BMTC sends it
	trip := func(zone, i int) *gtfs.FeedEntity {
		stus := make([]*gtfs.TripUpdate_StopTimeUpdate, o.StopsPerTrip)
		delay := int32(r.IntN(600) - 120)
		for s := range stus {
			stus[s] = &gtfs.TripUpdate_StopTimeUpdate{
				StopSequence: proto.Uint32(uint32(1000000000 + s)),
				StopId:       proto.String(fmt.Sprintf("S%05d", r.IntN(2000))),
				Arrival: &gtfs.TripUpdate_StopTimeEvent{
					Delay: proto.Int32(delay),
					Time:  proto.Int64(o.Now.Add(time.Duration(s*120+int(delay)) * time.Second).Unix()),
				},
			}
		}
		return &gtfs.FeedEntity{
			Id: proto.String(fmt.Sprint(i%500 + 1)), // not unique, like vehicles
			TripUpdate: &gtfs.TripUpdate{
				Trip: &gtfs.TripDescriptor{
					TripId:    proto.String(fmt.Sprintf("Z%d_T%05d", zone, i)),
					RouteId:   proto.String(fmt.Sprintf("R%03d", i%300)),
					StartDate: proto.String(day),
				},
				Timestamp:      proto.Uint64(uint64(o.Now.Add(-time.Duration(r.IntN(30)) * time.Second).Unix())),
				StopTimeUpdate: stus,
			},
		}
	}
	for i := range o.Trips {
		feed.Entity = append(feed.Entity, trip(o.Zone, i))
	}
	if o.Zone > 1 {
		for i := range o.CrossZone {
			feed.Entity = append(feed.Entity, trip(o.Zone-1, i))
		}
	}
	for i := range o.Invalid {
		e := trip(o.Zone, o.Trips+i)
		e.TripUpdate.Trip.TripId = nil
		feed.Entity = append(feed.Entity, e)
	}
	return feed
}
