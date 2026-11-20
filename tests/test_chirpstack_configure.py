import importlib.util
import tomllib
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("cs_configure", Path(__file__).parent.parent / "pi" / "chirpstack" / "configure.py")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)

# Shaped like the files the packages install (line numbers and keys from a real Pi 3B+ install).
CHIRPSTACK = '''
[logging]
  level="info"

[network]
  net_id="000000"
  enabled_regions = [
    "as923",
    "us915_0",
    "us915_1",
  ]

[api]
  bind = "0.0.0.0:8080"
  secret = "x"

[integration]
  enabled = ["mqtt"]

  [integration.mqtt]
    server = "tcp://localhost:1883/"
    json = true
'''
REGION = '''
[[regions]]
  id = "us915_1"
  [regions.gateway.backend]
    enabled = "mqtt"
    [regions.gateway.backend.mqtt]
        topic_prefix = "us915_1"
        share_name = "chirpstack"
        server = "tcp://localhost:1883"
        username = ""
        password = ""
        qos = 0
        clean_session = false
'''
BRIDGE = '''
[backend]
  type="semtech_udp"
  [backend.semtech_udp]
    udp_bind = "0.0.0.0:1700"

[integration]
  marshaler="protobuf"
  [integration.mqtt]
    event_topic_template="gateway/{{ .GatewayID }}/event/{{ .EventType }}"
    state_topic_template="gateway/{{ .GatewayID }}/state/{{ .StateType }}"
    command_topic_template="gateway/{{ .GatewayID }}/command/#"
    [integration.mqtt.auth.generic]
      server="tcp://127.0.0.1:1883"
      username=""
      password=""
'''
FORWARDER = '''{
  "gateway_conf": {
    "gateway_ID": "B827EBFFFE703DFD",
    "server_address": "localhost",
    "serv_port_up": 1730,
    "serv_port_down": 1730,
    "keepalive_interval": 10
  }
}'''


def test_chirpstack_gets_one_region_and_a_qos_1_mqtt_login():
    out, report = c.edit_chirpstack(CHIRPSTACK, "PW1")
    cfg = tomllib.loads(out)
    assert cfg["network"]["enabled_regions"] == ["us915_1"] and cfg["network"]["net_id"] == "000000"
    mqtt = cfg["integration"]["mqtt"]
    assert (mqtt["username"], mqtt["password"], mqtt["qos"], mqtt["json"]) == ("chirpstack", "PW1", 1, True)
    assert cfg["api"]["bind"] == "0.0.0.0:8080" and not any("NOT FOUND" in r for r in report)


def test_chirpstack_gets_an_absolute_sqlite_path():
    out, _ = c.edit_chirpstack(CHIRPSTACK, "PW1")
    assert tomllib.loads(out)["sqlite"]["path"] == "sqlite:///var/lib/chirpstack/chirpstack.sqlite"


def test_region_file_gets_the_login_and_nothing_else_changes():
    out, _ = c.edit_region(REGION, "PW1")
    b = tomllib.loads(out)["regions"][0]["gateway"]["backend"]["mqtt"]
    assert (b["username"], b["password"], b["topic_prefix"], b["qos"]) == ("chirpstack", "PW1", "us915_1", 0)


def test_bridge_publishes_under_the_region_prefix_and_listens_on_localhost_only():
    out, report = c.edit_bridge(BRIDGE, "PW2")
    cfg = tomllib.loads(out)
    mqtt = cfg["integration"]["mqtt"]
    assert mqtt["event_topic_template"] == "us915_1/gateway/{{ .GatewayID }}/event/{{ .EventType }}"
    assert mqtt["state_topic_template"].startswith("us915_1/gateway/")
    assert mqtt["command_topic_template"] == "us915_1/gateway/{{ .GatewayID }}/command/#"
    assert mqtt["auth"]["generic"]["username"] == "gwbridge" and mqtt["auth"]["generic"]["password"] == "PW2"
    assert cfg["backend"]["semtech_udp"]["udp_bind"] == "127.0.0.1:1700" and not any("NOT FOUND" in r for r in report)


def test_forwarder_points_at_the_bridge_and_keeps_its_other_settings():
    out, _ = c.edit_forwarder(FORWARDER)
    assert '"server_address": "127.0.0.1"' in out and '"serv_port_up": 1700' in out and '"serv_port_down": 1700' in out
    assert '"gateway_ID": "B827EBFFFE703DFD"' in out and '"keepalive_interval": 10' in out


def test_a_file_that_does_not_look_as_expected_is_reported_not_guessed():
    _, report = c.edit_bridge("[backend]\n", "PW")
    assert any("NOT FOUND" in r for r in report)


