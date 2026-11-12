"""Stock camera FCW reaches openpilot's replacement 0x370. Real CAN codec/parser/controller; no device I/O."""
import math

import pytest

from opendbc.car import Bus, structs
from opendbc.car.gm.stock_fcw import STOCK_FCW_TIMEOUT_FRAMES, StockFcw, stock_fcw_supported
from opendbc.car.gm.values import CAR, DBC, GMSafetyFlags
from opendbc.car.gm.tests.test_display_speed import runtime, build_cp, controller_state, frames, parser_state  # noqa: F401


def camera_status(packer, alert, set_kph=95.):
  return packer.make_can_msg("ASCMActiveCruiseControlStatus", 2, {"ACCSpeedSetpoint": set_kph, "FCWAlert": alert})


def pt_frames(packer):
  return [frame for frame in frames(packer) if frame[0] != 0x370]


def dashboard_alerts(sends):
  return [data[5] & 3 for address, data, bus in sends if address == 0x370 and bus == 0]


def test_alert_follows_camera_and_times_out():
  fcw = StockFcw()
  assert fcw.alert == 0
  assert fcw.update([]) == 0
  assert fcw.update([3.]) == 3
  for _ in range(STOCK_FCW_TIMEOUT_FRAMES):
    assert fcw.update([]) == 3
  assert fcw.update([]) == 0
  assert fcw.update([2., 0.]) == 2
  assert fcw.update([0.]) == 0


@pytest.mark.parametrize("bad", [math.nan, math.inf, -1., 4., 2.5, True, None, "3"])
def test_malformed_values_are_ignored(bad):
  fcw = StockFcw()
  assert fcw.update([bad]) == 0
  assert fcw.update([3., bad]) == 3


def test_supported_only_on_traverse_sdgm_long(runtime):
  assert stock_fcw_supported(build_cp(runtime))
  assert not stock_fcw_supported(build_cp(runtime, CAR.CHEVROLET_TRAVERSE, False))
  assert not stock_fcw_supported(build_cp(runtime, CAR.CHEVROLET_BOLT_EUV, True))
  cp = build_cp(runtime)
  cp.safetyConfigs[0].safetyParam &= ~int(GMSafetyFlags.HW_SDGM)
  assert not stock_fcw_supported(cp)


def test_real_parser_reads_camera_fcw_without_new_requirements(runtime):
  ci, packer = parser_state(runtime)
  plain, _ = parser_state(runtime)
  plain.CS.stock_fcw = None
  t = 1_000_000_000
  for alert, expected in ((0, 0), (3, 3), (2, 2), (0, 0), (3, 3)):
    t += 40_000_000
    packets = [(t, pt_frames(packer) + [camera_status(packer, alert)])]
    ret, plain_ret = ci.update(packets), plain.update(packets)
    assert ci.CS.cam_fcw_alert == expected
    assert plain.CS.cam_fcw_alert == 0
    assert ret.to_dict() == plain_ret.to_dict()
  for parser, plain_parser in zip(ci.can_parsers.values(), plain.can_parsers.values(), strict=True):
    assert set(parser.message_states) == set(plain_parser.message_states)
  # No camera frame: the last alert holds briefly, then clears.
  for tick in range(STOCK_FCW_TIMEOUT_FRAMES + 1):
    t += 10_000_000
    ci.update([(t, pt_frames(packer))])
    assert ci.CS.cam_fcw_alert == (3 if tick < STOCK_FCW_TIMEOUT_FRAMES else 0)


def test_other_cars_never_track_camera_fcw(runtime):
  ci, packer = parser_state(runtime, CAR.CHEVROLET_BOLT_EUV)
  assert ci.CS.stock_fcw is None
  ci.update([(1_000_000_000, pt_frames(packer) + [camera_status(packer, 3)])])
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


def test_camera_fcw_end_to_end(runtime):
  """bus2 camera 0x370 FCWAlert -> real CarState -> real controller -> bus0 0x370 FCWAlert."""
  ci, packer = parser_state(runtime)
  controller = runtime.controller.CarController(DBC[CAR.CHEVROLET_TRAVERSE], build_cp(runtime))
  controller.fcw_warning_test = None
  cc = structs.CarControl()
  alerts = []
  t = 1_000_000_000
  for tick in range(120):
    t += 10_000_000
    camera = [camera_status(packer, 3 if 20 <= tick < 60 else 0)] if tick % 4 == 0 else []
    ci.update([(t, pt_frames(packer) + camera)])
    _, sends = controller.update(cc.as_reader(), ci.CS, t)
    alerts += [(tick, alert) for alert in dashboard_alerts(sends)]
  assert alerts
  assert all(alert == 0 for tick, alert in alerts if tick < 20)
  assert all(alert == 3 for tick, alert in alerts if 20 <= tick < 60)
  assert all(alert == 0 for tick, alert in alerts if tick >= 64)
  assert ci.can_parsers[Bus.cam].bus == 2
