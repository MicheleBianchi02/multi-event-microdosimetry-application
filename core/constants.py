OVERFLOW_VALUE = 2**15  # 32768: sentinel of the legacy serial protocol / CSV
# format 1 only — see overflow_sentinel() for the current convention.


def overflow_sentinel(bits: int) -> int:
    """Overflow / missing-channel marker for a `bits`-deep ADC: one past the
    full scale, so it can never be a legitimate reading.  Produced by the
    drivers (and the simulator) and mapped to NaN by the conversion layer."""
    return 2 ** bits


# ADC resolution fallback (B6): the app-wide ADC context — bits, histogram
# bins, overflow sentinel — is dynamic, derived from the loaded hardware
# configuration (SessionModel.adc_bits) or from the archive being loaded.
# These constants remain only as defaults for legacy inputs and simulation.
DEFAULT_ADC_BITS = 12


APP_VERSION = "3.0"
FACILITIES = ["TIFPA", "CNAO", "Custom"]
PARTICLES = ["Proton", "Carbon", "Gamma", "Custom"]
BEAM_TYPES = {
    "TIFPA": ["Biological", "Physical"],
    "CNAO": ["Physical"],
    "Custom": []
}

OSC_FREQ = 2**15  # 32768 Hz
TMREG = 2**16  # timerH * timerL
UREG = 2**8  # timerU

# From the start of the interrupt to the end of the transmission
# if 190 us => TX_DELAY = 6 ticks
# The value in s is TX_DELAY / OSC_FREQ . It is an approximation by defects

# For 2 channel configuration
TX_DELAY_PRO_DOUBLE = 13  # ticks
TX_DELAY_DEBUG_DOUBLE = 24  # ticks

# For single channel configuration
TX_DELAY_PRO_SINGLE = 7  # ticks
TX_DELAY_DEBUG_SINGLE = 13  # ticks

UPDATE_INTERVAL_MS = 50  # in ms
NUM_BIT = DEFAULT_ADC_BITS  # legacy alias (B6): use SessionModel.adc_bits instead
NUM_BINS = 2**NUM_BIT  # legacy alias: default histogram bins (4096)
