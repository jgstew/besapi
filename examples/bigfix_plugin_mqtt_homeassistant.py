# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "besapi[plugins]>=4.4.1",
#     "paho-mqtt>=2.0",
# ]
#
# [tool.uv]
# # supply chain: skip releases under a week old, except besapi itself:
# exclude-newer = "7 days"
# exclude-newer-package = { besapi = false }
# ///
"""
Publish BigFix server info to MQTT so Home Assistant adds it as a device.

Each run queries BigFix with session relevance and publishes, all retained:
- a Home Assistant MQTT discovery config per sensor, so the BigFix server
  shows up automatically as one device, keyed by the masthead serial number
  and named after the masthead FQDN
- one JSON state message holding every sensor's current value, plus a
  Last Update timestamp of when this run published it

requires `besapi[plugins]` and `paho-mqtt`, install with command:
`pip install besapi[plugins] paho-mqtt`

or run it with its PEP 723 dependencies installed automatically:
`uv run bigfix_plugin_mqtt_homeassistant.py`

MQTT settings are read from `bigfix_plugin_mqtt_homeassistant.config.yaml`
next to this script. Copy `bigfix_plugin_mqtt_homeassistant.config.example.yaml`
to that name and fill in the real broker and creds.

When run as a BigFix Server Plugin Service on the root server, a plaintext
mqtt password in the config file is replaced with an encrypted one after the
first successful publish, using the same method as the server's own REST API
password (CryptoUtility on Linux, DPAPI on Windows). A password the broker
rejects stays plaintext. To change it later, put the new plaintext password
in the config file again.

Example Usage:
python bigfix_plugin_mqtt_homeassistant.py -r https://localhost:52311/api -u API_USER -p API_PASSWORD

Example Usage with besapi config file:
python bigfix_plugin_mqtt_homeassistant.py

This can also be run as a BigFix Server Plugin Service.

References:
- https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery
- https://eclipse.dev/paho/files/paho.mqtt.python/html/client.html#publishing
"""

import datetime
import json
import logging
import re

import besapi
import besapi.plugin_utilities

__version__ = "1.0.0"

# masthead serial number and the FQDN from the masthead gather url:
MASTHEAD_RELEVANCE = (
    '(site number of it, preceding text of first ":" of following text of '
    'first "://" of gather url of it) of bes license'
)

# high severity patches with an action, relevant on computers seen in 60 days.
# NOTE: `and` short circuits left to right, clauses are ordered fastest first:
# the first 4 do most of the filtering, so the costly severity check only runs
# on a few hundred fixlets, not every fixlet.
CRITICAL_PATCHES_CLAUSES = [
    "fixlet flag of it",
    # cheap guard, keeps fixlets with no applicable computers cheap at scale:
    "applicable computer count of it > 0",
    "exists applicable computers whose (now - last report time of it < 60 * day) of it",
    'exists (name of site of it) whose (it = "Enterprise Security"'
    ' or it starts with "Updates for" or it starts with "Patches for")',
    "exists (source severity of it as lowercase) whose"
    ' (it is contained by set of ("high";"important";"critical"))',
    "exists default action of it",
    "globally visible flag of it",
    'name of it does not contain "(Superseded)"',
]
CRITICAL_PATCHES_RELEVANCE = (
    "number of bes fixlets whose (" + " AND ".join(CRITICAL_PATCHES_CLAUSES) + ")"
)

# each becomes a Home Assistant sensor, override with `sensors:` in the config:
DEFAULT_SENSORS = [
    {
        "name": "Computers",
        "relevance": (
            "number of bes computers whose (last report time of it >= now - 45 * day)"
        ),
    },
    {
        "name": "Actions",
        "relevance": 'number of bes actions whose (state of it = "Open")',
    },
    {"name": "Users", "relevance": "number of bes users"},
    {
        "name": "Computers Not Reporting",
        "relevance": (
            "number of bes computers whose (last report time of it < now - 45 * day)"
        ),
    },
    {
        # approximate freshness of the data within BigFix itself:
        "name": "Data Age",
        "relevance": (
            "((now - it) / second) of maximum of last report times of bes computers"
        ),
        "device_class": "duration",
        "unit": "s",
    },
    {"name": "Relevant Critical Patches", "relevance": CRITICAL_PATCHES_RELEVANCE},
]

# decrypted when loading the config, and encrypted in the file if plaintext
# once publishing with it has worked:
SECRET_CONFIG_KEYS = [("mqtt", "password")]

# always added, the time this run published to MQTT:
LAST_UPDATE_SENSOR = {"name": "Last Update", "device_class": "timestamp"}

# always added, the version of this plugin that published the data:
PLUGIN_VERSION_SENSOR = {"name": "Plugin Version"}


def sensor_key(sensor):
    """Get the id used in topics, unique ids and the state JSON for a sensor."""
    return sensor.get("key") or re.sub(r"[^a-z0-9]+", "_", sensor["name"].lower())


