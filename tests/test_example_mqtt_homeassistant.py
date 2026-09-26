"""Tests for examples/bigfix_plugin_mqtt_homeassistant.py, without a broker."""

import contextlib
import datetime
import importlib.util
import json
import os
import sys
import types

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PLUGIN_PATH = os.path.join(ROOT, "examples", "bigfix_plugin_mqtt_homeassistant.py")

SERIAL = 152178487
FQDN = "bigfix.example.com"
DATA_AGE_RELEVANCE = (
    "((now - it) / second) of maximum of last report times of bes computers"
)
CRITICAL_PATCHES_RELEVANCE = (
    "number of bes fixlets whose (fixlet flag of it"
    " AND applicable computer count of it > 0"
    " AND exists applicable computers whose"
    " (now - last report time of it < 60 * day) of it"
    ' AND exists (name of site of it) whose (it = "Enterprise Security"'
    ' or it starts with "Updates for" or it starts with "Patches for")'
    " AND exists (source severity of it as lowercase) whose"
    ' (it is contained by set of ("high";"important";"critical"))'
    " AND exists default action of it"
    " AND globally visible flag of it"
    ' AND name of it does not contain "(Superseded)")'
)
LAST_UPDATE = datetime.datetime(2026, 9, 25, 22, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture
def plugin():
    """Load the example plugin as a module, without running main()."""
    spec = importlib.util.spec_from_file_location("mqtt_ha_plugin", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeConnection:
    """Answers session relevance and REST GETs from dicts of query/path -> result."""

    def __init__(self, answers, get_responses=None):
        self.answers = answers
        self.get_responses = get_responses or {}

    def session_relevance_json(self, relevance, **kwargs):
        return {"result": self.answers[relevance]}

    def get(self, path, **kwargs):
        return types.SimpleNamespace(text=self.get_responses[path])


def by_topic(messages):
    return {message["topic"]: message for message in messages}


def test_get_bigfix_info(plugin):
    """Test that masthead serial, fqdn and each sensor value are queried."""
    conn = FakeConnection(
        {
            plugin.MASTHEAD_RELEVANCE: [[SERIAL, FQDN]],
            "number of bes computers whose (last report time of it >= now - 45 * day)": [
                8
            ],
            'number of bes actions whose (state of it = "Open")': [92],
            "number of bes users": [10],
            DATA_AGE_RELEVANCE: [64],
            CRITICAL_PATCHES_RELEVANCE: [8],
            "number of bes computers whose (last report time of it < now - 45 * day)": [
                27
            ],
        }
    )
    serial, fqdn, values = plugin.get_bigfix_info(conn, plugin.DEFAULT_SENSORS)

    assert (serial, fqdn) == (str(SERIAL), FQDN)
    assert values == {
        "computers": 8,
        "actions": 92,
        "users": 10,
        "computers_not_reporting": 27,
        "data_age": 64,
        "relevant_critical_patches": 8,
    }


def test_get_root_server_version(plugin):
    """Test the root server's own installed BigFix version is queried."""
    conn = FakeConnection({}, {"serverinfo": '{"version": "10.0.7.52"}'})

    assert plugin.get_root_server_version(conn) == "10.0.7.52"


def test_build_messages_includes_sw_version(plugin):
    """Test sw_version is set, and model/manufacturer stay the generic ones."""
    messages = by_topic(
        plugin.build_messages(
            str(SERIAL),
            FQDN,
            {},
            [],
            last_update=LAST_UPDATE,
            sw_version="10.0.7.52",
        )
    )
    config = json.loads(
        messages[f"homeassistant/sensor/bigfix_{SERIAL}/last_update/config"]["payload"]
    )

    assert config["device"]["sw_version"] == "10.0.7.52"
    assert config["device"]["model"] == "BigFix Server"
    assert config["device"]["manufacturer"] == "HCL BigFix"


def test_discovery_messages_make_one_device(plugin):
    """Test HA discovery config: one device keyed by masthead serial."""
    values = {"computers": 8, "actions": 92}
    messages = by_topic(
        plugin.build_messages(str(SERIAL), FQDN, values, plugin.DEFAULT_SENSORS)
    )

    config_topics = [t for t in messages if t.endswith("/config")]
    assert sorted(config_topics) == [
        f"homeassistant/sensor/bigfix_{SERIAL}/actions/config",
        f"homeassistant/sensor/bigfix_{SERIAL}/computers/config",
        f"homeassistant/sensor/bigfix_{SERIAL}/computers_not_reporting/config",
        f"homeassistant/sensor/bigfix_{SERIAL}/data_age/config",
        f"homeassistant/sensor/bigfix_{SERIAL}/last_update/config",
        f"homeassistant/sensor/bigfix_{SERIAL}/plugin_version/config",
        f"homeassistant/sensor/bigfix_{SERIAL}/relevant_critical_patches/config",
        f"homeassistant/sensor/bigfix_{SERIAL}/users/config",
    ]

    for topic in config_topics:
        assert messages[topic]["retain"] is True
        config = json.loads(messages[topic]["payload"])
        assert config["device"]["identifiers"] == [f"bigfix_{SERIAL}"]
        assert config["device"]["name"] == FQDN
        assert config["device"]["configuration_url"] == f"https://{FQDN}:52311/api/help"
        assert config["state_topic"] == f"bigfix/{SERIAL}/state"
        assert config["unique_id"].startswith(f"bigfix_{SERIAL}_")

    computers = json.loads(
        messages[f"homeassistant/sensor/bigfix_{SERIAL}/computers/config"]["payload"]
    )
    assert computers["value_template"] == "{{ value_json.computers }}"
    assert computers["name"] == "Computers"
    assert computers["unique_id"] == f"bigfix_{SERIAL}_computers"


def test_state_message_has_values(plugin):
    """Test the retained state message holds every sensor value."""
    values = {"computers": 8, "actions": 92}
    messages = by_topic(
        plugin.build_messages(
            str(SERIAL),
            FQDN,
            values,
            plugin.DEFAULT_SENSORS,
            discovery_prefix="ha",
            base_topic="bf",
            last_update=LAST_UPDATE,
        )
    )

    state = messages[f"bf/{SERIAL}/state"]
    assert state["retain"] is True
    assert json.loads(state["payload"]) == {
        **values,
        "last_update": "2026-09-25T22:00:00+00:00",
        "plugin_version": plugin.__version__,
    }
    assert f"ha/sensor/bigfix_{SERIAL}/actions/config" in messages


def test_last_update_sensor_is_a_timestamp(plugin):
    """Test Last Update is discovered as a HA timestamp sensor."""
    messages = by_topic(
        plugin.build_messages(str(SERIAL), FQDN, {}, [], last_update=LAST_UPDATE)
    )
    config = json.loads(
        messages[f"homeassistant/sensor/bigfix_{SERIAL}/last_update/config"]["payload"]
    )

    assert config["name"] == "Last Update"
    assert config["device_class"] == "timestamp"
    # HA rejects a timestamp sensor with a state_class:
    assert "state_class" not in config
    assert config["value_template"] == "{{ value_json.last_update }}"
    assert config["device"]["identifiers"] == [f"bigfix_{SERIAL}"]


def test_data_age_sensor_is_a_duration_in_seconds(plugin):
    """Test Data Age is a numeric HA duration sensor in seconds."""
    messages = by_topic(
        plugin.build_messages(str(SERIAL), FQDN, {}, plugin.DEFAULT_SENSORS)
    )
    config = json.loads(
        messages[f"homeassistant/sensor/bigfix_{SERIAL}/data_age/config"]["payload"]
    )

    assert config["name"] == "Data Age"
    assert config["device_class"] == "duration"
    assert config["unit_of_measurement"] == "s"
    assert config["state_class"] == "measurement"


def test_last_update_defaults_to_now_with_timezone(plugin):
    """Test that without a given time, Last Update is now, timezone aware."""
    before = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    messages = by_topic(plugin.build_messages(str(SERIAL), FQDN, {}, []))
    after = datetime.datetime.now(datetime.timezone.utc)

    state = json.loads(messages[f"bigfix/{SERIAL}/state"]["payload"])
    last_update = datetime.datetime.fromisoformat(state["last_update"])
    assert last_update.tzinfo is not None
    assert before <= last_update <= after


def test_publish_passes_config_to_paho(plugin, monkeypatch):
    """Test that broker settings and creds from the config reach paho."""
    calls = []
    publish_module = types.SimpleNamespace(
        multiple=lambda msgs, **kwargs: calls.append((msgs, kwargs))
    )
    mqtt_package = types.ModuleType("paho.mqtt")
    mqtt_package.publish = publish_module
    paho_package = types.ModuleType("paho")
    paho_package.mqtt = mqtt_package
    monkeypatch.setitem(sys.modules, "paho", paho_package)
    monkeypatch.setitem(sys.modules, "paho.mqtt", mqtt_package)
    monkeypatch.setitem(sys.modules, "paho.mqtt.publish", publish_module)

    messages = [{"topic": "t", "payload": "p", "retain": True}]
    plugin.publish(
        messages,
        {
            "host": "broker.example",
            "port": 8883,
            "username": "<MQTT_USER>",
            "password": "<MQTT_PASSWORD>",
            "tls": True,
        },
    )

    assert len(calls) == 1
    sent, kwargs = calls[0]
    assert sent == messages
    assert kwargs["hostname"] == "broker.example"
    assert kwargs["port"] == 8883
    assert kwargs["auth"] == {"username": "<MQTT_USER>", "password": "<MQTT_PASSWORD>"}
    assert kwargs["tls"] == {}


def test_plugin_version_sensor_is_in_discovery_and_state(plugin):
    """Test Plugin Version is discovered as a sensor and set in the state."""
    messages = by_topic(
        plugin.build_messages(str(SERIAL), FQDN, {}, [], last_update=LAST_UPDATE)
    )

    config = json.loads(
        messages[f"homeassistant/sensor/bigfix_{SERIAL}/plugin_version/config"][
            "payload"
        ]
    )
    assert config["name"] == "Plugin Version"
    assert config["value_template"] == "{{ value_json.plugin_version }}"
    assert "state_class" not in config

    state = json.loads(messages[f"bigfix/{SERIAL}/state"]["payload"])
    assert state["plugin_version"] == plugin.__version__


def test_publish_requires_host(plugin):
    """Test a config without a broker host fails clearly."""
    with pytest.raises(ValueError, match="host"):
        plugin.publish([], {"port": 1883})


def run_main_with_fakes(plugin, monkeypatch, publish):
    """Run main() with fake BigFix, config and publish, return the call log."""
    calls = []

    @contextlib.contextmanager
    def fake_init_plugin(_version):
        yield None, FakeConnection(
            {plugin.MASTHEAD_RELEVANCE: [[SERIAL, FQDN]], "number of bes users": [10]},
            {"serverinfo": '{"version": "11.0.6.137"}'},
        )

    def fake_get_plugin_config(*_args, **kwargs):
        calls.append(("config", kwargs.get("secret_keys")))
        return {
            "mqtt": {"host": "broker", "password": "plaintextpw"},
            "sensors": [{"name": "Users", "relevance": "number of bes users"}],
        }

    def fake_publish(messages, mqtt_config):
        calls.append("publish")
        publish(messages, mqtt_config)

    def fake_protect(secret_keys, *_args, **_kwargs):
        calls.append(("protect", secret_keys))
        return []

    utils = plugin.besapi.plugin_utilities
    monkeypatch.setattr(utils, "init_plugin", fake_init_plugin)
    monkeypatch.setattr(utils, "get_plugin_config", fake_get_plugin_config)
    monkeypatch.setattr(utils, "protect_plugin_config_secrets", fake_protect)
    monkeypatch.setattr(plugin, "publish", fake_publish)

    return calls, plugin.main


def test_main_protects_secrets_after_publish(plugin, monkeypatch):
    """Test the plaintext password is only encrypted once it has worked."""
    calls, main = run_main_with_fakes(plugin, monkeypatch, lambda *_a: None)

    assert main() == 0

    assert ("mqtt", "password") in plugin.SECRET_CONFIG_KEYS
    assert calls == [
        # decrypts an already encrypted password:
        ("config", plugin.SECRET_CONFIG_KEYS),
        "publish",
        ("protect", plugin.SECRET_CONFIG_KEYS),
    ]


def test_main_does_not_protect_secrets_when_publish_fails(plugin, monkeypatch):
    """Test a password the broker rejected is left as plaintext to fix."""

    def failing_publish(*_args):
        raise ConnectionRefusedError("not authorised")

    calls, main = run_main_with_fakes(plugin, monkeypatch, failing_publish)

    with pytest.raises(ConnectionRefusedError):
        main()

    assert calls == [("config", plugin.SECRET_CONFIG_KEYS), "publish"]
