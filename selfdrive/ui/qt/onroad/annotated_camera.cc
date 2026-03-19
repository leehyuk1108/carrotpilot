#include "selfdrive/ui/qt/onroad/annotated_camera.h"

#include <QDateTime>
#include <QPainter>
#include <algorithm>
#include <cmath>
#include <unistd.h>

#include "common/swaglog.h"
#include "selfdrive/ui/qt/util.h"

AnnotatedCameraWidget::AnnotatedCameraWidget(VisionStreamType type, QWidget *parent)
    : fps_filter(UI_FREQ, 3, 1. / UI_FREQ), CameraWidget("camerad", type, parent) {
  pm = std::make_unique<PubMaster>(std::vector<const char*>{"uiDebug"});

  main_layout = new QVBoxLayout(this);
  main_layout->setContentsMargins(0, 0, 0, 0);
  main_layout->setSpacing(0);

  experimental_btn = new ExperimentalButton(this);
  main_layout->addWidget(experimental_btn, 0, Qt::AlignTop | Qt::AlignRight);

  blindspot_left_img = loadPixmap("../../files/icons/blindspot_left.png", {196, 224});
  blindspot_right_img = loadPixmap("../../files/icons/blindspot_right.png", {196, 224});
}

void AnnotatedCameraWidget::updateState(const UIState &s) {
  const SubMaster &sm = *(s.sm);
  const bool always_lateral_preview = qEnvironmentVariableIntValue("ALWAYS_LATERAL_PREVIEW") == 1;
  bool always_on_lateral_active = always_lateral_preview;
  if (sm.rcv_frame("selfdriveState") >= s.scene.started_frame &&
      sm.rcv_frame("carControl") >= s.scene.started_frame &&
      sm.rcv_frame("carState") >= s.scene.started_frame) {
    const auto selfdrive_state = sm["selfdriveState"].getSelfdriveState();
    const auto car_control = sm["carControl"].getCarControl();
    const auto car_state = sm["carState"].getCarState();
    always_on_lateral_active = always_on_lateral_active ||
                               (!selfdrive_state.getEnabled() && !car_control.getLongActive() &&
                                (car_control.getLatActive() || car_state.getLatEnabled()));
  }

  const bool camera_dimmed = s.scene.started && s.status == STATUS_DISENGAGED && !always_on_lateral_active;
  setFrameFilter(camera_dimmed ? 1.0f : 0.0f, camera_dimmed ? 0.62f : 1.0f);

  experimental_btn->updateState(s);
  experimental_btn->setVisible(false);
  dmon.updateState(s);

  if (sm.rcv_frame("carState") >= s.scene.started_frame) {
    const auto car_state = sm["carState"].getCarState();
    blindspot_left = car_state.getLeftBlindspot();
    blindspot_right = car_state.getRightBlindspot();
    blinker_left = car_state.getLeftBlinker();
    blinker_right = car_state.getRightBlinker();
  } else {
    blindspot_left = false;
    blindspot_right = false;
    blinker_left = false;
    blinker_right = false;
  }
}

void AnnotatedCameraWidget::initializeGL() {
  CameraWidget::initializeGL();
  qInfo() << "OpenGL version:" << QString((const char*)glGetString(GL_VERSION));
  qInfo() << "OpenGL vendor:" << QString((const char*)glGetString(GL_VENDOR));
  qInfo() << "OpenGL renderer:" << QString((const char*)glGetString(GL_RENDERER));
  qInfo() << "OpenGL language version:" << QString((const char*)glGetString(GL_SHADING_LANGUAGE_VERSION));

  prev_draw_t = millis_since_boot();
  setBackgroundColor(bg_colors[STATUS_DISENGAGED]);
}

