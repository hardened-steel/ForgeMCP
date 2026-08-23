#include "fixture/hierarchy.hpp"

namespace fixture {

std::string_view Animal::name() const {
    return "animal";
}

std::string_view Dog::name() const {
    return "dog";
}

Animal& global_dog() {
    static Dog dog;
    return dog;
}

}  // namespace fixture
