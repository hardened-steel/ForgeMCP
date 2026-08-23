#include "fixture/math.hpp"

int debug_leaf(int input) {
    int local_value = fixture::add(input, 2);
    // FIXTURE_BREAKPOINT_MARKER
    return local_value;
}

int debug_middle(int input) {
    int middle_value = debug_leaf(input);
    return middle_value;
}

int main() {
    int main_value = debug_middle(40);
    return main_value == 42 ? 0 : 1;
}
