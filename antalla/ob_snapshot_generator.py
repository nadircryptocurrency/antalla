from datetime import datetime
from datetime import timedelta
from collections import defaultdict

import numpy as np
import logging

from .db import session
from . import models
from . import actions

SNAPSHOT_INTERVAL_SECONDS = 1
DEFAULT_COMMIT_INTERVAL = 100

class OBSnapshotGenerator:
    def __init__(self, exchanges, timestamp, commit_interval=DEFAULT_COMMIT_INTERVAL, snapshot_interval=SNAPSHOT_INTERVAL_SECONDS):
        self.exchanges = exchanges
        self.stop_time = timestamp
        self.commit_interval = commit_interval
        self.snapshot_interval = snapshot_interval
        self.actions_buffer = []
        self.commit_counter = 0    

    def run(self):
        exchange_markets = self._query_exchange_markets()
        parsed_exchange_markets = self._parse_exchange_markets(exchange_markets) 
        for exchange in parsed_exchange_markets:
            for market in parsed_exchange_markets[exchange]:
                logging.debug("order book snapshot - {} - '{}-{}' - start time: {}".format(exchange.upper(), market["buy_sym_id"], market["sell_sym_id"], market["timestamp"]))
                while start_time < self.stop_time:
                    logging.debug("start: {}, end: {}".format(start_time, self.stop_time))
                    stop_time = datetime.strftime(start_time, '%Y-%m-%d %H:%M:%S.%f')
                    order_books = self._query_order_books(exchange, market["buy_sym_id"], market["sell_sym_id"], stop_time)
                    full_ob, quartile_ob = self._parse_order_books(order_books)
                    if full_ob is None:
                        start_time += timedelta(seconds=self.snapshot_interval)
                        continue
                    metadata = dict(timestamp=start_time, exchange_id=market["exchange_id"], buy_sym_id=market["buy_sym_id"], sell_sym_id=market["sell_sym_id"])
                    snapshot = self._generate_snapshot(full_ob, quartile_ob, metadata)
                    action = actions.InsertAction([snapshot])
                    action.execute(session)
                    if len(self.actions_buffer) >= self.commit_interval:
                        session.commit()
                        self.commit_counter += 1
                        logging.debug("order book snapshot commit[{}]".format(self.commit_counter))
                    else:
                        self.actions_buffer.append(action)
                    start_time += timedelta(seconds=self.snapshot_interval)
                    logging.debug("new order book snapshot created - {}".format(start_time))
        if len(self.actions_buffer) > 0:
            session.commit()
        logging.debug("completed order book snapshots - total commits: {}".format(self.commit_counter))

    def _query_exchange_markets(self):
        query = (
            """
            select * from (
                  select events.buy_sym_id,
                         events.sell_sym_id,
                         exchanges.name,
                         min(events.timestamp),
                         exchanges.id,
                         events.data_collected
                  from events
                           inner join exchanges on events.exchange_id = exchanges.id
                  group by events.buy_sym_id, events.sell_sym_id, exchanges.name, exchanges.id, events.data_collected
            ) pairs where pairs.data_collected = 'agg_order_book'
            """
        )
        return session.execute(query)

    # TODO: add test!
    def _parse_exchange_markets(self, exchange_markets):
        exchange_markets = list(exchange_markets)
        if len(exchange_markets) == 0:
            return None
        all_markets = defaultdict(list)
        for market in exchange_markets:
            #all_markets[market[2]].append((market[0], market[1], market[2], market[3], market[4]))
            all_markets[market[2]].append(dict(buy_sym_id=market[0], sell_sym_id=market[1], exchange=market[2], timestamp=market[3], exchange_id=market[4]))
        return all_markets

    def _query_order_books(self, exchange, buy_sym_id, sell_sym_id, timestamp):
        query = (
            """
            with order_book as (
            with latest_orders as (
            select order_type, price, max(last_update_id) max_update_id
            from aggregate_orders
            where timestamp <= :timestamp
            group by aggregate_orders.price,
                        aggregate_orders.order_type)
            select aggregate_orders.id,
                order_type,
                price,
                size,
                last_update_id,
                timestamp,
                name,
                buy_sym_id,
                sell_sym_id
            from aggregate_orders
                    inner join exchanges on aggregate_orders.exchange_id = exchanges.id
            where (order_type, price, last_update_id) in (select * from latest_orders)
            and size > 0
            and buy_sym_id = :buy_sym_id
            and sell_sym_id = :sell_sym_id
            and name = :exchange
            and timestamp <= :timestamp
            )
            select *, true as is_quartile
            from order_book where (order_book.order_type = 'bid' and order_book.price >= (
                select percentile_disc(0.75) within group (order by order_book.price)
                from order_book
                    where order_book.order_type = 'bid'
            )) or (order_book.order_type = 'ask' and order_book.price <= (
                select percentile_disc(0.25) within group (order by order_book.price)
                from order_book
                    where order_book.order_type = 'ask'
            ))
            union all
            select *, false as is_quartile from order_book
            order by timestamp desc
            """
        )
        return session.execute(query, {"timestamp": timestamp, "buy_sym_id": buy_sym_id.upper(), "sell_sym_id": sell_sym_id.upper(), "exchange": exchange.lower()})

    # TODO: add test!
    def _parse_order_books(self, order_books):
        full_order_book = []
        quartile_order_book = []
        order_books = list(order_books)
        if len(order_books) == 0:
            logging.debug("empty order book to be parsed")
            return None, None
        for order in order_books:
            if order[9] == True:
                # order is in quartile order book
                quartile_order_book.append(dict(
                    order_type=order[1],
                    price=order[2],
                    size=order[3]
                ))
            else:
                # order is in full order book
                full_order_book.append(dict(
                    order_type=order[1],
                    price=order[2],
                    size=order[3]
                ))
        logging.debug("ob_snapshot_generator - parsed order books: 'full order book' ({} orders), 'quartile order book' ({} orders)". format(len(full_order_book), len(quartile_order_book)))
        return full_order_book, quartile_order_book
        
    # TODO: add test!
    def _generate_snapshot(self, full_ob, quartile_ob, metadata):
        full_ob_stats = self._compute_stats(full_ob)
        quartile_ob_stats = self._compute_stats(quartile_ob)
        return models.OrderBookSnapshot(
            exchange_id=metadata["exchange_id"],
            sell_sym_id=metadata["sell_sym_id"],
            buy_sym_id=metadata["buy_sym_id"],
            timestamp=metadata["timestamp"],
            spread=full_ob_stats["spread"],
            bids_volume=full_ob_stats["bids_volume"], 
            asks_volume=full_ob_stats["asks_volume"],
            bids_count=full_ob_stats["bids_count"],
            asks_count=full_ob_stats["asks_count"],
            bids_price_stddev=full_ob_stats["bids_price_stddev"], 
            asks_price_stddev=full_ob_stats["asks_price_stddev"], 
            bids_price_mean=full_ob_stats["bids_price_mean"],
            asks_price_mean= full_ob_stats["asks_price_mean"],
            min_ask_price=full_ob_stats["min_ask_price"],
            min_ask_size=full_ob_stats["min_ask_size"],
            max_bid_price=full_ob_stats["max_bid_price"], 
            max_bid_size=full_ob_stats["max_bid_size"], 
            bid_price_median=full_ob_stats["bid_price_median"], 
            ask_price_median=full_ob_stats["ask_price_median"], 
            bid_price_upper_quartile=quartile_ob_stats["bid_price_upper_quartile"],
            ask_price_lower_quartile=quartile_ob_stats["ask_price_lower_quartile"],
            bids_volume_upper_quartile=quartile_ob_stats["bids_volume"],
            asks_volume_lower_quartile=quartile_ob_stats["asks_volume"],
            bids_count_upper_quartile=quartile_ob_stats["bids_count"],
            asks_count_lower_quartile=quartile_ob_stats["asks_count"],
            bids_price_stddev_upper_quartile=quartile_ob_stats["bids_price_stddev"],
            asks_price_stddev_lower_quartile=quartile_ob_stats["asks_price_stddev"],
            bids_price_mean_upper_quartile=quartile_ob_stats["bids_price_mean"],
            asks_price_mean_lower_quartile=quartile_ob_stats["asks_price_mean"]
        )

    def _compute_stats(self, order_book):
        bids = list(filter(lambda x: x["order_type"] == "bid", order_book))
        asks = list(filter(lambda x: x["order_type"] == "ask", order_book))
        bid_prices = list(map(lambda x: x["price"], bids))
        ask_prices = list(map(lambda x: x["price"], asks))
        avg_bid_price = float(sum(d['price'] for d in bids)) / len(bids)
        avg_ask_price = float(sum(d['price'] for d in asks)) / len(asks)
        bids_volume = float(sum(d['price']*d['size'] for d in bids))
        asks_volume = float(sum(d['price']*d['size'] for d in asks))
        min_ask_size = max(list(d['size'] if d['price'] == min(ask_prices) else 0 for d in asks))
        max_bid_size = max(list(d['size'] if d['price'] == max(bid_prices) else 0 for d in bids))
        spread = float(float(min(ask_prices)) - float(max(bid_prices)))
        return dict(
            spread=spread,
            min_ask_price=min(ask_prices),
            min_ask_size=min_ask_size,
            max_bid_price=max(bid_prices),
            max_bid_size=max_bid_size,
            bids_volume=bids_volume,
            asks_volume=asks_volume,
            bids_count = len(bids),
            asks_count = len(asks),
            bids_price_stddev = np.std(bid_prices),
            asks_price_stddev = np.std(ask_prices),
            bids_price_mean = avg_bid_price,
            asks_price_mean = avg_ask_price,
            bid_price_median = np.median(bid_prices),
            ask_price_median = np.median(ask_prices),
            # the two fields below are only accurate for already preprocessed quartile orderbooks
            bid_price_upper_quartile= min(bid_prices),
            ask_price_lower_quartile=max(ask_prices) 
        )
