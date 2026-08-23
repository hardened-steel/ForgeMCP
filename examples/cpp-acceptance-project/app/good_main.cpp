#include "fixture/hierarchy.hpp"
#include "fixture/math.hpp"

int main() {
    return fixture::add(20, 22) == 42 && fixture::global_dog().name() == "dog" ? 0 : 1;
}