mat4 AnnotatedCameraWidget::calcFrameMatrix() {
  auto *s = uiState();
  const bool wide_cam = active_stream_type == VISION_STREAM_WIDE_ROAD;
  const auto &intrinsic_matrix = wide_cam ? ECAM_INTRINSIC_MATRIX : FCAM_INTRINSIC_MATRIX;
  const auto &calibration = wide_cam ? s->scene.view_from_wide_calib : s->scene.view_from_calib;
  const auto calib_transform = intrinsic_matrix * calibration;

  float zoom = wide_cam ? 2.0f : 1.1f;
  Eigen::Vector3f inf(1000.f, 0.f, 0.f);
  auto kep = calib_transform * inf;

  int w = width();
  int h = height();
  float center_x = intrinsic_matrix(0, 2);
  float center_y = intrinsic_matrix(1, 2);

  float max_x_offset = center_x * zoom - w / 2 - 5;
  float max_y_offset = center_y * zoom - h / 2 - 5;
  float x_offset = std::clamp<float>((kep.x() / kep.z() - center_x) * zoom, -max_x_offset, max_x_offset);
  float y_offset = std::clamp<float>((kep.y() / kep.z() - center_y) * zoom, -max_y_offset, max_y_offset);

  Eigen::Matrix3f video_transform = (Eigen::Matrix3f() <<
    zoom, 0.0f, (w / 2 - x_offset) - (center_x * zoom),
    0.0f, zoom, (h / 2 - y_offset) - (center_y * zoom),
    0.0f, 0.0f, 1.0f).finished();

  model.setTransform(video_transform * calib_transform);

  constexpr float kHorizontalOverscan = 1.035f;
  constexpr float kLeftBias = -0.016f;

  float zx = zoom * 2 * center_x / w * kHorizontalOverscan;
  float zy = zoom * 2 * center_y / h;
  return mat4{{
    zx, 0.0, 0.0, (-x_offset / w * 2) + kLeftBias,
    0.0, zy, 0.0, y_offset / h * 2,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
  }};
}

void AnnotatedCameraWidget::paintGL() {
}

void AnnotatedCameraWidget::paintBlindspotIcons(QPainter &painter) {
  const QString preview = qEnvironmentVariable("BLINDSPOT_PREVIEW").trimmed().toLower();
  const bool preview_left = preview == "1" || preview.contains("left") || preview.contains("both");
  const bool preview_right = preview == "1" || preview.contains("right") || preview.contains("both");
  const bool preview_blink_left = preview.contains("left-blink") || preview.contains("both-blink");
  const bool preview_blink_right = preview.contains("right-blink") || preview.contains("both-blink");

  const bool show_left = blindspot_left || preview_left;
  const bool show_right = blindspot_right || preview_right;
  if (!show_left && !show_right) return;

  const bool blink_left = (blindspot_left && blinker_left) || preview_blink_left;
  const bool blink_right = (blindspot_right && blinker_right) || preview_blink_right;
  const bool blink_visible = ((QDateTime::currentMSecsSinceEpoch() / 120) % 2) == 0;

  const int left_margin = 44;
  const int right_margin = 40;
  const QRect viewport = painter.viewport();
  const int y = viewport.center().y() - 180;

  painter.save();
  painter.setRenderHint(QPainter::Antialiasing);
  painter.setRenderHint(QPainter::SmoothPixmapTransform);

  auto draw_glow = [&](const QRect &icon_rect, const QColor &glow_color) {
    QRect glow_rect = icon_rect.adjusted(-34, -28, 34, 28);
    QRadialGradient glow(glow_rect.center(), glow_rect.width() * 0.56);
    glow.setColorAt(0.0, QColor(glow_color.red(), glow_color.green(), glow_color.blue(), 82));
    glow.setColorAt(0.45, QColor(glow_color.red(), glow_color.green(), glow_color.blue(), 34));
    glow.setColorAt(1.0, QColor(glow_color.red(), glow_color.green(), glow_color.blue(), 0));
    painter.setPen(Qt::NoPen);
    painter.setBrush(glow);
    painter.drawEllipse(glow_rect);
  };

  if (show_left && (!blink_left || blink_visible) && !blindspot_left_img.isNull()) {
    const int x = viewport.left() + left_margin;
    draw_glow(QRect(x, y, blindspot_left_img.width(), blindspot_left_img.height()), QColor(0xFF, 0x67, 0x4A));
    painter.drawPixmap(x, y, blindspot_left_img);
  }

  if (show_right && (!blink_right || blink_visible) && !blindspot_right_img.isNull()) {
    const int x = viewport.right() - right_margin - blindspot_right_img.width() + 1;
    draw_glow(QRect(x, y, blindspot_right_img.width(), blindspot_right_img.height()), QColor(0xFF, 0x67, 0x4A));
    painter.drawPixmap(x, y, blindspot_right_img);
  }

  painter.restore();
}