def get_bigfix_info(bes_conn, sensors):
    """Query the masthead serial, masthead FQDN and each sensor's value."""
    serial, fqdn = bes_conn.session_relevance_json(MASTHEAD_RELEVANCE)["result"][0]

    values = {}
    for sensor in sensors:
        result = bes_conn.session_relevance_json(sensor["relevance"])["result"]
        values[sensor_key(sensor)] = result[0] if result else None

    return str(serial), fqdn, values


def get_root_server_version(bes_conn):
    """Query the root server's own installed BigFix version."""
    return json.loads(bes_conn.get("serverinfo").text)["version"]


def build_messages(
    serial,
    fqdn,
    values,
    sensors,
    discovery_prefix="homeassistant",
    base_topic="bigfix",
    last_update=None,
    sw_version=None,
):
    """Build the retained discovery and state messages for paho publish.multiple.

    Arguments:
        last_update: timezone aware datetime for the Last Update sensor,
            defaults to now.
        sw_version: the root server's own version, from
            get_root_server_version(), omitted from the device if not given.
    """
    last_update = last_update or datetime.datetime.now(datetime.timezone.utc)
    device_id = f"bigfix_{serial}"
    state_topic = f"{base_topic}/{serial}/state"
    device = {
        "identifiers": [device_id],
        "name": fqdn,
        "manufacturer": "HCL BigFix",
        "model": "BigFix Server",
        "serial_number": serial,
        # HA shows this as a link on the device page, surfacing the FQDN there too:
        "configuration_url": f"https://{fqdn}:52311/api/help",
    }
    if sw_version:
        device["sw_version"] = sw_version

    messages = []
    for sensor in [*sensors, LAST_UPDATE_SENSOR, PLUGIN_VERSION_SENSOR]:
        key = sensor_key(sensor)
        config = {
            "name": sensor["name"],
            "unique_id": f"{device_id}_{key}",
            "state_topic": state_topic,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "device": device,
            "origin": {"name": "besapi", "sw_version": __version__},
        }
        if sensor.get("device_class"):
            config["device_class"] = sensor["device_class"]
        if sensor.get("unit"):
            config["unit_of_measurement"] = sensor["unit"]
        # HA only graphs numbers with a state_class, and rejects it on a
        # timestamp or non-numeric sensor like Plugin Version:
        if (
            sensor.get("device_class") != "timestamp"
            and sensor is not PLUGIN_VERSION_SENSOR
        ):
            config["state_class"] = "measurement"
        messages.append(
            {
                "topic": f"{discovery_prefix}/sensor/{device_id}/{key}/config",
                "payload": json.dumps(config),
                "retain": True,
            }
        )

    state = {
        **values,
        sensor_key(LAST_UPDATE_SENSOR): last_update.isoformat(timespec="seconds"),
        sensor_key(PLUGIN_VERSION_SENSOR): __version__,
    }
    messages.append(
        {"topic": state_topic, "payload": json.dumps(state), "retain": True}
    )
    return messages


def publish(messages, mqtt_config):
    """Publish all messages to the broker in the `mqtt:` config section."""
    if not mqtt_config.get("host"):
        raise ValueError("mqtt `host` is required in the plugin config file")

    import paho.mqtt.publish  # pylint: disable=import-outside-toplevel

    auth = None
    if mqtt_config.get("username"):
        auth = {
            "username": mqtt_config["username"],
            "password": mqtt_config.get("password"),
        }

    logging.info("publishing %d messages to %s", len(messages), mqtt_config.get("host"))
    paho.mqtt.publish.multiple(
        messages,
        hostname=mqtt_config["host"],
        port=int(mqtt_config.get("port", 1883)),
        client_id=mqtt_config.get("client_id", ""),
        auth=auth,
        # {} means TLS using the system CA certs:
        tls={} if mqtt_config.get("tls") else None,
    )


def main():
    """Execution starts here."""
    with besapi.plugin_utilities.init_plugin(__version__) as (_args, bes_conn):
        config = besapi.plugin_utilities.get_plugin_config(
            secret_keys=SECRET_CONFIG_KEYS
        )
        mqtt_config = config["mqtt"]
        sensors = config.get("sensors") or DEFAULT_SENSORS

        serial, fqdn, values = get_bigfix_info(bes_conn, sensors)
        sw_version = get_root_server_version(bes_conn)
        logging.info("masthead %s (%s): %s", serial, fqdn, values)

        publish(
            build_messages(
                serial,
                fqdn,
                values,
                sensors,
                discovery_prefix=mqtt_config.get("discovery_prefix", "homeassistant"),
                base_topic=mqtt_config.get("base_topic", "bigfix"),
                sw_version=sw_version,
            ),
            mqtt_config,
        )

        # only now that the broker accepted it, encrypt a plaintext password:
        besapi.plugin_utilities.protect_plugin_config_secrets(SECRET_CONFIG_KEYS)

    return 0


if __name__ == "__main__":
    main()
