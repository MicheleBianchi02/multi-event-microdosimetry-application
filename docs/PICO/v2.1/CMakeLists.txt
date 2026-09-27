cmake_minimum_required(VERSION 3.13)

# Target the Raspberry Pi Pico 2 (RP2350 / Cortex-M33, 150 MHz).
# Without this the SDK defaults to the first-generation Pico (RP2040 /
# Cortex-M0+), which has no DWT cycle counter — the timing engine would hang.
# Must be a CACHE variable set BEFORE the SDK is pulled in.
set(PICO_BOARD pico2 CACHE STRING "Target board type")

# Pull in SDK functions
include($ENV{PICO_SDK_PATH}/external/pico_sdk_import.cmake)

project(pico2_daq_firmware C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)

# Initialize the Raspberry Pi Pico SDK
pico_sdk_init()

add_executable(pico2_daq_firmware
    main.c
)

# Generate header from the PIO assembly file
pico_generate_pio_header(pico2_daq_firmware ${CMAKE_CURRENT_LIST_DIR}/ramp_control.pio)

# Enable USB CDC serial output and disable UART serial output
pico_enable_stdio_usb(pico2_daq_firmware 1)
pico_enable_stdio_uart(pico2_daq_firmware 0)

# Link required SDK libraries
target_link_libraries(pico2_daq_firmware
    pico_stdlib
    pico_multicore
    pico_unique_id
    hardware_adc
    hardware_pio
    hardware_clocks
    hardware_sync
)

# Create UF2 file for drag-and-drop flashing
pico_add_extra_outputs(pico2_daq_firmware)
