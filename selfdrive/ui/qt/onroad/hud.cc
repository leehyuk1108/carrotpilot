#include "selfdrive/ui/qt/onroad/hud.h"

#include <algorithm>
#include <cmath>

#include "selfdrive/ui/qt/util.h"

constexpr int SET_SPEED_NA = 255;
constexpr int STEERING_ICON_SIZE = 156;
constexpr int STEERING_ICON_WARNING_SIZE = 188;
constexpr int LFA_ICON_SIZE = 104;
constexpr int GAP_ICON_WIDTH = 124;
constexpr int GAP_ICON_HEIGHT = 84;
constexpr int CARROT_BADGE_WIDTH = 112;
constexpr int CARROT_BADGE_HEIGHT = 52;
constexpr int CARROT_BADGE_GAP = 18;
constexpr int TURN_SIGNAL_ICON_SIZE = 72;
constexpr int TURN_SIGNAL_GAP = 28;
constexpr int HUD_SIDE_MARGIN = 104;
constexpr int HUD_BOTTOM_MARGIN = 96;
constexpr float PREVIEW_SPEED_KPH = 192.0f;
constexpr float PREVIEW_SET_SPEED_KPH = 65.0f;
constexpr qint64 TURN_SIGNAL_CYCLE_MS = 800;
constexpr qint64 TURN_SIGNAL_ON_MS = 420;

namespace {
QColor lateralOnlyMint() {
  return QColor(0x67, 0xF5, 0xD1);
}

QPixmap tintTurnSignalIcon(const QString &path, const QSize &size, const QColor &tint) {
  QPixmap src = loadPixmap(path, size);
  if (src.isNull()) return src;

  QImage img = src.toImage().convertToFormat(QImage::Format_ARGB32);
  for (int y = 0; y < img.height(); ++y) {
    for (int x = 0; x < img.width(); ++x) {
      QColor px = img.pixelColor(x, y);
      if (px.alpha() < 140) {
        px.setAlpha(0);
      } else {
        px.setAlpha(255);
        px.setRed(tint.red());
        px.setGreen(tint.green());
        px.setBlue(tint.blue());
      }
      img.setPixelColor(x, y, px);
    }
  }
  return QPixmap::fromImage(img);
}

QColor blendColor(const QColor &a, const QColor &b, float t) {
  t = std::clamp(t, 0.0f, 1.0f);
  return QColor(
    std::lround(a.red() + (b.red() - a.red()) * t),
    std::lround(a.green() + (b.green() - a.green()) * t),
    std::lround(a.blue() + (b.blue() - a.blue()) * t),
    std::lround(a.alpha() + (b.alpha() - a.alpha()) * t)
  );
}

QRect getSetSpeedRect(const QRect &surface_rect) {
  const int gap = 28;
  const int block_width = 236;
  const int block_height = 118;
  return QRect(surface_rect.width() - HUD_SIDE_MARGIN - STEERING_ICON_SIZE - gap - block_width,
               surface_rect.height() - HUD_BOTTOM_MARGIN - block_height,
               block_width, block_height);
}
}  // namespace

HudRenderer::HudRenderer() {}

