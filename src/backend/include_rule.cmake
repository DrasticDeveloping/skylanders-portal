# The backend's include rule (design D2; Opus F13, Opus r2 N5): no file under src/backend or
# tests/backend* includes an online/ header. The backend is game-independent; netplay's headers are
# allowed, the session library's are 1B's and the game's. Nor does one include a tests/parity_* file
# (the parity suite's twin, which includes online/ headers to run 1B's table beside the generic one).
#
# Two uses, one script:
# - at configure: src/backend/CMakeLists.txt includes it, so a broken rule fails the configure;
# - as the ctest backend.include_rule: cmake -DSKYLANDERS_PORTAL_ROOT=<portal> -P include_rule.cmake,
#   which globs the files again when it runs, so a file or an include added after the last
#   configure fails the next test run with no reconfigure.
#
# SKYLANDERS_PORTAL_ROOT is the portal's root (the folder holding src/ and tests/).

if(NOT SKYLANDERS_PORTAL_ROOT)
    message(FATAL_ERROR "backend include rule: SKYLANDERS_PORTAL_ROOT is not set")
endif()

file(GLOB_RECURSE _backend_rule_files LIST_DIRECTORIES false
    "${SKYLANDERS_PORTAL_ROOT}/src/backend/*.h"
    "${SKYLANDERS_PORTAL_ROOT}/src/backend/*.hpp"
    "${SKYLANDERS_PORTAL_ROOT}/src/backend/*.cpp"
    "${SKYLANDERS_PORTAL_ROOT}/src/backend/*.inl")
# tests/backend* : the files beside the other tests, and anything in a tests/backend folder.
file(GLOB _backend_rule_tests LIST_DIRECTORIES false
    "${SKYLANDERS_PORTAL_ROOT}/tests/backend*.h"
    "${SKYLANDERS_PORTAL_ROOT}/tests/backend*.hpp"
    "${SKYLANDERS_PORTAL_ROOT}/tests/backend*.cpp"
    "${SKYLANDERS_PORTAL_ROOT}/tests/backend*.inl")
file(GLOB_RECURSE _backend_rule_test_dir LIST_DIRECTORIES false
    "${SKYLANDERS_PORTAL_ROOT}/tests/backend/*.h"
    "${SKYLANDERS_PORTAL_ROOT}/tests/backend/*.hpp"
    "${SKYLANDERS_PORTAL_ROOT}/tests/backend/*.cpp"
    "${SKYLANDERS_PORTAL_ROOT}/tests/backend/*.inl")
list(APPEND _backend_rule_files ${_backend_rule_tests} ${_backend_rule_test_dir})
list(REMOVE_DUPLICATES _backend_rule_files)

# An include directive naming online/ (also through ../ or src/online/), with either bracket, or a
# parity_ test file (tests/parity_*: the parity suite's twin, which includes online/ headers, so it
# may not be reached from here either).
set(_backend_rule_regex "^[ \t]*#[ \t]*include[ \t]*[<\"](\\.\\./)*((src/)?online/|(tests/)?parity_)")

set(_backend_rule_broken "")
foreach(_file IN LISTS _backend_rule_files)
    file(STRINGS "${_file}" _hits REGEX "${_backend_rule_regex}")
    foreach(_hit IN LISTS _hits)
        file(RELATIVE_PATH _rel "${SKYLANDERS_PORTAL_ROOT}" "${_file}")
        string(STRIP "${_hit}" _hit)
        list(APPEND _backend_rule_broken "${_rel}: ${_hit}")
    endforeach()
endforeach()

list(LENGTH _backend_rule_files _backend_rule_count)
if(_backend_rule_broken)
    list(JOIN _backend_rule_broken "\n  " _backend_rule_lines)
    message(FATAL_ERROR
        "backend include rule broken (D2: src/backend and tests/backend* include no online/ header and no parity_ file):\n"
        "  ${_backend_rule_lines}")
elseif(CMAKE_SCRIPT_MODE_FILE)
    message(STATUS "backend include rule: ${_backend_rule_count} files, no online/ or parity_ include")
endif()
