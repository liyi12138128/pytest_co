import pytest
from src.example_math import sub, multiply

@pytest.mark.parametrize("a,b,expected", [
    (5, 3, 2),
    (3, 5, -2),
    (0, 0, 0),
    (10**20, 10**19, 10**20 - 10**19),
])
def test_sub_typical_and_boundary(a, b, expected):
    assert sub(a, b) == expected

def test_sub_anti_commutative():
    a, b = 7, 2
    assert sub(a, b) == -sub(b, a)

@pytest.mark.parametrize("a,b,expected", [
    (5, 3, 15),
    (0, 5, 0),
    (-2, 3, -6),
    (2**100, 2**50, 2**150),
])
def test_multiply_typical_and_boundary(a, b, expected):
    assert multiply(a, b) == expected

def test_multiply_commutative():
    a, b = 8, 9
    assert multiply(a, b) == multiply(b, a)

@pytest.mark.parametrize("func,args", [
    (sub, ("a", 1)),
    (sub, (1, "b")),
    (multiply, ("x", 2)),
    (multiply, (2, "y")),
])
def test_type_errors_for_incompatible_types(func, args):
    with pytest.raises(TypeError):
        func(*args)