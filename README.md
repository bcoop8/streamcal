# StreamCal

Support and privacy pages for StreamCal, an iPhone app that shows every new TV and streaming episode, day by day.

- Support: https://bcoop8.github.io/streamcal/
- Privacy policy: https://bcoop8.github.io/streamcal/privacy.html
- Sports schedule the app reads: https://bcoop8.github.io/streamcal/snow/v1/events.json

The schedule is rebuilt four times a day by `.github/workflows/snow.yml`, which runs
`snow/build_snow.py` (FIS's public calendar feeds and the Freeride World Tour's event list)
and commits `snow/v1/events.json` when something changed.
