# src.example_math

A minimal module providing basic integer arithmetic: subtraction and multiplication.

## Quick start
    # install (if published to PyPI)
    pip install src.example_math

    # import and use
    from src.example_math import sub, multiply

    result1 = sub(10, 4)      # result1 == 6
    result2 = multiply(3, 5)  # result2 == 15

## API overview

sub(a: int, b: int) → int  
Subtract two integers.  
Parameters  
• a – minuend  
• b – subtrahend  
Returns  
The difference (a − b).

multiply(a: int, b: int) → int  
Multiply two integers.  
Parameters  
• a – first factor  
• b – second factor  
Returns  
The product (a × b).