void HudRenderer::updateState(const UIState &s) {
  is_metric = s.scene.is_metric;
  status = s.status;

  const SubMaster &sm = *(s.sm);
  const auto &selfdrive_state = sm["selfdriveState"].getSelfdriveState();
  const QString current_alert_type = QString::fromUtf8(selfdrive_state.getAlertType().cStr());
  bool torque_preview_ok = false;
  const float torque_preview = qEnvironmentVariable("STEERING_TORQUE_PREVIEW").trimmed().toFloat(&torque_preview_ok);
  bool angle_preview_ok = false;
  const float angle_preview = qEnvironmentVariable("STEERING_ANGLE_PREVIEW").trimmed().toFloat(&angle_preview_ok);
  const bool accel_override_preview = qEnvironmentVariableIntValue("LFA_ACCEL_OVERRIDE_PREVIEW") == 1;
  const bool steering_override_preview = qEnvironmentVariableIntValue("STEERING_OVERRIDE_PREVIEW") == 1;
  bool gap_preview_ok = false;
  const int gap_preview = qEnvironmentVariableIntValue("GAP_PREVIEW", &gap_preview_ok);
  bool carrot_preview_ok = false;
  const int carrot_preview = qEnvironmentVariableIntValue("CARROT_BADGE_PREVIEW", &carrot_preview_ok);
  const bool left_turn_signal_preview = qEnvironmentVariableIntValue("TURN_SIGNAL_LEFT_PREVIEW") == 1;
  const bool right_turn_signal_preview = qEnvironmentVariableIntValue("TURN_SIGNAL_RIGHT_PREVIEW") == 1;
  const bool always_lateral_preview = qEnvironmentVariableIntValue("ALWAYS_LATERAL_PREVIEW") == 1;

  selfdrive_enabled = selfdrive_state.getEnabled();
  selfdrive_engageable = selfdrive_state.getEngageable() || selfdrive_enabled;
  longitudinal_override_active = accel_override_preview;
  lateral_override_active = steering_override_preview;
  always_on_lateral_active = always_lateral_preview;
  steer_limit_warning_active = current_alert_type.contains("steerSaturated", Qt::CaseInsensitive) ||
                               qEnvironmentVariableIntValue("STEER_LIMIT_PREVIEW") == 1 ||
                               (torque_preview_ok && torque_preview >= 0.95f);
  steering_torque_pct = 0.0f;
  steering_angle_deg = angle_preview_ok ? angle_preview : 0.0f;
  gap_level = std::clamp(Params().getInt("LongitudinalPersonality") + 1, 1, 4);
  if (gap_preview_ok && gap_preview > 0) {
    gap_level = std::clamp(gap_preview, 1, 4);
  } else if (sm.rcv_frame("selfdriveState") > 0) {
    gap_level = std::clamp(static_cast<int>(selfdrive_state.getPersonality()) + 1, 1, 4);
  }
  carrot_active_level = 0;
  left_turn_signal_active = left_turn_signal_preview;
  right_turn_signal_active = right_turn_signal_preview;
  if (sm.rcv_frame("carrotMan") > 0) {
    carrot_active_level = sm["carrotMan"].getCarrotMan().getActiveCarrot();
  } else if (carrot_preview_ok && carrot_preview > 0) {
    carrot_active_level = carrot_preview;
  }

  const bool force_preview = Params().getBool("ForceOnroad") || qEnvironmentVariableIntValue("FORCE_ONROAD_PREVIEW") == 1;
  const bool resume_required_active = current_alert_type.contains("resumeRequired", Qt::CaseInsensitive);
  bool standstill_preview_ok = false;
  const int standstill_preview = qEnvironmentVariableIntValue("STANDSTILL_PREVIEW", &standstill_preview_ok);

  if (resume_required_active) {
    standstill_duration = 0;
    standstill_timer.invalidate();
  } else if (standstill_preview_ok && standstill_preview >= 0) {
    standstill_duration = standstill_preview;
    standstill_timer.invalidate();
  }

  if (force_preview && sm.rcv_frame("carState") < s.scene.started_frame) {
    is_cruise_set = true;
    is_cruise_available = true;
    set_speed = is_metric ? PREVIEW_SET_SPEED_KPH : PREVIEW_SET_SPEED_KPH * KM_TO_MILE;
    speed = is_metric ? PREVIEW_SPEED_KPH : PREVIEW_SPEED_KPH * KM_TO_MILE;
    if (!standstill_preview_ok) {
      standstill_duration = 0;
    }
    return;
  }

  if (sm.rcv_frame("carState") < s.scene.started_frame) {
    is_cruise_set = false;
    set_speed = SET_SPEED_NA;
    speed = 0.0f;
    if (!standstill_preview_ok) {
      standstill_duration = 0;
    }
    return;
  }

  const auto &car_state = sm["carState"].getCarState();
  const auto &controls_state = sm["controlsState"].getControlsState();
  const auto &car_control = sm["carControl"].getCarControl();
  const auto lateral_state = controls_state.getLateralControlState();
  const auto lateral_which = lateral_state.which();
  const bool is_overriding = selfdrive_state.getState() == cereal::SelfdriveState::OpenpilotState::OVERRIDING;
  left_turn_signal_active = left_turn_signal_active || car_state.getLeftBlinker();
  right_turn_signal_active = right_turn_signal_active || car_state.getRightBlinker();

  always_on_lateral_active = always_on_lateral_active ||
                             (!selfdrive_enabled && !car_control.getLongActive() &&
                              (car_control.getLatActive() || car_state.getLatEnabled()));

  if (is_overriding) {
    longitudinal_override_active = longitudinal_override_active || car_state.getGasPressed();
    lateral_override_active = lateral_override_active || car_state.getSteeringPressed();
  }
  if (!angle_preview_ok) {
    steering_angle_deg = -car_state.getSteeringAngleDeg();
  }

  switch (lateral_which) {
    case cereal::ControlsState::LateralControlState::TORQUE_STATE: {
      const auto torque_state = lateral_state.getTorqueState();
      steering_torque_pct = std::clamp(std::abs(torque_state.getOutput()), 0.0f, 1.0f);
      if (torque_state.getSaturated()) steering_torque_pct = 1.0f;
      break;
    }
    case cereal::ControlsState::LateralControlState::PID_STATE: {
      const auto pid_state = lateral_state.getPidState();
      steering_torque_pct = std::clamp(std::abs(pid_state.getOutput()), 0.0f, 1.0f);
      if (pid_state.getSaturated()) steering_torque_pct = 1.0f;
      break;
    }
    case cereal::ControlsState::LateralControlState::ANGLE_STATE: {
      const auto angle_state = lateral_state.getAngleState();
      steering_torque_pct = std::clamp(std::abs(angle_state.getOutput()), 0.0f, 1.0f);
      if (angle_state.getSaturated()) steering_torque_pct = 1.0f;
      break;
    }
    case cereal::ControlsState::LateralControlState::DEBUG_STATE: {
      const auto debug_state = lateral_state.getDebugState();
      steering_torque_pct = std::clamp(std::abs(debug_state.getOutput()), 0.0f, 1.0f);
      if (debug_state.getSaturated()) steering_torque_pct = 1.0f;
      break;
    }
    default:
      break;
  }

  if (!standstill_preview_ok) {
    const bool standstill = car_state.getStandstill() && car_state.getGearShifter() != cereal::CarState::GearShifter::REVERSE;
    if (standstill) {
      if (!standstill_timer.isValid()) {
        standstill_timer.start();
      }
      int elapsed_seconds = standstill_timer.elapsed() / 1000;
      standstill_duration = elapsed_seconds >= 60 ? elapsed_seconds : 0;
    } else {
      standstill_duration = 0;
      standstill_timer.invalidate();
    }
  }

  set_speed = car_state.getVCruiseCluster() == 0.0 ? controls_state.getVCruiseDEPRECATED() : car_state.getVCruiseCluster();
  is_cruise_set = set_speed > 0 && set_speed != SET_SPEED_NA;
  is_cruise_available = set_speed != -1;

  if (is_cruise_set && !is_metric) {
    set_speed *= KM_TO_MILE;
  }

  v_ego_cluster_seen = v_ego_cluster_seen || car_state.getVEgoCluster() != 0.0;
  const float v_ego = v_ego_cluster_seen ? car_state.getVEgoCluster() : car_state.getVEgo();
  speed = std::max<float>(0.0f, v_ego * (is_metric ? MS_TO_KPH : MS_TO_MPH));
}

