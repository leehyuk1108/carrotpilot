#pragma once

#include <QPainter>
#include <QPolygonF>

#include "selfdrive/ui/ui.h"

class ModelRenderer {
public:
  ModelRenderer() {}
  void setTransform(const Eigen::Matrix3f &transform) { car_space_transform = transform; }
  void draw(QPainter &painter, const QRect &surface_rect);

private:
  struct LeadBoxState {
    bool visible = false;
    bool initialized = false;
    bool radar_detected = false;
    bool lead_scc = false;
    bool selected = false;
    float x = 0.0f;
    float y = 0.0f;
    float width = 0.0f;
    float radar_distance = 0.0f;
  };

public:
  bool mapToScreen(float in_x, float in_y, float in_z, QPointF *out);
  void mapLineToPolygon(const cereal::XYZTData::Reader &line, float y_off, float z_off,
                        QPolygonF *pvd, int max_idx, bool allow_invert = true, float center_y_off = 0.0f);
  void drawLeadBox(QPainter &painter, const LeadBoxState &lead_box, bool secondary = false);
  void drawLeadDistanceBadges(QPainter &painter, const LeadBoxState &lead_box);
  void update_leads(const cereal::RadarState::Reader &radar_state, const cereal::ModelDataV2::Reader &model,
                    const cereal::LongitudinalPlan::Reader &longitudinal_plan, const QRect &surface_rect);
  void update_model(const cereal::ModelDataV2::Reader &model, const cereal::RadarState::LeadData::Reader &lead);
  void drawLaneLines(QPainter &painter);
  void drawPath(QPainter &painter, const cereal::ModelDataV2::Reader &model, int height);
  void updatePathGradient(QLinearGradient &bg);
  QColor blendColors(const QColor &start, const QColor &end, float t);

  bool longitudinal_control = false;
  bool lateral_only_active = false;
  bool lane_mode_active = false;
  bool experimental_mode = false;
  cereal::LaneChangeState lane_change_state = cereal::LaneChangeState::OFF;
  cereal::LaneChangeDirection lane_change_direction = cereal::LaneChangeDirection::NONE;
  float blend_factor = 1.0f;
  bool prev_allow_throttle = true;
  float lane_line_probs[4] = {};
  float road_edge_stds[2] = {};
  float path_offset_z = 1.22f;
  QPolygonF track_vertices;
  QPolygonF lane_line_vertices[4] = {};
  QPolygonF road_edge_vertices[2] = {};
  LeadBoxState lead_boxes[2] = {};
  float vision_dist = 0.0f;
  Eigen::Matrix3f car_space_transform = Eigen::Matrix3f::Zero();
  QRectF clip_region;
};
