#!/usr/bin/env python


from Crypto.Cipher import AES
import numpy as np

import usb.core
import usb.util

from amplifier import Amplifier


VENDOR_ID = 0x1234
PRODUCT_ID = 0xed02

ENDPOINT_IN = usb.util.ENDPOINT_IN | 2  # second endpoint


class Epoc(Amplifier):

    def __init__(self):
        # find amplifier
        self.dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
        if self.dev is None:
            raise RuntimeError('Emotiv device is not connected.')
        # pyusb docs say you *have* to call set_configuration, but it does not
        # work unless i *don't* call it.
        #dev.set_configuration()
        # get the serial number
        serial = usb.util.get_string(self.dev, 17, self.dev.iSerialNumber)
        # claim the device and it's two interfaces
        if self.dev.is_kernel_driver_active(0):
            self.dev.detach_kernel_driver(0)
        usb.util.claim_interface(self.dev, 0)
        if self.dev.is_kernel_driver_active(1):
            self.dev.detach_kernel_driver(1)
        usb.util.claim_interface(self.dev, 1)
        # prepare AES
        self.cipher = AES.new(self.generate_key(serial, True))
        # internal states for battery and impedance we have to store since it
        # is not sent with every frame.
        self._battery = 0
        self._quality = [0 for i in range(14)]

    def get_data(self):
        try:
            raw = self.dev.read(ENDPOINT_IN, 32, 1, timeout=1000)
            raw = self.decrypt(raw)
            data = self.parse_raw(raw)
            data = np.array(data)
        except Exception as e:
            print e
            data = np.array()
        return data.reshape(1, -1)

    def generate_key(self, sn, research=True):
        """Generate the encryption key.

        The key is based on the serial number of the device and the information
        weather it is a research- or consumer device.

        """
        if research:
            key = ''.join([sn[15], '\x00', sn[14], '\x54',
                           sn[13], '\x10', sn[12], '\x42',
                           sn[15], '\x00', sn[14], '\x48',
                           sn[13], '\x00', sn[12], '\x50'])
        else:
            key = ''.join([sn[15], '\x00', sn[14], '\x48',
                           sn[13], '\x00', sn[12], '\x54',
                           sn[15], '\x10', sn[14], '\x42',
                           sn[13], '\x00', sn[12], '\x50'])
        return key

    def decrypt(self, raw):
        """Decrypt a raw package."""
        data = self.cipher.decrypt(raw[:16]) + self.cipher.decrypt(raw[16:])
        tmp = 0
        for i in range(32):
            tmp = tmp << 8
            tmp += ord(data[i])
        return tmp

    def parse_raw(self, raw):
        """Parse raw data."""
        # TODO: Handle battery and counter correctly
        data = []
        shift = 256
        # 1x counter / battery (8 bit)
        # if the first bit is not set, the remaining 7 bits are the counter
        # otherwise the remaining bits are the battery
        shift -= 8
        tmp = (raw >> shift) & 0b11111111
        if tmp & 0b10000000:
            # battery
            counter = 128
            self._battery = tmp & 0b01111111
        else:
            # counter
            counter = tmp & 0b01111111
        data.append(counter)
        data.append(self._battery)
        # 7x data, 2x ???, 7x data (14 bit)
        for i in range(16):
            shift -= 14
            data.append((raw >> shift) & 0b11111111111111)
        # 2x gyroscope (8 bit)
        # the first bit denotes the sign the remaining 7 bits the number
        for i in range(2):
            shift -= 8
            tmp = (raw >> shift) & 0b01111111
            tmp -= 100
            if (raw >> shift) & 0b10000000:
                tmp *= -1
            data.append(tmp)
        # 1x ??? (8 bit)
        # we assume it is the contact quality for an electrode, the counter
        # gives the number of the electrode. since we only have 14 electrodes
        # we only take the values from counters 0..13 and 64..77
        tmp = (raw & 0b11111111)
        if counter < 128:
            if counter % 64 < 14:
                self._quality[counter % 64] = int(tmp)
        data.extend(self._quality)
        return [int(i) for i in data]


if __name__ == '__main__':
    amp = Epoc()
    print 'Reading...'
    while 1:
        try:
            print amp.get_data()
        except Exception as e:
            print e
            break