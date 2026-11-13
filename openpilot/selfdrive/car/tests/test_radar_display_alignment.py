import math

import pytest

from openpilot.selfdrive.car import radar_display_alignment as rda

TRAVERSE = rda.for_fingerprint(rda.TRAVERSE)


def test_only_the_traverse_gets_a_display_correction():
  assert TRAVERSE.key() == (rda.OFFSET_M, rda.SLOPE) == (0.29, 0.0064)
  for other in ('CHEVROLET_TRAILBLAZER', 'CADILLAC_XT4', '', None, 'chevrolet_traverse'):
    assert rda.for_fingerprint(other) is None


def test_shift_grows_with_range_and_stops_at_80_m():
  assert TRAVERSE.shift(0.0) == pytest.approx(0.29)
  assert TRAVERSE.shift(30.0) == pytest.approx(0.29 + 0.192)
  assert TRAVERSE.shift(-5.0) == pytest.approx(0.29)
  assert TRAVERSE.shift(200.0) == TRAVERSE.shift(80.0) == pytest.approx(0.29 + 0.512)


def test_right_and_left_positive_frames_move_the_same_way():
  # Right-positive (Navdy model frame) goes down, left-positive yRel goes up: both mean "move left".
  assert TRAVERSE.right(30.0, 2.0) == pytest.approx(2.0 - 0.482)
  assert TRAVERSE.left(30.0, -2.0) == pytest.approx(-2.0 + 0.482)
  assert math.isnan(TRAVERSE.right(10.0, math.nan)) and math.isnan(TRAVERSE.left(10.0, math.nan))
  assert TRAVERSE.right(math.inf, 1.0) == 1.0 and TRAVERSE.left(math.nan, 1.0) == 1.0


class Params:
  def __init__(self, value=None, error=None):
    self.value, self.error = value, error

  def get(self, key):
    assert key == 'CarParamsPersistent'
    if self.error:
      raise self.error
    return self.value


@pytest.mark.parametrize('params', [Params(None), Params(b''), Params(b'not capnp'), Params(error=OSError('busy'))])
def test_unknown_or_unreadable_car_params_mean_no_correction(params):
  assert rda.read_alignment(params) is None


@pytest.mark.parametrize('fingerprint,expected', [(rda.TRAVERSE, (0.29, 0.0064)), ('CHEVROLET_TRAILBLAZER', None)])
def test_reads_the_fingerprint_from_car_params(fingerprint, expected):
  car = pytest.importorskip('openpilot.cereal').car
  raw = car.CarParams.new_message(carFingerprint=fingerprint, brand='gm').to_bytes()
  alignment = rda.read_alignment(Params(raw))
  assert (None if alignment is None else alignment.key()) == expected


def test_control_code_does_not_import_the_display_correction():
  """radard, the planner and the GM radar interface keep the radar's own yRel."""
  import ast
  import inspect
  from pathlib import Path
  root = Path(inspect.getfile(rda)).resolve().parents[3]
  for rel in ('openpilot/selfdrive/carrot/radar/radard_dpath.py', 'openpilot/selfdrive/carrot/radar/radarcan.py',
              'opendbc_repo/opendbc/car/gm/radar_interface.py', 'openpilot/selfdrive/controls/lib/longitudinal_planner.py'):
    path = root / rel
    if not path.exists():
      continue  # sparse checkout
    names = {alias.name for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, (ast.Import, ast.ImportFrom))
             for alias in node.names} | {node.module for node in ast.walk(ast.parse(path.read_text()))
                                         if isinstance(node, ast.ImportFrom) and node.module}
    assert not any('radar_display_alignment' in str(n) for n in names), rel
