import tomllib
from pathlib import Path

from app.routers.ingest import _TOPIC

PI = Path(__file__).resolve().parent.parent / "pi"


def test_telegraf_config_is_valid_and_subscribes_only_to_topics_telem_accepts():
    cfg = tomllib.loads((PI / "telegraf" / "telegraf.conf").read_text())
    mqtt = cfg["inputs"]["mqtt_consumer"][0]
    assert mqtt["qos"] == 1 and mqtt["persistent_session"] and mqtt["client_id"]
    assert "topic_tag" not in mqtt  # telem needs the default tag
    # a message without "time" must still be accepted, so it is a text field, not a mandatory time key
    assert "json_time_key" not in mqtt and mqtt["json_string_fields"] == ["time"]
    for pattern in mqtt["topics"]:
        assert _TOPIC.match(pattern.replace("+", "x1")), pattern
    out = cfg["outputs"]["http"][0]
    assert out["url"].endswith("/api/ingest/telegraf") and out["data_format"] == "json"
    assert out["headers"]["Content-Type"] == "application/json"
    assert out["namepass"] == [mqtt.get("name_override", "mqtt_consumer")]  # nothing else may reach telem


def test_acl_lets_the_bridge_publish_and_telegraf_subscribe_to_the_same_topics():
    acl = (PI / "mosquitto" / "acl").read_text().splitlines()
    rules = [l.split(None, 2) for l in acl if l and not l.startswith("#")]
    assert ["user", "bridge"] in [r[:2] for r in rules] and ["user", "telegraf"] in [r[:2] for r in rules]
    cfg = tomllib.loads((PI / "telegraf" / "telegraf.conf").read_text())
    for pattern in cfg["inputs"]["mqtt_consumer"][0]["topics"]:
        assert ["topic", "write", pattern] in rules  # the bridge may publish what Telegraf reads
    assert ["topic", "read", "vital/#"] in rules
