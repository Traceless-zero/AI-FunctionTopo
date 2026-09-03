"""定价：订单总价计算。"""
from __future__ import annotations

from decimal import Decimal

from src.common.models import ValidatedCart, Money, User

def compute_total(cart: ValidatedCart, user: User) -> Money:
    """商品小计 + 税费 - 优惠；VIP 用户享额外折扣。"""
    subtotal = Decimal("0")
    for item in cart.items:
        subtotal += item.price * item.qty

    tax = (subtotal * Decimal("0.06")).quantize(Decimal("0.01"))
    discount = _coupon_discount(cart, subtotal)

    if user.is_vip:
        # VIP 额外 95 折，与优惠券可叠加
        discount += (subtotal * Decimal("0.05")).quantize(Decimal("0.01"))

    total = subtotal + tax - discount
    if total < Decimal("0"):
        # 优惠叠加不会出现负数，这里是防御性兜底
        total = Decimal("0")
    return Money(amount=total, currency="CNY")


def _coupon_discount(cart: ValidatedCart, subtotal: Decimal) -> Decimal:
    """优惠券折扣，占位实现。"""
    return Decimal("0")
