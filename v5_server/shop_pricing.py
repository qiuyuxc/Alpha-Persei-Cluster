"""Authoritative ShopTools prices, including the secondary payment option."""
import datetime
import json

from client_rule_loader import load_rules
from core import OperationError


def _timestamp(value):
    if value in (None, '', '{}', '[]', 0) or value == [] or value == {}:
        return None
    try:
        if isinstance(value, str):
            if value.lstrip().startswith(('[', '{')):
                return _timestamp(json.loads(value))
            return int(datetime.datetime.fromisoformat(value).timestamp())
        if isinstance(value, (int, float)):
            return int(value)
        date, clock = value
        return int(datetime.datetime(*date, *clock).timestamp())
    except (ValueError, TypeError) as exc:
        raise OperationError(406, '商店折扣时间配置无效') from exc


def _window(open_time, close_time, now):
    start, end = _timestamp(open_time), _timestamp(close_time)
    return (start is None or start <= now) and (end is None or now < end)


def quote(goods, detail, shop_id, buy_type, quantity, now):
    """Return (payment currency, total price, cost type) from server configuration."""
    if buy_type not in (0, 1) or quantity <= 0:
        raise OperationError(2, '购买类型或数量无效')
    cfg = {**detail, **{key: value for key, value in goods.items() if value is not None}}
    cost_type = int(cfg.get('cost_type') or 0)
    if cost_type == 1:
        raise OperationError(406, '该商品需通过充值订单购买')
    # ShopAction bytecode selects cost_id_2 / the fourth GetPrice result for type 1.
    suffix = '_2' if buy_type else ''
    cid = int(cfg.get('cost_id' + suffix) or 0)
    if buy_type and cid <= 0:
        raise OperationError(2, '该商品未配置第二种支付方式')
    original = int(cfg.get('cost' + suffix) or 0)
    cheap = int(cfg.get('cheap_cost' + suffix) or 0)
    discount = int(cfg.get('discount') or 0)
    if original < 0 or cheap < 0 or not 0 <= discount <= 100:
        raise OperationError(406, '商品价格配置无效')
    if cheap and 0 < discount < 100:
        original = (cheap * 100 + discount - 1) // discount
    opening, closing = cfg.get('cheap_open_time' + suffix), cfg.get('cheap_close_time' + suffix)
    product_active = bool(cheap and _window(opening, closing, now) and
                          (0 < discount < 100 or int(cfg.get('is_limit_time_discount' + suffix) or 0) == 1))
    price = cheap if product_active else original
    if product_active:
        cid = int(cfg.get('cheap_cost_id' + suffix) or cid)

    shop = load_rules('shop_price_rules.json')['shops'].get(str(shop_id), {})
    shop_discount = int(shop.get('discount') or 0)
    shop_active = 0 < shop_discount < 100 and _window(shop.get('cheap_open_time'), shop.get('cheap_close_time'), now)
    if int(shop.get('is_limit_buy_discount') or 0) == 0 and int(cfg.get('limit_num') or 0) > 0:
        shop_active = False
    currency_limit = shop.get('cheap_limit_currency') or []
    if currency_limit:
        mode, currencies = currency_limit
        if (mode == 1 and cid not in currencies) or (mode == 2 and cid in currencies):
            shop_active = False
    if shop_active:
        discounted = original * shop_discount // 100
        price = min(price, discounted)
    if price and cid <= 0:
        raise OperationError(406, '商品支付货币配置缺失')
    return cid, price * quantity, cost_type


def declared_costs(entries):
    """Aggregate repeated currency entries before comparing the declared total."""
    totals = {}
    if not isinstance(entries, (list, tuple)):
        raise OperationError(2, '购买费用列表格式无效')
    for entry in entries:
        try:
            cid, amount = int(entry['id']), int(entry['num'])
        except (KeyError, TypeError, ValueError) as exc:
            raise OperationError(2, '购买费用格式无效') from exc
        if cid <= 0 or amount < 0:
            raise OperationError(2, '购买费用编号或数量无效')
        totals[cid] = totals.get(cid, 0) + amount
    return totals
