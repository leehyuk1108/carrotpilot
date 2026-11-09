from types import SimpleNamespace as NS

import pytest

from openpilot.selfdrive.car.ambient_light import (
  AMBIENT_MAX_AGE_NS, CAMERA_LIGHTS_MAX_AGE_NS, TRAVERSE_AMBIENT_ADDRESS, TRAVERSE_AMBIENT_BUS,
  TRAVERSE_CAMERA_LIGHTS_ADDRESS, TRAVERSE_CAMERA_LIGHTS_BUS, TraverseAmbientLight, TraverseCameraLights,
  ambient_dark, camera_lights, decode_ambient_dark, decode_camera_lights,
)

# Real 0x140 frames from the 11/08 evening drive (000001f4): 0x12 0x22 0x01 0x80 is
# low beams + tail lamps on, dark. Byte 2 low bits are the light-sensor state.
NIGHT = bytes.fromhex("12220180")
NIGHT_HIGH_BEAM = bytes.fromhex("92220180")
NIGHT_LEFT_SIGNAL = bytes.fromhex("12220980")


def frame(level=1, *, data=None, bus=TRAVERSE_AMBIENT_BUS, address=TRAVERSE_AMBIENT_ADDRESS):
  if data is None:
    data = bytes([0x00, 0x00, 0x40 | level, 0x80])
  return address, data, bus


@pytest.mark.parametrize("data,expected", [
  (NIGHT, True), (NIGHT_HIGH_BEAM, True), (NIGHT_LEFT_SIGNAL, True),
  (bytes([0x00, 0x00, 0x42]), False), (bytes([0x00, 0x00, 0x46, 0x80]), False),
])
def test_decode_reads_byte2_low_bits_only(data, expected):
  assert decode_ambient_dark(data) is expected


@pytest.mark.parametrize("data", [bytes([0, 0, 0x40]), bytes([0, 0, 0x43]), bytes(2), b""])
def test_decode_rejects_unused_values_and_short_frames(data):
  assert decode_ambient_dark(data) is None


def test_only_traverse_bus0_address_is_used():
  light = TraverseAmbientLight()
  now = 1_000_000_000
  wrong = [frame(bus=1), frame(bus=128), frame(address=0x310), frame(data=bytes(2))]
  assert light.update([(now, wrong)], now) is None
  assert light.update([(now, [frame(data=NIGHT)])], now) is True


def test_freshness_and_stale_reordered_future_frames():
  light = TraverseAmbientLight()
  now = 10_000_000_000
  assert light.update([(now - AMBIENT_MAX_AGE_NS - 1, [frame(1)])], now) is None
  assert light.update([(now, [frame(2)]), (now - 1, [frame(1)]), (now + 1, [frame(1)])], now) is False
  assert light.update([], now + AMBIENT_MAX_AGE_NS) is False
  assert light.update([], now + AMBIENT_MAX_AGE_NS + 1) is None
  later = now + AMBIENT_MAX_AGE_NS + 2
  assert light.update([(later, [frame(1)])], later) is True


def test_unused_value_keeps_last_good_reading_until_it_expires():
  light = TraverseAmbientLight()
  now = 1_000_000_000
  assert light.update([(now, [frame(1)])], now) is True
  assert light.update([(now + 1, [frame(0)])], now + 1) is True
  assert light.update([], now + AMBIENT_MAX_AGE_NS + 1) is None


@pytest.mark.parametrize("state,expected", [
  (NS(), None),
  (NS(ambientLight=NS(valid=False, dark=True)), None),
  (NS(ambientLight=NS(valid=True, dark=True)), True),
  (NS(ambientLight=NS(valid=True, dark=False)), False),
  (NS(ambientLight=NS(valid=True, dark=True), canValid=False), None),
  (NS(ambientLight=NS(valid=True, dark=True), canTimeout=True), None),
])
def test_car_state_reader(state, expected):
  assert ambient_dark(state) is expected


def test_capnp_field_round_trip():
  from opendbc.car import structs
  cs = structs.CarState.new_message()
  assert cs.ambientLight.valid is False
  cs.ambientLight.valid = True
  cs.ambientLight.dark = True
  cs.canValid = True
  with structs.CarState.from_bytes(cs.to_bytes()) as copy:
    assert ambient_dark(copy) is True


@pytest.mark.parametrize("data,expected", [
  (b"\x00", 0), (b"\x01", 1), (b"\x02", 2), (b"\x01\x00", 1), (b"\x03", None), (b"\x41", None), (b"", None),
])
def test_camera_lights_decode(data, expected):
  assert decode_camera_lights(data) == expected


def test_camera_lights_only_bus1_and_short_freshness():
  lights = TraverseCameraLights()
  now = 5_000_000_000
  # bus0 0x135 is a different 8-byte frame (gear and lights flags); it must not be read here.
  wrong = [(TRAVERSE_CAMERA_LIGHTS_ADDRESS, bytes.fromhex("020808030b1a0000"), 0),
           (TRAVERSE_CAMERA_LIGHTS_ADDRESS, b"\x01", 129), (0x136, b"\x01", TRAVERSE_CAMERA_LIGHTS_BUS)]
  assert lights.update([(now, wrong)], now) is None
  assert lights.update([(now, [(TRAVERSE_CAMERA_LIGHTS_ADDRESS, b"\x01", TRAVERSE_CAMERA_LIGHTS_BUS)])], now) == 1
  assert lights.update([], now + CAMERA_LIGHTS_MAX_AGE_NS) == 1
  assert lights.update([], now + CAMERA_LIGHTS_MAX_AGE_NS + 1) is None


def test_camera_and_ambient_decoders_are_independent():
  light, lights = TraverseAmbientLight(), TraverseCameraLights()
  now = 1_000_000_000
  batch = [(now, [frame(data=NIGHT), (TRAVERSE_CAMERA_LIGHTS_ADDRESS, b"\x00", TRAVERSE_CAMERA_LIGHTS_BUS)])]
  assert light.update(batch, now) is True
  assert lights.update(batch, now) == 0


@pytest.mark.parametrize("state,expected", [
  (NS(), None),
  (NS(ambientLight=NS(valid=True, dark=True)), None),
  (NS(ambientLight=NS(cameraValid=False, cameraLights=1)), None),
  (NS(ambientLight=NS(cameraValid=True, cameraLights=0)), 0),
  (NS(ambientLight=NS(cameraValid=True, cameraLights=2)), 2),
  (NS(ambientLight=NS(cameraValid=True, cameraLights=3)), None),
  (NS(ambientLight=NS(cameraValid=True, cameraLights=1), canValid=False), None),
  (NS(ambientLight=NS(cameraValid=True, cameraLights=1), canTimeout=True), None),
])
def test_camera_lights_car_state_reader(state, expected):
  assert camera_lights(state) == expected


def test_camera_capnp_field_round_trip():
  from opendbc.car import structs
  cs = structs.CarState.new_message()
  assert cs.ambientLight.cameraValid is False and cs.ambientLight.cameraLights == 0
  cs.ambientLight.cameraValid = True
  cs.ambientLight.cameraLights = 2
  cs.canValid = True
  with structs.CarState.from_bytes(cs.to_bytes()) as copy:
    assert camera_lights(copy) == 2
