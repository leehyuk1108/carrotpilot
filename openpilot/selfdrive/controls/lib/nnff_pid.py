"""Conditional integration used by FrogPilot's torque-space NNFF controller.

Keep this policy local to NNFF; the common PID also controls other lateral and
longitudinal paths whose anti-windup behaviour must remain unchanged.
"""
import numpy as np

from openpilot.common.pid import PIDController


class NNFFPIDController(PIDController):
  def update(self, error, error_rate=0.0, speed=0.0, feedforward=0.0, freeze_integrator=False):
    self.speed = speed
    self.p = self.k_p * float(error)
    self.d = self.k_d * error_rate
    self.f = self.k_f * feedforward

    if not freeze_integrator:
      candidate_i = self.i + self.k_i * self.i_rate * error
      candidate_control = self.p + candidate_i + self.d + self.f
      upper = self.i if candidate_control > self.pos_limit else self.pos_limit
      lower = self.i if candidate_control < self.neg_limit else self.neg_limit
      self.i = np.clip(candidate_i, lower, upper)

    self.control = np.clip(self.p + self.i + self.d + self.f, self.neg_limit, self.pos_limit)
    return self.control
