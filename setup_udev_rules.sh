#!/bin/bash
echo 'SUBSYSTEM=="tty", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="0009", SYMLINK+="pico_daq"' | sudo tee /etc/udev/rules.d/99-daq-devices.rules > /dev/null
sudo udevadm control --reload-rules
sudo udevadm trigger
