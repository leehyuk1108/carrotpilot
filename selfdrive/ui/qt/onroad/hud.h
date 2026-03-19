#pragma once

#include <QElapsedTimer>
#include <QPainter>

#include "selfdrive/ui/ui.h"

class HudRenderer : public QObject {
  Q_OBJECT

public:
  HudRenderer();
  void updateState(const UIState &s);
  void draw(QPainter &p, const QRect &surface_rect);

private:
  void drawSetSpeed(QPainter &p, const QRect &surface_rect);
  void drawCurrentSpeed(QPainter &p, const QRect &surface_rect);
  void drawStandstillTimer(QPainter &p, const QRect &surface_rect);
  void drawCarrotBadge(QPainter &p, const QRect &surface_rect);
  void drawGapIcon(QPainter &p, const QRect &surface_rect);
  void drawLfaIcon(QPainter &p, const QRect &surface_rect);
  void drawSteeringLimitWarningIcon(QPainter &p, const QRect &surface_rect);
  void drawSteeringWheelIcon(QPainter &p, const QRect &surface_rect);
  void drawText(QPainter &p, const QRect &rect, const QString &text, const QColor &color = QColor(0xFF, 0xFF, 0xFF),
                Qt::Alignment alignment = Qt::AlignLeft | Qt::AlignVCenter);

  float speed = 0;
  float set_speed = 0;
  bool is_cruise_set = false;
  bool is_cruise_available = true;
  bool is_metric = false;
  bool v_ego_cluster_seen = false;
  bool selfdrive_enabled = false;
  bool selfdrive_engageable = false;
  bool longitudinal_override_active = false;
  bool lateral_override_active = false;
  bool always_on_lateral_active = false;
  bool steer_limit_warning_active = false;
  int standstill_duration = 0;
  QElapsedTimer standstill_timer;
  float steering_torque_pct = 0.0f;
  float steering_angle_deg = 0.0f;
  int gap_level = 0;
  int carrot_active_level = 0;
  int status = STATUS_DISENGAGED;
};
