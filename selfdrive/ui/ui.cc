#include "selfdrive/ui/ui.h"

#include <algorithm>
#include <cmath>

#include <QtConcurrent>

#include "common/transformations/orientation.hpp"
#include "common/swaglog.h"
#include "common/timing.h"
#include "common/util.h"
#include "common/watchdog.h"
#include "system/hardware/hw.h"

#define BACKLIGHT_DT 0.05
#define BACKLIGHT_TS 10.00

constexpr float AUTO_BRIGHTNESS_MIN = 1.0f;
constexpr float AUTO_BRIGHTNESS_MAX = 100.0f;
constexpr float AUTO_BRIGHTNESS_EXPOSURE_MAX = 100.0f;
constexpr float AUTO_BRIGHTNESS_EXPOSURE_GAMMA = 0.8f;
constexpr float AUTO_BRIGHTNESS_DIM_FLOOR = 10.0f;
constexpr float AUTO_BRIGHTNESS_DARK_THRESHOLD = 4.0f;
constexpr int MIN_BACKLIGHT_BRIGHTNESS = 10;
constexpr double STARTED_FALL_DEBOUNCE_S = 5.0;

static void update_sockets(UIState *s) {
  s->sm->update(0);
}

static void update_state(UIState *s) {
  SubMaster &sm = *(s->sm);
  UIScene &scene = s->scene;
  const bool force_onroad_preview = qEnvironmentVariableIntValue("FORCE_ONROAD_PREVIEW") == 1;

  if (sm.updated("liveCalibration")) {
    auto list2rot = [](const capnp::List<float>::Reader &rpy_list) ->Eigen::Matrix3f {
      return euler2rot({rpy_list[0], rpy_list[1], rpy_list[2]}).cast<float>();
    };

    auto live_calib = sm["liveCalibration"].getLiveCalibration();
    if (live_calib.getCalStatus() == cereal::LiveCalibrationData::Status::CALIBRATED) {
      auto device_from_calib = list2rot(live_calib.getRpyCalib());
      auto wide_from_device = list2rot(live_calib.getWideFromDeviceEuler());
      s->scene.view_from_calib = VIEW_FROM_DEVICE * device_from_calib;
      s->scene.view_from_wide_calib = VIEW_FROM_DEVICE * wide_from_device * device_from_calib;
    } else {
      s->scene.view_from_calib = s->scene.view_from_wide_calib = VIEW_FROM_DEVICE;
    }
  }
  if (sm.updated("pandaStates")) {
    auto pandaStates = sm["pandaStates"].getPandaStates();
    if (pandaStates.size() > 0) {
      scene.pandaType = pandaStates[0].getPandaType();

      if (scene.pandaType != cereal::PandaState::PandaType::UNKNOWN) {
        scene.ignition = false;
        for (const auto& pandaState : pandaStates) {
          scene.ignition |= pandaState.getIgnitionLine() || pandaState.getIgnitionCan();
        }
      }
    }
  } else if ((s->sm->frame - s->sm->rcv_frame("pandaStates")) > 5*UI_FREQ) {
    scene.pandaType = cereal::PandaState::PandaType::UNKNOWN;
  }
  if (sm.updated("wideRoadCameraState")) {
    auto cam_state = sm["wideRoadCameraState"].getWideRoadCameraState();
    float exposure = std::clamp(cam_state.getExposureValPercent(), 0.0f, AUTO_BRIGHTNESS_EXPOSURE_MAX);
    float normalized_exposure = exposure / AUTO_BRIGHTNESS_EXPOSURE_MAX;
    float normalized_light = std::pow(1.0f - normalized_exposure, AUTO_BRIGHTNESS_EXPOSURE_GAMMA);
    scene.light_sensor = AUTO_BRIGHTNESS_MAX * normalized_light;
  } else if (!sm.allAliveAndValid({"wideRoadCameraState"})) {
    scene.light_sensor = -1;
  }
  if (sm.updated("longitudinalPlan")) {
    auto lp = sm["longitudinalPlan"].getLongitudinalPlan();
    scene.carrot_experimental_mode = lp.getXState() == 4;
  }

  if (force_onroad_preview) {
    scene.ignition = true;
    if (scene.pandaType == cereal::PandaState::PandaType::UNKNOWN) {
      scene.pandaType = cereal::PandaState::PandaType::UNO;
    }
  }

  bool raw_started = (sm["deviceState"].getDeviceState().getStarted() && scene.ignition) || force_onroad_preview;
  if (raw_started) {
    scene.started = true;
    s->started_false_since = -1.0;
  } else if (scene.started) {
    double now = seconds_since_boot();
    if (s->started_false_since < 0.0) {
      s->started_false_since = now;
    }
    scene.started = (now - s->started_false_since) < STARTED_FALL_DEBOUNCE_S;
  } else {
    s->started_false_since = -1.0;
    scene.started = false;
  }
}

void ui_update_params(UIState *s) {
  auto params = Params();
  s->scene.is_metric = params.getBool("IsMetric");
  s->show_brightness_ratio = params.getFloat("ShowCustomBrightness") / 100.;
  s->scene.map_on_left = params.getBool("NavSettingLeftSide");

}