def test_already_configured_chirpstack_is_refused_not_changed_twice():
    out, _ = c.edit_chirpstack(CHIRPSTACK, "PW1")
    with pytest.raises(SystemExit, match="already has MQTT"):
        c.edit_chirpstack(out, "PW2")


def test_passwords_are_long_and_safe_to_put_in_a_toml_string():
    pws = {c.new_password() for _ in range(50)}
    assert len(pws) == 50 and all(len(p) == 24 and p.isalnum() for p in pws)


def test_acl_block_covers_what_each_user_needs_and_no_more():
    rules = [l.split() for l in c.ACL.splitlines() if l and not l.startswith("#")]
    assert ["user", "lorawan"] in rules and ["topic", "read", "application/+/device/+/event/up"] in rules
    assert ["topic", "readwrite", "us915_1/gateway/#"] in rules
    assert ["topic", "write", "application/#"] not in rules  # the adapter only reads uplinks


def test_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    files = {"CHIRPSTACK": CHIRPSTACK, "REGION": REGION, "BRIDGE": BRIDGE, "FORWARDER": FORWARDER}
    for name, text in files.items():
        p = tmp_path / name
        p.write_text(text)
        monkeypatch.setattr(c, name, p)
    monkeypatch.setattr(c, "run", lambda cmd: (_ for _ in ()).throw(AssertionError("must not run commands")))
    monkeypatch.setattr("sys.argv", ["configure.py"])
    c.main()
    assert (tmp_path / "CHIRPSTACK").read_text() == CHIRPSTACK and "Dry run only" in capsys.readouterr().out
    assert not list(tmp_path.glob("*.orig"))


def test_apply_gives_mosquitto_and_the_config_files_the_same_passwords(tmp_path, monkeypatch):
    for name, text in {"CHIRPSTACK": CHIRPSTACK, "REGION": REGION, "BRIDGE": BRIDGE, "FORWARDER": FORWARDER}.items():
        p = tmp_path / name
        p.write_text(text)
        monkeypatch.setattr(c, name, p)
    acl, env, passwd = tmp_path / "acl", tmp_path / "adapter.env", tmp_path / "passwd"
    acl.write_text("user telegraf\ntopic read vital/#\n")
    monkeypatch.setattr(c, "MOSQ_ACL", acl)
    monkeypatch.setattr(c, "MOSQ_PASSWD", passwd)
    monkeypatch.setattr(c, "ADAPTER_ENV", env)
    commands = []
    monkeypatch.setattr(c, "run", commands.append)
    monkeypatch.setattr("sys.argv", ["configure.py", "--apply"])
    c.main()

    users = {cmd[3]: cmd[4] for cmd in commands if cmd[0] == "mosquitto_passwd"}
    assert set(users) == {"chirpstack", "gwbridge", "lorawan"}
    assert tomllib.loads(c.CHIRPSTACK.read_text())["integration"]["mqtt"]["password"] == users["chirpstack"]
    assert tomllib.loads(c.REGION.read_text())["regions"][0]["gateway"]["backend"]["mqtt"]["password"] == users["chirpstack"]
    assert tomllib.loads(c.BRIDGE.read_text())["integration"]["mqtt"]["auth"]["generic"]["password"] == users["gwbridge"]
    assert env.read_text() == f"LORAWAN_MQTT_PASSWORD={users['lorawan']}\n" and oct(env.stat().st_mode)[-3:] == "600"
    assert (tmp_path / "CHIRPSTACK.orig").read_text() == CHIRPSTACK  # the original is kept
    assert acl.read_text().count(c.MARKER) == 1 and acl.read_text().startswith("user telegraf")  # appended, not replaced
    assert [cmd[2] for cmd in commands if cmd[:2] == ["systemctl", "restart"]] == c.SERVICES
    assert ["chown", "root:mosquitto", str(passwd), str(acl)] in commands  # the step that broke Mosquitto twice


def test_a_missing_state_topic_is_added_with_the_region_prefix():
    without = "\n".join(l for l in BRIDGE.splitlines() if "state_topic_template" not in l) + "\n"
    out, report = c.edit_bridge(without, "PW")
    mqtt = tomllib.loads(out)["integration"]["mqtt"]
    assert mqtt["state_topic_template"] == "us915_1/gateway/{{ .GatewayID }}/state/{{ .StateType }}"
    assert mqtt["event_topic_template"].startswith("us915_1/") and not any("NOT FOUND" in r for r in report)


def test_a_commented_out_state_topic_is_treated_as_missing():
    commented = BRIDGE.replace('    state_topic_template=', '    #state_topic_template=')
    out, report = c.edit_bridge(commented, "PW")
    assert tomllib.loads(out)["integration"]["mqtt"]["state_topic_template"].startswith("us915_1/gateway/")
    assert not any("NOT FOUND" in r for r in report)
