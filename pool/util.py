import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal as D
from typing import Dict, List, Mapping, Optional
from urllib.parse import urlparse

from chia.protocols.pool_protocol import PoolErrorCode, ErrorResponse
from chia.util.ints import uint16
from chia.util.json_util import obj_to_response
from chia.wallet.util.tx_config import DEFAULT_TX_CONFIG
from packaging.version import Version

logger = logging.getLogger('util')


def error_response(code: PoolErrorCode, message: str):
    error: ErrorResponse = ErrorResponse(uint16(code.value), message)
    return obj_to_response(error)


def error_dict(code: PoolErrorCode, message: str):
    error: ErrorResponse = ErrorResponse(uint16(code.value), message)
    return error.to_json_dict()


@dataclass
class RequestMetadata:
    """
    HTTP-related metadata passed with HTTP requests
    """
    url: str  # original request url, as used by the client
    scheme: str  # for example https
    headers: Mapping[str, str]  # header names are all lower case
    cookies: Dict[str, str]
    query: Dict[str, str]  # query params passed in the url. These are not used by chia clients at the moment, but
    # allow for a lot of adjustments and thanks to including them now they can be used without introducing breaking changes
    remote: str  # address of the client making the request

    def __post_init__(self):
        self.headers = {k.lower(): v for k, v in self.headers.items()}

    def get_chia_version(self) -> Optional[str]:
        user_agent = self.headers.get('user-agent')
        if not user_agent:
            return

        if user_agent.startswith('Chia Blockchain v.'):
            try:
                return Version('.'.join(user_agent.split('Chia Blockchain v.', 1)[-1].split('-')[0].split('.', 3)[:3]))
            except Exception as e:
                logger.error('Failed to parse chia version %r: %r', user_agent, e)
                return

    def get_host(self) -> Optional[str]:
        try:
            forwarded = self.headers.get('x-forwarded-host')
            if forwarded:
                return forwarded
            parse = urlparse(self.url)
            return parse.hostname
        except ValueError:
            return None

    def get_remote(self) -> Optional[str]:
        if self.remote:
            return self.remote.split(',', 1)[0] or None

    def to_json_dict(self):
        return asdict(self)

    @classmethod
    def from_json_dict(cls, data):
        return cls(**data)


def payment_targets_to_additions(
        payment_targets: Dict, min_payment, launcher_min_payment: bool = True,
        limit: Optional[int] = None,
) -> List:
    additions = []
    for ph, payment in list(payment_targets.items()):

        if limit and len(additions) >= limit:
            payment_targets.pop(ph)
            continue

        amount = 0
        min_pay = min_payment
        for i in payment:
            amount += i['amount']
            if launcher_min_payment:
                launcher_min_pay = i.get('min_payout', None) or 0
                if launcher_min_pay > min_pay:
                    min_pay = launcher_min_pay

        if amount >= min_pay:
            additions.append({'puzzle_hash': ph, 'amount': amount})
        else:
            payment_targets.pop(ph)
    return additions


def check_transaction(transaction, wallet_ph):

    # We expect all non spent reward coins to be used in the transaction.
    # The goal is to only use coins assigned to a payout.
    # All other coins should be leftover (change) of previous payouts.
    # Coins in the wallet first address puzzle hash are reward coins.
    puzzle_hash_coins = set()
    non_puzzle_hash_coins = set()
    for coin in transaction.spend_bundle.removals():
        if coin.puzzle_hash == wallet_ph:
            puzzle_hash_coins.add(coin)
        else:
            non_puzzle_hash_coins.add(coin)

    return puzzle_hash_coins, non_puzzle_hash_coins


