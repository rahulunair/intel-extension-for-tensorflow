/* Copyright (c) 2021-2022 Intel Corporation

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
==============================================================================*/

#include "itex/core/devices/xpu_device_util.h"

#include <cstring>

namespace itex {

static const char* frozen_backend = nullptr;
static ConfigProto frozen_config;
static bool backend_is_frozen = false;

void itex_freeze_backend(const char* backend, const ConfigProto& config) {
  if (strcasecmp(backend, "GPU") == 0) {
    frozen_backend = itex::DEVICE_GPU;
  } else if (strcasecmp(backend, "CPU") == 0) {
    frozen_backend = itex::DEVICE_CPU;
  } else if (strcasecmp(backend, "AUTO") == 0) {
    frozen_backend = itex::DEVICE_AUTO;
  } else {
    ITEX_LOG(FATAL) << "Invalid ITEX_BACKEND: " << backend
                    << ", please select from CPU, GPU, AUTO";
  }

  backend_is_frozen = true;
  frozen_config = config;
}

void itex_freeze_backend(ITEX_BACKEND backend, const ConfigProto& config) {
  std::string backend_s;
  switch (backend) {
    case ITEX_BACKEND_GPU:
      frozen_backend = itex::DEVICE_GPU;
      break;
    case ITEX_BACKEND_CPU:
      frozen_backend = itex::DEVICE_CPU;
      break;
    case ITEX_BACKEND_AUTO:
      frozen_backend = itex::DEVICE_AUTO;
      break;
    default:
      ITEX_LOG(FATAL) << "Invalid ITEX_BACKEND: " << backend
                      << ", please select from CPU, GPU, AUTO";
  }
}

ITEX_BACKEND itex_get_backend() {
  const char* backend = nullptr;
  if (backend_is_frozen) {
    backend = frozen_backend;
  } else {
    backend = std::getenv("ITEX_BACKEND");
    if (backend == nullptr) return ITEX_BACKEND_DEFAULT;
  }

  if (strcasecmp(backend, "GPU") == 0) {
    return ITEX_BACKEND_GPU;
  } else if (strcasecmp(backend, "CPU") == 0) {
    return ITEX_BACKEND_CPU;
  } else if (strcasecmp(backend, "AUTO") == 0) {
    return ITEX_BACKEND_AUTO;
  } else {
    ITEX_LOG(FATAL) << "Invalid ITEX_BACKEND: " << backend
                    << ", please select from CPU, GPU, AUTO";
    return ITEX_BACKEND_DEFAULT;
  }
}

ConfigProto itex_get_config() { return frozen_config; }

void itex_set_backend(const char* backend, const ConfigProto& config) {
  if (backend_is_frozen && (strcasecmp(backend, frozen_backend) != 0)) {
    ITEX_LOG(INFO) << "ITEX backend is already set as " << frozen_backend
                   << ", setting backend as " << backend << " is ignored";
    return;
  }

  itex_freeze_backend(backend, config);
}

void itex_backend_to_string(ITEX_BACKEND backend, std::string* backend_string) {
  switch (backend) {
    case ITEX_BACKEND_GPU:
      *backend_string = "GPU";
      break;
    case ITEX_BACKEND_CPU:
      *backend_string = "CPU";
      break;
    case ITEX_BACKEND_AUTO:
      *backend_string = "AUTO";
      break;
    default:
      ITEX_LOG(INFO) << "Unkown ITEX_BACKEND: " << backend;
      *backend_string = "";
      break;
  }
}

const char* GetDeviceBackendName(const std::string& device_name) {
  if (device_name.find(DEVICE_XPU) != std::string::npos) {
    ITEX_BACKEND backend = itex_get_backend();
    switch (backend) {
      case ITEX_BACKEND_GPU:
        return DEVICE_GPU;
      case ITEX_BACKEND_CPU:
        return DEVICE_CPU;
      case ITEX_BACKEND_AUTO:
        return DEVICE_AUTO;
      default:
        return "";
    }

  } else if (device_name.find(itex::DEVICE_GPU) != std::string::npos) {
    return DEVICE_GPU;
  } else if (device_name.find(itex::DEVICE_CPU) != std::string::npos) {
    return DEVICE_CPU;
  } else {
    ITEX_CHECK(false) << "Unsupported device type: " << device_name;
  }
}

}  // namespace itex
