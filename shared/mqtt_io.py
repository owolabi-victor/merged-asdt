# shared/mqtt_io.py
"""
One place to open an MQTT connection.

Every layer previously called `client.connect(MQTT_BROKER, MQTT_PORT)` directly,
which works against a local anonymous broker and fails against every hosted one:
managed brokers are TLS-only on 8883 and require a username and password.

Routing all layers through connect() means moving the whole twin to a hosted
broker is an environment change, not an edit in six files.
"""
import ssl

import paho.mqtt.client as mqtt

from shared.config import (
    MQTT_BROKER, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD, MQTT_TLS,
)


def make_client(client_id: str) -> mqtt.Client:
    """A client configured for whichever broker the environment points at."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id)

    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    # Port 8883 implies TLS even when MQTT_TLS was not set explicitly, which is
    # the mistake that otherwise produces a silent connection timeout.
    if MQTT_TLS or MQTT_PORT == 8883:
        client.tls_set(tls_version=ssl.PROTOCOL_TLSv1_2)

    return client


def connect(client_id: str) -> mqtt.Client:
    """Create a client and connect it to the configured broker."""
    client = make_client(client_id)
    client.connect(MQTT_BROKER, MQTT_PORT)
    return client
