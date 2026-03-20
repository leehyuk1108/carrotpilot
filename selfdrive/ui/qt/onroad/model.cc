#include "selfdrive/ui/qt/onroad/model.h"

#include "selfdrive/ui/qt/util.h"

constexpr int CLIP_MARGIN = 500;
constexpr float MIN_DRAW_DISTANCE = 10.0;
constexpr float MAX_DRAW_DISTANCE = 100.0;

namespace {
constexpr float LEAD_BOX_ALPHA = 0.85f;
constexpr float LEAD_BOX_HALF_WIDTH_M = 1.2f;
constexpr float LEAD_BOX_HEIGHT_FACTOR = 0.8f;
constexpr float LEAD_BOX_MIN_WIDTH = 120.0f;
constexpr float LEAD_BOX_MAX_WIDTH = 800.0f;
constexpr float LEAD_BOX_RADIUS = 15.0f;
constexpr float LEAD_BOX_STROKE = 3.0f;
constexpr float LEAD_BOX_HPAD = 10.0f;
constexpr float LEAD_BADGE_OFFSET_X = 80.0f;
constexpr float LEAD_BADGE_OFFSET_Y = 60.0f;
constexpr float LEAD_BADGE_HEIGHT = 42.0f;

QColor carrotRed() {
  return QColor(255, 0, 0, 255);
}

QColor carrotOrange() {
  return QColor(255, 175, 3, 255);
}

QColor carrotBlue() {
  return QColor(0, 0, 255, 255);
}

QColor carrotOchre() {
  return QColor(218, 111, 37, 255);
}

QColor carrotBlackAlpha(int alpha) {
  return QColor(0, 0, 0, alpha);
}

QColor carrotRedAlpha(int alpha) {
  return QColor(255, 0, 0, alpha);
}

QColor lateralOnlyMint(int alpha = 255) {
  return QColor(0x67, 0xF5, 0xD1, alpha);
}

QColor laneChangeMint(int alpha = 255) {
  return QColor(0x53, 0xEE, 0xB6, alpha);
}

void drawOutlinedText(QPainter &painter, const QRectF &rect, const QString &text, const QColor &text_color) {
  const QRectF shadow_rect = rect.translated(0.0f, 1.5f);
  painter.setPen(QColor(0, 0, 0, 180));
  painter.drawText(shadow_rect, Qt::AlignCenter, text);
  painter.setPen(text_color);
  painter.drawText(rect, Qt::AlignCenter, text);
}

QColor grayToneColor(const QColor &color) {
  const float luminance = (0.299f * color.redF()) + (0.587f * color.greenF()) + (0.114f * color.blueF());
  const float toned = std::clamp(luminance * 0.82f, 0.0f, 1.0f);
  return QColor::fromRgbF(toned, toned, toned, color.alphaF());
}

void grayToneGradient(QLinearGradient &gradient) {
  const auto stops = gradient.stops();
  gradient.setStops({});
  for (const auto &[position, color] : stops) {
    gradient.setColorAt(position, grayToneColor(color));
  }
}

}  // namespace

int get_path_length_idx(const cereal::XYZTData::Reader &line, const float path_height) {
  const auto &line_x = line.getX();
  int max_idx = 0;
  for (int i = 1; i < line_x.size() && line_x[i] <= path_height; ++i) {
    max_idx = i;
  }
  return max_idx;
}

