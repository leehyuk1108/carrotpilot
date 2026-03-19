#include "selfdrive/ui/qt/home.h"

#include <algorithm>
#include <QCoreApplication>
#include <QDateTime>
#include <QDir>
#include <QHBoxLayout>
#include <QJsonDocument>
#include <QJsonObject>
#include <QMouseEvent>
#include <QProcess>
#include <QStackedWidget>
#include <QVBoxLayout>

#include "selfdrive/ui/qt/offroad/experimental_mode.h"
#include "selfdrive/ui/qt/util.h"
#include "selfdrive/ui/qt/widgets/prime.h"

#ifdef ENABLE_MAPS
#include "selfdrive/ui/qt/maps/map_settings.h"
#endif

namespace {

QJsonObject loadTrackedStats(Params &params) {
  return QJsonDocument::fromJson(QByteArray::fromStdString(params.get("FrogPilotStats"))).object();
}

QString formatTrackedTime(int total_seconds) {
  const int total_minutes = qMax(0, total_seconds) / 60;
  const int hours = total_minutes / 60;
  const int minutes = total_minutes % 60;
  if (hours > 0) {
    return QString("%1시간 %2분").arg(QLocale().toString(hours), QLocale().toString(minutes));
  }
  return QString("%1분").arg(QLocale().toString(total_minutes));
}

QString formatTrackedDistance(bool is_metric, double meters) {
  const double distance_value = is_metric ? meters / 1000.0 : meters * METER_TO_MILE;
  const QString distance_unit = is_metric ? QObject::tr("km") : QObject::tr("mi");
  return QString("%1 %2").arg(QLocale().toString(qRound(distance_value)), distance_unit);
}

double getTrackedValue(const QJsonObject &stats, const char *key) {
  return stats.value(key).toDouble();
}

void addTrackedValue(QJsonObject &stats, const char *key, double delta) {
  stats.insert(key, getTrackedValue(stats, key) + delta);
}

QString readGitCommand(const QString &working_dir, const QStringList &arguments) {
  QProcess process;
  process.setProgram("git");
  process.setArguments(arguments);
  process.setWorkingDirectory(working_dir);
  process.start();
  if (!process.waitForFinished(400)) {
    process.kill();
    process.waitForFinished();
    return {};
  }
  return QString::fromUtf8(process.readAllStandardOutput()).trimmed();
}

QString fallbackBranchName() {
  QDir repo_dir(QCoreApplication::applicationDirPath());
  repo_dir.cdUp();
  repo_dir.cdUp();
  return readGitCommand(repo_dir.absolutePath(), {"rev-parse", "--abbrev-ref", "HEAD"});
}

QString fallbackCommitHash() {
  QDir repo_dir(QCoreApplication::applicationDirPath());
  repo_dir.cdUp();
  repo_dir.cdUp();
  return readGitCommand(repo_dir.absolutePath(), {"rev-parse", "--short", "HEAD"});
}

}  // namespace

// HomeWindow: the container for the offroad and onroad UIs

HomeWindow::HomeWindow(QWidget* parent) : QWidget(parent) {
  QHBoxLayout *main_layout = new QHBoxLayout(this);
  main_layout->setMargin(0);
  main_layout->setSpacing(0);

  sidebar = new Sidebar(this);
  main_layout->addWidget(sidebar);
  sidebar->setVisible(false);
  QObject::connect(sidebar, &Sidebar::openSettings, this, &HomeWindow::openSettings);

  slayout = new QStackedLayout();
  main_layout->addLayout(slayout);

  home = new OffroadHome(this);
  QObject::connect(home, &OffroadHome::openSettings, this, &HomeWindow::openSettings);
  slayout->addWidget(home);

  onroad = new OnroadWindow(this);
  QObject::connect(onroad, &OnroadWindow::mapPanelRequested, this, [=] { sidebar->hide(); });
  slayout->addWidget(onroad);

  body = new BodyWindow(this);
  slayout->addWidget(body);

  driver_view = new DriverViewWindow(this);
  connect(driver_view, &DriverViewWindow::done, [=] {
    showDriverView(false);
  });
  slayout->addWidget(driver_view);
  setAttribute(Qt::WA_NoSystemBackground);
  QObject::connect(uiState(), &UIState::uiUpdate, this, &HomeWindow::updateState);
  QObject::connect(uiState(), &UIState::offroadTransition, this, &HomeWindow::offroadTransition);
  QObject::connect(uiState(), &UIState::offroadTransition, sidebar, &Sidebar::offroadTransition);
}

