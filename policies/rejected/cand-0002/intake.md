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
- If the customer gave a name, phone, and zip and said any open time works,
  but did not describe the problem, do not hold the booking for the service
  type. Leave it empty and let scheduling try each service type.