void HudRenderer::draw(QPainter &p, const QRect &surface_rect) {
  p.save();

  if (is_cruise_available) {
    drawSetSpeed(p, surface_rect);
  }
  if (standstill_duration == 0) {
    drawCurrentSpeed(p, surface_rect);
  } else {
    drawStandstillTimer(p, surface_rect);
  }
  drawTurnSignalIcons(p, surface_rect);
  drawCarrotBadge(p, surface_rect);
  drawGapIcon(p, surface_rect);
  drawLfaIcon(p, surface_rect);
  drawSteeringLimitWarningIcon(p, surface_rect);
  drawSteeringWheelIcon(p, surface_rect);

  p.restore();
}

void HudRenderer::drawSetSpeed(QPainter &p, const QRect &surface_rect) {
  QString set_speed_str = is_cruise_set ? QString::number(std::nearbyint(set_speed)) : "–";
  QColor value_color = QColor(0xF2, 0xF5, 0xF9, 0xF2);
  QColor label_color = QColor(0xD1, 0xD8, 0xDF, 0xD0);
  if (!is_cruise_set || status == STATUS_DISENGAGED) {
    value_color = QColor(0xD2, 0xD9, 0xE1, 0xCA);
    label_color = QColor(0xA7, 0xB0, 0xBA, 0xB8);
  } else if (status == STATUS_OVERRIDE) {
    value_color = QColor(0xF3, 0xE6, 0xCB, 0xE2);
    label_color = QColor(0xD6, 0xC3, 0x98, 0xBA);
  }

  const QFont label_font = InterFont(28, QFont::DemiBold);
  const QFont value_font = InterFont(88, QFont::Bold);
  QRect set_speed_rect = getSetSpeedRect(surface_rect);
  const int block_height = set_speed_rect.height();

  p.setFont(label_font);
  drawText(p, QRect(set_speed_rect.x(), set_speed_rect.y() + 44, 72, 34), tr("SET"), label_color,
           Qt::AlignLeft | Qt::AlignVCenter);

  p.setFont(value_font);
  drawText(p, QRect(set_speed_rect.x() + 72, set_speed_rect.y() - 6, set_speed_rect.width() - 72, block_height), set_speed_str,
           value_color, Qt::AlignLeft | Qt::AlignVCenter);
}