async def create_transaction(
    node_rpc_client,
    wallet,
    store,
    additions,
    fee,
    payment_targets,
):

    if wallet.get('use_reward_coin', True) is False:
        transaction = await wallet['rpc_client'].create_signed_transaction(
            additions, tx_config=DEFAULT_TX_CONFIG, fee=fee
        )
        return transaction

    # Lets get all coins rewards that are associated with the payouts in this round
    payout_ids = set()
    for targets in payment_targets.values():
        for t in targets:
            payout_ids.add(t['payout_id'])
    coin_rewards_names = await store.get_coin_rewards_from_payout_ids(
        payout_ids
    )

    coin_records = await node_rpc_client.get_coin_records_by_names(
        coin_rewards_names,
        include_spent_coins=True,
    )
    # Make sure to filter the not spent coins.
    # Coin rewards can be spent if they were part of a previous payment (min payment).
    unspent_coins = {cr.coin for cr in filter(lambda x: not x.spent, coin_records)}

    # If no reward coins are spent we can use them as sole source coins for the transaction
    # If there is a fee we will need additional coin. (FIXME)
    if len(coin_records) == len(unspent_coins) and fee == 0:
        transaction = await wallet['rpc_client'].create_signed_transaction(
            additions, tx_config=DEFAULT_TX_CONFIG, coins=list(unspent_coins), fee=fee
        )
        return transaction

    # If a coin was spent we give a shot for the Wallet automatically select the required coins
    transaction = await wallet['rpc_client'].create_signed_transaction(
        additions, tx_config=DEFAULT_TX_CONFIG, fee=fee,
    )

    ph_coins, non_ph_coins = check_transaction(transaction, wallet['puzzle_hash'])
    # If there are more coins in wallet puzzle hash than from unspent coin for the payouts
    # we try once again using only the unspent reward coins and the coins outside wallet puzzle hash.
    if ph_coins - unspent_coins:
        logger.info('Redoing transaction to only include reward coins')

        total_additions = sum(a['amount'] for a in additions)
        total_coins = sum(int(c.amount) for c in list(unspent_coins) + list(non_ph_coins))
        if total_additions + fee <= total_coins:
            transaction = await wallet['rpc_client'].create_signed_transaction(
                additions, tx_config=DEFAULT_TX_CONFIG, coins=list(unspent_coins) + list(non_ph_coins), fee=fee
            )
        else:
            # We are short of coins to make the payment
            logger.info('Getting extra non ph coins')

            balance = await wallet['rpc_client'].get_wallet_balance(wallet['id'])
            amount_missing = total_additions - total_coins
            for coin in await wallet['rpc_client'].select_coins(
                amount=balance['spendable_balance'],
                coin_selection_config=DEFAULT_TX_CONFIG.coin_selection_config,
                wallet_id=wallet['id'],
            ):
                if coin.puzzle_hash == wallet['puzzle_hash']:
                    continue
                if coin not in non_ph_coins:
                    amount_missing -= int(coin.amount)
                    non_ph_coins.add(coin)
                    if amount_missing <= 0:
                        break
            else:
                raise RuntimeError('Not enough non puzzle hash coins for payment')
            transaction = await wallet['rpc_client'].create_signed_transaction(
                additions,
                tx_config=DEFAULT_TX_CONFIG,
                coins=list(unspent_coins) + list(non_ph_coins),
                fee=fee,
            )
    return transaction


def days_pooling(
    joined_at: Optional[datetime], left_at: Optional[datetime], is_pool_member: bool,
) -> int:
    if not is_pool_member:
        return 0

    if not joined_at:
        joined_at = datetime(2021, 8, 9, tzinfo=timezone.utc)

    # Means has joined again
    if left_at and joined_at > left_at:
        left_at = None

    if not left_at:
        left_at = datetime.now(timezone.utc)

    if left_at < joined_at:
        return 0

    return (left_at - joined_at).days


def stay_fee_discount(stay_fee_discount: float, stay_fee_length: int, days_passed: int) -> D:
    if days_passed <= 0 or stay_fee_length <= 0 or stay_fee_discount <= 0:
        return D('0')

    # fee discount increases every week, not every day
    days_passed = D((days_passed // 7) * 7)

    passed_pct = min(days_passed / D(stay_fee_length), D('1'))

    return passed_pct * D(stay_fee_discount)


def size_discount(launcher_size: int, size_discount: Dict) -> D:
    launcher_size_tb = launcher_size / 1024 ** 4
    for size_tb, discount in reversed(sorted(size_discount.items())):
        if launcher_size_tb >= size_tb:
            return D(discount)
    else:
        return D('0')


def calculate_effort(
    last_etw: int,
    last_timestamp: int,
    now_etw: int,
    now_timestamp: int,
) -> float:

    # Effective ETW is the mean between last ETW and current ETW
    if last_etw != -1:
        effective_etw = (last_etw + now_etw) / 2
    else:
        effective_etw = now_etw

    time_since_last = now_timestamp - last_timestamp
    # If time is negative means we are adding a block that was won some time ago
    # e.g. farmer that wasn't sending partials to the pool
    if time_since_last < 0:
        effort = 0.0
    else:
        effort = (time_since_last / effective_etw) * 100

    return effort