void HomeWindow::showSidebar(bool show) {
  sidebar->setVisible(show && uiState()->scene.started);
}

void HomeWindow::showMapPanel(bool show) {
  onroad->showMapPanel(show);
}

void HomeWindow::updateState(const UIState &s) {
  const SubMaster &sm = *(s.sm);

  // switch to the generic robot UI
  if (onroad->isVisible() && !body->isEnabled() && sm["carParams"].getCarParams().getNotCar()) {
    body->setEnabled(true);
    slayout->setCurrentWidget(body);
  }
}

void HomeWindow::offroadTransition(bool offroad) {
  body->setEnabled(false);
  sidebar->setVisible(false);
  if (offroad) {
    slayout->setCurrentWidget(home);
  } else {
    slayout->setCurrentWidget(onroad);
    uiState()->show_brightness_timer = (int)(10. / 0.05);
  }
}

void HomeWindow::showDriverView(bool show) {
  if (show) {
    emit closeSettings();
    slayout->setCurrentWidget(driver_view);
  } else {
    slayout->setCurrentWidget(home);
  }
  sidebar->setVisible(show == false);
}

void HomeWindow::mousePressEvent(QMouseEvent* e) {
  if ((onroad->isVisible() || body->isVisible()) && (!sidebar->isVisible() || e->x() > sidebar->width())) {
    sidebar->setVisible(!sidebar->isVisible());
  }

  uiState()->show_brightness_timer = 100;
}

void HomeWindow::mouseDoubleClickEvent(QMouseEvent* e) {
  HomeWindow::mousePressEvent(e);
  const SubMaster &sm = *(uiState()->sm);
  if (sm["carParams"].getCarParams().getNotCar()) {
    if (onroad->isVisible()) {
      slayout->setCurrentWidget(body);
    } else if (body->isVisible()) {
      slayout->setCurrentWidget(onroad);
    }
    showSidebar(false);
  }
}

// OffroadHome: the offroad home page

