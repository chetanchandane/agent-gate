-- Single responsibility: the SQLite schema for the mock Brightline business.
-- A fresh in-memory database is built from this file for every attempt, then
-- seeded from the eval case so each run starts in a known, isolated state.

CREATE TABLE service_areas (zip TEXT PRIMARY KEY);

CREATE TABLE technicians (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  skills TEXT NOT NULL
);

CREATE TABLE slots (
  id INTEGER PRIMARY KEY,
  technician_id INTEGER REFERENCES technicians(id),
  service_type TEXT CHECK (service_type IN ('hvac', 'plumbing', 'electrical')),
  zip TEXT,
  start_ts TEXT,
  end_ts TEXT,
  status TEXT CHECK (status IN ('open', 'booked')) DEFAULT 'open'
);

CREATE TABLE bookings (
  id INTEGER PRIMARY KEY,
  slot_id INTEGER UNIQUE REFERENCES slots(id),
  customer_name TEXT,
  phone TEXT,
  issue_summary TEXT,
  created_at TEXT
);

CREATE TABLE escalations (
  id INTEGER PRIMARY KEY,
  reason TEXT,
  priority TEXT,
  created_at TEXT
);

CREATE TABLE messages (
  id INTEGER PRIMARY KEY,
  to_addr TEXT,
  text TEXT,
  created_at TEXT
);
