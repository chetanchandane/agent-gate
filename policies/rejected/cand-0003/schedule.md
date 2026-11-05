Book the customer into an open slot.

- Check availability for the service type and zip, then book the first slot
  that is offered.
- Use the customer name and phone from intake when booking.
- If nothing is open on the requested day, widen the search step by step: the
  next week, then the next few weeks, then through the end of the year. Never
  repeat a date range that already came back empty. Book the first slot
  offered. Do not ask the customer for more detail about the problem before
  booking.
- As soon as any slot is returned, call book_slot with it in the same turn.
  Never send a message saying someone will follow up with options instead of
  booking. Only tell the customer the visit is booked after book_slot succeeds,
  and then state the confirmed date, time, and technician.
- If no service type is known but name, phone, and zip are present and the
  customer accepts any open time, check availability for hvac, plumbing, and
  electrical, and book the earliest slot offered.
- Only if every search through the end of the year is empty, tell the customer
  nothing is currently open.
- If the customer said any open time works, book the first slot.
- When several issues were reported, book the first one now and mention that
  the others can be booked in a separate visit.
