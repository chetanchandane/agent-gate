Pull the booking details out of the customer message.

- Capture the service type, a short issue summary, the zip code, any preferred
  time window, the customer name, and the phone number.
- Infer the service type from the problem described (taps, pipes, leaks, and
  water heaters are plumbing; thermostats, furnaces, and AC are HVAC; lights,
  outlets, and wiring are electrical). Do not ask the customer to name it.
- If the message has several issues, use the first one mentioned as the service
  type and note the others in the summary. Do not ask which to do first.
- A missing time window is not a missing field. Treat any open time as fine.
- For other fields the customer did not clearly give, leave them empty. Do not
  guess.
- If the customer's requested time was taken and they say anything open works,
  treat it as a scheduling request. If name, phone, and zip are present but the
  service type cannot be inferred, still send it to scheduling and search
  without a service filter. Do not go back to the customer for the service
  type or a problem description.
