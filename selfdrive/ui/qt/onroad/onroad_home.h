#pragma once

#include <QWidget>

#include "selfdrive/ui/qt/onroad/alerts.h"
#include "selfdrive/ui/qt/onroad/annotated_camera.h"

class OnroadWindow : public QWidget {
  Q_OBJECT

public:
  OnroadWindow(QWidget *parent = 0);
  bool isMapVisible() const { return false; }
  void showMapPanel(bool show) { Q_UNUSED(show); }

signals:
  void mapPanelRequested();

private:
  void paintEvent(QPaintEvent *event) override;

  OnroadAlerts *alerts;
  AnnotatedCameraWidget *nvg;
  QColor bg = bg_colors[STATUS_DISENGAGED];
  QHBoxLayout *split;

private slots:
  void offroadTransition(bool offroad);
  void updateState(const UIState &s);
};