OffroadHome::OffroadHome(QWidget* parent) : QFrame(parent) {
  QVBoxLayout* main_layout = new QVBoxLayout(this);
  main_layout->setContentsMargins(48, 40, 48, 48);

  QHBoxLayout *top_layout = new QHBoxLayout();
  top_layout->setContentsMargins(0, 0, 0, 0);
  top_layout->setSpacing(0);

  settings_button = new QPushButton(this);
  settings_button->setFixedSize(110, 110);
  settings_button->setIcon(QIcon("../assets/offroad/icon_settings.png"));
  settings_button->setIconSize(QSize(52, 52));
  settings_button->setCursor(Qt::PointingHandCursor);
  settings_button->setStyleSheet(R"(
    QPushButton {
      background-color: #1D1D1D;
      border: 2px solid #3A3A3A;
      border-radius: 55px;
      padding: 0;
    }
    QPushButton:pressed {
      background-color: #2A2A2A;
    }
  )");
  QObject::connect(settings_button, &QPushButton::clicked, [=]() { emit openSettings(); });
  top_layout->addWidget(settings_button, 0, Qt::AlignLeft | Qt::AlignTop);
  top_layout->addStretch(1);

  version = new ElidedLabel(this);
  version->setVisible(false);
  version->setAlignment(Qt::AlignCenter);
  version->setMaximumWidth(760);
  version->setMinimumHeight(68);
  version->setStyleSheet(R"(
    QLabel {
      color: #D8D8D8;
      background-color: rgba(20, 20, 20, 210);
      border: 1px solid #313131;
      border-radius: 20px;
      font-size: 30px;
      font-weight: 600;
      padding: 10px 24px;
    }
  )");
  top_layout->addWidget(version, 0, Qt::AlignRight | Qt::AlignTop);

  main_layout->addLayout(top_layout);
  main_layout->addSpacing(18);

  update_notif = new QPushButton(tr("UPDATE"));
  update_notif->setVisible(false);
  QObject::connect(update_notif, &QPushButton::clicked, [=]() { center_layout->setCurrentIndex(1); });
  alert_notif = new QPushButton();
  alert_notif->setVisible(false);
  QObject::connect(alert_notif, &QPushButton::clicked, [=] { center_layout->setCurrentIndex(2); });

  center_layout = new QStackedLayout();

  QWidget *home_widget = new QWidget(this);
  {
    QVBoxLayout *home_layout = new QVBoxLayout(home_widget);
    home_layout->setContentsMargins(0, 0, 0, 0);
    home_layout->setSpacing(0);
    home_layout->addSpacing(24);

    greeting_title = new QLabel(tr("안녕하세요"), this);
    greeting_title->setAlignment(Qt::AlignHCenter);
    greeting_title->setStyleSheet("font-size: 112px; font-weight: 800; color: #FFFFFF;");
    home_layout->addWidget(greeting_title, 0, Qt::AlignHCenter);

    home_layout->addSpacing(16);

    greeting_description = new QLabel(tr("오늘도 편안한 주행 되세요"), this);
    greeting_description->setAlignment(Qt::AlignHCenter);
    greeting_description->setStyleSheet("font-size: 52px; font-weight: 500; color: #AFAFAF;");
    home_layout->addWidget(greeting_description, 0, Qt::AlignHCenter);

    home_layout->addSpacing(72);

    QHBoxLayout *stats_layout = new QHBoxLayout();
    stats_layout->setContentsMargins(80, 0, 80, 0);
    stats_layout->setSpacing(34);

    auto create_stat = [this](QWidget **card, QLabel **value, QLabel **label, const QString &title) {
      QWidget *stat = new QWidget(this);
      stat->setObjectName("summaryStatCard");
      stat->setMinimumSize(0, 250);

      QVBoxLayout *layout = new QVBoxLayout(stat);
      layout->setContentsMargins(40, 34, 40, 34);
      layout->setSpacing(14);

      *value = new QLabel("0", stat);
      (*value)->setAlignment(Qt::AlignCenter);
      (*value)->setStyleSheet("font-size: 84px; font-weight: 800; color: #FFFFFF;");

      *label = new QLabel(title, stat);
      (*label)->setAlignment(Qt::AlignCenter);
      (*label)->setStyleSheet("font-size: 34px; font-weight: 600; color: #8D8D8D;");

      layout->addStretch(1);
      layout->addWidget(*value);
      layout->addWidget(*label);
      layout->addStretch(1);
      *card = stat;
      return stat;
    };

    stats_layout->addWidget(create_stat(&drive_time_card, &drive_time_value, &drive_time_label, tr("주행 시간")), 1);
    stats_layout->addWidget(create_stat(&drive_distance_card, &drive_distance_value, &drive_distance_label, tr("주행 거리")), 1);
    stats_layout->addWidget(create_stat(&drive_count_card, &drive_count_value, &drive_count_label, tr("주행 횟수")), 1);

    home_layout->addLayout(stats_layout);
    home_layout->addStretch(1);
  }
  center_layout->addWidget(home_widget);

  // add update & alerts widgets
  update_widget = new UpdateAlert();
  QObject::connect(update_widget, &UpdateAlert::dismiss, [=]() { center_layout->setCurrentIndex(0); });
  center_layout->addWidget(update_widget);
  alerts_widget = new OffroadAlert();
  QObject::connect(alerts_widget, &OffroadAlert::dismiss, [=]() { center_layout->setCurrentIndex(0); });
  center_layout->addWidget(alerts_widget);

  main_layout->addLayout(center_layout, 1);

  timer = new QTimer(this);
  timer->callOnTimeout(this, &OffroadHome::refresh);

  tracked_stats = loadTrackedStats(params);
  if (tracked_stats.isEmpty()) {
    tracked_stats.insert("FrogPilotDrives", params.getInt("RouteCount"));
  }

  setStyleSheet(R"(
    * {
      color: white;
    }
    OffroadHome {
      background-color: black;
    }
    QWidget#summaryStatCard {
      background-color: #141414;
      border: 1px solid #242424;
      border-radius: 30px;
    }
    QWidget#summaryStatCard QLabel {
      background: transparent;
      border: none;
    }
  )");

  QObject::connect(uiState(), &UIState::uiUpdate, this, [this](const UIState &s) {
    updateTrackedStats(s);
  });
  QObject::connect(uiState(), &UIState::offroadTransition, this, [this](bool offroad) {
    tracked_stats = loadTrackedStats(params);
    if (!offroad) {
      previous_drive_stats = tracked_stats;
      drive_counted = false;
    } else if (previously_onroad) {
      persistTrackedStats();
      tracked_stats = loadTrackedStats(params);
      last_drive_ended_at = QDateTime::currentDateTime();
      show_recent_drive_summary = true;
    }

    previously_onroad = !offroad;
    updateOffroadContent();
  });
}

