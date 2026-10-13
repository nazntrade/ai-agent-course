# Quotation example contract

The isolated `examples/review_demo` package is a teaching fixture for code review.
It is not imported by the assistant runtime. A demonstration PR can change it
without breaking the working application; erroneous demo changes must not merge.

`pricing.quote(amount, discount_percent)` returns a Python dictionary with the
required field `total`. Discounts are percentages between 0 and 100.
Total is `amount * (1 - discount_percent / 100)`, rounded to two decimals.
For amount 200 and discount 10 the total is 180; amount 100 and discount 0 gives 100.
Negative amounts or discounts outside 0–100 raise `ValueError`.
`checkout.checkout_total` consumes the `total` field. Renaming that field requires
changing the caller and documenting the API change; otherwise checkout fails.

Review should compare the changed function to this documentation and related
caller. A valid code refactor preserving these outcomes should not be called a bug.
