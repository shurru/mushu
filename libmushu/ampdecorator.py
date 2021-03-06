# ampdecorator.py
# Copyright (C) 2013  Bastian Venthur
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.


"""
This module provides the :class:`AmpDecorator` class.

As a user, it is very unlikely that you'll have to deal with it
directly. Its main purpose is to add additional functionality to the low
level amplifier drivers. This functionality includes features like:
saving data to a file. or being able to receive marker via TCP/IP.

By using the :func:`libmushu.__init__.get_amp` method, you'll
automatically receive decorated amplifiers.

"""

from __future__ import division

import select
import socket
import time
from multiprocessing import Process, Queue, Event
import os
import struct
import json
import logging

from libmushu.amplifier import Amplifier


logger = logging.getLogger(__name__)
logger.info('Logger started')


END_MARKER = '\n'
BUFSIZE = 2**16
PORT = 12345


class AmpDecorator(Amplifier):
    """This class 'decorates' the Low-Level Amplifier classes with
    Marker and Save-To-File functionality.

    You use it by decorating (not as in Python-Decorator, but in the GoF
    sense) the low level amplifier class you want to use::

        import libmushu
        from libmushu.ampdecorator import AmpDecorator
        from libmushu.driver.randomamp import RandomAmp

        amp = Ampdecorator(RandomAmp)

    Waring: The TCP marker timings on Windows have a resolution of
    10ms-15ms.  On Linux the resolution is 1us. This is due to
    limitations of Python's time.time method, or rather a Windows
    specific issue.

    There exists currently no precise timer, providing times which are
    comparable between two running processes on Windows. The performance
    counter provided on Windows, has a much better resolution but is
    relative to the processes start time and it drifts (1s per 100s), so
    it is only precise for a relatively short amount of time.

    If a higher precision is needed one has to replace the time.time
    calls with something which provides a better precision. For example
    one could create a third process which provides times or regularly
    synchronize both processes with the clock synchronization algorithm
    as described here:

        http://en.wikipedia.org/wiki/Network_Time_Protocol

    Alternatively one could use `timeGetTime` from Windows' Multi Media
    library, which is tunable via `timeBeginPeriod` and provides a
    precision of 1-2ms. Apparently this is the way Chrome and many
    others do it.::

        from __future__ import division

        from ctypes import windll
        import time

        timeBeginPeriod = windll.winmm.timeBeginPeriod
        timeEndPeriod = windll.winmm.timeEndPeriod
        timeGetTime = windll.winmm.timeGetTime

        if __name__ == '__main__':
            # wrap the code that needs high precision in timeBegin- and
            # timeEndPeriod with the same parameter. The parameter is
            # the interval in ms you want as precision. Usually the
            # minimum value allowed is 1 (best).
            timeBeginPeriod(1)
            times = []
            t_start = time.time()
            while time.time() < (time.time() + 1):
                times.append(timeGetTime())
            times = sorted(list(set(times)))
            print(1000 / len(times))
            timeEndPeriod(1)

    """

    def __init__(self, ampcls):
        self.amp = ampcls()
        # Setting this option, will make the Amp to discard all markers
        # from the underlying amp driver and return only TCP markers. It
        # will also return the timestamp instead of sample number within
        # block. This is only useful for debugging and gathering latency
        # information.
        self._debug_tcp_marker_timestamps = False
        self.write_to_file = False

    @property
    def presets(self):
        return self.amp.presets

    def start(self, filename=None):
        # prepare files for writing
        self.write_to_file = False
        if filename is not None:
            self.write_to_file = True
            filename_marker = filename + '.marker'
            filename_eeg = filename + '.eeg'
            filename_meta = filename + '.meta'
            for filename in filename_marker, filename_eeg, filename_meta:
                if os.path.exists(filename):
                    logger.error('A file "%s" already exists, aborting.' % filename)
                    raise Exception
            self.fh_eeg = open(filename_eeg, 'w')
            self.fh_marker = open(filename_marker, 'w')
            self.fh_meta = open(filename_meta, 'w')
            # write meta data
            meta = {'Channels': self.amp.get_channels(),
                    'Sampling Frequency': self.amp.get_sampling_frequency(),
                    'Amp': str(self.amp)
                    }
            json.dump(meta, self.fh_meta, indent=4)

        # start the marker server
        self.marker_queue = Queue()
        self.tcp_reader_running = Event()
        self.tcp_reader_running.set()
        tcp_reader_ready = Event()
        self.tcp_reader = Process(target=tcp_reader,
                                  args=(self.marker_queue,
                                        self.tcp_reader_running,
                                        tcp_reader_ready)
                                  )
        self.tcp_reader.start()
        logger.debug('Waiting for TCP reader to become ready...')
        tcp_reader_ready.wait()
        logger.debug('TCP reader is ready...')
        # zero the sample counter
        self.received_samples = 0
        # start the amp
        self.time = time.time()
        self.amp.start()

    def stop(self):
        # stop the amp
        self.amp.stop()
        # stop the marker server
        self.tcp_reader_running.clear()
        logger.debug('Waiting for TCP reader process to stop...')
        self.tcp_reader.join()
        logger.debug('TCP reader process stopped.')
        # close the files
        if self.write_to_file:
            logger.debug('Closing files.')
            for fh in self.fh_eeg, self.fh_marker, self.fh_meta:
                fh.close()

    def configure(self, **kwargs):
        self.amp.configure(**kwargs)

    def get_data(self):
        # get data and marker from underlying amp
        data, marker = self.amp.get_data()
        # get tcp marker and merge them with markers from the amp
        self.time_old = self.time
        self.time = time.time()
        # merge markers
        tcp_marker = []
        # wait 0.2ms to let late markers arrive in time for this block
        time.sleep(0.2 / 1000)
        future_markers = []
        while not self.marker_queue.empty():
            m = self.marker_queue.get()
            if not self._debug_tcp_marker_timestamps:
                if m[0] > self.time:
                    #logger.debug('Marker is newer than the last sample of the current block, queueing it for the next block.')
                    future_markers.append(m)
                    continue
                dt = m[0] - self.time_old
                if dt < 0:
                    logger.warning('Marker is %.2fms older than current block, setting it to first sample of current block.' % abs(dt * 1000))
                    m[0] = 0
                else:
                    # int(x) truncates towards zero, that's what we want
                    m[0] = int(dt * self.amp.get_sampling_frequency())
            tcp_marker.append(m)
        # put markers which belong to the next block back into the queue
        for m in future_markers:
            self.marker_queue.put(m)
        if not self._debug_tcp_marker_timestamps:
            marker = sorted(marker + tcp_marker)
        else:
            marker = sorted(tcp_marker)
        # save data to files
        if self.write_to_file:
            for m in marker:
                # If we received 17 samples so far, the index of the last
                # sample from the last block is 16. Since m[0] is the sample
                # index of the current block starting with 0,
                # received_samples + m[0] gives the correct index for the
                # global marker list.
                self.fh_marker.write("%i %s\n" % (self.received_samples + m[0], m[1]))
            for t in data:
                for c in t:
                    self.fh_eeg.write(struct.pack("f", c))
        self.received_samples += len(data)
        return data, marker

    def get_channels(self):
        return self.amp.get_channels()


