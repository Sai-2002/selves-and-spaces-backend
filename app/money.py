from decimal import Decimal


def money(value) -> Decimal:
    """A DB numeric(12,2) as an exact 2-place Decimal.

    PostgREST sends numerics as JSON numbers (100.10 arrives as 100.1), so go via str to avoid
    float artefacts and re-apply the scale so amounts always render as "100.10".
    """
    return Decimal(str(value)).quantize(Decimal("0.01"))