void ModelRenderer::draw(QPainter &painter, const QRect &surface_rect) {
  auto *s = uiState();
  auto &sm = *(s->sm);
  // Check if data is up-to-date
  if (sm.rcv_frame("liveCalibration") < s->scene.started_frame ||
      sm.rcv_frame("modelV2") < s->scene.started_frame) {
    return;
  }

  clip_region = surface_rect.adjusted(-CLIP_MARGIN, -CLIP_MARGIN, CLIP_MARGIN, CLIP_MARGIN);
  experimental_mode = sm["selfdriveState"].getSelfdriveState().getExperimentalMode();
  experimental_mode |= s->scene.carrot_experimental_mode;
  longitudinal_control = sm["carParams"].getCarParams().getOpenpilotLongitudinalControl();
  path_offset_z = sm["liveCalibration"].getLiveCalibration().getHeight()[0];
  const auto &selfdrive_state = sm["selfdriveState"].getSelfdriveState();
  const auto &car_control = sm["carControl"].getCarControl();
  const auto &car_state = sm["carState"].getCarState();
  lane_mode_active = sm.alive("controlsState") && sm["controlsState"].getControlsState().getActiveLaneLine();
  lateral_only_active = qEnvironmentVariableIntValue("ALWAYS_LATERAL_PREVIEW") == 1 ||
                        (!selfdrive_state.getEnabled() && !car_control.getLongActive() &&
                         (car_control.getLatActive() || car_state.getLatEnabled()));

  painter.save();

  const auto &model = sm["modelV2"].getModelV2();
  const auto &meta = model.getMeta();
  lane_change_state = meta.getLaneChangeState();
  lane_change_direction = meta.getLaneChangeDirection();
  const QString lane_change_state_preview = qEnvironmentVariable("LANE_CHANGE_STATE_PREVIEW").trimmed().toLower();
  const QString lane_change_direction_preview = qEnvironmentVariable("LANE_CHANGE_DIRECTION_PREVIEW").trimmed().toLower();
  if (lane_change_state_preview == "pre") {
    lane_change_state = cereal::LaneChangeState::PRE_LANE_CHANGE;
  } else if (lane_change_state_preview == "starting") {
    lane_change_state = cereal::LaneChangeState::LANE_CHANGE_STARTING;
  } else if (lane_change_state_preview == "finishing") {
    lane_change_state = cereal::LaneChangeState::LANE_CHANGE_FINISHING;
  } else if (lane_change_state_preview == "off") {
    lane_change_state = cereal::LaneChangeState::OFF;
  }
  if (lane_change_direction_preview == "left") {
    lane_change_direction = cereal::LaneChangeDirection::LEFT;
  } else if (lane_change_direction_preview == "right") {
    lane_change_direction = cereal::LaneChangeDirection::RIGHT;
  } else if (lane_change_direction_preview == "none") {
    lane_change_direction = cereal::LaneChangeDirection::NONE;
  }
  const auto &radar_state = sm["radarState"].getRadarState();
  const auto &lead_one = radar_state.getLeadOne();

  update_model(model, lead_one);
  drawLaneLines(painter);
  drawPath(painter, model, surface_rect.height());

  static Params params;
  if (params.getInt("ShowPathEnd") > 0 && sm.alive("radarState") && sm.alive("longitudinalPlan") &&
      !(uiState()->status == STATUS_DISENGAGED && !lateral_only_active)) {
    update_leads(radar_state, model, sm["longitudinalPlan"].getLongitudinalPlan(), surface_rect);
    if (lead_boxes[1].visible) {
      drawLeadBox(painter, lead_boxes[1], true);
    }
    if (lead_boxes[0].visible) {
      drawLeadBox(painter, lead_boxes[0], false);
      drawLeadDistanceBadges(painter, lead_boxes[0]);
    }
  } else {
    lead_boxes[0].visible = false;
    lead_boxes[1].visible = false;
  }

  painter.restore();
}

