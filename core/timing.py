from core.constants import OSC_FREQ, TMREG, UREG


def get_int_time(value: float) -> tuple[float, bytes, bytes, bytes]:
    if value <= 0:
        raise ValueError("Integration time must be strictly positive")

    tmr_bin = 0
    u_bin = 0
    error = -1
    int_time = 0

    for u_bin_tmp in range(UREG, 0, -1):  # 256 -> 1
        tmr_bin_tmp = value * OSC_FREQ / u_bin_tmp
        tmr_bin_tmp = round(tmr_bin_tmp)
        if tmr_bin_tmp == 0:
            tmr_bin_tmp = 1

        int_time_tmp = tmr_bin_tmp * u_bin_tmp / OSC_FREQ
        error_tmp = (int_time_tmp - value) ** 2

        if (error_tmp <= error and tmr_bin_tmp <= TMREG) or error < 0:
            tmr_bin = tmr_bin_tmp
            u_bin = u_bin_tmp
            error = error_tmp
            int_time = int_time_tmp

    if tmr_bin > TMREG:
        tmr_bin = TMREG
        u_bin = UREG
        int_time = tmr_bin * u_bin / OSC_FREQ

    tmr_reg = TMREG - tmr_bin
    u_reg = UREG - u_bin

    timerU = bytes([u_reg & 0xFF])
    timerH = bytes([(tmr_reg >> 8) & 0xFF])
    timerL = bytes([tmr_reg & 0xFF])

    return int_time, timerU, timerH, timerL


def get_reset_time(value: float, tx_delay: int) -> tuple[float, bytes, bytes]:
    if value < 0:
        raise ValueError("Reset time cannot be negative")

    bin_raw = value * OSC_FREQ

    if bin_raw > TMREG:
        bin_value = TMREG + tx_delay
        reset_time = bin_value / OSC_FREQ
        timerH = bytes([0])
        timerL = bytes([0])
    else:
        bin_value = round(bin_raw)
        bin_value = bin_value - tx_delay
        if bin_value <= 0:
            bin_value = 1
        reset_time = (tx_delay + bin_value) / OSC_FREQ
        bin_value = TMREG - bin_value
        timerH = bytes([(bin_value >> 8) & 0xFF])
        timerL = bytes([bin_value & 0xFF])

    return reset_time, timerH, timerL
