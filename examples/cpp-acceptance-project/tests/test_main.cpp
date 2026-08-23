#include "fixture/math.hpp"

#include <chrono>
#include <string_view>
#include <thread>

int main(int argc, char** argv) {
    const std::string_view mode = argc > 1 ? argv[1] : "";
    if (mode == "--expected-failure" || mode == "--fail") {
        return 1;
    }
    if (mode == "--timeout") {
        std::this_thread::sleep_for(std::chrono::seconds(5));
        return 0;
    }
    return fixture::add(20, 22) == 42 ? 0 : 1;
}
