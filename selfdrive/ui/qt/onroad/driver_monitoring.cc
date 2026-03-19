#include "selfdrive/ui/qt/onroad/driver_monitoring.h"

#include <algorithm>
#include <cmath>

#include "selfdrive/ui/qt/util.h"

namespace {
constexpr int kDmSize = 156;
constexpr int kConeSize = 132;
constexpr float kConeMaxRotationDeg = 38.0f;
constexpr float kRadToDeg = 57.2957795f;

QPixmap tintedPixmap(const QPixmap &img, const QColor &tint) {
  QPixmap tinted(img.size());
  tinted.fill(Qt::transparent);

  QPainter painter(&tinted);
  painter.drawPixmap(0, 0, img);
  painter.setCompositionMode(QPainter::CompositionMode_SourceIn);
  painter.fillRect(tinted.rect(), tint);
  painter.end();

  return tinted;
}
}  // namespace

DriverMonitorRenderer::DriverMonitorRenderer() {
  dm_background_img = loadPixmap("../assets/icons_mici/onroad/driver_monitoring/dm_background.png", QSize(kDmSize, kDmSize));
  dm_cone_img = loadPixmap("../assets/icons_mici/onroad/driver_monitoring/dm_cone.png", QSize(kConeSize, kConeSize));
  dm_person_img = loadPixmap("../assets/icons_mici/onroad/driver_monitoring/dm_person.png", QSize(kDmSize - 16, kDmSize - 16));
  dm_cone_disengaged_img = tintedPixmap(dm_cone_img, QColor(0x8A, 0x8F, 0x96, 0xFF));
  dm_cone_engageable_img = tintedPixmap(dm_cone_img, QColor(0xF2, 0xF5, 0xF8, 0xFF));
  dm_cone_enabled_img = tintedPixmap(dm_cone_img, QColor(0x49, 0xD2, 0x83, 0xFF));
}

void DriverMonitorRenderer::updateState(const UIState &s) {
  auto &sm = *(s.sm);
  Params params;
  const bool force_onroad_preview = qEnvironmentVariableIntValue("FORCE_ONROAD_PREVIEW") == 1;
  const bool always_lateral_preview = qEnvironmentVariableIntValue("ALWAYS_LATERAL_PREVIEW") == 1;
  const bool force_preview = (params.getBool("ForceOnroad") || force_onroad_preview) &&
                             sm.rcv_frame("driverStateV2") <= s.scene.started_frame;
  const auto selfdrive_state = sm["selfdriveState"].getSelfdriveState();
  bool always_on_lateral_active = always_lateral_preview;
  if (sm.rcv_frame("carControl") >= s.scene.started_frame &&
      sm.rcv_frame("carState") >= s.scene.started_frame) {
    const auto car_control = sm["carControl"].getCarControl();
    const auto car_state = sm["carState"].getCarState();
    always_on_lateral_active = always_on_lateral_active ||
                               (!selfdrive_state.getEnabled() && !car_control.getLongActive() &&
                                (car_control.getLatActive() || car_state.getLatEnabled()));
  }
  is_visible = selfdrive_state.getAlertSize() == cereal::SelfdriveState::AlertSize::NONE &&
               (sm.rcv_frame("driverStateV2") > s.scene.started_frame || force_preview);
  if (!is_visible) return;

  is_enabled = force_preview || selfdrive_state.getEnabled() || always_on_lateral_active;
  is_engageable = force_preview || selfdrive_state.getEngageable() || is_enabled;

  if (force_preview) {
    is_active = true;
    is_rhd = false;
    dm_fade_state = 0.0f;
    cone_rotation_deg = 0.0f;
    return;
  }

  const auto dm_state = sm["driverMonitoringState"].getDriverMonitoringState();
  is_active = dm_state.getIsActiveMode();
  is_rhd = dm_state.getIsRHD();
  dm_fade_state = std::clamp(dm_fade_state + 0.2f * (0.5f - is_active), 0.0f, 1.0f);

  const auto &driverstate = sm["driverStateV2"].getDriverStateV2();
  const auto driver_orient = is_rhd ? driverstate.getRightDriverData().getFaceOrientation()
                                    : driverstate.getLeftDriverData().getFaceOrientation();

  float yaw = 0.0f;
  if (driver_orient.size() > 1 && std::isfinite(driver_orient[1])) {
    yaw = driver_orient[1];
  }
  cone_rotation_deg = std::clamp(-yaw * kRadToDeg, -kConeMaxRotationDeg, kConeMaxRotationDeg);
}

void DriverMonitorRenderer::draw(QPainter &painter, const QRect &surface_rect) {
  if (!is_visible) return;

  painter.save();

  const int left_margin = 104;
  const int bottom_margin = 96;
  const float x = left_margin + kDmSize / 2;
  const float y = surface_rect.height() - bottom_margin - kDmSize / 2;
  const float opacity = is_active ? 0.95f : 0.50f;

  const QPixmap *cone_img = &dm_cone_disengaged_img;
  if (is_enabled) {
    cone_img = &dm_cone_enabled_img;
  } else if (is_engageable) {
    cone_img = &dm_cone_engageable_img;
  }

  painter.setRenderHint(QPainter::Antialiasing);
  painter.setOpacity(opacity);

  if (!dm_background_img.isNull()) {
    painter.drawPixmap(x - dm_background_img.width() / 2,
                       y - dm_background_img.height() / 2,
                       dm_background_img);
  }

  painter.save();
  painter.translate(x, y);
  painter.rotate(cone_rotation_deg);
  if (cone_img != nullptr && !cone_img->isNull()) {
    painter.drawPixmap(-cone_img->width() / 2, -cone_img->height() / 2 - 18, *cone_img);
  }
  painter.restore();

  if (!dm_person_img.isNull()) {
    painter.drawPixmap(x - dm_person_img.width() / 2,
                       y - dm_person_img.height() / 2 + 4,
                       dm_person_img);
  }

  painter.restore();
}
