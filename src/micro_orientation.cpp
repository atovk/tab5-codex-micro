#include "micro_orientation.h"
#include "orientation_policy.h"
#include <Arduino.h>
#include <M5Unified.h>
#include <algorithm>
#include <cmath>

namespace MicroOrientation {
namespace {
OrientationPolicy::Policy policy{3};
Diagnostics state;
uint32_t lastPoll=0;
}
void begin(uint8_t rotation) {
  policy.reset(rotation);
  state={}; state.rotation=policy.rotation();
  state.available=M5.Imu.isEnabled();
  lastPoll=millis()-50;
}
bool poll(bool blocked) {
  state.blocked=blocked;
  const uint32_t now=millis();
  if (blocked) {
    policy.update(now,0,0,true);
    return false;
  }
  if (!state.available || now-lastPoll<50) return false;
  lastPoll=now;
  const uint32_t began=micros();
  const auto mask=M5.Imu.update();
  m5::IMU_Class::imu_data_t data{};
  M5.Imu.getImuData(&data);
  state.readMaxUs=std::max(state.readMaxUs,micros()-began);
  if (!(mask & m5::IMU_Class::sensor_mask_accel)) {
    ++state.failures; policy.update(now,0,0,true); return false;
  }
  state.ax=data.accel.x; state.ay=data.accel.y; state.az=data.accel.z;
  state.gx=data.gyro.x; state.gy=data.gyro.y; state.gz=data.gyro.z;
  ++state.samples; state.sampledAt=now;
  const float norm=std::sqrt(state.ax*state.ax+state.ay*state.ay+state.az*state.az);
  // Calibrated on this device with the user holding its screw-hole edge down:
  // ax is +1g and the display needs rotation3. Inverted ax<0 needs rotation1.
  // Project -ax into the policy; do not infer its sign from a rear-view diagram.
  // Keep the horizontal layout when upright in portrait or resting flat.
  const float down=OrientationPolicy::landscapeProjection(state.ax,state.ay);
  const bool moving=!std::isfinite(state.gx) || !std::isfinite(state.gy)
      || !std::isfinite(state.gz) || std::fabs(state.gx)>45.f
      || std::fabs(state.gy)>45.f || std::fabs(state.gz)>45.f;
  const uint8_t next=policy.update(now,down,norm,blocked || moving);
  if (next==state.rotation) return false;
  state.rotation=next; ++state.changes;
  return true;
}
uint8_t rotation() { return state.rotation; }
void setTestRotation(uint8_t rotation) {
  policy.reset(rotation); state.rotation=policy.rotation();
}
Diagnostics diagnostics() { return state; }
}
