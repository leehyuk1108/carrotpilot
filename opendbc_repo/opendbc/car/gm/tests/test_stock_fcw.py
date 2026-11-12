"""Stock camera FCW reaches openpilot's replacement 0x370. Real CAN codec/parser/controller; no device I/O."""
import math

import pytest

from opendbc.car import Bus, structs
from opendbc.car.gm import stock_fcw
from opendbc.car.gm.stock_fcw import STOCK_FCW_MAX_AGE_NS, StockFcw, stock_fcw_supported
from opendbc.car.gm.values import CAR, DBC, GMSafetyFlags
from opendbc.car.gm.tests.test_display_speed import runtime, build_cp, controller_state, frames, parser_state  # noqa: F401

MS = 1_000_000


@pytest.fixture
def clock(monkeypatch):
  """The boot clock that stamps CAN packets; tests move it with their synthetic packet times."""
  now = [0]
  monkeypatch.setattr(stock_fcw, "boottime_ns", lambda: now[0])
  return now


def camera_status(packer, alert, set_kph=95.):
  return packer.make_can_msg("ASCMActiveCruiseControlStatus", 2, {"ACCSpeedSetpoint": set_kph, "FCWAlert": alert})


def pt_frames(packer):
  return [frame for frame in frames(packer) if frame[0] != 0x370]


def dashboard_alerts(sends):
  return [data[5] & 3 for address, data, bus in sends if address == 0x370 and bus == 0]


def traverse_controller(runtime):
  controller = runtime.controller.CarController(DBC[CAR.CHEVROLET_TRAVERSE], build_cp(runtime))
  controller.fcw_warning_test = None
  return controller


def test_alert_follows_camera_and_expires_by_time():
  fcw = StockFcw()
  assert fcw.alert == 0
  assert fcw.update([], 0, 1000 * MS) == 0
  assert fcw.update([3.], 1000 * MS, 1000 * MS) == 3
  assert fcw.update([], 0, 1000 * MS + STOCK_FCW_MAX_AGE_NS) == 3
  assert fcw.update([], 0, 1000 * MS + STOCK_FCW_MAX_AGE_NS + 1) == 0
  # A stalled loop resuming much later never replays the old alert.
  assert fcw.update([2.], 2000 * MS, 2000 * MS) == 2
  assert fcw.update([], 0, 3000 * MS) == 0
  # An old frame processed late, a sample from the future or one with no time is stale.
  assert fcw.update([3.], 4000 * MS, 4300 * MS) == 0
  assert fcw.update([3.], 4000 * MS, 3990 * MS) == 0
  assert fcw.update([3.], 0, 5000 * MS) == 0


def test_default_clock_is_the_boot_clock(clock):
  clock[0] = 7000 * MS
  fcw = StockFcw()
  assert fcw.update([3.], 7000 * MS) == 3
  clock[0] += STOCK_FCW_MAX_AGE_NS + 1
  assert fcw.update([], 0) == 0


@pytest.mark.parametrize("values,expected", [([3., 0.], 0), ([3., 1.], 1), ([2., 0.], 0), ([0., 3.], 3), ([1., 2., 3.], 3)])
def test_newest_camera_value_wins(values, expected):
  fcw = StockFcw()
  assert fcw.update(values, 1000 * MS, 1000 * MS) == expected


@pytest.mark.parametrize("bad", [math.nan, math.inf, -1., 4., 2.5, True, None, "3"])
def test_malformed_values_are_ignored(bad):
  fcw = StockFcw()
  assert fcw.update([bad], 1000 * MS, 1000 * MS) == 0
  assert fcw.update([3., bad], 1000 * MS, 1000 * MS) == 3


def test_supported_only_on_traverse_sdgm_long(runtime):
  assert stock_fcw_supported(build_cp(runtime))
  assert not stock_fcw_supported(build_cp(runtime, CAR.CHEVROLET_TRAVERSE, False))
  assert not stock_fcw_supported(build_cp(runtime, CAR.CHEVROLET_BOLT_EUV, True))
  cp = build_cp(runtime)
  cp.safetyConfigs[0].safetyParam &= ~int(GMSafetyFlags.HW_SDGM)
  assert not stock_fcw_supported(cp)