void ModelRenderer::update_leads(const cereal::RadarState::Reader &radar_state, const cereal::ModelDataV2::Reader &model,
                                 const cereal::LongitudinalPlan::Reader &longitudinal_plan, const QRect &surface_rect) {
  const auto &line = model.getPosition();
  const auto &lead_one = radar_state.getLeadOne();
  const auto &lead_two = radar_state.getLeadTwo();
  const auto &leads_v3 = model.getLeadsV3();

  vision_dist = 0.0f;
  if (leads_v3.size() > 0) {
    const auto vision_lead = leads_v3[0];
    if (vision_lead.getProb() > 0.5f && vision_lead.getX().size() > 0) {
      vision_dist = vision_lead.getX()[0] - 1.52f;
    }
  }

  auto update_box = [&](const cereal::RadarState::LeadData::Reader &lead_data, LeadBoxState &box, bool selected) {
    if (!lead_data.getStatus()) {
      box.visible = false;
      return;
    }

    const float z = line.getZ()[get_path_length_idx(line, lead_data.getDRel())];
    const float y = -lead_data.getYRel();
    QPointF left, right;
    mapToScreen(lead_data.getDRel(), y - LEAD_BOX_HALF_WIDTH_M, z + path_offset_z, &left);
    mapToScreen(lead_data.getDRel(), y + LEAD_BOX_HALF_WIDTH_M, z + path_offset_z, &right);

    float raw_width = std::clamp<float>(right.x() - left.x(), LEAD_BOX_MIN_WIDTH, LEAD_BOX_MAX_WIDTH);
    float raw_x = std::clamp<float>((left.x() + right.x()) * 0.5f, 350.0f, surface_rect.width() - 350.0f);
    float raw_y = std::clamp<float>((left.y() + right.y()) * 0.5f, 200.0f, surface_rect.height() - 80.0f);

    if (!std::isfinite(raw_x) || !std::isfinite(raw_y) || !std::isfinite(raw_width)) {
      box.visible = false;
      return;
    }

    if (!box.initialized) {
      box.x = raw_x;
      box.y = raw_y;
      box.width = raw_width;
      box.initialized = true;
    } else {
      box.x = box.x * LEAD_BOX_ALPHA + raw_x * (1.0f - LEAD_BOX_ALPHA);
      box.y = box.y * LEAD_BOX_ALPHA + raw_y * (1.0f - LEAD_BOX_ALPHA);
      box.width = box.width * LEAD_BOX_ALPHA + raw_width * (1.0f - LEAD_BOX_ALPHA);
    }

    box.visible = true;
    box.selected = selected;
    box.radar_detected = lead_data.getRadarTrackId() >= 0;
    box.lead_scc = lead_data.getRadarTrackId() < 1;
    box.radar_distance = lead_data.getRadar() ? lead_data.getDRel() : 0.0f;
  };

  update_box(lead_one, lead_boxes[0], false);

  if (lead_two.getRadar() && (!lead_one.getStatus() || lead_two.getDRel() > lead_one.getDRel() + 3.0f)) {
    const bool selected = longitudinal_plan.getLongitudinalPlanSource() == cereal::LongitudinalPlan::LongitudinalPlanSource::LEAD1;
    update_box(lead_two, lead_boxes[1], selected);
  } else {
    lead_boxes[1].visible = false;
  }
}

void ModelRenderer::update_model(const cereal::ModelDataV2::Reader &model, const cereal::RadarState::LeadData::Reader &lead) {
  const auto &model_position = model.getPosition();
  float max_distance = std::clamp(*(model_position.getX().end() - 1), MIN_DRAW_DISTANCE, MAX_DRAW_DISTANCE);

  // update lane lines
  const auto &lane_lines = model.getLaneLines();
  const auto &line_probs = model.getLaneLineProbs();
  int max_idx = get_path_length_idx(lane_lines[0], max_distance);
  for (int i = 0; i < std::size(lane_line_vertices); i++) {
    lane_line_probs[i] = line_probs[i];
    float lane_width = 0.025f * lane_line_probs[i];
    if (lane_mode_active && (i == 1 || i == 2)) {
      lane_width = std::max(lane_width * 1.7f, 0.034f);
    } else {
      lane_width = std::max(lane_width * 1.25f, 0.028f);
    }
    mapLineToPolygon(lane_lines[i], lane_width, 0, &lane_line_vertices[i], max_idx);
  }

  // update road edges
  const auto &road_edges = model.getRoadEdges();
  const auto &edge_stds = model.getRoadEdgeStds();
  for (int i = 0; i < std::size(road_edge_vertices); i++) {
    road_edge_stds[i] = edge_stds[i];
    mapLineToPolygon(road_edges[i], 0.025, 0, &road_edge_vertices[i], max_idx);
  }

  // update path
  if (lead.getStatus()) {
    const float lead_d = lead.getDRel() * 2.;
    max_distance = std::clamp((float)(lead_d - fmin(lead_d * 0.35, 10.)), 0.0f, max_distance);
  }
  max_idx = get_path_length_idx(model_position, max_distance);
  mapLineToPolygon(model_position, 0.9, path_offset_z, &track_vertices, max_idx, false);
}

