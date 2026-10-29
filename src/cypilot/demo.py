# SPDX-License-Identifier: Apache-2.0
"""Simulated device node for trying cypilot without hardware and for the tests (from the cymon tool).

It behaves like a small drive with cymon-lib:

- registers ``demo.*`` (amplitude, frequency, read-only counters) and the
  cymon service port registers ``uavcan.srv.cymon_*.{id,type}``;
- the five cymon services, with the capture logic of cymon-lib
  (decimation, pre-trigger, edge/hysteresis/immediate trigger) on a virtual
  tick of ``tick_period_us``;
- ``uavcan.primitive.array.Real32.1.0`` telemetry [0.5 Hz sine, cosine, i_q]
  on subject 1000 at 20 Hz;
- a readiness subscription (``reg.udral.service.common.Readiness.0.1`` on
  subject 106) that engages only while refreshed within 0.5 s, like UNIMOC;
- for cypilot's control it passes as a UNIMOC drive: the probe register
  ``unimoc.settings.pending`` and the vendor status bits of the heartbeat
  (power stage enabled and engaged while engaged, E-stop latched);
- ExecuteCommand (restart, store, emergency stop) and diagnostic records;
- UNIMOC's startup aid: the commissioning commands, the status message
  ``unimoc.drive.CommissioningStatus.0.1`` on subject 215 and the result
  registers (:class:`DemoStartup`).

Import only after :func:`cypilot.dsdl.ensure_dsdl`.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from typing import Any

import cymon as cymon_dsdl
import pycyphal.application
import pycyphal.dsdl
import pycyphal.transport
import reg.udral.service.common
import uavcan.diagnostic
import uavcan.node
import uavcan.primitive.array
from pycyphal.application.register import Bit, Natural8, Natural16, Real32, String, Value

from cypilot import __version__

SERVICE_IDS = {
    "cymon_variables": 120,
    "cymon_buffer_info": 121,
    "cymon_setup": 122,
    "cymon_arm": 123,
    "cymon_read": 124,
}
TELEMETRY_SUBJECT = 1000
READINESS_SUBJECT = 106
COMMISSIONING_SUBJECT = 215
ENGAGED = 3

IDLE, CONFIGURED, ARMED, TRIGGERED, COMPLETE = range(5)
RISING, FALLING, HYSTERESIS, IMMEDIATE = range(4)


class CaptureEngine:
    """Python port of cymon::Device (capture part), driven by Tick()."""

    def __init__(
        self,
        variables: list[tuple[str, str]],
        read: Any,
        tick_period_us: float,
        buffer_floats: int = 1024,
        max_channels: int = 8,
    ) -> None:
        self.variables = variables
        self.read = read
        self.tick_period_us = tick_period_us
        self.buffer_floats = buffer_floats
        self.max_channels = max_channels
        self.state = IDLE
        self.channels: list[int] = []
        self.num_samples = 0
        self.pretrigger = 0
        self.decimation = 1
        self.period_us = 0.0
        self.counter = 0
        self.pre: list[list[float]] = []
        self.post: list[list[float]] = []
        self.mode = RISING
        self.source = 0
        self.level = 0.0
        self.band = 0.0
        self.prev = 0.0
        self.awaiting_low = False
        self.trigger_requested = False
        self.trigger_event: tuple[int, int, float] | None = None
        """Source, mode and value of the last trigger, until published as ``cymon.TriggerEvent``."""

    def setup(
        self, channels: list[int], period_us: float, num_samples: int, pretrigger: int
    ) -> tuple[int, float]:
        if self.state in (ARMED, TRIGGERED):
            return 5, 0.0
        if not channels or len(channels) > self.max_channels or num_samples == 0 or pretrigger >= num_samples:
            return 9, 0.0
        if any(c >= len(self.variables) for c in channels):
            return 1, 0.0
        if num_samples > self.buffer_floats // len(channels):
            return 2, 0.0
        self.channels, self.num_samples, self.pretrigger = list(channels), num_samples, pretrigger
        self.decimation = (
            max(1, min(65535, int(period_us / self.tick_period_us + 0.5)))
            if period_us > self.tick_period_us
            else 1
        )
        self.period_us = self.decimation * self.tick_period_us
        self.state = CONFIGURED
        return 0, self.period_us

    def arm(self, arm: bool, source: int, mode: int, level: float, band: float) -> int:
        if self.state == IDLE:
            return 3 if arm else 0
        self.state = CONFIGURED
        if not arm:
            return 0
        if mode > IMMEDIATE:
            return 9
        if mode != IMMEDIATE and source >= len(self.variables):
            return 1
        self.mode, self.source, self.level, self.band = mode, source, level, band
        self.prev = self.read(source) if source < len(self.variables) else 0.0
        self.awaiting_low = mode == HYSTERESIS and self.prev >= level
        self.pre, self.post, self.counter, self.trigger_requested = [], [], 0, False
        self.trigger_event = None
        self.state = ARMED
        return 0

    def _fires(self, value: float) -> bool:
        if self.mode == IMMEDIATE:
            return True
        if self.mode == RISING:
            return self.prev < self.level <= value
        if self.mode == FALLING:
            return self.prev > self.level >= value
        if self.awaiting_low:
            if value < self.level - self.band:
                self.awaiting_low = False
            return False
        return self.prev < self.level <= value

    def tick(self) -> None:
        if self.state not in (ARMED, TRIGGERED):
            return
        self.counter += 1
        if self.counter < self.decimation:
            return
        self.counter = 0
        if self.state == ARMED:
            value = self.read(self.source) if self.source < len(self.variables) else 0.0
            filled = len(self.pre) >= self.pretrigger
            if filled and (self._fires(value) or self.trigger_requested):
                self.state = TRIGGERED
                self.trigger_event = (self.source, self.mode, value)
            self.prev = value
        frame = [self.read(c) for c in self.channels]
        if self.state == ARMED:
            self.pre.append(frame)
            if len(self.pre) > self.pretrigger:
                self.pre.pop(0)
        else:
            self.post.append(frame)
            if len(self.post) >= self.num_samples - self.pretrigger:
                self.state = COMPLETE

    def frames(self) -> list[list[float]]:
        missing = self.pretrigger - len(self.pre)
        return [[0.0] * len(self.channels)] * missing + self.pre + self.post


class DemoStartup:
    """UNIMOC's startup aid as the bus sees it, without measurements.

    The commissioning commands and the status message behave like the
    firmware: a run starts only while not engaged and without a latched
    E-stop, each step "measures" for :attr:`measure_time`, a continue is
    refused while the step measures or waits for its reading
    (DC_LINK_VOLTAGE_CHECK, CURRENT_SENSE_CALIBRATION) and needs
    ``motor-connected`` at CONNECT_MOTOR, DONE writes the calibration
    registers, an abort in a run goes to FAULT.
    """

    STEPS = 14
    DC_LINK, CONNECT_MOTOR, CURRENT_SENSE, DONE, FAULT = 7, 9, 11, 12, 13
    IDLE_STATE, MEASURING, WAITING, NEEDS_READING, DONE_STATE, FAILED, ABORTED = range(7)
    NEEDS_NONE, NEEDS_VDC, NEEDS_CURRENT, NEEDS_MOTOR = range(4)
    FAULT_ABORTED, FAULT_ESTOP = 1, 2
    START, CONTINUE, SET_READING, ABORT = 0x0200, 0x0202, 0x0203, 0x0204
    measure_time = 0.1

    def __init__(self, registers: Any, engaged: Any, estopped: Any) -> None:
        self._engaged = engaged
        self._estopped = estopped
        self.running = False
        self.started = False
        self.step = 0
        self.passed = 0
        self.fault = 0
        self.ext_current = 0.0
        self.ext_vdc = 0.0
        self.since = 0.0
        self.calibration = {
            "adc_offset_a": 0.0,
            "adc_offset_b": 0.0,
            "adc_offset_c": 0.0,
            "gain_a": 1.0,
            "gain_b": 1.0,
            "gain_c": 1.0,
            "gain_vdc": 1.0,
        }
        prefix = "unimoc.startup."
        registers[prefix + "results"] = lambda: Value(
            natural8=Natural8([self.passed >> step & 1 for step in range(self.STEPS)])
        )
        for name in self.calibration:
            registers[prefix + name] = lambda name=name: Value(real32=Real32([self.calibration[name]]))

    def handles(self, command: int) -> bool:
        return 0x0200 <= command <= 0x0205

    def command(self, command: int, parameter: str) -> tuple[int, str]:
        """An ExecuteCommand of the commissioning interface: the status and the output text."""
        Status = uavcan.node.ExecuteCommand_1.Response
        if command == self.START:
            if self._engaged():
                return Status.STATUS_BAD_STATE, "engaged: disengage first"
            if self._estopped():
                return Status.STATUS_BAD_STATE, "emergency stop latched: restart first"
            if self.running:
                return Status.STATUS_BAD_STATE, "the startup aid runs already"
            self.running, self.started, self.passed, self.fault = True, True, 0, 0
            self.ext_current = self.ext_vdc = 0.0
            self._enter(1)
            return Status.STATUS_SUCCESS, "started: phase 1 without the motor"
        if command == self.CONTINUE:
            return self._continue(parameter)
        if command == self.SET_READING:
            if not self.running:
                return Status.STATUS_BAD_STATE, "the startup aid is not running"
            name, _, text = parameter.partition("=")
            try:
                value = float(text)
            except ValueError:
                value = math.nan
            if not (math.isfinite(value) and 0.0 < value <= 1000.0):
                return Status.STATUS_BAD_PARAMETER, "not a positive number"
            if name == "vdc":
                self.ext_vdc = value
            elif name == "current":
                self.ext_current = value
            else:
                return Status.STATUS_BAD_PARAMETER, "expected vdc=<V> or current=<A>"
            return Status.STATUS_SUCCESS, ""
        if command == self.ABORT:
            self.abort()
            return Status.STATUS_SUCCESS, ""
        return Status.STATUS_BAD_COMMAND, "not implemented"

    def _continue(self, parameter: str) -> tuple[int, str]:
        Status = uavcan.node.ExecuteCommand_1.Response
        if parameter not in ("", "motor-connected"):
            return Status.STATUS_BAD_PARAMETER, "unknown confirmation"
        if not self.running:
            return Status.STATUS_BAD_STATE, "the startup aid is not running"
        if not self.measured():
            return Status.STATUS_BAD_STATE, "the step is still measuring"
        if self.needs() in (self.NEEDS_VDC, self.NEEDS_CURRENT):
            return Status.STATUS_BAD_STATE, "the step needs its meter reading"
        if self.step == self.CONNECT_MOTOR and parameter != "motor-connected":
            return Status.STATUS_BAD_PARAMETER, "confirm motor-connected"
        if self.step != self.CONNECT_MOTOR and parameter:
            return Status.STATUS_BAD_PARAMETER, "this step takes no confirmation"
        self.passed |= 1 << self.step
        if self.step + 1 >= self.DONE:
            self.calibration.update(adc_offset_a=0.01, adc_offset_b=-0.01, gain_a=1.02, gain_b=0.98)
            self.running, self.step = False, self.DONE
        else:
            self._enter(self.step + 1)
        return Status.STATUS_SUCCESS, ""

    def abort(self, fault: int = FAULT_ABORTED) -> None:
        if self.running:
            self.running, self.step, self.fault = False, self.FAULT, fault

    def _enter(self, step: int) -> None:
        self.step, self.since = step, time.monotonic()

    def measured(self) -> bool:
        return self.step == self.CONNECT_MOTOR or time.monotonic() - self.since >= self.measure_time

    def needs(self) -> int:
        if not self.running or not self.measured():
            return self.NEEDS_NONE
        if self.step == self.DC_LINK and self.ext_vdc < 1.0:
            return self.NEEDS_VDC
        if self.step == self.CURRENT_SENSE and self.ext_current < 1e-3:
            return self.NEEDS_CURRENT
        return self.NEEDS_MOTOR if self.step == self.CONNECT_MOTOR else self.NEEDS_NONE

    def status(self) -> Any:
        """The status message ``unimoc.drive.CommissioningStatus.0.1``."""
        import unimoc.drive

        if self.running:
            needs = self.needs()
            if needs in (self.NEEDS_VDC, self.NEEDS_CURRENT):
                state = self.NEEDS_READING
            else:
                state = self.WAITING if self.measured() else self.MEASURING
        elif self.step == self.DONE:
            state = self.DONE_STATE
        elif self.step == self.FAULT:
            state = self.ABORTED if self.fault in (self.FAULT_ABORTED, self.FAULT_ESTOP) else self.FAILED
        else:
            state = self.IDLE_STATE
        progress = 100 if self.measured() else int(100 * (time.monotonic() - self.since) / self.measure_time)
        return unimoc.drive.CommissioningStatus_0_1(
            procedure=1 if self.started else 0,
            step=self.step,
            state=state,
            needs=self.needs(),
            progress=progress if self.running else 0,
            passed=self.passed,
            failed=0,
            fault=self.fault,
            counter=0,
        )


class DemoDevice:
    def __init__(
        self,
        transport: pycyphal.transport.Transport,
        name: str = "org.tecnologic.cypilot.demo",
        tick_period_us: float = 100.0,
        trigger_subject: int = 65535,
    ) -> None:
        """``trigger_subject``: the ports of ``cymon.TriggerEvent`` (65535: disabled, as on UNIMOC)."""
        env = {f"UAVCAN__SRV__{k.upper()}__ID": str(v) for k, v in SERVICE_IDS.items()}
        env["UAVCAN__PUB__TELEMETRY__ID"] = str(TELEMETRY_SUBJECT)
        env["UAVCAN__SUB__READINESS__ID"] = str(READINESS_SUBJECT)
        env["UAVCAN__PUB__COMMISSIONING__ID"] = str(COMMISSIONING_SUBJECT)
        registry = pycyphal.application.make_registry(None, env)
        info = uavcan.node.GetInfo_1.Response(
            protocol_version=uavcan.node.Version_1(1, 0),
            software_version=uavcan.node.Version_1(*(int(part) for part in __version__.split(".")[:2])),
            hardware_version=uavcan.node.Version_1(1, 0),
            unique_id=hashlib.md5(name.encode()).digest(),
            name=name,
        )
        self.node = pycyphal.application.make_node(info, registry, transport=transport)
        self.node.heartbeat_publisher.mode = uavcan.node.Mode_1.OPERATIONAL
        self.amplitude = 1.0
        self.frequency = 50.0
        self.engaged = False
        self.estopped = False
        self.readiness_deadline = 0.0
        self.store_count = 0
        self.t = 0.0
        registers = self.node.registry
        registers["demo.amplitude"] = lambda: Value(real32=Real32([self.amplitude])), self._set_amplitude
        registers["demo.frequency"] = lambda: Value(real32=Real32([self.frequency])), self._set_frequency
        registers["demo.engaged"] = lambda: Value(natural16=Natural16([int(self.engaged)]))
        registers["demo.store_count"] = lambda: Value(natural16=Natural16([self.store_count]))
        registers["unimoc.settings.pending"] = lambda: Value(bit=Bit([False]))
        self.startup = DemoStartup(registers, lambda: self.engaged, lambda: self.estopped)
        for name in ("uavcan.pub.cymon_trigger", "uavcan.sub.cymon_trigger"):
            registers[f"{name}.id"] = Value(natural16=Natural16([trigger_subject]))
            registers[f"{name}.type"] = Value(string=String("cymon.TriggerEvent.0.1"))
        self._trigger_pub: Any = None
        self._trigger_sub: Any = None

        variables = [("sine", "A"), ("cosine", "A"), ("ramp", ""), ("engaged", "")]
        self.capture = CaptureEngine(variables, self._read_variable, tick_period_us)
        self._tick_period_s = tick_period_us * 1e-6
        self._tasks: list[asyncio.Task[None]] = []

        get = self.node.get_server
        get(cymon_dsdl.GetVariableList_0_1, "cymon_variables").serve_in_background(self._on_variables)
        get(cymon_dsdl.GetBufferInfo_0_1, "cymon_buffer_info").serve_in_background(self._on_buffer_info)
        get(cymon_dsdl.SetupCapture_0_1, "cymon_setup").serve_in_background(self._on_setup)
        get(cymon_dsdl.ArmTrigger_0_1, "cymon_arm").serve_in_background(self._on_arm)
        get(cymon_dsdl.ReadSamples_0_1, "cymon_read").serve_in_background(self._on_read)
        get(uavcan.node.ExecuteCommand_1).serve_in_background(self._on_command)
        self._telemetry = self.node.make_publisher(uavcan.primitive.array.Real32_1_0, "telemetry")
        self._readiness = self.node.make_subscriber(reg.udral.service.common.Readiness_0_1, "readiness")
        self._readiness.receive_in_background(self._on_readiness)
        self._log = self.node.make_publisher(uavcan.diagnostic.Record_1)
        import unimoc.drive

        self._commissioning = self.node.make_publisher(unimoc.drive.CommissioningStatus_0_1, "commissioning")
        self._commissioning_last: Any = None
        self._commissioning_due = 0.0

    def _set_amplitude(self, value: Value) -> None:
        self.amplitude = float(value.real32.value[0])

    def _set_frequency(self, value: Value) -> None:
        self.frequency = float(value.real32.value[0])

    def _read_variable(self, index: int) -> float:
        w = 2 * math.pi * self.frequency * self.t
        return [
            self.amplitude * math.sin(w),
            self.amplitude * math.cos(w),
            (self.t * 10.0) % 1.0,
            float(self.engaged),
        ][index]

    def _apply_trigger_ports(self) -> None:
        """(Re)creates the trigger ports from their registers, as UNIMOC does at start."""
        for port in (self._trigger_pub, self._trigger_sub):
            if port is not None:
                port.close()
        self._trigger_pub = self._trigger_sub = None
        registry = self.node.registry
        pub = int(registry["uavcan.pub.cymon_trigger.id"])
        sub = int(registry["uavcan.sub.cymon_trigger.id"])
        if pub <= 8191:
            self._trigger_pub = self.node.presentation.make_publisher(cymon_dsdl.TriggerEvent_0_1, pub)
        if sub <= 8191:
            self._trigger_sub = self.node.presentation.make_subscriber(cymon_dsdl.TriggerEvent_0_1, sub)
            self._trigger_sub.receive_in_background(self._on_trigger_event)

    async def _on_trigger_event(self, _: Any, transfer: Any) -> None:
        if transfer.source_node_id != self.node.id and self.capture.state == ARMED:
            self.capture.trigger_requested = True

    async def _publish_trigger(self) -> None:
        event, self.capture.trigger_event = self.capture.trigger_event, None
        if event is not None and self._trigger_pub is not None:
            source, mode, value = event
            await self._trigger_pub.publish(
                cymon_dsdl.TriggerEvent_0_1(
                    source_variable_id=source, mode=cymon_dsdl.TriggerMode_0_1(mode), trigger_value=value
                )
            )

    def start(self) -> None:
        self.node.start()
        self._apply_trigger_ports()
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._sampling()), loop.create_task(self._publishing())]

    def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        self.node.close()

    async def _sampling(self) -> None:
        """Advances the virtual tick in real time (in batches, like an interrupt that catches up)."""
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.005)
            now = time.monotonic()
            ticks = int((now - last) / self._tick_period_s)
            last += ticks * self._tick_period_s
            for _ in range(ticks):
                self.t += self._tick_period_s
                self.capture.tick()
            if self.capture.trigger_event is not None:
                await self._publish_trigger()

    async def _publishing(self) -> None:
        while True:
            await asyncio.sleep(0.05)
            if self.engaged and time.monotonic() > self.readiness_deadline:
                await self._set_engaged(False, "readiness timeout")
            self._update_status()
            await self._publish_commissioning()
            # Slow signals: the 20 Hz telemetry would alias the 50 Hz scope signal.
            w = 2 * math.pi * 0.5 * self.t
            values = [self.amplitude * math.sin(w), self.amplitude * math.cos(w), 5.0 * float(self.engaged)]
            await self._telemetry.publish(uavcan.primitive.array.Real32_1_0(values))

    async def _publish_commissioning(self) -> None:
        """On every change, at 2 Hz in a run and at 0.5 Hz otherwise, like UNIMOC."""
        status = self.startup.status()
        fields = pycyphal.dsdl.to_builtin(status)
        now = time.monotonic()
        if fields == self._commissioning_last and now < self._commissioning_due:
            return
        self._commissioning_last = fields
        self._commissioning_due = now + (0.5 if self.startup.running else 2.0)
        await self._commissioning.publish(status)

    def _update_status(self) -> None:
        """The UNIMOC vendor status bits: power stage (0..2), engaged (3), E-stop latched (5)."""
        code = (0x01 | 0x08) if self.engaged else (0x01 if self.startup.running else 0x00)
        if self.estopped:
            code |= 0x20
        self.node.heartbeat_publisher.vendor_specific_status_code = code

    async def _set_engaged(self, engaged: bool, why: str) -> None:
        if engaged != self.engaged:
            self.engaged = engaged
            self._update_status()
            text = f"{'engaged' if engaged else 'disengaged'}: {why}"
            await self._log.publish(
                uavcan.diagnostic.Record_1(
                    severity=uavcan.diagnostic.Severity_1(4 if not engaged else 2), text=text
                )
            )

    async def _on_readiness(self, msg: Any, _: pycyphal.transport.TransferFrom) -> None:
        if msg.value == ENGAGED and not self.estopped and not self.startup.running:
            self.readiness_deadline = time.monotonic() + 0.5
            await self._set_engaged(True, "readiness ENGAGED")
        else:
            await self._set_engaged(False, "readiness not ENGAGED")

    async def _on_command(self, request: Any, _: Any) -> Any:
        Status = uavcan.node.ExecuteCommand_1.Response
        if request.command == 65531:
            self.estopped = True
            self.startup.abort(DemoStartup.FAULT_ESTOP)
            self._update_status()
            await self._set_engaged(False, "emergency stop")
            return Status(status=Status.STATUS_SUCCESS)
        if request.command == 65530:
            if self.engaged:
                return Status(status=Status.STATUS_BAD_STATE)
            self.store_count += 1
            return Status(status=Status.STATUS_SUCCESS)
        if request.command == 65535:
            self.estopped = False
            self._update_status()
            self._apply_trigger_ports()
            return Status(status=Status.STATUS_SUCCESS)
        if self.startup.handles(request.command):
            status, output = self.startup.command(
                int(request.command), request.parameter.tobytes().decode(errors="replace")
            )
            return Status(status=status, output=output)
        return Status(status=Status.STATUS_BAD_COMMAND)

    async def _on_variables(self, request: Any, _: Any) -> Any:
        if request.index < len(self.capture.variables):
            name, unit = self.capture.variables[request.index]
            return cymon_dsdl.GetVariableList_0_1.Response(name=name, unit=unit)
        return cymon_dsdl.GetVariableList_0_1.Response()

    async def _on_buffer_info(self, _: Any, __: Any) -> Any:
        c = self.capture
        return cymon_dsdl.GetBufferInfo_0_1.Response(
            buffer_bytes=4 * c.buffer_floats,
            max_channels=c.max_channels,
            max_frames=c.buffer_floats,
            max_read_samples=64,
            tick_period_us=c.tick_period_us,
        )

    async def _on_setup(self, request: Any, _: Any) -> Any:
        code, period = self.capture.setup(
            [int(c) for c in request.channel_ids],
            float(request.sample_period_us),
            int(request.num_samples),
            int(request.pretrigger_samples),
        )
        return cymon_dsdl.SetupCapture_0_1.Response(
            result=cymon_dsdl.Error_0_1(code), actual_sample_period_us=period
        )

    async def _on_arm(self, request: Any, _: Any) -> Any:
        code = self.capture.arm(
            bool(request.arm),
            int(request.source_variable_id),
            int(request.mode.value),
            float(request.level),
            float(request.hysteresis_band),
        )
        return cymon_dsdl.ArmTrigger_0_1.Response(result=cymon_dsdl.Error_0_1(code))

    async def _on_read(self, request: Any, _: Any) -> Any:
        c = self.capture
        resp = cymon_dsdl.ReadSamples_0_1.Response(state=cymon_dsdl.CaptureState_0_1(c.state))
        if c.state == IDLE:
            return resp
        resp.num_channels, resp.num_samples, resp.pretrigger_samples, resp.sample_period_us = (
            len(c.channels),
            c.num_samples,
            c.pretrigger,
            c.period_us,
        )
        if c.state != COMPLETE:
            return resp
        per_response = 64 // len(c.channels)
        count = min(int(request.max_frames), per_response)
        frames = c.frames()[request.frame_offset : request.frame_offset + count]
        resp.samples = [v for f in frames for v in f]
        resp.end_of_data = request.frame_offset + len(frames) >= c.num_samples
        return resp