def test_real_parser_reads_camera_fcw_without_new_requirements(runtime, clock):
  ci, packer = parser_state(runtime)
  plain, _ = parser_state(runtime)
  plain.CS.stock_fcw = None
  t = 1_000_000_000
  for alert, expected in ((0, 0), (3, 3), (2, 2), (0, 0), (3, 3)):
    t += 40 * MS
    clock[0] = t
    packets = [(t, pt_frames(packer) + [camera_status(packer, alert)])]
    ret, plain_ret = ci.update(packets), plain.update(packets)
    assert ci.CS.cam_fcw_alert == expected
    assert plain.CS.cam_fcw_alert == 0
    # Only the confirmed collision alert (3) raises the comma's stock FCW event; nothing else changes.
    assert ret.stockFcw == (expected == 3)
    assert not plain_ret.stockFcw
    changed, original = ret.to_dict(), plain_ret.to_dict()
    changed.pop("stockFcw", None)
    original.pop("stockFcw", None)
    assert changed == original
  for parser, plain_parser in zip(ci.can_parsers.values(), plain.can_parsers.values(), strict=True):
    assert set(parser.message_states) == set(plain_parser.message_states)
  # No camera frame: the last alert holds for at most 250 ms, then clears.
  last_camera = t
  for _ in range(30):
    t += 10 * MS
    clock[0] = t
    ci.update([(t, pt_frames(packer))])
    assert ci.CS.cam_fcw_alert == (3 if t - last_camera <= STOCK_FCW_MAX_AGE_NS else 0)


@pytest.mark.parametrize("delivery", ["pt_events", "empty_events", "no_events"])
@pytest.mark.parametrize("step_ms", [10, 20, 40, 1000])
def test_alert_expires_however_can_arrives(runtime, clock, delivery, step_ms):
  """Camera gone; other CAN, empty CAN events, or no CAN event at all while card keeps running."""
  ci, packer = parser_state(runtime)
  controller = traverse_controller(runtime)
  cc = structs.CarControl()
  t = 1_000_000_000
  clock[0] = t
  assert ci.update([(t, pt_frames(packer) + [camera_status(packer, 3)])]).stockFcw
  last_camera = t
  assert ci.CS.cam_fcw_alert == 3
  while t - last_camera <= 2_000 * MS:
    t += step_ms * MS
    clock[0] = t
    packets = {"pt_events": [(t, pt_frames(packer))], "empty_events": [(t, [])], "no_events": []}[delivery]
    ret = ci.update(packets)
    expected = 3 if t - last_camera <= STOCK_FCW_MAX_AGE_NS else 0
    assert ci.CS.cam_fcw_alert == expected
    assert ret.stockFcw == (expected == 3)
    _, sends = controller.update(cc.as_reader(), ci.CS, t)
    assert all(alert == expected for alert in dashboard_alerts(sends))


def test_old_camera_frame_processed_late_stays_dark(runtime, clock):
  ci, packer = parser_state(runtime)
  clock[0] = 2_000 * MS
  ci.update([(1_000 * MS, pt_frames(packer) + [camera_status(packer, 3)])])
  assert ci.CS.cam_fcw_alert == 0


@pytest.mark.parametrize("sequence,expected", [((3, 0), 0), ((3, 1), 1), ((2, 0), 0), ((0, 3), 3)])
def test_batched_camera_frames_use_the_newest(runtime, clock, sequence, expected):
  ci, packer = parser_state(runtime)
  first, second = sequence
  clock[0] = 1_040 * MS
  ci.update([(1_000 * MS, pt_frames(packer) + [camera_status(packer, first)]),
             (1_040 * MS, pt_frames(packer) + [camera_status(packer, second)])])
  assert ci.CS.cam_fcw_alert == expected
  _, sends = traverse_controller(runtime).update(structs.CarControl().as_reader(), ci.CS, 1_040 * MS)
  assert dashboard_alerts(sends) == [expected]


