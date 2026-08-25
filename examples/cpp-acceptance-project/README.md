# ForgeMCP C++ acceptance project

This is a small, dependency-free project used only by ForgeMCP acceptance tests.
It is deliberately portable across MSVC, Clang, and GCC and requires CMake 3.23
or newer.  Never use it as a real user project and do not commit generated build
trees, `compile_commands.json`, binaries, PDBs, or clangd caches.

The normal path is `cmake --preset ninja-debug`, followed by
`cmake --build --preset build-ninja-debug` and `ctest --preset test-ninja-debug`.
In ForgeMCP, select a discovered kit with `cmake__list_kits` /
`cmake__select_kit`, then configure a disposable workspace copy.  A preset and
a ForgeMCP kit are intentionally alternative workflows and must not be mixed.

Successful targets are `fixture_core`, `fixture_good`, `fixture_warning`, and
`fixture_debug`. `fixture_warning` intentionally reports a deprecation warning.
`fixture_compile_error` and `fixture_link_error` are `EXCLUDE_FROM_ALL` and are
expected to fail when built. The default tests are `fixture_pass` and the
`WILL_FAIL` `fixture_expected_failure`. Configure with
`FIXTURE_ENABLE_NEGATIVE_TESTS=ON` to register the exact-name failing and
timeout test scenarios.

The `analysis` files exist for real clangd, clang-format, and clang-tidy
scenarios. `shared.hpp` is a deliberately closed-header
cross-file-rename anchor; `analysis/clangd_anchors.cpp` provides deterministic
completion, signature, call-hierarchy, and type-hierarchy anchors.  They are
compiled only into the generated compilation database through an
`EXCLUDE_FROM_ALL` target. `reports` contains synthetic sanitizer text only;
it contains no host paths or secrets.
