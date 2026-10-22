"""A measurement group's Event values."""

#: Each measurement on its default event from the A2L.
DEFAULT_EVENT = "default"
#: Every measurement read every `rate_ms`.
POLL_EVENT = "poll"
#: Every measurement on one A2L event, `a2l_event`.
A2L_EVENT = "a2l"
#: Poll rate of a `default` group's measurements with no A2L event.
DEFAULT_POLL_MS = 100
