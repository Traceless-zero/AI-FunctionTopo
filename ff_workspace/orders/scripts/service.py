"""订单服务：用户加载、购物车校验、订单创建。"""
from __future__ import annotations

from src.orders.pricing import compute_total
from src.orders.notify import send_confirmation
from src.common.models import User, Cart, Order, Money
from src.common import db




def load_user(user_id: int) -> User:
    """按 user_id 加载用户，不存在时抛 UserNotFound。"""
    user = db.query(User).filter_by(id=user_id).first()
    if user is None:
        raise UserNotFound("user %d not found" % user_id)
    return user


# --- 购物车校验 ---


class CartInvalid(Exception):
    """购物车校验失败。"""



def validate_cart(cart: Cart) -> ValidatedCart:
    """校验库存、价格与上下架状态，返回校验后快照。"""
    snapshot = []
    for item in cart.items:
        sku = inventory.get(item.sku_id)
        if sku is None or not sku.on_sale:
            raise CartInvalid("sku %s 已下架" % item.sku_id)
        if sku.stock < item.qty:
            raise CartInvalid("sku %s 库存不足" % item.sku_id)
        if sku.price != item.price:
            raise CartInvalid("sku %s 价格已变动" % item.sku_id)
        snapshot.append(ValidatedItem.from_cart_item(item, sku))
    if not snapshot:
        raise CartInvalid("购物车为空")
    return ValidatedCart(user_id=cart.user_id, items=snapshot)


# --- 订单创建 ---


class UserNotFound(Exception):
    """用户不存在。"""


def _order_no() -> str:
    """生成全局唯一订单号。"""
    return "ORD%016d" % time.time_ns()





def create_order(user: User, cart: ValidatedCart, total: Money) -> Order:
    """落库创建订单、扣库存、生成支付单，事务内执行。"""
    with session_scope() as tx:
        order = Order(
            no=_order_no(),
            user_id=user.id,
            items=cart.items,
            total=total,
            status=OrderStatus.PENDING_PAYMENT,
        )
        tx.add(order)

        for item in cart.items:
            affected = tx.execute(
                inventory.deduct(item.sku_id, item.qty)
            )
            if affected != 1:
                raise InventoryConflict(item.sku_id)

        payment = Payment(
            order_id=order.id,
            amount=total,
            channel=user.default_channel,
        )
        tx.add(payment)

    # 事务提交成功后异步通知（失败自动重试）
    send_confirmation.delay(order.id)
    return order


class InventoryConflict(Exception):
    """库存扣减冲突，事务整体回滚。"""
