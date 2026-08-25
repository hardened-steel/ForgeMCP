cmake_minimum_required(VERSION 3.23)

project(forgemcp_cpp_acceptance LANGUAGES CXX)

option(FIXTURE_ENABLE_NEGATIVE_TESTS "Register intentionally failing CTest cases" OFF)
option(FIXTURE_LLVM_DWARF "Build fixture_debug with Clang DWARF debug information" OFF)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_library(fixture_core STATIC
    src/math.cpp
    src/hierarchy.cpp
)
target_include_directories(fixture_core PUBLIC include)

add_executable(fixture_good app/good_main.cpp)
target_link_libraries(fixture_good PRIVATE fixture_core)
target_include_directories(fixture_good PRIVATE .)

add_executable(fixture_warning app/warning_main.cpp)
target_link_libraries(fixture_warning PRIVATE fixture_core)
if(MSVC)
    target_compile_options(fixture_warning PRIVATE /W4)
else()
    target_compile_options(fixture_warning PRIVATE -Wall -Wextra -Wpedantic)
endif()

add_executable(fixture_compile_error EXCLUDE_FROM_ALL negative/compile_error.cpp)

add_executable(fixture_link_error EXCLUDE_FROM_ALL negative/link_error.cpp)
target_link_libraries(fixture_link_error PRIVATE fixture_core)

add_executable(fixture_debug debug/debug_main.cpp)
target_link_libraries(fixture_debug PRIVATE fixture_core)
if(MSVC)
    target_compile_options(fixture_debug PRIVATE /Od)
else()
    target_compile_options(fixture_debug PRIVATE -O0)
endif()
if(FIXTURE_LLVM_DWARF)
    if(NOT CMAKE_CXX_COMPILER_ID MATCHES "Clang")
        message(FATAL_ERROR "FIXTURE_LLVM_DWARF requires a qualified Clang kit.")
    endif()
    target_compile_options(fixture_debug PRIVATE -g -Xclang -gdwarf)
endif()

add_executable(fixture_tests tests/test_main.cpp)
target_link_libraries(fixture_tests PRIVATE fixture_core)

# This target intentionally never participates in the default build.  CMake
# still emits its entries to compile_commands.json, allowing real clangd tests
# to exercise malformed completion/code-action anchors without making the
# portable configure/build gate fail.
add_library(fixture_clangd_anchors OBJECT EXCLUDE_FROM_ALL
    analysis/clangd_anchors.cpp
    analysis/code_action.cpp
    analysis/format_me.cpp
)
target_include_directories(fixture_clangd_anchors PRIVATE include)

enable_testing()
add_test(NAME fixture_pass COMMAND fixture_tests)
add_test(NAME fixture_expected_failure COMMAND fixture_tests --expected-failure)
set_tests_properties(fixture_expected_failure PROPERTIES WILL_FAIL TRUE)

# These tests are intentionally absent from the normal CTest inventory.  A
# dedicated negative configuration adds them so a normal acceptance run has a
# deterministic passing set while exact-name negative runs remain possible.
if(FIXTURE_ENABLE_NEGATIVE_TESTS)
    add_test(NAME fixture_intentional_failure COMMAND fixture_tests --fail)
    add_test(NAME fixture_timeout COMMAND fixture_tests --timeout)
    set_tests_properties(fixture_timeout PROPERTIES TIMEOUT 1)
endif()