void AnnotatedCameraWidget::paintEvent(QPaintEvent *event) {
  Q_UNUSED(event);

  UIState *s = uiState();
  SubMaster &sm = *(s->sm);
  const double start_draw_t = millis_since_boot();

  QPainter painter(this);

  {
    std::lock_guard lk(frame_lock);

    if (frames.empty()) {
      if (skip_frame_count > 0) {
        skip_frame_count--;
        qDebug() << "skipping frame, not ready";
        return;
      }
    } else {
      skip_frame_count = 5;
    }

    bool has_wide_cam = available_streams.count(VISION_STREAM_WIDE_ROAD);
    if (has_wide_cam) {
      float v_ego = sm["carState"].getCarState().getVEgo();
      if ((v_ego < 10) || available_streams.size() == 1) {
        wide_cam_requested = true;
      } else if (v_ego > 15) {
        wide_cam_requested = false;
      }
      wide_cam_requested = wide_cam_requested && sm["selfdriveState"].getSelfdriveState().getExperimentalMode();
    }

    painter.beginNativePainting();
    CameraWidget::setStreamType(wide_cam_requested ? VISION_STREAM_WIDE_ROAD : VISION_STREAM_ROAD);
    CameraWidget::setFrameId(sm["modelV2"].getModelV2().getFrameId());
    CameraWidget::paintGL();
    painter.endNativePainting();
  }

  painter.setRenderHint(QPainter::Antialiasing);
  painter.setPen(Qt::NoPen);

  QLinearGradient chrome_gradient(0, 0, 0, height() * 0.55);
  chrome_gradient.setColorAt(0.0, QColor(0x0E, 0x1C, 0x29, 0x34));
  chrome_gradient.setColorAt(0.30, QColor(0x12, 0x2A, 0x3B, 0x18));
  chrome_gradient.setColorAt(1.0, QColor(0x12, 0x2A, 0x3B, 0x00));
  painter.fillRect(rect(), chrome_gradient);

  QLinearGradient footer_gradient(0, height() * 0.56, 0, height());
  footer_gradient.setColorAt(0.0, QColor(0x05, 0x09, 0x0F, 0x00));
  footer_gradient.setColorAt(0.35, QColor(0x05, 0x09, 0x0F, 0x34));
  footer_gradient.setColorAt(0.72, QColor(0x05, 0x09, 0x0F, 0x92));
  footer_gradient.setColorAt(1.0, QColor(0x05, 0x09, 0x0F, 0xD8));
  painter.fillRect(rect(), footer_gradient);

  QLinearGradient hud_gradient(0, height() * 0.66, 0, height());
  hud_gradient.setColorAt(0.0, QColor(0x03, 0x05, 0x09, 0x00));
  hud_gradient.setColorAt(0.22, QColor(0x03, 0x05, 0x09, 0x30));
  hud_gradient.setColorAt(0.58, QColor(0x03, 0x05, 0x09, 0x88));
  hud_gradient.setColorAt(1.0, QColor(0x03, 0x05, 0x09, 0xE8));
  painter.fillRect(rect(), hud_gradient);

  model.draw(painter, rect());
  dmon.draw(painter, rect());
  hud.updateState(*s);
  hud.draw(painter, rect());
  paintBlindspotIcons(painter);

  const double cur_draw_t = millis_since_boot();
  const double dt = cur_draw_t - prev_draw_t;
  const double fps = fps_filter.update(1. / dt * 1000);
  if (fps < 15) {
    LOGW("slow frame rate: %.2f fps", fps);
  }
  prev_draw_t = cur_draw_t;

  MessageBuilder msg;
  auto m = msg.initEvent().initUiDebug();
  m.setDrawTimeMillis(cur_draw_t - start_draw_t);
  pm->send("uiDebug", msg);
}

void AnnotatedCameraWidget::showEvent(QShowEvent *event) {
  CameraWidget::showEvent(event);

  ui_update_params(uiState());
  prev_draw_t = millis_since_boot();
}
