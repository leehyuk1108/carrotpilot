#pragma once

#include <QPainter>

#include "selfdrive/ui/ui.h"

class DriverMonitorRenderer {
public:
  DriverMonitorRenderer();
  void updateState(const UIState &s);
  void draw(QPainter &painter, const QRect &surface_rect);

private:
  bool is_visible = false;
  bool is_active = false;
  bool is_rhd = false;
  bool is_engageable = false;
  bool is_enabled = false;
  float dm_fade_state = 1.0f;
  float cone_rotation_deg = 0.0f;
  QPixmap dm_background_img;
  QPixmap dm_cone_img;
  QPixmap dm_person_img;
  QPixmap dm_cone_disengaged_img;
  QPixmap dm_cone_engageable_img;
  QPixmap dm_cone_enabled_img;
};
