from collections import deque
import math
import numpy as np

from openpilot.cereal import log
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.selfdrive.modeld.constants import ModelConstants
from openpilot.selfdrive.controls.lib.drive_helpers import CONTROL_N

from opendbc.car.interfaces import LatControlInputs
from opendbc.car.vehicle_model import ACCELERATION_DUE_TO_GRAVITY
from openpilot.selfdrive.controls.lib.latcontrol import LatControl
from openpilot.common.pid import PIDController

from openpilot.common.params import Params

# At higher speeds (25+mph) we can assume:
# Lateral acceleration achieved by a specific car correlates to
# torque applied to the steering rack. It does not correlate to
# wheel slip, or to speed.

# This controller applies torque to achieve desired lateral
# accelerations. To compensate for the low speed effects we
# use a LOW_SPEED_FACTOR in the error. Additionally, there is
# friction in the steering wheel that needs to be overcome to
# move it at all, this is compensated for too.

LOW_SPEED_X = [0, 10, 20, 30]
LOW_SPEED_Y = [15, 13, 10, 5]
LOW_SPEED_Y_NN = [12, 3, 1, 0]

LAT_PLAN_MIN_IDX = 5

NNFF_FROGPILOT_PARITY_CARS = ("CHEVROLET_TRAVERSE", "CHEVROLET_TRAILBLAZER", "CHEVROLET_TRAILBLAZER_CC", "CHEVROLET_TRAX")

def get_low_speed_factor(v_ego, nn_table):
  # FrogPilot's NNFF (and twilsonco's original) uses the smaller NN low-speed table.
  return np.interp(v_ego, LOW_SPEED_X, LOW_SPEED_Y_NN if nn_table else LOW_SPEED_Y)**2

def get_predicted_lateral_jerk(lat_accels, t_diffs):
  # compute finite difference between subsequent model_data.acceleration.y values
  lat_accel_diffs = np.diff(lat_accels)
  lat_jerk = lat_accel_diffs / t_diffs
  return lat_jerk.tolist()

def sign(x):
  return 1.0 if x > 0.0 else (-1.0 if x < 0.0 else 0.0)

def get_lookahead_value(future_vals, current_val):
  if len(future_vals) == 0:
    return current_val

  same_sign_vals = [v for v in future_vals if sign(v) == sign(current_val)]

  # if any future val has opposite sign of current val, return 0
  if len(same_sign_vals) < len(future_vals):
    return 0.0

  min_val = min(same_sign_vals + [current_val], key=lambda x: abs(x))
  return min_val

def roll_pitch_adjust(roll, pitch):
  return roll * math.cos(pitch)

