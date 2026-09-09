OVERFLOW_VALUE = 2**15

APP_VERSION = "v2.2.0"
FACILITIES = ["TIFPA", "CNAO", "Custom"]
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
NUM_BIT = 12
NUM_BINS = 2**NUM_BIT  # 4096 bars for the multichannel
