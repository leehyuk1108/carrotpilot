from parameterized import parameterized

from opendbc.car import gen_empty_fingerprint
from opendbc.car.gm.fingerprints import FINGERPRINTS
from opendbc.car.gm.interface import CarInterface
from opendbc.car.gm.values import CAMERA_ACC_CAR, GM_RX_OFFSET, CAR, GMFlags, GMSafetyFlags, SASCM_CAR, SDGM_CAR
from opendbc.car.structs import CarParams

CAMERA_DIAGNOSTIC_ADDRESS = 0x24b


class TestGMFingerprint:
  @parameterized.expand(FINGERPRINTS.items())
  def test_can_fingerprints(self, car_model, fingerprints):
    assert len(fingerprints) > 0

    assert all(len(finger) for finger in fingerprints)

    # The camera can sometimes be communicating on startup
    if car_model in CAMERA_ACC_CAR:
      for finger in fingerprints:
        for required_addr in (CAMERA_DIAGNOSTIC_ADDRESS, CAMERA_DIAGNOSTIC_ADDRESS + GM_RX_OFFSET):
          assert finger.get(required_addr) == 8, required_addr


class TestGmMalibuSascm:
  def test_ascm_sascm_configuration(self):
    fingerprint = gen_empty_fingerprint()
    fingerprint[0].update(FINGERPRINTS[CAR.CHEVROLET_MALIBU_SASCM][0])

    car_params = CarInterface.get_params(CAR.CHEVROLET_MALIBU_SASCM, fingerprint, [],
                                         alpha_long=True, is_release=False, docs=False)
    safety_param = car_params.safetyConfigs[0].safetyParam

    assert CAR.CHEVROLET_MALIBU_SASCM in SASCM_CAR
    assert CAR.CHEVROLET_MALIBU_SASCM not in SDGM_CAR
    assert car_params.networkLocation == CarParams.NetworkLocation.gateway
    assert car_params.openpilotLongitudinalControl
    assert safety_param & GMSafetyFlags.HW_ASCM_LONG
    assert not safety_param & GMSafetyFlags.HW_SDGM
    assert safety_param & GMSafetyFlags.FORCE_BRAKE_C9
    assert car_params.flags & GMFlags.NO_ACCELERATOR_POS_MSG
    assert car_params.flags & GMFlags.FORCE_BRAKE_C9