void OffroadHome::mousePressEvent(QMouseEvent *event) {
  if (settings_button->geometry().adjusted(-12, -12, 12, 12).contains(event->pos())) {
    emit openSettings();
    event->accept();
    return;
  }
  QFrame::mousePressEvent(event);
}

void OffroadHome::showEvent(QShowEvent *event) {
  updateVersionBadge();
  refresh();
  timer->start(10 * 1000);
}

void OffroadHome::hideEvent(QHideEvent *event) {
  timer->stop();
}

void OffroadHome::refresh() {
  updateVersionBadge();
  bool updateAvailable = update_widget->refresh();
  int alerts = alerts_widget->refresh();

  // pop-up new notification
  int idx = center_layout->currentIndex();
  if (!updateAvailable && !alerts) {
    idx = 0;
  } else if (updateAvailable && (!update_notif->isVisible() || (!alerts && idx == 2))) {
    idx = 1;
  } else if (alerts && (!alert_notif->isVisible() || (!updateAvailable && idx == 1))) {
    idx = 2;
  }
  center_layout->setCurrentIndex(idx);

  update_notif->setVisible(updateAvailable);
  alert_notif->setVisible(alerts);
  if (alerts) {
    alert_notif->setText(QString::number(alerts) + (alerts > 1 ? tr(" ALERTS") : tr(" ALERT")));
  }
  updateOffroadContent();
}

void OffroadHome::updateVersionBadge() {
  QString branch = QString::fromStdString(params.get("GitBranch")).trimmed();
  QString commit = QString::fromStdString(params.get("GitCommit")).trimmed();

  if ((branch.isEmpty() || commit.isEmpty()) && !git_fallback_loaded) {
    fallback_git_branch = fallbackBranchName();
    fallback_git_commit = fallbackCommitHash();
    git_fallback_loaded = true;
  }

  if (branch.isEmpty()) {
    branch = fallback_git_branch;
  }
  if (commit.isEmpty()) {
    commit = fallback_git_commit;
  }

  if (commit.size() > 7) {
    commit = commit.left(7);
  }

  QStringList parts;
  if (!branch.isEmpty()) {
    parts << branch;
  }
  if (!commit.isEmpty()) {
    parts << commit;
  }

  const QString badge_text = parts.isEmpty()
      ? QString()
      : QString("carrotpilot  %1").arg(parts.join("  ·  "));

  version->setVisible(!badge_text.isEmpty());
  version->setText(badge_text);
}

void OffroadHome::updateGreetingStats() {
  tracked_stats = loadTrackedStats(params);
  if (tracked_stats.isEmpty()) {
    tracked_stats.insert("FrogPilotDrives", params.getInt("RouteCount"));
  }

  const bool is_metric = params.getBool("IsMetric");
  const int total_drives = qMax(0, tracked_stats.value("FrogPilotDrives").toInt());
  const int total_seconds = qMax(0, qRound(getTrackedValue(tracked_stats, "FrogPilotSeconds")));
  const double total_meters = qMax(0.0, getTrackedValue(tracked_stats, "FrogPilotMeters"));

  drive_time_value->setText(formatTrackedTime(total_seconds));
  drive_time_label->setText(tr("주행 시간"));
  drive_distance_value->setText(formatTrackedDistance(is_metric, total_meters));
  drive_distance_label->setText(tr("주행 거리"));
  drive_count_value->setText(QLocale().toString(total_drives));
  drive_count_label->setText(tr("주행 횟수"));
}

