#include "selfdrive/ui/qt/onroad/alerts.h"

#include <QDateTime>
#include <QPainter>
#include <map>

#include "selfdrive/ui/qt/util.h"

void OnroadAlerts::updateState(const UIState &s) {
  Alert a = getAlert(*(s.sm), s.scene.started_frame);
  const auto selfdrive_state = (*s.sm)["selfdriveState"].getSelfdriveState();
  const bool selfdrive_enabled = selfdrive_state.getEnabled();
  const bool selfdrive_engageable = selfdrive_state.getEngageable() || selfdrive_enabled;
  const bool was_resume_required = alert.type.contains("resumeRequired", Qt::CaseInsensitive);
  const bool is_resume_required = a.type.contains("resumeRequired", Qt::CaseInsensitive);
  const bool is_lane_change_alert =
    a.type.contains("laneChange", Qt::CaseInsensitive) &&
    !a.type.contains("Blocked", Qt::CaseInsensitive) &&
    !a.type.contains("preLaneChange", Qt::CaseInsensitive);
  const bool animate_special_alert =
    a.type.contains("fcw", Qt::CaseInsensitive) ||
    a.type.contains("aeb", Qt::CaseInsensitive) ||
    a.type.contains("ldw", Qt::CaseInsensitive) ||
    is_lane_change_alert ||
    is_resume_required;

  if (is_resume_required && !was_resume_required) {
    resumeRequiredTimer.restart();
  } else if (!is_resume_required && was_resume_required) {
    resumeRequiredTimer.invalidate();
  }

  if (!alert.equal(a) || selfdriveEnabled != selfdrive_enabled || selfdriveEngageable != selfdrive_engageable || animate_special_alert) {
    alert = a;
    selfdriveEnabled = selfdrive_enabled;
    selfdriveEngageable = selfdrive_engageable;
    update();
  }
}

void OnroadAlerts::clear() {
  alert = {};
  alertHeight = 0;
  selfdriveEnabled = false;
  selfdriveEngageable = false;
  resumeRequiredTimer.invalidate();
  update();
}

