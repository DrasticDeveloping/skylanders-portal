# skylanders-portal: Skylanders Portal of Power emulation for ReXGlue recompilations.
#
# skylanders_portal_core has no ReXGlue dependency and is unit-tested. It still needs the AES and
# MD5 sources the ReXGlue SDK vendors (for figure_stats), so REXSDK_DIR must point at an SDK tree.
# skylanders_portal_rex (added below when available) needs the rex::runtime target.

cmake_minimum_required(VERSION 3.25)
project(skylanders_portal LANGUAGES CXX C)

# skylanders_game_icon(): the game's logo from its disc as the exe's icon (cmake/game_icon.cmake).
include(cmake/game_icon.cmake)
# skylanders_game_xex(): a game's disc check, its installer CLI and tests (cmake/game_xex.cmake).
include(cmake/game_xex.cmake)

# skylanders_alchemy: readers for the games' Alchemy files (IGA archives in their versions, IGZ
# objects, Swap Force's IGX, the LZMA and deflate blocks inside), no SDK needed, for the games and
# the launcher. inflate.cpp takes stb_image's zlib decoder: the SDK's copy, or without an SDK the
# folder SKYLANDERS_STB_DIR names (the one holding stb_image.h).
if(REXSDK_DIR)
    set(SKYLANDERS_ALCHEMY_STB "${REXSDK_DIR}/thirdparty/stb")
elseif(SKYLANDERS_STB_DIR)
    set(SKYLANDERS_ALCHEMY_STB "${SKYLANDERS_STB_DIR}")
endif()
if(SKYLANDERS_ALCHEMY_STB)
    add_library(skylanders_alchemy STATIC
        src/alchemy/iga_archive.cpp
        src/alchemy/iga8_archive.cpp
        src/alchemy/iga11_archive.cpp
        src/alchemy/igx_reader.cpp
        src/alchemy/igz_file.cpp
        src/alchemy/igz6_file.cpp
        src/alchemy/igz8_file.cpp
        src/alchemy/inflate.cpp
        src/alchemy/lzma_decoder.cpp
        src/alchemy/xenos_dxt.cpp
    )
    target_include_directories(skylanders_alchemy PUBLIC src)
    target_include_directories(skylanders_alchemy PRIVATE "${SKYLANDERS_ALCHEMY_STB}")
    target_compile_features(skylanders_alchemy PUBLIC cxx_std_23)
endif()

# skylanders_online: Swap Force Online's session (UDP sockets and lanes, the session messages, join
# codes, the shared tick, the level barrier, the request state machine). Game-independent and without the SDK, so every build
# mode has it (it stays above the figures-only return below).
add_library(skylanders_online STATIC
    src/netplay/datagram.cpp
    src/netplay/lanes.cpp
    src/netplay/port_mapping.cpp
    src/netplay/udp_socket.cpp
    src/online/join_code.cpp
    src/online/protocol.cpp
    src/online/requests.cpp
    src/online/session.cpp
    src/online/tick.cpp
)
target_include_directories(skylanders_online PUBLIC src)
target_compile_features(skylanders_online PUBLIC cxx_std_23)
if(WIN32)
    target_link_libraries(skylanders_online PUBLIC ws2_32)
endif()

# skylanders_nav: a path planner over a navigation mesh of convex areas (islands, point lookup, A*
# over the areas, a funnel over the shared edges) for the developer navto command. Game-independent,
# no SDK, nothing on disk: the game fills the mesh from memory.
add_library(skylanders_nav STATIC
    src/nav/nav_planner.cpp
)
target_include_directories(skylanders_nav PUBLIC src)
target_compile_features(skylanders_nav PUBLIC cxx_std_23)

# skylanders_backend: Swap Force Online's backend core and its tests (stage 2, src/backend), off
# until stage 2 is the current stage.
option(SKYLANDERS_BACKEND "Build the online backend core (stage 2) and its tests" OFF)
if(SKYLANDERS_BACKEND)
    add_subdirectory(src/backend)
endif()

# skylanders_portal_figures: only the figure files (catalog, names, elements, stats), for programs
# without the SDK such as the Core of Light launcher. On Windows it needs no SDK tree: AES and MD5
# come from Windows (CNG). Built whenever REXSDK_DIR isn't set.
set(SKYLANDERS_PORTAL_FIGURE_SOURCES
    src/portal/data_folder.cpp
    src/portal/figure_catalog.cpp
    src/portal/figure_crypto.cpp
    src/portal/figure_file.cpp
    src/portal/figure_info.cpp
    src/portal/figure_stats.cpp
    src/portal/trap_villain.cpp
)
if(NOT REXSDK_DIR)
    if(NOT WIN32)
        message(FATAL_ERROR "skylanders-portal: set REXSDK_DIR to a rexglue-sdk source tree")
    endif()
    add_library(skylanders_portal_figures STATIC ${SKYLANDERS_PORTAL_FIGURE_SOURCES})
    target_include_directories(skylanders_portal_figures PUBLIC src)
    target_compile_definitions(skylanders_portal_figures PRIVATE SKYLANDERS_PORTAL_CRYPTO_CNG)
    target_link_libraries(skylanders_portal_figures PRIVATE bcrypt shell32 ole32)
    target_compile_features(skylanders_portal_figures PUBLIC cxx_std_23)
    return()
endif()