void ModelRenderer::drawLaneLines(QPainter &painter) {
  if (uiState()->status == STATUS_DISENGAGED && !lateral_only_active) {
    return;
  }

  // lanelines
  for (int i = 0; i < std::size(lane_line_vertices); ++i) {
    const bool ego_lane_boundary = i == 1 || i == 2;
    const bool lane_change_active =
      (lane_change_state == cereal::LaneChangeState::LANE_CHANGE_STARTING ||
       lane_change_state == cereal::LaneChangeState::LANE_CHANGE_FINISHING) &&
      lane_change_direction != cereal::LaneChangeDirection::NONE;
    const bool between_current_and_target_lane =
      lane_change_active &&
      ((lane_change_direction == cereal::LaneChangeDirection::LEFT && i == 1) ||
       (lane_change_direction == cereal::LaneChangeDirection::RIGHT && i == 2));
    const bool current_lane_boundary =
      lane_change_active &&
      ((lane_change_direction == cereal::LaneChangeDirection::LEFT && i == 2) ||
       (lane_change_direction == cereal::LaneChangeDirection::RIGHT && i == 1));
    const bool target_lane_boundary =
      lane_change_active &&
      ((lane_change_direction == cereal::LaneChangeDirection::LEFT && i == 0) ||
       (lane_change_direction == cereal::LaneChangeDirection::RIGHT && i == 3));
    const bool lane_change_highlight = current_lane_boundary || target_lane_boundary;

    if (between_current_and_target_lane) {
      continue;
    }

    if (lane_change_highlight) {
      const float alpha = std::clamp<float>(0.22f + lane_line_probs[i] * 0.62f, 0.22f, 0.88f);
      const QColor lane_glow = laneChangeMint(128);
      painter.setPen(QPen(lane_glow, 9.0f, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
      painter.setBrush(QColor::fromRgbF(lane_glow.redF(), lane_glow.greenF(), lane_glow.blueF(), alpha));
    } else if (lane_mode_active && ego_lane_boundary) {
      const float alpha = std::clamp<float>(0.15f + lane_line_probs[i] * 0.65f, 0.15f, 0.8f);
      const QColor lane_glow = QColor::fromRgbF(0.24f, 0.95f, 0.46f, std::clamp<float>(alpha * 0.5f, 0.16f, 0.34f));
      painter.setPen(QPen(lane_glow, 10.0f, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
      painter.setBrush(QColor::fromRgbF(0.14f, 0.90f, 0.38f, alpha));
    } else {
      painter.setPen(Qt::NoPen);
      painter.setBrush(QColor::fromRgbF(1.0, 1.0, 1.0, std::clamp<float>(lane_line_probs[i], 0.0, 0.7)));
    }
    painter.drawPolygon(lane_line_vertices[i]);
    painter.setPen(Qt::NoPen);
  }

  // road edges
  for (int i = 0; i < std::size(road_edge_vertices); ++i) {
    painter.setBrush(QColor::fromRgbF(1.0, 0, 0, std::clamp<float>(1.0 - road_edge_stds[i], 0.0, 1.0)));
    painter.drawPolygon(road_edge_vertices[i]);
  }
}

void ModelRenderer::drawPath(QPainter &painter, const cereal::ModelDataV2::Reader &model, int height) {
  QLinearGradient bg(0, height, 0, 0);
  if (lateral_only_active) {
    bg.setColorAt(0.0f, lateralOnlyMint(110));
    bg.setColorAt(0.55f, lateralOnlyMint(84));
    bg.setColorAt(1.0f, lateralOnlyMint(8));
  } else if (lane_mode_active) {
    bg.setColorAt(0.0f, QColor(0xF6, 0xF8, 0xFB, 102));
    bg.setColorAt(0.55f, QColor(0xF6, 0xF8, 0xFB, 58));
    bg.setColorAt(1.0f, QColor(0xF6, 0xF8, 0xFB, 0));
  } else if (experimental_mode) {
    // The first half of track_vertices are the points for the right side of the path
    const auto &acceleration = model.getAcceleration().getX();
    const int max_len = std::min<int>(track_vertices.length() / 2, acceleration.size());

    for (int i = 0; i < max_len; ++i) {
      // Some points are out of frame
      int track_idx = max_len - i - 1;  // flip idx to start from bottom right
      if (track_vertices[track_idx].y() < 0 || track_vertices[track_idx].y() > height) continue;

      // Flip so 0 is bottom of frame
      float lin_grad_point = (height - track_vertices[track_idx].y()) / height;

      // speed up: 120, slow down: 0
      float path_hue = fmax(fmin(60 + acceleration[i] * 35, 120), 0);
      // FIXME: painter.drawPolygon can be slow if hue is not rounded
      path_hue = int(path_hue * 100 + 0.5) / 100;

      float saturation = fmin(fabs(acceleration[i] * 1.5), 1);
      float lightness = util::map_val(saturation, 0.0f, 1.0f, 0.95f, 0.62f);        // lighter when grey
      float alpha = util::map_val(lin_grad_point, 0.75f / 2.f, 0.75f, 0.4f, 0.0f);  // matches previous alpha fade
      bg.setColorAt(lin_grad_point, QColor::fromHslF(path_hue / 360., saturation, lightness, alpha));

      // Skip a point, unless next is last
      i += (i + 2) < max_len ? 1 : 0;
    }

  } else {
    updatePathGradient(bg);
  }

  if (uiState()->status == STATUS_DISENGAGED && !lateral_only_active) {
    grayToneGradient(bg);
  }

  painter.setBrush(bg);
  painter.drawPolygon(track_vertices);
}

void ModelRenderer::updatePathGradient(QLinearGradient &bg) {
  static const QColor throttle_colors[] = {
      QColor::fromHslF(148. / 360., 0.94, 0.51, 0.4),
      QColor::fromHslF(112. / 360., 1.0, 0.68, 0.35),
      QColor::fromHslF(112. / 360., 1.0, 0.68, 0.0)};

  static const QColor no_throttle_colors[] = {
      QColor::fromHslF(148. / 360., 0.62, 0.70, 0.4),
      QColor::fromHslF(112. / 360., 0.72, 0.72, 0.35),
      QColor::fromHslF(112. / 360., 0.72, 0.72, 0.0),
  };

  // Transition speed; 0.1 corresponds to 0.5 seconds at UI_FREQ
  constexpr float transition_speed = 0.1f;

  // Start transition if throttle state changes
  bool allow_throttle = (*uiState()->sm)["longitudinalPlan"].getLongitudinalPlan().getAllowThrottle() || !longitudinal_control;
  if (allow_throttle != prev_allow_throttle) {
    prev_allow_throttle = allow_throttle;
    // Invert blend factor for a smooth transition when the state changes mid-animation
    blend_factor = std::max(1.0f - blend_factor, 0.0f);
  }

  const QColor *begin_colors = allow_throttle ? no_throttle_colors : throttle_colors;
  const QColor *end_colors = allow_throttle ? throttle_colors : no_throttle_colors;
  if (blend_factor < 1.0f) {
    blend_factor = std::min(blend_factor + transition_speed, 1.0f);
  }

  // Set gradient colors by blending the start and end colors
  bg.setColorAt(0.0f, blendColors(begin_colors[0], end_colors[0], blend_factor));
  bg.setColorAt(0.5f, blendColors(begin_colors[1], end_colors[1], blend_factor));
  bg.setColorAt(1.0f, blendColors(begin_colors[2], end_colors[2], blend_factor));
}

QColor ModelRenderer::blendColors(const QColor &start, const QColor &end, float t) {
  if (t == 1.0f) return end;
  return QColor::fromRgbF(
      (1 - t) * start.redF() + t * end.redF(),
      (1 - t) * start.greenF() + t * end.greenF(),
      (1 - t) * start.blueF() + t * end.blueF(),
      (1 - t) * start.alphaF() + t * end.alphaF());
}

void ModelRenderer::drawLeadBox(QPainter &painter, const LeadBoxState &lead_box, bool secondary) {
  if (!lead_box.visible) return;

  const QRectF box_rect(lead_box.x - lead_box.width * 0.5f - LEAD_BOX_HPAD,
                        lead_box.y - lead_box.width * LEAD_BOX_HEIGHT_FACTOR,
                        lead_box.width + LEAD_BOX_HPAD * 2.0f,
                        lead_box.width * LEAD_BOX_HEIGHT_FACTOR);

  QColor stroke = carrotBlue();
  QColor fill = carrotBlackAlpha(20);
  if (secondary) {
    stroke = carrotOchre();
    fill = lead_box.selected ? carrotRedAlpha(50) : carrotBlackAlpha(20);
  } else if (lead_box.radar_detected) {
    stroke = lead_box.lead_scc ? carrotRed() : carrotOrange();
  }

  painter.save();
  painter.setRenderHint(QPainter::Antialiasing);
  painter.setBrush(fill);
  painter.setPen(QPen(stroke, LEAD_BOX_STROKE));
  painter.drawRoundedRect(box_rect, LEAD_BOX_RADIUS, LEAD_BOX_RADIUS);
  painter.restore();
}

void ModelRenderer::drawLeadDistanceBadges(QPainter &painter, const LeadBoxState &lead_box) {
  if (!lead_box.visible) return;

  const bool is_metric = uiState()->scene.is_metric;
  const float display_y = lead_box.y + LEAD_BADGE_OFFSET_Y;
  const QColor text_color = QColor(255, 255, 255);
  const QFont badge_font = InterFont(30, QFont::Bold);

  painter.save();
  painter.setRenderHint(QPainter::Antialiasing);
  painter.setFont(badge_font);

  auto draw_badge = [&](float center_x, float distance, const QColor &bg) {
    if (distance <= 0.0f) return;

    const float display_distance = distance * (is_metric ? 1.0f : METER_TO_FOOT);
    const QString text = QString::number(display_distance, 'f', 1);
    const int text_width = painter.fontMetrics().horizontalAdvance(text);
    const QRectF rect(center_x - (text_width + 28) * 0.5f, display_y - 35.0f, text_width + 28, LEAD_BADGE_HEIGHT);

    painter.setPen(Qt::NoPen);
    painter.setBrush(bg);
    painter.drawRoundedRect(rect, LEAD_BOX_RADIUS, LEAD_BOX_RADIUS);
    drawOutlinedText(painter, rect, text, text_color);
  };

  draw_badge(lead_box.x - LEAD_BADGE_OFFSET_X, lead_box.radar_distance, lead_box.lead_scc ? carrotRed() : carrotOrange());
  draw_badge(lead_box.x + LEAD_BADGE_OFFSET_X, vision_dist, carrotBlue());
  painter.restore();
}

// Projects a point in car to space to the corresponding point in full frame image space.
bool ModelRenderer::mapToScreen(float in_x, float in_y, float in_z, QPointF *out) {
  Eigen::Vector3f input(in_x, in_y, in_z);
  auto pt = car_space_transform * input;
  *out = QPointF(pt.x() / pt.z(), pt.y() / pt.z());
  return clip_region.contains(*out);
}

void ModelRenderer::mapLineToPolygon(const cereal::XYZTData::Reader &line, float y_off, float z_off,
                                     QPolygonF *pvd, int max_idx, bool allow_invert, float center_y_off) {
  const auto line_x = line.getX(), line_y = line.getY(), line_z = line.getZ();
  QPointF left, right;
  pvd->clear();
  for (int i = 0; i <= max_idx; i++) {
    // highly negative x positions  are drawn above the frame and cause flickering, clip to zy plane of camera
    if (line_x[i] < 0) continue;

    const float center_y = line_y[i] + center_y_off;
    bool l = mapToScreen(line_x[i], center_y - y_off, line_z[i] + z_off, &left);
    bool r = mapToScreen(line_x[i], center_y + y_off, line_z[i] + z_off, &right);
    if (l && r) {
      // For wider lines the drawn polygon will "invert" when going over a hill and cause artifacts
      if (!allow_invert && pvd->size() && left.y() > pvd->back().y()) {
        continue;
      }
      pvd->push_back(left);
      pvd->push_front(right);
    }
  }
}
