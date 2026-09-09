from abc import ABC, abstractmethod


class IGainController(ABC):
    """
    Interface for hardware that controls the gain (via GPIO-switched
    capacitors) and the event mode of the electronics.

    The mask is device-centric: one boolean per GPIO pin declared for this
    device in hardware_config.json, in the declared pin order.  Which pin
    switches which capacitor is routing knowledge resolved by the
    HardwareManager from the config's connections — the driver only sets
    pin states.
    """

    @abstractmethod
    def set_gpio_mask(self, gpio_mask: list[bool]) -> bool:
        """
        Set the state of every declared GPIO pin of this device.

        Parameters
        ----------
        gpio_mask : list[bool]
            One boolean per declared pin, in the config's pin order.  The
            length must match the pin count the driver can drive; anything
            else is rejected (returns False).
        """
        pass

    @abstractmethod
    def set_event_mode(self, is_single_event: bool) -> bool:
        """
        Switch the electronics between single-event and multi-event mode.

        The SE/ME line belongs to the gain controllers: switching mode must
        be done on every gain controller so that all circuits receive the
        signal.  On the current hardware the line is low = ME, high = SE.
        """
        pass
