#pragma once

#include <string_view>

namespace fixture {

class Animal {
public:
    virtual ~Animal() = default;
    virtual std::string_view name() const;
};

class Dog final : public Animal {
public:
    std::string_view name() const override;
};

Animal& global_dog();

}  // namespace fixture
