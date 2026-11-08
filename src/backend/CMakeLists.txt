# skylanders_backend: Swap Force Online's backend core (stage 2; the design is the game repository's
# docs/online-2.md): records with revisions under an epoch, one change log with a cursor per peer,
# baselines, obligations, leases and requests, over a transport contract. Game-independent: it
# includes no online/ header (D2), only netplay's, and netplay is compiled into skylanders_online,
# so it links that.
#
# Reached from the portal's CMakeLists.txt by one guarded line, if(SKYLANDERS_BACKEND), default off
# until stage 2 is the current stage (D1): a submodule bump during 1B pulls no new target in.

get_filename_component(SKYLANDERS_PORTAL_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)

# D2's include rule, at configure (the ctest backend.include_rule below runs it again each test run).
include("${CMAKE_CURRENT_SOURCE_DIR}/include_rule.cmake")

# Every part of the library is one .cpp in this folder (design section 3.1); stage 2's later items
# add theirs here, and CONFIGURE_DEPENDS picks them up at the next build.
file(GLOB SKYLANDERS_BACKEND_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/*.cpp")
add_library(skylanders_backend STATIC ${SKYLANDERS_BACKEND_SOURCES})
target_include_directories(skylanders_backend PUBLIC "${SKYLANDERS_PORTAL_ROOT}/src")
target_link_libraries(skylanders_backend PUBLIC skylanders_online)
target_compile_features(skylanders_backend PUBLIC cxx_std_23)

# The tests, under the same switch: one binary, skylanders_backend_test, one file per part
# (tests/backend_<part>_test.cpp, registered with BACKEND_TEST, tests/backend_test.h), and the
# include rule as its own ctest.
enable_testing()
# The parity suite's twin cells (tests/parity_*_test.cpp, design section 3.9) run 1B's request table
# beside the generic one, so they include online/ headers: test-only files outside tests/backend*,
# which the include rule covers (D2). 1B's table is in skylanders_online, which the backend links.
file(GLOB SKYLANDERS_BACKEND_TEST_SOURCES CONFIGURE_DEPENDS
    "${SKYLANDERS_PORTAL_ROOT}/tests/backend_*_test.cpp"
    "${SKYLANDERS_PORTAL_ROOT}/tests/parity_*_test.cpp")
add_executable(skylanders_backend_test
    "${SKYLANDERS_PORTAL_ROOT}/tests/backend_main.cpp"
    ${SKYLANDERS_BACKEND_TEST_SOURCES})
target_include_directories(skylanders_backend_test PRIVATE "${SKYLANDERS_PORTAL_ROOT}/tests")
target_link_libraries(skylanders_backend_test PRIVATE skylanders_backend)
# The golden files (MD1, CQ1 to CQ5): compared from the source tree, rewritten only by
# --update-golden.
target_compile_definitions(skylanders_backend_test PRIVATE
    SKYLANDERS_BACKEND_GOLDEN_DIR="${SKYLANDERS_PORTAL_ROOT}/tests/backend/golden")
add_test(NAME skylanders_backend_test COMMAND skylanders_backend_test)
add_test(NAME backend.include_rule
    COMMAND "${CMAKE_COMMAND}" "-DSKYLANDERS_PORTAL_ROOT=${SKYLANDERS_PORTAL_ROOT}"
            -P "${CMAKE_CURRENT_SOURCE_DIR}/include_rule.cmake")
