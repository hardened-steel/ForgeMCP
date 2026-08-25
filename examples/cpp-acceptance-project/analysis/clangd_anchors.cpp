#include "fixture/hierarchy.hpp"
#include "fixture/math.hpp"

int call_target(int value) {
    return fixture::add(value, 1);
}

int call_source() {
    return call_target(41);
}

int completion_anchor() {
    return fixture::add(0, 0);
}

int signature_anchor() {
    return fixture::add(1, 2);
}

fixture::Dog type_anchor() {
    return {};
}
