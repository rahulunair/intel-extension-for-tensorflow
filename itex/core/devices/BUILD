package(default_visibility = ["//visibility:public"])

cc_library(
    name = "xpu_device_util",
    srcs = ["xpu_device_util.cc"],
    hdrs = ["xpu_device_util.h"],
    linkstatic = 1,
    visibility = ["//visibility:public"],
    deps = [
        "//itex/core:protos_all_cc",
        "//itex/core/utils:env_var",
        "//itex/core/utils:logging",
    ],
    alwayslink = True,
)

cc_library(
    name = "xpu_device",
    srcs = ["xpu_device.cc"],
    linkstatic = 1,
    visibility = ["//visibility:public"],
    deps = [
        ":xpu_device_util",
        "@local_config_tf//:tf_header_lib",
    ] + select({
        "@local_config_dpcpp//dpcpp:using_dpcpp": ["//itex/core/devices/gpu:gpu_device_impl"],
        "//conditions:default": [],
    }),
    alwayslink = True,
)

cc_library(
    name = "xpu_device_util_hdr",
    hdrs = ["xpu_device_util.h"],
    visibility = ["//visibility:public"],
    alwayslink = True,
)

cc_library(
    name = "bfc_allocator",
    srcs = ["bfc_allocator.cc"],
    hdrs = [
        "allocator.h",
        "bfc_allocator.h",
    ],
    visibility = ["//visibility:public"],
    deps = [
        "//itex/core/utils:env_var",
        "//itex/core/utils:hw_info",
        "//itex/core/utils:logging",
        "//itex/core/utils:mutex",
        "//third_party/build_option/dpcpp:dpcpp_header",
        "@com_google_absl//absl/container:flat_hash_set",
    ],
    alwayslink = True,
)
