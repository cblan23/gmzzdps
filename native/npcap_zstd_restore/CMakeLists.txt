cmake_minimum_required(VERSION 3.20)
project(npcap_zstd_restore C)

if(NOT DEFINED ZSTD_ROOT)
    message(FATAL_ERROR "ZSTD_ROOT is required")
endif()
if(NOT DEFINED ZSTD_STATIC_LIB)
    message(FATAL_ERROR "ZSTD_STATIC_LIB is required")
endif()

add_library(npcap_zstd_restore SHARED npcap_zstd_restore.c)
target_compile_features(npcap_zstd_restore PRIVATE c_std_99)
target_compile_definitions(
    npcap_zstd_restore
    PRIVATE
        ZSTD_LEGACY_SUPPORT=5
)
target_include_directories(
    npcap_zstd_restore
    PRIVATE
        "${ZSTD_ROOT}/lib"
        "${ZSTD_ROOT}/lib/common"
        "${ZSTD_ROOT}/lib/decompress"
)
target_link_libraries(npcap_zstd_restore PRIVATE "${ZSTD_STATIC_LIB}")
set_property(
    TARGET npcap_zstd_restore
    PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
)