OnroadAlerts::Alert OnroadAlerts::getAlert(const SubMaster &sm, uint64_t started_frame) {
  const cereal::SelfdriveState::Reader &ss = sm["selfdriveState"].getSelfdriveState();
  const uint64_t selfdrive_frame = sm.rcv_frame("selfdriveState");
  const bool force_onroad_preview = qEnvironmentVariableIntValue("FORCE_ONROAD_PREVIEW") == 1;
  static int lead_departing_frames = 0;

  if (qEnvironmentVariableIntValue("FCW_PREVIEW") == 1) {
    return {tr("전방 추돌 주의"), tr("전방 차량과 추돌 위험이 있습니다"),
            "fcwPreview", cereal::SelfdriveState::AlertSize::MID,
            cereal::SelfdriveState::AlertStatus::CRITICAL};
  } else if (qEnvironmentVariableIntValue("LDW_PREVIEW") == 1) {
    return {tr("차선 이탈 감지됨"), tr("운전에 주의하세요"),
            "ldwPreview", cereal::SelfdriveState::AlertSize::MID,
            cereal::SelfdriveState::AlertStatus::USER_PROMPT};
  } else if (qEnvironmentVariableIntValue("GREEN_LIGHT_PREVIEW") == 1) {
    return {tr("신호가 변경되었습니다"), "",
            "greenLightPreview", cereal::SelfdriveState::AlertSize::SMALL,
            cereal::SelfdriveState::AlertStatus::NORMAL};
  } else if (qEnvironmentVariableIntValue("BELOW_STEER_SPEED_PREVIEW") == 1) {
    return {tr("조향 보조 비활성화됨"), tr("30 km/h 이상으로 주행하면 다시 활성화됩니다"),
            "belowSteerSpeedPreview", cereal::SelfdriveState::AlertSize::FULL,
            cereal::SelfdriveState::AlertStatus::NORMAL};
  } else if (qEnvironmentVariableIntValue("LEAD_DEPARTING_PREVIEW") == 1) {
    return {"", tr("선행 차량이 출발하였습니다"),
            "leadDepartingPreview", cereal::SelfdriveState::AlertSize::FULL,
            cereal::SelfdriveState::AlertStatus::NORMAL};
  } else if (qEnvironmentVariableIntValue("RESUME_REQUIRED_PREVIEW") == 1) {
    return {tr("오토 홀드"), tr("해제하려면 악셀을 밟거나 RES버튼을 누르세요"),
            "resumeRequiredPreview", cereal::SelfdriveState::AlertSize::MID,
            cereal::SelfdriveState::AlertStatus::NORMAL};
  }

  Alert a = {};
  if (selfdrive_frame >= started_frame) {
    a = {ss.getAlertText1().cStr(), ss.getAlertText2().cStr(),
         ss.getAlertType().cStr(), ss.getAlertSize(), ss.getAlertStatus()};
  }

  const auto car_state = sm["carState"].getCarState();
  const auto lead_one = sm["radarState"].getRadarState().getLeadOne();
  const bool standstill = car_state.getStandstill() && car_state.getGearShifter() != cereal::CarState::GearShifter::REVERSE;
  const bool lead_departing_detected = standstill &&
                                       lead_one.getStatus() &&
                                       lead_one.getDRel() > 1.0f &&
                                       lead_one.getDRel() < 30.0f &&
                                       (lead_one.getVRel() > 0.8f || lead_one.getVLeadK() > (car_state.getVEgo() + 0.8f));
  if (lead_departing_detected) {
    lead_departing_frames = 3 * UI_FREQ;
  } else if (lead_departing_frames > 0) {
    lead_departing_frames--;
  }

  const bool selfdrive_resume_required = a.type.contains("resumeRequired", Qt::CaseInsensitive);
  if ((a.size == cereal::SelfdriveState::AlertSize::NONE || selfdrive_resume_required) && lead_departing_frames > 0) {
    a = {"", tr("선행 차량이 출발하였습니다"),
         "leadDepartingUi", cereal::SelfdriveState::AlertSize::FULL,
         cereal::SelfdriveState::AlertStatus::NORMAL};
  }

  if (!force_onroad_preview && !sm.updated("selfdriveState") && (sm.frame - started_frame) > 5 * UI_FREQ) {
    const int SELFDRIVE_STATE_TIMEOUT = 5;
    const int ss_missing = (nanos_since_boot() - sm.rcv_time("selfdriveState")) / 1e9;

    if (selfdrive_frame < started_frame) {
      a = {tr("openpilot Unavailable"), tr("Waiting to start"),
           "selfdriveWaiting", cereal::SelfdriveState::AlertSize::MID,
           cereal::SelfdriveState::AlertStatus::NORMAL};
    } else if (ss_missing > SELFDRIVE_STATE_TIMEOUT && !Hardware::PC()) {
      if (ss.getEnabled() && (ss_missing - SELFDRIVE_STATE_TIMEOUT) < 10) {
        a = {tr("TAKE CONTROL IMMEDIATELY"), tr("System Unresponsive"),
             "selfdriveUnresponsive", cereal::SelfdriveState::AlertSize::FULL,
             cereal::SelfdriveState::AlertStatus::CRITICAL};
      } else {
        a = {tr("System Unresponsive"), tr("Reboot Device"),
             "selfdriveUnresponsivePermanent", cereal::SelfdriveState::AlertSize::MID,
             cereal::SelfdriveState::AlertStatus::NORMAL};
      }
    }
  }

  return a;
}

