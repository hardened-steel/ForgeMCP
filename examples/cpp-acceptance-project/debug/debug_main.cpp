#include "fixture/math.hpp"

#include <chrono>
#include <string_view>
#include <thread>

int debug_leaf(int input) {
    int local_value = fixture::add(input, 2);
    // FIXTURE_BREAKPOINT_MARKER
    return local_value;
}

int debug_middle(int input) {
    int middle_value = debug_leaf(input);
    return middle_value;
}

int debug_bounded_running() {
    // A deliberately bounded RUNNING anchor for pause/stop fixture sessions.
    // The test stops it through the managed adapter; if that fails it still
    // terminates normally without network or child-process dependencies.
    volatile int running_value = 0;
    for (int iteration = 0; iteration != 500; ++iteration) {
        running_value += iteration;
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return running_value == -1 ? 1 : 0;
}

int main(int argc, char** argv) {
    if (argc == 2 && std::string_view(argv[1]) == "bounded-running") {
        return debug_bounded_running();
    }
    int seed = 40;
    // FIXTURE_STEP_OVER_MARKER
    seed += 0;
    // FIXTURE_STEP_IN_MARKER
    int main_value = debug_middle(seed);
    return main_value == 42 ? 0 : 1;
}
