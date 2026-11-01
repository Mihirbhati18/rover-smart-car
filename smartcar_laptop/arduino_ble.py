"""HC-08 UART transport with bounded writes and newline reassembly."""
import asyncio
import concurrent.futures
import queue
import sys
import threading
import time
from pathlib import Path
from arduino_serial import ArduinoEvent, COMMAND_BYTES

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'ble-packages'))
from bleak import BleakClient, BleakScanner

ADDRESS = '44:EE:14:54:D5:CC'
SERVICE = '0000ffe0-0000-1000-8000-00805f9b34fb'
UART = '0000ffe1-0000-1000-8000-00805f9b34fb'


class ArduinoBLE:
    def __init__(self):
        self.port = 'HC-08 Bluetooth'
        self._loop = asyncio.new_event_loop()
        self._thread = None
        self._client = None
        self._events = queue.Queue()
        self._buffer = bytearray()
        self._failed = False

    @property
    def is_open(self):
        return bool(not self._failed and self._client and self._client.is_connected)

    def _run(self, coroutine, timeout=5):
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            self._failed = True
            raise TimeoutError('Bluetooth timed out. Reconnect HC-08 before continuing.')

    def _notify(self, sender, data):
        self._buffer.extend(data)
        while b'\n' in self._buffer:
            raw, _, remaining = self._buffer.partition(b'\n')
            self._buffer = bytearray(remaining)
            text = raw.decode('utf-8', errors='replace').strip()
            if text:
                self._events.put(ArduinoEvent(text, time.time()))
        if len(self._buffer) > 4096:
            self._buffer.clear()
            self._failed = True

    async def _connect(self):
        device = await BleakScanner.find_device_by_address(ADDRESS, timeout=15)
        if device is None:
            raise RuntimeError('HC-08 not found. Disconnect nRF Connect on the phone and power on the module.')
        self._client = BleakClient(device, timeout=25, pair=False,
                                   services=[SERVICE], winrt={'use_cached_services': False})
        await self._client.connect()
        char = self._client.services.get_characteristic(UART)
        if char is None or not {'notify', 'write-without-response'}.issubset(char.properties):
            raise RuntimeError('HC-08 data channel does not match the verified module.')
        await self._client.start_notify(char, self._notify)

    def connect(self):
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name='hc08-ble')
        self._thread.start()
        try:
            self._run(self._connect(), 50)
        except Exception:
            self.disconnect(send_stop=False)
            raise
        return self

    async def _write(self, packet):
        if not self.is_open:
            raise RuntimeError('HC-08 is disconnected. Reconnect before enabling controls.')
        await self._client.write_gatt_char(UART, packet, response=False)

    def send(self, command):
        normalized = command.strip().upper()
        if normalized not in COMMAND_BYTES:
            raise ValueError('Unknown Arduino command')
        self._run(self._write(COMMAND_BYTES[normalized]))

    def probe_route(self):
        # Empty route packet cannot contain a motor instruction. New route
        # firmware responds INVALID_ROUTE_PACKET; legacy firmware rejects it.
        self._run(self._write(b'~\n'))

    def send_route_pulse(self, direction, duration_ms):
        code = {'FORWARD':'F', 'LEFT':'L', 'RIGHT':'R'}.get(direction)
        if code is None or type(duration_ms) is not int or not 1 <= duration_ms <= 350:
            raise ValueError('Route pulse requires forward/left/right and 1–350 ms.')
        self._run(self._write(f'~{code}{duration_ms}\n'.encode('ascii')))

    def probe_manual(self):
        self._run(self._write(b'@\n'))

    def send_manual_pulse(self, direction, duration_ms):
        code = {'FORWARD':'F', 'BACKWARD':'B', 'LEFT':'L', 'RIGHT':'R'}.get(direction)
        if code is None or type(duration_ms) is not int or not 1 <= duration_ms <= 350:
            raise ValueError('Manual pulse requires a direction and 1–350 ms.')
        self._run(self._write(f'@{code}{duration_ms}\n'.encode('ascii')))

    def get_event(self, timeout=None):
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    async def _close(self):
        if self._client:
            await self._client.disconnect()

    def disconnect(self, send_stop=True):
        if not self._thread or not self._thread.is_alive() or not self._loop.is_running():
            return
        try:
            if send_stop and self.is_open:
                try:
                    self.send('STOP')
                except Exception:
                    pass
            self._run(self._close(), 10)
        finally:
            self._failed = True
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=2)
            self._thread = None