def test_other_cars_never_track_camera_fcw(runtime, clock):
  ci, packer = parser_state(runtime, CAR.CHEVROLET_BOLT_EUV)
  assert ci.CS.stock_fcw is None
  clock[0] = 1_000 * MS
  assert not ci.update([(1_000 * MS, pt_frames(packer) + [camera_status(packer, 3)])]).stockFcw
  assert ci.CS.cam_fcw_alert == 0


@pytest.mark.parametrize("engaged", [False, True])
@pytest.mark.parametrize("own_fcw", [False, True])
@pytest.mark.parametrize("stock_alert", [1, 2, 3])
def test_only_dashboard_fcw_bits_change(runtime, engaged, own_fcw, stock_alert):
  cp = build_cp(runtime)
  tested = runtime.controller.CarController(DBC[CAR.CHEVROLET_TRAVERSE], cp)
  baseline = runtime.controller.CarController(DBC[CAR.CHEVROLET_TRAVERSE], cp)
  tested.fcw_warning_test = baseline.fcw_warning_test = None
  cs, old_cs = controller_state(), controller_state()
  cs.out = old_cs.out = structs.CarState(canValid=True, vEgo=15., gearShifter="drive")
  cs.cam_fcw_alert = stock_alert
  cc = structs.CarControl(enabled=engaged, longActive=engaged, latActive=engaged)
  cc.actuators.longControlState = "pid" if engaged else "off"
  cc.hudControl.visualAlert = "fcw" if own_fcw else "none"
  seen = 0
  for tick in range(40):
    now = 1_000_000_000 + tick * 10_000_000
    output, sends = tested.update(cc.as_reader(), cs, now)
    old_output, old_sends = baseline.update(cc.as_reader(), old_cs, now)
    assert output.to_dict() == old_output.to_dict()
    assert len(sends) == len(old_sends)
    for (address, data, bus), (old_address, old_data, old_bus) in zip(sends, old_sends, strict=True):
      assert (address, bus) == (old_address, old_bus)
      if address == 0x370 and bus == 0:
        seen += 1
        assert data[:5] == old_data[:5]
        assert data[5] & ~3 == old_data[5] & ~3
        assert data[5] & 3 == (3 if own_fcw else stock_alert)
        assert old_data[5] & 3 == (3 if own_fcw else 0)
      else:
        assert data == old_data
  assert seen > 0


@pytest.mark.parametrize("candidate,alpha", [(CAR.CHEVROLET_TRAVERSE, False), (CAR.CHEVROLET_BOLT_EUV, True)])
def test_other_configurations_ignore_camera_fcw(runtime, candidate, alpha):
  cp = build_cp(runtime, candidate, alpha)
  tested = runtime.controller.CarController(DBC[candidate], cp)
  baseline = runtime.controller.CarController(DBC[candidate], cp)
  cs, old_cs = controller_state(), controller_state()
  cs.cam_fcw_alert = 3
  cc = structs.CarControl()
  for tick in range(40):
    now = 1_000_000_000 + tick * 10_000_000
    _, sends = tested.update(cc.as_reader(), cs, now)
    _, old_sends = baseline.update(cc.as_reader(), old_cs, now)
    assert sends == old_sends


def test_camera_fcw_end_to_end(runtime, clock):
  """bus2 camera 0x370 FCWAlert -> real CarState -> real controller -> bus0 0x370 FCWAlert."""
  ci, packer = parser_state(runtime)
  controller = traverse_controller(runtime)
  cc = structs.CarControl()
  alerts = []
  t = 1_000_000_000
  for tick in range(120):
    t += 10 * MS
    clock[0] = t
    camera = [camera_status(packer, 3 if 20 <= tick < 60 else 0)] if tick % 4 == 0 else []
    ci.update([(t, pt_frames(packer) + camera)])
    _, sends = controller.update(cc.as_reader(), ci.CS, t)
    alerts += [(tick, alert) for alert in dashboard_alerts(sends)]
  assert alerts
  assert all(alert == 0 for tick, alert in alerts if tick < 20)
  assert all(alert == 3 for tick, alert in alerts if 20 <= tick < 60)
  assert all(alert == 0 for tick, alert in alerts if tick >= 60)
  assert ci.can_parsers[Bus.cam].bus == 2
