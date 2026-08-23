#include "fixture/math.hpp"

namespace fixture {

int add(const int left, const int right) {
    return left + right;
}

int deprecated_increment(const int value) {
    return value + 1;
}

}  // namespace fixture
