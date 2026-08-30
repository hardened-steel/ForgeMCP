#include "fixture/math.hpp"

#include <cstddef>

int tidy_me() {
    int* value = NULL;
    return value == NULL ? fixture::add(40, 2) : 0;
}