class LatControlTorque(LatControl):
  def __init__(self, CP, CI):
    super().__init__(CP, CI)
    self.torque_params = CP.lateralTuning.torque.as_builder()
    self.pid = PIDController(self.torque_params.kp, self.torque_params.ki,
                             k_f=self.torque_params.kf, pos_limit=self.steer_max, neg_limit=-self.steer_max)
    self.torque_from_lateral_accel = CI.torque_from_lateral_accel()
    self.use_steering_angle = self.torque_params.useSteeringAngle
    self.steering_angle_deadzone_deg = self.torque_params.steeringAngleDeadzoneDeg

    # carrot
    self.frame = 0
    self.params = Params()
    self.lateralTorqueCustom = self.params.get_int("LateralTorqueCustom")
    self.latAccelFactor_default = self.torque_params.latAccelFactor
    self.latAccelOffset_default = self.torque_params.latAccelOffset
    self.friction_default = self.torque_params.friction

    # Twilsonco's Lateral Neural Network Feedforward
    self.use_nnff = CI.use_nnff
    self.use_nnff_lite = CI.use_nnff_lite

    if self.use_nnff or self.use_nnff_lite:
      self.friction_look_ahead_v = [1.4, 2.0]
      self.friction_look_ahead_bp = [9.0, 30.0]

      self.lat_jerk_friction_factor = 0.4
      self.lat_accel_friction_factor = 0.7

      self.t_diffs = np.diff(ModelConstants.T_IDXS)
      self.desired_lat_jerk_time = self.params.get_float("SteerActuatorDelay") * 0.01 + 0.3

    if self.use_nnff:
      self.pitch = FirstOrderFilter(0.0, 0.5, 0.01)
      self.torque_from_nn = CI.get_ff_nn
      self.nn_friction_override = CI.lat_torque_nn_model.friction_override

      self.nn_time_offset = CP.steerActuatorDelay + 0.2
      future_times = [0.3, 0.6, 1.0, 1.5]
      self.nn_future_times = [i + self.nn_time_offset for i in future_times]
      self.nn_future_times_np = np.array(self.nn_future_times)

      self.past_times = [-0.3, -0.2, -0.1]
      history_check_frames = [int(abs(i)*100) for i in self.past_times]
      self.history_frame_offsets = [history_check_frames[0] - i for i in history_check_frames]
      self.lateral_accel_desired_deque = deque(maxlen=history_check_frames[0])
      self.roll_deque = deque(maxlen=history_check_frames[0])
      self.error_deque = deque(maxlen=history_check_frames[0])
      self.past_future_len = len(self.past_times) + len(self.nn_future_times)

    self.nnff_frogpilot_parity = self.use_nnff and CP.carFingerprint in NNFF_FROGPILOT_PARITY_CARS
    if self.nnff_frogpilot_parity:
      # desired_lat_jerk_time 분모가 지나치게 작아져 Jerk가 튀는 현상 방지 (최소 0.35초 안전 보장)
      self.desired_lat_jerk_time = max(0.35, self.nn_time_offset)

  def update_live_torque_params(self, latAccelFactor, latAccelOffset, friction):
    if self.lateralTorqueCustom > 0:
      return

    self.torque_params.latAccelFactor = latAccelFactor
    self.torque_params.latAccelOffset = latAccelOffset
    self.torque_params.friction = friction

  def update(self, active, CS, VM, params, steer_limited_by_controls, desired_curvature, CC, curvature_limited, model_data=None):
    self.frame += 1
    if self.frame % 10 == 0:
      lateralTorqueCustom = self.params.get_int("LateralTorqueCustom")
      if lateralTorqueCustom > 0:
        self.torque_params.latAccelFactor = self.params.get_float("LateralTorqueAccelFactor")*0.001
        self.torque_params.friction = self.params.get_float("LateralTorqueFriction")*0.001
        lateralTorqueKp = self.params.get_float("LateralTorqueKpV")*0.01
        lateralTorqueKi = self.params.get_float("LateralTorqueKiV")*0.01
        lateralTorqueKf = self.params.get_float("LateralTorqueKf")*0.01
        lateralTorqueKd = self.params.get_float("LateralTorqueKd")*0.01
        self.pid._k_p = [[0], [lateralTorqueKp]]
        self.pid._k_i = [[0], [lateralTorqueKi]]
        self.pid.k_f = lateralTorqueKf
        self.pid._k_d = [[0], [lateralTorqueKd]]
        self.torque_params.latAccelOffset = self.latAccelOffset_default
      elif self.lateralTorqueCustom > 1:  # 1 -> 0, reset to default
        self.torque_params.latAccelFactor = self.latAccelFactor_default
        self.torque_params.friction = self.friction_default
        self.torque_params.latAccelOffset = self.latAccelOffset_default
      self.lateralTorqueCustom = lateralTorqueCustom

    pid_log = log.ControlsState.LateralTorqueState.new_message()
    nn_log = None
    if not active:
      output_torque = 0.0
      pid_log.active = False
      angle_steers_des = float(CS.steeringAngleDeg)
    else:
      angle_steers_des = math.degrees(VM.get_steer_from_curvature(-desired_curvature, CS.vEgo, params.roll))
      angle_steers_des += params.angleOffsetDeg

      actual_curvature_vm = -VM.calc_curvature(math.radians(CS.steeringAngleDeg - params.angleOffsetDeg), CS.vEgo, params.roll)
      roll_compensation = params.roll * ACCELERATION_DUE_TO_GRAVITY
      actual_lateral_jerk = 0.0
      if self.use_steering_angle:
        actual_curvature = actual_curvature_vm
        curvature_deadzone = abs(VM.calc_curvature(math.radians(self.steering_angle_deadzone_deg), CS.vEgo, 0.0))
        if self.use_nnff or self.use_nnff_lite:
          actual_curvature_rate = -VM.calc_curvature(math.radians(CS.steeringRateDeg), CS.vEgo, 0.0)
          actual_lateral_jerk = actual_curvature_rate * CS.vEgo ** 2
          if not math.isfinite(actual_lateral_jerk):
            actual_lateral_jerk = 0.0

          # [패치 1] 트래버스 급커브 지그재그 방지: 고조향각 진입 시 측정 Jerk의 과도한 부호 반전 클리핑
          if abs(CS.steeringAngleDeg) > 40.0:
            actual_lateral_jerk = np.clip(actual_lateral_jerk, -1.5, 1.5)

      else:
        if len(CC.angularVelocity) >= 3:
          actual_curvature_llk = CC.angularVelocity[2] / CS.vEgo
        else:
          actual_curvature_llk = 0
        actual_curvature = np.interp(CS.vEgo, [2.0, 5.0], [actual_curvature_vm, actual_curvature_llk])
        curvature_deadzone = 0.0
      desired_lateral_accel = desired_curvature * CS.vEgo ** 2

      actual_lateral_accel = actual_curvature * CS.vEgo ** 2
      lateral_accel_deadzone = curvature_deadzone * CS.vEgo ** 2

      low_speed_factor = get_low_speed_factor(CS.vEgo, self.nnff_frogpilot_parity)
      setpoint = desired_lateral_accel + low_speed_factor * desired_curvature
      measurement = actual_lateral_accel + low_speed_factor * actual_curvature

      lateral_jerk_setpoint = 0
      lateral_jerk_measurement = 0
      lookahead_lateral_jerk = 0

      model_good = model_data is not None and len(model_data.orientation.x) >= CONTROL_N
      if model_good and (self.use_nnff or self.use_nnff_lite):
        lookahead = np.interp(CS.vEgo, self.friction_look_ahead_bp, self.friction_look_ahead_v)
        friction_upper_idx = next((i for i, val in enumerate(ModelConstants.T_IDXS) if val > lookahead), 16)
        predicted_lateral_jerk = get_predicted_lateral_jerk(model_data.acceleration.y, self.t_diffs)
        desired_lateral_jerk = (np.interp(self.desired_lat_jerk_time, ModelConstants.T_IDXS, model_data.acceleration.y) - desired_lateral_accel) / self.desired_lat_jerk_time
        lookahead_lateral_jerk = get_lookahead_value(predicted_lateral_jerk[LAT_PLAN_MIN_IDX:friction_upper_idx], desired_lateral_jerk)

        if (self.use_steering_angle and not self.nnff_frogpilot_parity) or lookahead_lateral_jerk == 0.0:
          lookahead_lateral_jerk = 0.0
          actual_lateral_jerk = 0.0
          self.lat_accel_friction_factor = 1.0
        else:
          # [패치 2 & 리셋 버그 수정]
          # 급커브(횡가속도 > 1.2) 구간: 토크 빠짐을 막기 위해 마찰 팩터 하한선 0.85 유지
          # 직선/완곡선 복귀 시: 기본 설정값(0.7)으로 정상 리셋
          if abs(desired_lateral_accel) > 1.2:
            self.lat_accel_friction_factor = max(0.85, self.lat_accel_friction_factor)
          else:
            self.lat_accel_friction_factor = 0.7

        lateral_jerk_setpoint = self.lat_jerk_friction_factor * lookahead_lateral_jerk
        lateral_jerk_measurement = self.lat_jerk_friction_factor * actual_lateral_jerk

      if self.use_nnff and model_good:
        pitch = 0
        roll = params.roll
        if len(CC.orientationNED) > 1:
          pitch = self.pitch.update(CC.orientationNED[1])
          roll = roll_pitch_adjust(roll, pitch)
        self.roll_deque.append(roll)
        self.lateral_accel_desired_deque.append(desired_lateral_accel)

        adjusted_future_times = [t + 0.5*CS.aEgo*(t/max(CS.vEgo, 1.0)) for t in self.nn_future_times]
        past_rolls = [self.roll_deque[min(len(self.roll_deque)-1, i)] for i in self.history_frame_offsets]
        future_rolls = [roll_pitch_adjust(np.interp(t, ModelConstants.T_IDXS, model_data.orientation.x) + roll, np.interp(t, ModelConstants.T_IDXS, model_data.orientation.y) + pitch) for t in adjusted_future_times]
        past_lateral_accels_desired = [self.lateral_accel_desired_deque[min(len(self.lateral_accel_desired_deque)-1, i)] for i in self.history_frame_offsets]
        future_planned_lateral_accels = [np.interp(t, ModelConstants.T_IDXS, model_data.acceleration.y) for t in adjusted_future_times]

        nnff_setpoint_input = [CS.vEgo, setpoint, lateral_jerk_setpoint, roll] \
                              + [setpoint] * self.past_future_len \
                              + past_rolls + future_rolls
        nnff_measurement_input = [CS.vEgo, measurement, lateral_jerk_measurement, roll] \
                                 + [measurement] * self.past_future_len \
                                 + past_rolls + future_rolls
        torque_from_setpoint = self.torque_from_nn(nnff_setpoint_input)
        torque_from_measurement = self.torque_from_nn(nnff_measurement_input)

        pid_log.error = float(torque_from_setpoint - torque_from_measurement)
        error_blend_factor = np.interp(abs(desired_lateral_accel), [1.0, 2.0], [0.0, 1.0])
        if error_blend_factor > 0.0:
          nnff_error_input = [CS.vEgo, setpoint - measurement, lateral_jerk_setpoint - lateral_jerk_measurement, 0.0]
          torque_from_error = self.torque_from_nn(nnff_error_input)
          if sign(pid_log.error) == sign(torque_from_error) and abs(pid_log.error) < abs(torque_from_error):
            pid_log.error = float(pid_log.error * (1.0 - error_blend_factor) + torque_from_error * error_blend_factor)

        error = setpoint - measurement
        friction_input = self.lat_accel_friction_factor * error + self.lat_jerk_friction_factor * lookahead_lateral_jerk
        nn_input = [CS.vEgo, desired_lateral_accel, friction_input, roll] \
                   + past_lateral_accels_desired + future_planned_lateral_accels \
                   + past_rolls + future_rolls
        ff = self.torque_from_nn(nn_input)

        if self.nn_friction_override:
          pid_log.error += self.torque_from_lateral_accel(LatControlInputs(0.0, 0.0, CS.vEgo, CS.aEgo), self.torque_params,
                                                          friction_input, lateral_accel_deadzone, friction_compensation=True, gravity_adjusted=False)
        nn_log = nn_input + nnff_setpoint_input + nnff_measurement_input

      else:
        gravity_adjusted_lateral_accel = desired_lateral_accel - roll_compensation
        torque_from_setpoint = self.torque_from_lateral_accel(LatControlInputs(setpoint, roll_compensation, CS.vEgo, CS.aEgo), self.torque_params,
                                                              lateral_jerk_setpoint, lateral_accel_deadzone, friction_compensation=self.use_nnff_lite, gravity_adjusted=False)
        torque_from_measurement = self.torque_from_lateral_accel(LatControlInputs(measurement, roll_compensation, CS.vEgo, CS.aEgo), self.torque_params,
                                                                  lateral_jerk_measurement, lateral_accel_deadzone, friction_compensation=self.use_nnff_lite, gravity_adjusted=False)
        pid_log.error = float(torque_from_setpoint - torque_from_measurement)
        error = desired_lateral_accel - actual_lateral_accel
        if self.use_nnff_lite:
          friction_input = self.lat_accel_friction_factor * error + self.lat_jerk_friction_factor * lookahead_lateral_jerk
        else:
          friction_input = error
        ff = self.torque_from_lateral_accel(LatControlInputs(gravity_adjusted_lateral_accel, roll_compensation, CS.vEgo, CS.aEgo), self.torque_params,
                                            friction_input, lateral_accel_deadzone, friction_compensation=True,
                                            gravity_adjusted=True)

      # [패치 3] steer_limited_by_controls 분기 제거: 고조향 한계 시 Integrator 락으로 인한 토크 0 추락 차단
      freeze_integrator = CS.steeringPressed or CS.vEgo < 5
      output_torque = self.pid.update(pid_log.error,
                                      feedforward=ff,
                                      speed=CS.vEgo,
                                      freeze_integrator=freeze_integrator)

      pid_log.active = True
      pid_log.p = float(self.pid.p)
      pid_log.i = float(self.pid.i)
      pid_log.d = float(self.pid.d)
      pid_log.f = float(self.pid.f)
      pid_log.output = float(-output_torque)
      pid_log.actualLateralAccel = float(actual_lateral_accel)
      pid_log.desiredLateralAccel = float(desired_lateral_accel)
      pid_log.saturated = bool(self._check_saturation(self.steer_max - abs(output_torque) < 1e-3, CS, steer_limited_by_controls, curvature_limited))

    return -output_torque, angle_steers_des, pid_log
