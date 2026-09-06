"""
Static definitions of the mock fleet: which services and buoys exist, and the
baseline characteristics the data generator uses as a starting point for each
one's random walk. This is the one file you'd edit to change what the demo
"fleet" looks like — add a service, add a buoy, change a threshold.
"""

# Each service gets one time series: p95 latency in milliseconds.
# `bias` nudges the random walk so some services trend toward their alert
# thresholds more often than others, the way a flaky dependency would in
# real life, rather than every series just being uniformly quiet.
SERVICES = [
    {
        "name": "telemetry-gateway",
        "base_latency_ms": 115,
        "base_rps": 2100,
        "volatility": 22,
        "bias": 0.0,
        "warn_ms": 400,
        "crit_ms": 800,
    },
    {
        "name": "device-auth",
        "base_latency_ms": 82,
        "base_rps": 610,
        "volatility": 14,
        "bias": 0.0,
        "warn_ms": 400,
        "crit_ms": 800,
    },
    {
        "name": "calibration-worker",
        "base_latency_ms": 260,
        "base_rps": 45,
        "volatility": 60,
        "bias": 3.5,  # trends upward — this one is meant to occasionally breach
        "warn_ms": 400,
        "crit_ms": 800,
    },
    {
        "name": "sensor-ingest",
        "base_latency_ms": 150,
        "base_rps": 3900,
        "volatility": 20,
        "bias": 0.0,
        "warn_ms": 400,
        "crit_ms": 800,
    },
    {
        "name": "alert-dispatcher",
        "base_latency_ms": 60,
        "base_rps": 210,
        "volatility": 10,
        "bias": 0.0,
        "warn_ms": 400,
        "crit_ms": 800,
    },
    {
        "name": "timeseries-db",
        "base_latency_ms": 190,
        "base_rps": 340,
        "volatility": 35,
        "bias": 1.0,  # mildly noisy — occasionally crosses into warning
        "warn_ms": 400,
        "crit_ms": 800,
    },
]

# Each buoy reports four sensor channels plus battery. lat/lng are real
# coordinates off the Virginia coast (Chesapeake Bay mouth) purely for the
# map — swap these for wherever your actual deployment is.
BUOYS = [
    {
        "id": "buoy-01",
        "lat": 36.9012, "lng": -76.1834,
        "base": {"temp": 18.4, "salinity": 34.2, "turbidity": 3.1, "ph": 8.06},
        "volatility": {"temp": 0.4, "salinity": 0.15, "turbidity": 0.3, "ph": 0.03},
        "base_battery": 82,
        "solar_watts": 4.1,
    },
    {
        "id": "buoy-02",
        "lat": 36.9087, "lng": -76.1791,
        "base": {"temp": 18.1, "salinity": 33.9, "turbidity": 4.5, "ph": 8.01},
        "volatility": {"temp": 0.4, "salinity": 0.15, "turbidity": 1.4, "ph": 0.03},
        "base_battery": 64,
        "solar_watts": 3.6,
        "turbidity_drift": 0.06,  # sensor fault: turbidity trends upward over time
    },
    {
        "id": "buoy-03",
        "lat": 36.8945, "lng": -76.1902,
        "base": {"temp": 18.6, "salinity": 34.4, "turbidity": 2.7, "ph": 8.09},
        "volatility": {"temp": 0.4, "salinity": 0.15, "turbidity": 0.3, "ph": 0.03},
        "base_battery": 91,
        "solar_watts": 4.4,
    },
    {
        "id": "buoy-04",
        "lat": 36.9151, "lng": -76.1655,
        "base": {"temp": 17.9, "salinity": 34.0, "turbidity": 3.4, "ph": 8.02},
        "volatility": {"temp": 0.4, "salinity": 0.15, "turbidity": 0.3, "ph": 0.03},
        "base_battery": 22,
        "solar_watts": 0.0,
        "battery_drain": 0.18,  # low battery: draining steadily, not charging
    },
    {
        "id": "buoy-05",
        "lat": 36.9002, "lng": -76.1988,
        "base": {"temp": 18.3, "salinity": 34.1, "turbidity": 3.0, "ph": 8.05},
        "volatility": {"temp": 0.4, "salinity": 0.15, "turbidity": 0.3, "ph": 0.03},
        "base_battery": 55,
        "solar_watts": 2.9,
        "gnss_degraded": True,  # reports a stale/no-fix GNSS state
    },
    {
        "id": "buoy-06",
        "lat": 36.9203, "lng": -76.1710,
        "base": {"temp": 18.5, "salinity": 34.3, "turbidity": 3.2, "ph": 8.04},
        "volatility": {"temp": 0.4, "salinity": 0.15, "turbidity": 0.3, "ph": 0.03},
        "base_battery": 78,
        "solar_watts": 3.9,
    },
]

METRIC_RANGES = {
    "temp": (0, 30),
    "salinity": (28, 38),
    "turbidity": (0, 15),
    "ph": (6.5, 8.5),
}