void HudRenderer::drawCurrentSpeed(QPainter &p, const QRect &surface_rect) {
  QString speed_str = QString::number(std::nearbyint(speed));
  QColor speed_color = QColor(0xF6, 0xF8, 0xFB, 0xF4);
  QColor unit_color = QColor(0xDF, 0xE5, 0xEA, 0xD4);

  if (status == STATUS_DISENGAGED) {
    speed_color = QColor(0xE2, 0xE7, 0xEC, 0xE6);
    unit_color = QColor(0xC5, 0xCF, 0xD8, 0xC8);
  } else if (status == STATUS_OVERRIDE) {
    unit_color = QColor(0xE8, 0xDC, 0xC2, 0xC4);
  }

  const int group_top = surface_rect.height() - 246;
  const int center_x = surface_rect.center().x();
  p.setFont(InterFont(148, QFont::Bold));
  QRect speed_rect(center_x - 230, group_top - 18, 460, 162);
  drawText(p, speed_rect, speed_str, speed_color, Qt::AlignHCenter | Qt::AlignBottom);

  p.setFont(InterFont(34, QFont::DemiBold));
  QRect unit_rect(center_x - 116, speed_rect.bottom() + 2, 232, 40);
  drawText(p, unit_rect, is_metric ? tr("KM/H") : tr("MPH"), unit_color, Qt::AlignHCenter | Qt::AlignTop);
}

void HudRenderer::drawStandstillTimer(QPainter &p, const QRect &surface_rect) {
  const int minutes = standstill_duration / 60;
  const int seconds = standstill_duration % 60;
  const QString timer_text = QString("%1:%2").arg(minutes).arg(seconds, 2, 10, QChar('0'));

  const int group_top = surface_rect.height() - 246;
  const int center_x = surface_rect.center().x();

  p.setFont(InterFont(132, QFont::Bold));
  drawText(p, QRect(center_x - 260, group_top - 6, 520, 150), timer_text, QColor(0xFF, 0xFF, 0xFF),
           Qt::AlignHCenter | Qt::AlignBottom);
}

void HudRenderer::drawTurnSignalIcons(QPainter &p, const QRect &surface_rect) {
  static const QColor signal_color(0x1E, 0xFF, 0x5C);
  static const QPixmap left_img = tintTurnSignalIcon("../../files/icons/turn_signal_left.png", {TURN_SIGNAL_ICON_SIZE, TURN_SIGNAL_ICON_SIZE}, signal_color);
  static const QPixmap right_img = tintTurnSignalIcon("../../files/icons/turn_signal_right.png", {TURN_SIGNAL_ICON_SIZE, TURN_SIGNAL_ICON_SIZE}, signal_color);
  if ((!left_turn_signal_active || left_img.isNull()) && (!right_turn_signal_active || right_img.isNull())) return;

  const int group_top = surface_rect.height() - 246;
  const int center_x = surface_rect.center().x();
  const QRect speed_rect(center_x - 230, group_top - 18, 460, 162);
  const int icon_y = speed_rect.center().y() - (TURN_SIGNAL_ICON_SIZE / 2) + 2;
  const QRect left_rect(speed_rect.left() - TURN_SIGNAL_ICON_SIZE - TURN_SIGNAL_GAP, icon_y,
                        TURN_SIGNAL_ICON_SIZE, TURN_SIGNAL_ICON_SIZE);
  const QRect right_rect(speed_rect.right() + TURN_SIGNAL_GAP, icon_y,
                         TURN_SIGNAL_ICON_SIZE, TURN_SIGNAL_ICON_SIZE);
  const qint64 blink_phase = QDateTime::currentMSecsSinceEpoch() % TURN_SIGNAL_CYCLE_MS;
  const bool blink_on = blink_phase < TURN_SIGNAL_ON_MS;

  auto draw_indicator = [&](const QRect &icon_rect, const QPixmap &icon, bool active) {
    if (!active || icon.isNull()) return;

    const qreal icon_opacity = blink_on ? 0.96 : 0.14;

    p.save();
    p.setRenderHint(QPainter::Antialiasing);
    p.setRenderHint(QPainter::SmoothPixmapTransform);

    p.setOpacity(icon_opacity);
    p.drawPixmap(icon_rect, icon);
    p.restore();
  };

  draw_indicator(left_rect, left_img, left_turn_signal_active);
  draw_indicator(right_rect, right_img, right_turn_signal_active);
}

