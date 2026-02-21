import csv
import json
from collections import defaultdict


# ============================================================
# UTILS
# ============================================================

class Utils:

    @staticmethod
    def safe_int(x, default=0):
        try:
            return int(float(x))
        except:
            return default

    @staticmethod
    def normalize_station(s):
        return (s or "").strip()

    @staticmethod
    def dir_key(d):
        return (d or "").strip().upper()

    @staticmethod
    def type_key(t):
        return (t or "").strip().lower()


# ============================================================
# DOMAIN CLASSES
# ============================================================

class Event:
    def __init__(self, event_id, station, event_type):
        self.id = event_id
        self.station = station
        self.type = event_type  # "arrival" or "departure"


class Station:
    def __init__(self, name):
        self.name = name
        self.arrivals = []
        self.departures = []

    def add_event(self, event):
        if event.type == "arrival":
            self.arrivals.append(event)
        else:
            self.departures.append(event)


class Service:
    def __init__(self, srnum, start_time, direction, service_type, pattern_id):
        self.srnum = srnum
        self.start_time = start_time
        self.direction = direction
        self.service_type = service_type
        self.pattern_id = pattern_id

        self.events = []
        self.segments = {}
        self.first_dep = None
        self.last_arr = None

    def add_event(self, event):
        self.events.append(event)

    def add_segment(self, from_event, to_event, travel_time):
        key = f"{from_event.id}_{to_event.id}"
        self.segments[key] = travel_time

    def to_dict(self):
        return {
            "start_time": self.start_time,
            "Dir": self.direction,
            "Type": self.service_type,
            "PatNum": self.pattern_id,
            "segments": self.segments,
            "first_dep": self.first_dep,
            "last_arr": self.last_arr
        }


# ============================================================
# PREPROCESSOR
# ============================================================

