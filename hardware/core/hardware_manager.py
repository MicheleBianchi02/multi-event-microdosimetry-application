import logging
from typing import Any, Callable, Dict, Optional

from hardware.config import (
    ChannelGainModel,
    ConfigValidationError,
    HardwareConfig,
    build_channel_gain_model,
    channels_share_gain_device,
    combined_pin_states,
    compatible_gain_options,
    file_sha256,
    load_hardware_config,
)
from hardware.config.schema import FixedValue, ParameterBounds
from hardware.interfaces.acquisition import IDataAcquisition
from hardware.interfaces.timing import ITimingController, ActualTiming
from hardware.interfaces.gain import IGainController
from hardware.registry import DRIVER_REGISTRY, registry_descriptors

logger = logging.getLogger(__name__)


def persistent_port_name(dev_id: str) -> str:
    """Persistent port of a device, derived from its config id.

    The hardware config carries no port: the admin's udev rule maps each
    physical board (matched by USB serial number) to
    ``SYMLINK+="atomiqa/<device_id>"``, and the app opens ``/dev/atomiqa/<id>``.
    """
    return f"/dev/atomiqa/{dev_id}"


class HardwareManager:
    """
    Central manager for hardware connections and capability routing.

    Consumes a validated HardwareConfig (hardware_config.json): instantiates
    the devices through the driver registry, maps roles, and derives the
    gain tables from the circuit/connection wiring.  Without a loaded
    config no hardware is available (simulation/offline only).
    """

    def __init__(self):
        self.config: Optional[HardwareConfig] = None
        self.config_path: Optional[str] = None
        self.config_text: Optional[str] = None  # raw file text (archive provenance)
        self.config_sha256: Optional[str] = None
        self.devices: Dict[str, Any] = {}

        self.acquisition: Optional[IDataAcquisition] = None
        self.timing: Optional[ITimingController] = None
        self.gain_ch1: Optional[IGainController] = None
        self.gain_ch2: Optional[IGainController] = None

        self.gain_model_ch1: Optional[ChannelGainModel] = None
        self.gain_model_ch2: Optional[ChannelGainModel] = None
        self._selected_gain = {"ch1": None, "ch2": None}
        self._gain_state_unknown = False

        self._config_listeners: list[Callable] = []

    # ------------------------------------------------------------------
    # Config lifecycle
    # ------------------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        return self.config is not None

    def add_config_listener(self, callback: Callable) -> None:
        """Called (with the manager) whenever a config is loaded or unloaded."""
        self._config_listeners.append(callback)

    def load_config(self, filepath: str) -> HardwareConfig:
        """Load, validate and apply a hardware config file.

        Raises OSError / json errors on read problems and
        ConfigValidationError (with .errors) if the file is not acceptable.
        """
        with open(filepath, "r", encoding="utf-8") as f:
            text = f.read()
        config = load_hardware_config(filepath, registry_descriptors())
        self._apply_config(config, filepath, text)
        return config

    def _apply_config(self, config: HardwareConfig, path: Optional[str], text: Optional[str]) -> None:
        self.config = config
        self.config_path = path
        self.config_text = text
        self.config_sha256 = file_sha256(path) if path else None

        self.devices = {}
        for dev_id, device_config in config.devices.items():
            registered = DRIVER_REGISTRY.get(device_config.driver)
            if registered is None:  # already caught by validation; defensive
                logger.error("Driver %s not registered", device_config.driver)
                continue
            try:
                self.devices[dev_id] = registered.create(device_config.connection, device_config)
            except Exception:
                logger.exception("Failed to instantiate device %s", dev_id)
                continue
            # The persistent name is derived from the device id (the config
            # deliberately carries no port); the manual-connect dialog may
            # override it for the session.
            if device_config.connection.get("type") == "serial":
                self.devices[dev_id].set_connection_params(
                    persistent_port_name(dev_id),
                    device_config.connection.get("baud_rate", 115200),
                )

        self.acquisition = self.devices.get(config.roles.acquisition)
        self.timing = self.devices.get(config.roles.timing)
        self.gain_ch1 = self.devices.get(config.roles.gain_ch1)
        self.gain_ch2 = self.devices.get(config.roles.gain_ch2)

        # Instrument-style acquisition drivers map the config's channels
        # (with their termination) onto fixed input terminals; µC drivers
        # no-op (firmware with fixed inputs).  Missing termination defaults
        # to differential.
        if self.acquisition is not None and hasattr(self.acquisition, "configure_channels"):
            channel_plan = [
                (ch_id, ch.termination or "differential")
                for ch_id, ch in config.channels.items()
            ]
            try:
                if not self.acquisition.configure_channels(channel_plan):
                    logger.error("The acquisition driver rejected the channel plan %s", channel_plan)
            except Exception:
                logger.exception("configure_channels failed on the acquisition device")

        self.gain_model_ch1 = build_channel_gain_model(config, "ch1")
        self.gain_model_ch2 = build_channel_gain_model(config, "ch2")
        self._selected_gain = {"ch1": None, "ch2": None}

        for warning in config.warnings:
            logger.warning("hardware config: %s", warning)

        for listener in self._config_listeners:
            try:
                listener(self)
            except Exception:
                logger.exception("config listener failed")

    def unload_config(self) -> None:
        self.disconnect_all()
        listeners = self._config_listeners
        self.__init__()  # reset all state except listeners
        self._config_listeners = listeners
        self._notify_config_listeners()

    def _notify_config_listeners(self) -> None:
        for listener in self._config_listeners:
            try:
                listener(self)
            except Exception:
                logger.exception("config listener failed")

    # ------------------------------------------------------------------
    # Capability access for the UI
    # ------------------------------------------------------------------

    @property
    def supports_ramp_sampling(self) -> bool:
        return bool(self.timing and self.timing.supports_ramp_sampling)

    @property
    def active_channels(self) -> list:
        return list(self.config.channels) if self.config else []

    @property
    def max_channels(self) -> int:
        """How many channels the acquisition system can measure (B8 gating)."""
        if self.config and self.acquisition is not None:
            device = self.config.devices.get(self.config.roles.acquisition)
            if device and device.acquisition:
                return device.acquisition.num_channels
        return 0

    @property
    def channels_share_gain(self) -> bool:
        """True if the two channels' gains are physically coupled (ganged pins)."""
        return bool(self.config and channels_share_gain_device(self.config))

    @property
    def acquisition_is_virtual(self) -> bool:
        """True when the acquisition role is a dummy device (bench tests of
        the timing/gain interfaces; no data is collected)."""
        return bool(getattr(self.acquisition, "IS_VIRTUAL", False))

    @property
    def acquisition_max_rate_hz(self) -> Optional[float]:
        """Per-channel stream-rate limit of the acquisition driver (from its
        registry descriptor), used to gate the ramp dt at apply time:
        1/dt must not exceed it.  None = the driver declares no limit."""
        if self.config and self.acquisition is not None:
            device = self.config.devices.get(self.config.roles.acquisition)
            if device is not None:
                registered = DRIVER_REGISTRY.get(device.driver)
                if registered is not None:
                    return registered.descriptor.acq_max_rate_hz
        return None

    @property
    def can_ramp_while_acquiring(self) -> bool:
        """Whether ramp sampling and acquisition can run concurrently.

        A firmware property of the timing device (hardcoded in the driver):
        e.g. the Pico cannot generate ramp triggers while its ADC is
        acquiring.  Only binding when the same physical device holds both
        the timing and the acquisition role."""
        if self.timing is None or self.acquisition is None:
            return True
        if self.timing is not self.acquisition:
            return True
        return getattr(self.timing, "SUPPORTS_RAMP_WHILE_ACQUIRING", True)

    def declared_timing_limits(self) -> Optional[dict]:
        """The declared envelope of the timing device, for the UI spinboxes."""
        if not self.config or self.timing is None:
            return None
        device = self.config.devices[self.config.roles.timing]
        if device.timing is None:
            return None

        def as_dict(param):
            if isinstance(param, ParameterBounds):
                return {"min": param.min, "max": param.max, "step": param.step}
            if isinstance(param, FixedValue):
                return {"value": param.value}
            return None

        ramp = device.timing.ramp_sampling
        return {
            "integration_time_us": as_dict(device.timing.integration_time_us),
            "reset_time_us": as_dict(device.timing.reset_time_us),
            "period_us": as_dict(device.timing.period_us),
            "ramp_sampling": {
                "supported": ramp.supported,
                "dt_us": as_dict(ramp.dt_us),
            },
        }

    def get_adc_settings(self) -> dict:
        """ADC properties of the acquisition device, from the loaded config."""
        if not self.config or self.acquisition is None:
            return {}
        device = self.config.devices.get(self.config.roles.acquisition)
        if device is None or device.acquisition is None:
            return {}
        adc = device.acquisition.adc
        return {
            "bits": adc.bits,
            "num_channels": device.acquisition.num_channels,
            "input_range_volts": {
                "min_volts": adc.effective_range().min_volts,
                "max_volts": adc.effective_range().max_volts,
            },
            "input_ranges_volts": [
                {"min_volts": r.min_volts, "max_volts": r.max_volts}
                for r in adc.input_ranges_volts
            ],
            "supports_oversampling": adc.supports_oversampling,
        }

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------

    def role_device_ids(self) -> list:
        """Unique device IDs referenced by the assigned roles (first-seen order)."""
        if not self.config:
            return []
        roles = self.config.roles
        return list(
            dict.fromkeys(
                dev_id
                for dev_id in (roles.timing, roles.acquisition, roles.gain_ch1, roles.gain_ch2)
                if dev_id is not None
            )
        )

    def connect_device(self, dev_id: str) -> tuple:
        """Connect one configured device; returns (ok, error message).

        A serial device with no port configured (empty in the config, not yet
        mapped in the manual dialog) is rejected instead of silently failing
        inside the driver.
        """
        device = self.devices.get(dev_id)
        if device is None:
            return False, f"device {dev_id!r} not found in the loaded configuration"
        if not hasattr(device, "connect"):
            return False, f"device {dev_id!r} does not support connecting"
        if self.config.devices[dev_id].connection.get("type") == "serial":
            # _port comes from the config (may be empty there) or from a
            # session-only manual mapping; a serial device without one cannot
            # connect (the driver would fail silently).
            if not getattr(device, "_port", None):
                return False, "no port configured — select one in Manual Connect"
        try:
            if device.connect():
                return True, ""
        except Exception as exc:
            logger.exception("connect failed for %s", dev_id)
            return False, str(exc)
        return False, "could not open the connection (port missing, busy or wrong device?)"

    def connect_all(self) -> Dict[str, str]:
        """Connect all configured devices; returns dev_id -> error message.

        An empty dict means every device is connected (auto-connect path).
        """
        failed = {}
        for dev_id in self.devices:
            ok, error = self.connect_device(dev_id)
            if not ok:
                failed[dev_id] = error
        return failed

    def connection_status(self) -> Dict[str, dict]:
        """Per-device connection snapshot for the connection card and dialog."""
        status = {}
        for dev_id, device_config in (self.config.devices.items() if self.config else {}):
            device = self.devices.get(dev_id)
            connection = device_config.connection
            status[dev_id] = {
                "name": device_config.name,
                "driver": device_config.driver,
                "transport": connection.get("type"),
                "port": getattr(device, "_port", None) if device else connection.get("port"),
                "baud_rate": connection.get("baud_rate", 115200) if connection.get("type") == "serial" else None,
                "connected": bool(device.is_connected) if (device and hasattr(device, "is_connected")) else False,
            }
        return status

    def device_roles(self, dev_id: str) -> list:
        """Roles assigned to one device, for display ('timing, gain_ch1', ...)."""
        if not self.config:
            return []
        roles = self.config.roles
        return [
            role_name
            for role_name, holder in (
                ("timing", roles.timing),
                ("gain_ch1", roles.gain_ch1),
                ("gain_ch2", roles.gain_ch2),
                ("acquisition", roles.acquisition),
            )
            if holder == dev_id
        ]

    def all_role_devices_connected(self) -> bool:
        """Whether every role-assigned device is connected (the wizard gate)."""
        status = self.connection_status()
        return bool(status) and all(status[dev_id]["connected"] for dev_id in self.role_device_ids())

    def disconnect_all(self) -> None:
        for dev_id, device in self.devices.items():
            if hasattr(device, "disconnect"):
                try:
                    device.disconnect()
                except Exception:
                    logger.exception("disconnect failed for %s", dev_id)

    def set_device_port(self, dev_id: str, port: str) -> bool:
        """Session-only endpoint override (manual connect); never persisted."""
        device = self.devices.get(dev_id)
        if device is None:
            logger.error("Device %s not found.", dev_id)
            return False
        if not hasattr(device, "set_connection_params"):
            logger.warning("Device %s does not support port assignment.", dev_id)
            return False
        device_config = self.config.devices[dev_id]
        device.set_connection_params(port, device_config.connection.get("baud_rate", 115200))
        return True

    # ------------------------------------------------------------------
    # Timing & Arming
    # ------------------------------------------------------------------

    def arm(self) -> bool:
        """Arm the timing hardware before measurement (acts as integrator reset)."""
        if self.timing is not None:
            return self.timing.arm()
        logger.error("No timing controller assigned to arm")
        return False

    def start_timing_signal(self) -> bool:
        """Start the timing engine when its start is NOT owned by the
        acquisition start (virtual acquisition on the bench, split setups):
        the measurement start button must still run the real timing signals,
        only nothing is recorded.  When one physical device holds both roles
        the acquisition start_measurement sends the start, so this is a
        no-op (a second start would be refused mid-run)."""
        timing = self.timing
        if timing is None:
            return False
        if timing is self.acquisition:
            return True
        return timing.start_timing()

    def stop_timing_signal(self) -> bool:
        """Symmetric stop for start_timing_signal()."""
        timing = self.timing
        if timing is None:
            return False
        if timing is self.acquisition:
            return True
        return timing.stop_timing()

    def set_timing_channels(self, multiple_channel: bool) -> bool:
        if self.timing is not None:
            return self.timing.set_channels(multiple_channel)
        return False

    def set_timing_mode(self, debug_mode: bool) -> bool:
        if self.timing is not None:
            return self.timing.set_mode(debug_mode)
        return False

    def calculate_actual_timing(
        self,
        int_time_us: float,
        reset_time_us: float,
        period_us: Optional[float] = None,
    ) -> Optional[ActualTiming]:
        """Pure quantization preview; injects the config's active channel
        count because firmware quantization may depend on it (PIC TX delay)."""
        if self.timing is not None:
            num_channels = len(self.active_channels) if self.config else 1
            return self.timing.calculate_actual_timing(int_time_us, reset_time_us, period_us, num_channels)
        return None

    def calculate_ramp_timing(
        self,
        ramp_dt_us: float,
        int_time_us: float,
        reset_time_us: float,
    ) -> Optional[ActualTiming]:
        """Ramp-aware preview from the driver's exact mirror (generator
        selection + applied quantized values — the numbers the firmware's
        'Q' reports).  None when the timing device has no ramp mirror
        (e.g. the PIC) or cannot represent the request."""
        if self.timing is None or not hasattr(self.timing, "calculate_ramp_timing"):
            return None
        try:
            return self.timing.calculate_ramp_timing(ramp_dt_us, int_time_us, reset_time_us)
        except ValueError as exc:
            logger.warning("ramp timing preview rejected: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Timing-mode routing (internal / ramp / window markers)
    # ------------------------------------------------------------------

    def timing_mode_for(self, sample_ramp: bool) -> str:
        """Which timing mode a measurement with this hardware uses.

        - 'ramp': the user requested ramp sampling;
        - 'markers': split hardware (the timing device does not hold the
          acquisition role) and it can emit triggers: with ramp sampling
          UNchecked the timing device still runs its ramp generator with
          dt = integration time — two CONV_START edges per window spaced
          by T_int (integration start / end) for the external digitizer;
        - 'internal': classic no-ramp on the device's own ADC (all-in-one
          setups, or a timing device without trigger capability — e.g. the
          PIC — which falls back with a warning).
        """
        if sample_ramp:
            return "ramp"
        if (
            self.timing is not None
            and self.timing is not self.acquisition
            and getattr(self.timing, "supports_ramp_sampling", False)
        ):
            return "markers"
        if self.timing is not None and self.timing is not self.acquisition:
            logger.warning(
                "the timing device cannot emit window markers — running "
                "without conversion triggers for the external digitizer"
            )
        return "internal"

    def configure_timing(
        self,
        sample_ramp: bool,
        ramp_dt_us: float,
        int_time_us: float,
        reset_time_us: float,
        period_us: Optional[float] = None,
    ) -> bool:
        """Configure the timing device for a measurement, routed by
        timing_mode_for(): ramp sampling as requested, window markers
        (ramp generator with dt = int) on split hardware without ramp
        sampling, classic no-ramp otherwise."""
        if self.timing is None:
            logger.error("No timing controller assigned")
            return False
        mode = self.timing_mode_for(sample_ramp)
        if mode == "ramp":
            return self.timing.configure_ramp(ramp_dt_us, int_time_us, reset_time_us, period_us)
        if mode == "markers":
            # dt = int: N = int/dt + 1 = 2 pulses per integration window
            return self.timing.configure_ramp(int_time_us, int_time_us, reset_time_us, period_us)
        return self.timing.configure_noramp(int_time_us, reset_time_us, period_us)

    def preview_timing(
        self,
        sample_ramp: bool,
        ramp_dt_us: Optional[float],
        int_time_us: float,
        reset_time_us: float,
        period_us: Optional[float] = None,
    ) -> tuple:
        """True-values preview routed like configure_timing(); returns
        (ActualTiming | None, mode).  The marker/ramp previews come from
        the driver's exact mirror (what the firmware's 'Q' reports).

        When the mirror REJECTS the request (e.g. markers mode with T_int
        beyond the deployed firmware's ramp-dt cap) the ValueError is
        re-raised — the caller must surface it.  Falling back to the
        no-ramp numbers here would show values that can never be applied,
        with only a generic failure at Apply time."""
        mode = self.timing_mode_for(sample_ramp)

        def ramp_preview(dt_us):
            mirror = getattr(self.timing, "calculate_ramp_timing", None)
            if mirror is None:
                return None
            try:
                return mirror(dt_us, int_time_us, reset_time_us)
            except ValueError as exc:
                logger.error("timing preview rejected: %s", exc)
                raise

        if mode == "ramp" and ramp_dt_us is not None:
            actual = ramp_preview(ramp_dt_us)
        elif mode == "markers":
            actual = ramp_preview(int_time_us)
        else:
            actual = self.calculate_actual_timing(int_time_us, reset_time_us, period_us)
        return actual, mode

    # ------------------------------------------------------------------
    # Acquisition settings
    # ------------------------------------------------------------------

    def set_input_range(self, min_volts: float, max_volts: float) -> bool:
        """Set active ADC input range on the acquisition device."""
        if self.acquisition is not None:
            return self.acquisition.set_input_range(min_volts, max_volts)
        return False

    # ------------------------------------------------------------------
    # Event mode (owned by the gain controllers, sent once per unique device)
    # ------------------------------------------------------------------

    def set_event_mode(self, is_single_event: bool) -> bool:
        success = True
        sent = False
        # Deduplicated in first-seen order: a shared uC is signaled once, and
        # with two distinct devices the command order is deterministic.
        unique_controllers = list(dict.fromkeys(c for c in (self.gain_ch1, self.gain_ch2) if c is not None))
        for controller in unique_controllers:
            sent = True
            if not controller.set_event_mode(is_single_event):
                success = False
        if not sent:
            logger.warning("set_event_mode: no gain controller assigned")
            return False
        return success

    # ------------------------------------------------------------------
    # Gain routing
    # ------------------------------------------------------------------

    def _gain_model(self, channel: int) -> Optional[ChannelGainModel]:
        return self.gain_model_ch1 if channel == 1 else self.gain_model_ch2

    def get_gain_labels(self, channel: int) -> list:
        model = self._gain_model(channel)
        return [g.label for g in model.options] if model else []

    def get_compatible_gain_labels(self, channel: int, other_channel_label: Optional[str] = None) -> list:
        """Return gain labels for `channel` compatible with `other_channel_label`."""
        model = self._gain_model(channel)
        if not model:
            return []
        other_channel = 2 if channel == 1 else 1
        other_model = self._gain_model(other_channel)
        other_opt = other_model.option_by_label(other_channel_label) if (other_model and other_channel_label) else None
        compatible = compatible_gain_options(model, other_opt)
        return [g.label for g in compatible]

    def get_capacitance(self, channel: int, label: str) -> float:
        model = self._gain_model(channel)
        if model:
            option = model.option_by_label(label)
            if option:
                return option.capacitance_pF
        return 0.0

    def get_gain_mask(self, channel: int, label: str) -> Optional[list]:
        """Dense gpio mask (per device pins) of one gain option of one channel."""
        model = self._gain_model(channel)
        if not model:
            return None
        option = model.option_by_label(label)
        if option is None:
            return None
        gain_dev_id = self.config.roles.gain_ch1 if channel == 1 else self.config.roles.gain_ch2
        device = self.config.devices[gain_dev_id]
        return list(option.mask_for(gain_dev_id, len(device.gain.pins)))

    def gains_conflict(self, ch1_label: str, ch2_label: str) -> bool:
        """Whether a pair of channel gain selections is physically impossible
        (different states requested on the same ganged pin)."""
        if not (self.gain_model_ch1 and self.gain_model_ch2):
            return False
        o1 = self.gain_model_ch1.option_by_label(ch1_label)
        o2 = self.gain_model_ch2.option_by_label(ch2_label)
        if o1 is None or o2 is None:
            return False
        return combined_pin_states([o1, o2]) is None

    def apply_gains(self, ch1_label: Optional[str] = None, ch2_label: Optional[str] = None) -> bool:
        """Atomically validate and apply gain selections for all active channels.

        The merged pin state for the pair is computed first; if any shared pin
        conflicts, the request is rejected without touching any hardware (B19).
        When valid, set_gpio_mask is called exactly once per unique device.
        If a device rejects the command after another device already switched,
        the bench gain state is unknowable: gain_state_unknown is raised and
        stays set until a fully successful re-apply.
        """
        opt1 = self.gain_model_ch1.option_by_label(ch1_label) if (self.gain_model_ch1 and ch1_label) else None
        opt2 = self.gain_model_ch2.option_by_label(ch2_label) if (self.gain_model_ch2 and ch2_label) else None

        if ch1_label and opt1 is None:
            logger.error("Gain label %r not found for CH1", ch1_label)
            return False
        if ch2_label and opt2 is None:
            logger.error("Gain label %r not found for CH2", ch2_label)
            return False

        selections = [opt for opt in (opt1, opt2) if opt is not None]
        merged = combined_pin_states(selections)
        if merged is None:
            logger.error(
                "Gain selection rejected: the channels request different states on "
                "a shared GPIO pin (ganged wiring) — select matching gains"
            )
            return False

        success = True
        for device_id, sparse_states in merged.items():
            device_config = self.config.devices[device_id]
            num_pins = len(device_config.gain.pins)
            dense = [sparse_states.get(i, False) for i in range(num_pins)]
            if not self.devices[device_id].set_gpio_mask(dense):
                success = False

        if success:
            self._gain_state_unknown = False
            if opt1 is not None:
                self._selected_gain["ch1"] = opt1
            if opt2 is not None:
                self._selected_gain["ch2"] = opt2
        else:
            self._gain_state_unknown = True
            logger.error(
                "Gain apply failed on one or more devices: the capacitors are in an "
                "unknown state — fix the connection and re-apply the configuration"
            )
        return success

    @property
    def gain_state_unknown(self) -> bool:
        """True after a partially failed gain apply, until a full re-apply succeeds."""
        return self._gain_state_unknown

    def apply_gain(self, channel: int, label: str) -> bool:
        """Select and apply the gain of one channel with atomic validation."""
        other_ch = "ch2" if channel == 1 else "ch1"
        other_opt = self._selected_gain.get(other_ch)
        other_label = other_opt.label if other_opt else None
        if channel == 1:
            return self.apply_gains(ch1_label=label, ch2_label=other_label)
        else:
            return self.apply_gains(ch1_label=other_label, ch2_label=label)

    # ------------------------------------------------------------------
    # Provenance for the measurement archives
    # ------------------------------------------------------------------

    def devices_summary(self) -> dict:
        """Resolved hardware identity, embedded into measurement settings.json."""
        if not self.config:
            return {}
        config = self.config
        return {
            "config_file": self.config_path,
            "config_sha256": self.config_sha256,
            "devices": {
                dev_id: {
                    "name": dev.name,
                    "driver": dev.driver,
                    "firmware_version": dev.firmware_version,
                }
                for dev_id, dev in config.devices.items()
            },
            "circuits": {
                c_id: {
                    "name": circuit.name,
                    "model": circuit.model,
                    "fixed_capacitance_pF": circuit.fixed_capacitance_pF,
                    "switchable_capacitors": [
                        {"id": c.id, "value_pF": c.value_pF} for c in circuit.switchable_capacitors
                    ],
                }
                for c_id, circuit in config.circuits.items()
            },
            "channels": {ch_id: ch.circuit for ch_id, ch in config.channels.items()},
            "roles": {
                "timing": config.roles.timing,
                "acquisition": config.roles.acquisition,
                "gain_ch1": config.roles.gain_ch1,
                "gain_ch2": config.roles.gain_ch2,
            },
        }
