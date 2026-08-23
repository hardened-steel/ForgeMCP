#pragma once

namespace fixture {

[[deprecated("fixture warning")]] int deprecated_increment(int value);
int add(int left, int right);

}  // namespace fixture