void HudRenderer::drawGapIcon(QPainter &p, const QRect &surface_rect) {
  static const QPixmap gap1_img = loadPixmap("../../files/icons/gap1.png", {GAP_ICON_WIDTH, GAP_ICON_HEIGHT});
  static const QPixmap gap2_img = loadPixmap("../../files/icons/gap2.png", {GAP_ICON_WIDTH, GAP_ICON_HEIGHT});
  static const QPixmap gap3_img = loadPixmap("../../files/icons/gap3.png", {GAP_ICON_WIDTH, GAP_ICON_HEIGHT});
  static const QPixmap gap4_img = loadPixmap("../../files/icons/gap4.png", {GAP_ICON_WIDTH, GAP_ICON_HEIGHT});

  const QPixmap *gap_img = nullptr;
  switch (gap_level) {
    case 1: gap_img = &gap1_img; break;
    case 2: gap_img = &gap2_img; break;
    case 3: gap_img = &gap3_img; break;
    case 4: gap_img = &gap4_img; break;
    default: return;
  }
  if (gap_img == nullptr || gap_img->isNull()) return;

  QColor tint = QColor(0xF6, 0xF8, 0xFB, 0xEE);
  if (status == STATUS_DISENGAGED) {
    tint = QColor(0xD4, 0xDD, 0xE6, 0xDA);
  } else if (status == STATUS_OVERRIDE) {
    tint = QColor(0xF2, 0xE6, 0xC9, 0xEA);
  }

  QRect set_speed_rect = getSetSpeedRect(surface_rect);
  QRect icon_rect(set_speed_rect.left() - GAP_ICON_WIDTH - 34,
                  set_speed_rect.top() + ((set_speed_rect.height() - GAP_ICON_HEIGHT) / 2) + 2,
                  GAP_ICON_WIDTH,
                  GAP_ICON_HEIGHT);

  QPixmap shadow(gap_img->size());
  shadow.fill(Qt::transparent);
  {
    QPainter shadow_painter(&shadow);
    shadow_painter.drawPixmap(0, 0, *gap_img);
    shadow_painter.setCompositionMode(QPainter::CompositionMode_SourceIn);
    shadow_painter.fillRect(shadow.rect(), QColor(0x00, 0x00, 0x00, 0x74));
  }

  QPixmap tinted(gap_img->size());
  tinted.fill(Qt::transparent);
  {
    QPainter painter(&tinted);
    painter.drawPixmap(0, 0, *gap_img);
    painter.setCompositionMode(QPainter::CompositionMode_SourceIn);
    painter.fillRect(tinted.rect(), tint);
  }

  p.save();
  p.setRenderHint(QPainter::SmoothPixmapTransform);
  p.drawPixmap(icon_rect.translated(0, 4), shadow);
  p.drawPixmap(icon_rect, tinted);
  p.restore();
}