def tcp_reader(queue, running, ready):
    """Marker-reading process.

    This method runs in a seperate process and receives TCP markers.
    Whenever a marker is received, it is put together with a timestamp
    into a queue.

    Parameters
    ----------
    queue : Queue
        this queue is used to send markers to a different process
    running : Event
        this event is used to signal this process to terminate its main
        loop
    ready : Event
        this signal is used to signal the "parent"-process that this
        process is ready to receive marker

    """
    # setup the server socket
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.setblocking(0)
    server_socket.bind(('localhost', PORT))
    server_socket.listen(5)
    ready.set()
    # read the socket until 'running' is set to false
    rlist, wlist, elist = [server_socket], [], []
    while running.is_set():
        readables, _, _ = select.select(rlist, wlist, elist, 1)
        t = time.time()
        for sock in readables:
            if sock == server_socket:
                # a new connection is opened
                connection, address = sock.accept()
                logger.debug('Incoming connection from %s', address)
                rlist.append(connection)
                data_buffer = ''
            else:
                data = sock.recv(BUFSIZE)
                if not data:
                    # an open connection was closed
                    logger.debug('Connection closed')
                    sock.close()
                    rlist.remove(sock)
                else:
                    # received maybe incomplete data from a
                    # connection
                    # TODO: are we? I thought we're using TCP?
                    data_buffer = ''.join([data_buffer, data])
                    split = data_buffer.rsplit(END_MARKER, 1)
                    messages = []
                    if len(split) > 1:
                        # data_buffer contains at least one complete
                        # message
                        data_buffer = split[1]
                        messages = split[0].split(END_MARKER)
                        for m in messages:
                            queue.put([t, m])
    logger.debug('Terminated select-loop')
    # close all connections and the socket
    for s in rlist:
        s.close()
