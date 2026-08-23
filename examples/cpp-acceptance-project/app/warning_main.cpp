#include "fixture/math.hpp"

int main() {
    return fixture::deprecated_increment(41) == 42 ? 0 : 1;
}