void HudRenderer::drawCarrotBadge(QPainter &p, const QRect &surface_rect) {
  if (carrot_active_level < 1) return;

  QRect set_speed_rect = getSetSpeedRect(surface_rect);
  QRect gap_rect(set_speed_rect.left() - GAP_ICON_WIDTH - 34,
                 set_speed_rect.top() + ((set_speed_rect.height() - GAP_ICON_HEIGHT) / 2) + 2,
                 GAP_ICON_WIDTH,
                 GAP_ICON_HEIGHT);
  QRect badge_rect(gap_rect.left() + ((gap_rect.width() - CARROT_BADGE_WIDTH) / 2),
                   gap_rect.top() - CARROT_BADGE_HEIGHT - CARROT_BADGE_GAP,
                   CARROT_BADGE_WIDTH,
                   CARROT_BADGE_HEIGHT);

  QColor badge_color = carrot_active_level >= 2 ? QColor(0x2F, 0xC8, 0x67, 0xF2)
                                                : QColor(0x4F, 0x8D, 0xFF, 0xEC);
  QColor shadow_color = QColor(0x00, 0x00, 0x00, 0x56);

  p.save();
  p.setRenderHint(QPainter::Antialiasing);
  p.setPen(Qt::NoPen);
  p.setBrush(shadow_color);
  p.drawRoundedRect(badge_rect.translated(0, 4), 18, 18);
  p.setBrush(badge_color);
  p.drawRoundedRect(badge_rect, 18, 18);

  p.setFont(InterFont(28, QFont::Bold));
  drawText(p, badge_rect, "APN", QColor(0xFF, 0xFF, 0xFF), Qt::AlignCenter);
  p.restore();
}

void HudRenderer::drawLfaIcon(QPainter &p, const QRect &surface_rect) {
  static const QPixmap lfa_img = loadPixmap("../../files/icons/lfa.png", {LFA_ICON_SIZE, LFA_ICON_SIZE});
  if (lfa_img.isNull()) return;

  const int group_top = surface_rect.height() - 246;
  const int center_x = surface_rect.center().x();
  QRect speed_rect(center_x - 230, group_top - 18, 460, 162);
  QRect icon_rect(speed_rect.right() - 84, speed_rect.top() + 2, LFA_ICON_SIZE, LFA_ICON_SIZE);
  const bool active_preview = qEnvironmentVariableIntValue("LFA_ACTIVE_PREVIEW") == 1;

  QColor tint = QColor(0x92, 0x9D, 0xA8, 0xE6);
  QColor glow_base(0x49, 0xD2, 0x83);
  if (longitudinal_override_active) {
    tint = QColor(0x4F, 0x8D, 0xFF);
    glow_base = QColor(0x4F, 0x8D, 0xFF);
  } else if (always_on_lateral_active) {
    tint = lateralOnlyMint();
    glow_base = lateralOnlyMint();
  } else if (selfdrive_enabled || active_preview) {
    tint = QColor(0x49, 0xD2, 0x83);
  } else if (selfdrive_engageable) {
    tint = QColor(0xF4, 0xF7, 0xFB, 0xF4);
  }

  if (selfdrive_enabled || active_preview || longitudinal_override_active || always_on_lateral_active) {
    p.save();
    p.setRenderHint(QPainter::Antialiasing);
    p.setPen(Qt::NoPen);

    QRect glow_rect = icon_rect.adjusted(-26, -22, 26, 24);
    QRadialGradient glow(glow_rect.center(), glow_rect.width() * 0.55);
    glow.setColorAt(0.0, QColor(glow_base.red(), glow_base.green(), glow_base.blue(), 76));
    glow.setColorAt(0.45, QColor(glow_base.red(), glow_base.green(), glow_base.blue(), 34));
    glow.setColorAt(1.0, QColor(glow_base.red(), glow_base.green(), glow_base.blue(), 0));
    p.setBrush(glow);
    p.drawEllipse(glow_rect);
    p.restore();
  }

  QPixmap tinted(lfa_img.size());
  tinted.fill(Qt::transparent);

  QPainter painter(&tinted);
  painter.drawPixmap(0, 0, lfa_img);
  painter.setCompositionMode(QPainter::CompositionMode_SourceIn);
  painter.fillRect(tinted.rect(), tint);
  painter.end();

  p.save();
  p.setRenderHint(QPainter::SmoothPixmapTransform);
  p.drawPixmap(icon_rect, tinted);
  p.restore();
}