class RailwayPreprocessor:

    DEP_BASE = 20000
    ARR_BASE = 100

    OUT_DOWN = "1-o-event-ids_DOWN2.csv"
    OUT_UP = "1-o-event-ids_UP2.csv"
    OUT_JSON = "milp_preprocessed2.json"
    OUT_DUMP = "constraints_dump_rowwise2.txt"

    def __init__(self, services_csv, patterns_csv, supply_csv):

        self.services_csv = services_csv
        self.patterns_csv = patterns_csv
        self.supply_csv = supply_csv

        self.next_dep_id = self.DEP_BASE
        self.next_arr_id = self.ARR_BASE

        self.stations = {}
        self.services = []
        self.patterns = {}

        self.station_seq_down = []
        self.station_seq_up = []

        # MILP structures
        self.turnaround = defaultdict(lambda: {
            "fast": {
                "UP": {"dep": [], "arr": []},
                "DOWN": {"dep": [], "arr": []}
            },
            "slow": {
                "UP": {"dep": [], "arr": []},
                "DOWN": {"dep": [], "arr": []}
            }
        })

        self.route_dep_events = defaultdict(list)

        self.headway = {
            "UP": {"fast": defaultdict(list), "slow": defaultdict(list)},
            "DOWN": {"fast": defaultdict(list), "slow": defaultdict(list)}
        }

    # ========================================================
    # CSV READERS
    # ========================================================

    def read_csv_dict(self, path):
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return [{k.strip(): (v.strip() if v else "") for k, v in r.items()} for r in reader]

    def read_csv_rows(self, path):
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.reader(f))

    # ========================================================
    # LOAD SUPPLY
    # ========================================================

    def load_supply(self):
        rows = self.read_csv_rows(self.supply_csv)

        for row in rows:
            if len(row) >= 2 and row[1].strip().lower() == "station":
                for s in row[2:]:
                    s = Utils.normalize_station(s)
                    if not s or "total" in s.lower():
                        break
                    self.station_seq_down.append(s)
                break

        self.station_seq_up = list(reversed(self.station_seq_down))

        for st in self.station_seq_down:
            self.stations[st] = Station(st)

    # ========================================================
    # LOAD PATTERNS
    # ========================================================

    def load_patterns(self):
        rows = self.read_csv_rows(self.patterns_csv)
        header = rows[0]
        idx_pattern_id = header.index("Pattern_ID")

        for row in rows[1:]:
            pat_id = row[idx_pattern_id].strip()
            if not pat_id:
                continue

            segs = []
            for i, col in enumerate(header):
                if col.startswith("Major_Segment_"):
                    seg = row[i].strip()
                    time_col = col.replace("Major_Segment_", "Time_")
                    dt = 0
                    if time_col in header:
                        j = header.index(time_col)
                        dt = Utils.safe_int(row[j])
                    if "-" in seg:
                        a, b = seg.split("-", 1)
                        segs.append((a.strip(), b.strip(), dt))

            self.patterns[pat_id] = segs

    # ========================================================
    # PROCESS SERVICES
    # ========================================================

    def process_services(self):

        raw_services = self.read_csv_dict(self.services_csv)

        for s in raw_services:

            srnum = Utils.safe_int(s.get("SrNum"))
            start_time = Utils.safe_int(s.get("Time"))
            d = Utils.dir_key(s.get("Dir"))
            tp = Utils.type_key(s.get("Type"))
            pat = s.get("PatNum", "").strip()

            pattern = self.patterns.get(pat)
            if not pattern:
                continue

            service = Service(srnum, start_time, d, tp, pat)

            stations = [pattern[0][0]] + [b for _, b, _ in pattern]
            travel_times = [t for _, _, t in pattern]

            # FIRST DEPARTURE
            first_station = self.stations[stations[0]]
            dep_event = Event(self.next_dep_id, first_station, "departure")
            self.next_dep_id += 1

            service.first_dep = dep_event.id
            service.add_event(dep_event)
            first_station.add_event(dep_event)

            # HEADWAY add
            self.headway[d][tp][stations[0]].append(dep_event.id)

            # DISTRIBUTION add
            route_key = f"{stations[0]}-{stations[-1]}-{tp}-{d}"
            self.route_dep_events[route_key].append((start_time, dep_event.id))

            prev_event = dep_event

            # Traverse pattern
            for i in range(1, len(stations)):

                station_obj = self.stations[stations[i]]

                # ARRIVAL
                arr_event = Event(self.next_arr_id + 1, station_obj, "arrival")
                self.next_arr_id = arr_event.id

                service.add_event(arr_event)
                station_obj.add_event(arr_event)
                service.add_segment(prev_event, arr_event, travel_times[i-1])

                prev_event = arr_event
                service.last_arr = arr_event.id

                # INTERMEDIATE DEPARTURE
                if i < len(stations) - 1:
                    dep_event = Event(self.next_dep_id, station_obj, "departure")
                    self.next_dep_id += 1

                    service.add_event(dep_event)
                    station_obj.add_event(dep_event)
                    service.add_segment(prev_event, dep_event, 0)

                    # HEADWAY add
                    self.headway[d][tp][stations[i]].append(dep_event.id)

                    prev_event = dep_event

            # TURNAROUND primitive sets
            origin = stations[0]
            dest = stations[-1]

            self.turnaround[origin][tp][d]["dep"].append(service.first_dep)
            if service.last_arr is not None:
                self.turnaround[dest][tp][d]["arr"].append(service.last_arr)

            self.services.append(service)

    # ========================================================
    # EXPORT CSV
    # ========================================================

    def export_csv(self):

        def make_header(stations):
            h = ["SrNum", "Time", "Type", "Dir", "PatNum", "From", "To"]
            for st in stations:
                h += [st + "a", st + "d"]
            return h

        down_header = make_header(self.station_seq_down)
        up_header = make_header(self.station_seq_up)

        down_rows = [down_header]
        up_rows = [up_header]

        for service in self.services:

            header = down_header if service.direction == "DOWN" else up_header
            row = {h: "" for h in header}

            row.update({
                "SrNum": service.srnum,
                "Time": service.start_time,
                "Type": service.service_type,
                "Dir": service.direction,
                "PatNum": service.pattern_id,
                "From": service.events[0].station.name,
                "To": service.events[-1].station.name
            })

            for e in service.events:
                if e.type == "arrival":
                    row[e.station.name + "a"] = e.id
                else:
                    row[e.station.name + "d"] = e.id

            if service.direction == "DOWN":
                down_rows.append([row[h] for h in down_header])
            else:
                up_rows.append([row[h] for h in up_header])

        with open(self.OUT_DOWN, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(down_rows)

        with open(self.OUT_UP, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(up_rows)

    # ========================================================
    # EXPORT JSON + DUMP
    # ========================================================

    def export_json_and_dump(self):

        # DISTRIBUTION
        distribution_map = {}
        for route_key, items in self.route_dep_events.items():
            items.sort(key=lambda x: x[0])
            dep_ids = [eid for _, eid in items]
            if len(dep_ids) >= 2:
                distribution_map[route_key] = dep_ids

        # convert defaultdict to dict
        def normal_dict(x):
            if isinstance(x, defaultdict):
                x = dict(x)
            if isinstance(x, dict):
                return {k: normal_dict(v) for k, v in x.items()}
            return x

        milp_json = {
            "station_sequence": {
                "DOWN": self.station_seq_down,
                "UP": self.station_seq_up
            },
            "services": {str(s.srnum): s.to_dict() for s in self.services},
            "turnaround": dict(self.turnaround),
            "distribution": distribution_map,
            "headway": normal_dict(self.headway)
        }

        with open(self.OUT_JSON, "w", encoding="utf-8") as f:
            json.dump(milp_json, f, indent=2)

        with open(self.OUT_DUMP, "w", encoding="utf-8") as f:
            for s in self.services:
                f.write(f"SERVICE {s.srnum}: {s.segments}\n")

    # ========================================================

    def run(self):
        self.load_supply()
        self.load_patterns()
        self.process_services()
        self.export_csv()
        self.export_json_and_dump()

        print("Created:")
        print("  ", self.OUT_DOWN)
        print("  ", self.OUT_UP)
        print("  ", self.OUT_JSON)
        print("  ", self.OUT_DUMP)


# ============================================================

if __name__ == "__main__":
    RailwayPreprocessor(
        "Oservices_data.csv",
        "OWR-patterns-consolidated-sequential.csv",
        "WR_supply.csv"
    ).run()