void UIState::updateStatus() {
  const bool force_onroad_preview = qEnvironmentVariableIntValue("FORCE_ONROAD_PREVIEW") == 1;
  const bool force_engaged_preview = qEnvironmentVariableIntValue("FORCE_ENGAGED_PREVIEW") == 1;
  if (scene.started && sm->updated("selfdriveState")) {
    auto ss = (*sm)["selfdriveState"].getSelfdriveState();
    auto state = ss.getState();
    if (state == cereal::SelfdriveState::OpenpilotState::PRE_ENABLED || state == cereal::SelfdriveState::OpenpilotState::OVERRIDING) {
      status = STATUS_OVERRIDE;
    } else {
      status = ss.getEnabled() ? STATUS_ENGAGED : STATUS_DISENGAGED;
    }
  } else if (scene.started && force_onroad_preview) {
    status = force_engaged_preview ? STATUS_ENGAGED : STATUS_DISENGAGED;
  }

  // Handle onroad/offroad transition
  if (scene.started != started_prev || sm->frame == 1) {
    if (scene.started) {
      status = STATUS_DISENGAGED;
      scene.started_frame = sm->frame;
    }
    started_prev = scene.started;
    emit offroadTransition(!scene.started);
  }
}

UIState::UIState(QObject *parent) : QObject(parent) {
  ublox_avaliable = Params().getBool("UbloxAvailable");
  auto gps_service = (ublox_avaliable) ? "gpsLocationExternal" : "gpsLocation";
  sm = std::make_unique<SubMaster>(std::vector<const char*>{
    "modelV2", "controlsState", "liveCalibration", "radarState", "deviceState",
    "pandaStates", "carParams", "driverMonitoringState", "carState", "driverStateV2",
    "wideRoadCameraState", "managerState", "selfdriveState", "longitudinalPlan",
    "longitudinalPlan",
    "carControl", "carrotMan", "liveTorqueParameters", "lateralPlan", "liveParameters",
    "navRoute", "navInstruction", "navInstructionCarrot", gps_service, "liveDelay",
    "peripheralState",
  });
  prime_state = new PrimeState(this);
  language = QString::fromStdString(Params().get("LanguageSetting"));

  // update timer
  timer = new QTimer(this);
  QObject::connect(timer, &QTimer::timeout, this, &UIState::update);
  timer->start(1000 / UI_FREQ);
}

void UIState::update() {
  update_sockets(this);
  update_state(this);
  updateStatus();

  if (sm->frame % 100 == 0)
      ui_update_params(uiState());

  if (sm->frame % UI_FREQ == 0) {
    watchdog_kick(nanos_since_boot());
  }
  emit uiUpdate(*this);
}

Device::Device(QObject *parent) : brightness_filter(BACKLIGHT_OFFROAD, BACKLIGHT_TS, BACKLIGHT_DT), QObject(parent) {
  setAwake(true);
  resetInteractiveTimeout();

  QObject::connect(uiState(), &UIState::uiUpdate, this, &Device::update);
}

void Device::update(const UIState &s) {
  updateBrightness(s);
  updateWakefulness(s);

}

void Device::setAwake(bool on) {
  if (on != awake) {
    awake = on;
    Hardware::set_display_power(awake);
    LOGD("setting display power %d", awake);
    emit displayPowerChanged(awake);
  }
}

void Device::resetInteractiveTimeout(int timeout) {
  if (timeout == -1) {
    timeout = (ignition_on ? 10 : 30);
  }
  interactive_timeout = timeout * UI_FREQ;
}

void Device::updateBrightness(const UIState &s) {
  float clipped_brightness = offroad_brightness;
  if (s.scene.started && s.scene.light_sensor >= 0) {
    const float raw_light_sensor = s.scene.light_sensor;
    clipped_brightness = raw_light_sensor;

    // CIE 1931 - https://www.photonstophotos.net/GeneralTopics/Exposure/Psychometric_Lightness_and_Gamma.htm
    if (clipped_brightness <= 8) {
      clipped_brightness = (clipped_brightness / 903.3);
    } else {
      clipped_brightness = std::pow((clipped_brightness + 16.0) / 116.0, 3.0);
    }

    const float auto_brightness_min = raw_light_sensor > AUTO_BRIGHTNESS_DARK_THRESHOLD ? AUTO_BRIGHTNESS_DIM_FLOOR : AUTO_BRIGHTNESS_MIN;
    clipped_brightness = std::clamp(100.0f * clipped_brightness, auto_brightness_min, AUTO_BRIGHTNESS_MAX);
  }

  if (s.scene.started) {
      if (s.show_brightness_timer > 0) {
          UIState* s1 = uiState();
          s1->show_brightness_timer--;
      }
      else clipped_brightness *= s.show_brightness_ratio;
      //printf("show_brightness_timer: %d, clipped_brightness = %.2f ratio = %.1f\n", s.show_brightness_timer, clipped_brightness, s.show_brightness_ratio);
  }

  int brightness = brightness_filter.update(clipped_brightness);
  if (!awake) {
    brightness = 0;
  } else {
    brightness = std::max(brightness, MIN_BACKLIGHT_BRIGHTNESS);
  }

  if (brightness != last_brightness) {
    if (!brightness_future.isRunning()) {
      brightness_future = QtConcurrent::run(Hardware::set_brightness, brightness);
      last_brightness = brightness;
    }
  }
}

void Device::updateWakefulness(const UIState &s) {
  bool ignition_just_turned_off = !s.scene.ignition && ignition_on;
  ignition_on = s.scene.ignition;

  if (ignition_just_turned_off) {
    resetInteractiveTimeout();
  } else if (interactive_timeout > 0 && --interactive_timeout == 0) {
    emit interactiveTimeout();
  }

  setAwake(s.scene.ignition || interactive_timeout > 0);
}

UIState *uiState() {
  static UIState ui_state;
  return &ui_state;
}

Device *device() {
  static Device _device;
  return &_device;
}
