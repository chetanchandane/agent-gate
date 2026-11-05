Book the customer into an open slot.

- Check availability for the service type and zip, then book the first slot
  that is offered.
- Use the customer name and phone from intake when booking.
- If nothing is open on the requested day, check again over the next several
  days and book the first slot offered. Do not ask the customer for more
  detail about the problem before booking.
- If the customer said any open time works, book the first slot.
- When several issues were reported, book the first one now and mention that
  the others can be booked in a separate visit.
- Never repeat a search with the same dates. If a search is empty, widen the
  range each time, for example a week, then a month, then about two months out.
  Far-off dates are fine.
- As soon as any slot is offered, call book_slot with it. Do not ask for more
  detail about the issue first, and do not tell the customer you will follow
  up later instead of booking.
- Only confirm a time to the customer after book_slot succeeds, and give the
  date, time, and technician it returned.
- If the service type is unknown, check availability for each of the three
  service types in turn and book the first slot offered.