void OnroadAlerts::paintEvent(QPaintEvent *event) {
  Q_UNUSED(event);

  QPainter p(this);
  p.setRenderHint(QPainter::Antialiasing);
  p.setRenderHint(QPainter::TextAntialiasing);

  if (alert.type.contains("steerSaturated", Qt::CaseInsensitive)) {
    alertHeight = 0;
    return;
  }

  if (alert.size == cereal::SelfdriveState::AlertSize::NONE) {
    alertHeight = 0;
    return;
  }

  static std::map<cereal::SelfdriveState::AlertSize, const int> alert_heights = {
    {cereal::SelfdriveState::AlertSize::SMALL, 271},
    {cereal::SelfdriveState::AlertSize::MID, 420},
    {cereal::SelfdriveState::AlertSize::FULL, height()},
  };
  alertHeight = alert_heights[alert.size];
  int h = alertHeight;

  int margin = 40;
  int radius = 30;
  if (alert.size == cereal::SelfdriveState::AlertSize::FULL) {
    margin = 0;
    radius = 0;
  }
  alertHeight -= margin;
  QRect r(margin, height() - h + margin, width() - margin * 2, h - margin * 2);

  const bool is_resume_required_alert = alert.type.contains("resumeRequired", Qt::CaseInsensitive);
  const bool is_lead_departing_alert = alert.type.contains("leadDeparting", Qt::CaseInsensitive);
  const bool is_collision_alert = alert.type.contains("fcw", Qt::CaseInsensitive) || alert.type.contains("aeb", Qt::CaseInsensitive);
  const bool is_lane_departure_alert = alert.type.contains("ldw", Qt::CaseInsensitive);
  const bool icon_alert = is_collision_alert || is_lane_departure_alert;
  const QColor alert_color = alert_colors[alert.status];

  if (is_lead_departing_alert) {
    QLinearGradient full_grad(0, rect().top(), 0, rect().bottom());
    full_grad.setColorAt(0.0, QColor(0x20, 0x22, 0x25, 188));
    full_grad.setColorAt(0.45, QColor(0x16, 0x18, 0x1C, 206));
    full_grad.setColorAt(1.0, QColor(0x10, 0x12, 0x16, 224));
    p.setPen(Qt::NoPen);
    p.setBrush(full_grad);
    p.drawRect(rect());
  }

  if (is_resume_required_alert) {
    QLinearGradient full_grad(0, rect().top(), 0, rect().bottom());
    full_grad.setColorAt(0.0, QColor(alert_color.red(), alert_color.green(), alert_color.blue(), 178));
    full_grad.setColorAt(0.45, QColor(alert_color.red(), alert_color.green(), alert_color.blue(), 194));
    full_grad.setColorAt(1.0, QColor(std::max(alert_color.red() - 18, 0), std::max(alert_color.green() - 18, 0), std::max(alert_color.blue() - 18, 0), 212));
    p.setPen(Qt::NoPen);
    p.setBrush(full_grad);
    p.drawRect(rect());
  }

  if (icon_alert) {
    QLinearGradient overlay_grad(0, rect().top(), 0, rect().bottom());
    overlay_grad.setColorAt(0.0, QColor(0x2C, 0x33, 0x3A, 0x74));
    overlay_grad.setColorAt(0.25, QColor(0x25, 0x2B, 0x32, 0x98));
    overlay_grad.setColorAt(0.55, QColor(0x1E, 0x24, 0x2A, 0xBF));
    overlay_grad.setColorAt(1.0, QColor(0x16, 0x1A, 0x1F, 0xD8));
    p.setPen(Qt::NoPen);
    p.setBrush(overlay_grad);
    p.drawRect(rect());
  }

  p.setPen(Qt::NoPen);
  if (!is_resume_required_alert && !is_lead_departing_alert) {
    p.setBrush(QBrush(alert_color));
    p.drawRoundedRect(r, radius, radius);
  }

  QLinearGradient g(0, r.y(), 0, r.bottom());
  g.setColorAt(0, QColor::fromRgbF(0, 0, 0, 0.05));
  g.setColorAt(1, QColor::fromRgbF(0, 0, 0, 0.35));

  if (!is_resume_required_alert && !is_lead_departing_alert) {
    p.setCompositionMode(QPainter::CompositionMode_DestinationOver);
    p.setBrush(QBrush(g));
    p.drawRoundedRect(r, radius, radius);
    p.setCompositionMode(QPainter::CompositionMode_SourceOver);
  }

  if (icon_alert) {
    const int icon_size = is_lane_departure_alert ? 292 : 348;
    const QString icon_path = is_lane_departure_alert ? "../../files/icons/lanecrossing.png" : "../../files/icons/collision.png";
    const QPixmap icon_img = loadPixmap(icon_path, {icon_size, icon_size});
    const int blink_interval_ms = is_lane_departure_alert ? 320 : 120;
    const bool blink_visible = ((QDateTime::currentMSecsSinceEpoch() / blink_interval_ms) % 2) == 0;

    if (!icon_img.isNull() && blink_visible) {
      const int icon_y = is_lane_departure_alert ? 188 : 164;
      QRect icon_rect((width() - icon_size) / 2, icon_y, icon_size, icon_size);
      QRect glow_rect = icon_rect.adjusted(-58, -44, 58, 52);
      QRadialGradient glow(glow_rect.center(), glow_rect.width() * 0.56);
      const QColor glow_color = is_lane_departure_alert ? QColor(0xFF, 0xC3, 0x53) : QColor(0xFF, 0x5D, 0x57);
      glow.setColorAt(0.0, QColor(glow_color.red(), glow_color.green(), glow_color.blue(), 82));
      glow.setColorAt(0.45, QColor(glow_color.red(), glow_color.green(), glow_color.blue(), 34));
      glow.setColorAt(1.0, QColor(glow_color.red(), glow_color.green(), glow_color.blue(), 0));
      p.setPen(Qt::NoPen);
      p.setBrush(glow);
      p.drawEllipse(glow_rect);
      p.drawPixmap(icon_rect, icon_img);
    }
  }

  if (is_lead_departing_alert) {
    static const int lead_depart_icon_size = 320;
    static const QPixmap lead_depart_img = loadPixmap("../../files/icons/lead_depart.png", {lead_depart_icon_size, lead_depart_icon_size});
    if (!lead_depart_img.isNull()) {
      QRect icon_rect((width() - lead_depart_icon_size) / 2, (height() - lead_depart_icon_size) / 2 - 70, lead_depart_icon_size, lead_depart_icon_size);
      const QPixmap scaled_img = lead_depart_img.scaled(icon_rect.size(), Qt::KeepAspectRatio, Qt::SmoothTransformation);
      QRect draw_rect(icon_rect.left() + (icon_rect.width() - scaled_img.width()) / 2,
                      icon_rect.top() + (icon_rect.height() - scaled_img.height()) / 2,
                      scaled_img.width(), scaled_img.height());
      p.drawPixmap(draw_rect, scaled_img);

      p.setPen(QColor(0xff, 0xff, 0xff));
      p.setFont(InterFont(82, QFont::Bold));
      p.drawText(QRect(0, icon_rect.bottom() + 52, width(), 110), Qt::AlignHCenter | Qt::AlignTop, alert.text2);
    }
  }

  const QPoint c = r.center();
  p.setPen(QColor(0xff, 0xff, 0xff));

  if (alert.size == cereal::SelfdriveState::AlertSize::SMALL) {
    p.setFont(InterFont(74, QFont::DemiBold));
    p.drawText(r, Qt::AlignCenter, alert.text1);
  } else if (alert.size == cereal::SelfdriveState::AlertSize::MID) {
    if (is_resume_required_alert) {
      static const int autohold_icon_size = 184;
      static const QPixmap autohold_img = loadPixmap("../../files/icons/autohold.png", {autohold_icon_size, autohold_icon_size});
      const int elapsed_seconds = resumeRequiredTimer.isValid() ? resumeRequiredTimer.elapsed() / 1000 : 0;
      const QString elapsed_text = QString("%1:%2").arg(elapsed_seconds / 60).arg(elapsed_seconds % 60, 2, 10, QChar('0'));
      const QPoint screen_center = rect().center();

      const int group_gap = 28;
      const int timer_width = 260;
      const int group_width = autohold_icon_size + group_gap + timer_width;
      const int group_left = (width() - group_width) / 2;
      const int group_top = screen_center.y() - 150;
      QRect icon_rect(group_left, group_top, autohold_icon_size, autohold_icon_size);
      QRect timer_rect(icon_rect.right() + group_gap, group_top + 10, timer_width, autohold_icon_size - 20);
      QRect description_rect(0, group_top + autohold_icon_size + 26, width(), 90);

      if (!autohold_img.isNull()) {
        const QPixmap scaled_img = autohold_img.scaled(icon_rect.size(), Qt::KeepAspectRatio, Qt::SmoothTransformation);
        QRect draw_rect(icon_rect.left(), icon_rect.top() + (icon_rect.height() - scaled_img.height()) / 2, scaled_img.width(), scaled_img.height());
        p.drawPixmap(draw_rect, scaled_img);
      }

      p.setFont(InterFont(112, QFont::Bold));
      p.drawText(timer_rect, Qt::AlignCenter, elapsed_text);
      p.setFont(InterFont(66));
      p.drawText(description_rect, Qt::AlignHCenter | Qt::AlignTop, alert.text2);
    } else {
      p.setFont(InterFont(88, QFont::Bold));
      p.drawText(QRect(0, c.y() - 125, width(), 150), Qt::AlignHCenter | Qt::AlignTop, alert.text1);
      p.setFont(InterFont(66));
      p.drawText(QRect(0, c.y() + 21, width(), 90), Qt::AlignHCenter, alert.text2);
    }
  } else if (alert.size == cereal::SelfdriveState::AlertSize::FULL && !is_lead_departing_alert) {
    const bool l = alert.text1.length() > 15;
    p.setFont(InterFont(l ? 132 : 177, QFont::Bold));
    p.drawText(QRect(0, r.y() + (l ? 240 : 270), width(), 600), Qt::AlignHCenter | Qt::TextWordWrap, alert.text1);
    p.setFont(InterFont(88));
    p.drawText(QRect(0, r.height() - (l ? 361 : 420), width(), 300), Qt::AlignHCenter | Qt::TextWordWrap, alert.text2);
  }
}
