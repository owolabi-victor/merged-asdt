"""IrrigationActuatorBridge — MQTT publish for a physical valve controller.

Set DT_USE_PHYSICAL_ACTUATORS=true in bpdt.env to enable.
When enabled this class publishes every significant irrigation-rate change to:

    dt/<asset_id>/actuator/irrigation
    (override with DT_ACTUATOR_TOPIC_IRRIGATION)

Message schema:
    {"rate_L_hr": 5.2, "active": true, "source": "bpdt_pid"}

The physical valve controller subscribes to this topic and opens or closes the
valve proportionally.  When active=false the controller should close the valve
and the rate_L_hr is 0.0.

Physical sensor → digital twin path (read):
    Real sensors publish to:  dt/<asset_id>/sensors/<field>
    Simulator subscribes when DT_USE_PHYSICAL_SENSORS=true

Digital twin → physical actuator path (write, this module):
    PID output publishes to:  dt/<asset_id>/actuator/irrigation
    Valve controller subscribes and actuates
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dt_forge.core.config import TwinConfig

log = logging.getLogger(__name__)


class IrrigationActuatorBridge:
    """
    Optional bridge that publishes PID irrigation commands to a physical valve
    controller via MQTT.

    When ``DT_USE_PHYSICAL_ACTUATORS`` is not set (or set to ``false``), all
    methods are no-ops so the bridge can always be created without side effects.
    """

    def __init__(self, config: "TwinConfig"):
        self._enabled = os.getenv("DT_USE_PHYSICAL_ACTUATORS", "false").lower() == "true"
        self._topic = os.getenv(
            "DT_ACTUATOR_TOPIC_IRRIGATION",
            f"dt/{config.asset_id}/actuator/irrigation",
        )
        self._transport = None
        self._last_rate: float = -1.0   # sentinel — forces first publish

        if self._enabled:
            try:
                from dt_forge.network.transport import MQTTTransport
                self._transport = MQTTTransport(config)
                self._transport.connect()
                log.info(
                    "IrrigationActuatorBridge enabled — publishing to '%s'",
                    self._topic,
                )
            except Exception as e:
                log.warning("IrrigationActuatorBridge MQTT setup failed: %s", e)
                self._enabled = False

    def publish(self, rate_L_hr: float) -> None:
        """Publish irrigation rate if it has changed by more than 0.01 L/hr."""
        if not self._enabled or self._transport is None:
            return
        if abs(rate_L_hr - self._last_rate) < 0.01:
            return
        try:
            self._transport.publish(self._topic, {
                "rate_L_hr": round(rate_L_hr, 2),
                "active":    rate_L_hr > 0.0,
                "source":    "bpdt_pid",
            })
            self._last_rate = rate_L_hr
            log.debug("ActuatorBridge: irrigation=%.2f L/hr", rate_L_hr)
        except Exception as e:
            log.warning("ActuatorBridge publish error: %s", e)

    def close(self) -> None:
        if self._transport:
            try:
                self._transport.disconnect()
            except Exception:
                pass