void OffroadHome::updateDriveSummaryStats() {
  tracked_stats = loadTrackedStats(params);
  const bool is_metric = params.getBool("IsMetric");

  auto diff_double = [&](const char *key) {
    return getTrackedValue(tracked_stats, key) - getTrackedValue(previous_drive_stats, key);
  };

  const int tracked_time = qMax(0, qRound(diff_double("TrackedTime")));
  const int engaged_time = qMax(0, qRound(diff_double("AOLTime") + diff_double("LongitudinalTime")));
  const double drive_meters = qMax(0.0, diff_double("FrogPilotMeters"));
  const int engagement_percent = tracked_time > 0 ? engaged_time * 100 / tracked_time : 0;

  drive_time_value->setText(formatTrackedTime(tracked_time));
  drive_time_label->setText(tr("주행 시간"));
  drive_distance_value->setText(formatTrackedDistance(is_metric, drive_meters));
  drive_distance_label->setText(tr("주행 거리"));
  drive_count_value->setText(QString("%1%").arg(QLocale().toString(engagement_percent)));
  drive_count_label->setText(tr("오픈파일럿 사용 비율"));
}

void OffroadHome::updateOffroadContent() {
  const bool summary_preview = util::getenv("OFFROAD_SUMMARY_PREVIEW", 0) == 1;
  const bool recent_summary_active = summary_preview || (show_recent_drive_summary && last_drive_ended_at.isValid() &&
                                     last_drive_ended_at.secsTo(QDateTime::currentDateTime()) < 600);

  if (recent_summary_active) {
    greeting_title->setText(tr("주행이 종료되었습니다"));
    greeting_description->setText(tr("수고하셨습니다"));
    updateDriveSummaryStats();
  } else {
    show_recent_drive_summary = false;
    greeting_title->setText(tr("안녕하세요"));
    greeting_description->setText(tr("오늘도 편안한 주행 되세요"));
    updateGreetingStats();
  }
}

void OffroadHome::persistTrackedStats() {
  params.put("FrogPilotStats", QJsonDocument(tracked_stats).toJson(QJsonDocument::Compact).toStdString());
}

void OffroadHome::updateTrackedStats(const UIState &s) {
  const double now = QDateTime::currentMSecsSinceEpoch() / 1000.0;
  if (last_tracking_time <= 0.0) {
    last_tracking_time = now;
    last_persist_time = now;
    return;
  }

  const double dt = std::clamp(now - last_tracking_time, 0.0, 0.2);
  last_tracking_time = now;

  if (!s.scene.started || !s.sm->alive("carState")) {
    if (now - last_persist_time >= 5.0 && !tracked_stats.isEmpty()) {
      persistTrackedStats();
      last_persist_time = now;
    }
    drive_counted = false;
    return;
  }

  if (tracked_stats.isEmpty()) {
    tracked_stats = loadTrackedStats(params);
  }
  if (!drive_counted) {
    addTrackedValue(tracked_stats, "FrogPilotDrives", 1);
    drive_counted = true;
  }
  if (dt <= 0.0) {
    return;
  }

  const SubMaster &sm = *(s.sm);
  const auto car_state = sm["carState"].getCarState();
  const auto car_control = sm["carControl"].getCarControl();

  addTrackedValue(tracked_stats, "FrogPilotSeconds", dt);
  addTrackedValue(tracked_stats, "TrackedTime", dt);
  addTrackedValue(tracked_stats, "FrogPilotMeters", std::max(0.0f, car_state.getVEgo()) * dt);

  if (car_control.getLongActive()) {
    addTrackedValue(tracked_stats, "LongitudinalTime", dt);
  } else if (car_control.getLatActive() || car_state.getLatEnabled()) {
    addTrackedValue(tracked_stats, "AOLTime", dt);
  }

  if (now - last_persist_time >= 5.0) {
    persistTrackedStats();
    last_persist_time = now;
  }
}
