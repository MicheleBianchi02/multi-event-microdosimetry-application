from abc import ABC, abstractmethod

class IGainController(ABC):
    """
    Interface for hardware that controls the gain (via GPIO-switched capacitors).
    """

    @abstractmethod
    def set_gpio_mask(self, channel: int, gpio_mask: list[bool]) -> bool:
        """
        Set the GPIO states for a specific channel to switch capacitors.
        
        Parameters
        ----------
        channel : int
            The channel to configure (e.g., 1 or 2).
        gpio_mask : list[bool]
            A list of booleans representing the state of each available GPIO pin.
        """
        pass

    @property
    @abstractmethod
    def num_gpios_per_channel(self) -> int:
        """Return the maximum number of GPIO pins this controller can manipulate per channel."""
        pass