void HudRenderer::drawSteeringWheelIcon(QPainter &p, const QRect &surface_rect) {
  const int wheel_button_size = steer_limit_warning_active ? STEERING_ICON_WARNING_SIZE : STEERING_ICON_SIZE;
  const int wheel_icon_size = steer_limit_warning_active ? 178 : 150;
  const QPixmap wheel_img = loadPixmap("../../files/icons/steeringwheel.png", {wheel_icon_size, wheel_icon_size});
  bool torque_preview_ok = false;
  const float torque_preview = qEnvironmentVariable("STEERING_TORQUE_PREVIEW").trimmed().toFloat(&torque_preview_ok);

  QPoint center(surface_rect.width() - HUD_SIDE_MARGIN - wheel_button_size / 2,
                surface_rect.height() - HUD_BOTTOM_MARGIN - wheel_button_size / 2);

  QColor bg = QColor(0x0A, 0x10, 0x16, 0xA8);
  QColor tint = QColor(0x9F, 0xA8, 0xB2, 0xE6);
  const bool steering_active = selfdrive_enabled || longitudinal_override_active || lateral_override_active ||
                               always_on_lateral_active || (torque_preview_ok && torque_preview >= 0.0f);
  const float torque_level = torque_preview_ok ? std::clamp(torque_preview, 0.0f, 1.0f) : steering_torque_pct;

  if (lateral_override_active) {
    bg = QColor(0x0A, 0x12, 0x1F, 0xB8);
    tint = QColor(0x4F, 0x8D, 0xFF);
  } else if (steering_active) {
    const QColor active_white(0xF4, 0xF7, 0xFB, 0xF4);
    const QColor warm_color(0xFF, 0xC7, 0x58, 0xF6);
    const QColor hot_color(0xFF, 0x5D, 0x57, 0xF8);
    bg = QColor(0x0D, 0x16, 0x12, 0xB6);
    if (torque_level < 0.65f) {
      tint = blendColor(active_white, warm_color, torque_level / 0.65f * 0.35f);
    } else {
      tint = blendColor(warm_color, hot_color, (torque_level - 0.65f) / 0.35f);
    }
  }

  p.save();
  p.setRenderHint(QPainter::Antialiasing);
  p.setPen(Qt::NoPen);
  p.setBrush(bg);
  p.drawEllipse(center, wheel_button_size / 2, wheel_button_size / 2);

  if (!wheel_img.isNull()) {
    QPixmap tinted(wheel_img.size());
    tinted.fill(Qt::transparent);

    QPainter painter(&tinted);
    painter.drawPixmap(0, 0, wheel_img);
    painter.setCompositionMode(QPainter::CompositionMode_SourceIn);
    painter.fillRect(tinted.rect(), tint);
    painter.end();

    p.save();
    p.setRenderHint(QPainter::SmoothPixmapTransform);
    p.translate(center);
    p.rotate(steering_angle_deg);
    p.drawPixmap(-tinted.width() / 2, -tinted.height() / 2, tinted);
    p.restore();
  }
  p.restore();
}

void HudRenderer::drawSteeringLimitWarningIcon(QPainter &p, const QRect &surface_rect) {
  if (!steer_limit_warning_active) return;

  static const int warning_icon_size = 76;
  static const QPixmap warning_img = loadPixmap("../../files/icons/warning.png", {warning_icon_size, warning_icon_size});
  if (warning_img.isNull()) return;

  const QPoint wheel_center(surface_rect.width() - HUD_SIDE_MARGIN - STEERING_ICON_WARNING_SIZE / 2,
                            surface_rect.height() - HUD_BOTTOM_MARGIN - STEERING_ICON_WARNING_SIZE / 2);
  const QRect wheel_rect(wheel_center.x() - STEERING_ICON_WARNING_SIZE / 2,
                         wheel_center.y() - STEERING_ICON_WARNING_SIZE / 2,
                         STEERING_ICON_WARNING_SIZE,
                         STEERING_ICON_WARNING_SIZE);
  const QRect icon_rect(wheel_rect.left() - 84, wheel_rect.top() - 20, warning_icon_size, warning_icon_size);

  p.save();
  p.setRenderHint(QPainter::Antialiasing);
  p.setRenderHint(QPainter::SmoothPixmapTransform);

  QRect glow_rect = icon_rect.adjusted(-26, -22, 26, 22);
  QRadialGradient glow(glow_rect.center(), glow_rect.width() * 0.55);
  glow.setColorAt(0.0, QColor(0xFF, 0x68, 0x5B, 64));
  glow.setColorAt(0.45, QColor(0xFF, 0x68, 0x5B, 26));
  glow.setColorAt(1.0, QColor(0xFF, 0x68, 0x5B, 0));
  p.setPen(Qt::NoPen);
  p.setBrush(glow);
  p.drawEllipse(glow_rect);
  p.drawPixmap(icon_rect, warning_img);
  p.restore();
}

void HudRenderer::drawText(QPainter &p, const QRect &rect, const QString &text, const QColor &color, Qt::Alignment alignment) {
  p.setPen(color);
  p.drawText(rect, alignment | Qt::TextWordWrap, text);
}