add_library(skylanders_portal_core STATIC
    src/portal/audio.cpp
    src/portal/data_folder.cpp
    src/portal/figure_catalog.cpp
    src/portal/figure_crypto.cpp
    src/portal/figure_file.cpp
    src/portal/figure_info.cpp
    src/portal/figure_stats.cpp
    src/portal/g726.cpp
    src/portal/hybrid/real_portal_bridge.cpp
    src/portal/portal_mode.cpp
    src/portal/xbox_frame.cpp
    src/portal/software/software_portal.cpp
    src/portal/trap_villain.cpp
    src/portal/xam_bridge.cpp
    "${REXSDK_DIR}/thirdparty/aes_128/aes.c"
    "${REXSDK_DIR}/thirdparty/libmspack/cabextract/md5.c"
)
target_include_directories(skylanders_portal_core PUBLIC src)
target_include_directories(skylanders_portal_core PRIVATE
    "${REXSDK_DIR}/thirdparty/aes_128"
    "${REXSDK_DIR}/thirdparty/libmspack/cabextract")
target_compile_features(skylanders_portal_core PUBLIC cxx_std_23)
if(WIN32)
    # data_folder.cpp: the Documents and AppData known folders.
    target_link_libraries(skylanders_portal_core PUBLIC shell32 ole32)
endif()

option(SKYLANDERS_PORTAL_TESTS "Build skylanders-portal unit tests" ON)
if(SKYLANDERS_PORTAL_TESTS)
    enable_testing()
    foreach(t portal_framing_test portal_mode_test software_portal_test figure_file_test
              figure_catalog_test figure_stats_test audio_test xam_bridge_test
              trap_villain_test g726_test figure_info_test real_portal_bridge_test)
        add_executable(skylanders_${t} tests/${t}.cpp)
        target_include_directories(skylanders_${t} PRIVATE tests)
        target_link_libraries(skylanders_${t} PRIVATE skylanders_portal_core)
        add_test(NAME skylanders_${t} COMMAND skylanders_${t})
    endforeach()
    # The online session (skylanders_online), on the fake network and on loopback UDP.
    foreach(t netplay_datagram_test netplay_lanes_test netplay_port_mapping_test netplay_udp_socket_test
              online_protocol_test
              online_join_code_test online_tick_test online_session_test online_requests_test)
        add_executable(skylanders_${t} tests/${t}.cpp)
        target_include_directories(skylanders_${t} PRIVATE tests)
        target_link_libraries(skylanders_${t} PRIVATE skylanders_online)
        add_test(NAME skylanders_${t} COMMAND skylanders_${t})
    endforeach()
    # The navmesh planner (skylanders_nav), on hand-built meshes.
    foreach(t nav_planner_test)
        add_executable(skylanders_${t} tests/${t}.cpp)
        target_include_directories(skylanders_${t} PRIVATE tests)
        target_link_libraries(skylanders_${t} PRIVATE skylanders_nav)
        add_test(NAME skylanders_${t} COMMAND skylanders_${t})
    endforeach()
endif()

# skylanders_portal_rex: ReXGlue glue (settings, XAM handler, F6 overlay, USB portal, audio).
# Only built when the including project provides the rex::runtime target.
if(TARGET rex::runtime)
    if(NOT EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/thirdparty/hidapi/CMakeLists.txt")
        message(FATAL_ERROR "skylanders-portal: thirdparty/hidapi missing; run git submodule update --init --recursive")
    endif()
    set(HIDAPI_WITH_TESTS OFF CACHE BOOL "" FORCE)
    set(BUILD_SHARED_LIBS OFF)
    add_subdirectory(thirdparty/hidapi)

    add_library(skylanders_portal_rex STATIC
        src/portal_rex/portal_rex.cpp
        src/portal_rex/portal_overlay_dialog.cpp
        src/portal_rex/portal_menu.cpp
        src/portal_rex/portal_menu_input.cpp
        src/portal_rex/menu_draw.cpp
        src/portal_rex/achievements_ui.cpp
        src/portal_rex/discord_presence.cpp
        src/portal_rex/input_pictures.cpp
        src/portal_rex/player_menu.cpp
        src/portal_rex/settings_file.cpp
        src/portal_rex/settings_menu.cpp
        src/portal_rex/settings_schema.cpp
        src/portal_rex/title_music.cpp
        src/portal_rex/png_file.cpp
        src/portal_rex/problem_note.cpp
        src/portal/usb/usb_portal.cpp
        src/portal/usb/xbox360_portal.cpp
        src/portal_rex/sdl_audio_sink.cpp
        src/portal_rex/speaker_overlay.cpp
        # Test tools: scripted input, memory watches, the crash report, GPU crash diagnostics,
        # where the player is (for problem reports).
        src/dev/gpu_diagnostics.cpp
        src/dev/input_script.cpp
        src/dev/where.cpp
        src/dev/write_watch.cpp
    )
    target_include_directories(skylanders_portal_rex PUBLIC src)
    target_include_directories(skylanders_portal_rex PRIVATE "${REXSDK_DIR}/thirdparty/imgui")
    target_link_libraries(skylanders_portal_rex PUBLIC skylanders_portal_core rex::runtime hidapi::hidapi SDL3::SDL3)
    # The SDK's FFmpeg decodes the title music from the disc (title_music.cpp).
    target_link_libraries(skylanders_portal_rex PRIVATE libavcodec libavutil)
    if(WIN32)
        target_link_libraries(skylanders_portal_rex PRIVATE windowscodecs ole32)
    endif()
endif()
