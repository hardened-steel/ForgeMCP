#include "fixture/hierarchy.hpp"
#include "fixture/math.hpp"
#include "shared.hpp"

int main() {
    return fixture::add(20, 22) == shared_value
        && fixture::global_dog().name() == "dog" ? 0 : 1;
}
