import json
import logging
from typing import Dict, Any, Optional

from hardware.interfaces.acquisition import IDataAcquisition
from hardware.interfaces.timing import ITimingController
from hardware.interfaces.gain import IGainController

logger = logging.getLogger(__name__)

class HardwareManager:
    """
    Central manager for hardware connections and capability routing.
    Parses hardware_config.json to map roles to specific devices.
    """
    
    def __init__(self):
        self.config: Dict[str, Any] = {}
        self.devices: Dict[str, Any] = {}  # instantiated physical devices
        
        self.acquisition: Optional[IDataAcquisition] = None
        self.timing: Optional[ITimingController] = None
        self.gain_ch1: Optional[IGainController] = None
        self.gain_ch2: Optional[IGainController] = None
        
        # Computed dynamic gain combinations
        self.gain_map_ch1: list[dict] = []
        self.gain_map_ch2: list[dict] = []
        
    def load_config(self, filepath: str):
        with open(filepath, 'r') as f:
            self.config = json.load(f)
            
        self._calculate_gains()
        self._instantiate_devices()
        
    def _calculate_gains(self):
        """Calculates dynamic gain combinations based on the physical circuit definition in the config."""
        gain_settings = self.config.get("gain_settings", {})
        
        self.gain_map_ch1 = self._calc_channel_gains(gain_settings.get("ch1", {}))
        self.gain_map_ch2 = self._calc_channel_gains(gain_settings.get("ch2", {}))
        
    def _calc_channel_gains(self, ch_config: dict) -> list[dict]:
        if not ch_config:
            return []
            
        fixed_c = ch_config.get("fixed_capacitor_pF", 0.0)
        switchable = ch_config.get("switchable_capacitors", [])
        n_switches = len(switchable)
        
        if n_switches == 0:
            if fixed_c > 0.0:
                return [{"label": "1x", "capacitance": fixed_c, "gpio_mask": []}]
            return []
            
        combinations = []
        for i in range(1 << n_switches):
            mask = [(i >> bit) & 1 == 1 for bit in range(n_switches)]
            
            c_total = fixed_c
            for bit_idx, is_on in enumerate(mask):
                if is_on:
                    c_total += switchable[bit_idx].get("value_pF", 0.0)
                    
            if c_total > 0.0:
                combinations.append({
                    "capacitance": c_total,
                    "gpio_mask": mask
                })
            
        # Sort by capacitance descending (largest C = smallest voltage = lowest gain)
        combinations.sort(key=lambda x: x["capacitance"], reverse=True)
        
        if combinations:
            c_max = combinations[0]["capacitance"]
            for combo in combinations:
                gain_val = c_max / combo["capacitance"]
                if gain_val == 1.0:
                    combo["label"] = "1x"
                else:
                    # format nicely, e.g. 10x, 2x, etc.
                    combo["label"] = f"{gain_val:g}x"
                    
        return combinations

    def _instantiate_devices(self):
        from hardware.pico.pico_daq import PicoSerialDAQ
        from hardware.pic.pic_daq import PICSerialDAQ
        
        # Factory map
        class_map = {
            "PicoSerialDAQ": PicoSerialDAQ,
            "PICSerialDAQ": PICSerialDAQ
        }
        
        # Instantiate devices
        devices_config = self.config.get("devices", {})
        for dev_id, dev_info in devices_config.items():
            cls_name = dev_info.get("class")
            if cls_name in class_map:
                port = dev_info.get("port")
                self.devices[dev_id] = class_map[cls_name](port=port)
            else:
                logger.error(f"Hardware class {cls_name} not found in factory map.")
                
        # Map roles
        roles_config = self.config.get("roles", {})
        
        acq_id = roles_config.get("acquisition")
        if acq_id in self.devices:
            self.acquisition = self.devices[acq_id]
            
        timing_id = roles_config.get("timing")
        if timing_id in self.devices:
            self.timing = self.devices[timing_id]
            
        gain_ch1_id = roles_config.get("gain_ch1")
        if gain_ch1_id in self.devices:
            self.gain_ch1 = self.devices[gain_ch1_id]
            
        gain_ch2_id = roles_config.get("gain_ch2")
        if gain_ch2_id in self.devices:
            self.gain_ch2 = self.devices[gain_ch2_id]
            
    def connect_all(self) -> list[str]:
        """
        Attempts to connect all registered devices.
        Returns a list of device IDs that failed to connect.
        """
        failed = []
        for dev_id, device in self.devices.items():
            if hasattr(device, 'connect'):
                success = device.connect()
                if not success:
                    failed.append(dev_id)
            else:
                logger.warning(f"Device {dev_id} does not have a connect method.")
    def disconnect_all(self):
        for dev_id, device in self.devices.items():
            if hasattr(device, 'disconnect'):
                device.disconnect()
                
    def set_device_port(self, dev_id: str, port: str):
        if dev_id in self.devices:
            device = self.devices[dev_id]
            if hasattr(device, '_port'):
                device._port = port
            else:
                logger.warning(f"Device {dev_id} does not support port assignment.")
        else:
            logger.error(f"Device {dev_id} not found.")

    def get_adc_settings(self) -> dict:
        if self.acquisition:
            return self.acquisition.get_adc_settings()
        return {}

    def get_gain_labels(self, channel: int) -> list[str]:
        if channel == 1:
            return [g["label"] for g in self.gain_map_ch1]
        else:
            return [g["label"] for g in self.gain_map_ch2]
            
    def apply_gain(self, channel: int, label: str) -> bool:
        map_to_use = self.gain_map_ch1 if channel == 1 else self.gain_map_ch2
        controller = self.gain_ch1 if channel == 1 else self.gain_ch2
        
        if not controller:
            logger.warning(f"No gain controller assigned for CH{channel}")
            return False
            
        for combo in map_to_use:
            if combo["label"] == label:
                return controller.set_gpio_mask(channel, combo["gpio_mask"])
                
        logger.error(f"Gain label {label} not found for CH{channel}")
        return False

    def get_capacitance(self, channel: int, label: str) -> float:
        map_to_use = self.gain_map_ch1 if channel == 1 else self.gain_map_ch2
        for combo in map_to_use:
            if combo["label"] == label:
                return combo["capacitance"]
        return 0.0
